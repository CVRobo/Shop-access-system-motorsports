import os
import sys
import csv
import time
import logging
import tempfile
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from dotenv import load_dotenv

# When running as a PyInstaller bundle, data files live in sys._MEIPASS.
# When running normally, they live next to this script.
_BASE_DIR = sys._MEIPASS if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_BASE_DIR, ".env"))

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
MEMBERS_CHANNEL_ID = "C09HVFVPCN9"
MEMBERS_FILE = "members.csv"
MEMBERS_HEADERS = ["card_uid", "member_name", "slack_id", "seniority", "lead_slack_id"]

DEFAULT_SENIORITY = 5  # 1 = most senior, 5 = most junior

logger = logging.getLogger(__name__)
client = WebClient(token=SLACK_BOT_TOKEN)

# --------------------------
# Fetch channel members
# --------------------------
def get_channel_members(channel_id):
    members = []
    cursor = None
    while True:
        try:
            response = client.conversations_members(channel=channel_id, cursor=cursor)
            members.extend(response["members"])
            cursor = response["response_metadata"].get("next_cursor")
            if not cursor:
                break
        except SlackApiError as e:
            if e.response["error"] == "ratelimited":
                retry_after = int(e.response.headers.get("Retry-After", 20))
                logger.warning(f"Rate limited fetching members. Retrying in {retry_after}s...")
                time.sleep(retry_after)
            else:
                logger.error(f"Error fetching channel members: {e.response['error']}")
                break
    return members

def get_user_details(user_id):
    try:
        response = client.users_info(user=user_id)
        if response["ok"]:
            user = response["user"]
            if not user.get("is_bot") and not user.get("deleted"):
                return {
                    "name": user["profile"].get("real_name", user["name"]),
                    "id": user["id"]
                }
        return None
    except SlackApiError as e:
        logger.error(f"Failed to fetch user {user_id}: {e.response['error']}")
        return None

# --------------------------
# Atomic CSV write
# --------------------------
def _atomic_write_csv(filepath, headers, rows):
    """
    Write rows to a temp file in the same directory, then atomically
    replace the target file. Prevents corruption on power loss mid-write.
    """
    dirpath = os.path.dirname(os.path.abspath(filepath))
    fd, tmp_path = tempfile.mkstemp(dir=dirpath, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp_path, filepath)  # atomic on all supported OSes
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

# --------------------------
# Load existing members.csv to preserve manual edits
# --------------------------
def load_existing_members():
    """Return dict of slack_id -> existing row dict so manual edits are preserved."""
    existing = {}
    if not os.path.exists(MEMBERS_FILE):
        return existing
    with open(MEMBERS_FILE, "r", newline="") as f:
        for row in csv.DictReader(f):
            existing[row["slack_id"].strip()] = row
    return existing

# --------------------------
# Main update function (called by slack_bot_main on startup)
# --------------------------
def update_members_csv():
    if not SLACK_BOT_TOKEN:
        logger.warning("Missing SLACK_BOT_TOKEN — skipping member sync.")
        return

    logger.info(f"Syncing members from channel {MEMBERS_CHANNEL_ID}...")
    member_ids = get_channel_members(MEMBERS_CHANNEL_ID)
    logger.info(f"Found {len(member_ids)} member IDs. Fetching details...")

    existing = load_existing_members()
    rows = []

    for uid in member_ids:
        info = get_user_details(uid)
        if not info:
            continue

        slack_id = info["id"]
        name = info["name"]

        if slack_id in existing:
            prev = existing[slack_id]
            rows.append({
                "card_uid":      prev.get("card_uid", "ABC123"),
                "member_name":   name,
                "slack_id":      slack_id,
                "seniority":     prev.get("seniority", DEFAULT_SENIORITY),
                "lead_slack_id": prev.get("lead_slack_id", ""),
            })
        else:
            logger.info(f"New member detected: {name} ({slack_id})")
            rows.append({
                "card_uid":      "ABC123",
                "member_name":   name,
                "slack_id":      slack_id,
                "seniority":     DEFAULT_SENIORITY,
                "lead_slack_id": "",
            })

    _atomic_write_csv(MEMBERS_FILE, MEMBERS_HEADERS, rows)
    logger.info(f"members.csv updated with {len(rows)} members.")

# --------------------------
# Allow running standalone
# --------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    update_members_csv()