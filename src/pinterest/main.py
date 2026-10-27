"""pinterest-agent — thin CLI actuator for publishing Pinterest pins.

The host agent (WorkBuddy) is the brain: it writes the pin — title,
description, link, board — and decides what to post. This tool is the
hands: it opens pinterest.com/pin-creation-tool/ in a stealth browser,
uploads media, fills the form, and clicks Publish.

Commands (read-only unless noted):

    login-check                    Is the browser session logged in?
    login                          Open Chrome; human signs in; cookies saved
    publish image --title T --description D | --desc-file F
                   --image f [--link URL] [--board B]
    publish video --title T --description D | --desc-file F
                   --video f [--link URL] [--board B] [--upload-timeout S]
    pins [--limit N]               Recent pins from the local ledger
    status                         Paused/quota snapshot → JSON
    digest                         Today's stats → JSON
    pause / resume                 Trip / clear the circuit breaker

Hard gates enforced here (the host agent does NOT police these):
  - circuit breaker: publish refuses while paused
  - daily quota: publish refuses past PIN_MAX_PINS_PER_DAY
  - min interval: publish refuses inside PIN_MIN_PIN_INTERVAL_MINUTES
  - platform limits: title ≤ 100 chars, description ≤ 500 chars,
    exactly one media file
"""

import argparse
import asyncio
import json
import math
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from src.log import get_logger, setup_logging
from src.pinterest.config import (
    DESC_MAX_CHARS,
    IMAGE_EXTS,
    PIN_PAUSED_FLAG,
    TITLE_MAX_CHARS,
    VIDEO_EXTS,
    PinConfig,
    load_pin_config,
)
from src.pinterest.db import (
    get_daily_summary,
    get_last_pin_at,
    get_today_pin_count,
    init_db,
    record_pin,
)
from src.pinterest.session import (
    LoginRequiredError,
    PinSession,
    VerificationRequiredError,
)

log = get_logger("pinterest.main")


# ─── helpers ──────────────────────────────────────


def _emit(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _check_gates(config: PinConfig) -> dict | None:
    """Hard gates before any publish. Returns an error dict, or None."""
    from src.safety.breaker import get_state

    state = get_state(PIN_PAUSED_FLAG)
    if state.paused:
        return {"success": False, "error": "paused",
                "reason": state.reason, "since": state.since}

    posted = get_today_pin_count()
    if posted >= config.max_pins_per_day:
        return {"success": False, "error": "daily_quota_reached",
                "today": posted, "limit": config.max_pins_per_day}

    last = get_last_pin_at()
    if last:
        try:
            elapsed_min = (
                datetime.utcnow() - datetime.fromisoformat(last)
            ).total_seconds() / 60
            if elapsed_min < config.min_pin_interval_minutes:
                return {
                    "success": False, "error": "too_soon",
                    "last_pin_at": last,
                    "wait_minutes": math.ceil(
                        config.min_pin_interval_minutes - elapsed_min
                    ),
                }
        except ValueError:
            pass
    return None


def _read_desc(args) -> str:
    """Description text from --description or --desc-file."""
    if getattr(args, "desc_file", None):
        return Path(args.desc_file).read_text(encoding="utf-8")
    return getattr(args, "description", "") or ""


def _validate_pin(title: str, desc: str, media: str,
                  exts: set[str]) -> list[str]:
    """Local, pre-browser validation. Returns a list of error strings."""
    errors = []
    if not title.strip():
        errors.append("title_required")
    elif len(title) > TITLE_MAX_CHARS:
        errors.append(f"title_too_long:{len(title)}>{TITLE_MAX_CHARS}")
    if len(desc) > DESC_MAX_CHARS:
        errors.append(f"desc_too_long:{len(desc)}>{DESC_MAX_CHARS}")

    p = Path(media)
    if not p.exists():
        errors.append(f"file_not_found:{media}")
    elif p.suffix.lower() not in exts:
        errors.append(
            f"unsupported_file_type:{p.suffix or media} "
            f"(allowed: {sorted(exts)})"
        )
    return errors


async def _with_session(config: PinConfig, fn):
    """Run fn(session) inside a browser session, always closing it."""
    session = await PinSession(config).start()
    try:
        return await fn(session)
    finally:
        await session.close()


# ─── commands ─────────────────────────────────────


async def cmd_login_check(config: PinConfig) -> None:
    try:
        async def go(session):
            return {"logged_in": bool(await session.is_healthy())}
        _emit(await _with_session(config, go))
    except LoginRequiredError:
        _emit({"logged_in": False})


async def cmd_login(config: PinConfig, timeout_s: int = 300) -> None:
    """Open Chrome on the Pinterest login page; wait for a HUMAN login.

    Pinterest auth is email/password or OAuth — only a human can do it.
    Once the `_pinterest_sess` cookie appears, cookies are saved and
    later commands restore the session.
    """
    from src.pinterest.config import LOGIN_URL

    session = await PinSession(config).start(require_login=False)
    try:
        if await session.is_healthy():
            _emit({"logged_in": True, "cookies_saved": True,
                   "note": "already logged in"})
            return
        await session.page.goto(LOGIN_URL, wait_until="domcontentloaded")
        print("Log in manually in the Chrome window — "
              "I'll detect it and save cookies. (5 min timeout)",
              file=sys.stderr)

        deadline = time.time() + timeout_s
        logged_in = False
        while time.time() < deadline:
            cookies = await session.context.cookies(
                "https://www.pinterest.com"
            )
            # _pinterest_sess 未登录也会发；_auth=="1" 才是真已登录
            if any(
                c["name"] == "_auth" and c.get("value") == "1"
                for c in cookies
            ):
                logged_in = True
                break
            if not ("/login" in session.page.url or
                    "pinterest.com" in session.page.url):
                if await session.is_healthy():
                    logged_in = True
                    break
            await asyncio.sleep(3)

        if logged_in:
            await session._save_cookies()
            _emit({"logged_in": True, "cookies_saved": True})
        else:
            _emit({"logged_in": False,
                   "error": "timeout waiting for login"})
    finally:
        await session.close()


def _cmd_status(config: PinConfig) -> None:
    from src.safety.breaker import get_state

    paused = get_state(PIN_PAUSED_FLAG)
    pins_today = get_today_pin_count()
    limit = config.max_pins_per_day
    quota_left = max(0, limit - pins_today)

    alerts = []
    if paused.paused:
        alerts.append(
            f"PAUSED: {paused.reason} (since {paused.since}) — "
            f"run `pinterest-agent resume` after investigating"
        )
    if quota_left == 0:
        alerts.append("daily pin quota reached")

    _emit({
        "paused": paused.paused,
        "paused_reason": paused.reason if paused.paused else None,
        "quota_ok": quota_left > 0,
        "last_pin_at": get_last_pin_at(),
        "today": {
            "pins": pins_today,
            "pin_limit": limit,
            "quota_left": quota_left,
        },
        "alerts": alerts,
    })


def _cmd_pins(config: PinConfig, limit: int) -> None:
    """Recent pins from the local ledger (published by this tool)."""
    from src.db import get_connection
    from src.pinterest.config import PIN_DB_PATH

    with get_connection(PIN_DB_PATH) as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT kind, title, board, pin_url, posted_at, status, "
                "extra FROM pins ORDER BY posted_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        ]
    _emit({"pins": rows})


async def _cmd_publish(config: PinConfig, args, kind: str) -> None:
    title = args.title.strip()
    desc = _read_desc(args)
    link = (args.link or "").strip()
    board = (args.board or config.default_board or "").strip()
    media_path = str(Path(
        args.image if kind == "image" else args.video
    ).expanduser().resolve())
    tags = [t.strip()
            for t in re.split(r"[,，;；]", args.tags or "")
            if t.strip()]
    product = (args.product or "").strip()
    ai_modified = bool(getattr(args, "ai_modified", False))
    ai_people = bool(getattr(args, "ai_people", False))

    exts = IMAGE_EXTS if kind == "image" else VIDEO_EXTS
    errors = _validate_pin(title, desc, media_path, exts)
    if errors:
        _emit({"success": False, "errors": errors})
        return
    gate = _check_gates(config)
    if gate:
        _emit(gate)
        return

    from src.pinterest.publish import publish_pin

    async def go(session):
        return await publish_pin(
            session, kind, title, desc, link, board, media_path,
            tags=tags,
            alt_text=args.alt_text,
            comments=args.comments,
            similar_products=args.similar_products,
            ai_modified=ai_modified,
            ai_people=ai_people,
            product=product,
            upload_timeout_s=getattr(args, "upload_timeout", 120),
            auto_click=args.auto,
            confirm_timeout_s=args.confirm_timeout,
        )
    result = await _with_session(config, go)

    if result.get("success"):
        record_pin(
            kind=kind, title=title, description=desc, link=link,
            board=board, media=[media_path],
            pin_url=result.get("url", ""),
            extra={
                "tags": tags,
                "alt_text": result.get("check", {}).get("alt_text"),
                "product": product,
                "ai_modified": ai_people or ai_modified,
            },
        )
    _emit(result)


# ─── entry point ──────────────────────────────────


async def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pinterest-agent",
        description="Pinterest pin-creation-tool actuator for WorkBuddy",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login-check", help="Is the session logged in?")
    sub.add_parser(
        "login",
        help="Open Chrome and wait for a manual login, then save cookies",
    )
    sub.add_parser("status", help="Paused/quota snapshot (JSON)")
    sub.add_parser("digest", help="Today's stats (JSON)")
    sub.add_parser("pause", help="Trip the circuit breaker (stop all posting)")
    sub.add_parser("resume", help="Clear the circuit breaker")

    p_pins = sub.add_parser("pins", help="Recent pins from the local ledger")
    p_pins.add_argument("--limit", type=int, default=20)

    p_pub = sub.add_parser("publish", help="Publish a pin supplied by the agent")
    pub_sub = p_pub.add_subparsers(dest="kind", required=True)

    def _pin_args(p):
        p.add_argument("--title", required=True, help="Pin title, ≤100 chars")
        p.add_argument("--description", default="",
                       help="Pin description, ≤500 chars")
        p.add_argument("--desc-file", default="",
                       help="Read description from a file "
                            "(overrides --description)")
        p.add_argument("--link", default="",
                       help="Destination link (optional)")
        p.add_argument("--board", default="",
                       help="Board name to save to (default: PIN_DEFAULT_BOARD)")
        p.add_argument("--tags", default="",
                       help="Tagged topics, comma-separated (interest "
                            "tags — invisible to users, feeds the "
                            "topic graph)")
        p.add_argument("--alt-text", default=None,
                       help="Image alt text (default: the title; "
                            "pass --alt-text '' to leave empty)")
        p.add_argument("--comments",
                       action=argparse.BooleanOptionalAction,
                       default=True,
                       help="Allow comments (default: on; "
                            "--no-comments to disable)")
        p.add_argument("--similar-products",
                       action=argparse.BooleanOptionalAction,
                       default=True,
                       help="Show similar products via visual search "
                            "(default: on; --no-similar-products "
                            "to disable)")
        p.add_argument("--ai-modified", action="store_true",
                       help="Check the AI-modified disclosure")
        p.add_argument("--ai-people", action="store_true",
                       help="Also mark 'contains AI-generated people' "
                            "(implies --ai-modified)")
        p.add_argument("--product", default="",
                       help="Tag a product: product-pin URL or a search "
                            "term — the first picker result is used")
        p.add_argument("--auto", action="store_true",
                       help="Attempt the Publish click automatically "
                            "(default: fill the form and wait for a human "
                            "to click Publish in the Chrome window)")
        p.add_argument("--confirm-timeout", type=int, default=900,
                       help="Seconds to wait for the manual Publish click")

    p_img = pub_sub.add_parser("image", help="Publish an image pin")
    _pin_args(p_img)
    p_img.add_argument("--image", required=True,
                       help="Image file (jpg/png/webp)")

    p_vid = pub_sub.add_parser("video", help="Publish a video pin")
    _pin_args(p_vid)
    p_vid.add_argument("--video", required=True,
                       help="Video file (mp4/mov/webm)")
    p_vid.add_argument("--upload-timeout", type=int, default=600,
                       help="Seconds to wait for video upload/transcode")

    args = parser.parse_args()
    config = load_pin_config()
    init_db()
    setup_logging(config.log_level)

    try:
        if args.command == "status":
            _cmd_status(config)
        elif args.command == "digest":
            _emit(get_daily_summary())
        elif args.command == "pause":
            from src.safety.breaker import trip
            trip("manual pause", flag=PIN_PAUSED_FLAG)
            print("Paused. Use `pinterest-agent resume` to clear.")
        elif args.command == "resume":
            from src.safety.breaker import clear, get_state
            state = get_state(PIN_PAUSED_FLAG)
            print(f"Resumed. (was paused: {state.reason})"
                  if clear(PIN_PAUSED_FLAG)
                  else "Not paused — nothing to resume.")
        elif args.command == "login-check":
            await cmd_login_check(config)
        elif args.command == "login":
            await cmd_login(config)
        elif args.command == "pins":
            _cmd_pins(config, args.limit)
        elif args.command == "publish":
            await _cmd_publish(config, args, args.kind)
        else:
            parser.print_help()
            sys.exit(1)
    except LoginRequiredError as e:
        _emit({"success": False, "error": "login_required", "detail": str(e)})
    except VerificationRequiredError as e:
        _emit({"success": False, "error": "verification_required",
               "detail": str(e)})


def cli() -> None:
    """Entry point for the `pinterest-agent` command."""
    asyncio.run(main())


if __name__ == "__main__":
    cli()
