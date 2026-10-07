"""Full Reddit engagement: upvote, reply to replies, create posts.

Goes beyond just commenting — makes the account behave like a real user.
"""

import asyncio
import random

from src.browser.stealth import (
    human_click,
    human_delay,
    human_scroll,
    human_type,
)
from src.config import Config, SubredditConfig
from src.db import get_connection
from src.log import get_logger

log = get_logger("engage")


async def check_ban_status(session, subreddit_name: str) -> dict:
    """Due diligence before acting in a subreddit: am I banned or restricted?

    Visits the subreddit and reads the on-page signals a careful human would:
      - "You're banned from this community" banner  -> banned
      - "you can't comment/post" / quarantine / private gates -> restricted
    Returns a decision dict:
      {"banned": bool, "restricted": bool, "can_comment": bool, "reason": str}
    Fails OPEN-CAUTIOUS: on any error we report not-banned but can_comment=False
    so the caller defaults to the safe path (upvote/browse only), never blind
    commenting.
    """
    page = session.page
    try:
        # The "banned from this community" banner appears on a THREAD/comment
        # page (next to the composer), NOT on the subreddit landing page. So we
        # open the hot feed, grab the first thread, and check there — that's
        # where a human would see the banner when trying to comment.
        await page.goto(
            f"https://www.reddit.com/r/{subreddit_name}/hot/",
            wait_until="domcontentloaded",
        )
        await asyncio.sleep(human_delay(2500, 4500))

        # Private/quarantine gates DO show on the landing/feed page.
        feed_signals = await page.evaluate("""
            () => {
                const text = (document.body.innerText || '').toLowerCase();
                return {
                    quarantined: /quarantined community/.test(text),
                    private: /this community is private|is set to private/.test(text),
                    secblock: /blocked by network security/.test(text),
                    permalink: (document.querySelector('shreddit-post') || {}).getAttribute
                        ? document.querySelector('shreddit-post').getAttribute('permalink') : null,
                };
            }
        """)

        if feed_signals.get("secblock"):
            log.warning(f"r/{subreddit_name}: hit network-security block during ban check")
            return {"banned": False, "restricted": True, "can_comment": False,
                    "reason": "network_security_block"}

        # Navigate into the first thread to read the ban/can't-comment banner.
        permalink = feed_signals.get("permalink")
        if permalink:
            url = permalink if permalink.startswith("http") else "https://www.reddit.com" + permalink
            await page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(human_delay(2500, 4500))
            await human_scroll(page, 600)
            await asyncio.sleep(human_delay(1000, 2000))

        signals = await page.evaluate("""
            () => {
                const text = (document.body.innerText || '').toLowerCase();
                return {
                    banned: /banned from this community|you are banned|you're banned/.test(text),
                    cantComment: /can't comment on posts|cannot comment|posting is restricted|not allowed to post/.test(text),
                    quarantined: %s,
                    private: %s,
                    secblock: /blocked by network security/.test(text),
                };
            }
        """ % (str(bool(feed_signals.get("quarantined"))).lower(),
               str(bool(feed_signals.get("private"))).lower()))

        if signals.get("secblock"):
            # Not a ban — a bot-detection block. Report as "unknown, be safe".
            log.warning(f"r/{subreddit_name}: hit network-security block during ban check")
            return {"banned": False, "restricted": True, "can_comment": False,
                    "reason": "network_security_block"}

        banned = bool(signals.get("banned"))
        restricted = bool(signals.get("cantComment") or signals.get("quarantined")
                          or signals.get("private"))
        can_comment = not banned and not restricted

        if banned:
            log.warning(f"r/{subreddit_name}: ACCOUNT IS BANNED — skip (upvote/browse only elsewhere)")
            reason = "banned"
        elif restricted:
            log.info(f"r/{subreddit_name}: restricted (can't comment) — upvote/browse only")
            reason = "restricted"
        else:
            log.info(f"r/{subreddit_name}: in good standing — commenting allowed")
            reason = "ok"

        return {"banned": banned, "restricted": restricted,
                "can_comment": can_comment, "reason": reason}

    except Exception as e:
        log.warning(f"Ban check failed for r/{subreddit_name}: {e}; defaulting to no-comment")
        return {"banned": False, "restricted": True, "can_comment": False,
                "reason": "check_failed"}


async def join_subreddit(session, subreddit_name: str) -> bool:
    """Join (subscribe to) a subreddit if not already a member.

    Real members behave more naturally and some subs gate posting on membership.
    Idempotent: returns True if joined or already a member, False on failure.
    """
    page = session.page
    try:
        await page.goto(
            f"https://www.reddit.com/r/{subreddit_name}/",
            wait_until="domcontentloaded",
        )
        await asyncio.sleep(human_delay(1500, 3000))

        # If a "Joined" state is already shown, we're a member.
        already = await page.evaluate("""
            () => {
                const els = Array.from(document.querySelectorAll(
                    'button, [role="button"], shreddit-join-button'
                ));
                return els.some(e => /joined|leave/i.test(
                    (e.innerText || '') + ' ' + (e.getAttribute('aria-label') || '')
                ));
            }
        """)
        if already:
            log.info(f"Already a member of r/{subreddit_name}")
            return True

        # Click the Join button (text or aria-label "Join") with the real
        # pointer — a DOM el.click() emits untrusted events with no mouse.
        clicked = False
        for el in await page.query_selector_all(
            'button, [role="button"], shreddit-join-button'
        ):
            try:
                t = " ".join([
                    await el.inner_text() or "",
                    await el.get_attribute("aria-label") or "",
                ]).lower().strip()
                if t == "join" or t.endswith(" join") or t.startswith("join "):
                    await el.scroll_into_view_if_needed()
                    await human_click(page, el)
                    clicked = True
                    break
            except Exception:
                continue
        if clicked:
            await asyncio.sleep(human_delay(1000, 2000))
            log.info(f"Joined r/{subreddit_name}")
            return True
        log.info(f"No Join button found for r/{subreddit_name} (maybe already joined)")
        return False
    except Exception as e:
        log.warning(f"Join failed for r/{subreddit_name}: {e}")
        return False


async def upvote_posts(session, subreddit_name: str, count: int = 3) -> int:
    """Upvote a few posts in a subreddit to look like a real user.

    Returns number of posts upvoted.
    """
    page = session.page
    await page.goto(
        f"https://www.reddit.com/r/{subreddit_name}/hot/",
        wait_until="domcontentloaded",
    )
    await asyncio.sleep(human_delay(2000, 4000))

    # Dismiss cookie popup
    try:
        for btn in await page.query_selector_all("button"):
            if "Accept All" in (await btn.inner_text()):
                await human_click(page, btn)
                await asyncio.sleep(1)
                break
    except Exception:
        pass

    # Our own username — never upvote our own content (a ban signal).
    try:
        my_username = (session.config.reddit_account.username or "").lower()
    except AttributeError:
        my_username = ""

    # Iterate posts (not bare buttons) so we can check authorship per post and
    # skip our own. shreddit-post carries the author on an attribute.
    posts = await page.query_selector_all("shreddit-post")
    indices = list(range(len(posts)))
    random.shuffle(indices)  # don't always hit the top posts

    upvoted = 0
    skipped_own = 0
    for i in indices:
        if upvoted >= count:
            break
        try:
            post = posts[i]
            author = (await post.get_attribute("author") or "").lower()
            if my_username and author == my_username:
                skipped_own += 1
                log.info("Skipped upvoting our own post")
                continue
            btn = await post.query_selector(
                'button[aria-label="upvote"], button[upvote], '
                'button[data-click-id="upvote"]'
            )
            if btn and await btn.is_visible():
                await btn.scroll_into_view_if_needed()
                await asyncio.sleep(human_delay(500, 1500))
                await human_click(page, btn)
                upvoted += 1
                log.info(f"Upvoted post {upvoted}/{count} in r/{subreddit_name}")
                await asyncio.sleep(human_delay(1000, 3000))
        except Exception as e:
            log.warning(f"Upvote failed: {e}")
            continue

    log.info(
        f"Upvoted {upvoted} posts in r/{subreddit_name}"
        + (f" (skipped {skipped_own} own)" if skipped_own else "")
    )
    return upvoted


async def create_post(
    session,
    config: Config,
    subreddit: SubredditConfig,
    title: str,
    body: str,
) -> dict:
    """Create a new text post in a subreddit.

    Returns dict with 'success' and 'url' or 'error'.
    """
    page = session.page

    await page.goto(
        f"https://www.reddit.com/r/{subreddit.name}/submit/",
        wait_until="domcontentloaded",
    )
    await asyncio.sleep(human_delay(2000, 4000))

    # Dismiss cookie popup
    try:
        for btn in await page.query_selector_all("button"):
            if "Accept All" in (await btn.inner_text()):
                await human_click(page, btn)
                await asyncio.sleep(1)
                break
    except Exception:
        pass

    # Fill title
    try:
        title_input = await page.wait_for_selector(
            'textarea[placeholder*="title"], '
            'input[placeholder*="title"], '
            'div[data-testid="post-title"] textarea, '
            '[aria-label*="title"]',
            timeout=10000,
        )
        await human_click(page, title_input)
        await asyncio.sleep(human_delay(300, 600))
        await human_type(page, title)
        log.info(f"Filled title: {title[:50]}...")
    except Exception as e:
        log.error(f"Could not find title input: {e}")
        return {"success": False, "error": "title_input_not_found"}

    await asyncio.sleep(human_delay(500, 1000))

    # Fill body
    try:
        body_input = await page.query_selector(
            'div[contenteditable="true"], '
            'textarea[placeholder*="text"], '
            'div[data-testid="post-body"] div[contenteditable]'
        )
        if body_input:
            await human_click(page, body_input)
            await asyncio.sleep(human_delay(300, 600))
            await human_type(page, body)
            log.info(f"Filled body ({len(body)} chars)")
    except Exception as e:
        log.warning(f"Could not fill body: {e}")

    await asyncio.sleep(human_delay(1000, 2000))

    # Click Post button
    try:
        btns = await page.query_selector_all("button")
        for btn in btns:
            txt = (await btn.inner_text()).strip()
            if txt == "Post" and await btn.is_visible():
                await human_click(page, btn)
                log.info("Clicked Post button")
                break
    except Exception as e:
        log.error(f"Could not find Post button: {e}")
        return {"success": False, "error": "post_button_not_found"}

    await asyncio.sleep(human_delay(3000, 5000))

    # Check if we landed on the new post
    current_url = page.url
    if "/comments/" in current_url:
        log.info(f"Post created: {current_url}")
        return {"success": True, "url": current_url}

    log.error("Post may have failed — didn't redirect to new post")
    return {"success": False, "error": "no_redirect"}


async def browse_subreddit(session, subreddit_name: str) -> None:
    """Just browse a subreddit like a real user. Scroll through posts.

    This adds natural browsing activity to the account's behavior pattern.
    """
    page = session.page
    await page.goto(
        f"https://www.reddit.com/r/{subreddit_name}/",
        wait_until="domcontentloaded",
    )
    await asyncio.sleep(human_delay(2000, 4000))

    # Scroll through a few posts naturally
    for _ in range(random.randint(2, 5)):
        await human_scroll(page, random.randint(500, 1100))
        await asyncio.sleep(human_delay(1500, 4000))

    # Maybe click on a post to read it
    if random.random() < 0.3:
        posts = await page.query_selector_all("shreddit-post")
        if posts:
            post = random.choice(posts[:5])
            try:
                link = await post.query_selector('a[slot="full-post-link"]')
                if link:
                    await human_click(page, link)
                    await asyncio.sleep(human_delay(3000, 8000))
                    # Scroll through the post
                    for _ in range(random.randint(1, 3)):
                        await human_scroll(page, 400)
                        await asyncio.sleep(human_delay(1000, 3000))
            except Exception:
                pass

    log.info(f"Browsed r/{subreddit_name}")
