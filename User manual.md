# Shop Attendance & Office Hours Bot — User & Maintainer Manual

Repo: https://github.com/CVRobo/Shop-access-system-motorsports

This document has two parts: a **user guide** (for anyone using the bot day-to-day) and a **maintainer guide** (for whoever inherits the code). Read only Part 1 if you just want to use the bot. Read all of it if you're taking over the project.

---

# PART 1 — USER GUIDE

## The basics

- The bot lives in Slack. You talk to it by **direct-messaging it** — most commands (check in, check out, your hours, admin stuff) **only work in a DM**, not in a public channel.
- Two exceptions work in the public shop channel too: asking **"who is in shop"** (or "who's in shop" / "who is in the shop") and **"is shop open"** (or "is the shop open"). Everything else you type in a public channel is ignored by the bot.
- Referencing another member in a command: use an **@mention** (`@Jane`) whenever you can — it's unambiguous. Plain full names also work (matched case-insensitively against `members.csv`), but if two people share a name, only @mentions are safe.
- Times: `3pm`, `3:00pm`, `3:30 PM`, or 24-hour `15:00` / `15:30`. A bare hour like `3` with no am/pm is rejected on purpose — always include am/pm or use 24h format.
- Time ranges: `start-end`, e.g. `3pm-5pm`. The end must be after the start on the same day — ranges can't cross midnight (e.g. `11pm-1am` won't work).
- Days: `Mon`/`Tue`/`Wed`/`Thu`/`Fri`/`Sat`/`Sun` or the full name, case-insensitive.
- Dates (for `add session` / `admin semester restart`): `today`, `yesterday`, `YYYY-MM-DD`, or `M/D`.

## The seniority system

Every member has a **seniority rank from 1 to 5**:

- **1 = most senior/trusted** (e.g. eboard/leadership)
- **5 = newest** — this is the default for everyone when they're first registered

Lower number = more senior = more permissions. Each rank unlocks everything below it (a seniority-1 member can do everything a seniority-2, -3, -4, and -5 member can do, plus more).

| Rank | Unlocks |
|---|---|
| 5 (everyone) | Check in/out, view your own hours/info, set your own lead, check who's in / shop status, view (not set) office hours, send anonymous feedback |
| 4 | Approve/disapprove hours for members less senior than you |
| 3 | Schedule and cancel your **own** recurring office hours |
| 2 | Your own hours auto-approve on checkout, approve/disapprove your own sessions, register new members, retroactively log a forgotten session (`add session`) |
| 1 | Force-checkout anyone, change anyone's seniority/lead, switch announcement mode, deactivate/restore members, view logs, shut down the bot, restart the semester, promote/demote admins |

Separately from seniority, there's an **admin list** (`admins.csv`) — people promoted to admin-level access without necessarily being seniority-1. See "What counts as admin-level" in Part 2.

Note: when the bot's replies say something like *"requires seniority 3 or higher,"* it means rank 3 **or more senior** (i.e. rank 1, 2, or 3) — not rank 3, 4, or 5. This phrasing trips people up; see Known Issues.

## Everyone's commands

| Command | What it does |
|---|---|
| `check in` | Checks you in. DM only. |
| `check out` | Checks you out and records your hours. DM only. |
| `who is in` | Lists who's currently checked in, plus today's office hours. |
| `is shop open` | Same info, phrased as open/closed. |
| `office hours today` | Who's scheduled for office hours today. |
| `office hours list [@mention]` | View your (or someone else's) recurring office-hours schedule. |
| `my hours` | Your hours for the current semester. |
| `my hours weekly` | Your hours for this Mon–Sun week. |
| `my info` | Your seniority, your lead, and session counts. |
| `set my lead @mention` / `set my lead none` | Set or clear who gets pinged if you don't respond during a long shift. |
| `feedback <message>` | Sends an anonymous message to all admins. |
| `about` | What the bot is. |
| `help` | Full command list. |

## Seniority 3+ commands

| Command | What it does |
|---|---|
| `office hours set <day> <start>-<end>` | Schedule a recurring weekly office-hours block, e.g. `office hours set Tue 3pm-5pm`. |
| `office hours cancel <id>` / `office hours cancel all` | Cancel your own block(s). See the ID with `office hours list`. |

## Approvals (seniors, leads, or admins)

Available to anyone more senior than the target member, that member's designated lead, or an admin.

| Command | What it does |
|---|---|
| `approve @mention` | Approves all of that person's pending sessions. |
| `disapprove @mention` | Lists that person's pending sessions with their IDs. |
| `disapprove @mention <session_id>` | Disapproves one specific session. |
| `hours report @mention` | Full semester breakdown (approved/pending/disapproved) for that person. |

Seniority 1–2 members' hours auto-approve when they check out; they can still self-approve/disapprove a specific session with the same commands.

## Seniority 2+ / Admin commands

| Command | What it does |
|---|---|
| `register @mention [Full Name]` | Adds a new member (seniority 5 by default). If no name is given, the bot pulls their Slack display name. |
| `add session @mention <date> <start>-<end>` | Retroactively logs a forgotten check-in/out, e.g. `add session @jake yesterday 4pm-5pm`. Goes in as pending, needs approval. |

## Admin-level only

"Admin-level" = the hardcoded root admin, anyone in `admins.csv`, or any seniority-1 member.

| Command | What it does |
|---|---|
| `admin force checkout @mention` | Force-closes someone's open session (e.g. they forgot to check out). |
| `set seniority @mention <1-5>` | Changes someone's seniority. |
| `set lead @mention @lead` / `set lead @mention none` | Sets or clears someone else's lead. |
| `office hours cancel @mention <id/all>` | Cancels someone **else's** office hours (regular members can only cancel their own). |
| `announcement formal` / `announcement casual` | Switches the shop-open announcement style. |
| `admin remove member @mention` | Deactivates a member — auto-checks them out if active, clears their office hours, strips admin status. History is kept, not deleted. |
| `admin restore member @mention` | Reactivates a deactivated member. |
| `admin add admin @mention` / `admin remove admin @mention` | Promotes/demotes a promoted admin. Target must already be a registered member. |
| `admin semester restart [name]` / `admin semester restart from <date> [name]` | Starts a new reporting period. Requires typing `confirm semester restart` within 2 minutes, or `cancel` to abort. Old history is never deleted — this only changes the report window. |
| `admin log [n]` | Shows the last `n` lines of `bot.log` (default 30, max 200). |
| `admin shutdown` | Gracefully checks everyone out and shuts the bot down completely. Requires `confirm shutdown` within 2 minutes. **Does not auto-restart** — someone has to start it again manually. |

## Inactivity check-ins

If you've been checked in for a while (`SESSION_CHECK_HOURS`, currently 3h), the bot DMs you asking if you're still there — reply **`y`** to confirm. If you don't respond within 30 minutes, it escalates to the most senior person currently in the shop, who can also reply `y` on your behalf. If nobody confirms, you're auto-checked-out. Regardless of any of this, everyone is auto-checked-out at the 8-hour hard cap no matter what.

---

# PART 2 — MAINTAINER GUIDE

## Getting set up

1. **Get repo access**: message Kush directly on Slack and ask to be added as a collaborator on the GitHub repo.
2. **Getting a build / redeploying**: this bot ships as a standalone Windows `.exe`, built automatically by GitHub Actions on every push to `main`. To get a fresh copy:
   - Push your change to `main`.
   - Go to the repo's **Actions** tab → the latest **"Build Windows EXE"** run.
   - Download the **`slack-bot-windows`** artifact (kept for 30 days) — it contains `slack_bot_main.exe`.
   - Replace the exe on the machine that runs the bot and restart it. (Confirm with Kush how it's actually launched/kept running on the shop's machine — the bot itself doesn't auto-restart after a shutdown.)

## How it's built / architecture

This is a **single always-on Python process** using Slack's Socket Mode (a persistent websocket connection, not a hosted webhook), so it needs to be running continuously on some machine to work at all — it's not a serverless/on-demand bot.

**File map:**

- **`slack_bot_main.py`** — the whole bot. Config constants at the top, then CSV helpers, then command handlers, then the session watchdog, then startup code that runs top-to-bottom when the process starts.
  - `_dispatch_message()` is the router every DM goes through. New commands get added as new `elif` branches near the bottom of that function.
  - `is_admin_level()` is the single permission check used everywhere admin access is required — root admin, `admins.csv`, or seniority-1. If you add a new admin-only command, use this function rather than re-deriving the logic.
  - The **session watchdog** (`start_watchdog` / `_watchdog_tick`) runs on its own background thread every `WATCHDOG_INTERVAL_SECONDS` (60s). It checks for long open sessions, sends inactivity pings, escalates to the most senior person present, and enforces the hard checkout cap. It's also where the daily CSV backup and the office-hours reminder check happen.
  - All CSV writes go through `_atomic_write_csv()` (write to a temp file, then `os.replace`) so a crash mid-write can't corrupt a data file.
- **`office_hours.py`** — deliberately self-contained: its own CSV read/write, its own day/time parsing, no dependency on anything else in the bot. Handles only recurring weekly blocks (no calendar dates, no timezone handling beyond the server's local clock).
- **`get_members.py`** (not covered in this manual) — `update_members_csv()` is called on startup to sync `members.csv`. If member data ever looks stale or wrong after a restart, this is the first place to check.

**Data files** (plain CSV, all created automatically if missing, all living in the same folder as the exe):

| File | Contents |
|---|---|
| `members.csv` | Roster: card_uid, member_name, slack_id, seniority, lead_slack_id, active |
| `attendance.csv` | One row per session: check-in/out times, hours, approval status |
| `admins.csv` | Promoted admins (in addition to the hardcoded root admin in `slack_bot_main.py`) |
| `office_hours.csv` | Recurring weekly office-hours blocks |
| `semester_state.csv` | Current reporting-period name and start date |
| `bot.log` | Rotating log file (2MB × 5 backups) |
| `backups/` | Daily copies of all the above, kept for 30 days |

## Configuration knobs

Near the top of `slack_bot_main.py`:

- `ADMIN_SLACK_ID` — the hardcoded root admin, can't be removed via commands.
- `ANNOUNCE_CHANNEL_ID` — where shop-open/shop-closed messages post.
- `OFFICE_HOURS_MAX_SENIORITY_LEVEL` (currently 3) — the eligibility cutoff for scheduling office hours.
- `STALE_SESSION_HOURS`, `SESSION_CHECK_HOURS`, `SESSION_RESPONSE_MINUTES`, `SESSION_AUTO_CHECKOUT_HOURS`, `WATCHDOG_INTERVAL_SECONDS` — all the inactivity-check timing.
- `BACKUP_RETENTION_DAYS`, `ERROR_NOTIFY_COOLDOWN_HOURS`, `ADMIN_CONFIRM_TIMEOUT_MINUTES` — self-explanatory.

## Known issues / things to look into

These were flagged in a code review and haven't been fixed yet. None of them are actively breaking things, but they're worth understanding before you touch the related code, and worth fixing when you have time:

1. **Security — the built `.exe` contains live Slack tokens.** The GitHub Actions workflow writes `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN` into a `.env` and bundles it directly into the exe via `--add-data ".env;."`, then uploads that exe as a downloadable build artifact. Anyone who can download the artifact can extract the tokens (PyInstaller bundles are trivial to unpack) and impersonate the bot. **Fix before the next real deploy**: have the bot load `.env` from next to the exe (`_DATA_DIR`) instead of from the bundle (`_MEIPASS`/`_ASSET_DIR`), and stop baking real secrets into CI-built artifacts. Rotate both tokens once this is fixed, since existing artifacts still have the old ones embedded.
2. **Office-hours ID reuse can suppress a reminder.** New block IDs are `max(existing) + 1`, so canceling a block and creating a new one same-day can reuse its old ID. If the old block already triggered today's reminder, the new block (different time, same recycled ID) won't get its own reminder, because the dedup key collides.
3. **`"check in"` / `"check out"` match anywhere in a message, not just as a full command.** The dispatcher does a substring check (`"check in" in text_lc`), so an ordinary sentence that happens to contain that phrase (e.g. "forgot to check in yesterday") will actually trigger a check-in.
4. **"Seniority N or higher" wording is easy to misread.** Since lower numbers are more senior, a couple of reply strings (office-hours eligibility message, help text) read like you need a *higher number* when you actually need a *lower* one. Worth rewording for clarity.
5. **No locking between the watchdog thread and the message-handling thread.** Both do read-modify-write on the same CSV files without any lock. If an auto-checkout and a manual `check out` land at nearly the same moment, you can get a duplicate notification or (rarer) a lost write. Hasn't caused real problems yet, but if you ever see odd duplicate messages, this is why.
6. **Office hours / retroactive sessions can't cross midnight.** By design, end time must be later than start time on the same day. Fine today, but worth knowing if evening shifts ever need to run past 12am.

## Contact

For repo access, questions about original design decisions, or anything not covered here — message Kush directly on Slack.