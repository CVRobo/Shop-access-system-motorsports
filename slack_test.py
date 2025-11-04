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
ANNOUNCE_CHANNEL_ID = "C09MS0MFKBK"  # channel ID to post open/close notices
MEMBERS_FILE = "members.csv"
ATTENDANCE_FILE = "attendance.csv"

web_client = WebClient(token=SLACK_BOT_TOKEN)
socket_client = SocketModeClient(app_token=SLACK_APP_TOKEN, web_client=web_client)

# Ensure attendance file exists with headers
if not os.path.exists(ATTENDANCE_FILE):
    with open(ATTENDANCE_FILE, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["card_uid", "member_name", "check_in", "check_out", "hours", "approved"])

# --------------------------
# Low-level CSV helpers
# --------------------------
def read_attendance_rows():
    """Return list of rows (dict). If file missing or empty, return []"""
    if not os.path.exists(ATTENDANCE_FILE):
        return []
    with open(ATTENDANCE_FILE, "r", newline="") as f:
        reader = list(csv.DictReader(f))
        return reader

def write_attendance_rows(rows):
    """Overwrite attendance CSV with given rows (list of dicts)."""
    if not rows:
        # write header only
        with open(ATTENDANCE_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["card_uid", "member_name", "check_in", "check_out", "hours", "approved"])
        return
    with open(ATTENDANCE_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

# --------------------------
# Higher-level helpers
# --------------------------
def load_members():
    """Return dict mapping slack_id -> member row dict."""
    members = {}
    if not os.path.exists(MEMBERS_FILE):
        return members
    with open(MEMBERS_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            members[row["slack_id"].strip()] = {k: v.strip() for k, v in row.items()}
    return members

def current_checked_in():
    """Return list of member_name for sessions with empty check_out."""
    rows = read_attendance_rows()
    checked = []
    for r in rows:
        if r.get("check_out", "").strip() == "":
            checked.append(r.get("member_name"))
    return checked

def append_session(card_uid, name, check_in_dt):
    """Add a new check-in row. check_in_dt is a datetime."""
    iso = check_in_dt.isoformat()
    with open(ATTENDANCE_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([card_uid, name, iso, "", 0.0, "False"])

def get_open_session(card_uid):
    """Return the most recent open session dict for a card_uid or None."""
    rows = read_attendance_rows()
    for r in reversed(rows):
        if r.get("card_uid") == card_uid and r.get("check_out", "").strip() == "":
            return r
    return None

def update_session_checkout(card_uid, checkout_dt):
    """Find most recent open session for card_uid and set checkout/hours/approved=False.
    Returns the computed hours (float) or None if no open session found."""
    rows = read_attendance_rows()
    updated = False
    hours = None
    # iterate reversed to find latest open
    for i in range(len(rows)-1, -1, -1):
        r = rows[i]
        if r.get("card_uid") == card_uid and r.get("check_out", "").strip() == "":
            # parse check_in
            try:
                t1 = datetime.fromisoformat(r["check_in"])
            except Exception:
                # fallback parse
                t1 = datetime.strptime(r["check_in"], "%Y-%m-%d %H:%M:%S.%f")
            t2 = checkout_dt
            r["check_out"] = t2.isoformat()
            r["hours"] = round((t2 - t1).total_seconds() / 3600, 2)
            r["approved"] = "False"
            updated = True
            hours = float(r["hours"])
            break
    if updated:
        write_attendance_rows(rows)
    return hours

def get_unapproved_sessions_with_indices(member_name):
    """Return list of tuples (global_index, row_dict) for unapproved sessions of member_name in file order."""
    rows = read_attendance_rows()
    matches = []
    for idx, r in enumerate(rows):
        if r.get("member_name", "").strip().lower() == member_name.strip().lower():
            if str(r.get("approved", "")).lower() in ("false", "", "none"):
                matches.append((idx, r))
    return matches

def approve_specific_session_by_global_index(global_index):
    """Set approved=True for the row at global_index. Return True if success."""
    rows = read_attendance_rows()
    if global_index < 0 or global_index >= len(rows):
        return False
    rows[global_index]["approved"] = "True"
    write_attendance_rows(rows)
    return True

def disapprove_specific_session_by_global_index(global_index):
    """Remove the row at global_index from the file. Return True if removed."""
    rows = read_attendance_rows()
    if global_index < 0 or global_index >= len(rows):
        return False
    # remove the targeted row
    rows.pop(global_index)
    write_attendance_rows(rows)
    return True

def approve_all_unapproved(member_name):
    """Mark all unapproved sessions for member_name as approved. Returns count approved."""
    rows = read_attendance_rows()
    count = 0
    for r in rows:
        if r.get("member_name", "").strip().lower() == member_name.strip().lower():
            if str(r.get("approved", "")).lower() in ("false", "", "none"):
                r["approved"] = "True"
                count += 1
    if count > 0:
        write_attendance_rows(rows)
    return count

# --------------------------
# Slack posting helper
# --------------------------
def post_to_channel(channel_id, text):
    """Post message directly to a channel by ID."""
    try:
        web_client.chat_postMessage(channel=channel_id, text=text)
    except SlackApiError as e:
        print(f"Error posting to {channel_id}: {e.response['error']}")

# --------------------------
# Message handler
# --------------------------
def process_message(client: SocketModeClient, req: SocketModeRequest):
    if req.type != "events_api":
        return
    # ack
    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

    event = req.payload.get("event", {})
    if event.get("type") != "message" or "bot_id" in event:  # ignore bot messages
        return

    text = event.get("text", "").strip()
    text_lc = text.lower()
    slack_id = event.get("user")
    channel_type = event.get("channel_type")
    members = load_members()

    # -----------------------
    # Public channel listening
    # -----------------------
    if channel_type in ("channel", "group"):
        # lightweight phrase matching
        if any(phrase in text_lc for phrase in ["who is in shop", "who's in shop",
                                                "who is in the shop", "who's in the shop"]):
            people = current_checked_in()
            reply = "🏁 Currently in shop: " + ", ".join(people) if people else "🚪 The shop is currently empty."
            try:
                web_client.chat_postMessage(channel=event["channel"], text=reply)
            except SlackApiError as e:
                print("Error posting public reply:", e.response["error"])
        return

    # -----------------------
    # Direct messages only below
    # -----------------------
    if channel_type != "im" or not slack_id:
        return

    # Validate member exists
    if slack_id not in members:
        web_client.chat_postMessage(channel=event["channel"], text="❌ You are not registered in members.csv.")
        return

    member = members[slack_id]
    name = member["member_name"]
    card_uid = member["card_uid"]
    lead_slack = member.get("lead_slack_id")

    # ---------- CHECK IN ----------
    if "check in" in text_lc:
        was_empty = len(current_checked_in()) == 0
        check_in_time = datetime.now()
        append_session(card_uid, name, check_in_time)
        web_client.chat_postMessage(channel=event["channel"],
                                    text=f"✅ {name}, you’ve been checked in at {check_in_time.strftime('%H:%M:%S')}.")
        # announce open if first
        if was_empty:
            people = current_checked_in()
            post_to_channel(ANNOUNCE_CHANNEL_ID,
                            f"🟢 Shop open! {name} just checked in.\nCurrently in shop: {', '.join(people)}")
        return

    # ---------- CHECK OUT ----------
    if "check out" in text_lc:
        open_sess = get_open_session(card_uid)
        if not open_sess:
            web_client.chat_postMessage(channel=event["channel"], text="⚠️ You’re not currently checked in.")
            return
        checkout_time = datetime.now()
        hours = update_session_checkout(card_uid, checkout_time)
        web_client.chat_postMessage(channel=event["channel"],
                                    text=f"👋 Checked you out at {checkout_time.strftime('%H:%M:%S')}.")
        # Notify lead for approval (if lead available)
        if lead_slack:
            # compute hours again to show precise amount (safest)
            try:
                t1 = datetime.fromisoformat(open_sess["check_in"])
            except Exception:
                t1 = datetime.strptime(open_sess["check_in"], "%Y-%m-%d %H:%M:%S.%f")
            hrs = round((checkout_time - t1).total_seconds() / 3600, 2)
            msg = (f"🧾 {name} checked out.\nHours worked: {hrs}\n"
                   f"To review pending sessions: `approve pending {name}`\n"
                   f"To approve a specific session: `approve {name} <number>`\n"
                   f"To disapprove a specific session: `disapprove {name} <number>`")
            try:
                web_client.chat_postMessage(channel=lead_slack, text=msg)
            except SlackApiError as e:
                print("Error DMing lead:", e.response["error"])

        # announce closed if last person
        if len(current_checked_in()) == 0:
            post_to_channel(ANNOUNCE_CHANNEL_ID, f"🔴 Shop closed. Last person out: {name}")
        return

    # ---------- APPROVAL / DISAPPROVAL HANDLING ----------
    # Commands supported:
    #  - approve pending <Name>
    #  - approve all <Name>
    #  - approve <Name> <n>
    #  - disapprove <Name> <n>
    if text_lc.startswith("approve ") or text_lc.startswith("disapprove "):
        parts = text.split()
        cmd = parts[0].lower()

        # handle 'approve pending <Name>'
        if len(parts) >= 3 and parts[1].lower() == "pending":
            target_name = " ".join(parts[2:]).strip()
            # ensure approver is lead for that member
            approver_id = slack_id
            lead_for = [m["member_name"] for m in members.values() if m.get("lead_slack_id") == approver_id]
            # permit if approver is lead for target
            if not any(t.strip().lower() == target_name.strip().lower() for t in lead_for):
                web_client.chat_postMessage(channel=event["channel"],
                                            text="🚫 You’re not authorized to view pending sessions for that member.")
                return
            pending = get_unapproved_sessions_with_indices(target_name)
            if not pending:
                web_client.chat_postMessage(channel=event["channel"], text=f"✅ No pending sessions for {target_name}.")
                return
            # build listing
            msg_lines = [f"🕒 Pending sessions for *{target_name}*:"]
            for i, (gidx, row) in enumerate(pending, start=1):
                ci = row.get("check_in", "")
                co = row.get("check_out", "")
                hrs = row.get("hours", "")
                msg_lines.append(f"{i}. check_in: {ci}, check_out: {co or '(open)'}, hours: {hrs or '0.0'}")
            msg_lines.append("\nApprove a session: `approve <Name> <number>`")
            msg_lines.append("Disapprove (remove) a session: `disapprove <Name> <number>`")
            web_client.chat_postMessage(channel=event["channel"], text="\n".join(msg_lines))
            return

        # handle 'approve all <Name>'
        if cmd == "approve" and len(parts) >= 3 and parts[1].lower() == "all":
            target_name = " ".join(parts[2:]).strip()
            approver_id = slack_id
            lead_for = [m["member_name"] for m in members.values() if m.get("lead_slack_id") == approver_id]
            if not any(t.strip().lower() == target_name.strip().lower() for t in lead_for):
                web_client.chat_postMessage(channel=event["channel"],
                                            text="🚫 You’re not authorized to approve hours for that member.")
                return
            count = approve_all_unapproved(target_name)
            web_client.chat_postMessage(channel=event["channel"],
                                        text=f"✅ Approved {count} unapproved session(s) for {target_name}.")
            return

        # handle 'approve <Name> <n>' or 'disapprove <Name> <n>'
        # fallback parsing: last token numeric is session number, rest is name
        if len(parts) >= 3 and parts[-1].isdigit():
            session_num = int(parts[-1])
            target_name = " ".join(parts[1:-1]).strip()
            if session_num <= 0:
                web_client.chat_postMessage(channel=event["channel"], text="⚠️ Session number must be >= 1.")
                return

            # ensure approver is lead for that member
            approver_id = slack_id
            lead_for = [m["member_name"] for m in members.values() if m.get("lead_slack_id") == approver_id]
            if not any(t.strip().lower() == target_name.strip().lower() for t in lead_for):
                web_client.chat_postMessage(channel=event["channel"],
                                            text="🚫 You’re not authorized to approve/disapprove for that member.")
                return

            pending = get_unapproved_sessions_with_indices(target_name)
            if session_num > len(pending):
                web_client.chat_postMessage(channel=event["channel"],
                                            text=f"⚠️ Invalid session number. There are {len(pending)} pending sessions for {target_name}.")
                return

            # map session number to global index
            global_index, row = pending[session_num - 1]

            if cmd == "approve":
                ok = approve_specific_session_by_global_index(global_index)
                if ok:
                    web_client.chat_postMessage(channel=event["channel"],
                                                text=f"✅ Approved session #{session_num} for {target_name}.")
                else:
                    web_client.chat_postMessage(channel=event["channel"],
                                                text=f"⚠️ Failed to approve session #{session_num} for {target_name}.")
                return

            if cmd == "disapprove":
                ok = disapprove_specific_session_by_global_index(global_index)
                if ok:
                    web_client.chat_postMessage(channel=event["channel"],
                                                text=f"🗑️ Disapproved (removed) session #{session_num} for {target_name}.")
                else:
                    web_client.chat_postMessage(channel=event["channel"],
                                                text=f"⚠️ Failed to disapprove session #{session_num} for {target_name}.")
                return

        # if we fell through to here, unknown approve/disapprove usage
        web_client.chat_postMessage(channel=event["channel"],
                                    text="Usage:\n• `approve pending <Name>`\n• `approve <Name> <number>`\n• `approve all <Name>`\n• `disapprove <Name> <number>`")
        return

    # ---------- WHO IS IN ----------
    if "who is in" in text_lc or "who's in" in text_lc:
        current = current_checked_in()
        if current:
            reply = "🏁 Checked in:\n• " + "\n• ".join(current)
        else:
            reply = "😴 No one is currently checked in."
        web_client.chat_postMessage(channel=event["channel"], text=reply)
        return

    # ---------- HELP / FALLBACK ----------
    web_client.chat_postMessage(channel=event["channel"],
                                text="Try `check in`, `check out`, `who is in`, `approve pending <Name>`, `approve <Name> <n>`, `approve all <Name>`, or `disapprove <Name> <n>`.")

# --------------------------
# Run
# --------------------------
socket_client.socket_mode_request_listeners.append(process_message)
print("✅ Slack attendance bot (session-level approvals) running… (Ctrl+C to stop)")
socket_client.connect()

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("Shutting down…")
