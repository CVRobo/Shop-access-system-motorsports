import os
import time
import pandas as pd
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# choose RealPN532 when on the Pi
from real_pn532 import RealPN532

# real SSD1306 display (example)
import board, busio
from adafruit_ssd1306 import SSD1306_I2C
from PIL import Image, ImageDraw, ImageFont

# --------------------------
# Environment
# --------------------------
load_dotenv()
SLACK_TOKEN = os.getenv("SLACK_TOKEN")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "#shop-status")  # default if not set

# --------------------------
# Slack helper
# --------------------------
client = WebClient(token=SLACK_TOKEN)

def send_slack_message(msg):
    try:
        client.chat_postMessage(channel=SLACK_CHANNEL, text=msg)
    except SlackApiError as e:
        print("Slack error:", e.response["error"])

# --------------------------
# Display classes
# --------------------------
class OLEDDisplay:
    def __init__(self):
        i2c = busio.I2C(board.SCL, board.SDA)
        self.oled = SSD1306_I2C(128, 64, i2c)
        self.oled.fill(0)
        self.oled.show()
        # blank image for drawing
        self.image = Image.new("1", (128, 64))
        self.draw = ImageDraw.Draw(self.image)
        self.font = ImageFont.load_default()

    def show_text(self, text, duration=3):
        self.draw.rectangle((0, 0, 128, 64), outline=0, fill=0)
        self.draw.text((0, 0), text, font=self.font, fill=255)
        self.oled.image(self.image)
        self.oled.show()
        time.sleep(duration)

# fallback: if OLED not wired yet
class MockDisplay:
    def show_text(self, text, duration=0):
        print("[OLED] " + text)
        if duration:
            time.sleep(duration)

# --------------------------
# Initialize hardware
# --------------------------
pn532 = RealPN532(debug=False)
# display = OLEDDisplay()   # uncomment if OLED wired
display = MockDisplay()      # fallback for testing

# --------------------------
# Load members CSV
# --------------------------
members = pd.read_csv("members.csv", dtype=str)
members["card_uid"] = members["card_uid"].str.upper().str.strip()

logged_in = set()

# --------------------------
# Main loop
# --------------------------
print("Waiting for cards. Press Ctrl+C to exit.")
try:
    while True:
        uid = pn532.read_passive_target()
        if not uid:
            continue

        uid = uid.upper().strip()
        row = members[members["card_uid"] == uid]
        if row.empty:
            display.show_text("Unknown card")
            continue

        name = row.iloc[0]["name"]

        if uid in logged_in:
            logged_in.remove(uid)
            display.show_text(f"Goodbye {name}", duration=2)
            send_slack_message(f"{name} checked out. {len(logged_in)} in shop.")
            if len(logged_in) == 0:
                send_slack_message("🏁 Shop closed.")
        else:
            was_empty = len(logged_in) == 0
            logged_in.add(uid)
            display.show_text(f"Welcome {name}", duration=2)
            if was_empty:
                send_slack_message("🚀 Shop open!")
            send_slack_message(f"{name} checked in. {len(logged_in)} in shop.")

except KeyboardInterrupt:
    print("Exiting.")
finally:
    pn532.close()
