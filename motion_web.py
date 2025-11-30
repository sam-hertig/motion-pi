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

# Remember when the script started, so we know which bins
# have ever had the chance to register events.
start_time = datetime.now()


def motion_watcher():
    global last_motion, motion_events
    print("PIR watcher started...")
    time.sleep(2)

    while True:
        pir.wait_for_motion()
        now = datetime.now()
        last_motion = now
        motion_events.append(now)

        # Keep a bounded history: 48h is enough to compute "last 24h"
        cutoff = now - timedelta(hours=48)
        motion_events[:] = [t for t in motion_events if t >= cutoff]

        print("Motion detected at", now.strftime("%Y-%m-%d %H:%M:%S"))

        pir.wait_for_no_motion()
        print("No motion")


def build_bins_html():
    """
    Build HTML listing motion counts in BIN_MINUTES bins.

    - Always shows 24h worth of bins aligned to time-of-day, e.g. for 5-minute bins:
      00:00–00:05, 00:05–00:10, ..., 23:55–00:00.
    - For each bin (time-of-day slot), we look at events from the LAST 24 HOURS.
      If there were events in that bin, we show the most recent day for that bin
      and the count of events on that day.
    - If there were NO events in that bin in the last 24h:
        * If the bin's time window has ALREADY occurred at least once
          since the script started, we show 0 and the date of the FIRST time
          that bin occurred since script start.
        * If the bin's time window has NEVER occurred since the script
          started, we show 0 and date "N/A".
    """
    now = datetime.now()
    window_start = now - timedelta(hours=24)

    # Only consider events from the last 24 hours for "last recorded" values
    recent_events = [t for t in motion_events if t >= window_start]

    minutes_per_day = 24 * 60
    num_bins = minutes_per_day // BIN_MINUTES

    # Aggregate by (date, bin_index): count + latest event time for that day/bin
    # key: (date, bin_index) -> {"count": int, "latest_dt": datetime}
    bin_day_info = {}

    for t in recent_events:
        # Determine bin index from time-of-day
        minute_of_day = t.hour * 60 + t.minute
        bin_index = minute_of_day // BIN_MINUTES  # 0 .. num_bins-1

        key = (t.date(), bin_index)
        entry = bin_day_info.get(key)
        if entry is None:
            bin_day_info[key] = {"count": 1, "latest_dt": t}
        else:
            entry["count"] += 1
            if t > entry["latest_dt"]:
                entry["latest_dt"] = t

    # For each bin index, pick the most recent (date, count) over the last 24h
    # bin_display[bin_index] = {
    #     "date": date,
    #     "count": int,
    #     "latest_dt": datetime
    # }
    bin_display = {}

    for (day, idx), entry in bin_day_info.items():
        current = bin_display.get(idx)
        if current is None or entry["latest_dt"] > current["latest_dt"]:
            bin_display[idx] = {
                "date": day,
                "count": entry["count"],
                "latest_dt": entry["latest_dt"],
            }

    html_parts = []

    # Helper: determine if a bin's time window has ever occurred since script start,
    # and if so, the first time that window started since script start.
    def first_occurrence_since_start(start_hour: int, start_minute: int):
        """
        Returns:
            first_start (datetime) if the bin's [start_hour:start_minute]
            has occurred at least once since script start, else None.
        """
        # First candidate on the start day
        first_candidate = start_time.replace(
            hour=start_hour, minute=start_minute, second=0, microsecond=0
        )
        if first_candidate < start_time:
            # We already passed this slot on the start day, so the first
            # occurrence is on the next day
            first_candidate += timedelta(days=1)

        if first_candidate <= now:
            return first_candidate
        else:
            return None

    for bin_index in range(num_bins):
        # Compute time-of-day range for this bin index
        start_minutes = bin_index * BIN_MINUTES
        end_minutes = start_minutes + BIN_MINUTES

        start_hour = start_minutes // 60
        start_minute = start_minutes % 60

        # End hour/minute, wrapping at 24:00 -> 00:00
        end_hour = (end_minutes // 60) % 24
        end_minute = end_minutes % 60

        # 1) If we have recorded events for this bin in the last 24h:
        info = bin_display.get(bin_index)
        if info is not None:
            date_for_bin = info["date"]
            count = info["count"]
            date_label = date_for_bin.strftime("%Y-%m-%d")
        else:
            # 2) No events in last 24h for this bin.
            #    Check whether its time window has occurred since script start.
            first_occ = first_occurrence_since_start(start_hour, start_minute)

            if first_occ is not None:
                # The bin window has occurred at least once since the script started,
                # but there were no events for that bin in the last 24h.
                count = 0
                date_label = first_occ.date().strftime("%Y-%m-%d")
            else:
                # The bin window has NEVER occurred since the script started:
                # show 0 and N/A so it's obvious this time-of-day hasn't been reached yet.
                count = 0
                date_label = "N/A"

        start_str = f"{start_hour:02d}:{start_minute:02d}"
        end_str = f"{end_hour:02d}:{end_minute:02d}"

        line = (
            f"{date_label} {start_str} - {end_str}: "
            f"Detected {count:2d} motion events."
        )
        html_parts.append(f"<div class='row'>{line}</div>")

        # Separator at the end of each hour, except after the very last bin
        if (start_minutes + BIN_MINUTES) % 60 == 0 and bin_index < num_bins - 1:
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
          24-hour view in {BIN_MINUTES}-minute bins, aligned to time-of-day (00:00 → 23:59).<br>
          Each bin shows the last recorded number of motion events for that bin
          within the past 24 hours, plus the date of that last activity.<br>
          If a bin's time window has never occurred since the script started,
          its date is shown as "N/A".<br>
          Auto-refreshes every 30 seconds.
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
