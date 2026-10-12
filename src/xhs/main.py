"""xhs-agent — thin CLI actuator for publishing Xiaohongshu (小红书) notes.

The host agent (WorkBuddy) is the brain: it writes the note — title, body,
topics — and decides what to post. This tool is the hands: it opens
creator.xiaohongshu.com in a stealth browser, uploads media, fills the form,
and clicks publish.

Commands (read-only unless noted):

    login-check                    Is the browser session logged in?
    login                          Open Chrome; human scans QR; cookies saved
    publish image --title T --content C | --content-file F
                   --images f1 [f2 ...] [--tags t1 t2 ...]
    publish video --title T --content C | --content-file F
                   --video f [--tags ...] [--upload-timeout S]
    notes [--limit N]              Recent notes from 笔记管理
    status                         Paused/quota snapshot → JSON
    digest                         Today's stats → JSON
    pause / resume                 Trip / clear the circuit breaker

Hard gates enforced here (the host agent does NOT police these):
  - circuit breaker: publish refuses while paused
  - daily quota: publish refuses past XHS_MAX_NOTES_PER_DAY
  - min interval: publish refuses inside XHS_MIN_NOTE_INTERVAL_MINUTES
  - platform limits: title ≤ 20 chars, content ≤ 1000 chars, ≤ 18 images,
    no GIF
"""

import argparse
import asyncio
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path

from src.log import get_logger, setup_logging
from src.xhs.config import (
    CONTENT_MAX_CHARS,
    IMAGE_EXTS,
    MAX_IMAGES,
    TITLE_MAX_CHARS,
    VIDEO_EXTS,
    XHS_PAUSED_FLAG,
    XhsConfig,
    load_xhs_config,
)
from src.xhs.db import (
    get_daily_summary,
    get_last_note_at,
    get_today_note_count,
    init_db,
    record_note,
)
from src.xhs.session import (
    LoginRequiredError,
    VerificationRequiredError,
    XhsSession,
)

log = get_logger("xhs.main")


# ─── helpers ──────────────────────────────────────


def _emit(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _check_gates(config: XhsConfig) -> dict | None:
    """Hard gates before any publish. Returns an error dict, or None."""
    from src.safety.breaker import get_state

    state = get_state(XHS_PAUSED_FLAG)
    if state.paused:
        return {"success": False, "error": "paused",
                "reason": state.reason, "since": state.since}

    posted = get_today_note_count()
    if posted >= config.max_notes_per_day:
        return {"success": False, "error": "daily_quota_reached",
                "today": posted, "limit": config.max_notes_per_day}

    last = get_last_note_at()
    if last:
        try:
            elapsed_min = (
                datetime.utcnow() - datetime.fromisoformat(last)
            ).total_seconds() / 60
            if elapsed_min < config.min_note_interval_minutes:
                return {
                    "success": False, "error": "too_soon",
                    "last_note_at": last,
                    "wait_minutes": math.ceil(
                        config.min_note_interval_minutes - elapsed_min
                    ),
                }
        except ValueError:
            pass
    return None


def _read_content(args) -> str:
    """Body text from --content or --content-file."""
    if getattr(args, "content_file", None):
        return Path(args.content_file).read_text(encoding="utf-8")
    return getattr(args, "content", "") or ""


def _validate_note(title: str, content: str, media: list[str],
                   exts: set[str], max_files: int) -> list[str]:
    """Local, pre-browser validation. Returns a list of error strings."""
    errors = []
    if not title.strip():
        errors.append("title_required")
    elif len(title) > TITLE_MAX_CHARS:
        errors.append(f"title_too_long:{len(title)}>{TITLE_MAX_CHARS}")
    if len(content) > CONTENT_MAX_CHARS:
        errors.append(f"content_too_long:{len(content)}>{CONTENT_MAX_CHARS}")

    if not media:
        errors.append("media_required")
    elif len(media) > max_files:
        errors.append(f"too_many_files:{len(media)}>{max_files}")
    else:
        for m in media:
            p = Path(m)
            if not p.exists():
                errors.append(f"file_not_found:{m}")
            elif p.suffix.lower() not in exts:
                errors.append(
                    f"unsupported_file_type:{p.suffix or m} "
                    f"(allowed: {sorted(exts)})"
                )
    return errors


async def _with_session(config: XhsConfig, fn):
    """Run fn(session) inside a browser session, always closing it."""
    session = await XhsSession(config).start()
    try:
        return await fn(session)
    finally:
        await session.close()


# ─── commands ─────────────────────────────────────


async def cmd_login_check(config: XhsConfig) -> None:
    try:
        async def go(session):
            return {"logged_in": bool(await session.is_healthy())}
        _emit(await _with_session(config, go))
    except LoginRequiredError:
        _emit({"logged_in": False})


async def cmd_login(config: XhsConfig, timeout_s: int = 300) -> None:
    """Open Chrome on the XHS login page and wait for a HUMAN to sign in.

    XHS login is QR-scan / SMS — only a human can do it. Once the web_session
    cookie appears (or the URL leaves /login), cookies are saved and later
    commands restore the session without logging in again.
    """
    from src.xhs.config import LOGIN_URL

    session = await XhsSession(config).start(require_login=False)
    try:
        if await session.is_healthy():
            _emit({"logged_in": True, "cookies_saved": True,
                   "note": "already logged in"})
            return
        await session.page.goto(LOGIN_URL, wait_until="domcontentloaded")
        print("Log in manually in the Chrome window (小红书 App 扫码) — "
              "I'll detect it and save cookies. (5 min timeout)",
              file=sys.stderr)

        deadline = time.time() + timeout_s
        logged_in = False
        while time.time() < deadline:
            cookies = await session.context.cookies(
                "https://www.xiaohongshu.com"
            )
            if any(c["name"] == "web_session" for c in cookies):
                logged_in = True
                break
            if not ("/login" in session.page.url or
                    "xiaohongshu.com" in session.page.url):
                # User navigated away — check if they landed logged-in.
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


def _cmd_status(config: XhsConfig) -> None:
    from src.safety.breaker import get_state

    paused = get_state(XHS_PAUSED_FLAG)
    notes_today = get_today_note_count()
    limit = config.max_notes_per_day
    quota_left = max(0, limit - notes_today)

    alerts = []
    if paused.paused:
        alerts.append(
            f"PAUSED: {paused.reason} (since {paused.since}) — "
            f"run `xhs-agent resume` after investigating"
        )
    if quota_left == 0:
        alerts.append("daily note quota reached")

    _emit({
        "paused": paused.paused,
        "paused_reason": paused.reason if paused.paused else None,
        "quota_ok": quota_left > 0,
        "last_note_at": get_last_note_at(),
        "today": {
            "notes": notes_today,
            "note_limit": limit,
            "quota_left": quota_left,
        },
        "alerts": alerts,
    })


async def cmd_notes(config: XhsConfig, limit: int) -> None:
    from src.xhs.notes import list_notes

    async def go(session):
        return {"notes": await list_notes(session, limit)}
    _emit(await _with_session(config, go))


async def cmd_publish_image(config: XhsConfig, args) -> None:
    title = args.title.strip()
    content = _read_content(args)
    tags = args.tags or []
    images = [str(Path(m).expanduser().resolve()) for m in args.images]

    errors = _validate_note(title, content, images, IMAGE_EXTS, MAX_IMAGES)
    if errors:
        _emit({"success": False, "errors": errors})
        return
    gate = _check_gates(config)
    if gate:
        _emit(gate)
        return

    from src.xhs.publish import publish_image_note

    async def go(session):
        return await publish_image_note(
            session, title, content, images, tags,
            auto_click=args.auto, confirm_timeout_s=args.confirm_timeout,
        )
    result = await _with_session(config, go)

    if result.get("success"):
        record_note(
            kind="image", title=title, content=content, media=images,
            tags=tags, topics_added=result.get("topics_added", 0),
            note_url=result.get("url", ""),
        )
    _emit(result)


async def cmd_publish_video(config: XhsConfig, args) -> None:
    title = args.title.strip()
    content = _read_content(args)
    tags = args.tags or []
    video = str(Path(args.video).expanduser().resolve())

    errors = _validate_note(title, content, [video], VIDEO_EXTS, 1)
    if errors:
        _emit({"success": False, "errors": errors})
        return
    gate = _check_gates(config)
    if gate:
        _emit(gate)
        return

    from src.xhs.publish import publish_video_note

    async def go(session):
        return await publish_video_note(
            session, title, content, video, tags,
            upload_timeout_s=args.upload_timeout,
            auto_click=args.auto, confirm_timeout_s=args.confirm_timeout,
        )
    result = await _with_session(config, go)

    if result.get("success"):
        record_note(
            kind="video", title=title, content=content, media=[video],
            tags=tags, topics_added=result.get("topics_added", 0),
            note_url=result.get("url", ""),
        )
    _emit(result)


# ─── entry point ──────────────────────────────────


async def main() -> None:
    parser = argparse.ArgumentParser(
        prog="xhs-agent",
        description="Xiaohongshu creator-platform actuator for WorkBuddy",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login-check", help="Is the session logged in?")
    sub.add_parser(
        "login",
        help="Open Chrome and wait for a manual (QR) login, then save cookies",
    )
    sub.add_parser("status", help="Paused/quota snapshot (JSON)")
    sub.add_parser("digest", help="Today's stats (JSON)")
    sub.add_parser("pause", help="Trip the circuit breaker (stop all posting)")
    sub.add_parser("resume", help="Clear the circuit breaker")

    p_notes = sub.add_parser("notes", help="List notes from 笔记管理")
    p_notes.add_argument("--limit", type=int, default=20)

    p_pub = sub.add_parser("publish", help="Publish a note supplied by the agent")
    pub_sub = p_pub.add_subparsers(dest="kind", required=True)

    def _note_args(p):
        p.add_argument("--title", required=True, help="标题, ≤20 chars")
        p.add_argument("--content", default="", help="正文, ≤1000 chars")
        p.add_argument("--content-file", default="",
                       help="Read 正文 from a file (overrides --content)")
        p.add_argument("--tags", nargs="*", default=[],
                       help="话题 tags (without #), inserted as real topics")
        p.add_argument("--auto", action="store_true",
                       help="Attempt the publish click automatically "
                            "(default: fill the form and wait for a human "
                            "to click 发布 in the Chrome window)")
        p.add_argument("--confirm-timeout", type=int, default=900,
                       help="Seconds to wait for the manual 发布 click")

    p_img = pub_sub.add_parser("image", help="Publish a 图文 note")
    _note_args(p_img)
    p_img.add_argument("--images", nargs="+", required=True,
                       help="Image files, in note order (≤18, jpg/png/webp)")

    p_vid = pub_sub.add_parser("video", help="Publish a 视频 note")
    _note_args(p_vid)
    p_vid.add_argument("--video", required=True, help="Video file (mp4/mov)")
    p_vid.add_argument("--upload-timeout", type=int, default=600,
                       help="Seconds to wait for video upload/transcode")

    args = parser.parse_args()
    config = load_xhs_config()
    init_db()
    setup_logging(config.log_level)

    try:
        if args.command == "status":
            _cmd_status(config)
        elif args.command == "digest":
            _emit(get_daily_summary())
        elif args.command == "pause":
            from src.safety.breaker import trip
            trip("manual pause", flag=XHS_PAUSED_FLAG)
            print("Paused. Use `xhs-agent resume` to clear.")
        elif args.command == "resume":
            from src.safety.breaker import clear, get_state
            state = get_state(XHS_PAUSED_FLAG)
            print(f"Resumed. (was paused: {state.reason})"
                  if clear(XHS_PAUSED_FLAG)
                  else "Not paused — nothing to resume.")
        elif args.command == "login-check":
            await cmd_login_check(config)
        elif args.command == "login":
            await cmd_login(config)
        elif args.command == "notes":
            await cmd_notes(config, args.limit)
        elif args.command == "publish":
            if args.kind == "image":
                await cmd_publish_image(config, args)
            elif args.kind == "video":
                await cmd_publish_video(config, args)
        else:
            parser.print_help()
            sys.exit(1)
    except LoginRequiredError as e:
        _emit({"success": False, "error": "login_required", "detail": str(e)})
    except VerificationRequiredError as e:
        _emit({"success": False, "error": "verification_required",
               "detail": str(e)})


def cli() -> None:
    """Entry point for the `xhs-agent` command."""
    asyncio.run(main())


if __name__ == "__main__":
    cli()
