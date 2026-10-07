"""Scan subreddits for relevant threads by browsing the feed."""

from dataclasses import dataclass

from src.browser.session import NetworkBlockedError
from src.config import SubredditConfig
from src.db import has_commented_on_thread, record_thread
from src.log import get_logger

log = get_logger("scanner")


@dataclass
class DiscoveredThread:
    id: str
    subreddit: str
    title: str
    body: str
    url: str
    score: int
    comment_count: int
    top_comments: list[dict]


async def scan_subreddit(
    browser_session,
    subreddit: SubredditConfig,
    limit: int = 15,
) -> list[DiscoveredThread]:
    """Scan a subreddit's hot/new feed for threads to engage with.

    Instead of using Reddit's search (which uses different DOM and is unreliable),
    we browse the subreddit feed directly and let the LLM evaluator filter for relevance.
    """
    log.info(f"Scanning r/{subreddit.name} feed for threads")
    threads: list[DiscoveredThread] = []

    try:
        from src.browser.actions import extract_feed_posts

        posts = await extract_feed_posts(browser_session, subreddit.name, limit)

        for post in posts:
            if has_commented_on_thread(post["id"]):
                log.info(f"Skipping {post['id']} — already commented")
                continue

            thread = DiscoveredThread(
                id=post["id"],
                subreddit=subreddit.name,
                title=post["title"],
                body="",
                url=post["url"],
                score=post.get("score", 0),
                comment_count=post.get("comment_count", 0),
                top_comments=[],
            )
            threads.append(thread)

            record_thread(
                thread_id=thread.id,
                subreddit=subreddit.name,
                title=thread.title,
                url=thread.url,
                score=thread.score,
                comment_count=thread.comment_count,
            )

    except NetworkBlockedError:
        # A network block is global (IP/session-level), not specific to this
        # sub. Propagate so the caller can abort the whole pass instead of
        # grinding through every remaining sub with guaranteed-fail backoff.
        raise
    except Exception as e:
        log.error(f"Error scanning r/{subreddit.name}: {e}")

    log.info(f"Found {len(threads)} new threads in r/{subreddit.name}")
    return threads


async def read_thread_details(
    browser_session,
    thread_url: str,
    max_comments: int = 10,
) -> dict:
    """Navigate to a thread and extract full details including top comments."""
    from src.browser.actions import extract_thread_content

    try:
        return await extract_thread_content(browser_session, thread_url, max_comments)
    except Exception as e:
        log.error(f"Failed to read thread {thread_url}: {e}")
        return {"title": "", "body": "", "comments": []}
