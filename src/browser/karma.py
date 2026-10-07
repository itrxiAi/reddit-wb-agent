"""Check account karma and filter subreddits accordingly."""

import asyncio
import re
from datetime import datetime, timezone

from src.log import get_logger

log = get_logger("karma")

_cached_karma: int | None = None
# Account age in days, scraped in the same profile pass as karma.
# None means "unknown" (callers fail closed where age is required).
_cached_age_days: int | None = None


async def get_account_karma(session) -> int:
    """Get the current account's comment karma.

    Caches the result for the duration of the cycle to avoid repeated checks.
    """
    global _cached_karma
    if _cached_karma is not None:
        return _cached_karma

    page = session.page
    try:
        # We already know who we are — it's the login username in config. Use it
        # directly instead of scraping the header (which varies by Reddit layout
        # and was the cause of the karma=0 bug). Fall back to a page scrape only
        # if config somehow lacks it.
        username = ""
        try:
            username = session.config.reddit_account.username
        except AttributeError:
            username = ""

        if not username:
            username = await page.evaluate("""
                () => {
                    const clean = s => (s || '').trim().replace(/^u\\//, '');
                    const el = document.querySelector('[data-testid="username"]');
                    if (el && el.textContent.trim()) return clean(el.textContent);
                    const link = document.querySelector('a[href^="/user/"]');
                    if (link) {
                        const m = link.getAttribute('href').match(/\\/user\\/([^/]+)/);
                        if (m) return clean(m[1]);
                    }
                    return '';
                }
            """)

        if not username:
            log.warning("Could not determine username (not in config or page)")
            _cached_karma = 0
            return 0

        log.info(f"Reading profile for u/{username}")

        await page.goto(
            f"https://www.reddit.com/user/{username}/",
            wait_until="domcontentloaded",
        )
        await asyncio.sleep(2)

        # Extract karma from the profile page
        text = await page.evaluate("document.body.innerText")

        # Parse account age (cake-day) from the same page in one pass.
        _parse_and_cache_age(text)

        # Parse karma. Modern Reddit renders the number and the "Karma" label as
        # separate lines ("5\nKarma" or "Karma\n5"); older/inline shows "5 karma".
        # Try all three (whitespace includes newlines).
        karma = _parse_karma(text)
        if karma is not None:
            log.info(f"Account karma: {karma}")
            _cached_karma = karma
            return karma

        log.warning("Could not parse karma from profile")
        _cached_karma = 0
        return 0

    except Exception as e:
        log.error(f"Failed to check karma: {e}")
        _cached_karma = 0
        return 0


def _parse_karma(text: str) -> int | None:
    """Parse total karma from profile text across old and new Reddit layouts.

    Handles: "5 karma" (inline), "5\\nKarma" (number then label), and
    "Karma\\n5" (label then number). Returns None if nothing matches.
    """
    patterns = [
        r"([\d,]+)\s*karma\b",        # "5 karma" / "5\nKarma"
        r"\bkarma\s*([\d,]+)",        # "Karma\n5"
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                continue
    return None


def _parse_and_cache_age(profile_text: str) -> None:
    """Parse account age from profile text. Leaves cache None if unparseable.

    Reddit shows the cake-day in a few forms, e.g.:
      "Cake day: January 15, 2024"
      "Reddit age 2y" / "2y" next to the avatar
    We try the explicit date first (most reliable), then a relative "Ny/Nm".
    None on failure means "unknown" — callers fail closed where age matters.
    """
    global _cached_age_days

    # 1) Explicit cake day date: "Cake day: January 15, 2024"
    m = re.search(
        r"cake\s*day[:\s]+([A-Z][a-z]+ \d{1,2},? \d{4})",
        profile_text,
        re.IGNORECASE,
    )
    if m:
        for fmt in ("%B %d, %Y", "%B %d %Y"):
            try:
                created = datetime.strptime(m.group(1), fmt).replace(
                    tzinfo=timezone.utc
                )
                _cached_age_days = (datetime.now(timezone.utc) - created).days
                log.info(f"Account age: {_cached_age_days}d (cake day)")
                return
            except ValueError:
                continue

    # 2) Relative age, ONLY when anchored to an age label (so we don't grab a
    #    post timestamp like "5 months ago" and report a wrong age). Modern
    #    Reddit shows "3 m" / "Reddit Age" with the number BEFORE the label and
    #    single-letter units (m=months, y=years). Match both orders.
    label = r"(?:reddit\s*age|cake\s*day|account\s*age)"
    unit = r"(y|yr|year|mo|month|m|d|day)"
    # number-before-label: "3 m\nReddit Age"
    m = re.search(rf"(\d+)\s*{unit}s?\b[^\n]{{0,4}}\n?\s*{label}", profile_text, re.IGNORECASE)
    # label-before-number: "Reddit Age\n3 m" or "Reddit age 2y"
    if not m:
        m = re.search(rf"{label}[^\d]{{0,20}}(\d+)\s*{unit}s?\b", profile_text, re.IGNORECASE)
    if m:
        n = int(m.group(1))
        u = m.group(2).lower()
        if u.startswith("y"):
            _cached_age_days = n * 365
        elif u.startswith("d"):
            _cached_age_days = n
        else:  # m / mo / month
            _cached_age_days = n * 30
        log.info(f"Account age: ~{_cached_age_days}d (relative, anchored)")
        return

    log.warning("Could not parse account age; treating as unknown (fail closed)")
    _cached_age_days = None


async def get_account_age_days(session) -> int | None:
    """Get cached account age in days. None means unknown (fail closed).

    Relies on get_account_karma having run first in the cycle (same page pass).
    """
    return _cached_age_days


def reset_karma_cache() -> None:
    """Reset the karma + age cache (call at start of each cycle)."""
    global _cached_karma, _cached_age_days
    _cached_karma = None
    _cached_age_days = None


def can_post_to_subreddit(karma: int, min_karma: int) -> bool:
    """Check if the account has enough karma for a subreddit."""
    return karma >= min_karma
