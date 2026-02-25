import os
import csv
import time
import random
from datetime import datetime
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.errors import SlackApiError

# --------------------------
# Configuration
# --------------------------
load_dotenv()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
ANNOUNCE_CHANNEL_ID = "C09MS0MFKBK"
ADMIN_SLACK_ID = "U07U7V298Q2"
MEMBERS_FILE = "members.csv"
ATTENDANCE_FILE = "attendance.csv"
ATTENDANCE_HEADERS = ["card_uid", "member_name", "check_in", "check_out", "hours", "approved"]

FORMAL_OPEN_MESSAGE = "The shop is now open."

SHOP_OPEN_MESSAGES = [
    "Shop portal detached from frame alignment (shop open)",
    "Workroom barrier rotated off-axis from jamb (facility accessible)",
    "Workshop door decoupled from its seal (shop active)",
    "Maker-space barrier angularly displaced from frame (open condition)",
    "Door-frame interface disengaged (workspace open)",
    "Access panel rotated beyond 0-10 degree threshold (shop open)",
    "Entry barrier uncompressed from gasket (facility open)",
    "Primary door unengaged from strike plate (shop accessible)",
    "Ingress point mechanically liberated from frame (room open)",
    "Entrance panel no longer flush with threshold (open state achieved)",
    "Portal hinge system mobilized; access vector unobstructed (shop open)",
    "Entry mechanism actuated into the unsealed configuration (space open)",
    "Door-frame cohesion reduced to negligible levels (shop accessible)",
    "Barrier rotation > 1 radian detected (workspace open)",
    "Ingress aperture expanded beyond secure bounds (shop open)",
    "Physical access impedance minimized (facility open)",
    "Portal integrity intentionally compromised (open mode active)",
    "Threshold obstruction set to null (workspace open)",
    "Door has divorced the frame - irreconcilable openness achieved",
    "The door and frame are on a break (shop open)",
    "Portal is vibing away from the frame (shop open)",
    "Door reoriented into welcoming position (shop open)",
    "Barrier is expressing its extroverted phase (shop open)",
    "Door is in open world mode (shop open)",
    "Entry panel socially distancing from frame (shop open)",
    "The gateway withdraws from its seal; the shop awakens",
    "The barrier relinquishes its duty; the workshop calls",
    "The entry rune de-binds; passage permitted",
    "The portal yields; creativity may enter",
    "Barrier unsealed (shop open)",
    "Portal unlocked (workspace active)",
    "Ingress enabled (shop open)",
    "Access granted (shop active)",
    "Portal disengaged (shop open)",
    "Workshop portal unbarred - operational state achieved",
    "Workshop ingress panel unsealed - entry permitted",
    "Lab barrier unlocked - space accessible",
    "Workspace door ajar - open mode engaged",
    "Shop portal unlatched - environment active",
    "Studio entry barrier de-secured - shop accessible",
]

# Live in-memory state (resets on bot restart by design)
CURRENT_MEMBERS = set()
USE_FORMAL_MODE = False  # when True, shop-open announcements use FORMAL_OPEN_MESSAGE

web_client = WebClient(token=SLACK_BOT_TOKEN)
socket_client = SocketModeClient(app_token=SLACK_APP_TOKEN, web_client=web_client)

# --------------------------
# Attendance CSV helpers
# --------------------------
def ensure_attendance_file():
    if not os.path.exists(ATTENDANCE_FILE):
        with open(ATTENDANCE_FILE, "w", newline="") as f:
            csv.writer(f).writerow(ATTENDANCE_HEADERS)

def read_attendance_rows():
    ensure_attendance_file()
    with open(ATTENDANCE_FILE, "r", newline="") as f:
        return list(csv.DictReader(f))

def write_attendance_rows(rows):
    with open(ATTENDANCE_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ATTENDANCE_HEADERS)
        writer.writeheader()
        writer.writerows(rows)

def append_session(card_uid, name, check_in_dt):
    with open(ATTENDANCE_FILE, "a", newline="") as f:
        csv.writer(f).writerow([card_uid, name, check_in_dt.isoformat(), "", 0.0, "False"])

def get_open_session(card_uid):
    for row in reversed(read_attendance_rows()):
        if row["card_uid"] == card_uid and not row["check_out"].strip():
            return row
    return None

def close_open_session(card_uid, member_name, checkout_dt):
    """
    Find the most recent open session by card_uid, falling back to member_name.
    Returns (hours, check_in_iso) or (None, None) if no open session found.
    """
    rows = read_attendance_rows()
    target = None

    for i in range(len(rows) - 1, -1, -1):
        if rows[i]["card_uid"] == card_uid and not rows[i]["check_out"].strip():
            target = i
            break

    if target is None:
        for i in range(len(rows) - 1, -1, -1):
            if rows[i]["member_name"].strip().lower() == member_name.strip().lower() and not rows[i]["check_out"].strip():
                target = i
                break

    if target is None:
        return None, None

    row = rows[target]
    check_in_iso = row["check_in"]

    try:
        t1 = datetime.fromisoformat(check_in_iso)
    except (ValueError, TypeError):
        t1 = None

    hours = round((checkout_dt - t1).total_seconds() / 3600, 2) if t1 else 0.0
    row["check_out"] = checkout_dt.isoformat()
    row["hours"] = hours
    row["approved"] = "False"
    write_attendance_rows(rows)
    return hours, check_in_iso

def get_unapproved_sessions(member_name):
    """Return list of (global_index, row) for all unapproved sessions of a member."""
    return [
        (i, row)
        for i, row in enumerate(read_attendance_rows())
        if row["member_name"].strip().lower() == member_name.strip().lower()
        and str(row.get("approved", "")).lower() in ("false", "", "none")
    ]

def approve_session(global_index):
    rows = read_attendance_rows()
    if not (0 <= global_index < len(rows)):
        return False
    rows[global_index]["approved"] = "True"
    write_attendance_rows(rows)
    return True

def delete_session(global_index):
    rows = read_attendance_rows()
    if not (0 <= global_index < len(rows)):
        return False
    rows.pop(global_index)
    write_attendance_rows(rows)
    return True

def approve_all_sessions(member_name):
    rows = read_attendance_rows()
    count = 0
    for row in rows:
        if row["member_name"].strip().lower() == member_name.strip().lower():
            if str(row.get("approved", "")).lower() in ("false", "", "none"):
                row["approved"] = "True"
                count += 1
    if count:
        write_attendance_rows(rows)
    return count

# --------------------------
# Member CSV helpers
# --------------------------
def load_members():
    """Return dict of slack_id -> member row dict."""
    if not os.path.exists(MEMBERS_FILE):
        return {}
    with open(MEMBERS_FILE, "r", newline="") as f:
        return {
            row["slack_id"].strip(): {k: v.strip() for k, v in row.items()}
            for row in csv.DictReader(f)
        }

# --------------------------
# Slack helpers
# --------------------------
def post(channel, text):
    try:
        web_client.chat_postMessage(channel=channel, text=text)
    except SlackApiError as e:
        print(f"[Slack error] channel={channel}: {e.response['error']}")

def reply(event, text):
    post(event["channel"], text)

def is_authorized_lead(approver_id, target_name, members):
    return any(
        m["member_name"].strip().lower() == target_name.strip().lower()
        for m in members.values()
        if m.get("lead_slack_id") == approver_id
    )

# --------------------------
# Command handlers
# --------------------------
def handle_check_in(event, member):
    name = member["member_name"]
    card_uid = member["card_uid"]

    existing = get_open_session(card_uid)
    if existing or name in CURRENT_MEMBERS:
        if existing:
            try:
                since = datetime.fromisoformat(existing["check_in"]).strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                since = existing.get("check_in", "unknown")
            reply(event, f"You are already checked in since {since}. Please `check out` first.")
        else:
            reply(event, "You are already checked in. Please `check out` first.")
        return

    was_empty = len(CURRENT_MEMBERS) == 0
    check_in_time = datetime.now()

    try:
        append_session(card_uid, name, check_in_time)
        CURRENT_MEMBERS.add(name)
    except Exception as e:
        print(f"[Error] Failed to append session: {e}")
        reply(event, "Failed to record check-in. Please try again or contact an admin.")
        return

    reply(event, f"Checked in at {check_in_time.strftime('%H:%M:%S')}.")

    if was_empty:
        open_msg = FORMAL_OPEN_MESSAGE if USE_FORMAL_MODE else f"{random.choice(SHOP_OPEN_MESSAGES)}."
        post(ANNOUNCE_CHANNEL_ID, f"{open_msg} {name} checked in.")


def handle_check_out(event, member):
    name = member["member_name"]
    card_uid = member["card_uid"]
    checkout_time = datetime.now()

    hours, check_in_iso = close_open_session(card_uid, name, checkout_time)

    if hours is None:
        if name in CURRENT_MEMBERS:
            CURRENT_MEMBERS.discard(name)
            reply(event, "Inconsistency detected: you were marked as checked in but no CSV session was found. "
                         "Your live state has been cleared - please check in again.")
        else:
            reply(event, "You're not currently checked in.")
        return

    CURRENT_MEMBERS.discard(name)
    reply(event, f"Checked out at {checkout_time.strftime('%H:%M:%S')}.")

    lead_slack = member.get("lead_slack_id")
    if lead_slack:
        try:
            hrs = round((checkout_time - datetime.fromisoformat(check_in_iso)).total_seconds() / 3600, 2) if check_in_iso else round(hours, 2)
        except (ValueError, TypeError):
            hrs = round(hours, 2)
        post(lead_slack,
             f"{name} checked out. Hours worked: {hrs}\n"
             f"- `approve pending {name}` to view pending sessions\n"
             f"- `approve {name} <number>` to approve a specific session\n"
             f"- `disapprove {name} <number>` to remove a specific session")

    if len(CURRENT_MEMBERS) == 0:
        post(ANNOUNCE_CHANNEL_ID, f"Shop closed. Last person out: {name}")


def handle_approve_disapprove(event, slack_id, text, members):
    parts = text.split()
    cmd = parts[0].lower()

    # approve pending <Name>
    if len(parts) >= 3 and parts[1].lower() == "pending":
        target_name = " ".join(parts[2:])
        if not is_authorized_lead(slack_id, target_name, members):
            reply(event, "You're not authorized to view pending sessions for that member.")
            return
        pending = get_unapproved_sessions(target_name)
        if not pending:
            reply(event, f"No pending sessions for {target_name}.")
            return
        lines = [f"Pending sessions for {target_name}:"]
        for i, (_, row) in enumerate(pending, start=1):
            lines.append(f"{i}. check_in: {row['check_in']}  check_out: {row['check_out'] or '(open)'}  hours: {row['hours'] or '0.0'}")
        lines += ["", "- `approve <Name> <number>` to approve", "- `disapprove <Name> <number>` to remove"]
        reply(event, "\n".join(lines))
        return

    # approve all <Name>
    if cmd == "approve" and len(parts) >= 3 and parts[1].lower() == "all":
        target_name = " ".join(parts[2:])
        if not is_authorized_lead(slack_id, target_name, members):
            reply(event, "You're not authorized to approve hours for that member.")
            return
        count = approve_all_sessions(target_name)
        reply(event, f"Approved {count} session(s) for {target_name}.")
        return

    # approve/disapprove <Name> <number>
    if len(parts) >= 3 and parts[-1].isdigit():
        session_num = int(parts[-1])
        target_name = " ".join(parts[1:-1])
        if session_num <= 0:
            reply(event, "Session number must be 1 or greater.")
            return
        if not is_authorized_lead(slack_id, target_name, members):
            reply(event, "You're not authorized to approve/disapprove sessions for that member.")
            return
        pending = get_unapproved_sessions(target_name)
        if session_num > len(pending):
            reply(event, f"Invalid session number - {target_name} has {len(pending)} pending session(s).")
            return
        global_index, _ = pending[session_num - 1]
        if cmd == "approve":
            ok = approve_session(global_index)
            reply(event, f"Approved session #{session_num} for {target_name}." if ok else f"Failed to approve session #{session_num}.")
        else:
            ok = delete_session(global_index)
            reply(event, f"Removed session #{session_num} for {target_name}." if ok else f"Failed to remove session #{session_num}.")
        return

    reply(event, (
        "Usage:\n"
        "- `approve pending <Name>`\n"
        "- `approve <Name> <number>`\n"
        "- `approve all <Name>`\n"
        "- `disapprove <Name> <number>`"
    ))


def handle_announcement_formal(event, slack_id):
    global USE_FORMAL_MODE
    if slack_id != ADMIN_SLACK_ID:
        reply(event, "You're not authorized to use this command.")
        return
    USE_FORMAL_MODE = True
    reply(event, f"Formal mode enabled. All future shop-open announcements will use:\n\"{FORMAL_OPEN_MESSAGE}\"")


def handle_announcement_casual(event, slack_id):
    global USE_FORMAL_MODE
    if slack_id != ADMIN_SLACK_ID:
        reply(event, "You're not authorized to use this command.")
        return
    USE_FORMAL_MODE = False
    reply(event, "Casual mode restored. Shop-open announcements will use random messages again.")


def handle_is_shop_open(channel):
    if CURRENT_MEMBERS:
        people = sorted(CURRENT_MEMBERS)
        post(channel, "Yes, the shop is open. Currently checked in:\n- " + "\n- ".join(people))
    else:
        post(channel, "No, the shop is currently closed.")


def handle_who_is_in(event):
    people = sorted(CURRENT_MEMBERS)
    if people:
        reply(event, "Currently checked in:\n- " + "\n- ".join(people))
    else:
        reply(event, "No one is currently checked in.")


# --------------------------
# Main event dispatcher
# --------------------------
def process_message(client, req):
    if req.type != "events_api":
        return
    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

    event = req.payload.get("event", {})
    if event.get("type") != "message" or "bot_id" in event:
        return

    text = event.get("text", "").strip()
    text_lc = text.lower()
    slack_id = event.get("user")
    channel_type = event.get("channel_type")

    # Public channels
    if channel_type in ("channel", "group"):
        if any(p in text_lc for p in ["who is in shop", "who's in shop", "who is in the shop", "who's in the shop"]):
            people = sorted(CURRENT_MEMBERS)
            msg = "Currently in shop: " + ", ".join(people) if people else "The shop is currently empty."
            post(event["channel"], msg)
        elif "is shop open" in text_lc or "is the shop open" in text_lc:
            handle_is_shop_open(event["channel"])
        return

    # DMs only from here
    if channel_type != "im" or not slack_id:
        return

    members = load_members()

    if slack_id not in members:
        reply(event, "You are not registered in members.csv.")
        return

    member = members[slack_id]

    if "check in" in text_lc:
        handle_check_in(event, member)
    elif "check out" in text_lc:
        handle_check_out(event, member)
    elif text_lc.startswith("approve ") or text_lc.startswith("disapprove "):
        handle_approve_disapprove(event, slack_id, text, members)
    elif text_lc == "announcement formal":
        handle_announcement_formal(event, slack_id)
    elif text_lc == "announcement casual":
        handle_announcement_casual(event, slack_id)
    elif "is shop open" in text_lc or "is the shop open" in text_lc:
        handle_is_shop_open(event["channel"])
    elif "who is in" in text_lc or "who's in" in text_lc:
        handle_who_is_in(event)
    else:
        reply(event, (
            "Available commands:\n"
            "- `check in` / `check out`\n"
            "- `who is in` / `is shop open`\n"
            "- `approve pending <Name>`\n"
            "- `approve <Name> <number>`\n"
            "- `approve all <Name>`\n"
            "- `disapprove <Name> <number>`\n"
            "- `announcement formal` / `announcement casual` (admin only)"
        ))


# --------------------------
# Startup
# --------------------------
ensure_attendance_file()

socket_client.socket_mode_request_listeners.append(process_message)
print("Slack attendance bot running... (Ctrl+C to stop)")
socket_client.connect()

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("Shutting down...")