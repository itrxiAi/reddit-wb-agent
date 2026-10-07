"""Browser actions for Reddit interaction.

Navigate, extract content, post comments, and verify results.
"""

import asyncio
import random
import re

from playwright.async_api import Page

from src.browser.stealth import (
    human_click,
    human_delay,
    human_scroll,
    human_type,
)
from src.config import SCREENSHOTS_DIR, prune_screenshots
from src.log import get_logger

log = get_logger("actions")


class SearchBlockedError(Exception):
    """Reddit returned its network-security 403 wall for a search request.

    Search endpoints are reliably blocked for automation; feeds/threads are not.
    Callers catch this to stop retrying search and fall back to feed scanning.
    """


async def extract_feed_posts(
    session,  # RedditSession
    subreddit_name: str,
    limit: int = 15,
) -> list[dict]:
    """Browse a subreddit's hot feed and extract posts.

    Uses the reliable shreddit-post elements with their attributes.
    Returns list of dicts with: id, title, url, score, comment_count.
    """
    page = session.page
    url = f"https://www.reddit.com/r/{subreddit_name}/hot/"
    # navigate() recovers from Reddit's transient 403 rate-block with backoff.
    await session.navigate(url)

    # Scroll to load more posts
    for _ in range(3):
        await human_scroll(page)
        await asyncio.sleep(human_delay(800, 1500))

    results = await page.evaluate(f"""
        () => {{
            const posts = [];
            const els = document.querySelectorAll('shreddit-post');
            for (const el of els) {{
                if (posts.length >= {limit}) break;
                const id = el.getAttribute('id') || el.getAttribute('thingid') || '';
                const title = el.getAttribute('post-title') || '';
                const permalink = el.getAttribute('permalink') || '';
                const score = parseInt(el.getAttribute('score') || '0') || 0;
                const commentCount = parseInt(el.getAttribute('comment-count') || '0') || 0;

                let url = permalink;
                if (url && !url.startsWith('http')) url = 'https://www.reddit.com' + url;

                if (title && (id || url)) {{
                    posts.push({{
                        id: id.replace('t3_', ''),
                        title,
                        url,
                        score,
                        comment_count: commentCount
                    }});
                }}
            }}
            return posts;
        }}
    """)

    log.info(f"Extracted {len(results)} posts from r/{subreddit_name} feed")
    return results


async def extract_search_results(
    session,  # RedditSession
    search_url: str,
    limit: int = 15,
) -> list[dict]:
    """Navigate to a Reddit search URL and extract thread results.

    Returns a list of dicts with: id, title, url, score, comment_count.
    """
    from src.browser.session import NetworkBlockedError

    page = session.page
    # Reddit hard-blocks automated access to ALL search endpoints (www, old,
    # .json, in-subreddit) with a 403 "You've been blocked by network security"
    # wall — confirmed live 2026-06-04. Feeds and threads are NOT blocked.
    # navigate() retries with backoff; a persistent block on search means the
    # endpoint is policy-blocked, so surface SearchBlockedError to fall back to
    # feed scanning rather than wasting more retries on every query.
    try:
        await session.navigate(search_url)
    except NetworkBlockedError as e:
        raise SearchBlockedError(
            f"Reddit blocked search ({e}); falling back to feeds"
        ) from e

    # Scroll down to load more results
    for _ in range(2):
        await human_scroll(page)
        await asyncio.sleep(human_delay(800, 1500))

    results = await page.evaluate(f"""
        () => {{
            const posts = [];

            // Primary: shreddit-post elements (modern Reddit)
            const shredditPosts = document.querySelectorAll('shreddit-post');
            for (const el of shredditPosts) {{
                if (posts.length >= {limit}) break;

                const id = el.getAttribute('id') || el.getAttribute('thingid') || '';
                const title = el.getAttribute('post-title') || '';
                const permalink = el.getAttribute('permalink') || '';
                const score = parseInt(el.getAttribute('score') || '0') || 0;
                const commentCount = parseInt(el.getAttribute('comment-count') || '0') || 0;

                let url = permalink;
                if (url && !url.startsWith('http')) {{
                    url = 'https://www.reddit.com' + url;
                }}

                if (title && (id || url)) {{
                    posts.push({{
                        id: id.replace('t3_', ''),
                        title,
                        url,
                        score,
                        comment_count: commentCount
                    }});
                }}
            }}

            // Fallback: older Reddit layouts
            if (posts.length === 0) {{
                const oldPosts = document.querySelectorAll('div[data-testid="post-container"], div.Post, article');
                for (const el of oldPosts) {{
                    if (posts.length >= {limit}) break;
                    const titleEl = el.querySelector('h3, [slot="title"]');
                    const title = titleEl ? titleEl.textContent.trim() : '';
                    const linkEl = el.querySelector('a[href*="comments"]');
                    let url = linkEl ? linkEl.getAttribute('href') : '';
                    if (url && !url.startsWith('http')) url = 'https://www.reddit.com' + url;
                    const id = el.getAttribute('id') || el.getAttribute('data-fullname') || '';
                    if (title) {{
                        posts.push({{ id: id.replace('t3_', ''), title, url, score: 0, comment_count: 0 }});
                    }}
                }}
            }}

            return posts;
        }}
    """)

    log.info(f"Extracted {len(results)} search results")
    return results


async def extract_thread_content(
    session,  # RedditSession
    thread_url: str,
    max_comments: int = 10,
) -> dict:
    """Navigate to a thread and extract title, body, and top comments."""
    page = session.page
    # navigate() recovers from Reddit's transient 403 rate-block with backoff.
    await session.navigate(thread_url)

    # Scroll to load comments
    await human_scroll(page, 500)
    await asyncio.sleep(human_delay(1000, 2000))

    content = await page.evaluate(f"""
        () => {{
            // Extract post title
            const titleEl = document.querySelector(
                'h1, [data-testid="post-title"], [slot="title"]'
            );
            const title = titleEl ? titleEl.textContent.trim() : '';

            // Extract post body
            const bodyEl = document.querySelector(
                '[data-testid="post-content"] .md, '
                + '[slot="text-body"], '
                + '.Post .RichTextJSON-root, '
                + '.selftext'
            );
            const body = bodyEl ? bodyEl.textContent.trim() : '';

            // Extract comments
            const comments = [];
            const commentEls = document.querySelectorAll(
                '[data-testid="comment"], '
                + 'shreddit-comment, '
                + '.Comment'
            );

            for (const el of commentEls) {{
                if (comments.length >= {max_comments}) break;

                const authorEl = el.querySelector(
                    'a[href^="/user/"], [data-testid="comment_author"]'
                );
                const author = authorEl ? authorEl.textContent.trim() : 'anon';

                const bodyEl = el.querySelector(
                    '.md, [slot="comment-body"], .RichTextJSON-root'
                );
                const commentBody = bodyEl
                    ? bodyEl.textContent.trim().slice(0, 500)
                    : '';

                const scoreEl = el.querySelector(
                    '[data-testid="comment-score"], .score'
                );
                const score = scoreEl
                    ? parseInt(scoreEl.textContent.replace(/[^0-9-]/g, '')) || 0
                    : 0;

                if (commentBody) {{
                    comments.push({{ author, body: commentBody, score }});
                }}
            }}

            return {{ title, body: body.slice(0, 3000), comments }};
        }}
    """)

    log.info(
        f"Extracted thread: '{content.get('title', '')[:50]}' "
        f"with {len(content.get('comments', []))} comments"
    )
    return content


async def extract_subreddit_data(
    session,  # RedditSession
    subreddit_name: str,
) -> dict:
    """Gather data about a subreddit for the intelligence report.

    Visits the subreddit, extracts top posts, sample comments, and sidebar rules.
    """
    page = session.page
    data = {"posts": [], "sample_comments": [], "sidebar": ""}

    # Get hot posts
    await page.goto(
        f"https://www.reddit.com/r/{subreddit_name}/hot/",
        wait_until="domcontentloaded",
    )
    await asyncio.sleep(human_delay(2000, 4000))

    # Scroll to load more
    for _ in range(3):
        await human_scroll(page)
        await asyncio.sleep(human_delay(800, 1500))

    posts = await page.evaluate("""
        () => {
            const posts = [];
            // Modern Reddit uses shreddit-post web components with attributes
            const els = document.querySelectorAll('shreddit-post');
            for (const el of els) {
                if (posts.length >= 30) break;
                posts.push({
                    title: el.getAttribute('post-title') || '',
                    score: parseInt(el.getAttribute('score') || '0') || 0,
                    comment_count: parseInt(el.getAttribute('comment-count') || '0') || 0,
                    url: el.getAttribute('permalink') || '',
                });
            }
            // Fallback for older layouts
            if (posts.length === 0) {
                const oldEls = document.querySelectorAll('div[data-testid="post-container"], article');
                for (const el of oldEls) {
                    if (posts.length >= 30) break;
                    const titleEl = el.querySelector('h3, [slot="title"]');
                    posts.push({
                        title: titleEl ? titleEl.textContent.trim() : '',
                        score: 0,
                        comment_count: 0,
                        url: '',
                    });
                }
            }
            return posts;
        }
    """)
    data["posts"] = posts

    # Visit top post to get sample comments
    if posts and posts[0].get("url"):
        url = posts[0]["url"]
        if not url.startswith("http"):
            url = "https://www.reddit.com" + url

        thread = await extract_thread_content(session, url, max_comments=10)
        data["sample_comments"] = thread.get("comments", [])

    # Try to get sidebar/rules
    try:
        await page.goto(
            f"https://www.reddit.com/r/{subreddit_name}/about/rules/",
            wait_until="domcontentloaded",
        )
        await asyncio.sleep(human_delay(1500, 3000))

        sidebar = await page.evaluate("""
            () => {
                const rules = [];
                const ruleEls = document.querySelectorAll(
                    '[data-testid="rule"], .rule-description, article'
                );
                for (const el of ruleEls) {
                    rules.push(el.textContent.trim());
                }
                return rules.join('\\n');
            }
        """)
        data["sidebar"] = sidebar
    except Exception as e:
        log.warning(f"Could not fetch rules for r/{subreddit_name}: {e}")

    return data


async def post_comment(
    session,  # RedditSession
    thread_url: str,
    comment_text: str,
) -> dict:
    """Navigate to a thread and post a comment.

    Returns dict with 'success', 'comment_id' (if successful), 'error' (if failed).

    Flow (tested against Reddit's modern shreddit UI):
    1. Navigate to thread
    2. Dismiss cookie popup
    3. Scroll to bring composer into view
    4. Click composer-host to activate/expand the input
    5. Click contenteditable div to focus
    6. Type comment with human-like delays
    7. Click "Comment" button
    """
    page = session.page
    await page.goto(thread_url, wait_until="domcontentloaded")
    await asyncio.sleep(human_delay(2000, 4000))

    # Check if thread is locked
    is_locked = await page.evaluate("""
        () => {
            const locked = document.querySelector(
                '[data-testid="locked-icon"], .locked-icon, [aria-label*="locked"]'
            );
            return !!locked;
        }
    """)
    if is_locked:
        log.warning(f"Thread is locked: {thread_url}")
        return {"success": False, "error": "thread_locked"}

    # Check if we're banned from this community. Reddit renders a banner
    # ("You're currently banned from this community and can't comment on posts.")
    # and simply does NOT render a composer for banned users. Without this check
    # the ban masquerades as comment_box_not_found and we burn every attempt in
    # the sub retrying a box that will never exist. Detect it, return a distinct
    # terminal error, and let the caller cool the whole subreddit down.
    ban_text = await page.evaluate("""
        () => {
            const t = (document.body.innerText || '').toLowerCase();
            // Match Reddit's exact phrasing without being so broad we false-trip
            // on unrelated "ban" words elsewhere on the page.
            if (t.includes("banned from this community") ||
                t.includes("you're currently banned") ||
                t.includes("you are currently banned") ||
                t.includes("can't comment on posts") ||
                t.includes("cannot comment on posts")) {
                // Pull the sentence for the learning log.
                const m = (document.body.innerText || '').match(
                    /[^.\\n]*banned from this community[^.\\n]*/i
                );
                return m ? m[0].trim() : "banned from this community";
            }
            return '';
        }
    """)
    if ban_text:
        log.warning(f"Account is banned from this community: {thread_url} — {ban_text}")
        return {"success": False, "error": "subreddit_banned", "reason": ban_text}

    # Step 1: Dismiss cookie popup
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

    # Read like a human first: skim down through the comments before we
    # decide to say something. A session that opens a thread and instantly
    # opens the composer is a strong behavioral tell.
    for _ in range(random.randint(1, 3)):
        await human_scroll(page, random.randint(300, 800))
        await asyncio.sleep(human_delay(1000, 3000))

    # Step 2: The composer sits at the TOP of the comment section, right under
    # the post — NOT down in the comment list. Earlier code scrolled down past
    # it (confirmed via error screenshots). Scroll the composer/post area into
    # view explicitly instead.
    async def _scroll_to_composer_area() -> None:
        await page.evaluate("""
            () => {
                // Prefer the composer element itself, else the trigger, else the
                // post header (composer is just below it).
                const sel = [
                    'shreddit-composer', 'comment-composer-host',
                    'shreddit-comment-composer', 'faceplate-form',
                ];
                for (const s of sel) {
                    const el = document.querySelector(s);
                    if (el) { el.scrollIntoView({block: 'center'}); return; }
                }
                // Trigger by text
                const cands = Array.from(document.querySelectorAll('button, [role="button"], [placeholder]'));
                const trig = cands.find(e => /add a comment|join the conversation|write a comment/i.test(
                    (e.innerText||'') + ' ' + (e.getAttribute('aria-label')||'') + ' ' + (e.getAttribute('placeholder')||'')
                ));
                if (trig) { trig.scrollIntoView({block: 'center'}); return; }
                // Fallback: top of the post so the composer below it loads.
                const post = document.querySelector('shreddit-post');
                if (post) post.scrollIntoView({block: 'start'});
            }
        """)
        await asyncio.sleep(human_delay(800, 1500))

    await _scroll_to_composer_area()

    # Step 3: Activate the composer by clicking its trigger (now in view).
    # DOM el.click() inside evaluate produces untrusted events with no mouse
    # trajectory — find the element here and click it with the real pointer.
    async def _try_open_composer() -> None:
        wants = ['add a comment', 'join the conversation',
                 'write a comment', 'add comment']
        for el in await page.query_selector_all(
            'button, [role="button"], a, [aria-label], [placeholder]'
        ):
            try:
                text = " ".join([
                    await el.inner_text() or "",
                    await el.get_attribute("aria-label") or "",
                    await el.get_attribute("placeholder") or "",
                ]).lower()
                if any(w in text for w in wants) and await el.is_visible():
                    await el.scroll_into_view_if_needed()
                    await human_click(page, el)
                    log.info("Clicked 'add a comment' trigger to open composer")
                    await asyncio.sleep(human_delay(1000, 2000))
                    break
            except Exception:
                continue
        try:
            host = await page.query_selector(
                "shreddit-composer, comment-composer-host, shreddit-comment-composer"
            )
            if host:
                await host.scroll_into_view_if_needed()
                await human_click(page, host)
                await asyncio.sleep(human_delay(800, 1500))
        except Exception:
            pass

    await _try_open_composer()

    # Step 4: Find the contenteditable / textarea editor. Retry once after
    # re-opening the composer if it isn't visible yet.
    async def _find_box():
        for selector in [
            'shreddit-composer div[contenteditable="true"]',
            'faceplate-form div[contenteditable="true"]',
            'div[contenteditable="true"]',
            'textarea[name="comment"]',
            "textarea",
        ]:
            el = await page.query_selector(selector)
            if el and await el.is_visible():
                log.info(f"Found visible comment box: {selector}")
                return el
        return None

    comment_box = await _find_box()
    if not comment_box:
        # Retry: re-scroll to the composer area (NOT further down the comments)
        # and re-open. This is the fix for the ~90% miss rate seen live.
        await _scroll_to_composer_area()
        await _try_open_composer()
        comment_box = await _find_box()

    if not comment_box:
        log.error("Could not find visible comment box")
        await _screenshot_error(page, "no_comment_box")
        return {"success": False, "error": "comment_box_not_found"}

    await human_click(page, comment_box)
    await asyncio.sleep(human_delay(300, 600))

    # Step 5: Type the comment with human-like rhythm
    await human_type(page, comment_text)

    await asyncio.sleep(human_delay(1000, 2000))

    # Step 6: Submit. Prefer Ctrl/Cmd+Enter (works in Reddit's editor and is
    # layout-independent), then fall back to clicking a Comment/Reply/Post button.
    submitted = False
    try:
        import sys as _sys
        await page.keyboard.down("Meta" if _sys.platform == "darwin" else "Control")
        await page.keyboard.press("Enter")
        await page.keyboard.up("Meta" if _sys.platform == "darwin" else "Control")
        await asyncio.sleep(human_delay(1500, 2500))
        # Heuristic: if the editor is now empty, the submit went through.
        still_has_text = await page.evaluate("""
            () => {
                const ed = document.querySelector('div[contenteditable="true"], textarea');
                if (!ed) return false;
                const v = (ed.innerText || ed.value || '').trim();
                return v.length > 5;
            }
        """)
        submitted = not still_has_text
        if submitted:
            log.info("Submitted comment via keyboard shortcut")
    except Exception:
        pass

    if not submitted:
        submit_btn = None
        btns = await page.query_selector_all('button, [role="button"]')
        for btn in btns:
            try:
                txt = (await btn.inner_text()).strip().lower()
                aria = (await btn.get_attribute("aria-label") or "").lower()
                label = f"{txt} {aria}"
                if (("comment" in label or "reply" in label or "post" in label)
                        and "add a comment" not in label
                        and await btn.is_visible()):
                    submit_btn = btn
                    break
            except Exception:
                continue

        if not submit_btn:
            log.error("Could not find visible Comment button")
            await _screenshot_error(page, "no_submit_button")
            return {"success": False, "error": "submit_button_not_found"}

        log.info("Clicking Comment submit button")
        await human_click(page, submit_btn)

    # Wait for the comment to appear
    await asyncio.sleep(human_delay(3000, 5000))

    # Check for errors (rate limit, etc.)
    error_text = await page.evaluate("""
        () => {
            const errorEl = document.querySelector(
                '[class*="error"], [data-testid="comment-error"]'
            );
            return errorEl ? errorEl.textContent.trim() : '';
        }
    """)

    if error_text:
        log.error(f"Reddit posting error: {error_text}")
        return {"success": False, "error": error_text}

    # Try to extract the comment ID from the page
    comment_id = await page.evaluate("""
        () => {
            // Get the most recent comment by the logged-in user
            const comments = document.querySelectorAll(
                '[data-testid="comment"], shreddit-comment'
            );
            const last = comments[comments.length - 1];
            if (last) {
                return last.getAttribute('id')
                    || last.getAttribute('data-fullname')
                    || last.getAttribute('thingid')
                    || 'unknown';
            }
            return 'unknown';
        }
    """)

    log.info(f"Comment posted successfully (id: {comment_id})")
    return {"success": True, "comment_id": comment_id}


async def check_comment_visible(
    incognito_page: Page,
    thread_url: str,
    comment_text_snippet: str,
) -> bool:
    """Check if a comment is visible to a logged-out user.

    Used for shadowban detection. Opens the thread in an incognito
    context and searches for the comment text.
    """
    try:
        await incognito_page.goto(thread_url, wait_until="domcontentloaded")
        await asyncio.sleep(human_delay(2000, 4000))

        # Normalize both sides so curly quotes, emoji, and whitespace don't cause
        # false misses. We match on a distinctive run of words from the comment,
        # not the literal first-50-chars (which breaks on a single quote diff).
        def _norm(s: str) -> str:
            s = (s or "").lower()
            for a, b in [("’", "'"), ("‘", "'"), ("“", '"'),
                         ("”", '"'), ("—", "-"), ("–", "-")]:
                s = s.replace(a, b)
            return re.sub(r"[^a-z0-9 ]", " ", re.sub(r"\s+", " ", s)).strip()

        target = _norm(comment_text_snippet)
        # A distinctive ~6-word slice is enough to identify the comment uniquely
        # without being fragile to trailing emoji/punctuation.
        words = target.split()
        needle = " ".join(words[:8]) if len(words) >= 4 else target
        if not needle:
            return True  # nothing to match → don't claim shadowban

        # Scroll harder to trigger lazy-loaded comments on big threads, checking
        # after each scroll so we can stop early once found.
        for _ in range(8):
            page_text = _norm(await incognito_page.evaluate("document.body.innerText"))
            if needle in page_text:
                return True
            await human_scroll(incognito_page, random.randint(1200, 2000))
            await asyncio.sleep(human_delay(600, 1200))

        # Final check after all scrolling.
        page_text = _norm(await incognito_page.evaluate("document.body.innerText"))
        return needle in page_text

    except Exception as e:
        log.error(f"Shadowban check failed: {e}")
        return True  # Assume visible on error (fail open — never false-accuse)


async def _screenshot_error(page: Page, name: str) -> None:
    """Take a screenshot on error."""
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    from datetime import datetime

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = SCREENSHOTS_DIR / f"error_{name}_{timestamp}.png"
    try:
        await page.screenshot(path=str(path))
        log.info(f"Error screenshot: {path}")
    except Exception:
        pass
    # Keep the screenshot dir bounded so a long unattended run can't fill disk.
    prune_screenshots()
