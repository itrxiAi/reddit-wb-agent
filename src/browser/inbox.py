"""Read Reddit inbox and notifications to detect bans, removals, and mod actions.

This module MUST run before any posting cycle. If the account has
ban notices or mod warnings, the agent must stop posting to those subreddits.
"""

import asyncio
import re
from dataclasses import dataclass

from src.browser.stealth import human_click, human_delay
from src.db import get_connection
from src.log import get_logger

log = get_logger("inbox")


@dataclass
class InboxMessage:
    subject: str
    body: str
    subreddit: str | None
    is_ban: bool
    is_removal: bool
    is_warning: bool


async def check_inbox(session) -> list[InboxMessage]:
    """Read Reddit inbox/notifications and parse for ban/removal notices.

    Returns a list of parsed inbox messages.
    """
    page = session.page

    log.info("Checking inbox for bans and mod messages")

    await page.goto(
        "https://www.reddit.com/message/inbox/", wait_until="domcontentloaded"
    )
    await asyncio.sleep(human_delay(2000, 4000))

    # Dismiss cookie popup
    try:
        btns = await page.query_selector_all("button")
        for btn in btns:
            try:
                txt = await btn.inner_text()
                if "Accept All" in txt:
                    await human_click(page, btn)
                    await asyncio.sleep(1)
                    break
            except Exception:
                continue
    except Exception:
        pass

    # Extract messages
    messages_raw = await page.evaluate("""
        () => {
            const messages = [];
            // Try multiple selectors for Reddit's message layout
            const msgEls = document.querySelectorAll(
                '[data-testid="message"], .message, article, .thing'
            );
            for (const el of msgEls) {
                if (messages.length >= 20) break;
                const subject = el.querySelector(
                    '[data-testid="message-subject"], .subject, h4, h3'
                );
                const body = el.querySelector(
                    '[data-testid="message-body"], .md, p'
                );
                messages.push({
                    subject: subject ? subject.textContent.trim() : '',
                    body: body ? body.textContent.trim().slice(0, 500) : '',
                });
            }

            // Also try getting raw page text as fallback
            if (messages.length === 0) {
                const text = document.body.innerText;
                return [{subject: 'RAW_PAGE', body: text.slice(0, 3000)}];
            }

            return messages;
        }
    """)

    parsed = []
    for msg in messages_raw:
        subject = msg.get("subject", "").lower()
        body = msg.get("body", "").lower()
        full_text = f"{subject} {body}"

        # Detect subreddit from message
        sub_match = re.search(r"r/(\w+)", full_text)
        subreddit = sub_match.group(1) if sub_match else None

        # Detect ban
        is_ban = any(
            phrase in full_text
            for phrase in [
                "you have been permanently banned",
                "you have been temporarily banned",
                "you've been permanently banned",
                "you've been temporarily banned",
                "banned from participating",
                "you are banned",
                "permanently banned",
            ]
        )

        # Detect removal
        is_removal = any(
            phrase in full_text
            for phrase in [
                "has been removed",
                "was removed",
                "your post has been removed",
                "your comment has been removed",
                "removed by moderator",
                "automod",
                "automoderator",
                "low comment karma",
                "low karma",
                "minimum karma",
            ]
        )

        # Detect warning
        is_warning = any(
            phrase in full_text
            for phrase in [
                "warning",
                "rule violation",
                "please review our rules",
                "your account has been flagged",
            ]
        )

        if is_ban or is_removal or is_warning:
            parsed_msg = InboxMessage(
                subject=msg.get("subject", ""),
                body=msg.get("body", "")[:300],
                subreddit=subreddit,
                is_ban=is_ban,
                is_removal=is_removal,
                is_warning=is_warning,
            )
            parsed.append(parsed_msg)

            action = "BAN" if is_ban else "REMOVAL" if is_removal else "WARNING"
            log.warning(
                f"Inbox {action} detected: sub={subreddit} subject='{msg.get('subject', '')[:80]}'"
            )

    log.info(f"Inbox check: {len(parsed)} actionable messages found")
    return parsed


def apply_inbox_actions(messages: list[InboxMessage], config) -> list[str]:
    """Apply inbox findings: disable banned subreddits, log removals.

    Returns list of action descriptions.
    """
    import yaml
    from src.config import DATA_DIR

    actions = []
    subreddits_path = DATA_DIR / "subreddits.yaml"

    banned_subs = set()
    for msg in messages:
        if msg.is_ban and msg.subreddit:
            banned_subs.add(msg.subreddit)
            actions.append(f"BANNED from r/{msg.subreddit} — disabled posting")
            log.warning(f"Account BANNED from r/{msg.subreddit}")

        if msg.is_removal and msg.subreddit:
            reason = msg.body.strip()[:200] or "removed (no reason given)"
            actions.append(
                f"Post removed in r/{msg.subreddit}: {reason[:100]}"
            )
            log.warning(f"Removal in r/{msg.subreddit}: {reason}")

    # Disable banned subreddits in the config
    if banned_subs and subreddits_path.exists():
        with open(subreddits_path) as f:
            data = yaml.safe_load(f)

        changed = False
        for sub in data.get("subreddits", []):
            if sub["name"] in banned_subs:
                sub["min_karma"] = 99999
                sub["notes"] = f"BANNED — account banned from this subreddit"
                changed = True
                log.warning(f"Disabled r/{sub['name']} in config (banned)")

        if changed:
            with open(subreddits_path, "w") as f:
                yaml.dump(data, f, default_flow_style=False)

    return actions
