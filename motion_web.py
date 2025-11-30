from gpiozero import MotionSensor
from flask import Flask
from datetime import datetime, timedelta
from pathlib import Path
import threading
import time

# ============================================================
# CONFIGURATION
# ============================================================
BIN_MINUTES = 5   
PIR_PIN = 17
LOG_RETENTION_DAYS = 90
# ============================================================

pir = MotionSensor(PIR_PIN)
app = Flask(__name__)

motion_events = []  # last ~48h of events for UI logic
last_motion = None

start_time = datetime.now()

# Logging
log_file_path = None
last_log_prune = None
# Track the *current day's* bin counts as they accumulate
current_day_bin_counts = {}  # key = bin_index, value = count
last_logged_bin = None       # bin_index that was last written to log


# ------------------------------------------------------------
# Logging helpers
# ------------------------------------------------------------

def init_log_file():
    """Create a new log file in the same folder as the script."""
    global log_file_path, last_log_prune

    script_dir = Path(__file__).resolve().parent
    ts = start_time.strftime("%Y%m%d_%H%M%S")
    log_file_path = script_dir / f"motion_log_{ts}.txt"

    with log_file_path.open("w", encoding="utf-8") as f:
        f.write("Motion bin log\n")
        f.write(f"Started: {start_time:%Y-%m-%d %H:%M:%S}\n")
        f.write(f"Retention: last {LOG_RETENTION_DAYS} days\n")
        f.write("Format: YYYY-MM-DD HH:MM - HH:MM: Detected NN motion events.\n")
        f.write("-------------------------------------------------------------\n")

    last_log_prune = start_time


def prune_log_file(now):
    """Remove log lines older than LOG_RETENTION_DAYS."""
    global last_log_prune
    if log_file_path is None:
        return

    # prune once per day (or if first time)
    if last_log_prune and (now - last_log_prune) < timedelta(days=1):
        return

    cutoff = now - timedelta(days=LOG_RETENTION_DAYS)
    cutoff_date_str = cutoff.strftime("%Y-%m-%d")

    lines_to_keep = []
    with log_file_path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line[:10].isdigit():
                # header or non-timestamp lines
                lines_to_keep.append(line)
                continue

            date_str = line[:10]
            if date_str >= cutoff_date_str:
                lines_to_keep.append(line)

    with log_file_path.open("w", encoding="utf-8") as f:
        f.writelines(lines_to_keep)

    last_log_prune = now


def write_bin_to_log(date, bin_index, count):
    """Append one finished bin to the log file."""
    if log_file_path is None:
        return

    start_minutes = bin_index * BIN_MINUTES
    end_minutes = start_minutes + BIN_MINUTES

    sh = start_minutes // 60
    sm = start_minutes % 60
    eh = (end_minutes // 60) % 24
    em = end_minutes % 60

    date_str = date.strftime("%Y-%m-%d")
    start_str = f"{sh:02d}:{sm:02d}"
    end_str   = f"{eh:02d}:{em:02d}"

    line = f"{date_str} {start_str} - {end_str}: Detected {count:2d} motion events.\n"
    with log_file_path.open("a", encoding="utf-8") as f:
        f.write(line)


def update_and_log_bins(now):
    """
    Called after every motion event AND periodically from watcher loop.
    Detects when a bin has finished and writes it to the log exactly once.
    """
    global last_logged_bin, current_day_bin_counts

    # Determine current bin index
    minute_of_day = now.hour * 60 + now.minute
    current_bin = minute_of_day // BIN_MINUTES

    # If it's a new day, flush remaining previous bins (if needed)
    if now.date() != start_time.date():
        # midnight rollover handling: log all bins before midnight (0 if missing)
        # Only do this the first time we see a new date
        start_time.replace(day=now.day)  # update start day tracking, simplified
        last_logged_bin = None
        current_day_bin_counts = {}

    # If we already logged bins up to last_logged_bin,
    # log any bins between last_logged_bin+1 and current_bin-1
    # These are now *finished* bins.
    if last_logged_bin is None:
        last_logged_bin = current_bin - 1

    while last_logged_bin < current_bin - 1:
        bin_to_write = last_logged_bin + 1
        # count may be zero if never hit
        count = current_day_bin_counts.get(bin_to_write, 0)
        write_bin_to_log(now.date(), bin_to_write, count)
        last_logged_bin = bin_to_write

    # prune occasionally
    prune_log_file(now)


# ------------------------------------------------------------
# Motion watcher
# ------------------------------------------------------------

def motion_watcher():
    global last_motion, motion_events, current_day_bin_counts

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

        # Increment count for THIS bin
        minute_of_day = now.hour * 60 + now.minute
        bin_index = minute_of_day // BIN_MINUTES
        current_day_bin_counts[bin_index] = current_day_bin_counts.get(bin_index, 0) + 1

        # Check whether any bins finished and log them
        update_and_log_bins(now)

        print("Motion detected at", now.strftime("%Y-%m-%d %H:%M:%S"))

        pir.wait_for_no_motion()
        print("No motion")


# ------------------------------------------------------------
# UI helpers (unchanged)
# ------------------------------------------------------------

def build_bins_html():
    """
    (UNCHANGED from previous version)
    Returns HTML for the 24h rolling per-bin UI.
    """

    now = datetime.now()
    window_start = now - timedelta(hours=24)

    recent_events = [t for t in motion_events if t >= window_start]

    minutes_per_day = 24 * 60
    num_bins = minutes_per_day // BIN_MINUTES

    # Aggregate by day and bin
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

    # Most recent 24h per-bin
    bin_display = {}
    for (day, idx), e in bin_day_info.items():
        cur = bin_display.get(idx)
        if cur is None or e["latest_dt"] > cur["latest_dt"]:
            bin_display[idx] = {
                "date": day,
                "count": e["count"],
                "latest_dt": e["latest_dt"]
            }

    html_parts = []

    def first_occ_after_start(h, m):
        first = start_time.replace(hour=h, minute=m, second=0, microsecond=0)
        if first < start_time:
            first += timedelta(days=1)
        return first if first <= now else None

    for i in range(num_bins):
        start_min = i * BIN_MINUTES
        end_min   = start_min + BIN_MINUTES

        sh, sm = divmod(start_min, 60)
        eh, em = divmod(end_min, 60)
        eh %= 24

        if i in bin_display:
            d = bin_display[i]["date"]
            count = bin_display[i]["count"]
            date_label = d.strftime("%Y-%m-%d")
        else:
            occ = first_occ_after_start(sh, sm)
            if occ:
                date_label = occ.date().strftime("%Y-%m-%d")
            else:
                date_label = "N/A"
            count = 0

        html_parts.append(
            f"<div class='row'>{date_label} "
            f"{sh:02d}:{sm:02d} - {eh:02d}:{em:02d}: "
            f"Detected {count:2d} motion events.</div>"
        )

        if (start_min + BIN_MINUTES) % 60 == 0 and i < num_bins - 1:
            html_parts.append("<hr class='hour-sep'>")

    return "".join(html_parts)


# ------------------------------------------------------------
# Flask UI
# ------------------------------------------------------------

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
    app.run(host="0.0.0.0", port=8080)
