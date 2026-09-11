"""
Recurring weekly "office hours" scheduling.

Kept intentionally simple on purpose: a block is just (day-of-week, start
time, end time) with no calendar dates and no timezone handling beyond the
server's local clock (same as the rest of the bot) -- it repeats every week
forever until cancelled. That keeps the parsing to a day-name lookup and one
small time regex, so it doesn't need a separate parsing service or process;
it's just split into its own file for readability, imported by the bot like
get_members.py already is.
"""
import os
import re
import csv
import tempfile
from datetime import datetime

OFFICE_HOURS_HEADERS = ["id", "member_name", "slack_id", "day", "start_time", "end_time", "created_at"]

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

_DAY_ALIASES = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

_TIME_RE = re.compile(r"^(\d{1,2})(?::([0-5]\d))?\s*(am|pm)?$", re.IGNORECASE)


# --------------------------
# Parsing / formatting
# --------------------------
def parse_day(token):
    """Return 0-6 (Mon-Sun), or None if unrecognized."""
    return _DAY_ALIASES.get(token.strip().lower())


def parse_time(token):
    """
    Parse a time string into 24h "HH:MM", or None if unparseable/ambiguous.
    Accepts "3pm", "3:00pm", "15:00", "3:30 PM". Requires either an am/pm
    suffix or an unambiguous 24h hour (0, or 13-23) -- a bare 1-12 with no
    am/pm is rejected rather than guessed, since guessing wrong silently
    schedules the wrong half of the day.
    """
    m = _TIME_RE.match(token.strip())
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    suffix = (m.group(3) or "").lower()

    if suffix:
        if not (1 <= hour <= 12):
            return None
        if suffix == "am":
            hour = 0 if hour == 12 else hour
        else:
            hour = 12 if hour == 12 else hour + 12
    else:
        if not (0 <= hour <= 23):
            return None
        if 1 <= hour <= 12:
            return None  # ambiguous without am/pm

    return f"{hour:02d}:{minute:02d}"


def parse_time_range(text):
    """
    Parse "3pm-5pm", "3:00pm - 5:30pm", "15:00-17:30" into (start, end) as
    "HH:MM" 24h strings. Returns None on any parse failure or if end <= start.
    """
    if "-" not in text:
        return None
    left, right = text.split("-", 1)
    start = parse_time(left)
    end = parse_time(right)
    if start is None or end is None:
        return None
    if end <= start:
        return None
    return start, end


def format_time_12h(hhmm):
    h, m = (int(x) for x in hhmm.split(":"))
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suffix}"


# --------------------------
# Storage (small, self-contained -- doesn't depend on the caller's helpers)
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


def ensure_file(path):
    if not os.path.exists(path):
        _atomic_write_csv(path, OFFICE_HOURS_HEADERS, [])


def read_all(path):
    ensure_file(path)
    with open(path, "r", newline="") as f:
        return list(csv.DictReader(f))


def write_all(path, rows):
    _atomic_write_csv(path, OFFICE_HOURS_HEADERS, rows)


def add_block(path, member_name, slack_id, day, start, end):
    """
    Returns (new_row, None) on success, or (None, conflicting_row) if this
    overlaps a block the same person already has on the same day.
    """
    rows = read_all(path)
    for r in rows:
        if r["slack_id"] == slack_id and int(r["day"]) == day:
            if not (end <= r["start_time"] or start >= r["end_time"]):
                return None, r
    max_id = max((int(r["id"]) for r in rows), default=0)
    new_row = {
        "id":           str(max_id + 1),
        "member_name":  member_name,
        "slack_id":     slack_id,
        "day":          str(day),
        "start_time":   start,
        "end_time":     end,
        "created_at":   datetime.now().isoformat(timespec="seconds"),
    }
    rows.append(new_row)
    write_all(path, rows)
    return new_row, None


def remove_block(path, block_id, slack_id=None):
    """Removes a block by id. If slack_id is given, only removes it if owned by that person."""
    rows = read_all(path)
    for i, r in enumerate(rows):
        if r["id"] == str(block_id) and (slack_id is None or r["slack_id"] == slack_id):
            removed = rows.pop(i)
            write_all(path, rows)
            return removed
    return None


def clear_for_member(path, slack_id):
    rows = read_all(path)
    kept = [r for r in rows if r["slack_id"] != slack_id]
    removed_count = len(rows) - len(kept)
    if removed_count:
        write_all(path, kept)
    return removed_count


def list_for_member(path, slack_id):
    return sorted(
        (r for r in read_all(path) if r["slack_id"] == slack_id),
        key=lambda r: (int(r["day"]), r["start_time"]),
    )


def list_for_day(path, day):
    return sorted(
        (r for r in read_all(path) if int(r["day"]) == day),
        key=lambda r: r["start_time"],
    )