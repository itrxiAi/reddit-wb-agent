"""Collect items that need a reply — fetch-only, no posting.

Returns JSON-friendly data for the host agent to read and decide what to say.
Two sources:
1. Replies to our past comments (scraped from the threads we commented on)
2. New DMs in the Reddit inbox
"""

import asyncio

from src.browser.dms import already_replied
from src.browser.stealth import human_click, human_delay
from src.db import get_connection
from src.log import get_logger

log = get_logger("pending")


async def collect_comment_replies(session, limit: int = 5) -> list[dict]:
    """Check our recent comments for replies. Read-only.

    Returns a list of items:
      {comment_id, thread_id, thread_url, subreddit, our_comment, replies}
    where replies is [{author, body}]. Threads we've already answered in
    (we have more than one comment there) are skipped.
    """
    page = session.page
    results = []

    with get_connection() as conn:
        comments = conn.execute(
            """SELECT id, thread_id, subreddit, comment_text
               FROM comments
               WHERE status = 'posted'
               AND posted_at >= datetime('now', '-7 days')
               ORDER BY posted_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()

    if not comments:
        log.info("No recent comments to check for replies")
        return results

    for comment in comments:
        thread_id = comment["thread_id"]

        with get_connection() as conn:
            thread = conn.execute(
                "SELECT url FROM threads WHERE id = ?", (thread_id,)
            ).fetchone()
            if not thread:
                continue
            thread_url = thread["url"]

            # Skip threads where we already posted a follow-up comment
            our_count = conn.execute(
                "SELECT COUNT(*) FROM comments WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()[0]
            if our_count > 1:
                continue

        try:
            await page.goto(thread_url, wait_until="domcontentloaded")
            await asyncio.sleep(human_delay(2000, 4000))

            our_text_snippet = comment["comment_text"][:40]
            replies = await page.evaluate(f"""
                () => {{
                    const allComments = document.querySelectorAll('shreddit-comment, [data-testid="comment"]');
                    const replies = [];
                    let foundOurs = false;

                    for (const el of allComments) {{
                        const text = el.textContent || '';
                        if (text.includes('{our_text_snippet.replace("'", "\\'")}')) {{
                            foundOurs = true;
                            continue;
                        }}
                        // Comments after ours at a deeper nesting level are replies
                        if (foundOurs && replies.length < 3) {{
                            const depth = el.getAttribute('depth') || '0';
                            if (parseInt(depth) > 0) {{
                                const body = el.querySelector('[slot="comment-body"], .md');
                                const author = el.querySelector('a[href^="/user/"]');
                                if (body) {{
                                    replies.push({{
                                        body: body.textContent.trim().slice(0, 300),
                                        author: author ? author.textContent.trim() : 'anon',
                                    }});
                                }}
                            }}
                        }}
                    }}
                    return replies;
                }}
            """)

            if replies:
                results.append({
                    "comment_id": comment["id"],
                    "thread_id": thread_id,
                    "thread_url": thread_url,
                    "subreddit": comment["subreddit"],
                    "our_comment": comment["comment_text"][:300],
                    "replies": replies,
                })

        except Exception as e:
            log.warning(f"Error checking replies for thread {thread_id}: {e}")
            continue

    log.info(f"Found replies on {len(results)} of {len(comments)} recent comments")
    return results


async def collect_dms(session, limit: int = 10) -> list[dict]:
    """Fetch incoming DMs from the inbox. Read-only.

    Returns [{author, subject, body, is_new}]. Users we've already replied
    to are skipped.
    """
    page = session.page

    log.info("Checking DMs")

    await page.goto(
        "https://www.reddit.com/message/messages/",
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

    messages = await page.evaluate(f"""
        () => {{
            const msgs = [];
            const elements = document.querySelectorAll(
                '[data-testid="message"], .message, article'
            );
            for (const el of elements) {{
                if (msgs.length >= {limit}) break;
                const author = el.querySelector('a[href^="/user/"]');
                const subject = el.querySelector('[data-testid="message-subject"], .subject, h4');
                const body = el.querySelector('[data-testid="message-body"], .md, p');
                const isNew = el.querySelector('.unread, [class*="unread"]');

                if (body && body.textContent.trim()) {{
                    msgs.push({{
                        author: author ? author.textContent.trim().replace('u/', '') : 'unknown',
                        subject: subject ? subject.textContent.trim() : '',
                        body: body.textContent.trim().slice(0, 500),
                        is_new: !!isNew,
                    }});
                }}
            }}
            return msgs;
        }}
    """)

    return [m for m in messages if not already_replied(m["author"])]
