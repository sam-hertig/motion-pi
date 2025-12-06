from gpiozero import MotionSensor
from flask import Flask
from datetime import datetime, timedelta, date
from pathlib import Path
import threading
import time
import subprocess

# ============================================================
# CONFIGURATION
# ============================================================
BIN_MINUTES = 5   # change as needed
PIR_PIN = 17
LOG_RETENTION_DAYS = 90

# Network monitoring
NETWORK_CHECK_INTERVAL = 60  # seconds between checks
EXTERNAL_IP = "1.1.1.1"      # external host for internet reachability

# Bin logging
BIN_FLUSH_INTERVAL = 2       # seconds between checks for finished bins

# Pruning
PRUNE_CHECK_INTERVAL = 60    # check once per minute whether it's time to prune
PRUNE_AT_HOUR = 2            # run daily prune at ~02:00 local time
# ============================================================

pir = MotionSensor(PIR_PIN)
app = Flask(__name__)

motion_events = []  # last ~48h of events for UI logic
last_motion = None

start_time = datetime.now()

# Logging
log_file_path = None
last_log_prune_at = None

# Network monitoring globals
ROUTER_IP = None
network_state = None  # "NO_ROUTER_INFO", "LAN_DOWN", "LAN_UP_INTERNET_DOWN", "INTERNET_UP"

# Bin logging state (shared between threads)
bin_lock = threading.Lock()
log_lock = threading.Lock()

active_date = start_time.date()           # date whose bins we're currently counting
current_day_bin_counts = {}               # bin_index -> count, for active_date
start_bin_index = (start_time.hour * 60 + start_time.minute) // BIN_MINUTES
last_logged_bin = start_bin_index - 1     # last bin written for active_date


# ------------------------------------------------------------
# Helper: detect router IP
# ------------------------------------------------------------

def get_router_ip():
    """Try to detect the default router IP using `ip route`."""
    try:
        out = subprocess.check_output(["ip", "route"], stderr=subprocess.DEVNULL).decode()
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("default via "):
                parts = line.split()
                if len(parts) >= 3:
                    return parts[2]
    except Exception:
        pass
    return None


# ------------------------------------------------------------
# Log file helpers
# ------------------------------------------------------------

def init_log_file():
    """Create a new log file in the same folder as the script."""
    global log_file_path, last_log_prune_at, ROUTER_IP

    script_dir = Path(__file__).resolve().parent
    ts = start_time.strftime("%Y%m%d_%H%M%S")
    log_file_path = script_dir / f"motion_log_{ts}.txt"

    ROUTER_IP = get_router_ip()

    with log_lock:
        with log_file_path.open("w", encoding="utf-8") as f:
            f.write("Motion bin log\n")
            f.write(f"Started: {start_time:%Y-%m-%d %H:%M:%S}\n")
            f.write(f"Retention: last {LOG_RETENTION_DAYS} days\n")
            if ROUTER_IP:
                f.write(f"Router IP detected: {ROUTER_IP}\n")
            else:
                f.write("Router IP detected: UNKNOWN (could not detect default gateway)\n")
            f.write(f"External IP used for internet check: {EXTERNAL_IP}\n")
            f.write("Format: YYYY-MM-DD HH:MM - HH:MM: Detected NN motion events.\n")
            f.write("-------------------------------------------------------------\n")

    last_log_prune_at = None


def write_bin_to_log(day: date, bin_index: int, count: int):
    """Append one finished bin to the log file."""
    if log_file_path is None:
        return

    start_minutes = bin_index * BIN_MINUTES
    end_minutes = start_minutes + BIN_MINUTES

    sh = start_minutes // 60
    sm = start_minutes % 60
    eh = (end_minutes // 60) % 24
    em = end_minutes % 60

    date_str = day.strftime("%Y-%m-%d")
    start_str = f"{sh:02d}:{sm:02d}"
    end_str = f"{eh:02d}:{em:02d}"

    line = f"{date_str} {start_str} - {end_str}: Detected {count:2d} motion events.\n"
    with log_lock:
        with log_file_path.open("a", encoding="utf-8") as f:
            f.write(line)


def log_network_event(message: str):
    """Append a network-related line to the log file."""
    if log_file_path is None:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} NET: {message}\n"
    with log_lock:
        with log_file_path.open("a", encoding="utf-8") as f:
            f.write(line)


def log_prune_event(removed: int, kept: int, cutoff_dt: datetime):
    """Append a prune summary line after a prune run."""
    if log_file_path is None:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = (
        f"{ts} PRUNE: Removed {removed} lines older than {LOG_RETENTION_DAYS} days "
        f"(cutoff {cutoff_dt:%Y-%m-%d %H:%M:%S}). Kept {kept} lines.\n"
    )
    with log_lock:
        with log_file_path.open("a", encoding="utf-8") as f:
            f.write(line)


# ------------------------------------------------------------
# Robust pruning (true “older than N days”)
# ------------------------------------------------------------

def _parse_line_timestamp(line: str):
    """
    Returns a datetime for timestamped lines, or None.

    Supported:
    - NET/PRUNE/etc: "YYYY-MM-DD HH:MM:SS ..."
    - Bin lines:     "YYYY-MM-DD HH:MM - HH:MM: ..."
      (uses bin START time as the line timestamp)
    """
    if len(line) < 16 or not line[:10].isdigit():
        return None

    # Case 1: full timestamp with seconds
    # "YYYY-MM-DD HH:MM:SS"
    if len(line) >= 19 and line[4] == "-" and line[7] == "-" and line[10] == " " and line[13] == ":" and line[16] == ":":
        try:
            return datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass

    # Case 2: bin line timestamp (start time)
    # "YYYY-MM-DD HH:MM -"
    if len(line) >= 18 and line[4] == "-" and line[7] == "-" and line[10] == " " and line[13] == ":" and line[16:18] == " -":
        try:
            return datetime.strptime(line[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            pass

    return None


def prune_log_file(force: bool = False):
    """
    Remove log lines older than LOG_RETENTION_DAYS (true age).

    - Keeps header lines (non-timestamped lines).
    - Removes timestamped lines with timestamp < cutoff_dt.
    - Logs a PRUNE summary line after successful pruning.
    """
    global last_log_prune_at
    if log_file_path is None:
        return

    now = datetime.now()
    if not force and last_log_prune_at is not None:
        # Don’t prune too frequently unless forced
        if (now - last_log_prune_at) < timedelta(hours=12):
            return

    cutoff_dt = now - timedelta(days=LOG_RETENTION_DAYS)

    with log_lock:
        try:
            with log_file_path.open("r", encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return

        kept_lines = []
        removed = 0
        kept_timestamped = 0

        for line in lines:
            ts = _parse_line_timestamp(line)
            if ts is None:
                # header / unknown -> keep
                kept_lines.append(line)
                continue

            if ts >= cutoff_dt:
                kept_lines.append(line)
                kept_timestamped += 1
            else:
                removed += 1

        with log_file_path.open("w", encoding="utf-8") as f:
            f.writelines(kept_lines)

    last_log_prune_at = now
    # PRUNE summary is appended (so it will always exist even after rewrite)
    log_prune_event(removed=removed, kept=kept_timestamped, cutoff_dt=cutoff_dt)


def prune_watcher():
    """
    Background thread that runs pruning daily (and once shortly after startup).
    """
    # Run a prune soon after startup so retention is enforced even if we rebooted late
    time.sleep(5)
    prune_log_file(force=True)

    while True:
        now = datetime.now()
        # Run at PRUNE_AT_HOUR each day (best-effort)
        if now.hour == PRUNE_AT_HOUR:
            prune_log_file(force=True)
            # avoid re-running multiple times during the same hour
            time.sleep(3600)
            continue
        time.sleep(PRUNE_CHECK_INTERVAL)


# ------------------------------------------------------------
# Bin logging core (independent of motion)
# ------------------------------------------------------------

def flush_finished_bins(now: datetime):
    """
    Write out any bins that have finished since last_logged_bin.
    Ensures bins are logged even with 0 motion and during quiet periods.
    """
    global active_date, current_day_bin_counts, last_logged_bin

    minutes_per_day = 24 * 60
    num_bins = minutes_per_day // BIN_MINUTES
    current_bin = (now.hour * 60 + now.minute) // BIN_MINUTES

    # Day rollover: flush remaining bins of the previous day.
    if now.date() != active_date:
        for b in range(last_logged_bin + 1, num_bins):
            count = current_day_bin_counts.get(b, 0)
            write_bin_to_log(active_date, b, count)

        active_date = now.date()
        current_day_bin_counts = {}
        last_logged_bin = -1

    # For current day: bins < current_bin are finished (current_bin is in-progress)
    target_last = current_bin - 1
    if target_last > last_logged_bin:
        for b in range(last_logged_bin + 1, target_last + 1):
            count = current_day_bin_counts.get(b, 0)
            write_bin_to_log(active_date, b, count)
        last_logged_bin = target_last


def bin_logger():
    """Background thread that continuously flushes finished bins (even during no motion)."""
    while True:
        now = datetime.now()
        with bin_lock:
            flush_finished_bins(now)
        time.sleep(BIN_FLUSH_INTERVAL)


# ------------------------------------------------------------
# Motion watcher (ONLY increments per-bin counts; bin_logger does the flushing)
# ------------------------------------------------------------

def motion_watcher():
    global last_motion, motion_events, active_date, current_day_bin_counts

    print("PIR watcher started...")
    time.sleep(2)

    while True:
        pir.wait_for_motion()
        now = datetime.now()
        last_motion = now
        motion_events.append(now)

        # Keep memory short (only last 48 hours)
        cutoff_mem = now - timedelta(hours=48)
        motion_events[:] = [t for t in motion_events if t >= cutoff_mem]

        # Increment count for THIS bin (for the correct day)
        with bin_lock:
            flush_finished_bins(now)
            minute_of_day = now.hour * 60 + now.minute
            bin_index = minute_of_day // BIN_MINUTES
            current_day_bin_counts[bin_index] = current_day_bin_counts.get(bin_index, 0) + 1

        print("Motion detected at", now.strftime("%Y-%m-%d %H:%M:%S"))
        pir.wait_for_no_motion()
        print("No motion")


# ------------------------------------------------------------
# Network monitoring
# ------------------------------------------------------------

def ping_host(host, timeout=1):
    """Return True if host responds to a single ping, else False."""
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except Exception:
        return False


def network_watcher():
    """Background thread that monitors LAN + internet reachability and logs only on state changes."""
    global ROUTER_IP, network_state

    prev_state = None

    while True:
        if ROUTER_IP is None:
            router_ip = get_router_ip()
            if router_ip is not None:
                ROUTER_IP = router_ip
                log_network_event(f"Detected router IP: {ROUTER_IP}")

        router_ok = False
        external_ok = False

        if ROUTER_IP:
            router_ok = ping_host(ROUTER_IP)

        if router_ok:
            external_ok = ping_host(EXTERNAL_IP)

        if not ROUTER_IP:
            state = "NO_ROUTER_INFO"
            msg = "Router IP unknown; cannot perform network checks."
        elif not router_ok:
            state = "LAN_DOWN"
            msg = f"Router unreachable ({ROUTER_IP}). Network DOWN."
        elif not external_ok:
            state = "LAN_UP_INTERNET_DOWN"
            msg = f"Router reachable ({ROUTER_IP}) but external host {EXTERNAL_IP} unreachable. Internet DOWN."
        else:
            state = "INTERNET_UP"
            msg = f"Router reachable ({ROUTER_IP}) and external host {EXTERNAL_IP} reachable. Network UP."

        if state != prev_state:
            log_network_event(msg)
            prev_state = state

        network_state = state
        time.sleep(NETWORK_CHECK_INTERVAL)


# ------------------------------------------------------------
# UI helpers (24h rolling view)
# ------------------------------------------------------------

def build_bins_html():
    now = datetime.now()
    window_start = now - timedelta(hours=24)

    recent_events = [t for t in motion_events if t >= window_start]

    minutes_per_day = 24 * 60
    num_bins = minutes_per_day // BIN_MINUTES

    # Aggregate by (date, bin_index): count + latest event time
    bin_day_info = {}
    for t in recent_events:
        minute = t.hour * 60 + t.minute
        idx = minute // BIN_MINUTES
        key = (t.date(), idx)
        e = bin_day_info.get(key)
        if e is None:
            bin_day_info[key] = {"count": 1, "latest_dt": t}
        else:
            e["count"] += 1
            if t > e["latest_dt"]:
                e["latest_dt"] = t

    # For each bin index, pick the most recent day with activity in last 24h
    bin_display = {}
    for (day, idx), e in bin_day_info.items():
        cur = bin_display.get(idx)
        if cur is None or e["latest_dt"] > cur["latest_dt"]:
            bin_display[idx] = {"date": day, "count": e["count"], "latest_dt": e["latest_dt"]}

    html_parts = []

    def latest_occurrence_since_start(h, m):
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate > now:
            candidate -= timedelta(days=1)
        if candidate < start_time:
            return None
        return candidate

    for i in range(num_bins):
        start_min = i * BIN_MINUTES
        end_min = start_min + BIN_MINUTES

        sh, sm = divmod(start_min, 60)
        eh, em = divmod(end_min, 60)
        eh %= 24

        if i in bin_display:
            d = bin_display[i]["date"]
            count = bin_display[i]["count"]
            date_label = d.strftime("%Y-%m-%d")
        else:
            occ = latest_occurrence_since_start(sh, sm)
            date_label = occ.date().strftime("%Y-%m-%d") if occ else "N/A"
            count = 0

        html_parts.append(
            f"<div class='row'>{date_label} "
            f"{sh:02d}:{sm:02d} - {eh:02d}:{em:02d}: "
            f"Detected {count:2d} motion events.</div>"
        )

        if (start_min + BIN_MINUTES) % 60 == 0 and i < num_bins - 1:
            html_parts.append("<hr class='hour-sep'>")

    return "".join(html_parts)


@app.route("/")
def index():
    bins_html = build_bins_html()
    return f"""
    <html>
      <head>
        <title>Motion Activity</title>
        <meta http-equiv="refresh" content="30">
        <style>
          body {{
            font-family: sans-serif;
            margin: 2rem;
            line-height: 1.4;
          }}
          .row {{
            margin: 2px 0;
            white-space: pre;
          }}
          .hour-sep {{
            border: none;
            border-top: 1px solid #ccc;
            margin: 6px 0;
          }}
        </style>
      </head>
      <body>
        <h1>Motion Activity</h1>
        <div class='box'>{bins_html}</div>
      </body>
    </html>
    """


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

if __name__ == "__main__":
    init_log_file()

    t = threading.Thread(target=motion_watcher, daemon=True)
    t.start()

    bin_t = threading.Thread(target=bin_logger, daemon=True)
    bin_t.start()

    net_t = threading.Thread(target=network_watcher, daemon=True)
    net_t.start()

    prune_t = threading.Thread(target=prune_watcher, daemon=True)
    prune_t.start()

    app.run(host="0.0.0.0", port=8080)
