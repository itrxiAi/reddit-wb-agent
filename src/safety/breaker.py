"""Global circuit breaker: pause ALL activity when something goes wrong.

Today the agent disables a single subreddit on a ban but keeps going elsewhere.
This adds a global kill switch: on a shadowban, a removal-rate spike, or a high
compliance-block rate, write a PAUSED flag that blocks the whole cycle until a
human clears it with `reddit-agent --resume`.

State lives in a flag file (DATA_DIR/paused.flag) holding the reason + timestamp,
so it survives process restarts and is visible to the status command.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from src.config import DATA_DIR
from src.log import get_logger

log = get_logger("breaker")

PAUSED_FLAG = DATA_DIR / "paused.flag"

# Minimum number of checked actions before the breaker is allowed to trip, so a
# single bad result on day 1 doesn't pause everything.
MIN_SAMPLE = 5
# Removal rate (removed / checked) above this trips the breaker.
REMOVAL_RATE_THRESHOLD = 0.5
# Compliance-block rate (blocks / attempts) above this trips the breaker.
GATE_BLOCK_RATE_THRESHOLD = 0.7


@dataclass
class PausedState:
    paused: bool
    reason: str = ""
    since: str = ""


def is_paused() -> bool:
    """True if the global breaker is currently tripped."""
    return PAUSED_FLAG.exists()


def get_state() -> PausedState:
    """Read the current paused state (reason + timestamp)."""
    if not PAUSED_FLAG.exists():
        return PausedState(paused=False)
    try:
        data = json.loads(PAUSED_FLAG.read_text())
        return PausedState(paused=True, reason=data.get("reason", ""),
                           since=data.get("since", ""))
    except Exception:
        return PausedState(paused=True, reason="unknown", since="")


def trip(reason: str) -> None:
    """Trip the breaker: write the PAUSED flag with a reason."""
    PAUSED_FLAG.parent.mkdir(parents=True, exist_ok=True)
    PAUSED_FLAG.write_text(json.dumps({
        "reason": reason,
        "since": datetime.now(timezone.utc).isoformat(),
    }))
    log.warning(f"CIRCUIT BREAKER TRIPPED — all posting paused: {reason}")


def clear() -> bool:
    """Clear the breaker (the `reddit-agent --resume` action).

    Returns True if a flag was cleared, False if nothing was paused.
    """
    if PAUSED_FLAG.exists():
        PAUSED_FLAG.unlink()
        log.info("Circuit breaker cleared — posting resumed")
        return True
    return False


def evaluate_feedback(feedback: dict, results: dict) -> str | None:
    """Decide whether the breaker should trip given a cycle's signals.

    Returns a reason string if it should trip, else None. Honors the min-sample
    guard so noisy early cycles don't pause everything.
    """
    # Shadowban is serious, but the detector can produce false positives on big
    # lazy-loaded threads. Require >= 2 invisible comments in a cycle before
    # tripping, so one flaky incognito lookup doesn't pause everything. A real
    # shadowban makes ALL comments vanish, so it still trips reliably.
    if feedback.get("shadowbanned", 0) >= 2:
        return f"shadowban detected ({feedback['shadowbanned']} comments invisible)"

    checked = feedback.get("checked", 0)
    removed = feedback.get("removed", 0)
    if checked >= MIN_SAMPLE and removed / checked > REMOVAL_RATE_THRESHOLD:
        return f"removal rate {removed}/{checked} over {REMOVAL_RATE_THRESHOLD:.0%}"

    # High DETERMINISTIC-block rate means the agent keeps trying to break HARD
    # rules (links, karma, age, banned phrases) — a real danger signal. Fuzzy
    # judge rejections ("doesn't fit this sub's tone") are NOT counted here:
    # a picky sub (e.g. ELI5 wanting real explanations) is a fit problem, not an
    # account-safety problem, and shouldn't trip the global breaker.
    blocks = results.get("deterministic_blocks", 0)
    attempts = blocks + results.get("comments_posted", 0)
    if attempts >= MIN_SAMPLE and blocks / attempts > GATE_BLOCK_RATE_THRESHOLD:
        return f"deterministic block rate {blocks}/{attempts} over {GATE_BLOCK_RATE_THRESHOLD:.0%}"

    return None
