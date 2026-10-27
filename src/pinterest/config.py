"""Pinterest configuration — env vars + data paths.

All Pinterest state is kept under data/pinterest/ so it never collides
with the Reddit/XHS sides of this plugin (own cookies, own db, own breaker
flag).
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from src.config import DATA_DIR

load_dotenv()

PIN_DATA_DIR = DATA_DIR / "pinterest"
PIN_COOKIES_PATH = PIN_DATA_DIR / "cookies.json"
PIN_DB_PATH = PIN_DATA_DIR / "pinterest.db"
PIN_SCREENSHOTS_DIR = PIN_DATA_DIR / "screenshots"
PIN_PAUSED_FLAG = PIN_DATA_DIR / "paused.flag"

# Cap debug PNGs (same policy as the other platforms).
MAX_SCREENSHOTS = 20

# Pin-creation-tool hard limits.
TITLE_MAX_CHARS = 100
DESC_MAX_CHARS = 500
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}  # GIF goes through video path
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm"}

HOME = "https://www.pinterest.com"
LOGIN_URL = "https://www.pinterest.com/login/"
PIN_BUILDER_URL = "https://www.pinterest.com/pin-creation-tool/"


@dataclass
class PinConfig:
    # Cadence
    max_pins_per_day: int = 5
    min_pin_interval_minutes: int = 30

    # Objective — the user's goal for Pinterest posting (read by the agent)
    objective: str = ""

    # Default board when the caller doesn't pass --board ("" = leave the
    # builder's own default / let the user pick in the window)
    default_board: str = ""

    # Logging
    log_level: str = "INFO"
    screenshot_on_error: bool = True


def load_pin_config() -> PinConfig:
    """Load Pinterest configuration from environment."""
    return PinConfig(
        max_pins_per_day=int(os.environ.get("PIN_MAX_PINS_PER_DAY", "5")),
        min_pin_interval_minutes=int(
            os.environ.get("PIN_MIN_PIN_INTERVAL_MINUTES", "30")
        ),
        objective=os.environ.get("PIN_AGENT_OBJECTIVE", ""),
        default_board=os.environ.get("PIN_DEFAULT_BOARD", ""),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        screenshot_on_error=os.environ.get("SCREENSHOT_ON_ERROR", "true").lower()
        == "true",
    )


def prune_screenshots(keep: int = MAX_SCREENSHOTS) -> None:
    """Delete oldest screenshots beyond the retention cap. Never raises."""
    try:
        pngs = sorted(
            PIN_SCREENSHOTS_DIR.glob("*.png"),
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
