"""Read the account's own notes from the creator platform (笔记管理).

XHS signature-gates its API — self-made requests get a 406. So instead of
calling endpoints ourselves we let the notemanage page fire its own signed
requests and read the JSON responses off the wire. DOM scraping is the
fallback if response capture comes up empty.
"""

import asyncio

from src.browser.stealth import human_delay, human_scroll
from src.log import get_logger
from src.xhs.config import NOTEMANAGE_URL

log = get_logger("xhs.notes")


def _collect_note_items(node, out: list[dict]) -> None:
    """Recursively walk a JSON payload for note-shaped dicts."""
    if isinstance(node, dict):
        nid = node.get("note_id") or node.get("noteId") or node.get("id")
        if isinstance(nid, str) and nid and (
            "title" in node or "display_title" in node or "type" in node
        ):
            out.append({
                "note_id": nid,
                "title": node.get("title") or node.get("display_title") or "",
                "status": str(
                    node.get("status")
                    or node.get("audit_status")
                    or ""
                ),
                "time": str(
                    node.get("time")
                    or node.get("create_time")
                    or node.get("last_update_time")
                    or ""
                ),
                "url": f"https://www.xiaohongshu.com/explore/{nid}",
            })
        for v in node.values():
            _collect_note_items(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_note_items(v, out)


async def _dom_fallback(page, limit: int) -> list[dict]:
    """Best-effort scrape of the notemanage table if no JSON was captured."""
    return await page.evaluate(
        """(limit) => {
            const out = [];
            const seen = new Set();
            for (const a of document.querySelectorAll('a[href]')) {
                const href = a.getAttribute('href') || '';
                if (!/note|content|publish/i.test(href)) continue;
                const row = a.closest(
                    'tr, [class*=row], [class*=item], [class*=card]'
                ) || a;
                const text = (row.textContent || '')
                    .replace(/\\s+/g, ' ').trim().slice(0, 200);
                const key = href + '|' + text;
                if (text && !seen.has(key)) {
                    seen.add(key);
                    out.push({
                        title: (a.textContent || '').trim(),
                        href, text,
                    });
                }
                if (out.length >= limit) break;
            }
            return out;
        }""",
        limit,
    )


async def list_notes(session, limit: int = 20) -> list[dict]:
    """Open 笔记管理, capture the page's own API responses, return notes."""
    page = session.page
    captured: list[dict] = []

    async def on_response(resp):
        try:
            if resp.request.resource_type not in ("xhr", "fetch"):
                return
            data = await resp.json()
        except Exception:
            return
        _collect_note_items(data, captured)

    def _listener(resp):
        asyncio.ensure_future(on_response(resp))

    page.on("response", _listener)
    try:
        await session.navigate(NOTEMANAGE_URL)
        await asyncio.sleep(human_delay(2000, 4000))
        for _ in range(2):
            await human_scroll(page)
            await asyncio.sleep(human_delay(1200, 2500))
    finally:
        page.remove_listener("response", _listener)

    if captured:
        seen: set[str] = set()
        notes = []
        for n in captured:
            if n["note_id"] not in seen:
                seen.add(n["note_id"])
                notes.append(n)
        log.info(f"Captured {len(notes)} notes from page API responses")
        return notes[:limit]

    log.info("No note JSON captured — falling back to DOM scrape")
    return await _dom_fallback(page, limit)
