"""SQLite tracking for published Pinterest pins — separate DB."""

import json
from datetime import datetime

from src.db import get_connection
from src.pinterest.config import PIN_DB_PATH


def init_db() -> None:
    """Create Pinterest tables if they don't exist."""
    PIN_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with get_connection(PIN_DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS pins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,            -- image | video
                title TEXT NOT NULL,
                description TEXT,
                link TEXT,
                board TEXT,
                media TEXT,                    -- JSON array of file paths
                pin_url TEXT,
                posted_at TEXT NOT NULL,
                status TEXT DEFAULT 'published'  -- published, failed
            );

            CREATE INDEX IF NOT EXISTS idx_pins_posted ON pins(posted_at);
        """)
        # migrate: older DBs lack the extra column
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pins)")}
        if "extra" not in cols:
            conn.execute("ALTER TABLE pins ADD COLUMN extra TEXT")


def record_pin(
    kind: str,
    title: str,
    description: str,
    link: str,
    board: str,
    media: list[str],
    pin_url: str = "",
    status: str = "published",
    extra: dict | None = None,
) -> None:
    """Record a published pin for quota/interval/digest accounting."""
    with get_connection(PIN_DB_PATH) as conn:
        conn.execute(
            """INSERT INTO pins
               (kind, title, description, link, board, media, pin_url,
                posted_at, status, extra)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                kind, title, description, link, board,
                json.dumps(media, ensure_ascii=False), pin_url,
                datetime.utcnow().isoformat(), status,
                json.dumps(extra, ensure_ascii=False) if extra else None,
            ),
        )


def get_today_pin_count() -> int:
    """Number of pins published today."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with get_connection(PIN_DB_PATH) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM pins WHERE posted_at >= ? AND status = 'published'",
            (today,),
        ).fetchone()
        return row[0]


def get_last_pin_at() -> str | None:
    """ISO timestamp of the most recent published pin, or None."""
    try:
        with get_connection(PIN_DB_PATH) as conn:
            row = conn.execute(
                "SELECT posted_at FROM pins WHERE status = 'published' "
                "ORDER BY posted_at DESC LIMIT 1"
            ).fetchone()
            return row[0] if row else None
    except Exception:
        return None


def get_daily_summary() -> dict:
    """Today's stats for the digest command."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with get_connection(PIN_DB_PATH) as conn:
        posted = conn.execute(
            "SELECT COUNT(*) FROM pins WHERE posted_at >= ? AND status = 'published'",
            (today,),
        ).fetchone()[0]

        by_kind = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT kind, COUNT(*) FROM pins "
                "WHERE posted_at >= ? AND status = 'published' GROUP BY kind",
                (today,),
            ).fetchall()
        }

        recent = [
            dict(r)
            for r in conn.execute(
                "SELECT kind, title, board, posted_at FROM pins "
                "ORDER BY posted_at DESC LIMIT 5"
            ).fetchall()
        ]

        return {
            "date": today,
            "pins_posted": posted,
            "by_kind": by_kind,
            "recent_pins": recent,
        }
