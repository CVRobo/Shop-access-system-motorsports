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
# Setup
# --------------------------
load_dotenv()
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
ANNOUNCE_CHANNEL_ID = "C09MS0MFKBK"  # channel ID to post open/close notices
MEMBERS_FILE = "members.csv"
ATTENDANCE_FILE = "attendance.csv"

SHOP_OPEN_MESSAGES = [
    "Shop portal detached from frame alignment (shop open) ",
    "Workroom barrier rotated off-axis from jamb (facility accessible) ",
    "Workshop door decoupled from its seal (shop active) ",
    "Maker-space barrier angularly displaced from frame (open condition) ",
    "Door–frame interface disengaged (workspace open) ",
    "Access panel rotated beyond 0°–10° threshold (shop open) ",
    "Entry barrier uncompressed from gasket (facility open) ",
    "Primary door unengaged from strike plate (shop accessible) ",
    "Ingress point mechanically liberated from frame (room open) ",
    "Entrance panel no longer flush with threshold (open state achieved) ",
    "Portal hinge system mobilized; access vector unobstructed (shop open) ",
    "Entry mechanism actuated into the ‘unsealed’ configuration (space open) ",
    "Door–frame cohesion reduced to negligible levels (shop accessible) ",
    "Barrier rotation > 1 radian detected (workspace open) ",
    "Ingress aperture expanded beyond secure bounds (shop open) ",
    "Physical access impedance minimized (facility open) ",
    "Portal integrity intentionally compromised (open mode active) ",
    "Threshold obstruction set to null (workspace open) ",
    "Door has divorced the frame — irreconcilable openness achieved ",
    "The door and frame are ‘on a break’ (shop open) ",
    "Portal is vibing away from the frame (shop open) ",
    "Door reoriented into welcoming position (shop open) ",
    "Barrier is expressing its extroverted phase (shop open) ",
    "Door is in ‘open world’ mode (shop open) ",
    "Entry panel socially distancing from frame (shop open) ",
    "The gateway withdraws from its seal; the shop awakens ",
    "The barrier relinquishes its duty; the workshop calls ",
    "The entry rune de-binds; passage permitted ",
    "The portal yields; creativity may enter ",
    "Barrier unsealed (shop open) ",
    "Portal unlocked (workspace active) ",
    "Ingress enabled (shop open) ",
    "Access granted (shop active) ",
    "Portal disengaged (shop open) ",
    "Workshop portal unbarred — operational state achieved ",
    "Workshop ingress panel unsealed — entry permitted ",
    "Lab barrier unlocked — space accessible ",
    "Workspace door ajar — open mode engaged ",
    "Shop portal unlatched — environment active ",
    "Studio entry barrier de-secured — shop accessible ",
]

web_client = WebClient(token=SLACK_BOT_TOKEN)
socket_client = SocketModeClient(app_token=SLACK_APP_TOKEN, web_client=web_client)

# --------------------------
# Live in-memory occupancy
# --------------------------
# holds member_name strings (resets on bot restart; as requested)
CURRENT_MEMBERS = set()

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
        return list(csv.DictReader(f))

def write_attendance_rows(rows):
    """Overwrite attendance CSV with given rows (list of dicts)."""
    if not rows:
        with open(ATTENDANCE_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["card_uid", "member_name", "check_in", "check_out", "hours", "approved"])
        return
    # keep header ordering stable
    fieldnames = ["card_uid", "member_name", "check_in", "check_out", "hours", "approved"]
    with open(ATTENDANCE_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
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
            # strip whitespace from fields
            members[row["slack_id"].strip()] = {k: v.strip() for k, v in row.items()}
    return members

def current_checked_in():
    """(legacy) Return list of member_name for sessions with empty check_out."""
    rows = read_attendance_rows()
    checked = []
    for r in rows:
        if r.get("check_out", "").strip() == "":
            checked.append(r.get("member_name"))
    seen = set()
    unique = []
    for name in checked:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return unique

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

def find_open_session_index_by_name(member_name):
    """Return the global index and row for the most recent open session by member_name, or (None, None)."""
    rows = read_attendance_rows()
    for i in range(len(rows)-1, -1, -1):
        r = rows[i]
        if r.get("member_name", "").strip().lower() == member_name.strip().lower() and r.get("check_out", "").strip() == "":
            return i, r
    return None, None

def update_session_checkout_flexible(card_uid, member_name, checkout_dt):
    """
    Find most recent open session first by card_uid, then fallback to member_name.
    Update check_out, hours, approved, write file.
    Return (hours, original_check_in_iso) or (None, None) if none found.
    """
    rows = read_attendance_rows()
    target_idx = None
    # try by card_uid first
    for i in range(len(rows)-1, -1, -1):
        r = rows[i]
        if r.get("card_uid") == card_uid and r.get("check_out", "").strip() == "":
            target_idx = i
            break
    # fallback by name
    if target_idx is None:
        for i in range(len(rows)-1, -1, -1):
            r = rows[i]
            if r.get("member_name", "").strip().lower() == member_name.strip().lower() and r.get("check_out", "").strip() == "":
                target_idx = i
                break
    if target_idx is None:
        return None, None

    r = rows[target_idx]
    # parse check_in safely
    try:
        t1 = datetime.fromisoformat(r["check_in"])
    except Exception:
        try:
            t1 = datetime.strptime(r["check_in"], "%Y-%m-%d %H:%M:%S.%f")
        except Exception:
            # can't parse check_in — still set check_out but hours unknown
            t1 = None

    t2 = checkout_dt
    r["check_out"] = t2.isoformat()
    if t1:
        r["hours"] = round((t2 - t1).total_seconds() / 3600, 2)
        hours = float(r["hours"])
    else:
        r["hours"] = 0.0
        hours = 0.0
    r["approved"] = "False"
    write_attendance_rows(rows)
    return hours, r.get("check_in")

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
    rows = read_attendance_rows()
    if global_index < 0 or global_index >= len(rows):
        return False
    rows[global_index]["approved"] = "True"
    write_attendance_rows(rows)
    return True

def disapprove_specific_session_by_global_index(global_index):
    rows = read_attendance_rows()
    if global_index < 0 or global_index >= len(rows):
        return False
    rows.pop(global_index)
    write_attendance_rows(rows)
    return True

def approve_all_unapproved(member_name):
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
    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

    event = req.payload.get("event", {})
    if event.get("type") != "message" or "bot_id" in event:
        return

    text = event.get("text", "").strip()
    text_lc = text.lower()
    slack_id = event.get("user")
    channel_type = event.get("channel_type")
    members = load_members()

    # Public channels
    if channel_type in ("channel", "group"):
        if any(phrase in text_lc for phrase in ["who is in shop", "who's in shop",
                                                "who is in the shop", "who's in the shop"]):
            people = list(CURRENT_MEMBERS)
            reply = "🏁 Currently in shop: " + ", ".join(people) if people else "🚪 The shop is currently empty."
            try:
                web_client.chat_postMessage(channel=event["channel"], text=reply)
            except SlackApiError as e:
                print("Error posting public reply:", e.response["error"])
        return

    # DM only
    if channel_type != "im" or not slack_id:
        return

    if slack_id not in members:
        web_client.chat_postMessage(channel=event["channel"], text="❌ You are not registered in members.csv.")
        return

    member = members[slack_id]
    name = member["member_name"]
    card_uid = member["card_uid"]
    lead_slack = member.get("lead_slack_id")

    # CHECK IN
    if "check in" in text_lc:
        existing = get_open_session(card_uid)
        if existing or name in CURRENT_MEMBERS:
            if existing:
                try:
                    t = datetime.fromisoformat(existing["check_in"])
                    since = t.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    since = existing.get("check_in", "(unknown)")
                web_client.chat_postMessage(channel=event["channel"],
                                            text=f"⚠️ {name}, you are already checked in (since {since}). Please `check out` first.")
            else:
                web_client.chat_postMessage(channel=event["channel"],
                                            text=f"⚠️ {name}, you are already checked in. Please `check out` first.")
            return

        # Determine if the shop was empty before this check-in (for announce)
        was_empty_before = len(CURRENT_MEMBERS) == 0

        check_in_time = datetime.now()
        # make append defensively; only add to CURRENT_MEMBERS if append succeeded
        try:
            append_session(card_uid, name, check_in_time)
            CURRENT_MEMBERS.add(name)
        except Exception as e:
            print("Failed to append session:", e)
            web_client.chat_postMessage(channel=event["channel"],
                                        text="❌ Failed to record check-in (server error). Try again or contact admin.")
            return

        web_client.chat_postMessage(channel=event["channel"],
                                    text=f"✅ {name}, you’ve been checked in at {check_in_time.strftime('%H:%M:%S')}.")

        # announce open if first
        if was_empty_before:
            message = random.choice(SHOP_OPEN_MESSAGES)
            post_to_channel(ANNOUNCE_CHANNEL_ID,
                            f"{message}. {name} checked in.")
        return

    # CHECK OUT
    if "check out" in text_lc:
        # Try to update checkout, flexible fallback by name built-in
        checkout_time = datetime.now()
        hours, open_checkin_iso = update_session_checkout_flexible(card_uid, name, checkout_time)
        if hours is None:
            # nothing in CSV to close; maybe set mismatch — remove from CURRENT_MEMBERS defensively if present
            if name in CURRENT_MEMBERS:
                # we had them live-checked-in but CSV had no row; fix state and notify
                CURRENT_MEMBERS.discard(name)
                web_client.chat_postMessage(channel=event["channel"],
                                            text="⚠️ Inconsistency detected: you were marked in-memory as checked in but no CSV session found. I've cleared your live state. Please check in again.")
                return
            else:
                web_client.chat_postMessage(channel=event["channel"], text="⚠️ You’re not currently checked in.")
                return

        # remove from live set if present
        CURRENT_MEMBERS.discard(name)

        web_client.chat_postMessage(channel=event["channel"],
                                    text=f"👋 Checked you out at {checkout_time.strftime('%H:%M:%S')}.")

        # Notify lead for approval (if lead available)
        if lead_slack:
            # compute hours again safely from returned check_in if available
            try:
                if open_checkin_iso:
                    t1 = datetime.fromisoformat(open_checkin_iso)
                    hrs = round((checkout_time - t1).total_seconds() / 3600, 2)
                else:
                    hrs = round(hours, 2)
            except Exception:
                hrs = round(hours, 2)
            msg = (f"🧾 {name} checked out.\nHours worked: {hrs}\n"
                   f"To review pending sessions: `approve pending {name}`\n"
                   f"To approve a specific session: `approve {name} <number>`\n"
                   f"To disapprove a specific session: `disapprove {name} <number>`")
            try:
                web_client.chat_postMessage(channel=lead_slack, text=msg)
            except SlackApiError as e:
                print("Error DMing lead:", e.response["error"])

        # announce closed if last person
        print(len(CURRENT_MEMBERS))
        if len(CURRENT_MEMBERS) == 0:
            post_to_channel(ANNOUNCE_CHANNEL_ID, f" Shop closed. Last person out: {name}")
        return

    # APPROVAL / DISAPPROVAL HANDLING
    if text_lc.startswith("approve ") or text_lc.startswith("disapprove "):
        parts = text.split()
        cmd = parts[0].lower()

        if len(parts) >= 3 and parts[1].lower() == "pending":
            target_name = " ".join(parts[2:]).strip()
            approver_id = slack_id
            lead_for = [m["member_name"] for m in members.values() if m.get("lead_slack_id") == approver_id]
            if not any(t.strip().lower() == target_name.strip().lower() for t in lead_for):
                web_client.chat_postMessage(channel=event["channel"],
                                            text="🚫 You’re not authorized to view pending sessions for that member.")
                return
            pending = get_unapproved_sessions_with_indices(target_name)
            if not pending:
                web_client.chat_postMessage(channel=event["channel"], text=f"✅ No pending sessions for {target_name}.")
                return
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

        if len(parts) >= 3 and parts[-1].isdigit():
            session_num = int(parts[-1])
            target_name = " ".join(parts[1:-1]).strip()
            if session_num <= 0:
                web_client.chat_postMessage(channel=event["channel"], text="⚠️ Session number must be >= 1.")
                return
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

        web_client.chat_postMessage(channel=event["channel"],
                                    text="Usage:\n• `approve pending <Name>`\n• `approve <Name> <number>`\n• `approve all <Name>`\n• `disapprove <Name> <number>`")
        return

    # WHO IS IN
    if "who is in" in text_lc or "who's in" in text_lc:
        current = list(CURRENT_MEMBERS)
        if current:
            reply = " Checked in:\n• " + "\n• ".join(current)
        else:
            reply = " No one is currently checked in."
        web_client.chat_postMessage(channel=event["channel"], text=reply)
        return

    # HELP / FALLBACK
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
