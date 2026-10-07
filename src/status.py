"""Status snapshot for the driving agent (WorkBuddy).

Machine-readable state so the host can decide when to run what: paused state,
daily quota, last activity. Scheduling logic lives in the host; this just
reports facts.
"""

import json
from datetime import datetime, timezone

from src.config import DATA_DIR, Config
from src.db import get_connection, get_today_comment_count
from src.log import get_logger

log = get_logger("status")


def _last_posted_iso() -> str | None:
    """Timestamp of the most recent recorded comment."""
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT posted_at FROM comments ORDER BY posted_at DESC LIMIT 1"
            ).fetchone()
            return row[0] if row else None
    except Exception:
        return None


def build_status(config: Config) -> dict:
    from src.safety.breaker import get_state

    paused = get_state()
    comments_today = get_today_comment_count()
    limit = config.max_comments_per_day
    quota_left = max(0, limit - comments_today)

    alerts = []
    if paused.paused:
        alerts.append(
            f"PAUSED: {paused.reason} (since {paused.since}) — "
            f"run `reddit-agent resume` after investigating"
        )
    if quota_left == 0:
        alerts.append("daily comment quota reached")
    if config.dry_run:
        alerts.append("DRY_RUN is on — submit commands only log, nothing posts")

    return {
        "paused": paused.paused,
        "paused_reason": paused.reason if paused.paused else None,
        "dry_run": config.dry_run,
        "quota_ok": quota_left > 0,
        "last_comment_at": _last_posted_iso(),
        "today": {
            "comments": comments_today,
            "comment_limit": limit,
            "quota_left": quota_left,
        },
        "alerts": alerts,
    }


def print_status(config: Config) -> None:
    print(json.dumps(build_status(config), indent=2, ensure_ascii=False))
