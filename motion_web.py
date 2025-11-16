from gpiozero import MotionSensor
from flask import Flask
from datetime import datetime, timedelta
import threading
import time

# ============================================================
# CONFIGURATION: CHANGE BIN SIZE HERE
# ============================================================
BIN_MINUTES = 5   # <-- change this to 5, 10, 30, 60, etc.
# ============================================================

PIR_PIN = 17

pir = MotionSensor(PIR_PIN)
app = Flask(__name__)

motion_events = []
last_motion = None
start_time = datetime.now()   # first visible bin starts here


def floor_to_bin(dt: datetime) -> datetime:
    """Floor a datetime to the previous BIN_MINUTES boundary."""
    minute_block = (dt.minute // BIN_MINUTES) * BIN_MINUTES
    return dt.replace(minute=minute_block, second=0, microsecond=0)


def motion_watcher():
    global last_motion, motion_events
    print("PIR watcher started...")
    time.sleep(2)

    while True:
        pir.wait_for_motion()
        now = datetime.now()
        last_motion = now
        motion_events.append(now)

        # prune older than ~48h
        cutoff = now - timedelta(hours=48)
        motion_events[:] = [t for t in motion_events if t >= cutoff]

        print("Motion detected at", now.strftime("%Y-%m-%d %H:%M:%S"))

        pir.wait_for_no_motion()
        print("No motion")


def build_bins_html():
    """Build HTML listing motion counts in BIN_MINUTES bins."""
    now = datetime.now()

    # Only show from service start time or last 24h
    visible_start = max(start_time, now - timedelta(hours=24))

    recent_events = [t for t in motion_events if t >= visible_start]

    bin_length = timedelta(minutes=BIN_MINUTES)
    current_bin_start = floor_to_bin(now)
    start_bin = floor_to_bin(visible_start)

    html_parts = []
    bin_start = start_bin

    # We never exceed 24h worth of bins
    max_bins = int(24 * 60 / BIN_MINUTES)

    count_bins = 0
    while bin_start <= current_bin_start and count_bins < max_bins:
        bin_end = bin_start + bin_length
        display_end = min(bin_end, now)

        count = sum(1 for t in recent_events if bin_start <= t < bin_end)

        date_str = bin_start.strftime("%Y-%m-%d")
        start_str = bin_start.strftime("%H:%M")
        end_str = display_end.strftime("%H:%M")

        line = f"{date_str} {start_str} - {end_str}: Detected {count:2d} motion events."
        html_parts.append(f"<div class='row'>{line}</div>")

        # Separator rule at the end of each hour
        if (bin_start.minute + BIN_MINUTES) % 60 == 0 and bin_start < current_bin_start:
            html_parts.append("<hr class='hour-sep'>")

        bin_start += bin_length
        count_bins += 1

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
          h1 {{
            margin-bottom: 0.3rem;
          }}
          .subtitle {{
            color: #555;
            margin-bottom: 1rem;
          }}
          .box {{
            border: 1px solid #ccc;
            border-radius: 8px;
            padding: 1rem 1.5rem;
            max-width: 650px;
            background: #fafafa;
            text-align: left;
            font-family: monospace;
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
        <div class="subtitle">
          Activity in {BIN_MINUTES}-minute bins since service start (max last 24h).
          Page refreshes every 30 seconds.
        </div>
        <div class="box">
          {bins_html}
        </div>
      </body>
    </html>
    """


if __name__ == "__main__":
    t = threading.Thread(target=motion_watcher, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=8080)
