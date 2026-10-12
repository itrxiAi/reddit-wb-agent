"""SQLite tracking for published XHS notes — separate DB from the reddit one."""

import json
from datetime import datetime

from src.db import get_connection
from src.xhs.config import XHS_DB_PATH


def init_db() -> None:
    """Create XHS tables if they don't exist."""
    XHS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with get_connection(XHS_DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,            -- image | video
                title TEXT NOT NULL,
                content TEXT,
                media TEXT,                    -- JSON array of file paths
                tags TEXT,                     -- JSON array of topic names
                topics_added INTEGER DEFAULT 0,
                note_url TEXT,
                posted_at TEXT NOT NULL,
                status TEXT DEFAULT 'published'  -- published, failed
            );

            CREATE INDEX IF NOT EXISTS idx_notes_posted ON notes(posted_at);
        """)


def record_note(
    kind: str,
    title: str,
    content: str,
    media: list[str],
    tags: list[str],
    topics_added: int = 0,
    note_url: str = "",
    status: str = "published",
) -> None:
    """Record a published note for quota/interval/digest accounting."""
    with get_connection(XHS_DB_PATH) as conn:
        conn.execute(
            """INSERT INTO notes
               (kind, title, content, media, tags, topics_added, note_url,
                posted_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                kind, title, content, json.dumps(media, ensure_ascii=False),
                json.dumps(tags, ensure_ascii=False), topics_added, note_url,
                datetime.utcnow().isoformat(), status,
            ),
        )


def get_today_note_count() -> int:
    """Number of notes published today."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with get_connection(XHS_DB_PATH) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM notes WHERE posted_at >= ? AND status = 'published'",
            (today,),
        ).fetchone()
        return row[0]


def get_last_note_at() -> str | None:
    """ISO timestamp of the most recent published note, or None."""
    try:
        with get_connection(XHS_DB_PATH) as conn:
            row = conn.execute(
                "SELECT posted_at FROM notes WHERE status = 'published' "
                "ORDER BY posted_at DESC LIMIT 1"
            ).fetchone()
            return row[0] if row else None
    except Exception:
        return None


def get_daily_summary() -> dict:
    """Today's stats for the digest command."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with get_connection(XHS_DB_PATH) as conn:
        posted = conn.execute(
            "SELECT COUNT(*) FROM notes WHERE posted_at >= ? AND status = 'published'",
            (today,),
        ).fetchone()[0]

        by_kind = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT kind, COUNT(*) FROM notes "
                "WHERE posted_at >= ? AND status = 'published' GROUP BY kind",
                (today,),
            ).fetchall()
        }

        recent = [
            dict(r)
            for r in conn.execute(
                "SELECT kind, title, topics_added, posted_at FROM notes "
                "ORDER BY posted_at DESC LIMIT 5"
            ).fetchall()
        ]

        return {
            "date": today,
            "notes_posted": posted,
            "by_kind": by_kind,
            "recent_notes": recent,
        }
