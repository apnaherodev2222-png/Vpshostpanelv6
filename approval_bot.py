"""
approval_bot.py
──────────────────────────────────────────────────────────────────────────
SEPARATE Telegram bot (own bot token, own process) whose only job is
auto-moderation of free-tier uploads for hosting_panel.py.

Flow:
  1. hosting_panel.py inserts a row into `pending_approvals`
     (status='pending') whenever a FREE-tier user uploads a script.
  2. This process polls that table every POLL_SECONDS.
  3. For each pending row it runs script_scanner.scan_file() against the
     uploaded file and writes the verdict back:
       - clear   -> status='approved', decided_by='bot'
       - flagged -> status='rejected', decided_by='bot', and inserts a
                    2-hour row into `muted_users`
  4. hosting_panel.py's own approval_result_notifier() background loop
     (running in the MAIN bot process) picks up the decision and DMs the
     uploader via the MAIN bot — not this one. That's deliberate: the user
     has already /start'd the main bot (they used it to upload), but very
     likely never started this separate approval bot, so a send_message
     from here would fail with "chat not found". This bot only decides;
     it doesn't need to be the one that talks to the user.

Two approvers, either sufficient: hosting_panel.py's admin panel can also
write status='approved'/'rejected' with decided_by='admin' to the same
table — whichever decides first wins.

Run as its own process, alongside (not instead of) hosting_panel.py:
    python3 approval_bot.py

Needs its own token in .env:
    APPROVAL_BOT_TOKEN=123456:ABC-your-second-bot-token
"""
import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from script_scanner import scan_file

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s [approval_bot] %(message)s')
logger = logging.getLogger(__name__)

APPROVAL_BOT_TOKEN = os.getenv("APPROVAL_BOT_TOKEN")

BASE_DIR = Path(__file__).parent.absolute()
UPLOAD_BOTS_DIR = BASE_DIR / 'upload_bots'      # same layout hosting_panel.py uses per-user
DATABASE_PATH = BASE_DIR / 'inf' / 'bot_data.db'  # shared sqlite db with hosting_panel.py

MUTE_DURATION = timedelta(hours=2)
POLL_SECONDS = 10

# Defensive: if this process starts before hosting_panel.py has created
# the shared 'inf/' folder (race at boot, e.g. both launched by
# supervisor at the same instant), sqlite3.connect() fails with
# "unable to open database file" because SQLite won't create missing
# parent directories on its own.
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)


def _conn():
    # WAL mode is a persistent property of the DB file itself, so it
    # doesn't matter which process (this one or hosting_panel.py) sets
    # it first — whichever connects first "wins" and it sticks. Setting
    # it here too means this process is safe to boot before
    # hosting_panel.py has ever touched the DB.
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout = 10000")  # main bot writes to the same file
    return conn


def fetch_pending():
    conn = _conn()
    c = conn.cursor()
    c.execute("SELECT user_id, file_name FROM pending_approvals WHERE status='pending'")
    rows = c.fetchall()
    conn.close()
    return rows


def decide(user_id: int, file_name: str, status: str, reason: str):
    conn = _conn()
    c = conn.cursor()
    c.execute(
        "UPDATE pending_approvals SET status=?, decided_at=?, decided_by='bot', reason=? "
        "WHERE user_id=? AND file_name=? AND status='pending'",
        (status, datetime.now().isoformat(), reason, user_id, file_name)
    )
    changed = c.rowcount > 0
    if changed and status == 'rejected':
        mute_until = (datetime.now() + MUTE_DURATION).isoformat()
        c.execute(
            "INSERT INTO muted_users (user_id, mute_until, reason) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET mute_until=excluded.mute_until, reason=excluded.reason",
            (user_id, mute_until, reason)
        )
    conn.commit()
    conn.close()
    return changed


def review_loop_once():
    for user_id, file_name in fetch_pending():
        path = UPLOAD_BOTS_DIR / str(user_id) / file_name
        if not path.exists():
            # File vanished (deleted before review) — clear the queue entry silently.
            decide(user_id, file_name, 'approved', 'file no longer present, skipped')
            continue

        verdict, findings = scan_file(path)

        if verdict == 'clear':
            if decide(user_id, file_name, 'approved', 'no suspicious patterns found'):
                logger.info(f"approved {file_name} for {user_id}")
        else:
            reason = ", ".join(f"{label} ({sev})" for label, sev in findings)
            if decide(user_id, file_name, 'rejected', reason):
                logger.warning(f"rejected {file_name} for {user_id}: {reason}")


async def review_loop():
    while True:
        try:
            review_loop_once()
        except Exception as e:
            logger.error(f"review_loop error: {e}")
        await asyncio.sleep(POLL_SECONDS)


async def main():
    logger.info("Approval bot starting, polling pending_approvals every %ss...", POLL_SECONDS)
    await review_loop()


if __name__ == "__main__":
    asyncio.run(main())
