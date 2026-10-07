"""Configuration loaded from environment variables and YAML files."""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
PROMPTS_DIR = PROJECT_ROOT / "prompts"
SCREENSHOTS_DIR = DATA_DIR / "screenshots"
DB_PATH = DATA_DIR / "reddit.db"
LEARNINGS_PATH = DATA_DIR / "learnings.md"
SUBREDDIT_REPORTS_DIR = DATA_DIR / "subreddit_reports"

# Research / Opportunity-Discovery mode outputs.
SERVICES_PATH = DATA_DIR / "services.yaml"
OPPORTUNITIES_MD = DATA_DIR / "opportunities.md"
OPPORTUNITIES_JSON = DATA_DIR / "opportunities.json"
OPPORTUNITIES_CSV = DATA_DIR / "opportunities.csv"
RESEARCH_INSIGHTS_PATH = DATA_DIR / "research_insights.md"

# Cap how many debug PNGs we keep so an unattended multi-hour run can't fill
# the disk (the agent saves one on every error). Keep only the newest N.
MAX_SCREENSHOTS = 20


def prune_screenshots(keep: int = MAX_SCREENSHOTS) -> None:
    """Delete oldest screenshots beyond the retention cap.

    Best-effort: never raises into the caller (a screenshot is debug-only and
    must not break the run). Keeps the `keep` most recently modified PNGs.
    """
    try:
        pngs = sorted(
            SCREENSHOTS_DIR.glob("*.png"),
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


@dataclass
class RedditAccount:
    username: str
    password: str


@dataclass
class SubredditConfig:
    name: str
    keywords: list[str]
    max_daily_comments: int
    tone: str
    notes: str = ""
    min_karma: int = 0
    min_account_age_days: int = 0


@dataclass
class Config:
    # Reddit
    reddit_account: RedditAccount

    # Anthropic
    anthropic_api_key: str

    # Slack
    slack_webhook_url: str

    # Cadence
    max_comments_per_day: int
    min_comment_interval_minutes: int
    quality_threshold: int
    cycle_interval_hours: int

    # Objective — the user's goal for Reddit engagement
    objective: str = ""
    # Domain — the topic/field the account builds authority in (drives the
    # three-tier content mix). Empty falls back to brand-relevance only.
    domain: str = ""
    # Rolling window size for tier-ratio enforcement.
    tier_window: int = 20
    # Account-warming phase thresholds (karma). Phase 1 < phase2_min (comments
    # only, no posts/promo); Phase 2 < phase3_min (soft posts); Phase 3 = full.
    phase2_min_karma: int = 50
    phase3_min_karma: int = 200

    # Engagement modes
    engage_comment: bool = True
    engage_upvote: bool = True
    engage_reply: bool = True
    engage_post: bool = False
    engage_browse: bool = True
    engage_join: bool = True
    engage_dm_reply: bool = True
    engage_dm_outreach: bool = False

    # Research / Opportunity-Discovery mode.
    #   "off"          — disabled (default; today's behavior).
    #   "after_quota"  — run engagement first; spend the rest of the cycle (and
    #                    cycles where the daily quota is already used up)
    #                    researching, so a 24/7 agent never idles.
    #   "only"         — pure passive discovery; never comment/upvote/DM.
    # Research is READ-ONLY on Reddit — it only reads and builds a lead list, so
    # it is safe to keep running even while the posting circuit breaker is tripped.
    research_mode: str = "off"
    # How wide each research pass casts: candidate subs + threads it inspects.
    research_max_subreddits: int = 8
    research_max_threads_per_sub: int = 12
    # Only opportunities scoring >= this priority (1-10) are recorded/notified.
    research_min_priority: int = 6
    # Refresh the discovered-subreddit shortlist at most this often.
    research_discovery_interval_hours: int = 24
    # Use the LLM provider's web search to discover new communities (Anthropic).
    research_web_search: bool = False
    # Optional push of opportunities to your platform (e.g. ReachLLM).
    reachllm_opportunities_url: str = ""
    reachllm_api_token: str = ""

    # Subreddits
    subreddits: list[SubredditConfig] = field(default_factory=list)

    # Paths
    ai_marketing_tracker_path: Path | None = None

    # Dry run: generate + log everything but perform NO mutating actions
    # (no posting, upvoting, DMing). Safe way to onboard a new account/objective.
    dry_run: bool = False

    # Logging
    log_level: str = "INFO"
    screenshot_on_error: bool = True


RESEARCH_MODES = ("off", "after_quota", "only")


def _normalize_research_mode(value: str) -> str:
    """Coerce RESEARCH_MODE to a known value, defaulting to 'off' if unknown."""
    mode = (value or "off").strip().lower()
    return mode if mode in RESEARCH_MODES else "off"


def _require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise EnvironmentError(f"Required environment variable {key} is not set")
    return val


def _get_api_key() -> str:
    """Get an LLM API key from environment.

    Checks multiple sources in order:
    1. ANTHROPIC_API_KEY from .env or environment
    2. OPENAI_API_KEY as fallback (for OpenClaw instances using OpenAI)
    3. "agent-provided" placeholder (OpenClaw agent handles LLM calls)
    """
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key and key != "agent-provided":
        return key

    # OpenClaw may have the key in its shell env
    key = os.environ.get("OPENAI_API_KEY", "")
    if key:
        return key

    return ""


def load_subreddits() -> list[SubredditConfig]:
    """Load subreddit configuration from YAML."""
    config_path = DATA_DIR / "subreddits.yaml"
    if not config_path.exists():
        return []

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    return [
        SubredditConfig(
            name=s["name"],
            keywords=s.get("keywords", []),
            max_daily_comments=s.get("max_daily_comments", 2),
            tone=s.get("tone", ""),
            notes=s.get("notes", ""),
            min_karma=s.get("min_karma", 0),
            min_account_age_days=s.get("min_account_age_days", 0),
        )
        for s in raw.get("subreddits", [])
    ]


def load_config() -> Config:
    """Load full configuration from environment and YAML files."""
    tracker_path = os.environ.get("AI_MARKETING_TRACKER_PATH")

    return Config(
        reddit_account=RedditAccount(
            username=_require_env("REDDIT_USERNAME"),
            password=_require_env("REDDIT_PASSWORD"),
        ),
        anthropic_api_key=_get_api_key(),
        slack_webhook_url=os.environ.get("SLACK_WEBHOOK_URL", ""),
        max_comments_per_day=int(os.environ.get("MAX_COMMENTS_PER_DAY", "5")),
        min_comment_interval_minutes=int(
            os.environ.get("MIN_COMMENT_INTERVAL_MINUTES", "20")
        ),
        quality_threshold=int(os.environ.get("QUALITY_THRESHOLD", "7")),
        cycle_interval_hours=int(os.environ.get("CYCLE_INTERVAL_HOURS", "2")),
        objective=os.environ.get("REDDIT_AGENT_OBJECTIVE", ""),
        domain=os.environ.get("REDDIT_AGENT_DOMAIN", ""),
        tier_window=int(os.environ.get("TIER_WINDOW", "20")),
        phase2_min_karma=int(os.environ.get("PHASE2_MIN_KARMA", "50")),
        phase3_min_karma=int(os.environ.get("PHASE3_MIN_KARMA", "200")),
        engage_comment=os.environ.get("ENGAGE_COMMENT", "true").lower() == "true",
        engage_upvote=os.environ.get("ENGAGE_UPVOTE", "true").lower() == "true",
        engage_reply=os.environ.get("ENGAGE_REPLY", "true").lower() == "true",
        engage_post=os.environ.get("ENGAGE_POST", "false").lower() == "true",
        engage_browse=os.environ.get("ENGAGE_BROWSE", "true").lower() == "true",
        engage_join=os.environ.get("ENGAGE_JOIN", "true").lower() == "true",
        engage_dm_reply=os.environ.get("ENGAGE_DM_REPLY", "true").lower() == "true",
        engage_dm_outreach=os.environ.get("ENGAGE_DM_OUTREACH", "false").lower() == "true",
        research_mode=_normalize_research_mode(os.environ.get("RESEARCH_MODE", "off")),
        research_max_subreddits=int(os.environ.get("RESEARCH_MAX_SUBREDDITS", "8")),
        research_max_threads_per_sub=int(
            os.environ.get("RESEARCH_MAX_THREADS_PER_SUB", "12")
        ),
        research_min_priority=int(os.environ.get("RESEARCH_MIN_PRIORITY", "6")),
        research_discovery_interval_hours=int(
            os.environ.get("RESEARCH_DISCOVERY_INTERVAL_HOURS", "24")
        ),
        research_web_search=os.environ.get("RESEARCH_WEB_SEARCH", "false").lower()
        == "true",
        reachllm_opportunities_url=os.environ.get("REACHLLM_OPPORTUNITIES_URL", ""),
        reachllm_api_token=os.environ.get("REACHLLM_API_TOKEN", ""),
        subreddits=load_subreddits(),
        ai_marketing_tracker_path=Path(tracker_path) if tracker_path else None,
        dry_run=os.environ.get("DRY_RUN", "false").lower() == "true",
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        screenshot_on_error=os.environ.get("SCREENSHOT_ON_ERROR", "true").lower()
        == "true",
    )


@dataclass
class Service:
    id: str
    name: str
    solves: list[str]
    signals: list[str]
    pitch: str


@dataclass
class ServiceCatalog:
    company_name: str
    company_url: str
    company_one_liner: str
    services: list[Service]
    audiences: list[str]


def load_service_catalog(path: Path = SERVICES_PATH) -> ServiceCatalog:
    """Load the service catalog used by Research mode to match opportunities.

    Returns an empty catalog (no services) if the file is absent, so research
    code can degrade to objective-only matching instead of crashing.
    """
    if not path.exists():
        return ServiceCatalog("", "", "", [], [])

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    company = raw.get("company", {}) or {}
    services = [
        Service(
            id=s.get("id", s.get("name", "service")),
            name=s.get("name", ""),
            solves=s.get("solves", []) or [],
            signals=s.get("signals", []) or [],
            pitch=(s.get("pitch", "") or "").strip(),
        )
        for s in (raw.get("services", []) or [])
    ]
    return ServiceCatalog(
        company_name=company.get("name", ""),
        company_url=company.get("url", ""),
        company_one_liner=(company.get("one_liner", "") or "").strip(),
        services=services,
        audiences=raw.get("audiences", []) or [],
    )


def load_prompt(name: str, **kwargs: str) -> str:
    """Load a prompt template from prompts/ and substitute variables."""
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt template not found: {path}")

    template = path.read_text()
    for key, value in kwargs.items():
        template = template.replace(f"{{{{{key}}}}}", value)
    return template
