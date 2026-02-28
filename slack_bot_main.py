import os
import sys
import csv
import time
import random
import signal
import logging
import tempfile
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.errors import SlackApiError

from get_members import update_members_csv

# --------------------------
# Configuration
# --------------------------
_BASE_DIR = sys._MEIPASS if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_BASE_DIR, ".env"))

SLACK_BOT_TOKEN     = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN     = os.getenv("SLACK_APP_TOKEN")
ANNOUNCE_CHANNEL_ID = "C09MS0MFKBK"
ADMIN_SLACK_ID      = "U07U7V298Q2"
MEMBERS_FILE        = "members.csv"
ATTENDANCE_FILE   = "attendance.csv"
ATTENDANCE_HEADERS = ["card_uid", "member_name", "check_in", "check_out", "hours", "approved"]

# Sessions open longer than this are considered stale (likely left open by a power cut)
STALE_SESSION_HOURS = 12

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

# Live in-memory state
CURRENT_MEMBERS = set()
USE_FORMAL_MODE  = False

web_client    = WebClient(token=SLACK_BOT_TOKEN)
socket_client = SocketModeClient(app_token=SLACK_APP_TOKEN, web_client=web_client)

# --------------------------
# Logging setup
# --------------------------
def setup_logging():
    """
    Log to both a rotating file (bot.log, max 2MB, 5 backups) and stdout.
    This means logs survive restarts and can be inspected after a power cut.
    """
    log_format = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = RotatingFileHandler(
        "bot.log", maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(log_format)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(log_format)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

logger = logging.getLogger(__name__)

# --------------------------
# Atomic CSV write
# --------------------------
def _atomic_write_csv(filepath, headers, rows):
    """
    Write to a temp file in the same directory, then atomically replace
    the real file via os.replace(). Safe against power loss mid-write —
    you will always end up with either the old file or the new file, never
    a half-written one.
    """
    dirpath = os.path.dirname(os.path.abspath(filepath))
    fd, tmp_path = tempfile.mkstemp(dir=dirpath, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp_path, filepath)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

# --------------------------
# Attendance CSV helpers
# --------------------------
def ensure_attendance_file():
    if not os.path.exists(ATTENDANCE_FILE):
        _atomic_write_csv(ATTENDANCE_FILE, ATTENDANCE_HEADERS, [])
        logger.info("Created new attendance.csv")

def read_attendance_rows():
    ensure_attendance_file()
    with open(ATTENDANCE_FILE, "r", newline="") as f:
        return list(csv.DictReader(f))

def write_attendance_rows(rows):
    _atomic_write_csv(ATTENDANCE_FILE, ATTENDANCE_HEADERS, rows)

def append_session(card_uid, name, check_in_dt):
    """Append a new check-in row. Uses atomic write to avoid corruption."""
    rows = read_attendance_rows()
    rows.append({
        "card_uid":    card_uid,
        "member_name": name,
        "check_in":    check_in_dt.isoformat(),
        "check_out":   "",
        "hours":       "0.0",
        "approved":    "False",
    })
    write_attendance_rows(rows)

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
            if rows[i]["member_name"].strip().lower() == member_name.strip().lower() \
                    and not rows[i]["check_out"].strip():
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
    row["hours"]     = hours
    row["approved"]  = "False"
    write_attendance_rows(rows)
    logger.info(f"Session closed for {member_name}: {check_in_iso} -> {checkout_dt.isoformat()} ({hours}h)")
    return hours, check_in_iso

def get_unapproved_sessions(member_name):
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
    name = rows[global_index].get("member_name", "?")
    rows.pop(global_index)
    write_attendance_rows(rows)
    logger.info(f"Session deleted for {name} at index {global_index}")
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
# Startup recovery
# --------------------------
def rebuild_current_members():
    """
    On startup, scan the attendance CSV for any sessions with no check_out.
    These represent people who were checked in when the bot last stopped
    (whether due to a power cut, crash, or normal shutdown).

    Sessions open longer than STALE_SESSION_HOURS are flagged as stale —
    they are NOT added to CURRENT_MEMBERS since that person almost certainly
    left. Stale sessions are left open in the CSV so a human can review and
    close them manually, and the admin is notified.
    """
    rows = read_attendance_rows()
    now = datetime.now()
    stale = []
    recovered = []

    # Track which names we've already processed so we only act on the most
    # recent open session per person (handles edge case of duplicate open rows)
    seen_names = set()

    for row in reversed(rows):
        if row.get("check_out", "").strip():
            continue  # already closed

        name = row.get("member_name", "").strip()
        if name in seen_names:
            continue
        seen_names.add(name)

        try:
            check_in_dt = datetime.fromisoformat(row["check_in"])
            age_hours = (now - check_in_dt).total_seconds() / 3600
        except (ValueError, TypeError):
            age_hours = 0

        if age_hours > STALE_SESSION_HOURS:
            stale.append((name, row["check_in"], round(age_hours, 1)))
            logger.warning(
                f"Stale open session found for {name} (checked in {row['check_in']}, "
                f"{round(age_hours, 1)}h ago) — NOT restoring to CURRENT_MEMBERS."
            )
        else:
            CURRENT_MEMBERS.add(name)
            recovered.append(name)
            logger.info(f"Restored {name} to CURRENT_MEMBERS (session started {row['check_in']})")

    if recovered:
        logger.info(f"Recovered {len(recovered)} active session(s) after restart: {', '.join(recovered)}")

    if stale:
        stale_lines = "\n".join(
            f"- {name} (checked in {ci}, {age}h ago)"
            for name, ci, age in stale
        )
        msg = (
            f"⚠️ Bot restarted and found {len(stale)} stale open session(s) "
            f"(open >{STALE_SESSION_HOURS}h). These were NOT restored and need manual review:\n"
            f"{stale_lines}\n\n"
            f"To close a session manually, use: `admin force checkout <name>`"
        )
        _post_direct(ADMIN_SLACK_ID, msg)
        logger.warning(f"Notified admin of {len(stale)} stale session(s).")

    return recovered, stale

# --------------------------
# Member CSV helpers
# --------------------------
def load_members():
    if not os.path.exists(MEMBERS_FILE):
        return {}
    with open(MEMBERS_FILE, "r", newline="") as f:
        return {
            row["slack_id"].strip(): {k: v.strip() for k, v in row.items()}
            for row in csv.DictReader(f)
        }

def get_seniority(member):
    """1 = most senior, 5 = most junior. Defaults to 5 on bad data."""
    try:
        val = int(member.get("seniority", 5))
        if val < 1 or val > 5:
            raise ValueError
        return val
    except (ValueError, TypeError):
        logger.warning(f"Invalid seniority value for {member.get('member_name', '?')}: "
                       f"'{member.get('seniority')}' — defaulting to 5")
        return 5

# --------------------------
# Seniority-based notification helpers
# --------------------------
def find_most_senior_in_shop(members, exclude_name=None):
    candidates = [
        m for m in members.values()
        if m["member_name"] in CURRENT_MEMBERS
        and m["member_name"] != exclude_name
    ]
    if not candidates:
        return None
    best = min(candidates, key=lambda m: (get_seniority(m), m["member_name"]))
    return best["slack_id"]

def find_notify_target(check_in_iso, checkout_dt, checking_out_member, members):
    """
    Notification priority for when the shop empties:
      1. Most senior person co-present during the session (from attendance log)
      2. Member's designated lead (fallback for solo sessions)
      3. Admin (last resort if lead is unset)
    """
    exclude_name = checking_out_member["member_name"]
    lead_id = checking_out_member.get("lead_slack_id", "").strip()

    try:
        session_start = datetime.fromisoformat(check_in_iso)
    except (ValueError, TypeError):
        logger.warning(f"Could not parse check_in '{check_in_iso}' for {exclude_name} — falling back to lead/admin.")
        return lead_id or ADMIN_SLACK_ID

    name_to_member = {
        m["member_name"].strip().lower(): m
        for m in members.values()
    }

    # Only look at attendance within a 24h window to avoid false matches
    # from old sessions on different days
    window_start = checkout_dt - timedelta(hours=24)

    co_present = []
    for row in read_attendance_rows():
        row_name = row.get("member_name", "").strip()
        if row_name.lower() == exclude_name.strip().lower():
            continue
        if row_name.lower() not in name_to_member:
            continue

        try:
            row_checkin = datetime.fromisoformat(row["check_in"])
        except (ValueError, TypeError):
            continue

        # Ignore rows outside our 24h window
        if row_checkin < window_start:
            continue

        row_checkout_str = row.get("check_out", "").strip()
        if row_checkout_str:
            try:
                row_checkout = datetime.fromisoformat(row_checkout_str)
            except (ValueError, TypeError):
                continue
            overlaps = row_checkin < checkout_dt and row_checkout > session_start
        else:
            overlaps = row_checkin < checkout_dt

        if overlaps:
            co_present.append(name_to_member[row_name.lower()])

    if co_present:
        best = min(co_present, key=lambda m: (get_seniority(m), m["member_name"]))
        logger.info(f"Notifying most senior co-present member: {best['member_name']}")
        return best["slack_id"]

    if lead_id:
        logger.info(f"{exclude_name} was alone — notifying lead {lead_id}")
        return lead_id

    logger.warning(f"No lead set for {exclude_name} — falling back to admin")
    return ADMIN_SLACK_ID

# --------------------------
# Slack posting helpers
# --------------------------
def _post_direct(channel, text, retries=3):
    """Post a message with retry on rate limiting."""
    for attempt in range(retries):
        try:
            web_client.chat_postMessage(channel=channel, text=text)
            return
        except SlackApiError as e:
            err = e.response["error"]
            if err == "ratelimited" and attempt < retries - 1:
                wait = int(e.response.headers.get("Retry-After", 5))
                logger.warning(f"Rate limited posting to {channel}. Retrying in {wait}s...")
                time.sleep(wait)
            else:
                logger.error(f"Failed to post to {channel} after {attempt + 1} attempt(s): {err}")
                return

def post(channel, text):
    _post_direct(channel, text)

def reply(event, text):
    post(event["channel"], text)

def is_authorized_approver(approver_id, target_name, members):
    """
    Authorized if the approver is either:
      - More senior (lower seniority number) than the target, OR
      - The target's designated lead
    """
    approver = members.get(approver_id)
    if not approver:
        return False
    target = next(
        (m for m in members.values() if m["member_name"].strip().lower() == target_name.strip().lower()),
        None
    )
    if not target:
        return False
    is_more_senior = get_seniority(approver) < get_seniority(target)
    is_lead = target.get("lead_slack_id", "").strip() == approver_id
    return is_more_senior or is_lead

# --------------------------
# Command handlers
# --------------------------
def handle_check_in(event, member):
    name     = member["member_name"]
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
        logger.info(f"{name} checked in at {check_in_time.isoformat()}")
    except Exception as e:
        logger.error(f"Failed to append session for {name}: {e}")
        reply(event, "Failed to record check-in. Please try again or contact an admin.")
        return

    reply(event, f"Checked in at {check_in_time.strftime('%H:%M:%S')}.")

    if was_empty:
        open_msg = FORMAL_OPEN_MESSAGE if USE_FORMAL_MODE else f"{random.choice(SHOP_OPEN_MESSAGES)}."
        post(ANNOUNCE_CHANNEL_ID, f"{open_msg} {name} checked in.")


def handle_check_out(event, member):
    name     = member["member_name"]
    card_uid = member["card_uid"]
    checkout_time = datetime.now()
    members  = load_members()

    hours, check_in_iso = close_open_session(card_uid, name, checkout_time)

    if hours is None:
        if name in CURRENT_MEMBERS:
            CURRENT_MEMBERS.discard(name)
            logger.warning(f"{name} was in CURRENT_MEMBERS but had no open CSV session — cleared.")
            reply(event, "Inconsistency detected: you were marked as checked in but no CSV session was found. "
                         "Your live state has been cleared - please check in again.")
        else:
            reply(event, "You're not currently checked in.")
        return

    try:
        hrs = round((checkout_time - datetime.fromisoformat(check_in_iso)).total_seconds() / 3600, 2) \
              if check_in_iso else round(hours, 2)
    except (ValueError, TypeError):
        hrs = round(hours, 2)

    CURRENT_MEMBERS.discard(name)
    reply(event, f"Checked out at {checkout_time.strftime('%H:%M:%S')}.")

    if CURRENT_MEMBERS:
        notify_id = find_most_senior_in_shop(members, exclude_name=name)
    else:
        notify_id = find_notify_target(check_in_iso, checkout_time, member, members)

    if notify_id:
        post(notify_id,
             f"{name} checked out. Hours worked: {hrs}\n"
             f"- `approve pending {name}` to view pending sessions\n"
             f"- `approve {name} <number>` to approve a specific session\n"
             f"- `disapprove {name} <number>` to remove a specific session")

    if len(CURRENT_MEMBERS) == 0:
        post(ANNOUNCE_CHANNEL_ID, f"Shop closed. Last person out: {name}")


def handle_admin_force_checkout(event, slack_id, parts, members):
    """
    `admin force checkout <member name>`
    Available to any seniority-1 member or the designated admin.
    Lets authorized users manually close a stale open session without
    the member needing to DM the bot. Useful after a power cut.
    """
    approver = members.get(slack_id)
    is_seniority_1 = approver and get_seniority(approver) == 1
    is_admin = slack_id == ADMIN_SLACK_ID

    if not is_seniority_1 and not is_admin:
        reply(event, "You're not authorized. Only seniority-1 members or the admin can force check out.")
        return

    if len(parts) < 4:
        reply(event, "Usage: `admin force checkout <member name>`")
        return

    target_name = " ".join(parts[3:])
    checkout_time = datetime.now()

    # Try to find an open session by name
    rows = read_attendance_rows()
    target_idx = None
    for i in range(len(rows) - 1, -1, -1):
        if rows[i]["member_name"].strip().lower() == target_name.strip().lower() \
                and not rows[i]["check_out"].strip():
            target_idx = i
            break

    if target_idx is None:
        reply(event, f"No open session found for '{target_name}'.")
        return

    row = rows[target_idx]
    try:
        t1 = datetime.fromisoformat(row["check_in"])
        hrs = round((checkout_time - t1).total_seconds() / 3600, 2)
    except (ValueError, TypeError):
        hrs = 0.0

    row["check_out"] = checkout_time.isoformat()
    row["hours"]     = hrs
    row["approved"]  = "False"
    write_attendance_rows(rows)
    CURRENT_MEMBERS.discard(target_name)

    logger.info(f"Admin force-closed session for {target_name} ({hrs}h)")
    reply(event, f"✅ Force closed session for {target_name}. Hours recorded: {hrs}")

    if len(CURRENT_MEMBERS) == 0:
        post(ANNOUNCE_CHANNEL_ID, f"Shop closed. Last person out: {target_name} (admin force checkout)")


def handle_approve_disapprove(event, slack_id, text, members):
    parts = text.split()
    cmd   = parts[0].lower()

    if len(parts) >= 3 and parts[1].lower() == "pending":
        target_name = " ".join(parts[2:])
        if not is_authorized_approver(slack_id, target_name, members):
            reply(event, "You're not authorized to view pending sessions for that member.")
            return
        pending = get_unapproved_sessions(target_name)
        if not pending:
            reply(event, f"No pending sessions for {target_name}.")
            return
        lines = [f"Pending sessions for {target_name}:"]
        for i, (_, row) in enumerate(pending, start=1):
            lines.append(f"{i}. check_in: {row['check_in']}  check_out: {row['check_out'] or '(open)'}  hours: {row['hours'] or '0.0'}")
        lines += ["", "- `approve <name> <number>` to approve", "- `disapprove <name> <number>` to remove"]
        reply(event, "\n".join(lines))
        return

    if cmd == "approve" and len(parts) >= 3 and parts[1].lower() == "all":
        target_name = " ".join(parts[2:])
        if not is_authorized_approver(slack_id, target_name, members):
            reply(event, "You're not authorized to approve hours for that member.")
            return
        count = approve_all_sessions(target_name)
        reply(event, f"Approved {count} session(s) for {target_name}.")
        return

    if len(parts) >= 3 and parts[-1].isdigit():
        session_num = int(parts[-1])
        target_name = " ".join(parts[1:-1])
        if session_num <= 0:
            reply(event, "Session number must be 1 or greater.")
            return
        if not is_authorized_approver(slack_id, target_name, members):
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
        "- `approve pending <name>`\n"
        "- `approve <name> <number>`\n"
        "- `approve all <name>`\n"
        "- `disapprove <name> <number>`"
    ))


def handle_announcement_formal(event, slack_id):
    global USE_FORMAL_MODE
    if slack_id != ADMIN_SLACK_ID:
        reply(event, "You're not authorized to use this command.")
        return
    USE_FORMAL_MODE = True
    logger.info("Formal announcement mode enabled")
    reply(event, f"Formal mode enabled. All future shop-open announcements will use:\n\"{FORMAL_OPEN_MESSAGE}\"")


def handle_announcement_casual(event, slack_id):
    global USE_FORMAL_MODE
    if slack_id != ADMIN_SLACK_ID:
        reply(event, "You're not authorized to use this command.")
        return
    USE_FORMAL_MODE = False
    logger.info("Casual announcement mode restored")
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

    text         = event.get("text", "").strip()
    text_lc      = text.lower()
    slack_id     = event.get("user")
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
    parts  = text_lc.split()

    logger.info(f"Command from {member['member_name']} ({slack_id}): {text!r}")

    if "check in" in text_lc:
        handle_check_in(event, member)
    elif "check out" in text_lc:
        handle_check_out(event, member)
    elif text_lc.startswith("admin "):
        if len(parts) >= 3 and parts[1] == "force" and parts[2] == "checkout":
            handle_admin_force_checkout(event, slack_id, parts, members)
        else:
            reply(event, "Unknown admin command. Available: `admin force checkout <name>`")
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
            "- `approve pending <name>`\n"
            "- `approve <name> <number>`\n"
            "- `approve all <name>`\n"
            "- `disapprove <name> <number>`\n"
            "- `announcement formal` / `announcement casual` (admin only)\n"
            "- `admin force checkout <name>` (admin only)"
        ))


# --------------------------
# Graceful shutdown
# --------------------------
def handle_shutdown(signum, frame):
    logger.info(f"Received signal {signum}. Shutting down gracefully...")
    # Log who is still checked in so it's easy to reconstruct state
    if CURRENT_MEMBERS:
        logger.info(f"Members still checked in at shutdown: {', '.join(sorted(CURRENT_MEMBERS))}")
    sys.exit(0)


# --------------------------
# Startup
# --------------------------
setup_logging()
logger.info("=" * 60)
logger.info("Bot starting up")

logger.info("Syncing members list...")
try:
    update_members_csv()
except Exception as e:
    logger.error(f"Member sync failed: {e} — continuing with existing members.csv")

ensure_attendance_file()

logger.info("Rebuilding in-memory state from attendance log...")
recovered, stale = rebuild_current_members()
if recovered:
    logger.info(f"Shop currently has {len(recovered)} active member(s): {', '.join(sorted(recovered))}")
else:
    logger.info("Shop is empty at startup.")

# Register signal handlers for graceful shutdown
signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT,  handle_shutdown)

socket_client.socket_mode_request_listeners.append(process_message)
socket_client.connect()
logger.info("Slack attendance bot running and connected.")

try:
    while True:
        time.sleep(1)
except (KeyboardInterrupt, SystemExit):
    logger.info("Shutting down...")