import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst

import hailo
from hailo_apps_infra.hailo_rpi_common import app_callback_class
from hailo_apps_infra.detection_pipeline_simple import GStreamerDetectionApp

from hackerbot import Hackerbot
import atexit
import time
import threading
import queue

# ========== Hackerbot Setup ==========
bot = Hackerbot()
bot.head.look(180, 220, 70)

# ========== Queues for Movement & Speech ==========
movement_queue = queue.Queue(maxsize=1)
speech_queue = queue.Queue()
missed_frames = 0

# ========== Cleanup ==========
def clean_up():
    print("Cleaning up: stopping robot...")
    bot.base.drive(0, 0, block=False)
    bot.base.destroy(auto_dock=False)

atexit.register(clean_up)

# ========== PID Control Params ==========
KP = 200.0
CENTER = 0.5
THRESHOLD = 0.05
MAX_TURN = 75
BACKWARD_SPEED = -75
MAX_MISSES = 10
STOP_HEIGHT_THRESHOLD = 0.4
TOO_CLOSE_HEIGHT_THRESHOLD = 0.6
CENTER_TOLERANCE = 0.08
MAX_FORWARD_SPEED = 150
MIN_FORWARD_SPEED = 40

# ========== Speed Computation ==========
def compute_forward_speed(height):
    if height >= TOO_CLOSE_HEIGHT_THRESHOLD:
        return 0
    elif height <= 0.1:
        return MAX_FORWARD_SPEED
    else:
        scale = 1.0 - (height - 0.1) / (TOO_CLOSE_HEIGHT_THRESHOLD - 0.1)
        return int(MIN_FORWARD_SPEED + scale * (MAX_FORWARD_SPEED - MIN_FORWARD_SPEED))

# ========== Movement Logic ==========
def move_bot_towards(data):
    if data is None:
        print("Rotate due to lost person")
        bot.base.drive(0, 20, block=False)
        return

    x_center, height = data

    if height > TOO_CLOSE_HEIGHT_THRESHOLD and abs(x_center - CENTER) < CENTER_TOLERANCE:
        print("Person is too close — moving back")
        bot.base.drive(BACKWARD_SPEED, 0, block=False)
        speech_queue.put("Stay back!")
        return

    if height > STOP_HEIGHT_THRESHOLD and abs(x_center - CENTER) < CENTER_TOLERANCE:
        print("Person is close and centered — stopping")
        bot.base.drive(0, 0, block=False)
        speech_queue.put("Got you!")
        return

    error = x_center - CENTER
    forward_speed = compute_forward_speed(height)

    if abs(error) < THRESHOLD:
        print(f"Aligned — moving forward at {forward_speed}")
        bot.base.drive(forward_speed, 0, block=False)
        # speech_queue.put("GOT you!")
    else:
        turn = int(KP * error)
        turn = max(min(turn, MAX_TURN), -MAX_TURN)
        forward = forward_speed if abs(error) < 0.15 else 0
        direction = "left" if turn > 0 else "right"
        print(f"Turning {direction} — turn: {turn}, forward: {forward}")
        bot.base.drive(forward, turn, block=False)

# ========== Background Threads ==========
def movement_worker():
    while True:
        try:
            data = movement_queue.get(timeout=1.0)
            move_bot_towards(data)
        except queue.Empty:
            continue

def speech_worker():
    last_spoken = ""
    last_time = 0

    def speak_async(text):
        try:
            bot.base.speak(model_src="en_GB-semaine-medium", text=text, speaker_id=None)
        except Exception as e:
            print(f"Speech error: {e}")

    while True:
        text = speech_queue.get()
        now = time.time()

        # Only speak if the phrase is new or enough time has passed
        if text != last_spoken or (now - last_time) > 5:
            threading.Thread(target=speak_async, args=(text,), daemon=True).start()
            last_spoken = text
            last_time = now

threading.Thread(target=movement_worker, daemon=True).start()
threading.Thread(target=speech_worker, daemon=True).start()

# ========== Frame Counter Class ==========
class user_app_callback_class(app_callback_class):
    def __init__(self):
        super().__init__()

# ========== GStreamer Callback ==========
def app_callback(pad, info, user_data):
    global missed_frames

    buffer = info.get_buffer()
    if buffer is None:
        return Gst.PadProbeReturn.OK

    detections = hailo.get_roi_from_buffer(buffer).get_objects_typed(hailo.HAILO_DETECTION)

    found_person = False
    for detection in detections:
        if detection.get_label() == "person":
            bbox = detection.get_bbox()
            x_center = (bbox.xmin() + bbox.xmax()) / 2.0
            height = bbox.ymax() - bbox.ymin()
            confidence = detection.get_confidence()

            print(f"[{time.strftime('%H:%M:%S')}] Person: X={x_center:.2f}, Height={height:.2f}, Confidence={confidence:.2f}")

            if movement_queue.empty():
                movement_queue.put((x_center, height))

            missed_frames = 0
            found_person = True
            break

    if not found_person:
        missed_frames += 1
        if missed_frames >= MAX_MISSES:
            if movement_queue.empty():
                movement_queue.put(None)

    return Gst.PadProbeReturn.OK

# ========== Run Detection App ==========
if __name__ == "__main__":
    user_data = user_app_callback_class()
    app = GStreamerDetectionApp(app_callback, user_data)
    try:
        app.run()
    finally:
        clean_up()
