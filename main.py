import time
import pandas as pd
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# --------------------------
# Mock hardware classes
# --------------------------
class MockPN532:
    def read_passive_target(self):
        time.sleep(1)
        return input("Scan a card (enter UID or blank to skip): ").strip().upper()

class MockDisplay:
    def show_text(self, text):
        print(f"[OLED DISPLAY] {text}")

# --------------------------
# Initialize
# --------------------------
pn532 = MockPN532()
display = MockDisplay()
members = pd.read_csv("members.csv")
logged_in = set()

# Slack bot token and channel
slack_token = "YOUR_SLACK_BOT_TOKEN"
slack_channel = "#shop-status"
client = WebClient(token=slack_token)

def send_slack_message(msg):
    try:
        client.chat_postMessage(channel=slack_channel, text=msg)
        print(f"[Slack] {msg}")
    except SlackApiError as e:
        print(f"Slack error: {e.response['error']}")

# --------------------------
# Main loop
# --------------------------
print("Ready to simulate card scans. Ctrl+C to stop.")
while True:
    uid = pn532.read_passive_target()
    if not uid:
        continue

    user = members[members["card_uid"] == uid]
    if user.empty:
        display.show_text("Unknown card")
        continue

    name = user["name"].values[0]
    if uid in logged_in:
        logged_in.remove(uid)
        display.show_text(f"Goodbye {name}")
        send_slack_message(f"{name} checked out. {len(logged_in)} in shop.")
        if len(logged_in) == 0:
            send_slack_message("🏁 Shop closed.")
    else:
        was_empty = len(logged_in) == 0
        logged_in.add(uid)
        display.show_text(f"Welcome {name}")
        if was_empty:
            send_slack_message("🚀 Shop open!")
        send_slack_message(f"{name} checked in. {len(logged_in)} in shop.")

