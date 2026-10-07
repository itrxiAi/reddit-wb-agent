"""Reddit DM sending + dedup tracking.

Reading the DM inbox lives in pending.py (fetch-only). This module only sends
DMs with text supplied by the host agent, and records them so the same user
is never DM'd twice.
"""

import asyncio
from datetime import datetime

from src.browser.stealth import human_click, human_delay, human_type
from src.config import Config
from src.db import get_connection
from src.log import get_logger

log = get_logger("dms")


async def send_dm(
    session, config: Config, username: str, subject: str, message: str
) -> bool:
    """Send a DM to a Reddit user.

    Returns True if sent successfully.
    """
    page = session.page

    log.info(f"Sending DM to u/{username}")

    await page.goto(
        f"https://www.reddit.com/message/compose/?to={username}",
        wait_until="domcontentloaded",
    )
    await asyncio.sleep(human_delay(2000, 4000))

    # Dismiss cookie popup
    try:
        for btn in await page.query_selector_all("button"):
            if "Accept All" in (await btn.inner_text()):
                await human_click(page, btn)
                await asyncio.sleep(1)
                break
    except Exception:
        pass

    # Fill subject
    try:
        subject_input = await page.query_selector(
            'input[name="subject"], '
            'textarea[placeholder*="subject"], '
            '[aria-label*="subject"]'
        )
        if subject_input:
            await human_click(page, subject_input)
            await asyncio.sleep(human_delay(300, 600))
            await human_type(page, subject)
    except Exception as e:
        log.warning(f"Could not fill DM subject: {e}")

    await asyncio.sleep(human_delay(500, 1000))

    # Fill message body
    try:
        body_input = await page.query_selector(
            'textarea[name="message"], '
            'div[contenteditable="true"], '
            'textarea[placeholder*="message"]'
        )
        if body_input:
            await human_click(page, body_input)
            await asyncio.sleep(human_delay(300, 600))
            await human_type(page, message)
    except Exception as e:
        log.error(f"Could not fill DM body: {e}")
        return False

    await asyncio.sleep(human_delay(1000, 2000))

    # Click send
    try:
        for btn in await page.query_selector_all("button"):
            txt = (await btn.inner_text()).strip()
            if txt in ("Send", "Send message", "Submit") and await btn.is_visible():
                await human_click(page, btn)
                log.info(f"DM sent to u/{username}")
                _record_dm_sent(username, subject, message)
                return True
    except Exception as e:
        log.error(f"Failed to send DM: {e}")

    return False


# ─── Dedup tracking ────────────────────────────────


def already_replied(username: str) -> bool:
    """Check if we already replied to this user's DM."""
    with get_connection() as conn:
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM dm_log WHERE username = ? AND direction = 'reply'",
                (username,),
            ).fetchone()
            return row[0] > 0
        except Exception:
            # Table might not exist yet
            return False


def already_dmed(username: str) -> bool:
    """Check if we already sent an outbound DM to this user."""
    with get_connection() as conn:
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM dm_log WHERE username = ? AND direction = 'outbound'",
                (username,),
            ).fetchone()
            return row[0] > 0
        except Exception:
            return False


def record_dm_reply(username: str, their_msg: str, our_reply: str) -> None:
    """Record that we replied to a DM."""
    _ensure_dm_table()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO dm_log (username, direction, their_text, our_text, created_at) VALUES (?, ?, ?, ?, ?)",
            (username, "reply", their_msg[:500], our_reply[:500], datetime.utcnow().isoformat()),
        )


def _record_dm_sent(username: str, subject: str, message: str) -> None:
    """Record that we sent a DM."""
    _ensure_dm_table()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO dm_log (username, direction, their_text, our_text, created_at) VALUES (?, ?, ?, ?, ?)",
            (username, "outbound", subject, message[:500], datetime.utcnow().isoformat()),
        )


def _ensure_dm_table() -> None:
    """Create the dm_log table if it doesn't exist."""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dm_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                direction TEXT NOT NULL,
                their_text TEXT,
                our_text TEXT,
                created_at TEXT NOT NULL
            )
        """)
