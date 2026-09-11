import os
import sys
import csv
import time
import random
import re
import signal
import shutil
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
import office_hours

# --------------------------
# Configuration
# --------------------------
_ASSET_DIR = sys._MEIPASS if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
_DATA_DIR  = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))

load_dotenv(os.path.join(_ASSET_DIR, ".env"))

SLACK_BOT_TOKEN     = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN     = os.getenv("SLACK_APP_TOKEN")
ANNOUNCE_CHANNEL_ID = "C06CT1ZTYUS"
ADMIN_SLACK_ID      = "U07U7V298Q2"   # bootstrap/root admin - always authorized, can't be removed
MEMBERS_FILE        = os.path.join(_DATA_DIR, "members.csv")
ATTENDANCE_FILE     = os.path.join(_DATA_DIR, "attendance.csv")
ATTENDANCE_HEADERS  = ["session_id", "member_name", "slack_id", "check_in_date", "check_in_time",
                        "check_out_date", "check_out_time", "hours", "approved"]

# --------------------------
# Admins (promoted, in addition to the root ADMIN_SLACK_ID above)
# --------------------------
ADMINS_FILE   = os.path.join(_DATA_DIR, "admins.csv")
ADMIN_HEADERS = ["slack_id", "member_name", "added_by", "added_at"]

# --------------------------
# Semester state (see get_current_semester / `admin semester restart`)
# --------------------------
SEMESTER_STATE_FILE    = os.path.join(_DATA_DIR, "semester_state.csv")
SEMESTER_STATE_HEADERS = ["semester_name", "start_date", "restarted_by", "restarted_at"]

# --------------------------
# Office hours (see office_hours.py)
# --------------------------
OFFICE_HOURS_FILE = os.path.join(_DATA_DIR, "office_hours.csv")
# Seniority is 1 (most senior/trusted) through 5 (newest, default for new registrants).
# "Seniority 3 or above" is interpreted here as rank <= 3 -- i.e. everyone except the
# two newest tiers (4, 5). If that's backwards from what you meant, flip this to >=.
OFFICE_HOURS_MAX_SENIORITY_LEVEL = 3

# Night-before heads-up for tomorrow's scheduled office hours (24h "HH:MM", local time)
OFFICE_HOURS_DAY_BEFORE_REMINDER_TIME = "20:00"
OFFICE_HOURS_DAY_BEFORE_REMINDED = set()   # {(slack_id, block_id, "YYYY-MM-DD")} -- date of the upcoming shift

# --------------------------
# Pending admin confirmations (shutdown / semester restart)
# --------------------------
PENDING_ADMIN_CONFIRM        = {}   # slack_id -> {"action", "payload", "expires_at"}
ADMIN_CONFIRM_TIMEOUT_MINUTES = 2

# --------------------------
# Rate limit for admin error notifications (avoid paging admins on every
# single bad message -- log everything, but only interrupt them occasionally)
# --------------------------
ERROR_NOTIFY_COOLDOWN_HOURS = 3
_last_error_notify_at = None

# --------------------------
# Daily data backups
# --------------------------
BACKUP_DIR            = os.path.join(_DATA_DIR, "backups")
BACKUP_RETENTION_DAYS = 30
_LAST_BACKUP_DATE      = None

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

CURRENT_MEMBERS = set()   # set of slack_ids (NOT names -- two members can share a name)
USE_FORMAL_MODE  = True

SESSION_ALERTS = {}   # slack_id -> {"stage", "alert_sent_at", "check_in_dt", "senior_slack_id"}
SENIOR_PENDING = {}   # senior's slack_id -> target member's slack_id

OFFICE_HOURS_REMINDED = set()   # {(slack_id, block_id, "YYYY-MM-DD")} -- de-dupes daily reminder pings

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

def _row_matches_member(row, slack_id, member_name):
    """
    True if this attendance row belongs to the given member. Prefers matching
    by slack_id (collision-proof); only falls back to name matching when either
    side lacks a slack_id (legacy rows written before this column existed, or a
    target that couldn't be resolved to a current member).
    """
    row_slack_id = row.get("slack_id", "").strip()
    if slack_id and row_slack_id:
        return row_slack_id == slack_id
    return row.get("member_name", "").strip().lower() == member_name.strip().lower()

def _next_session_id(rows):
    max_id = 0
    for r in rows:
        try:
            max_id = max(max_id, int(r.get("session_id", 0)))
        except (ValueError, TypeError):
            pass
    return max_id + 1

def append_session(name, slack_id, check_in_dt):
    ci_date, ci_time = dt_to_row(check_in_dt)
    rows = read_attendance_rows()
    rows.append({
        "session_id":     str(_next_session_id(rows)),
        "member_name":    name,
        "slack_id":       slack_id,
        "check_in_date":  ci_date,
        "check_in_time":  ci_time,
        "check_out_date": "",
        "check_out_time": "",
        "hours":          "0.0",
        "approved":       "False",
    })
    write_attendance_rows(rows)

def append_closed_session(name, slack_id, check_in_dt, check_out_dt, hours):
    """Adds an already-complete session -- used by `add session` to retroactively
    log a forgotten check-in/out."""
    ci_date, ci_time = dt_to_row(check_in_dt)
    co_date, co_time = dt_to_row(check_out_dt)
    rows = read_attendance_rows()
    row = {
        "session_id":     str(_next_session_id(rows)),
        "member_name":    name,
        "slack_id":       slack_id,
        "check_in_date":  ci_date,
        "check_in_time":  ci_time,
        "check_out_date": co_date,
        "check_out_time": co_time,
        "hours":          str(hours),
        "approved":       "False",
    }
    rows.append(row)
    write_attendance_rows(rows)
    return row

def get_open_session(slack_id, member_name):
    for row in reversed(read_attendance_rows()):
        if not row.get("check_out_date", "").strip() and _row_matches_member(row, slack_id, member_name):
            return row
    return None

def close_open_session(slack_id, member_name, checkout_dt):
    rows = read_attendance_rows()
    target = None
    for i in range(len(rows) - 1, -1, -1):
        if not rows[i].get("check_out_date", "").strip() and _row_matches_member(rows[i], slack_id, member_name):
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

def get_unapproved_sessions(slack_id, member_name):
    return [
        (i, row)
        for i, row in enumerate(read_attendance_rows())
        if _row_matches_member(row, slack_id, member_name)
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

def approve_all_sessions(slack_id, member_name):
    rows = read_attendance_rows()
    count = 0
    for row in rows:
        if _row_matches_member(row, slack_id, member_name):
            if str(row.get("approved", "")).lower() in ("false", "", "none"):
                row["approved"] = "True"
                count += 1
    if count:
        write_attendance_rows(rows)
    return count

# --------------------------
# Startup recovery
# --------------------------
def _resolve_row_slack_id(row, members):
    """Best-effort slack_id for an attendance row: the row's own slack_id column
    if present (new rows), else resolved by matching member_name against the
    current members list (legacy rows from before this column existed)."""
    sid = row.get("slack_id", "").strip()
    if sid:
        return sid
    name = row.get("member_name", "").strip().lower()
    if not name:
        return None
    for m in members.values():
        if m["member_name"].strip().lower() == name:
            return m["slack_id"]
    return None

def rebuild_current_members():
    rows = read_attendance_rows()
    members = load_members()
    now = datetime.now()
    stale = []
    recovered = []
    seen = set()

    for row in reversed(rows):
        if row.get("check_out_date", "").strip():
            continue
        name = row.get("member_name", "").strip()
        row_slack_id = _resolve_row_slack_id(row, members)
        dedup_key = row_slack_id or f"name:{name.lower()}"
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        try:
            check_in_dt = row_to_dt(row, "check_in")
            age_hours = (now - check_in_dt).total_seconds() / 3600
        except (ValueError, TypeError):
            age_hours = 0

        ci_label = f"{row.get('check_in_date','')} {row.get('check_in_time','')}".strip()

        if not row_slack_id:
            # Can't safely restore live presence state without knowing who this is
            # (e.g. the member was removed from members.csv while still checked in).
            stale.append((f"{name} (unresolved member - not in members.csv)", ci_label, round(age_hours, 1)))
            logger.warning(f"Open session for '{name}' has no resolvable member - NOT restoring, needs manual review.")
            continue

        if age_hours > STALE_SESSION_HOURS:
            stale.append((name, ci_label, round(age_hours, 1)))
            logger.warning(
                f"Stale open session found for {name} (checked in {ci_label}, "
                f"{round(age_hours, 1)}h ago) - NOT restoring to CURRENT_MEMBERS."
            )
        else:
            CURRENT_MEMBERS.add(row_slack_id)
            recovered.append(name)
            logger.info(f"Restored {name} to CURRENT_MEMBERS (session started {ci_label})")

    if recovered:
        logger.info(f"Recovered {len(recovered)} active session(s) after restart: {', '.join(recovered)}")

    if stale:
        stale_lines = "\n".join(
            f"- {name} (checked in {ci}, {age}h ago)" for name, ci, age in stale
        )
        msg = (
            f"Bot restarted and found {len(stale)} stale/unresolved open session(s):\n"
            f"{stale_lines}\n\n"
            f"To close a session manually, use: `admin force checkout <name>`"
        )
        notify_all_admins(msg)
        logger.warning(f"Notified admin(s) of {len(stale)} stale session(s).")

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

MEMBERS_HEADERS = ["card_uid", "member_name", "slack_id", "seniority", "lead_slack_id", "active"]

def write_members(members_dict):
    rows = list(members_dict.values())
    _atomic_write_csv(MEMBERS_FILE, MEMBERS_HEADERS, rows)

def is_active_member(member):
    # Backward compatible: rows written before the "active" column existed
    # (or blank values) are treated as active.
    return str(member.get("active", "True")).strip().lower() != "false"

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
                       f"'{member.get('seniority')}' - defaulting to 5")
        return 5

# --------------------------
# Admin management
# --------------------------
def ensure_admins_file():
    if not os.path.exists(ADMINS_FILE):
        _atomic_write_csv(ADMINS_FILE, ADMIN_HEADERS, [])

def load_admin_rows():
    ensure_admins_file()
    with open(ADMINS_FILE, "r", newline="") as f:
        return list(csv.DictReader(f))

def load_admin_ids():
    return {row["slack_id"] for row in load_admin_rows() if row.get("slack_id")}

def add_admin_record(slack_id, member_name, added_by):
    rows = load_admin_rows()
    if any(r["slack_id"] == slack_id for r in rows):
        return False
    rows.append({
        "slack_id":    slack_id,
        "member_name": member_name,
        "added_by":    added_by,
        "added_at":    datetime.now().isoformat(timespec="seconds"),
    })
    _atomic_write_csv(ADMINS_FILE, ADMIN_HEADERS, rows)
    return True

def remove_admin_record(slack_id):
    rows = load_admin_rows()
    kept = [r for r in rows if r["slack_id"] != slack_id]
    if len(kept) == len(rows):
        return False
    _atomic_write_csv(ADMINS_FILE, ADMIN_HEADERS, kept)
    return True

def is_admin_level(slack_id, members):
    """
    True for the root admin, any promoted admin, or any seniority-1 member.
    This is the single check used everywhere "admin-tier" access is required,
    so seniority-1 members and promoted admins get equivalent capabilities
    throughout the bot without each handler duplicating the logic.
    """
    if slack_id == ADMIN_SLACK_ID:
        return True
    if slack_id in load_admin_ids():
        return True
    member = members.get(slack_id)
    return bool(member and get_seniority(member) == 1)

def notify_all_admins(text, exclude_slack_id=None):
    targets = {ADMIN_SLACK_ID} | load_admin_ids()
    for sid in targets:
        if sid and sid != exclude_slack_id:
            post(sid, text)

def _notify_admins_of_error(text):
    """Every error is always logged to bot.log regardless -- this just throttles
    how often admins get pinged about it, so a burst of bad messages doesn't spam
    them."""
    global _last_error_notify_at
    now = datetime.now()
    if _last_error_notify_at and (now - _last_error_notify_at) < timedelta(hours=ERROR_NOTIFY_COOLDOWN_HOURS):
        return
    _last_error_notify_at = now
    notify_all_admins(text + f"\n(Further error notifications are suppressed for {ERROR_NOTIFY_COOLDOWN_HOURS}h; "
                              f"check bot.log for anything that happens in between.)")

# --------------------------
# Seniority-based notification helpers
# --------------------------
def find_most_senior_in_shop(members, exclude_slack_id=None):
    candidates = [
        m for m in members.values()
        if m["slack_id"] in CURRENT_MEMBERS
        and m["slack_id"] != exclude_slack_id
    ]
    if not candidates:
        return None
    best = min(candidates, key=lambda m: (get_seniority(m), m["member_name"]))
    return best["slack_id"]

def find_notify_target(check_in_iso, checkout_dt, checking_out_member, members):
    exclude_slack_id = checking_out_member["slack_id"]
    exclude_name = checking_out_member["member_name"]
    lead_id = checking_out_member.get("lead_slack_id", "").strip()

    try:
        session_start = datetime.fromisoformat(check_in_iso)
    except (ValueError, TypeError):
        logger.warning(f"Could not parse check_in '{check_in_iso}' for {exclude_name} - falling back to lead/admin.")
        return lead_id or ADMIN_SLACK_ID

    window_start = checkout_dt - timedelta(hours=24)
    co_present = []

    for row in read_attendance_rows():
        row_slack_id = _resolve_row_slack_id(row, members)
        if not row_slack_id or row_slack_id == exclude_slack_id:
            continue
        member_obj = members.get(row_slack_id)
        if not member_obj:
            continue
        try:
            row_checkin = row_to_dt(row, "check_in")
        except (ValueError, TypeError):
            continue
        if row_checkin is None or row_checkin < window_start:
            continue
        if row.get("check_out_date", "").strip():
            # FIXED: was parsing check_out_date alone (silently truncating to
            # midnight and losing the actual checkout time); use both date+time.
            row_checkout = row_to_dt(row, "check_out")
            if row_checkout is None:
                continue
            overlaps = row_checkin < checkout_dt and row_checkout > session_start
        else:
            overlaps = row_checkin < checkout_dt
        if overlaps:
            co_present.append(member_obj)

    if co_present:
        best = min(co_present, key=lambda m: (get_seniority(m), m["member_name"]))
        logger.info(f"Notifying most senior co-present member: {best['member_name']}")
        return best["slack_id"]

    if lead_id:
        logger.info(f"{exclude_name} was alone - notifying lead {lead_id}")
        return lead_id

    logger.warning(f"No lead set for {exclude_name} - falling back to admin")
    return ADMIN_SLACK_ID

# --------------------------
# Session watchdog
# --------------------------
def _auto_checkout_member(slack_id, members, reason="no_response"):
    """reason: "no_response" (didn't answer the inactivity check in time) or
    "max_hours" (hit the SESSION_AUTO_CHECKOUT_HOURS hard cap regardless of
    whether they'd confirmed they were still there)."""
    checkout_time = datetime.now()
    member = members.get(slack_id)
    name = member["member_name"] if member else slack_id

    hours, check_in_iso = close_open_session(slack_id, name, checkout_time)
    CURRENT_MEMBERS.discard(slack_id)
    SESSION_ALERTS.pop(slack_id, None)
    logger.info(f"Watchdog auto-checked out {name} ({reason}, {hours}h)")

    try:
        hrs = round((checkout_time - datetime.fromisoformat(check_in_iso)).total_seconds() / 3600, 2) \
              if check_in_iso else round(hours or 0, 2)
    except (ValueError, TypeError):
        hrs = round(hours or 0, 2)

    if member:
        if reason == "max_hours":
            post(member["slack_id"],
                 f"You've been automatically checked out after reaching the {SESSION_AUTO_CHECKOUT_HOURS}-hour mark "
                 f"- we don't expect anyone to work a single shift longer than that. "
                 f"Hours recorded: {hrs}. Please `check in` again if you're still working.")
        else:
            post(member["slack_id"],
                 f"You have been automatically checked out after no response. "
                 f"Hours recorded: {hrs}. If this is incorrect, contact an admin.")

    if member and check_in_iso:
        if CURRENT_MEMBERS:
            notify_id = find_most_senior_in_shop(members, exclude_slack_id=slack_id)
        else:
            notify_id = find_notify_target(check_in_iso, checkout_time, member, members)
        if notify_id:
            reason_text = f"reached the {SESSION_AUTO_CHECKOUT_HOURS}h cap" if reason == "max_hours" else "no response to inactivity check"
            post(notify_id,
                 f"{name} was auto-checked out ({reason_text}). "
                 f"Hours recorded: {hrs}\n"
                 f"- `approve pending {name}` to review")

    if len(CURRENT_MEMBERS) == 0:
        reason_text = "reaching the hour cap" if reason == "max_hours" else "inactivity"
        post(ANNOUNCE_CHANNEL_ID, f"Shop closed. {name} was auto-checked out after {reason_text}.")

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

def _maybe_backup_data():
    """Copies the CSV data files once per day, keeping BACKUP_RETENTION_DAYS worth."""
    global _LAST_BACKUP_DATE
    today = datetime.now().date()
    if _LAST_BACKUP_DATE == today:
        return
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = today.strftime("%Y-%m-%d")
        for src_name in ("attendance.csv", "members.csv", "admins.csv", "office_hours.csv", "semester_state.csv"):
            src = os.path.join(_DATA_DIR, src_name)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(BACKUP_DIR, f"{stamp}_{src_name}"))
        _prune_old_backups()
        _LAST_BACKUP_DATE = today
        logger.info(f"Daily backup completed for {stamp}.")
    except OSError as e:
        logger.warning(f"Daily backup failed: {e}")

def _prune_old_backups():
    cutoff = datetime.now() - timedelta(days=BACKUP_RETENTION_DAYS)
    try:
        for fname in os.listdir(BACKUP_DIR):
            fpath = os.path.join(BACKUP_DIR, fname)
            try:
                if datetime.fromtimestamp(os.path.getmtime(fpath)) < cutoff:
                    os.remove(fpath)
            except OSError:
                pass
    except OSError:
        pass

def _watchdog_tick():
    _maybe_backup_data()
    _check_office_hours_reminders()
    _check_office_hours_day_before_reminders()
    if not CURRENT_MEMBERS:
        return
    members = load_members()
    now = datetime.now()
    for slack_id in list(CURRENT_MEMBERS):
        member = members.get(slack_id)
        if not member:
            continue
        name = member["member_name"]

        open_row = get_open_session(slack_id, name)
        if not open_row:
            continue
        try:
            check_in_dt = row_to_dt(open_row, "check_in")
        except (ValueError, TypeError):
            continue
        if check_in_dt is None:
            continue

        elapsed_h = (now - check_in_dt).total_seconds() / 3600
        alert = SESSION_ALERTS.get(slack_id)

        # Hard cap: fires regardless of confirmation stage, even if they replied
        # "y" earlier. Nobody is expected to work a single shift longer than this.
        if elapsed_h >= SESSION_AUTO_CHECKOUT_HOURS:
            logger.info(f"Watchdog: {name} reached {SESSION_AUTO_CHECKOUT_HOURS}h hard limit - auto-checking out.")
            if alert and alert.get("senior_slack_id"):
                SENIOR_PENDING.pop(alert["senior_slack_id"], None)
            _auto_checkout_member(slack_id, members, reason="max_hours")
            continue

        if alert is None and elapsed_h >= SESSION_CHECK_HOURS:
            logger.info(f"Watchdog: {name} has been in {elapsed_h:.1f}h - sending check-in ping.")
            SESSION_ALERTS[slack_id] = {
                "stage":           "awaiting_member",
                "alert_sent_at":   now,
                "check_in_dt":     check_in_dt,
                "senior_slack_id": None,
            }
            _send_member_alert(name, slack_id, elapsed_h)
            continue

        if alert is None:
            continue

        # Once confirmed, we don't ping again -- they just ride out to the hard
        # cap above. (Previously there was a second re-ping near the 7.5h mark;
        # removed per spec: confirm once, then it's silent until the 8h cutoff.)
        if alert["stage"] == "confirmed":
            continue

        alert_age_min = (now - alert["alert_sent_at"]).total_seconds() / 60

        if alert["stage"] == "awaiting_member" and alert_age_min >= SESSION_RESPONSE_MINUTES:
            senior_slack_id = find_most_senior_in_shop(members, exclude_slack_id=slack_id)
            if senior_slack_id:
                logger.info(f"Watchdog: {name} did not respond - escalating to senior {senior_slack_id}.")
                alert["stage"]           = "awaiting_senior"
                alert["alert_sent_at"]   = now
                alert["senior_slack_id"] = senior_slack_id
                SENIOR_PENDING[senior_slack_id] = slack_id
                _send_senior_alert(senior_slack_id, name, elapsed_h)
            else:
                logger.info(f"Watchdog: {name} did not respond and is alone - auto-checking out.")
                _auto_checkout_member(slack_id, members, reason="no_response")
            continue

        if alert["stage"] == "awaiting_senior" and alert_age_min >= SESSION_RESPONSE_MINUTES:
            logger.info(f"Watchdog: Senior did not respond for {name} - auto-checking out.")
            if alert.get("senior_slack_id"):
                SENIOR_PENDING.pop(alert["senior_slack_id"], None)
            _auto_checkout_member(slack_id, members, reason="no_response")
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

def confirm_session(slack_id, confirmed_by_slack_id, members):
    """
    Resolves a pending inactivity alert for slack_id, whatever stage it's in
    (awaiting_member OR awaiting_senior) -- a member's own confirmation should
    always count, even if it arrives after the senior escalation already fired.
    Returns False if there's nothing pending, or it's already past the 8h cap.
    """
    alert = SESSION_ALERTS.get(slack_id)
    if not alert:
        return False
    elapsed_h = (datetime.now() - alert["check_in_dt"]).total_seconds() / 3600
    if elapsed_h >= SESSION_AUTO_CHECKOUT_HOURS:
        return False
    if alert.get("senior_slack_id"):
        SENIOR_PENDING.pop(alert["senior_slack_id"], None)
    logger.info(f"Session confirmed for {slack_id} by {confirmed_by_slack_id}.")
    SESSION_ALERTS[slack_id] = {
        "stage":           "confirmed",
        "alert_sent_at":   datetime.now(),
        "check_in_dt":     alert["check_in_dt"],
        "senior_slack_id": None,
    }
    return True

# --------------------------
# Office hours reminders
# --------------------------
def _check_office_hours_reminders():
    """Pings anyone currently inside their scheduled office-hours window who
    hasn't checked in yet. De-duped per (person, block, day) so it fires once,
    not every watchdog tick."""
    global OFFICE_HOURS_REMINDED
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    OFFICE_HOURS_REMINDED = {k for k in OFFICE_HOURS_REMINDED if k[2] == date_str}

    now_hhmm = now.strftime("%H:%M")
    blocks = office_hours.list_for_day(OFFICE_HOURS_FILE, now.weekday())
    for b in blocks:
        if not (b["start_time"] <= now_hhmm <= b["end_time"]):
            continue
        if b["slack_id"] in CURRENT_MEMBERS:
            continue
        key = (b["slack_id"], b["id"], date_str)
        if key in OFFICE_HOURS_REMINDED:
            continue
        OFFICE_HOURS_REMINDED.add(key)
        post(b["slack_id"],
             f"You're scheduled for office hours right now "
             f"({office_hours.format_time_12h(b['start_time'])}-{office_hours.format_time_12h(b['end_time'])}) "
             f"but haven't checked in yet. Send `check in` when you arrive.")

def _check_office_hours_day_before_reminders():
    """Sends a one-time heads-up the evening before someone's scheduled office
    hours (default 8 PM local time, see OFFICE_HOURS_DAY_BEFORE_REMINDER_TIME).
    De-duped per (person, block, upcoming date)."""
    global OFFICE_HOURS_DAY_BEFORE_REMINDED
    now = datetime.now()
    if now.strftime("%H:%M") < OFFICE_HOURS_DAY_BEFORE_REMINDER_TIME:
        return

    tomorrow = now.date() + timedelta(days=1)
    tomorrow_str = tomorrow.strftime("%Y-%m-%d")
    OFFICE_HOURS_DAY_BEFORE_REMINDED = {k for k in OFFICE_HOURS_DAY_BEFORE_REMINDED if k[2] == tomorrow_str}

    blocks = office_hours.list_for_day(OFFICE_HOURS_FILE, tomorrow.weekday())
    for b in blocks:
        key = (b["slack_id"], b["id"], tomorrow_str)
        if key in OFFICE_HOURS_DAY_BEFORE_REMINDED:
            continue
        OFFICE_HOURS_DAY_BEFORE_REMINDED.add(key)
        post(b["slack_id"],
             f"Reminder: you're scheduled for office hours tomorrow "
             f"({office_hours.DAY_NAMES[tomorrow.weekday()]}, "
             f"{office_hours.format_time_12h(b['start_time'])}-{office_hours.format_time_12h(b['end_time'])}).")

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

def is_office_hours_eligible(member):
    return get_seniority(member) <= OFFICE_HOURS_MAX_SENIORITY_LEVEL

def handle_office_hours(event, slack_id, text, members):
    """
    `office hours set <day> <start>-<end>`   e.g. `office hours set Tue 3pm-5pm`
    `office hours cancel <id>` / `office hours cancel all`
    `office hours cancel @mention <id>`      (admin-level only, cancels for someone else)
    `office hours list [@mention]`
    `office hours today`
    """
    text_lc = text.lower()
    prefix = "office hours"
    rest = text[len(prefix):].strip() if text_lc.strip().startswith(prefix) else ""
    sub_parts = rest.split(None, 1)
    subcmd = sub_parts[0].lower() if sub_parts else ""
    arg = sub_parts[1].strip() if len(sub_parts) > 1 else ""

    if subcmd == "today":
        lines = _office_hours_today_lines()
        reply(event, "Scheduled office hours today:\n" + "\n".join(lines) if lines
              else "No office hours scheduled today.")
        return

    if subcmd == "list":
        if arg:
            target, _ = extract_mention_and_rest(arg, members)
            if not target:
                reply(event, f"Member not found: {arg!r}.")
                return
        else:
            target = members.get(slack_id)
            if not target:
                reply(event, "You are not registered in members.csv.")
                return
        blocks = office_hours.list_for_member(OFFICE_HOURS_FILE, target["slack_id"])
        if not blocks:
            reply(event, f"{target['member_name']} has no office hours scheduled.")
            return
        lines = [f"Office hours for *{target['member_name']}*:"]
        for b in blocks:
            day_name = office_hours.DAY_NAMES[int(b["day"])]
            lines.append(f"#{b['id']} - {day_name} "
                         f"{office_hours.format_time_12h(b['start_time'])}-{office_hours.format_time_12h(b['end_time'])}")
        reply(event, "\n".join(lines))
        return

    # Everything below mutates a schedule -- requires eligibility (or admin-level).
    member = members.get(slack_id)
    if not member:
        reply(event, "You are not registered in members.csv.")
        return
    if not (is_office_hours_eligible(member) or is_admin_level(slack_id, members)):
        reply(event, f"You're not authorized to schedule office hours. "
                     f"(Requires seniority {OFFICE_HOURS_MAX_SENIORITY_LEVEL} or higher.)")
        return

    if subcmd == "set":
        set_parts = arg.split(None, 1)
        if len(set_parts) < 2:
            reply(event, "Usage: `office hours set <day> <start>-<end>`\nExample: `office hours set Tue 3pm-5pm`")
            return
        day = office_hours.parse_day(set_parts[0])
        if day is None:
            reply(event, f"Couldn't understand day '{set_parts[0]}'. Use Mon/Tue/Wed/Thu/Fri/Sat/Sun (or full names).")
            return
        time_range = office_hours.parse_time_range(set_parts[1])
        if time_range is None:
            reply(event, "Couldn't understand that time range. Use e.g. `3pm-5pm`, `3:00pm-5:30pm`, "
                         "or 24h `15:00-17:30`. (Bare hours like `3-5` are ambiguous - include am/pm.)")
            return
        start, end = time_range
        new_row, conflict = office_hours.add_block(OFFICE_HOURS_FILE, member["member_name"], slack_id, day, start, end)
        day_name = office_hours.DAY_NAMES[day]
        if conflict:
            reply(event, f"That overlaps your existing {day_name} slot "
                         f"{office_hours.format_time_12h(conflict['start_time'])}-{office_hours.format_time_12h(conflict['end_time'])} "
                         f"(#{conflict['id']}). Cancel it first if you want to replace it.")
            return
        reply(event, f"✅ Office hours set: every *{day_name}*, "
                      f"{office_hours.format_time_12h(start)}-{office_hours.format_time_12h(end)} (#{new_row['id']}).")
        logger.info(f"{member['member_name']} set office hours: {day_name} {start}-{end}")
        return

    if subcmd == "cancel":
        cancel_tokens = arg.split()
        target_slack_id = slack_id
        if cancel_tokens and parse_mention(cancel_tokens[0]) and is_admin_level(slack_id, members):
            target_slack_id = parse_mention(cancel_tokens[0])
            arg = " ".join(cancel_tokens[1:]).strip()

        if not arg:
            reply(event, "Usage: `office hours cancel <id>` or `office hours cancel all`\n"
                         "(See your IDs with `office hours list`.)")
            return
        if arg.lower() == "all":
            count = office_hours.clear_for_member(OFFICE_HOURS_FILE, target_slack_id)
            reply(event, f"Cleared {count} office hours slot(s)." if count else "No office hours scheduled.")
            return
        if not arg.isdigit():
            reply(event, "Usage: `office hours cancel <id>` - see IDs with `office hours list`.")
            return
        removed = office_hours.remove_block(OFFICE_HOURS_FILE, int(arg), slack_id=target_slack_id)
        if not removed:
            reply(event, f"No office hours slot #{arg} found.")
            return
        day_name = office_hours.DAY_NAMES[int(removed["day"])]
        reply(event, f"Cancelled #{arg} ({day_name} "
                     f"{office_hours.format_time_12h(removed['start_time'])}-{office_hours.format_time_12h(removed['end_time'])}).")
        return

    reply(event, (
        "Office hours commands:\n"
        "- `office hours set <day> <start>-<end>` - e.g. `office hours set Tue 3pm-5pm`\n"
        "- `office hours cancel <id>` / `office hours cancel all`\n"
        "- `office hours list` / `office hours list @mention`\n"
        "- `office hours today` - who's scheduled today"
    ))


# --------------------------
# Command handlers
# --------------------------
def handle_check_in(event, member):
    name = member["member_name"]
    slack_id = member["slack_id"]

    existing = get_open_session(slack_id, name)
    if existing or slack_id in CURRENT_MEMBERS:
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
        append_session(name, slack_id, check_in_time)
        CURRENT_MEMBERS.add(slack_id)
        SESSION_ALERTS.pop(slack_id, None)
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
    slack_id      = member["slack_id"]
    checkout_time = datetime.now()
    members       = load_members()

    hours, check_in_iso = close_open_session(slack_id, name, checkout_time)

    if hours is None:
        if slack_id in CURRENT_MEMBERS:
            CURRENT_MEMBERS.discard(slack_id)
            logger.warning(f"{name} was in CURRENT_MEMBERS but had no open CSV session - cleared.")
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

    CURRENT_MEMBERS.discard(slack_id)
    SESSION_ALERTS.pop(slack_id, None)

    seniority = get_seniority(member)
    if seniority <= 2:
        count = approve_all_sessions(slack_id, name)
        reply(event, f"Checked out at {checkout_time.strftime('%H:%M:%S')}. "
                     f"Hours auto-approved ({hrs}h) - eboard member.")
        logger.info(f"Auto-approved {count} session(s) for eboard member {name}")
    else:
        reply(event, f"Checked out at {checkout_time.strftime('%H:%M:%S')}.")

        if CURRENT_MEMBERS:
            notify_id = find_most_senior_in_shop(members, exclude_slack_id=slack_id)
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

    if not is_admin_level(slack_id, members):
        reply(event, "You're not authorized. Only seniority-1 members or an admin can force check out.")
        return

    if len(parts) < 4:
        reply(event, "Usage: `admin force checkout <member name>`")
        return

    raw_target = " ".join(parts[3:]).strip()
    checkout_time = datetime.now()

    # Try to resolve as a @mention or a registered member name first -- this is
    # what lets us clean up in-memory state (CURRENT_MEMBERS etc) precisely by
    # slack_id afterward, with no risk of matching the wrong same-named person.
    resolved = resolve_member(raw_target, members)
    target_name = resolved["member_name"] if resolved else raw_target
    target_slack_id = resolved["slack_id"] if resolved else ""

    hours, check_in_iso = close_open_session(target_slack_id, target_name, checkout_time)
    if hours is None:
        reply(event, f"No open session found for '{raw_target}'.")
        return

    if resolved:
        CURRENT_MEMBERS.discard(target_slack_id)
        SESSION_ALERTS.pop(target_slack_id, None)
        if target_slack_id in SENIOR_PENDING.values():
            SENIOR_PENDING = {k: v for k, v in SENIOR_PENDING.items() if v != target_slack_id}
    else:
        # Rare: an open session exists under this name but it's not a currently
        # registered member (e.g. removed from members.csv without being
        # checked out first). The CSV row is still closed above, but we can't
        # safely touch live presence state without a slack_id to key it by.
        logger.warning(f"Force-closed a session for '{target_name}', who isn't a currently registered "
                        f"member - couldn't clean up in-memory shop-presence state for them.")

    logger.info(f"Admin force-closed session for {target_name} ({hours}h)")
    reply(event, f"Force closed session for {target_name}. Hours recorded: {hours}")

    if len(CURRENT_MEMBERS) == 0:
        post(ANNOUNCE_CHANNEL_ID, f"Shop closed. Last person out: {target_name} (force checkout)")


def handle_approve_disapprove(event, slack_id, text, members):
    """
    approve @mention / approve <name>           - approve ALL pending sessions
    disapprove @mention / disapprove <name>     - list pending sessions with session IDs
    disapprove @mention <id>                    - disapprove a specific session by global ID
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
        reply(event, f"You can't {cmd} your own sessions.")
        return
    if not is_self and not (is_admin_level(slack_id, members) or is_authorized_approver(slack_id, target_name, members)):
        reply(event, "You're not authorized to approve/disapprove sessions for that member.")
        return

    if cmd == "approve":
        count = approve_all_sessions(target_slack_id, target_name)
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
        if not _row_matches_member(row, target_slack_id, target_name):
            reply(event, f"Session #{trailing_id} does not belong to {target_name}.")
            return
        ok = delete_session(target_idx)
        reply(event, f"Disapproved session #{trailing_id} for {target_name}." if ok else "Failed.")
        return

    if cmd == "disapprove":
        pending = get_unapproved_sessions(target_slack_id, target_name)
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


# --------------------------
# Seniority capability descriptions (used for registration / promotion notices)
# --------------------------
# Seniority 1 = most senior/trusted, 5 = newest (default at registration). Each
# tier lists what's newly unlocked AT that level; a member's full capability
# list is the union of their own tier and everything below it (5 down to them).
SENIORITY_CAPABILITIES = {
    5: [
        "Check in / check out of the shop",
        "View your hours (`my hours`, `my hours weekly`, `my info`)",
        "Set your own lead (`set my lead @mention`)",
        "Check who's in and whether the shop is open (`who is in`, `is shop open`)",
        "View office hours schedules (`office hours list`, `office hours today`)",
        "Check the hours leaderboard (`top hours`)",
        "Send anonymous feedback to the admins (`feedback <message>`)",
    ],
    4: [
        "Approve or disapprove hours for members less senior than you "
        "(`approve @mention`, `disapprove @mention`)",
    ],
    3: [
        "Schedule and cancel your own recurring office hours "
        "(`office hours set`, `office hours cancel`)",
    ],
    2: [
        "Your own hours are auto-approved when you check out",
        "Approve/disapprove your own pending sessions",
        "Register new members (`register @mention`)",
        "Add a forgotten session for someone retroactively (`add session`)",
    ],
    1: [
        "Force-checkout anyone (`admin force checkout @mention`)",
        "Change any member's seniority or lead (`set seniority`, `set lead`)",
        "Switch shop-open announcement mode (`announcement formal` / `announcement casual`)",
        "Deactivate/reactivate members (`admin remove member`, `admin restore member`)",
        "View recent bot logs (`admin log`)",
        "Shut down the bot (`admin shutdown`)",
        "Start a new semester, optionally backdated (`admin semester restart`)",
        "Promote/demote other admins (`admin add admin`, `admin remove admin`)",
    ],
}

def _cumulative_capabilities(level):
    caps = []
    for lvl in range(5, level - 1, -1):
        caps.extend(SENIORITY_CAPABILITIES.get(lvl, []))
    return caps

def _format_capabilities_message(level, header=None):
    lines = []
    if header:
        lines.append(header)
    lines.append(f"What you can do at seniority {level}:")
    for cap in _cumulative_capabilities(level):
        lines.append(f"- {cap}")
    lines.append("\nType `help` any time to see the full command list.")
    return "\n".join(lines)


def handle_add_session(event, slack_id, text, members):
    """
    `add session <name or @mention> <date> <start>-<end>`
    date: `today`, `yesterday`, `YYYY-MM-DD`, or `M/D` (assumes this year, or
    last year if that would otherwise land in the future -- always in the past).
    Available to seniority-2 and above, or admin-level -- lets a trusted member
    log a session retroactively for someone who forgot to check in/out.
    """
    approver = members.get(slack_id)
    if not (is_admin_level(slack_id, members) or (approver and get_seniority(approver) <= 2)):
        reply(event, "You're not authorized. Only seniority-1/2 members or an admin can add sessions retroactively.")
        return

    text_lc = text.lower()
    prefix = "add session"
    rest = text[len(prefix):].strip() if text_lc.strip().startswith(prefix) else ""
    if not rest:
        reply(event, "Usage: `add session <name or @mention> <date> <start>-<end>`\n"
                     "Example: `add session @jake yesterday 4pm-5pm`\n"
                     "Date can be `today`, `yesterday`, `YYYY-MM-DD`, or `M/D`.")
        return

    target, remainder = extract_mention_and_rest(rest, members)
    if not target:
        reply(event, f"Member not found in {rest!r}. Start with a @mention or their full name.")
        return
    if not remainder:
        reply(event, "Usage: `add session <name or @mention> <date> <start>-<end>`")
        return

    date_time_parts = remainder.split(None, 1)
    if len(date_time_parts) < 2:
        reply(event, "Usage: `add session <name or @mention> <date> <start>-<end>`\n"
                     "Example: `add session @jake yesterday 4pm-5pm`")
        return
    date_token, time_token = date_time_parts

    session_date = _parse_flexible_date(date_token)
    if session_date is None:
        reply(event, f"Couldn't understand date '{date_token}'. Use `today`, `yesterday`, `YYYY-MM-DD`, or `M/D`.")
        return
    if session_date > datetime.now().date():
        reply(event, "Can't add a session in the future.")
        return

    time_range = office_hours.parse_time_range(time_token)
    if time_range is None:
        reply(event, "Couldn't understand that time range. Use e.g. `4pm-5pm`, `4:00pm-5:30pm`, or 24h "
                     "`16:00-17:30`. (Bare hours like `4-5` are ambiguous - include am/pm.)")
        return
    start_hhmm, end_hhmm = time_range
    check_in_dt = datetime.combine(session_date, datetime.strptime(start_hhmm, "%H:%M").time())
    check_out_dt = datetime.combine(session_date, datetime.strptime(end_hhmm, "%H:%M").time())
    hours = round((check_out_dt - check_in_dt).total_seconds() / 3600, 2)

    dup = any(
        _row_matches_member(r, target["slack_id"], target["member_name"]) and row_to_dt(r, "check_in") == check_in_dt
        for r in read_attendance_rows()
    )
    if dup:
        reply(event, f"{target['member_name']} already has a session starting at that exact time. "
                     f"Skipping to avoid a duplicate.")
        return

    who = approver["member_name"] if approver else slack_id
    new_row = append_closed_session(target["member_name"], target["slack_id"], check_in_dt, check_out_dt, hours)
    logger.info(f"{who} retroactively added session #{new_row['session_id']} for {target['member_name']}: "
                f"{check_in_dt} - {check_out_dt} ({hours}h)")

    note = ""
    if target["slack_id"] in CURRENT_MEMBERS:
        note = f"\n(Note: {target['member_name']} currently has a separate open session right now.)"

    reply(event, f"✅ Added a {hours}h session for {target['member_name']} on "
                 f"{session_date.strftime('%b %d, %Y')} "
                 f"({office_hours.format_time_12h(start_hhmm)}-{office_hours.format_time_12h(end_hhmm)}), "
                 f"#{new_row['session_id']}. Pending approval - `approve {target['member_name']}` to approve it.{note}")
    try:
        post(target["slack_id"],
             f"{who} added a {hours}h session for you on {session_date.strftime('%b %d, %Y')} "
             f"({office_hours.format_time_12h(start_hhmm)}-{office_hours.format_time_12h(end_hhmm)}), pending approval.")
    except SlackApiError as e:
        logger.warning(f"Could not DM {target['member_name']} about retroactive session: {e}")


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
    approver = members.get(slack_id)

    if not is_admin_level(slack_id, members):
        reply(event, "You're not authorized. Only seniority-1 members or an admin can use this command.")
        return

    who = approver["member_name"] if approver else slack_id

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

        old_level = get_seniority(target)
        target["seniority"] = str(new_seniority)
        write_members(members)
        logger.info(f"{who} set seniority for {target['member_name']}: {old_level} -> {new_seniority}")
        reply(event, f"Updated seniority for {target['member_name']}: {old_level} -> {new_seniority}")

        if new_seniority < old_level:
            try:
                post(target["slack_id"], _format_capabilities_message(
                    new_seniority, header=f"Congratulations - you've been granted seniority {new_seniority}!"
                ))
            except SlackApiError as e:
                logger.warning(f"Could not DM promotion notice to {target['member_name']}: {e}")
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
            logger.info(f"{who} cleared lead for {target['member_name']}")
            reply(event, f"Cleared lead for {target['member_name']}.")
            return

        # FIXED: was a split-and-lowercase-compare loop that failed for @mentions on
        # both the target and lead sides. Now uses extract_mention_and_rest() for the
        # target first, then for the lead from the remainder - handles all combinations
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
        logger.info(f"{who} set lead for {target['member_name']} -> {lead['member_name']}")
        reply(event, f"Set lead for {target['member_name']} -> {lead['member_name']}.")
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
    approver = members.get(slack_id)
    is_authorized = is_admin_level(slack_id, members) or (approver and get_seniority(approver) <= 2)
    if not is_authorized:
        reply(event, "You're not authorized. Only seniority-1/2 members or an admin can register new members.")
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
        "active":        "True",
    }
    members[new_slack_id] = new_member
    write_members(members)

    logger.info(f"Admin registered new member: {display_name} ({new_slack_id}), card_uid={card_uid}")
    reply(event, (
        f"✅ Registered *{display_name}* (<@{new_slack_id}>)\n"
        f"- Seniority: 5 (lowest by default)\n"
        # f"- Card UID: `{card_uid}` (placeholder, update if they have a physical card)\n"
        f"- Lead: not set\n\n"
        f"To update: `set seniority @mention <1-5>` / `set lead @mention @lead`"
    ))
    # Notify the new member
    try:
        post(new_slack_id,
             f"You've been registered in the shop attendance system by an admin. "
             f"You can now use `check in` / `check out` here in DMs.\n"
             f"To set your lead: `set my lead @mention`\n\n"
             + _format_capabilities_message(5))
    except SlackApiError as e:
        logger.warning(f"Could not DM new member {new_slack_id}: {e}")

def handle_announcement_formal(event, slack_id, members):
    global USE_FORMAL_MODE
    if not is_admin_level(slack_id, members):
        reply(event, "You're not authorized to use this command.")
        return
    USE_FORMAL_MODE = True
    logger.info("Formal announcement mode enabled")
    reply(event, f"Formal mode enabled. All future shop-open announcements will use:\n\"{FORMAL_OPEN_MESSAGE}\"")


def handle_announcement_casual(event, slack_id, members):
    global USE_FORMAL_MODE
    if not is_admin_level(slack_id, members):
        reply(event, "You're not authorized to use this command.")
        return
    USE_FORMAL_MODE = False
    logger.info("Casual announcement mode restored")
    reply(event, "Casual mode restored. Shop-open announcements will use random messages again.")


# --------------------------
# Pending admin confirmations (shutdown / semester restart)
# --------------------------
def _set_pending_confirm(slack_id, action, payload=None):
    PENDING_ADMIN_CONFIRM[slack_id] = {
        "action":     action,
        "payload":    payload or {},
        "expires_at": datetime.now() + timedelta(minutes=ADMIN_CONFIRM_TIMEOUT_MINUTES),
    }

def _pop_valid_pending(slack_id, action):
    entry = PENDING_ADMIN_CONFIRM.get(slack_id)
    if not entry or entry["action"] != action:
        return None
    PENDING_ADMIN_CONFIRM.pop(slack_id, None)
    if datetime.now() > entry["expires_at"]:
        return None
    return entry["payload"]


def handle_admin_shutdown_request(event, slack_id, members):
    if not is_admin_level(slack_id, members):
        reply(event, "You're not authorized to shut down the bot.")
        return
    n = len(CURRENT_MEMBERS)
    _set_pending_confirm(slack_id, "shutdown")
    reply(event, (
        f"This will check out all {n} currently active member(s) and shut the bot down completely. "
        f"It will need to be started manually afterward - it will *not* restart on its own.\n\n"
        f"Type `confirm shutdown` within {ADMIN_CONFIRM_TIMEOUT_MINUTES} minutes to proceed, or `cancel` to abort."
    ))


def handle_confirm_shutdown(event, slack_id, members):
    if not is_admin_level(slack_id, members):
        return
    if _pop_valid_pending(slack_id, "shutdown") is None:
        reply(event, "No shutdown request is pending (or it expired). Run `admin shutdown` first.")
        return
    member = members.get(slack_id)
    who = member["member_name"] if member else slack_id
    logger.warning(f"Admin shutdown confirmed by {who} ({slack_id}).")
    reply(event, "Shutting down now - checking everyone out and saving state.")
    notify_all_admins(f"Bot is being shut down by {who}.", exclude_slack_id=slack_id)
    # Reuse the exact same tested graceful-shutdown path used for SIGTERM/SIGINT,
    # rather than duplicating the checkout/save logic here.
    os.kill(os.getpid(), signal.SIGTERM)


def _parse_flexible_date(token, allow_future=False):
    """
    Accepts 'today', 'yesterday', 'tomorrow', 'YYYY-MM-DD', or 'M/D'['/YYYY']
    (American month/day, matching how the rest of the bot displays dates).
    Returns a date object, or None if unparseable.

    If no year is given and allow_future is False, a date that would land in
    the future is assumed to mean last year instead (since this path is only
    used for entering things that already happened). Semester restarts pass
    allow_future=True since planning a future start date is a legitimate use.
    """
    token = token.strip().lower()
    today = datetime.now().date()
    if token == "today":
        return today
    if token == "yesterday":
        return today - timedelta(days=1)
    if token == "tomorrow":
        return today + timedelta(days=1)

    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", token)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        except ValueError:
            return None

    m = re.match(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?$", token)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = today.year
        explicit_year = bool(m.group(3))
        if explicit_year:
            year = int(m.group(3))
            if year < 100:
                year += 2000
        try:
            d = datetime(year, month, day).date()
        except ValueError:
            return None
        if not explicit_year and not allow_future and d > today:
            d = d.replace(year=year - 1)
        return d

    return None


def handle_admin_semester_restart_request(event, slack_id, text, members):
    """
    `admin semester restart [name]` - starts today.
    `admin semester restart from <date> [name]` - starts from a given date,
    past or future (`today`, `yesterday`, `YYYY-MM-DD`, `M/D[/YYYY]`).
    """
    if not is_admin_level(slack_id, members):
        reply(event, "You're not authorized to restart the semester.")
        return
    text_lc = text.lower()
    prefix = "admin semester restart"
    rest = text[len(prefix):].strip() if text_lc.strip().startswith(prefix) else ""

    start_date = datetime.now().date()
    label = ""
    if rest.lower().startswith("from "):
        from_tokens = rest[len("from "):].strip().split(None, 1)
        if not from_tokens:
            reply(event, "Usage: `admin semester restart from <date> [name]` - "
                         "date can be `today`, `yesterday`, `YYYY-MM-DD`, `M/D`, past or future.")
            return
        parsed = _parse_flexible_date(from_tokens[0], allow_future=True)
        if parsed is None:
            reply(event, f"Couldn't understand date '{from_tokens[0]}'. Use `today`, `yesterday`, `YYYY-MM-DD`, or `M/D`.")
            return
        start_date = parsed
        label = from_tokens[1].strip() if len(from_tokens) > 1 else ""
    else:
        label = rest

    if not label:
        label = _guess_semester_label(datetime.combine(start_date, datetime.min.time()))

    _set_pending_confirm(slack_id, "semester_restart", {"label": label, "start_date": start_date.isoformat()})
    when = "today" if start_date == datetime.now().date() else start_date.strftime("%b %d, %Y")
    reply(event, (
        f"This will start a new reporting period: *{label}*, beginning {when}.\n"
        f"Past attendance history is kept forever - `my hours` / `hours report` will only show "
        f"sessions from {when} onward until the next restart.\n\n"
        f"Type `confirm semester restart` within {ADMIN_CONFIRM_TIMEOUT_MINUTES} minutes to proceed, "
        f"or `cancel` to abort.\n"
        f"(Want a different name or date? Run `admin semester restart` again, e.g. "
        f"`admin semester restart from 2026-08-25 Fall 2026`, then confirm.)"
    ))


def handle_confirm_semester_restart(event, slack_id, members):
    if not is_admin_level(slack_id, members):
        return
    payload = _pop_valid_pending(slack_id, "semester_restart")
    if payload is None:
        reply(event, "No semester restart is pending (or it expired). Run `admin semester restart` first.")
        return
    label = payload["label"]
    try:
        start_date = datetime.fromisoformat(payload["start_date"]).date()
    except (ValueError, TypeError):
        start_date = datetime.now().date()
    member = members.get(slack_id)
    who = member["member_name"] if member else slack_id
    set_semester_state(label, who, start_date=start_date)
    logger.warning(f"Semester restarted by {who} ({slack_id}): {label}, starting {start_date}")
    reply(event, f"✅ New semester started: *{label}*, beginning {start_date.strftime('%b %d, %Y')}.")
    post(ANNOUNCE_CHANNEL_ID, f"New semester started: *{label}*. Hours tracking has reset for the new term.")


def handle_admin_add(event, slack_id, text, members):
    if not is_admin_level(slack_id, members):
        reply(event, "You're not authorized to add admins.")
        return
    raw = text.split(None, 3)
    if len(raw) < 4 or not raw[3].strip():
        reply(event, "Usage: `admin add admin <name or @mention>`")
        return
    target_str = raw[3].strip()
    target = resolve_member(target_str, members)
    if not target:
        reply(event, f"Member not found: {target_str!r}. They need to `register` first.")
        return
    if target["slack_id"] == ADMIN_SLACK_ID or target["slack_id"] in load_admin_ids():
        reply(event, f"{target['member_name']} is already an admin.")
        return
    approver = members.get(slack_id)
    added_by = approver["member_name"] if approver else slack_id
    add_admin_record(target["slack_id"], target["member_name"], added_by)
    logger.warning(f"{added_by} added {target['member_name']} ({target['slack_id']}) as an admin.")
    reply(event, f"✅ {target['member_name']} is now an admin.")
    try:
        post(target["slack_id"], f"You've been made an admin of the shop bot by {added_by}. "
                                   f"Send `help` to see the full command list, including admin commands.")
    except SlackApiError as e:
        logger.warning(f"Could not DM new admin {target['slack_id']}: {e}")


def handle_admin_remove(event, slack_id, text, members):
    if not is_admin_level(slack_id, members):
        reply(event, "You're not authorized to remove admins.")
        return
    raw = text.split(None, 3)
    if len(raw) < 4 or not raw[3].strip():
        reply(event, "Usage: `admin remove admin <name or @mention>`")
        return
    target_str = raw[3].strip()
    target = resolve_member(target_str, members)
    if not target:
        reply(event, f"Member not found: {target_str!r}.")
        return
    if target["slack_id"] == ADMIN_SLACK_ID:
        reply(event, "The primary admin can't be removed.")
        return
    ok = remove_admin_record(target["slack_id"])
    reply(event, f"Removed {target['member_name']} as admin." if ok else f"{target['member_name']} wasn't a promoted admin.")
    if ok:
        logger.warning(f"{slack_id} removed admin status from {target['member_name']} ({target['slack_id']}).")


def handle_admin_remove_member(event, slack_id, text, members):
    """`admin remove member <name or @mention>` - deactivates a member (admin-level only)."""
    if not is_admin_level(slack_id, members):
        reply(event, "You're not authorized to remove members.")
        return
    raw = text.split(None, 3)
    if len(raw) < 4 or not raw[3].strip():
        reply(event, "Usage: `admin remove member <name or @mention>`")
        return
    target_str = raw[3].strip()
    target = resolve_member(target_str, members)
    if not target:
        reply(event, f"Member not found: {target_str!r}.")
        return
    if target["slack_id"] == ADMIN_SLACK_ID:
        reply(event, "The primary admin can't be removed.")
        return
    if target["slack_id"] == slack_id:
        reply(event, "You can't remove yourself. Have another admin do it.")
        return

    name = target["member_name"]
    target_slack_id = target["slack_id"]
    if target_slack_id in CURRENT_MEMBERS:
        checkout_time = datetime.now()
        hours, _ = close_open_session(target_slack_id, name, checkout_time)
        CURRENT_MEMBERS.discard(target_slack_id)
        SESSION_ALERTS.pop(target_slack_id, None)
        logger.info(f"Auto-checked out {name} on removal ({hours}h)")

    target["active"] = "False"
    write_members(members)
    remove_admin_record(target["slack_id"])
    office_hours.clear_for_member(OFFICE_HOURS_FILE, target["slack_id"])

    logger.warning(f"{slack_id} deactivated member {name} ({target['slack_id']}).")
    reply(event, f"{name} has been deactivated. Their attendance history is preserved; "
                 f"they can be restored with `admin restore member {name}`.")


def handle_admin_restore_member(event, slack_id, text, members):
    """`admin restore member <name or @mention>` - reactivates a previously removed member."""
    if not is_admin_level(slack_id, members):
        reply(event, "You're not authorized to restore members.")
        return
    raw = text.split(None, 3)
    if len(raw) < 4 or not raw[3].strip():
        reply(event, "Usage: `admin restore member <name or @mention>`")
        return
    target_str = raw[3].strip()
    target = resolve_member(target_str, members)
    if not target:
        reply(event, f"Member not found: {target_str!r}.")
        return
    if is_active_member(target):
        reply(event, f"{target['member_name']} is already active.")
        return
    target["active"] = "True"
    write_members(members)
    logger.info(f"{slack_id} restored member {target['member_name']} ({target['slack_id']}).")
    reply(event, f"{target['member_name']} has been restored and can check in again.")


def handle_admin_log(event, slack_id, text, members):
    """`admin log [n]` - tail the last n lines of bot.log (default 30, max 200)."""
    if not is_admin_level(slack_id, members):
        reply(event, "You're not authorized to view logs.")
        return

    n = 30
    parts = text.split()
    if len(parts) >= 3 and parts[2].isdigit():
        n = max(1, min(int(parts[2]), 200))

    log_path = os.path.join(_DATA_DIR, "bot.log")
    if not os.path.exists(log_path):
        reply(event, "No log file found yet.")
        return

    try:
        with open(log_path, "r", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        reply(event, f"Could not read log file: {e}")
        return

    tail = lines[-n:]
    text_out = "".join(tail)
    max_chars = 3500
    truncated = False
    if len(text_out) > max_chars:
        text_out = text_out[-max_chars:]
        truncated = True

    header = f"Last {len(tail)} log line(s)" + (" (truncated to fit)" if truncated else "") + ":"
    reply(event, f"{header}\n```{text_out}```")


def handle_about(event):
    reply(event, (
        "*Shop Attendance Bot*\n\n"
        "I track check-ins and check-outs for the shop, keep hours per semester, "
        "handle approvals, and cover a few admin and office-hours-scheduling tasks "
        "behind the scenes.\n\n"
        "Send `help` any time to see everything I can do.\n\n"
        "Built and maintained by Kushagra Taneja. Found a bug or have a suggestion? "
        "DM him directly on Slack, or use `feedback <message>` to send it anonymously."
    ))


def _office_hours_today_lines():
    today_idx = datetime.now().weekday()
    blocks = office_hours.list_for_day(OFFICE_HOURS_FILE, today_idx)
    return [
        f"- {b['member_name']}: {office_hours.format_time_12h(b['start_time'])}"
        f"-{office_hours.format_time_12h(b['end_time'])}"
        for b in blocks
    ]


def _current_member_names(members):
    """CURRENT_MEMBERS holds slack_ids; resolve to display names, sorted."""
    names = []
    for slack_id in CURRENT_MEMBERS:
        m = members.get(slack_id)
        names.append(m["member_name"] if m else slack_id)
    return sorted(names)


def handle_is_shop_open(channel):
    members = load_members()
    if CURRENT_MEMBERS:
        people = _current_member_names(members)
        msg = "Yes, the shop is open. Currently checked in:\n- " + "\n- ".join(people)
    else:
        msg = "No, the shop is currently closed."
    oh_lines = _office_hours_today_lines()
    if oh_lines:
        msg += "\n\nScheduled office hours today:\n" + "\n".join(oh_lines)
    post(channel, msg)


def handle_who_is_in(event, members):
    people = _current_member_names(members)
    if people:
        msg = "Currently checked in:\n- " + "\n- ".join(people)
    else:
        msg = "No one is currently checked in."
    oh_lines = _office_hours_today_lines()
    if oh_lines:
        msg += "\n\nScheduled office hours today:\n" + "\n".join(oh_lines)
    reply(event, msg)


# --------------------------
# Hours report helpers
# --------------------------

# --------------------------
# Semester state
# --------------------------
# The authoritative "current semester" is whatever `admin semester restart` last
# set, persisted in SEMESTER_STATE_FILE. This is what lets the club run for years
# without anyone editing code to update academic-calendar dates. On a brand-new
# install (no state file yet) we bootstrap from a best-guess based on the
# traditional SBU calendar and persist that guess so every later call is stable.
def _guess_semester_label(dt):
    m, d, year = dt.month, dt.day, dt.year
    if (m == 12 and d >= 21) or (m == 1 and d <= 26):
        return f"Winter {year if m == 1 else year + 1}"
    if (m == 1 and d >= 27) or (2 <= m <= 4) or (m == 5 and d <= 31):
        return f"Spring {year}"
    if m in (6, 7) or (m == 8 and d <= 14):
        return f"Summer {year}"
    return f"Fall {year}"

def _guess_semester_start(dt):
    m, d, year = dt.month, dt.day, dt.year
    if m == 12 and d >= 21:
        return datetime(year, 12, 21).date()
    if m == 1 and d <= 26:
        return datetime(year - 1, 12, 21).date()
    if (m == 1 and d >= 27) or (2 <= m <= 4) or (m == 5 and d <= 31):
        return datetime(year, 1, 27).date()
    if m in (6, 7) or (m == 8 and d <= 14):
        return datetime(year, 6, 1).date()
    return datetime(year, 8, 15).date()

def get_semester_state():
    if not os.path.exists(SEMESTER_STATE_FILE):
        return None
    with open(SEMESTER_STATE_FILE, "r", newline="") as f:
        rows = list(csv.DictReader(f))
    return rows[0] if rows else None

def set_semester_state(label, restarted_by, start_date=None):
    if start_date is None:
        start_date = datetime.now().date()
    row = {
        "semester_name": label,
        "start_date":    start_date.strftime("%Y-%m-%d"),
        "restarted_by":  restarted_by,
        "restarted_at":  datetime.now().isoformat(timespec="seconds"),
    }
    _atomic_write_csv(SEMESTER_STATE_FILE, SEMESTER_STATE_HEADERS, [row])

def get_current_semester():
    """
    Returns (semester_name, start_date, end_date) as (str, date, date), used to
    bound hours-report queries. end_date is always "today" -- the semester is
    open-ended until the next `admin semester restart`. History in attendance.csv
    is never deleted; this only changes what window reports look at.
    """
    state = get_semester_state()
    if state and state.get("semester_name") and state.get("start_date"):
        try:
            start = datetime.strptime(state["start_date"], "%Y-%m-%d").date()
            return state["semester_name"], start, datetime.now().date()
        except (ValueError, TypeError):
            pass

    # No valid state yet -- bootstrap from the calendar and persist it.
    now = datetime.now()
    label = _guess_semester_label(now)
    start = _guess_semester_start(now)
    set_semester_state(label, "auto (bootstrap)", start_date=start)
    return label, start, now.date()

def get_current_week_bounds():
    today = datetime.now().date()
    start = today - timedelta(days=today.weekday())
    end   = start + timedelta(days=6)
    return start, end

def format_hours_report(sessions, include_disapproved=False):
    lines          = []
    total_approved = 0.0
    total_pending  = 0.0
    i              = 0

    for row in sessions:
        approved = str(row.get("approved", "")).strip().lower()

        if approved in ("false", ""):
            status = "Pending"
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

        i += 1
        lines.append(f"{i}. {ci} - {co}  |  {hrs}  |  {status}")

    return "\n".join(lines), round(total_approved, 2), round(total_pending, 2)


def get_semester_sessions(slack_id, member_name, start_date, end_date, include_disapproved=False):
    rows    = read_attendance_rows()
    results = []
    for row in rows:
        if not _row_matches_member(row, slack_id, member_name):
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
        lead_str = "Not set - use `set my lead @mention` to assign one"

    sem_name, start, end = get_current_semester()
    if sem_name:
        all_sessions = get_semester_sessions(member["slack_id"], name, start, end, include_disapproved=True)
        approved  = sum(1 for r in all_sessions if str(r.get("approved","")).lower() == "true")
        pending   = sum(1 for r in all_sessions if str(r.get("approved","")).lower() in ("false","","none"))
        total_hrs = sum(
            float(r.get("hours", 0))
            for r in all_sessions
            if str(r.get("approved","")).lower() == "true"
        )
        sem_label = sem_name
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
    # Strip the command prefix case-insensitively (dispatcher matched on text_lc,
    # so `text` itself may not start with a lowercase "set my lead").
    prefix = "set my lead"
    arg = text.strip()[len(prefix):].strip() if text.lower().strip().startswith(prefix) else ""
    if not arg:
        reply(event, "Usage: `set my lead @mention` or `set my lead none`")
        return

    me = members.get(slack_id)
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
    prefix = "feedback"
    msg = text.strip()[len(prefix):].strip() if text.lower().strip().startswith(prefix) else text.strip()
    if not msg:
        reply(event, "Usage: `feedback <your message>`")
        return
    notify_all_admins(f"*Anonymous feedback:*\n{msg}")
    reply(event, "Your feedback has been sent anonymously. Thank you.")
    logger.info("Anonymous feedback received (sender identity withheld)")


def handle_my_hours(event, member, weekly=False):
    """
    `my hours`        - current semester summary
    `my hours weekly` - this Mon-Sun week only

    FIXED: added `weekly` parameter (was missing, causing TypeError when dispatcher
    called handle_my_hours(event, member, weekly=True)).
    """
    name = member["member_name"]

    if weekly:
        start, end = get_current_week_bounds()
        label      = f"week of {start} - {end}"
        sessions   = get_semester_sessions(member["slack_id"], name, start, end, include_disapproved=False)
        if not sessions:
            reply(event, f"No sessions recorded for you this week ({start} - {end}).")
            return
        body, approved_hrs, pending_hrs = format_hours_report(sessions, include_disapproved=False)
        reply(event, (
            f"Your hours - {label}:\n\n"
            f"{body}\n\n"
            f"Approved: {approved_hrs}h  |  Pending approval: {pending_hrs}h"
        ))
        return

    sem_name, start, end = get_current_semester()
    if sem_name is None:
        reply(event, "Could not determine the current semester. Contact an admin.")
        return

    sessions = get_semester_sessions(member["slack_id"], name, start, end, include_disapproved=False)
    if not sessions:
        reply(event, f"No sessions recorded for you this {sem_name} semester ({start} - {end}).")
        return

    body, approved_hrs, pending_hrs = format_hours_report(sessions, include_disapproved=False)
    reply(event, (
        f"Your hours - {sem_name} ({start} - {end}):\n\n"
        f"{body}\n\n"
        f"Approved: {approved_hrs}h  |  Pending approval: {pending_hrs}h"
    ))


def handle_top_hours(event, member, members):
    """`top hours` -- top 5 approved-hours totals for the current semester,
    plus the caller's own rank even if they're outside the top 5."""
    sem_name, start, end = get_current_semester()
    if sem_name is None:
        reply(event, "Could not determine the current semester. Contact an admin.")
        return

    totals = {}   # slack_id -> [display_name, hours]
    for row in read_attendance_rows():
        if str(row.get("approved", "")).strip().lower() != "true":
            continue
        try:
            ci_dt = row_to_dt(row, "check_in")
            ci_date = ci_dt.date() if ci_dt else None
        except (ValueError, TypeError):
            ci_date = None
        if ci_date is None or not (start <= ci_date <= end):
            continue
        sid = _resolve_row_slack_id(row, members)
        if not sid:
            continue
        try:
            hrs = float(row.get("hours", 0))
        except (ValueError, TypeError):
            hrs = 0.0
        display_name = members.get(sid, {}).get("member_name", row.get("member_name", "?"))
        if sid not in totals:
            totals[sid] = [display_name, 0.0]
        totals[sid][1] += hrs

    if not totals:
        reply(event, f"No approved hours recorded yet this {sem_name}.")
        return

    ranking = sorted(totals.items(), key=lambda kv: kv[1][1], reverse=True)

    lines = [f"Top hours - {sem_name}:"]
    for i, (sid, (name, hrs)) in enumerate(ranking[:5], start=1):
        lines.append(f"{i}. {name} - {round(hrs, 2)}h")

    caller_sid = member["slack_id"]
    caller_rank = next((i for i, (sid, _) in enumerate(ranking, start=1) if sid == caller_sid), None)
    if caller_rank is None:
        lines.append(f"\nYou: no approved hours yet this {sem_name}.")
        lines.append(f"\nThis command is just for fun!\n This leaderboard tracks hours logged, not how much someone contributes or how valuable they are to the workshop.\n Everyone contributes in different ways, so hours ≠ value!")
    elif caller_rank <= 5:
        lines.append(f"\nYou're #{caller_rank} of {len(ranking)}.")
        lines.append(f"\nThis command is just for fun!\n This leaderboard tracks hours logged, not how much someone contributes or how valuable they are to the workshop.\n Everyone contributes in different ways, so hours ≠ value!")

    else:
        caller_hrs = totals[caller_sid][1]
        lines.append(f"\nYou're #{caller_rank} of {len(ranking)} with {round(caller_hrs, 2)}h.")
        lines.append(f"\nReminder: This command is just for fun!\n This leaderboard tracks hours logged, not how much someone contributes or how valuable they are to the workshop.\n Everyone contributes in different ways, so hours ≠ value!")


    reply(event, "\n".join(lines))


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
    # name-only next() lookup - both failed for @mentions.
    target, _ = extract_mention_and_rest(raw, members)
    if not target:
        reply(event, f"Member not found: {raw!r}. Use a @mention or their full name.")
        return

    display_name = target["member_name"]

    if not (is_admin_level(slack_id, members) or is_authorized_approver(slack_id, display_name, members)):
        reply(event, "You're not authorized to view hours for that member.")
        return

    sem_name, start, end = get_current_semester()
    if sem_name is None:
        reply(event, "Could not determine the current semester. Contact an admin.")
        return

    sessions = get_semester_sessions(target["slack_id"], display_name, start, end, include_disapproved=True)
    if not sessions:
        reply(event, f"No sessions found for {display_name} this {sem_name} semester ({start} - {end}).")
        return

    body, approved_hrs, pending_hrs = format_hours_report(sessions, include_disapproved=True)
    reply(event, (
        f"Hours report for {display_name} - {sem_name} ({start} - {end}):\n\n"
        f"{body}\n\n"
        f"Approved: {approved_hrs}h  |  Pending: {pending_hrs}h"
    ))


# --------------------------
# Main event dispatcher
# --------------------------
def _help_text():
    return (
        "Available commands:\n"
        "\n"
        "*Attendance*\n"
        "- `check in` / `check out`\n"
        "\n"
        "*Shop status*\n"
        "- `who is in` / `is shop open`\n"
        "- `office hours today` - who's scheduled today\n"
        "\n"
        "*Hours*\n"
        "- `my hours` - semester summary\n"
        "- `my hours weekly` - this week\n"
        "- `my info` - your profile, lead, and session counts\n"
        "- `top hours` - top 5 for the semester, and your own rank\n"
        "\n"
        "*Approvals* (seniors/leads/admins)\n"
        "- `approve @mention` - approve all pending sessions\n"
        "- `disapprove @mention` - list pending sessions with IDs\n"
        "- `disapprove @mention <session_id>` - disapprove a specific session\n"
        "- `hours report @mention` - full semester report\n"
        "\n"
        "*Seniority-2+ / Admin*\n"
        "- `add session @mention <date> <start>-<end>` - log a forgotten session, "
        "e.g. `add session @jake yesterday 4pm-5pm`\n"
        "\n"
        f"*Office hours* (seniority {OFFICE_HOURS_MAX_SENIORITY_LEVEL}+ or admin to schedule; anyone can view)\n"
        "- `office hours set <day> <start>-<end>` - e.g. `office hours set Tue 3pm-5pm`\n"
        "- `office hours cancel <id>` / `office hours cancel all`\n"
        "- `office hours list` / `office hours list @mention`\n"
        "\n"
        "*Settings*\n"
        "- `set my lead @mention` / `set my lead none`\n"
        "\n"
        "*Admin / Seniority-1*\n"
        "- `admin force checkout @mention`\n"
        "- `set seniority @mention <1-5>`\n"
        "- `set lead @mention @lead` / `set lead @mention none`\n"
        "- `announcement formal` / `announcement casual`\n"
        "- `register @mention [Full Name]` - add a new member\n"
        "- `admin remove member @mention` / `admin restore member @mention`\n"
        "- `admin add admin @mention` / `admin remove admin @mention`\n"
        "- `admin semester restart [name]` / `admin semester restart from <date> [name]`\n"
        "- `admin log [n]` - view recent log lines (default 30, max 200)\n"
        "- `admin shutdown` - gracefully shut the bot down\n"
        "\n"
        "*Other*\n"
        "- `feedback <message>` - send anonymous feedback to the admins\n"
        "- `about` - what this bot is\n"
        "- `help` - this list"
    )


def process_message(client, req):
    if req.type != "events_api":
        return
    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

    event = req.payload.get("event", {})
    if event.get("type") != "message" or "bot_id" in event:
        return

    # Everything below can raise (bad CSV row, malformed mention, a Slack API
    # hiccup, etc). Catching it here means one bad message logs an error and
    # tells the person something went wrong, instead of taking the whole bot
    # (and everyone's open sessions) down with it.
    try:
        _dispatch_message(event)
    except Exception as e:
        logger.error(f"Unhandled error processing message: {e}", exc_info=True)
        try:
            if event.get("channel_type") == "im" and event.get("channel"):
                post(event["channel"], "Something went wrong processing that command. Please try again, "
                                       "or contact an admin if it keeps happening.")
        except Exception:
            logger.error("Failed to notify user of error.", exc_info=True)
        try:
            _notify_admins_of_error(f"Bot error while handling a message: `{e}`\nCheck bot.log for details.")
        except Exception:
            logger.error("Failed to notify admins of error.", exc_info=True)


def _dispatch_message(event):
    text         = event.get("text", "").strip()
    text_lc      = text.lower()
    slack_id     = event.get("user")
    channel_type = event.get("channel_type")

    # Public channels
    if channel_type in ("channel", "group"):
        if any(p in text_lc for p in ["who is in shop", "who's in shop", "who is in the shop", "who's in the shop"]):
            people = _current_member_names(load_members())
            msg = "Currently in shop: " + ", ".join(people) if people else "The shop is currently empty."
            oh_lines = _office_hours_today_lines()
            if oh_lines:
                msg += "\n\nScheduled office hours today:\n" + "\n".join(oh_lines)
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

    if not is_active_member(member):
        reply(event, "Your account has been deactivated. Contact an admin if this is a mistake.")
        return

    parts = text_lc.split()

    logger.info(f"Command from {member['member_name']} ({slack_id}): {text!r}")

    # Watchdog confirmation - member or senior replying "y".
    #
    # FIXED: the member's own "y" now resolves their pending alert regardless
    # of its stage (awaiting_member OR awaiting_senior) -- confirm_session()
    # already clears any senior escalation tied to it. Previously this only
    # matched the "awaiting_member" stage, so a "y" sent *after* the 30-minute
    # escalation to a senior (but still before the 8h hard cutoff) was
    # silently dropped, and the person could still get auto-checked-out
    # despite having replied "y" -- this was the "still bugs out" behavior.
    if text_lc == "y":
        if slack_id in SESSION_ALERTS:
            ok = confirm_session(slack_id, slack_id, members)
            if ok:
                reply(event, "Got it - you're all set until the 8-hour mark, at which point "
                             "you'll be automatically checked out.")
            else:
                reply(event, "That confirmation has expired (you're past the 8-hour mark) - "
                             "please `check out`, or contact an admin if you're still working.")
            return
        if slack_id in SENIOR_PENDING:
            target_slack_id = SENIOR_PENDING[slack_id]
            ok = confirm_session(target_slack_id, slack_id, members)
            target_member = members.get(target_slack_id)
            target_name = target_member["member_name"] if target_member else target_slack_id
            if ok:
                reply(event, f"Confirmed - {target_name}'s session has been extended.")
                if target_member:
                    post(target_member["slack_id"],
                         f"Your session was confirmed by a senior member. "
                         f"You will be checked out automatically at the 8-hour mark.")
            else:
                reply(event, f"That confirmation for {target_name} has expired.")
            return
        return  # spurious "y" - ignore silently

    if "check in" in text_lc:
        handle_check_in(event, member)
    elif "check out" in text_lc:
        handle_check_out(event, member)
    elif text_lc == "confirm shutdown":
        handle_confirm_shutdown(event, slack_id, members)
    elif text_lc == "confirm semester restart":
        handle_confirm_semester_restart(event, slack_id, members)
    elif text_lc == "cancel":
        if slack_id in PENDING_ADMIN_CONFIRM:
            PENDING_ADMIN_CONFIRM.pop(slack_id, None)
            reply(event, "Cancelled.")
        else:
            reply(event, "Nothing to cancel.")
    elif text_lc == "admin shutdown":
        handle_admin_shutdown_request(event, slack_id, members)
    elif text_lc.startswith("admin semester restart"):
        handle_admin_semester_restart_request(event, slack_id, text, members)
    elif text_lc.startswith("admin "):
        if len(parts) >= 3 and parts[1] == "force" and parts[2] == "checkout":
            handle_admin_force_checkout(event, slack_id, parts, members)
        elif len(parts) >= 3 and parts[1] == "add" and parts[2] == "admin":
            handle_admin_add(event, slack_id, text, members)
        elif len(parts) >= 3 and parts[1] == "remove" and parts[2] == "admin":
            handle_admin_remove(event, slack_id, text, members)
        elif len(parts) >= 3 and parts[1] == "remove" and parts[2] == "member":
            handle_admin_remove_member(event, slack_id, text, members)
        elif len(parts) >= 3 and parts[1] == "restore" and parts[2] == "member":
            handle_admin_restore_member(event, slack_id, text, members)
        elif len(parts) >= 2 and parts[1] == "log":
            handle_admin_log(event, slack_id, text, members)
        else:
            reply(event, (
                "Unknown admin command. Available: `admin force checkout <name>`, `admin shutdown`, "
                "`admin semester restart`, `admin add admin <name>`, `admin remove admin <name>`, "
                "`admin remove member <name>`, `admin restore member <name>`, `admin log [n]`"
            ))
    elif text_lc.startswith("office hours"):
        handle_office_hours(event, slack_id, text, members)
    elif text_lc.startswith("set my lead"):
        handle_set_my_lead(event, slack_id, text, members)
    elif text_lc.startswith("set seniority ") or text_lc.startswith("set lead "):
        # Pass original `text` so @mentions are preserved for parse_mention()
        handle_set_member_field(event, slack_id, text, members)
    elif text_lc.startswith("approve ") or text_lc.startswith("disapprove "):
        handle_approve_disapprove(event, slack_id, text, members)
    elif text_lc.startswith("add session"):
        handle_add_session(event, slack_id, text, members)
    elif text_lc == "announcement formal":
        handle_announcement_formal(event, slack_id, members)
    elif text_lc == "announcement casual":
        handle_announcement_casual(event, slack_id, members)
    elif "is shop open" in text_lc or "is the shop open" in text_lc:
        handle_is_shop_open(event["channel"])
    elif text_lc == "my info":
        handle_my_info(event, member, members)
    elif text_lc == "my hours weekly":
        handle_my_hours(event, member, weekly=True)
    elif text_lc == "my hours":
        handle_my_hours(event, member)
    elif text_lc in ("top hours", "leaderboard"):
        handle_top_hours(event, member, members)
    elif text_lc.startswith("hours report "):
        # Pass original `text` so @mentions are preserved for parse_mention()
        handle_hours_report(event, slack_id, text, members)
    elif text_lc.startswith("feedback "):
        handle_feedback(event, slack_id, text, members)
    elif text_lc in ("about", "about bot", "who are you"):
        handle_about(event)
    elif text_lc in ("help", "commands", "?"):
        reply(event, _help_text())
    elif "who is in" in text_lc or "who's in" in text_lc:
        handle_who_is_in(event, members)
    elif text_lc.startswith("register "):
        handle_register(event, slack_id, text, members)
    else:
        reply(event, _help_text())

# --------------------------
# Graceful shutdown
# --------------------------
def force_checkout_all(reason="shutdown"):
    if not CURRENT_MEMBERS:
        return
    members = load_members()
    checkout_time = datetime.now()
    for slack_id in list(CURRENT_MEMBERS):
        member = members.get(slack_id)
        name = member["member_name"] if member else slack_id
        hours, check_in_iso = close_open_session(slack_id, name, checkout_time)
        CURRENT_MEMBERS.discard(slack_id)
        SESSION_ALERTS.pop(slack_id, None)
        logger.info(f"Auto-checked out {name} on {reason} ({hours}h)")
        if member:
            try:
                post(member["slack_id"],
                     f"You were automatically checked out due to {reason}. "
                     f"Hours recorded: {hours}. Contact an admin if this is incorrect.")
            except Exception as e:
                logger.warning(f"Could not notify {name} on {reason}: {e}")
    try:
        post(ANNOUNCE_CHANNEL_ID, f"Shop closed - all members checked out due to {reason}.")
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
    logger.error(f"Member sync failed: {e} - continuing with existing members.csv")

ensure_attendance_file()
ensure_admins_file()
office_hours.ensure_file(OFFICE_HOURS_FILE)

_promoted_admin_count = len(load_admin_ids())
logger.info(f"Root admin: {ADMIN_SLACK_ID}. Promoted admins: {_promoted_admin_count}.")

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