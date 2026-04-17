import os
import sys
import csv
import time
import random
import re
import signal
import threading
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
_ASSET_DIR = sys._MEIPASS if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
_DATA_DIR  = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))

load_dotenv(os.path.join(_ASSET_DIR, ".env"))

SLACK_BOT_TOKEN     = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN     = os.getenv("SLACK_APP_TOKEN")
ANNOUNCE_CHANNEL_ID = "C09MS0MFKBK"
ADMIN_SLACK_ID      = "U07U7V298Q2"
MEMBERS_FILE        = os.path.join(_DATA_DIR, "members.csv")
ATTENDANCE_FILE     = os.path.join(_DATA_DIR, "attendance.csv")
ATTENDANCE_HEADERS  = ["session_id", "member_name", "check_in_date", "check_in_time",
                        "check_out_date", "check_out_time", "hours", "approved"]

# --------------------------
# Semester configuration (Stony Brook University academic calendar)
# --------------------------
SEMESTERS = {
    "Winter": {
        "ranges": [
            (12, 21, 12, 31),
            (1,   1,  1, 26),
        ]
    },
    "Spring": {"ranges": [(1, 27, 5, 31)]},
    "Summer": {"ranges": [(6,  1, 8, 14)]},
    "Fall":   {"ranges": [(8, 15, 12, 20)]},
}

STALE_SESSION_HOURS = 12

# --------------------------
# Session watchdog configuration
# --------------------------
SESSION_CHECK_HOURS         = 3
SESSION_RESPONSE_MINUTES    = 30
SESSION_AUTO_CHECKOUT_HOURS = 8
WATCHDOG_INTERVAL_SECONDS   = 60

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

CURRENT_MEMBERS = set()
USE_FORMAL_MODE  = False

SESSION_ALERTS = {}
SENIOR_PENDING = {}

web_client    = WebClient(token=SLACK_BOT_TOKEN)
socket_client = SocketModeClient(app_token=SLACK_APP_TOKEN, web_client=web_client)

# --------------------------
# Logging setup
# --------------------------
def setup_logging():
    log_format = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler = RotatingFileHandler(
        os.path.join(_DATA_DIR, "bot.log"), maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
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

def dt_to_row(dt):
    if dt is None:
        return "", ""
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M:%S")

def row_to_dt(row, prefix):
    d = row.get(f"{prefix}_date", "").strip()
    t = row.get(f"{prefix}_time", "").strip()
    if not d:
        return None
    try:
        return datetime.fromisoformat(f"{d} {t}" if t else d)
    except (ValueError, TypeError):
        return None

def append_session(card_uid, name, check_in_dt):
    ci_date, ci_time = dt_to_row(check_in_dt)
    rows = read_attendance_rows()
    max_id = 0
    for r in rows:
        try:
            max_id = max(max_id, int(r.get("session_id", 0)))
        except (ValueError, TypeError):
            pass
    rows.append({
        "session_id":     str(max_id + 1),
        "member_name":    name,
        "check_in_date":  ci_date,
        "check_in_time":  ci_time,
        "check_out_date": "",
        "check_out_time": "",
        "hours":          "0.0",
        "approved":       "False",
    })
    write_attendance_rows(rows)

def get_open_session(member_name):
    for row in reversed(read_attendance_rows()):
        if row["member_name"].strip().lower() == member_name.strip().lower() \
                and not row.get("check_out_date", "").strip():
            return row
    return None

def close_open_session(card_uid, member_name, checkout_dt):
    rows = read_attendance_rows()
    target = None
    for i in range(len(rows) - 1, -1, -1):
        if rows[i]["member_name"].strip().lower() == member_name.strip().lower() \
                and not rows[i].get("check_out_date", "").strip():
            target = i
            break
    if target is None:
        return None, None
    row = rows[target]
    t1 = row_to_dt(row, "check_in")
    check_in_iso = f"{row.get('check_in_date', '')} {row.get('check_in_time', '')}".strip()
    hours = round((checkout_dt - t1).total_seconds() / 3600, 2) if t1 else 0.0
    co_date, co_time = dt_to_row(checkout_dt)
    row["check_out_date"] = co_date
    row["check_out_time"] = co_time
    row["hours"]    = hours
    row["approved"] = "False"
    write_attendance_rows(rows)
    logger.info(f"Session closed for {member_name}: {check_in_iso} -> {co_date} {co_time} ({hours}h)")
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
    rows[global_index]["approved"] = "Disapproved"
    write_attendance_rows(rows)
    logger.info(f"Session disapproved for {name} at index {global_index}")
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
    rows = read_attendance_rows()
    now = datetime.now()
    stale = []
    recovered = []
    seen_names = set()

    for row in reversed(rows):
        if row.get("check_out_date", "").strip():
            continue
        name = row.get("member_name", "").strip()
        if name in seen_names:
            continue
        seen_names.add(name)
        try:
            check_in_dt = row_to_dt(row, "check_in")
            age_hours = (now - check_in_dt).total_seconds() / 3600
        except (ValueError, TypeError):
            age_hours = 0

        if age_hours > STALE_SESSION_HOURS:
            stale.append((name, f"{row.get('check_in_date','')} {row.get('check_in_time','')}".strip(), round(age_hours, 1)))
            logger.warning(
                f"Stale open session found for {name} (checked in "
                f"{row.get('check_in_date','')} {row.get('check_in_time','')}, "
                f"{round(age_hours, 1)}h ago) — NOT restoring to CURRENT_MEMBERS."
            )
        else:
            CURRENT_MEMBERS.add(name)
            recovered.append(name)
            logger.info(f"Restored {name} to CURRENT_MEMBERS (session started "
                        f"{row.get('check_in_date','')} {row.get('check_in_time','')})")

    if recovered:
        logger.info(f"Recovered {len(recovered)} active session(s) after restart: {', '.join(recovered)}")

    if stale:
        stale_lines = "\n".join(
            f"- {name} (checked in {ci}, {age}h ago)" for name, ci, age in stale
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
        result = {}
        for row in csv.DictReader(f):
            cleaned = {
                k: (v.strip() if isinstance(v, str) else (v[0].strip() if isinstance(v, list) and v else ""))
                for k, v in row.items()
                if k is not None
            }
            slack_id = cleaned.get("slack_id", "")
            if slack_id:
                result[slack_id] = cleaned
        return result

MEMBERS_HEADERS = ["card_uid", "member_name", "slack_id", "seniority", "lead_slack_id"]

def write_members(members_dict):
    rows = list(members_dict.values())
    _atomic_write_csv(MEMBERS_FILE, MEMBERS_HEADERS, rows)

def parse_mention(token):
    """
    Extract a Slack ID from a mention token like <@U07U7V298Q2> or <@U07U7V298Q2|name>.
    Returns the uppercase Slack ID string, or None if the token is not a mention.
    Works correctly on lowercased input because it calls .upper() on the captured group.
    """
    m = re.match(r"<@([A-Za-z0-9]+)(?:[|][^>]*)?>", token)
    return m.group(1).upper() if m else None

def resolve_member(token, members):
    """
    Resolve a member from either a @mention token or a plain name string.
    Handles lowercased mention tokens (from text_lc) correctly via parse_mention.
    Returns the member dict or None.
    """
    slack_id = parse_mention(token)
    if slack_id:
        return members.get(slack_id)
    name_lc = token.strip().lower()
    return next(
        (m for m in members.values() if m["member_name"].strip().lower() == name_lc),
        None
    )

def extract_mention_and_rest(text, members):
    """
    Given a string that may start with a @mention or a name, return
    (member, remainder) where remainder is the text after the mention/name.
    Tries @mention first, then falls back to longest-prefix name match.
    """
    tokens = text.split()
    if not tokens:
        return None, text

    if tokens[0].startswith("<@"):
        slack_id = parse_mention(tokens[0])
        member = members.get(slack_id) if slack_id else None
        return member, " ".join(tokens[1:]).strip()

    for end in range(len(tokens), 0, -1):
        candidate = " ".join(tokens[:end]).lower()
        m = next(
            (mem for mem in members.values() if mem["member_name"].strip().lower() == candidate),
            None
        )
        if m:
            return m, " ".join(tokens[end:]).strip()

    return None, text

def get_seniority(member):
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
    exclude_name = checking_out_member["member_name"]
    lead_id = checking_out_member.get("lead_slack_id", "").strip()

    try:
        session_start = datetime.fromisoformat(check_in_iso)
    except (ValueError, TypeError):
        logger.warning(f"Could not parse check_in '{check_in_iso}' for {exclude_name} — falling back to lead/admin.")
        return lead_id or ADMIN_SLACK_ID

    name_to_member = {m["member_name"].strip().lower(): m for m in members.values()}
    window_start = checkout_dt - timedelta(hours=24)
    co_present = []

    for row in read_attendance_rows():
        row_name = row.get("member_name", "").strip()
        if row_name.lower() == exclude_name.strip().lower():
            continue
        if row_name.lower() not in name_to_member:
            continue
        try:
            row_checkin = row_to_dt(row, "check_in")
        except (ValueError, TypeError):
            continue
        if row_checkin < window_start:
            continue
        row_checkout_str = row.get("check_out_date", "").strip()
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
# Session watchdog
# --------------------------
def _auto_checkout_member(name, members):
    checkout_time = datetime.now()
    member = next((m for m in members.values() if m["member_name"].strip() == name), None)
    card_uid = member["card_uid"] if member else "ABC123"

    hours, check_in_iso = close_open_session(card_uid, name, checkout_time)
    CURRENT_MEMBERS.discard(name)
    SESSION_ALERTS.pop(name, None)
    logger.info(f"Watchdog auto-checked out {name} after no response ({hours}h)")

    try:
        hrs = round((checkout_time - datetime.fromisoformat(check_in_iso)).total_seconds() / 3600, 2) \
              if check_in_iso else round(hours or 0, 2)
    except (ValueError, TypeError):
        hrs = round(hours or 0, 2)

    if member:
        post(member["slack_id"],
             f"You have been automatically checked out after no response. "
             f"Hours recorded: {hrs}. If this is incorrect, contact an admin.")

    if member and check_in_iso:
        if CURRENT_MEMBERS:
            notify_id = find_most_senior_in_shop(members, exclude_name=name)
        else:
            notify_id = find_notify_target(check_in_iso, checkout_time, member, members)
        if notify_id:
            post(notify_id,
                 f"{name} was auto-checked out after no response to inactivity check. "
                 f"Hours recorded: {hrs}\n"
                 f"- `approve pending {name}` to review")

    if len(CURRENT_MEMBERS) == 0:
        post(ANNOUNCE_CHANNEL_ID, f"Shop closed. {name} was auto-checked out after inactivity.")

def _send_member_alert(name, member_slack_id, elapsed_h):
    post(member_slack_id,
         f"You have been checked in for {elapsed_h:.1f} hours. Are you still in the shop?\n"
         f"Reply *y* to confirm, or `check out` if you have left.")

def _send_senior_alert(senior_slack_id, member_name, elapsed_h):
    post(senior_slack_id,
         f"{member_name} has been in the shop for {elapsed_h:.1f} hours and has not responded "
         f"to the inactivity check.\n"
         f"Reply *y* if they are still present, or ignore this message to allow auto-checkout "
         f"in {SESSION_RESPONSE_MINUTES} minutes.")

def _watchdog_tick():
    if not CURRENT_MEMBERS:
        return
    members = load_members()
    now = datetime.now()
    for name in list(CURRENT_MEMBERS):
        open_row = None
        for row in reversed(read_attendance_rows()):
            if row.get("member_name", "").strip() == name and not row.get("check_out_date", "").strip():
                open_row = row
                break
        if not open_row:
            continue
        try:
            check_in_dt = row_to_dt(open_row, "check_in")
        except (ValueError, TypeError):
            continue

        elapsed_h = (now - check_in_dt).total_seconds() / 3600
        alert = SESSION_ALERTS.get(name)
        member = next((m for m in members.values() if m["member_name"].strip() == name), None)
        if not member:
            continue

        if elapsed_h >= SESSION_AUTO_CHECKOUT_HOURS:
            logger.info(f"Watchdog: {name} reached {SESSION_AUTO_CHECKOUT_HOURS}h hard limit — auto-checking out.")
            if alert and alert.get("senior_slack_id"):
                SENIOR_PENDING.pop(alert["senior_slack_id"], None)
            _auto_checkout_member(name, members)
            continue

        if alert is None and elapsed_h >= SESSION_CHECK_HOURS:
            logger.info(f"Watchdog: {name} has been in {elapsed_h:.1f}h — sending check-in ping.")
            SESSION_ALERTS[name] = {
                "stage":           "awaiting_member",
                "alert_sent_at":   now,
                "check_in_dt":     check_in_dt,
                "senior_slack_id": None,
            }
            _send_member_alert(name, member["slack_id"], elapsed_h)
            continue

        if alert is None:
            continue

        alert_age_min = (now - alert["alert_sent_at"]).total_seconds() / 60

        if alert["stage"] == "awaiting_member" and alert_age_min >= SESSION_RESPONSE_MINUTES:
            senior_slack_id = find_most_senior_in_shop(members, exclude_name=name)
            if senior_slack_id:
                logger.info(f"Watchdog: {name} did not respond — escalating to senior {senior_slack_id}.")
                alert["stage"]           = "awaiting_senior"
                alert["alert_sent_at"]   = now
                alert["senior_slack_id"] = senior_slack_id
                SENIOR_PENDING[senior_slack_id] = name
                _send_senior_alert(senior_slack_id, name, elapsed_h)
            else:
                logger.info(f"Watchdog: {name} did not respond and is alone — auto-checking out.")
                _auto_checkout_member(name, members)
            continue

        if alert["stage"] == "awaiting_senior" and alert_age_min >= SESSION_RESPONSE_MINUTES:
            logger.info(f"Watchdog: Senior did not respond for {name} — auto-checking out.")
            if alert.get("senior_slack_id"):
                SENIOR_PENDING.pop(alert["senior_slack_id"], None)
            _auto_checkout_member(name, members)
            continue

        if alert["stage"] == "confirmed_8h" and elapsed_h >= SESSION_AUTO_CHECKOUT_HOURS - 0.5:
            logger.info(f"Watchdog: {name} approaching 8h — sending final check-in ping.")
            alert["stage"]         = "awaiting_member"
            alert["alert_sent_at"] = now
            _send_member_alert(name, member["slack_id"], elapsed_h)
            continue

def start_watchdog():
    def loop():
        logger.info("Session watchdog started.")
        while True:
            try:
                _watchdog_tick()
            except Exception as e:
                logger.error(f"Watchdog error: {e}", exc_info=True)
            time.sleep(WATCHDOG_INTERVAL_SECONDS)
    t = threading.Thread(target=loop, daemon=True, name="SessionWatchdog")
    t.start()
    return t

def confirm_session(name, confirmed_by_slack_id, members):
    alert = SESSION_ALERTS.get(name)
    if not alert:
        return False
    elapsed_h = (datetime.now() - alert["check_in_dt"]).total_seconds() / 3600
    if elapsed_h >= SESSION_AUTO_CHECKOUT_HOURS:
        return False
    if alert.get("senior_slack_id"):
        SENIOR_PENDING.pop(alert["senior_slack_id"], None)
    logger.info(f"Session confirmed for {name} by {confirmed_by_slack_id}.")
    SESSION_ALERTS[name] = {
        "stage":           "confirmed_8h",
        "alert_sent_at":   datetime.now(),
        "check_in_dt":     alert["check_in_dt"],
        "senior_slack_id": None,
    }
    return True

# --------------------------
# Slack posting helpers
# --------------------------
def _post_direct(channel, text, retries=3):
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
                ci_dt = row_to_dt(existing, "check_in")
                since = ci_dt.strftime("%Y-%m-%d %H:%M:%S") if ci_dt else "unknown"
            except (ValueError, TypeError):
                since = f"{existing.get('check_in_date','')} {existing.get('check_in_time','')}".strip() or "unknown"
            reply(event, f"You are already checked in since {since}. Please `check out` first.")
        else:
            reply(event, "You are already checked in. Please `check out` first.")
        return

    was_empty     = len(CURRENT_MEMBERS) == 0
    check_in_time = datetime.now()

    try:
        append_session(card_uid, name, check_in_time)
        CURRENT_MEMBERS.add(name)
        SESSION_ALERTS.pop(name, None)
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
    name          = member["member_name"]
    card_uid      = member["card_uid"]
    checkout_time = datetime.now()
    members       = load_members()

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
    SESSION_ALERTS.pop(name, None)

    seniority = get_seniority(member)
    if seniority <= 2:
        count = approve_all_sessions(name)
        reply(event, f"Checked out at {checkout_time.strftime('%H:%M:%S')}. "
                     f"Hours auto-approved ({hrs}h) — eboard member.")
        logger.info(f"Auto-approved {count} session(s) for eboard member {name}")
    else:
        reply(event, f"Checked out at {checkout_time.strftime('%H:%M:%S')}.")

        if CURRENT_MEMBERS:
            notify_id = find_most_senior_in_shop(members, exclude_name=name)
        else:
            notify_id = find_notify_target(check_in_iso, checkout_time, member, members)

        if notify_id:
            post(notify_id,
                 f"{name} checked out. Hours worked: {hrs}\n"
                 f"- `approve @mention` to approve all pending\n"
                 f"- `disapprove @mention` to view sessions with IDs\n"
                 f"- `disapprove @mention <session_id>` to disapprove a specific session")

    if len(CURRENT_MEMBERS) == 0:
        post(ANNOUNCE_CHANNEL_ID, f"Shop closed. Last person out: {name}")


def handle_admin_force_checkout(event, slack_id, parts, members):
    """
    `admin force checkout <member name or @mention>`
    Available to any seniority-1 member or the designated admin.

    FIXED: parts come from text_lc.split() so mentions are lowercased.
    We now attempt to resolve the token as a @mention via resolve_member
    before falling back to a plain-name CSV scan, so both
    `admin force checkout @mention` and `admin force checkout First Last` work.
    """
    global SENIOR_PENDING
    approver       = members.get(slack_id)
    is_seniority_1 = approver and get_seniority(approver) == 1
    is_admin       = slack_id == ADMIN_SLACK_ID

    if not is_seniority_1 and not is_admin:
        reply(event, "You're not authorized. Only seniority-1 members or the admin can force check out.")
        return

    if len(parts) < 4:
        reply(event, "Usage: `admin force checkout <member name>`")
        return

    raw_target = " ".join(parts[3:]).strip()
    checkout_time = datetime.now()

    # FIXED: try to resolve as @mention first (handles lowercased mention from text_lc),
    # then fall back to a case-insensitive name scan of the open CSV sessions.
    resolved = resolve_member(raw_target, members)
    if resolved:
        target_name_input = resolved["member_name"]
    else:
        target_name_input = raw_target  # plain name — CSV scan below handles case-insensitivity

    rows = read_attendance_rows()
    target_idx = None
    for i in range(len(rows) - 1, -1, -1):
        if rows[i]["member_name"].strip().lower() == target_name_input.strip().lower() \
                and not rows[i].get("check_out_date", "").strip():
            target_idx = i
            break

    if target_idx is None:
        reply(event, f"No open session found for '{raw_target}'.")
        return

    row         = rows[target_idx]
    target_name = row["member_name"].strip()  # canonical casing from CSV

    try:
        t1  = row_to_dt(row, "check_in")
        hrs = round((checkout_time - t1).total_seconds() / 3600, 2)
    except (ValueError, TypeError):
        hrs = 0.0

    co_date, co_time = dt_to_row(checkout_time)
    row["check_out_date"] = co_date
    row["check_out_time"] = co_time
    row["hours"]          = hrs
    row["approved"]       = "False"
    write_attendance_rows(rows)

    CURRENT_MEMBERS.discard(target_name)
    SESSION_ALERTS.pop(target_name, None)
    if target_name in SENIOR_PENDING.values():
        SENIOR_PENDING = {k: v for k, v in SENIOR_PENDING.items() if v != target_name}

    logger.info(f"Admin force-closed session for {target_name} ({hrs}h)")
    reply(event, f"Force closed session for {target_name}. Hours recorded: {hrs}")

    if len(CURRENT_MEMBERS) == 0:
        post(ANNOUNCE_CHANNEL_ID, f"Shop closed. Last person out: {target_name} (force checkout)")


def handle_approve_disapprove(event, slack_id, text, members):
    """
    approve @mention / approve <name>           — approve ALL pending sessions
    disapprove @mention / disapprove <name>     — list pending sessions with session IDs
    disapprove @mention <id>                    — disapprove a specific session by global ID
    """
    parts = text.split()
    cmd   = parts[0].lower()
    rest  = " ".join(parts[1:]).strip()

    if not rest:
        reply(event, "Usage: `approve @mention` or `disapprove @mention` or `disapprove @mention <session_id>`")
        return

    trailing_id     = None
    rest_for_lookup = rest
    if cmd == "disapprove":
        rtokens = rest.split()
        if rtokens and rtokens[-1].isdigit():
            trailing_id     = int(rtokens[-1])
            rest_for_lookup = " ".join(rtokens[:-1]).strip()

    target, _ = extract_mention_and_rest(rest_for_lookup, members)
    if not target:
        reply(event, f"Member not found: {rest_for_lookup!r}. Use a @mention or their full name.")
        return

    target_name     = target["member_name"]
    target_slack_id = target["slack_id"]
    approver        = members.get(slack_id)
    approver_seniority = get_seniority(approver) if approver else 99

    is_self = slack_id == target_slack_id
    if is_self and approver_seniority > 2:
        reply(event, "You can't approve your own sessions.")
        return
    if not is_self and not is_authorized_approver(slack_id, target_name, members):
        reply(event, "You're not authorized to approve/disapprove sessions for that member.")
        return

    if cmd == "approve":
        count = approve_all_sessions(target_name)
        if count:
            reply(event, f"Approved {count} pending session(s) for {target_name}.")
        else:
            reply(event, f"No pending sessions to approve for {target_name}.")
        return

    if cmd == "disapprove" and trailing_id is not None:
        rows = read_attendance_rows()
        target_idx = None
        for i, row in enumerate(rows):
            try:
                if int(row.get("session_id", -1)) == trailing_id:
                    target_idx = i
                    break
            except (ValueError, TypeError):
                pass
        if target_idx is None:
            reply(event, f"Session #{trailing_id} not found.")
            return
        row = rows[target_idx]
        if row["member_name"].strip().lower() != target_name.lower():
            reply(event, f"Session #{trailing_id} does not belong to {target_name}.")
            return
        ok = delete_session(target_idx)
        reply(event, f"Disapproved session #{trailing_id} for {target_name}." if ok else "Failed.")
        return

    if cmd == "disapprove":
        pending = get_unapproved_sessions(target_name)
        if not pending:
            reply(event, f"No pending sessions for {target_name}.")
            return
        lines = [f"Pending sessions for *{target_name}*:"]
        lines.append(f"{'ID':<6} {'Check-in':<18} {'Check-out':<10} {'Hours'}")
        lines.append("-" * 46)
        for global_idx, row in pending:
            sid    = row.get("session_id", "?")
            ci_dt  = row_to_dt(row, "check_in")
            co_dt  = row_to_dt(row, "check_out")
            ci_str = ci_dt.strftime("%b %d %H:%M") if ci_dt else "?"
            co_str = co_dt.strftime("%H:%M") if co_dt else "(open)"
            hrs    = row.get("hours") or "0.0"
            lines.append(f"#{sid:<5} {ci_str:<18} {co_str:<10} {hrs}")
        lines.append("")
        lines.append("To disapprove: `disapprove @mention <session_id>`")
        reply(event, "\n".join(lines))
        return


def handle_set_member_field(event, slack_id, text, members):
    """
    `set seniority <name or @mention> <1-5>`
    `set lead <name or @mention> <lead name or @mention>`
    `set lead <name or @mention> none`

    Available to seniority-1 members and admin.

    FIXED (multiple):
    - Parameter renamed from text_lc to text (receives original-cased text from dispatcher).
    - subcmd comparison now uses text.lower() split so it's always case-insensitive.
    - set seniority: uses resolve_member() instead of plain name lookup.
    - set lead ... none: uses resolve_member() for target instead of plain name lookup.
    - set lead <target> <lead>: uses extract_mention_and_rest() for both target and lead
      instead of the split-and-lowercase-compare loop that failed on @mentions.
    """
    approver       = members.get(slack_id)
    is_seniority_1 = approver and get_seniority(approver) == 1
    is_admin       = slack_id == ADMIN_SLACK_ID

    if not is_seniority_1 and not is_admin:
        reply(event, "You're not authorized. Only seniority-1 members or the admin can use this command.")
        return

    # FIXED: split on the lowercased text for command detection so subcmd is always
    # lowercase, but keep original `text` for the rest portion so @mentions are
    # preserved in their original casing for parse_mention() to work correctly.
    parts_lc = text.lower().split(None, 2)
    if len(parts_lc) < 3:
        reply(event, "Usage:\n- `set seniority <name> <1-5>`\n- `set lead <name> <lead name>`\n- `set lead <name> none`")
        return

    subcmd = parts_lc[1]                        # always lowercase: "seniority" or "lead"
    rest   = text.split(None, 2)[2]             # original-cased: preserves <@U...> tokens

    # --- set seniority <name or @mention> <1-5> ---
    if subcmd == "seniority":
        tokens = rest.rsplit(None, 1)
        if len(tokens) < 2 or not tokens[1].isdigit():
            reply(event, "Usage: `set seniority <member name> <1-5>`")
            return
        new_seniority = int(tokens[1])
        if not (1 <= new_seniority <= 5):
            reply(event, "Seniority must be between 1 and 5.")
            return

        # FIXED: was a plain name-only next() lookup; now uses resolve_member()
        # so both @mentions and plain names work.
        target = resolve_member(tokens[0].strip(), members)
        if not target:
            reply(event, f"Member '{tokens[0].strip()}' not found.")
            return

        old_val = target.get("seniority", "?")
        target["seniority"] = str(new_seniority)
        write_members(members)
        logger.info(f"{approver['member_name']} set seniority for {target['member_name']}: {old_val} -> {new_seniority}")
        reply(event, f"Updated seniority for {target['member_name']}: {old_val} → {new_seniority}")
        return

    # --- set lead <name or @mention> <lead or none> ---
    if subcmd == "lead":
        rest_tokens = rest.split()

        # FIXED: "none" branch was using a plain name-only next() lookup;
        # now uses resolve_member() so `set lead @mention none` works.
        if rest_tokens and rest_tokens[-1].lower() == "none":
            target_str = " ".join(rest_tokens[:-1]).strip()
            target = resolve_member(target_str, members)
            if not target:
                reply(event, f"Member '{target_str}' not found.")
                return
            target["lead_slack_id"] = ""
            write_members(members)
            logger.info(f"{approver['member_name']} cleared lead for {target['member_name']}")
            reply(event, f"Cleared lead for {target['member_name']}.")
            return

        # FIXED: was a split-and-lowercase-compare loop that failed for @mentions on
        # both the target and lead sides. Now uses extract_mention_and_rest() for the
        # target first, then for the lead from the remainder — handles all combinations
        # of @mention and plain name for both arguments.
        target, remainder = extract_mention_and_rest(rest, members)
        if not target:
            reply(event, "Could not find target member.\n"
                         "Usage: `set lead <member> <lead>` or `set lead <member> none`")
            return

        if not remainder:
            reply(event, "Please specify a lead.\n"
                         "Usage: `set lead <member> <lead>` or `set lead <member> none`")
            return

        lead, _ = extract_mention_and_rest(remainder, members)
        if not lead:
            reply(event, f"Lead member not found: {remainder!r}. Use a @mention or their full name.")
            return

        if target["slack_id"] == lead["slack_id"]:
            reply(event, "A member can't be their own lead.")
            return

        target["lead_slack_id"] = lead["slack_id"]
        write_members(members)
        logger.info(f"{approver['member_name']} set lead for {target['member_name']} -> {lead['member_name']}")
        reply(event, f"Set lead for {target['member_name']} → {lead['member_name']}.")
        return

    reply(event, "Unknown subcommand. Use `set seniority` or `set lead`.")

def handle_register(event, slack_id, text, members):
    """
    `register @mention [name override]`
    Admin-only. Looks up the user's Slack display name, creates a member
    entry with seniority 5, no lead, and a placeholder card_uid.

    Optionally a plain-text name override can follow the mention:
      register @mention John Smith
    This is useful when the Slack display name is a username/handle rather
    than a real name.
    """
    if slack_id != ADMIN_SLACK_ID:
        reply(event, "You're not authorized. Only the admin can register new members.")
        return

    parts = text.split(None, 1)          # ["register", "<rest>"]
    if len(parts) < 2 or not parts[1].strip():
        reply(event, "Usage: `register @mention` or `register @mention Full Name`")
        return

    rest = parts[1].strip()
    rest_tokens = rest.split()

    # First token must be a @mention
    new_slack_id = parse_mention(rest_tokens[0])
    if not new_slack_id:
        reply(event, "Please provide a @mention as the first argument.\nUsage: `register @mention`")
        return

    # Optional name override supplied after the mention
    name_override = " ".join(rest_tokens[1:]).strip() if len(rest_tokens) > 1 else ""

    # Duplicate check
    if new_slack_id in members:
        existing = members[new_slack_id]
        reply(event, f"{existing['member_name']} is already registered (slack_id: {new_slack_id}).")
        return

    # Resolve display name from Slack
    if name_override:
        display_name = name_override
    else:
        try:
            info = web_client.users_info(user=new_slack_id)
            profile = info["user"]["profile"]
            # Prefer real_name, fall back to display_name, then username
            display_name = (
                profile.get("real_name", "").strip()
                or profile.get("display_name", "").strip()
                or info["user"].get("name", new_slack_id)
            )
        except SlackApiError as e:
            logger.error(f"Could not fetch Slack profile for {new_slack_id}: {e}")
            reply(event, f"Could not look up Slack profile for <@{new_slack_id}>. "
                         f"Try: `register @mention Full Name` to set the name manually.")
            return

    if not display_name:
        reply(event, f"Could not determine a display name for <@{new_slack_id}>. "
                     f"Use: `register @mention Full Name`")
        return

    # Generate a placeholder card_uid (8 hex chars, guaranteed unique within the file)
    import secrets
    existing_uids = {m.get("card_uid", "").upper() for m in members.values()}
    while True:
        card_uid = secrets.token_hex(4).upper()   # e.g. "A3F2C109"
        if card_uid not in existing_uids:
            break

    new_member = {
        "card_uid":      card_uid,
        "member_name":   display_name,
        "slack_id":      new_slack_id,
        "seniority":     "5",
        "lead_slack_id": "",
    }
    members[new_slack_id] = new_member
    write_members(members)

    logger.info(f"Admin registered new member: {display_name} ({new_slack_id}), card_uid={card_uid}")
    reply(event, (
        f" Registered *{display_name}* (<@{new_slack_id}>)\n"
        f"• Seniority: 5 (lowest by default)\n"
        # f"• Card UID: `{card_uid}` (placeholder, update if they have a physical card)\n"
        f"• Lead: not set\n\n"
        f"To update: `set seniority @mention <1-5>` · `set lead @mention @lead`"
    ))
    # Notify the new member
    try:
        post(new_slack_id,
             f"You've been registered in the shop attendance system by an admin. "
             f"You can now use `check in` / `check out` here in DMs.\n"
             f"To set your lead: `set my lead @mention`")
    except SlackApiError as e:
        logger.warning(f"Could not DM new member {new_slack_id}: {e}")

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
# Hours report helpers
# --------------------------
def get_academic_year_bounds():
    today = datetime.now()
    if today.month >= 9:
        start = datetime(today.year, 9, 1)
        end   = datetime(today.year + 1, 8, 31, 23, 59, 59)
    else:
        start = datetime(today.year - 1, 9, 1)
        end   = datetime(today.year, 8, 31, 23, 59, 59)
    return start, end

def get_sessions_this_year(member_name, include_disapproved=False):
    start, end = get_academic_year_bounds()
    rows = read_attendance_rows()
    results = []
    for row in rows:
        if row["member_name"].strip().lower() != member_name.strip().lower():
            continue
        try:
            check_in_dt = row_to_dt(row, "check_in")
        except (ValueError, TypeError):
            continue
        if not (start <= check_in_dt <= end):
            continue
        approved_val   = str(row.get("approved", "")).strip().lower()
        is_disapproved = approved_val == "disapproved"
        if not include_disapproved and is_disapproved:
            continue
        results.append(row)
    return results

# --------------------------
# Semester helpers
# --------------------------
def get_current_semester():
    today = datetime.now().date()
    year  = today.year

    for sem_name, cfg in SEMESTERS.items():
        for rng in cfg["ranges"]:
            sm, sd, em, ed = rng
            start = datetime(year, sm, sd).date()
            end   = datetime(year, em, ed).date()

            if sem_name == "Winter" and sm == 12:
                dec_start = datetime(year - 1, 12, 21).date()
                dec_end   = datetime(year - 1, 12, 31).date()
                jan_start = datetime(year, 1, 1).date()
                jan_end   = datetime(year, 1, 26).date()
                if dec_start <= today <= dec_end or jan_start <= today <= jan_end:
                    return "Winter", dec_start, jan_end

            if start <= today <= end:
                return sem_name, start, end

    return None, None, None

def get_current_week_bounds():
    today = datetime.now().date()
    start = today - timedelta(days=today.weekday())
    end   = start + timedelta(days=6)
    return start, end

def format_hours_report(sessions, include_disapproved=False):
    lines          = []
    total_approved = 0.0
    total_pending  = 0.0

    for i, row in enumerate(sessions, start=1):
        approved = str(row.get("approved", "")).strip().lower()

        if approved in ("false", ""):
            status = "⏳ Pending"
            try:
                total_pending += float(row.get("hours", 0))
            except (ValueError, TypeError):
                pass
        elif approved == "true":
            status = "✅ Approved"
            try:
                total_approved += float(row.get("hours", 0))
            except (ValueError, TypeError):
                pass
        else:
            if not include_disapproved:
                continue
            status = "❌ Disapproved"

        try:
            ci_dt = row_to_dt(row, "check_in")
            ci = ci_dt.strftime("%b %d  %H:%M") if ci_dt else row.get("check_in_date", "?")
        except (ValueError, TypeError):
            ci = f"{row.get('check_in_date','?')}".strip()

        try:
            co_dt = row_to_dt(row, "check_out")
            co = co_dt.strftime("%H:%M") if co_dt else ("(open)" if not row.get("check_out_date","").strip() else row.get("check_out_date","?"))
        except (ValueError, TypeError):
            co = f"{row.get('check_out_date','?')}".strip()

        try:
            hrs = f"{float(row.get('hours', 0)):.2f}h"
        except (ValueError, TypeError):
            hrs = "?h"

        lines.append(f"{i}. {ci} – {co}  |  {hrs}  |  {status}")

    return "\n".join(lines), round(total_approved, 2), round(total_pending, 2)


def get_semester_sessions(member_name, start_date, end_date, include_disapproved=False):
    rows    = read_attendance_rows()
    results = []
    for row in rows:
        if row.get("member_name", "").strip().lower() != member_name.strip().lower():
            continue
        approved = str(row.get("approved", "")).strip().lower()
        if not include_disapproved and approved not in ("true", "false", ""):
            continue
        try:
            ci_dt   = row_to_dt(row, "check_in")
            ci_date = ci_dt.date() if ci_dt else None
            if ci_date is None:
                continue
        except (ValueError, TypeError):
            continue
        if start_date <= ci_date <= end_date:
            results.append(row)
    return results


# --------------------------
# Hours report handlers
# --------------------------
def handle_my_info(event, member, members):
    name      = member["member_name"]
    seniority = get_seniority(member)
    lead_id   = member.get("lead_slack_id", "").strip()

    if lead_id:
        lead     = members.get(lead_id)
        lead_str = lead["member_name"] if lead else f"Unknown ({lead_id})"
    else:
        lead_str = "Not set — use `set my lead @mention` to assign one"

    sem_name, start, end = get_current_semester()
    if sem_name:
        all_sessions = get_semester_sessions(name, start, end, include_disapproved=True)
        approved  = sum(1 for r in all_sessions if str(r.get("approved","")).lower() == "true")
        pending   = sum(1 for r in all_sessions if str(r.get("approved","")).lower() in ("false","","none"))
        total_hrs = sum(
            float(r.get("hours", 0))
            for r in all_sessions
            if str(r.get("approved","")).lower() == "true"
        )
        sem_label = f"{sem_name} {start.year}"
    else:
        approved = pending = 0
        total_hrs = 0.0
        sem_label = "unknown semester"

    reply(event, (
        f"*{name}*\n"
        f"Seniority: {seniority}\n"
        f"Lead: {lead_str}\n"
        f"\n"
        f"*{sem_label} summary:*\n"
        f"Approved sessions: {approved}\n"
        f"Pending sessions:  {pending}\n"
        f"Total approved hours: {round(total_hrs, 2)}h"
    ))


def handle_set_my_lead(event, slack_id, text, members):
    rest = text.strip()
    if not rest or rest.lower() == "set my lead":
        reply(event, "Usage: `set my lead @mention` or `set my lead none`")
        return

    arg = rest.removeprefix("set my lead").strip()
    me  = members.get(slack_id)
    if not me:
        return

    if arg.lower() == "none":
        me["lead_slack_id"] = ""
        write_members(members)
        reply(event, "Your lead has been cleared.")
        return

    lead, _ = extract_mention_and_rest(arg, members)
    if not lead:
        reply(event, f"Member not found: {arg!r}. Use a @mention or their full name.")
        return
    if lead["slack_id"] == slack_id:
        reply(event, "You can't set yourself as your own lead.")
        return

    me["lead_slack_id"] = lead["slack_id"]
    write_members(members)
    reply(event, f"Your lead has been set to {lead['member_name']}.")


def handle_feedback(event, slack_id, text, members):
    msg = text.removeprefix("feedback").strip()
    if not msg:
        reply(event, "Usage: `feedback <your message>`")
        return
    _post_direct(ADMIN_SLACK_ID, f"📬 *Anonymous feedback:*\n{msg}")
    reply(event, "Your feedback has been sent anonymously. Thank you.")
    logger.info("Anonymous feedback received (sender identity withheld)")


def handle_my_hours(event, member, weekly=False):
    """
    `my hours`        — current semester summary
    `my hours weekly` — this Mon–Sun week only

    FIXED: added `weekly` parameter (was missing, causing TypeError when dispatcher
    called handle_my_hours(event, member, weekly=True)).
    """
    name = member["member_name"]

    if weekly:
        start, end = get_current_week_bounds()
        label      = f"week of {start} – {end}"
        sessions   = get_semester_sessions(name, start, end, include_disapproved=False)
        if not sessions:
            reply(event, f"No sessions recorded for you this week ({start} – {end}).")
            return
        body, approved_hrs, pending_hrs = format_hours_report(sessions, include_disapproved=False)
        reply(event, (
            f"Your hours — {label}:\n\n"
            f"{body}\n\n"
            f"Approved: {approved_hrs}h  |  Pending approval: {pending_hrs}h"
        ))
        return

    sem_name, start, end = get_current_semester()
    if sem_name is None:
        reply(event, "Could not determine the current semester. Contact an admin.")
        return

    sessions = get_semester_sessions(name, start, end, include_disapproved=False)
    if not sessions:
        reply(event, f"No sessions recorded for you this {sem_name} semester ({start} – {end}).")
        return

    body, approved_hrs, pending_hrs = format_hours_report(sessions, include_disapproved=False)
    reply(event, (
        f"Your hours — {sem_name} {start.year} ({start} – {end}):\n\n"
        f"{body}\n\n"
        f"Approved: {approved_hrs}h  |  Pending approval: {pending_hrs}h"
    ))


def handle_hours_report(event, slack_id, text, members):
    """
    `hours report <member name or @mention>`
    Available to anyone more senior than the target, or their designated lead.

    FIXED: parameter renamed from text_lc to text (receives original-cased text).
    Now uses extract_mention_and_rest() to resolve the target before doing any
    authorization check, so `hours report @mention` correctly resolves the member
    instead of comparing the raw mention string against member names and failing.
    """
    raw = text.split(None, 2)[2].strip() if len(text.split(None, 2)) >= 3 else ""
    if not raw:
        reply(event, "Usage: `hours report <member name>`")
        return

    # FIXED: was text_lc.removeprefix("hours report ").strip() followed by a plain
    # name-only next() lookup — both failed for @mentions.
    target, _ = extract_mention_and_rest(raw, members)
    if not target:
        reply(event, f"Member not found: {raw!r}. Use a @mention or their full name.")
        return

    display_name = target["member_name"]

    if not is_authorized_approver(slack_id, display_name, members):
        reply(event, "You're not authorized to view hours for that member.")
        return

    sem_name, start, end = get_current_semester()
    if sem_name is None:
        reply(event, "Could not determine the current semester. Contact an admin.")
        return

    sessions = get_semester_sessions(display_name, start, end, include_disapproved=True)
    if not sessions:
        reply(event, f"No sessions found for {display_name} this {sem_name} semester ({start} – {end}).")
        return

    body, approved_hrs, pending_hrs = format_hours_report(sessions, include_disapproved=True)
    reply(event, (
        f"Hours report for {display_name} — {sem_name} {start.year} ({start} – {end}):\n\n"
        f"{body}\n\n"
        f"Approved: {approved_hrs}h  |  Pending: {pending_hrs}h"
    ))


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

    # Watchdog confirmation — member or senior replying "y"
    if text_lc == "y":
        name = member["member_name"]
        if name in SESSION_ALERTS and SESSION_ALERTS[name]["stage"] == "awaiting_member":
            ok = confirm_session(name, slack_id, members)
            if ok:
                reply(event, "Got it — session extended. We'll check in again closer to 8 hours.")
            return
        if slack_id in SENIOR_PENDING:
            target_name = SENIOR_PENDING[slack_id]
            ok = confirm_session(target_name, slack_id, members)
            if ok:
                reply(event, f"Confirmed — {target_name}'s session has been extended.")
                target_member = next(
                    (m for m in members.values() if m["member_name"].strip() == target_name),
                    None
                )
                if target_member:
                    post(target_member["slack_id"],
                         f"Your session was confirmed by a senior member. "
                         f"You will be checked out automatically at 8 hours if you don't respond to the next check.")
            return
        return  # spurious "y" — ignore silently

    if "check in" in text_lc:
        handle_check_in(event, member)
    elif "check out" in text_lc:
        handle_check_out(event, member)
    elif text_lc.startswith("admin "):
        if len(parts) >= 3 and parts[1] == "force" and parts[2] == "checkout":
            handle_admin_force_checkout(event, slack_id, parts, members)
        else:
            reply(event, "Unknown admin command. Available: `admin force checkout <name>`")
    elif text_lc.startswith("set my lead"):
        handle_set_my_lead(event, slack_id, text, members)
    elif text_lc.startswith("set seniority ") or text_lc.startswith("set lead "):
        # Pass original `text` so @mentions are preserved for parse_mention()
        handle_set_member_field(event, slack_id, text, members)
    elif text_lc.startswith("approve ") or text_lc.startswith("disapprove "):
        handle_approve_disapprove(event, slack_id, text, members)
    elif text_lc == "announcement formal":
        handle_announcement_formal(event, slack_id)
    elif text_lc == "announcement casual":
        handle_announcement_casual(event, slack_id)
    elif "is shop open" in text_lc or "is the shop open" in text_lc:
        handle_is_shop_open(event["channel"])
    elif text_lc == "my info":
        handle_my_info(event, member, members)
    elif text_lc == "my hours weekly":
        # FIXED: was calling handle_my_hours(event, member, weekly=True) but the old
        # function signature was handle_my_hours(event, member) — TypeError every time.
        handle_my_hours(event, member, weekly=True)
    elif text_lc == "my hours":
        handle_my_hours(event, member)
    elif text_lc.startswith("hours report "):
        # Pass original `text` so @mentions are preserved for parse_mention()
        handle_hours_report(event, slack_id, text, members)
    elif text_lc.startswith("feedback "):
        handle_feedback(event, slack_id, text, members)
    elif "who is in" in text_lc or "who's in" in text_lc:
        handle_who_is_in(event)
    elif text_lc.startswith("register "):
        handle_register(event, slack_id, text, members)
    else:
        reply(event, (
            "Available commands:\n"
            "\n"
            "*Attendance*\n"
            "- `check in` / `check out`\n"
            "\n"
            "*Shop status*\n"
            "- `who is in` / `is shop open`\n"
            "\n"
            "*Hours*\n"
            "- `my hours` — semester summary\n"
            "- `my hours weekly` — this week\n"
            "- `my info` — your profile, lead, and session counts\n"
            "\n"
            "*Approvals* (seniors/leads only)\n"
            "- `approve @mention` — approve all pending sessions\n"
            "- `disapprove @mention` — list pending sessions with IDs\n"
            "- `disapprove @mention <session_id>` — disapprove a specific session\n"
            "- `hours report @mention` — full semester report\n"
            "\n"
            "*Settings*\n"
            "- `set my lead @mention` / `set my lead none`\n"
            "\n"
            "*Admin / Seniority-1*\n"
            "- `admin force checkout @mention`\n"
            "- `set seniority @mention <1-5>`\n"
            "- `set lead @mention @lead` / `set lead @mention none`\n"
            "- `announcement formal` / `announcement casual`\n"
            "- `register @mention [Full Name]`, add a new member\n"
            "\n"
            "*Other*\n"
            "- `feedback <message>` — send anonymous feedback to admin"
        ))

# --------------------------
# Graceful shutdown
# --------------------------
def force_checkout_all(reason="shutdown"):
    if not CURRENT_MEMBERS:
        return
    members = load_members()
    checkout_time = datetime.now()
    for name in list(CURRENT_MEMBERS):
        member = next((m for m in members.values() if m["member_name"].strip() == name), None)
        card_uid = member["card_uid"] if member else "ABC123"
        hours, check_in_iso = close_open_session(card_uid, name, checkout_time)
        CURRENT_MEMBERS.discard(name)
        SESSION_ALERTS.pop(name, None)
        logger.info(f"Auto-checked out {name} on {reason} ({hours}h)")
        if member:
            try:
                post(member["slack_id"],
                     f"You were automatically checked out due to {reason}. "
                     f"Hours recorded: {hours}. Contact an admin if this is incorrect.")
            except Exception as e:
                logger.warning(f"Could not notify {name} on {reason}: {e}")
    try:
        post(ANNOUNCE_CHANNEL_ID, f"Shop closed — all members checked out due to {reason}.")
    except Exception as e:
        logger.warning(f"Could not post shutdown announcement: {e}")

def handle_shutdown(signum, frame):
    logger.info(f"Received signal {signum}. Shutting down gracefully...")
    force_checkout_all(reason="bot shutdown")
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

logger.info("Starting session watchdog...")
start_watchdog()

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
    force_checkout_all(reason="bot shutdown")