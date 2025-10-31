import os
import csv
import time
from datetime import datetime
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.errors import SlackApiError

# --------------------------
# Setup
# --------------------------
load_dotenv()
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
MEMBERS_FILE = "members.csv"
ATTENDANCE_FILE = "attendance.csv"

web_client = WebClient(token=SLACK_BOT_TOKEN)
socket_client = SocketModeClient(app_token=SLACK_APP_TOKEN, web_client=web_client)

# Ensure attendance file exists
if not os.path.exists(ATTENDANCE_FILE):
    with open(ATTENDANCE_FILE, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["card_uid", "member_name", "check_in", "check_out", "hours", "approved"])

# --------------------------
# Helpers
# --------------------------
def load_members():
    members = {}
    with open(MEMBERS_FILE, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            members[row["slack_id"]] = row
    return members

def get_open_session(card_uid):
    with open(ATTENDANCE_FILE, "r") as f:
        reader = csv.DictReader(f)
        for row in reversed(list(reader)):  # iterate backwards
            if row["card_uid"] == card_uid and row["check_out"].strip() == "":
                return row
    return None

def append_session(card_uid, name, check_in):
    with open(ATTENDANCE_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([card_uid, name, check_in, "", 0.0, False])

def update_session_checkout(card_uid, checkout_time):
    rows = []
    with open(ATTENDANCE_FILE, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["card_uid"] == card_uid and row["check_out"].strip() == "":
                # compute hours
                t1 = datetime.fromisoformat(row["check_in"])
                t2 = checkout_time
                row["check_out"] = str(t2)
                row["hours"] = round((t2 - t1).total_seconds() / 3600, 2)
                row["approved"] = False
            rows.append(row)

    with open(ATTENDANCE_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

def approve_session(member_name):
    rows = []
    with open(ATTENDANCE_FILE, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["member_name"] == member_name and row["approved"] in ("False", "false", ""):
                row["approved"] = True
            rows.append(row)
    with open(ATTENDANCE_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

def current_checked_in():
    checked_in = []
    with open(ATTENDANCE_FILE, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["check_out"].strip() == "":
                checked_in.append(row["member_name"])
    return checked_in

# --------------------------
# Message handler
# --------------------------
def process_message(client: SocketModeClient, req: SocketModeRequest):
    if req.type != "events_api":
        return
    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

    event = req.payload.get("event", {})
    if event.get("type") != "message" or "bot_id" in event:
        return
    if event.get("channel_type") != "im":
        return  # only DMs

    slack_id = event["user"]
    text = event["text"].strip().lower()
    members = load_members()

    # Validate member
    if slack_id not in members:
        web_client.chat_postMessage(channel=event["channel"], text="❌ You are not registered in members.csv.")
        return

    member = members[slack_id]
    name = member["member_name"]
    card_uid = member["card_uid"]

    # --- Commands ---
    if "check in" in text:
        check_in_time = datetime.now()
        append_session(card_uid, name, str(check_in_time))
        web_client.chat_postMessage(channel=event["channel"], text=f"✅ {name}, you’ve been checked in at {check_in_time.strftime('%H:%M:%S')}.")

    elif "check out" in text:
        open_session = get_open_session(card_uid)
        if not open_session:
            web_client.chat_postMessage(channel=event["channel"], text="⚠️ You’re not currently checked in.")
            return
        checkout_time = datetime.now()
        update_session_checkout(card_uid, checkout_time)
        web_client.chat_postMessage(channel=event["channel"], text=f"👋 Checked you out at {checkout_time.strftime('%H:%M:%S')}.")

        # Notify lead for approval
        lead_id = member["lead_slack_id"]
        message = f"🧾 {name} checked out.\nHours worked: {round((checkout_time - datetime.fromisoformat(open_session['check_in'])).total_seconds() / 3600, 2)}\nApprove? Type `approve {name}`."
        web_client.chat_postMessage(channel=lead_id, text=message)

    elif text.startswith("approve "):
        approver_id = slack_id
        target_name = text.replace("approve ", "").strip().title()

        # Ensure approver is actually a lead for that member
        lead_for = [m["member_name"] for m in members.values() if m["lead_slack_id"] == approver_id]
        if target_name not in lead_for:
            web_client.chat_postMessage(channel=event["channel"], text="🚫 You’re not the lead for that member.")
            return

        approve_session(target_name)
        web_client.chat_postMessage(channel=event["channel"], text=f"✅ Approved hours for {target_name}.")

    elif "who is in" in text:
        current = current_checked_in()
        if current:
            reply = "🏁 Checked in:\n• " + "\n• ".join(current)
        else:
            reply = "😴 No one is currently checked in."
        web_client.chat_postMessage(channel=event["channel"], text=reply)

    else:
        web_client.chat_postMessage(channel=event["channel"], text="Try `check in`, `check out`, `who is in`, or `approve <name>`.")

# --------------------------
# Run
# --------------------------
socket_client.socket_mode_request_listeners.append(process_message)
print("✅ Slack attendance bot running… (Ctrl+C to stop)")
socket_client.connect()

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("Shutting down…")
