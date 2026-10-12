"""Publish notes on creator.xiaohongshu.com.

DOM map verified against the live creator platform (2026-09) — XHS re-skins
this page often, so every step verifies its own effect and falls back before
giving up:

- /publish/publish defaults to the 上传视频 tab; 上传图文 is a span you click.
- The first image goes to `input.upload-input`; once the editor mounts, that
  input is gone and later images go to a hidden `input[type=file][multiple]`.
- Images upload ONE at a time — the order IS the note order; the page renders
  the progress counter as "N/18" text. GIF is rejected by XHS.
- Title/body/topic text is typed with real keystrokes (human_type) — bulk
  execCommand injection produces input events with zero keystroke dynamics,
  a behavioral tell on a signature-gated, risk-controlled platform. Enter
  per line creates the paragraph break.
- Topic: type ' #tag' → suggestion list `div.item` (`.name`) → click
  the exact match → becomes `a.tiptap-topic`. Enter key is unreliable.
- Publish: `<xhs-publish-btn>` carries `submit-text`/`submit-disabled`
  attributes (reliable enable signal). Its buttons live in a CLOSED shadow
  root DOM queries can't pierce (`._sr` is gone on current builds) — so we
  click by coordinates on the host's right side (发布 is rightmost; 暂存离开
  is the left button). Success navigates to /publish/success, then returns
  to the publish page ~4s later.
"""

import asyncio
import sys
import time

from src.browser.stealth import (
    human_click,
    human_delay,
    human_move,
    human_type,
)
from src.log import get_logger

log = get_logger("xhs.publish")

PUBLISH_PAGE = (
    "https://creator.xiaohongshu.com/publish/publish"
    "?source=official&from=tab_switch&target=image"
)

_EDITOR_SEL = (
    ".tiptap.ProseMirror, .ql-editor, div[contenteditable='true']"
)


# ─── small eval helpers ─────────────────────────────────


async def _switch_tab(page, name: str) -> bool:
    """Click the publish-page tab (上传图文 / 上传视频) by its label text."""
    return bool(await page.evaluate(
        """(name) => {
            const byText = (els) => els.find(
                e => (e.textContent || '').trim() === name
            );
            // Tabs are label spans; match leaf-level elements first so a
            // container div can't shadow the real target.
            let t = byText([...document.querySelectorAll('span')]);
            if (!t) {
                t = byText([...document.querySelectorAll(
                    '.creator-tab, [class*=tab], a, li, div'
                )]);
            }
            if (t) { t.click(); return true; }
            return false;
        }""",
        name,
    ))


async def _media_count(page) -> int:
    """Read the rendered "N/18" upload counter (-1 if not rendered yet)."""
    return await page.evaluate(
        """() => {
            const t = document.body ? document.body.textContent || '' : '';
            const m = t.match(/(\\d+)\\s*\\/\\s*18/);
            return m ? parseInt(m[1], 10) : -1;
        }"""
    )


async def _wait_media_count(page, expected: int, timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if await _media_count(page) >= expected:
            return True
        await asyncio.sleep(1)
    return False


async def _wait_editor(page, timeout_s: float = 30) -> bool:
    """Wait until the note editor (title input + rich body) is mounted."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        ready = await page.evaluate(
            """() => {
                const title = document.querySelector('input[placeholder*=标题]');
                const body = document.querySelector(
                    '.tiptap.ProseMirror, .ql-editor, div[contenteditable="true"]'
                );
                return !!(title && body);
            }"""
        )
        if ready:
            return True
        await asyncio.sleep(1)
    return False


async def _set_file(page, selector: str, path: str) -> None:
    """Feed one file to a file input; fall back to the file-chooser API."""
    el = await page.wait_for_selector(selector, state="attached", timeout=15000)
    if not el:
        raise RuntimeError(f"file input not found: {selector}")
    try:
        await el.set_input_files(path)
        return
    except Exception as e:
        log.warning(f"set_input_files failed on {selector} ({e}); trying file chooser")
    async with page.expect_file_chooser(timeout=15000) as fc_info:
        await el.evaluate("e => e.click()")
    chooser = await fc_info.value
    await chooser.set_files(path)


async def _upload_images(page, paths: list[str]) -> int:
    """Upload images one at a time, in order. Returns the confirmed count."""
    for i, path in enumerate(paths, start=1):
        if i == 1:
            selector = "input.upload-input"
        else:
            # After the first upload the mount input is replaced by a hidden
            # multiple-file input inside the editor's upload area.
            selector = "input[type=file][multiple]"
        try:
            await _set_file(page, selector, path)
        except RuntimeError:
            if i > 1:
                # Some layouts keep a single input around — retry with it.
                await _set_file(page, "input.upload-input", path)
            else:
                raise

        # The N/18 counter is the source of truth for "server accepted it".
        if not await _wait_media_count(page, i, timeout_s=90):
            # One more shot via the alternate input before giving up.
            alt = "input.upload-input" if "multiple" in selector else \
                "input[type=file][multiple]"
            try:
                await _set_file(page, alt, path)
            except RuntimeError:
                pass
            if not await _wait_media_count(page, i, timeout_s=60):
                raise RuntimeError(
                    f"image {i}/{len(paths)} did not register (N/18 counter "
                    f"stuck at {await _media_count(page)})"
                )
        log.info(f"Uploaded image {i}/{len(paths)}")
        await asyncio.sleep(human_delay(800, 2000))

    # Editor mounts after the first image; make sure it's fully there.
    if not await _wait_editor(page, timeout_s=30):
        raise RuntimeError("note editor did not appear after image upload")
    return await _media_count(page)


async def _set_title(page, title: str) -> str | None:
    """Type the title with real keystrokes. Returns the final value."""
    el = await page.query_selector('input[placeholder*=标题]')
    if not el:
        return None
    await human_click(page, el)
    # select() so a retry replaces whatever landed the first time.
    await el.evaluate("e => e.select()")
    await human_type(page, title)
    return await el.evaluate("e => e.value")


async def _set_body(page, content: str) -> int:
    """Type the body into the tiptap editor with real keystrokes.

    '\n' arrives as Enter, which the editor turns into a paragraph break.
    Returns the rendered character count (-1 if the editor is missing).
    """
    ed = await page.query_selector(_EDITOR_SEL)
    if not ed:
        return -1
    await human_click(page, ed)
    await page.evaluate(
        """() => {
            document.execCommand('selectAll');
            document.execCommand('delete');
        }"""
    )
    await human_type(page, content)
    return await page.evaluate(
        """() => {
            const ed = document.querySelector(
                '.tiptap.ProseMirror, .ql-editor, div[contenteditable="true"]'
            );
            return ed ? (ed.innerText || '').length : -1;
        }"""
    )


async def _topic_count(page) -> int:
    return await page.evaluate(
        """() => {
            const ed = document.querySelector(
                '.tiptap.ProseMirror, .ql-editor, div[contenteditable="true"]'
            );
            return ed ? ed.querySelectorAll('a.tiptap-topic').length : 0;
        }"""
    )


async def _add_topic(page, tag: str, timeout_s: float = 30) -> bool:
    """Turn `tag` into a real #话题 link via the suggestion dropdown.

    Plain '#tag' text is NOT a topic — the editor only creates a
    `a.tiptap-topic` when you pick a suggestion. The suggestion list can take
    7-20s while the page's own script stalls; keep polling.
    """
    before = await _topic_count(page)
    ed = await page.query_selector(_EDITOR_SEL)
    if not ed:
        return False
    await ed.evaluate(
        """(ed) => {
            ed.focus();
            const sel = window.getSelection();
            sel.selectAllChildren(ed);
            sel.collapseToEnd();
        }"""
    )
    await human_type(page, " #" + tag)

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        picked = await page.evaluate(
            """(tag) => {
                const items = [...document.querySelectorAll('div.item')];
                if (!items.length) return false;
                const norm = s => (s || '').replace(/\\s+/g, '');
                const exact = items.find(it => {
                    const n = it.querySelector('.name');
                    const t = norm((n && n.textContent) || it.textContent);
                    return t === '#' + tag || t === tag;
                });
                (exact || items[0]).click();
                return true;
            }""",
            tag,
        )
        if picked:
            await asyncio.sleep(human_delay(500, 1200))
            return await _topic_count(page) > before
        await asyncio.sleep(1)
    return False


async def _prepublish_check(page) -> dict:
    """Self-check before clicking publish: counters, title, topics."""
    return await page.evaluate(
        """() => {
            const text = document.body ? document.body.textContent || '' : '';
            const im = text.match(/(\\d+)\\s*\\/\\s*18/);
            const cm = text.match(/(\\d+)\\s*\\/\\s*1000/);
            const titleEl = document.querySelector('input[placeholder*=标题]');
            const ed = document.querySelector(
                '.tiptap.ProseMirror, .ql-editor, div[contenteditable="true"]'
            );
            const host = document.querySelector('xhs-publish-btn');
            return {
                images: im ? parseInt(im[1], 10) : null,
                title: titleEl ? titleEl.value : null,
                chars: cm ? parseInt(cm[1], 10) : null,
                topics: ed ? ed.querySelectorAll('a.tiptap-topic').length : 0,
                submit_disabled: host
                    ? host.getAttribute('submit-disabled') : null,
            };
        }"""
    )


async def _publish_enabled(page) -> bool:
    """True if the publish button exists and is clickable.

    The `submit-disabled` attribute on xhs-publish-btn is the reliable
    signal — the buttons themselves are in a closed shadow root.
    """
    return bool(await page.evaluate(
        """() => {
            const host = document.querySelector('xhs-publish-btn');
            if (host) {
                const dis = host.getAttribute('submit-disabled');
                if (dis !== null) {
                    const loading = host.getAttribute('submit-loading');
                    return dis === 'false' && loading !== 'true';
                }
            }
            const b = [...document.querySelectorAll('button')]
                .find(b => (b.textContent || '').trim() === '发布');
            return b ? !b.disabled : false;
        }"""
    ))


async def _click_publish(page) -> str | None:
    """Click 发布. Returns how it was clicked, or None if not found.

    Order: 1) legacy `._sr` shadow root if a page version still exposes it,
    2) a trusted coordinate click on the host's right side (发布 is the
    rightmost button — real mouse events compose through closed shadow),
    3) a plain-DOM button fallback for older layouts.
    """
    via_sr = await page.evaluate(
        """() => {
            const host = document.querySelector('xhs-publish-btn');
            if (host && host._sr) {
                const b = [...host._sr.querySelectorAll('button')]
                    .find(b => /发布/.test(b.textContent || '') && !b.disabled);
                if (b) { b.click(); return 'shadow-dom'; }
            }
            return null;
        }"""
    )
    if via_sr:
        return via_sr

    host = await page.query_selector("xhs-publish-btn")
    if host:
        box = await host.bounding_box()
        if box:
            # 发布 sits at the right of the host (暂存离开 is on the left).
            x = box["x"] + box["width"] * 0.77
            y = box["y"] + box["height"] / 2
            await human_move(page, x, y)
            await page.mouse.click(x, y)
            return "host-coords"

    via_dom = await page.evaluate(
        """() => {
            const b = [...document.querySelectorAll('button')]
                .find(b => (b.textContent || '').trim() === '发布' && !b.disabled);
            if (b) { b.click(); return 'dom'; }
            return null;
        }"""
    )
    return via_dom


async def _dismiss_popups(page) -> None:
    """Close feature-tip modals (我知道了/知道了) that intercept clicks.

    The creator page pops tutorial cards (e.g. 图片可以编辑啦) with a
    click-catching overlay — a coordinate click on 发布 then lands on the
    overlay instead of the button.
    """
    await page.evaluate(
        """() => {
            const wants = ['我知道了', '知道了', '好的', '关闭'];
            for (const el of document.querySelectorAll(
                'button, [class*=btn], [class*=close]'
            )) {
                const t = (el.textContent || '').trim();
                if (wants.includes(t) && el.offsetParent !== null) {
                    el.click();
                    return;
                }
            }
            // fallback: X close icon on modal/guide cards
            const x = document.querySelector(
                '[class*=modal] [class*=close], [class*=dialog] [class*=close], '
                + '[class*=guide] [class*=close], [class*=popover] [class*=close]'
            );
            if (x) x.click();
        }"""
    )


async def _click_point_blocked(page) -> str | None:
    """Check the intended click point isn't covered by an overlay.

    elementFromPoint returns the topmost element; inside closed shadow DOM
    it retargets to the host — so anything else here means a modal/overlay
    is intercepting clicks. Returns a descriptor, or None when clear.
    """
    return await page.evaluate(
        """() => {
            const host = document.querySelector('xhs-publish-btn');
            if (!host) return 'no-host';
            const r = host.getBoundingClientRect();
            const el = document.elementFromPoint(
                r.x + r.width * 0.77, r.y + r.height / 2
            );
            if (!el) return 'no-element';
            if (el === host || host.contains(el)
                || (el.closest && el.closest('xhs-publish-btn'))) {
                return null;
            }
            return (el.tagName.toLowerCase() + '.' + String(el.className))
                .slice(0, 80);
        }"""
    )


async def _wait_published(page, timeout_s: float = 20) -> bool:
    """Success = navigation to /publish/success or a 发布成功 toast."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if "/publish/success" in page.url:
            return True
        try:
            ok = await page.evaluate(
                """() => {
                    const t = document.body ? document.body.innerText || '' : '';
                    return t.includes('发布成功') || t.includes('笔记发布成功');
                }"""
            )
            if ok:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return False


async def _wait_publishable(page, timeout_s: float) -> bool:
    """Wait until the publish button is enabled (media finished uploading)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if await _publish_enabled(page):
            return True
        await asyncio.sleep(2)
    return False


async def _fill_note(page, title: str, content: str, tags: list[str]) -> dict:
    """Shared fill step: title, body, topics. Returns per-field results."""
    result: dict = {"tags_failed": []}

    got_title = await _set_title(page, title)
    if got_title != title:
        # One retry — XHS has dropped the input event before.
        await asyncio.sleep(human_delay(400, 900))
        got_title = await _set_title(page, title)
    result["title_set"] = got_title == title
    result["title_value"] = got_title
    await asyncio.sleep(human_delay(400, 900))

    chars = await _set_body(page, content)
    result["body_chars"] = chars
    result["body_set"] = chars >= 0
    await asyncio.sleep(human_delay(500, 1200))

    for tag in tags:
        try:
            ok = await _add_topic(page, tag)
        except Exception as e:
            log.warning(f"topic '#{tag}' failed: {e}")
            ok = False
        if not ok:
            result["tags_failed"].append(tag)
        await asyncio.sleep(human_delay(500, 1500))
    result["topics_added"] = await _topic_count(page)

    result["check"] = await _prepublish_check(page)
    return result


async def _finish_publish(
    page,
    result: dict,
    auto_click: bool = False,
    confirm_timeout_s: float = 900,
) -> dict:
    """Finish a filled note: either wait for the human to click 发布
    (default — that click IS the final approval, and it works reliably on
    the closed-shadow button), or attempt the click ourselves with --auto.
    """
    if not await _wait_publishable(page, timeout_s=60):
        result.update(success=False, error="publish_button_stayed_disabled")
        return result

    await page.bring_to_front()
    await asyncio.sleep(human_delay(500, 1200))

    # Tutorial popovers can sit between us and the button — dismiss first.
    await _dismiss_popups(page)
    await asyncio.sleep(human_delay(400, 900))

    if not auto_click:
        print(
            "表单已填好 — 请在 Chrome 窗口检查内容并点击「发布」"
            f"(等待中，{int(confirm_timeout_s / 60)} 分钟超时)",
            file=sys.stderr,
        )
        if await _wait_published(page, timeout_s=confirm_timeout_s):
            result.update(success=True, via="manual-confirm", url=page.url)
        else:
            result.update(
                success=False,
                error="manual_confirm_timeout",
                detail="no publish click detected before timeout — "
                       "the filled form was closed with the browser; "
                       "check 草稿箱, the platform may have auto-saved it",
            )
        return result

    blocked = await _click_point_blocked(page)
    if blocked:
        await _dismiss_popups(page)
        await asyncio.sleep(human_delay(400, 900))
        blocked = await _click_point_blocked(page)
    if blocked:
        result.update(success=False, error=f"click_blocked:{blocked}")
        return result

    how = await _click_publish(page)
    if not how:
        result.update(success=False, error="publish_button_not_found")
        return result
    log.info(f"Clicked publish via {how}")

    if await _wait_published(page):
        result.update(success=True, via=how, url=page.url)
    else:
        result.update(
            success=False, via=how, url=page.url,
            error="no_success_signal_after_publish",
        )
    return result


# ─── public entry points ────────────────────────────────


async def publish_image_note(
    session,
    title: str,
    content: str,
    images: list[str],
    tags: list[str],
    auto_click: bool = False,
    confirm_timeout_s: float = 900,
) -> dict:
    """Publish a 图文 note: upload images → fill form → 发布."""
    page = session.page
    result: dict = {"success": False, "kind": "image", "title": title}

    await session.navigate(PUBLISH_PAGE)

    if not await _switch_tab(page, "上传图文"):
        log.warning("上传图文 tab not found — assuming already on it")

    try:
        await page.wait_for_selector(
            "input.upload-input", state="attached", timeout=15000
        )
    except Exception:
        result["error"] = "upload_input_not_found"
        return result

    try:
        result["images_uploaded"] = await _upload_images(page, images)
    except Exception as e:
        log.error(f"Image upload failed: {e}")
        await session._screenshot("xhs_upload_failed")
        result["error"] = f"upload_failed: {e}"
        return result

    result.update(await _fill_note(page, title, content, tags))
    if not result.get("title_set") or not result.get("body_set"):
        await session._screenshot("xhs_fill_failed")
        result["error"] = "form_fill_failed"
        return result

    out = await _finish_publish(
        page, result, auto_click=auto_click, confirm_timeout_s=confirm_timeout_s
    )
    if not out.get("success"):
        await session._screenshot("xhs_publish_failed")
    return out


async def publish_video_note(
    session,
    title: str,
    content: str,
    video: str,
    tags: list[str],
    upload_timeout_s: float = 600,
    auto_click: bool = False,
    confirm_timeout_s: float = 900,
) -> dict:
    """Publish a 视频 note: upload video → fill form → wait enable → 发布."""
    page = session.page
    result: dict = {"success": False, "kind": "video", "title": title}

    await session.navigate(PUBLISH_PAGE)

    # 上传视频 is the default tab; click anyway in case the URL restored 图文.
    if not await _switch_tab(page, "上传视频"):
        log.warning("上传视频 tab not found — assuming already on it")

    try:
        await _set_file(page, "input.upload-input", video)
    except RuntimeError as e:
        result["error"] = f"video_input_not_found: {e}"
        return result

    if not await _wait_editor(page, timeout_s=60):
        result["error"] = "editor_not_found_after_video_upload"
        return result

    # Video transcodes server-side — the publish button stays disabled until
    # it's ready. Fill the form while that happens.
    result.update(await _fill_note(page, title, content, tags))
    if not result.get("title_set") or not result.get("body_set"):
        await session._screenshot("xhs_fill_failed")
        result["error"] = "form_fill_failed"
        return result

    if not await _wait_publishable(page, timeout_s=upload_timeout_s):
        await session._screenshot("xhs_video_upload_timeout")
        result["error"] = "video_upload_timeout"
        return result

    out = await _finish_publish(
        page, result, auto_click=auto_click, confirm_timeout_s=confirm_timeout_s
    )
    if not out.get("success"):
        await session._screenshot("xhs_publish_failed")
    return out
