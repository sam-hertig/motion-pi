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
# ============================================================

pir = MotionSensor(PIR_PIN)
app = Flask(__name__)

motion_events = []  # last ~48h of events for UI logic
last_motion = None

start_time = datetime.now()

# Logging
log_file_path = None
last_log_prune = None

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
    global log_file_path, last_log_prune, ROUTER_IP

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

    last_log_prune = start_time


def prune_log_file(now):
    """Remove log lines older than LOG_RETENTION_DAYS (by date prefix)."""
    global last_log_prune
    if log_file_path is None:
        return

    # prune once per day
    if last_log_prune and (now - last_log_prune) < timedelta(days=1):
        return

    cutoff = now - timedelta(days=LOG_RETENTION_DAYS)
    cutoff_date_str = cutoff.strftime("%Y-%m-%d")

    with log_lock:
        lines_to_keep = []
        with log_file_path.open("r", encoding="utf-8") as f:
            for line in f:
                # Keep headers / non-date lines
                if not line[:10].isdigit():
                    lines_to_keep.append(line)
                    continue

                date_str = line[:10]
                if date_str >= cutoff_date_str:
                    lines_to_keep.append(line)

        with log_file_path.open("w", encoding="utf-8") as f:
            f.writelines(lines_to_keep)

    last_log_prune = now


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


# ------------------------------------------------------------
# Bin logging core (independent of motion)
# ------------------------------------------------------------

def flush_finished_bins(now: datetime):
    """
    Write out any bins that have finished since last_logged_bin.
    This is what ensures bins are logged even with 0 motion and during quiet periods.
    """
    global active_date, current_day_bin_counts, last_logged_bin

    minutes_per_day = 24 * 60
    num_bins = minutes_per_day // BIN_MINUTES
    current_bin = (now.hour * 60 + now.minute) // BIN_MINUTES

    # Handle day rollover: if we are now on a later date than active_date,
    # flush the remaining bins of the old active_date completely.
    if now.date() != active_date:
        # 1) Flush remaining bins for old active_date (from last_logged_bin+1 to last bin)
        for b in range(last_logged_bin + 1, num_bins):
            count = current_day_bin_counts.get(b, 0)
            write_bin_to_log(active_date, b, count)

        # 2) Reset state for the new day
        active_date = now.date()
        current_day_bin_counts = {}
        last_logged_bin = -1  # no finished bins written yet for the new day

        # Also prune once per day around rollover
        prune_log_file(now)

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
            # Ensure bin state is on the correct date (in case midnight passed)
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
    """
    Background thread that monitors LAN + internet reachability
    and logs only on state changes.
    """
    global ROUTER_IP, network_state

    prev_state = None

    while True:
        # If router IP is still unknown, try to detect it again
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
# UI helpers (24h rolling view) — unchanged
# ------------------------------------------------------------

def build_bins_html():
    """
    24h view in BIN_MINUTES bins, aligned to time-of-day.

    Per bin:
    - If there were events in the last 24h for that bin: show the most recent day with activity and that count.
    - If NO events in last 24h: still show the bin with count 0.
        * If that bin time-of-day has occurred at least once since script start: show the most recent occurrence date.
        * Otherwise: show N/A.
    """
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

    app.run(host="0.0.0.0", port=8080)
