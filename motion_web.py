from gpiozero import MotionSensor
from flask import Flask
from datetime import datetime
import threading
import time

# GPIO pin for PIR
PIR_PIN = 17

pir = MotionSensor(PIR_PIN)
app = Flask(__name__)

last_motion = None  # will store datetime of last motion


def motion_watcher():
    global last_motion
    print("PIR watcher started, waiting for motion...")
    # give sensor time to stabilise
    time.sleep(2)
    while True:
        pir.wait_for_motion()
        last_motion = datetime.now()
        print("Motion detected at", last_motion.strftime("%Y-%m-%d %H:%M:%S"))
        # wait until no motion to avoid spamming
        pir.wait_for_no_motion()
        print("No motion")


@app.route("/")
def index():
    if last_motion is None:
        msg = "No motion detected yet."
    else:
        msg = last_motion.strftime("%Y-%m-%d %H:%M:%S")
    # very simple HTML with auto-refresh
    return f"""
    <html>
      <head>
        <title>Motion Sensor</title>
        <meta http-equiv="refresh" content="5">
        <style>
          body {{
            font-family: sans-serif;
            margin: 2rem;
          }}
          .card {{
            border: 1px solid #ccc;
            border-radius: 8px;
            padding: 1.5rem;
            max-width: 400px;
          }}
          .label {{
            color: #555;
            margin-bottom: 0.5rem;
          }}
          .time {{
            font-size: 1.5rem;
            font-weight: bold;
          }}
        </style>
      </head>
      <body>
        <div class="card">
          <div class="label">Last motion detected:</div>
          <div class="time">{msg}</div>
        </div>
      </body>
    </html>
    """


if __name__ == "__main__":
    # run motion watcher in background thread
    t = threading.Thread(target=motion_watcher, daemon=True)
    t.start()

    # run web server on port 8080, accessible from anywhere via cloudflared
    app.run(host="0.0.0.0", port=8080)
