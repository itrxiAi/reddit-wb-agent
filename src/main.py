"""reddit-agent — thin CLI actuator for the WorkBuddy agent.

The host agent (WorkBuddy) is the brain: it reads threads, decides what to
say, and writes the text. This tool is the hands: it opens Reddit in a
stealth browser, reads pages, and submits text it is given.

Commands (all read-only unless noted):

    login-check                      Is the browser session logged in?
    inbox                            Ban/removal/mod notices → JSON
    karma                            Account karma → JSON
    scan --sub NAME [--limit N]      Feed of a subreddit → JSON threads
    thread --url URL [--comments N]  Full thread + comments → JSON
    pending                          Replies to our comments + new DMs → JSON
    submit comment --thread-url URL --text "..."
    submit post --sub NAME --title "..." --body "..."
    submit dm --to USER --subject "..." --text "..."
    status                           Paused/quota snapshot → JSON
    digest                           Today's stats → JSON
    pause / resume                   Trip / clear the circuit breaker

Safety rails enforced here (the host agent does NOT police these):
  - circuit breaker: submit refuses while paused
  - daily quota: submit refuses past MAX_COMMENTS_PER_DAY
"""

import argparse
import asyncio
import json
import re
import sys

from src.config import Config, SubredditConfig, load_config, load_subreddits
from src.db import (
    get_daily_summary,
    get_today_comment_count,
    init_db,
    record_comment,
)
from src.log import get_logger, setup_logging

log = get_logger("main")


# ─── helpers ──────────────────────────────────────


def _emit(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _thread_id_from_url(url: str) -> str:
    m = re.search(r"/comments/(\w+)", url or "")
    return m.group(1) if m else ""


def _sub_for(name: str, config: Config) -> SubredditConfig:
    """Look up a subreddit in subreddits.yaml; fall back to a bare config."""
    for s in config.subreddits:
        if s.name.lower() == name.lower():
            return s
    return SubredditConfig(
        name=name, keywords=[], max_daily_comments=99,
        tone="", notes="", min_karma=0,
    )


def _check_gates(config: Config) -> dict | None:
    """Hard gates before any submit. Returns an error dict, or None if clear."""
    from src.safety.breaker import get_state

    state = get_state()
    if state.paused:
        return {"success": False, "error": "paused",
                "reason": state.reason, "since": state.since}

    posted = get_today_comment_count()
    if posted >= config.max_comments_per_day:
        return {"success": False, "error": "daily_quota_reached",
                "today": posted, "limit": config.max_comments_per_day}
    return None


# ─── commands ─────────────────────────────────────


async def _with_session(config: Config, fn):
    """Run `fn(session)` inside a browser session, always closing it."""
    from src.browser.session import RedditSession

    session = await RedditSession(config).start()
    try:
        return await fn(session)
    finally:
        await session.close()


async def cmd_login_check(config: Config) -> None:
    async def go(session):
        healthy = await session.is_healthy()
        return {"logged_in": bool(healthy)}
    _emit(await _with_session(config, go))


async def cmd_login(config: Config, timeout_s: int = 300) -> None:
    """Open a real Chrome window and wait for a HUMAN to log in.

    Scripted login trips Reddit's network-security block on some IPs. This
    just leaves the browser open on the login page; once the session cookie
    appears (up to `timeout_s`), cookies are saved and every later command
    restores the session without logging in again.
    """
    import time
    from src.browser.session import RedditSession

    session = await RedditSession(config).start(auto_login=False)
    try:
        await session.page.goto(
            "https://www.reddit.com/login", wait_until="domcontentloaded"
        )
        print("Log in manually in the Chrome window — "
              "I'll detect it and save cookies. (5 min timeout)",
              file=sys.stderr)

        deadline = time.time() + timeout_s
        logged_in = False
        while time.time() < deadline:
            cookies = await session._context.cookies("https://www.reddit.com")
            if any(c["name"] == "reddit_session" for c in cookies):
                logged_in = True
                break
            await asyncio.sleep(3)

        if logged_in:
            await session._save_cookies()
            _emit({"logged_in": True, "cookies_saved": True})
        else:
            _emit({"logged_in": False, "error": "timeout waiting for login"})
    finally:
        await session.close()


async def cmd_inbox(config: Config) -> None:
    from src.browser.inbox import apply_inbox_actions, check_inbox

    async def go(session):
        messages = await check_inbox(session)
        actions = apply_inbox_actions(messages, config)
        return {
            "messages": [
                {
                    "subject": m.subject, "body": m.body,
                    "subreddit": m.subreddit,
                    "is_ban": m.is_ban, "is_removal": m.is_removal,
                    "is_warning": m.is_warning,
                }
                for m in messages
            ],
            "actions_taken": actions,
        }
    _emit(await _with_session(config, go))


async def cmd_karma(config: Config) -> None:
    from src.browser.karma import get_account_karma

    async def go(session):
        return {"karma": await get_account_karma(session)}
    _emit(await _with_session(config, go))


async def cmd_scan(config: Config, sub: str, limit: int) -> None:
    from src.scanner.subreddit import scan_subreddit

    async def go(session):
        threads = await scan_subreddit(session, _sub_for(sub, config), limit)
        return {"subreddit": sub, "threads": [
            {
                "id": t.id, "subreddit": t.subreddit, "title": t.title,
                "url": t.url, "score": t.score, "comment_count": t.comment_count,
            }
            for t in threads
        ]}
    _emit(await _with_session(config, go))


async def cmd_thread(config: Config, url: str, max_comments: int) -> None:
    from src.scanner.subreddit import read_thread_details

    async def go(session):
        return await read_thread_details(session, url, max_comments)
    _emit(await _with_session(config, go))


async def cmd_pending(config: Config) -> None:
    from src.browser.pending import collect_comment_replies, collect_dms

    async def go(session):
        replies = await collect_comment_replies(session)
        dms = await collect_dms(session)
        return {"comment_replies": replies, "dms": dms}
    _emit(await _with_session(config, go))


async def cmd_submit_comment(config: Config, thread_url: str, text: str) -> None:
    gate = _check_gates(config)
    if gate:
        _emit(gate)
        return

    from src.browser.actions import post_comment

    async def go(session):
        return await post_comment(session, thread_url, text)

    result = await _with_session(config, go)
    if result.get("success"):
        record_comment(
            comment_id=result.get("comment_id") or "unknown",
            thread_id=_thread_id_from_url(thread_url),
            subreddit="",  # filled from thread if needed
            comment_text=text,
            quality_score=0,
        )
    _emit(result)


async def cmd_submit_post(config: Config, sub: str, title: str, body: str) -> None:
    gate = _check_gates(config)
    if gate:
        _emit(gate)
        return

    from src.browser.engage import create_post

    async def go(session):
        return await create_post(session, config, _sub_for(sub, config),
                                 title, body)
    _emit(await _with_session(config, go))


async def cmd_cycle(config: Config, thread_details: int = 2) -> None:
    """One browser pass over the whole read-only flow.

    inbox + pending + karma + per-sub scan/browse/upvote, then full details
    for the best thread candidates — so the caller can pick and write a
    comment without burning extra browser launches. Comments are still sent
    separately via `submit` so the writing step stays with the agent.
    """
    import random
    from src.browser.session import RedditSession
    from src.browser.inbox import check_inbox, apply_inbox_actions
    from src.browser.pending import collect_comment_replies, collect_dms
    from src.browser.karma import get_account_karma
    from src.browser.engage import upvote_posts
    from src.browser.stealth import human_delay
    from src.scanner.subreddit import scan_subreddit, read_thread_details

    session = await RedditSession(config).start()
    try:
        result: dict = {}

        # 1) inbox — bans/removals are tripwires; report first
        messages = await check_inbox(session)
        actions = apply_inbox_actions(messages, config)
        result["inbox"] = {
            "messages": [
                {
                    "subject": m.subject, "body": m.body,
                    "subreddit": m.subreddit, "is_ban": m.is_ban,
                    "is_removal": m.is_removal, "is_warning": m.is_warning,
                }
                for m in messages
            ],
            "actions_taken": actions,
        }

        # 2) pending — replies/DMs the agent may want to answer first
        result["pending"] = {
            "comment_replies": await collect_comment_replies(session),
            "dms": await collect_dms(session),
        }

        # 3) karma — gates which subs are in scope
        karma = await get_account_karma(session)
        result["karma"] = karma

        # 4) per-sub: scan feed + a couple of organic upvotes
        subs_out = []
        pool = []
        for sub in config.subreddits:
            entry: dict = {"name": sub.name}
            if karma < sub.min_karma:
                entry["skipped"] = f"needs {sub.min_karma} karma, have {karma}"
                subs_out.append(entry)
                continue
            threads = await scan_subreddit(session, sub, limit=10)
            entry["threads"] = [
                {
                    "id": t.id, "title": t.title, "url": t.url,
                    "score": t.score, "comment_count": t.comment_count,
                }
                for t in threads
            ]
            entry["upvoted"] = await upvote_posts(
                session, sub.name, count=random.randint(1, 3)
            )
            subs_out.append(entry)
            pool.extend(threads[:3])
            await asyncio.sleep(human_delay(3000, 6000))

        result["subs"] = subs_out

        # 5) fetch bodies for the most promising threads (fewest comments
        # first — better odds a fresh comment gets seen)
        pool.sort(key=lambda t: t.comment_count or 0)
        candidates = []
        for t in pool[:thread_details]:
            try:
                details = await read_thread_details(session, t.url, 10)
                details["url"] = t.url
                candidates.append(details)
            except Exception as e:
                log.warning(f"Could not read candidate {t.url}: {e}")
            await asyncio.sleep(human_delay(2000, 4000))
        result["candidates"] = candidates

        _emit(result)
    finally:
        await session.close()


async def cmd_submit_dm(config: Config, to: str, subject: str, text: str) -> None:
    gate = _check_gates(config)
    if gate:
        _emit(gate)
        return

    from src.browser.dms import send_dm

    async def go(session):
        ok = await send_dm(session, config, to, subject, text)
        return {"success": ok}
    _emit(await _with_session(config, go))


# ─── entry point ──────────────────────────────────


async def main() -> None:
    parser = argparse.ArgumentParser(
        prog="reddit-agent",
        description="Reddit actuator CLI for WorkBuddy",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login-check", help="Is the session logged in?")
    sub.add_parser(
        "login",
        help="Open Chrome and wait for a manual login, then save cookies",
    )
    p_cycle = sub.add_parser(
        "cycle",
        help="One pass: inbox + pending + karma + scan/browse/upvote all subs",
    )
    p_cycle.add_argument(
        "--threads", type=int, default=2,
        help="how many candidate threads to fetch bodies for",
    )
    sub.add_parser("inbox", help="Ban/removal/mod notices")
    sub.add_parser("karma", help="Account karma")
    sub.add_parser("pending", help="Replies to our comments + new DMs")
    sub.add_parser("status", help="Paused/quota snapshot (JSON)")
    sub.add_parser("digest", help="Today's stats (JSON)")
    sub.add_parser("pause", help="Trip the circuit breaker (stop all posting)")
    sub.add_parser("resume", help="Clear the circuit breaker")

    p_scan = sub.add_parser("scan", help="Feed of a subreddit")
    p_scan.add_argument("--sub", required=True, help="Subreddit name")
    p_scan.add_argument("--limit", type=int, default=15)

    p_thread = sub.add_parser("thread", help="Full thread + comments")
    p_thread.add_argument("--url", required=True, help="Thread URL")
    p_thread.add_argument("--comments", type=int, default=10)

    p_submit = sub.add_parser("submit", help="Post text supplied by the agent")
    submit_sub = p_submit.add_subparsers(dest="kind", required=True)

    p_sc = submit_sub.add_parser("comment", help="Comment on a thread")
    p_sc.add_argument("--thread-url", required=True)
    p_sc.add_argument("--text", required=True)

    p_sp = submit_sub.add_parser("post", help="Create an original post")
    p_sp.add_argument("--sub", required=True)
    p_sp.add_argument("--title", required=True)
    p_sp.add_argument("--body", default="")

    p_sd = submit_sub.add_parser("dm", help="Send a DM")
    p_sd.add_argument("--to", required=True)
    p_sd.add_argument("--subject", required=True)
    p_sd.add_argument("--text", required=True)

    args = parser.parse_args()
    config = load_config()
    init_db()
    setup_logging(config.log_level)

    if args.command == "status":
        from src.status import print_status
        print_status(config)
        return
    if args.command == "digest":
        _emit(get_daily_summary())
        return
    if args.command == "pause":
        from src.safety.breaker import trip
        trip("manual pause")
        print("Paused. Use `reddit-agent resume` to clear.")
        return
    if args.command == "resume":
        from src.safety.breaker import clear, get_state
        state = get_state()
        print(f"Resumed. (was paused: {state.reason})" if clear()
              else "Not paused — nothing to resume.")
        return

    if args.command == "login-check":
        await cmd_login_check(config)
    elif args.command == "login":
        await cmd_login(config)
    elif args.command == "cycle":
        await cmd_cycle(config, args.threads)
    elif args.command == "inbox":
        await cmd_inbox(config)
    elif args.command == "karma":
        await cmd_karma(config)
    elif args.command == "scan":
        await cmd_scan(config, args.sub, args.limit)
    elif args.command == "thread":
        await cmd_thread(config, args.url, args.comments)
    elif args.command == "pending":
        await cmd_pending(config)
    elif args.command == "submit":
        if args.kind == "comment":
            await cmd_submit_comment(config, args.thread_url, args.text)
        elif args.kind == "post":
            await cmd_submit_post(config, args.sub, args.title, args.body)
        elif args.kind == "dm":
            await cmd_submit_dm(config, args.to, args.subject, args.text)
    else:
        parser.print_help()
        sys.exit(1)


def cli() -> None:
    """Entry point for the `reddit-agent` command."""
    asyncio.run(main())


if __name__ == "__main__":
    cli()
