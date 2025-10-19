import time
import pandas as pd
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# choose RealPN532 when on the Pi
from real_pn532 import RealPN532

# real SSD1306 display (example)
import board, busio
from adafruit_ssd1306 import SSD1306_I2C
from PIL import Image, ImageDraw, ImageFont

class OLEDDisplay:
    def __init__(self):
        i2c = busio.I2C(board.SCL, board.SDA)
        self.oled = SSD1306_I2C(128, 64, i2c)
        self.oled.fill(0)
        self.oled.show()
        # create blank image for drawing
        self.image = Image.new("1", (128, 64))
        self.draw = ImageDraw.Draw(self.image)
        self.font = ImageFont.load_default()

    def show_text(self, text, duration=3):
        self.draw.rectangle((0, 0, 128, 64), outline=0, fill=0)
        self.draw.text((0, 0), text, font=self.font, fill=255)
        self.oled.image(self.image)
        self.oled.show()
        # keep it on for duration seconds
        time.sleep(duration)

# fallback: if you don't have the OLED wired yet:
class MockDisplay:
    def show_text(self, text, duration=0):
        print("[OLED] " + text)
        if duration:
            time.sleep(duration)


# --------------------------
# Init hardware & libs
# --------------------------
pn532 = RealPN532(debug=False)
# display = OLEDDisplay()   # uncomment if OLED wired and library installed on Pi
display = MockDisplay()     # use mock if no OLED yet

members = pd.read_csv("members.csv", dtype=str)  # keep UIDs as strings
members["card_uid"] = members["card_uid"].str.upper().str.strip()

logged_in = set()

# Slack config
slack_token = "xoxb-..."   # replace with your bot token
slack_channel = "#shop-status"
client = WebClient(token=slack_token)

def send_slack_message(msg):
    try:
        client.chat_postMessage(channel=slack_channel, text=msg)
    except SlackApiError as e:
        print("Slack error:", e.response["error"])

print("Waiting for cards. Press Ctrl+C to exit.")
try:
    while True:
        uid = pn532.read_passive_target()
        if not uid:
            continue

        # sanitize
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
