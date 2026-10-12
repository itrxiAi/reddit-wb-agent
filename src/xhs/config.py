"""XHS (小红书) configuration — env vars + data paths.

All XHS state is kept under data/xhs/ so it never collides with the
Reddit side of this plugin (own cookies, own db, own breaker flag).
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from src.config import DATA_DIR

load_dotenv()

XHS_DATA_DIR = DATA_DIR / "xhs"
XHS_COOKIES_PATH = XHS_DATA_DIR / "cookies.json"
XHS_DB_PATH = XHS_DATA_DIR / "xhs.db"
XHS_SCREENSHOTS_DIR = XHS_DATA_DIR / "screenshots"
XHS_PAUSED_FLAG = XHS_DATA_DIR / "paused.flag"

# Cap debug PNGs (same policy as the reddit side).
MAX_SCREENSHOTS = 20

# Creator-platform hard limits.
TITLE_MAX_CHARS = 20
CONTENT_MAX_CHARS = 1000
MAX_IMAGES = 18
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}  # GIF is rejected by XHS
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm"}

CREATOR_HOME = "https://creator.xiaohongshu.com"
LOGIN_URL = "https://creator.xiaohongshu.com/login"
PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish?source=official"
NOTEMANAGE_URL = "https://creator.xiaohongshu.com/creator/notemanage"


@dataclass
class XhsConfig:
    # Cadence
    max_notes_per_day: int = 3
    min_note_interval_minutes: int = 60

    # Objective — the user's goal for XHS posting (read by the agent, not the CLI)
    objective: str = ""

    # Logging
    log_level: str = "INFO"
    screenshot_on_error: bool = True


def load_xhs_config() -> XhsConfig:
    """Load XHS configuration from environment."""
    return XhsConfig(
        max_notes_per_day=int(os.environ.get("XHS_MAX_NOTES_PER_DAY", "3")),
        min_note_interval_minutes=int(
            os.environ.get("XHS_MIN_NOTE_INTERVAL_MINUTES", "60")
        ),
        objective=os.environ.get("XHS_AGENT_OBJECTIVE", ""),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        screenshot_on_error=os.environ.get("SCREENSHOT_ON_ERROR", "true").lower()
        == "true",
    )


def prune_screenshots(keep: int = MAX_SCREENSHOTS) -> None:
    """Delete oldest XHS screenshots beyond the retention cap. Never raises."""
    try:
        pngs = sorted(
            XHS_SCREENSHOTS_DIR.glob("*.png"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in pngs[keep:]:
            try:
                stale.unlink()
            except OSError:
                pass
    except OSError:
        pass
