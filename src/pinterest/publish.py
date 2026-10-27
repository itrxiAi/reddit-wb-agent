"""Publish pins via pinterest.com/pin-creation-tool/.

The pin builder is a React app — its DOM re-skins often, so every step
verifies its own effect and falls back through several selector
strategies before giving up:

- Media: a drag-drop zone backed by `input[type=file]` (accept image or
  video). One media file per pin.
- Title: text input, placeholder/aria-label "Add a title" (≤100 chars).
- Description: textarea or contenteditable — "Tell everyone what your
  Pin is about" (≤500 chars). Typed with real keystrokes.
- Link: "Add a destination link" input.
- Board: "Choose a board" opens a searchable list — pick by exact name,
  or leave the default.
- Tagged topics: `#combobox-storyboard-interest-tags` combobox — type a
  topic, pick the suggestion. Invisible to users, feeds Pinterest's
  topic graph.
- Product tags: "添加产品" opens a picker sidebar (search product pins
  or paste a link) — pick a result to stick a product tag on the image.
- AI disclosure: `pin-draft-ai-disclosure-*` checkboxes — "标记为 AI
  修改" plus the "包含 AI 生成的人物" sub-checkbox.
- More options (更多选项) expands: alt text (`#storyboardAltText`),
  comments toggle (`#CommentSwitch`), similar-products toggle
  (`#stelaSwitch`).
- Publish: red "Publish" button (top right). Success = the page leaves
  the builder, shows a "Pin published" toast, or lands on the pin URL.
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
from src.pinterest.config import PIN_BUILDER_URL

log = get_logger("pinterest.publish")


# ─── small eval helpers ─────────────────────────────────


async def _find_el(page, js: str, arg=None):
    """JS locates the element, Python gets an ElementHandle — so the click
    itself can go through the real mouse (JS `el.click()` emits no pointer
    events and no trajectory — a bot tell)."""
    h = await (
        page.evaluate_handle(js, arg) if arg is not None
        else page.evaluate_handle(js)
    )
    return h.as_element() if h else None


async def _click_el(page, el) -> bool:
    """Real-mouse click on an element found by JS: scroll into view, bezier
    move, click. Falls back to Playwright's click (still genuine input
    events, just no move trail) if the element is awkward."""
    if not el:
        return False
    try:
        await el.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    try:
        await human_click(page, el)
    except Exception:
        await el.click()
    return True


async def _wait_builder(page, timeout_s: float = 30) -> bool:
    """Wait until the pin-builder form (file input or title field) mounts."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        ready = await page.evaluate(
            """() => {
                const file = document.querySelector('input[type=file]');
                const re = /title|标题|简要描述/i;
                const title = [...document.querySelectorAll(
                    'input, textarea'
                )].filter(f => f.offsetParent !== null)
                  .find(f => re.test(
                    (f.getAttribute('placeholder') || '')
                    + (f.getAttribute('aria-label') || '')
                ));
                return !!(file || title);
            }"""
        )
        if ready:
            return True
        await asyncio.sleep(1)
    return False


async def _set_file(page, path: str) -> None:
    """Feed the media file to the builder's file input."""
    el = await page.wait_for_selector(
        'input[type=file]', state="attached", timeout=20000
    )
    if not el:
        raise RuntimeError("media file input not found")
    try:
        await el.set_input_files(path)
        return
    except Exception as e:
        log.warning(f"set_input_files failed ({e}); trying file chooser")
    async with page.expect_file_chooser(timeout=15000) as fc_info:
        await el.evaluate("e => e.click()")
    chooser = await fc_info.value
    await chooser.set_files(path)


async def _media_ready(page, timeout_s: float = 120) -> bool:
    """Wait until the uploaded media has a rendered preview / edit controls.

    The builder shows an Edit/delete affordance (or the title field becomes
    visible) once the upload is accepted. Videos can take a while to
    transcode — caller passes a longer timeout.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        ready = await page.evaluate(
            """() => {
                const t = document.body ? document.body.innerText || '' : '';
                if (/upload failed|couldn't upload|try again|上传失败|无法上传/i.test(t)) {
                    return 'failed';
                }
                const re = /title|标题|简要描述/i;
                const title = [...document.querySelectorAll(
                    'input, textarea'
                )].filter(f => f.offsetParent !== null)
                  .find(f => re.test(
                    (f.getAttribute('placeholder') || '')
                    + (f.getAttribute('aria-label') || '')
                ));
                const edit = document.querySelector(
                    '[aria-label*="edit" i], [data-test-id*="edit" i], '
                    + '[aria-label*="编辑" i]'
                );
                return !!(title && (edit || true));
            }"""
        )
        if ready == 'failed':
            raise RuntimeError("media upload was rejected by the page")
        if ready:
            return True
        await asyncio.sleep(1.5)
    return False


async def _set_field(page, label_frag: str, value: str) -> str | None:
    """Type `value` into the field whose placeholder/aria-label matches the
    `label_frag` regex (case-insensitive, bilingual en/zh). Returns the
    resulting value, or None.
    """
    el = await page.evaluate_handle(
        """(frag) => {
            const re = new RegExp(frag, 'i');
            const fields = [...document.querySelectorAll(
                'input[type=text], input:not([type]), textarea, '
                + '[contenteditable="true"]'
            )].filter(f => f.offsetParent !== null);
            const labelText = f => {
                let t = (f.getAttribute('placeholder') || '') + ' '
                    + (f.getAttribute('aria-label') || '') + ' '
                    + (f.id || '') + ' '
                    + (f.getAttribute('name') || '');
                if (f.id) {
                    const lb = document.querySelector(
                        'label[for="' + f.id + '"]'
                    );
                    if (lb) t += ' ' + lb.textContent;
                }
                const wrap = f.closest('label');
                if (wrap) t += ' ' + wrap.textContent;
                const al = f.getAttribute('aria-labelledby');
                if (al) {
                    const el = document.getElementById(al);
                    if (el) t += ' ' + el.textContent;
                }
                return t;
            };
            return fields.find(f => re.test(labelText(f))) || null;
        }""",
        label_frag,
    )
    el = el.as_element() if el else None
    if not el:
        return None
    await human_click(page, el)
    await el.evaluate("e => { try { e.select(); } catch(_) {} }")
    await human_type(page, value)
    return await el.evaluate(
        "e => e.value !== undefined ? e.value : e.innerText"
    )


_DESC_CSS = ('[contenteditable="true"][data-test-id*="description" i], '
             '[contenteditable="true"][aria-label*="Pin 图" i], '
             '[contenteditable="true"][aria-label*="pin is about" i], '
             '.public-DraftEditor-content, [contenteditable="true"]')


async def _set_description(page, desc: str) -> int:
    """Type the description — textarea or contenteditable. Returns char count.

    The builder's description is a Lexical editor: keystroke events get
    mangled (letters dropped, digits/punct land). `keyboard.insert_text`
    fires a proper beforeinput/insertText the editor accepts.
    """
    kind = await page.evaluate(
        """() => {
            const ta = [...document.querySelectorAll('textarea')].find(f =>
                /pin is about|description|描述你的 Pin/i.test(
                    (f.getAttribute('placeholder') || '')
                    + (f.getAttribute('aria-label') || '')
                )
            );
            if (ta) return 'textarea';
            const ce = document.querySelector(`""" + _DESC_CSS.replace(
            "'", '"') + """`);
            return ce ? 'editable' : null;
        }"""
    )
    if kind == 'textarea':
        val = await _set_field(
            page, "pin is about|description|描述你的 Pin", desc
        )
        return len(val or "")
    if kind == 'editable':
        ed = await page.query_selector(_DESC_CSS)
        # contenteditable supports fill — it select-all + insertTexts,
        # which Lexical accepts (raw keystrokes get mangled).
        try:
            await human_click(page, ed)
        except Exception:
            pass
        try:
            await ed.fill(desc)
        except Exception:
            pass
        n = await ed.evaluate("e => (e.innerText || '').trim().length")
        if n == 0:
            # fallback: focus + execCommand insertText in page context
            n = await page.evaluate(
                """(o) => {
                    const ed = document.querySelector(o.css);
                    if (!ed) return -1;
                    ed.focus();
                    window.getSelection().selectAllChildren(ed);
                    document.execCommand('delete');
                    document.execCommand('insertText', false, o.t);
                    return (ed.innerText || '').trim().length;
                }""",
                {"css": _DESC_CSS.replace("'", '"'), "t": desc},
            )
        await asyncio.sleep(human_delay(300, 700))
        return n
    return -1


async def _pick_board(page, board: str, timeout_s: float = 20) -> bool:
    """Open the board picker and select `board` by name."""
    opener = await _find_el(
        page,
        """() => {
            const b = document.querySelector(
                '[data-test-id=board-dropdown-select-button]');
            if (b) return b;
            const re = /choose a board|choose board|save to|选择.*图板|图板/i;
            // bare /图板/ also matches the left-nav "你的图板" <a> — links
            // and nav items must never be the click target
            return [...document.querySelectorAll(
                'button, [role=button], [role=combobox], [data-test-id*=board]'
            )].find(e => e.tagName !== 'A'
                         && !e.closest('nav, aside, [data-test-id$="-tab"]')
                         && (re.test((e.textContent || '').trim())
                             || re.test(e.getAttribute('aria-label') || '')))
                || null;
        }""",
    )
    if not await _click_el(page, opener):
        return False
    await asyncio.sleep(human_delay(800, 1500))

    # If the board dropdown has its own search field, type the name into
    # it. The input MUST live inside the dropdown/board panel — the page's
    # global site search box also matches /search|搜索/ and typing there
    # pops site suggestions that navigate away when clicked.
    search = await _find_el(
        page,
        """() => {
            const re = /search|搜索/i;
            return [...document.querySelectorAll(
                'input[type=text], input:not([type]), input[type=search]'
            )].filter(f => f.offsetParent !== null
                && f.id !== 'searchBoxInput'
                && !f.closest('header, nav')
                && f.closest(
                    '[data-test-id*=board], [role=dialog], [class*=dropdown]'
                )
                && re.test(
                    (f.getAttribute('placeholder') || '')
                    + (f.getAttribute('aria-label') || '')
                ))[0] || null;
        }""",
    )
    if search:
        await human_click(page, search)
        await human_type(page, board)
    await asyncio.sleep(human_delay(800, 1500))

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if "pin-creation-tool" not in page.url:
            return False  # something navigated — stop clicking around
        target = await _find_el(
            page,
            """(board) => {
                const norm = s => (s || '').trim().toLowerCase();
                // only pick inside an open listbox/dropdown — page-wide
                // [role=option] can match the site-search typeahead
                const scope = document.querySelector('[role=listbox]')
                    || document.querySelector('[data-test-id*=board]')
                    || document;
                const items = [...scope.querySelectorAll(
                    '[role=option], [role=listbox] *, [data-test-id*=board] *'
                )].filter(e => e.offsetParent !== null);
                const exact = items.find(e =>
                    norm(e.textContent) === norm(board));
                return exact || items.find(e =>
                    norm(e.textContent).includes(norm(board))) || null;
            }""",
            board,
        )
        if target:
            try:
                if await _click_el(page, target):
                    await asyncio.sleep(human_delay(500, 1000))
                    return True
            except Exception:
                pass  # stale handle — re-find next poll
        await asyncio.sleep(1)
    return False


async def _expand_more_options(page, timeout_s: float = 10) -> bool:
    """Open the 更多选项 section (alt text, comments, similar products)."""
    already = await page.evaluate(
        "() => !!document.getElementById('storyboardAltText')"
    )
    if already:
        return True
    btn = await page.query_selector(
        '[data-test-id=storyboard-show-more-options-button]'
    )
    if not await _click_el(page, btn):
        return False
    try:
        await page.wait_for_selector(
            "#storyboardAltText", timeout=int(timeout_s * 1000)
        )
        return True
    except Exception:
        return False


async def _set_switch(page, css: str, want: bool) -> bool | None:
    """Set a checkbox/switch to `want` via a real click. Returns new state,
    None if absent. Pinterest's toggles are skinned inputs — if the input
    itself has no click area, click its label/wrapper instead."""
    el = await page.query_selector(css)
    if not el:
        return None
    if bool(await el.evaluate("e => e.checked")) == want:
        return want
    target = el
    box = await el.bounding_box()
    if not box or box["width"] < 3 or box["height"] < 3:
        h = await el.evaluate_handle(
            "e => e.closest('label') || e.parentElement || e"
        )
        target = h.as_element() or el
    try:
        await _click_el(page, target)
    except Exception:
        pass
    await asyncio.sleep(human_delay(250, 600))
    state = bool(await el.evaluate("e => e.checked"))
    if state != want:
        try:
            await el.click(timeout=3000)
            await asyncio.sleep(human_delay(200, 500))
            state = bool(await el.evaluate("e => e.checked"))
        except Exception:
            pass
    return state


async def _set_alt_text(page, text: str) -> int:
    """Fill #storyboardAltText. Returns chars written, -1 if absent."""
    el = await page.query_selector("#storyboardAltText")
    if not el:
        return -1
    await human_click(page, el)
    await el.evaluate("e => { try { e.select(); } catch(_) {} }")
    await human_type(page, text)
    val = await el.evaluate("e => e.value || ''")
    return len(val)


async def _set_interest_tags(page, tags: list[str], timeout_s: float = 8) -> int:
    """Type each topic into the interest-tags combobox and pick a suggestion.

    The combobox only accepts picking from its dropdown — free text
    doesn't stick. Returns the number of topics added.
    """
    box = await page.query_selector("#combobox-storyboard-interest-tags")
    if not box:
        return 0
    added = 0
    for tag in tags:
        tag = tag.strip()
        if not tag:
            continue
        await human_click(page, box)
        await page.keyboard.press("ControlOrMeta+a")
        await page.keyboard.press("Backspace")
        await human_type(page, tag)
        # let the suggestion list refresh — before it does, the dropdown
        # still shows default recommendations unrelated to the tag
        await asyncio.sleep(human_delay(700, 1200))
        picked = False
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if "pin-creation-tool" not in page.url:
                return added  # navigated away — stop before more damage
            # Strict match only: never fall back to opts[0]. A stale or
            # unrelated first suggestion is how a tag like "beijing" ends
            # up pinning "motorcycles".
            target = await _find_el(
                page,
                """(t) => {
                    const norm = s => (s || '').trim().toLowerCase();
                    const opts = [...document.querySelectorAll(
                        '[role=option], [role=listbox] [role=option]'
                    )].filter(e => e.offsetParent !== null);
                    if (!opts.length) return null;
                    const nt = norm(t);
                    return opts.find(e => norm(e.textContent) === nt)
                        || opts.find(e => norm(e.textContent).includes(nt))
                        || opts.find(e => nt.includes(norm(e.textContent)))
                        || null;
                }""",
                tag,
            )
            if target:
                try:
                    if await _click_el(page, target):
                        picked = True
                        break
                except Exception:
                    pass  # stale handle — the list re-rendered; re-find
            await asyncio.sleep(0.5)
        if picked:
            added += 1
            await asyncio.sleep(human_delay(400, 900))
        else:
            # no suggestion matched the typed tag — close the dropdown and
            # clear the box instead of pinning an unrelated topic
            await page.keyboard.press("Escape")
            await human_click(page, box)
            await page.keyboard.press("ControlOrMeta+a")
            await page.keyboard.press("Backspace")
            await asyncio.sleep(human_delay(300, 600))
            log.warning(f"interest tag skipped, no matching option: {tag}")
    return added


async def _set_ai_disclosure(page, ai_modified: bool, ai_people: bool) -> dict:
    """Check/uncheck the AI-content disclosure checkboxes (dynamic ids)."""
    main_css = ('input[id^="pin-draft-ai-disclosure"]'
                ':not([id$="-synthetic-performer"])')
    people_css = ('input[id^="pin-draft-ai-disclosure"]'
                  '[id$="-synthetic-performer"]')
    out = {"ai_modified": await _set_switch(page, main_css, ai_modified)}
    if ai_modified or ai_people:
        # the sub-checkbox is only enabled when the main one is on
        await asyncio.sleep(human_delay(300, 700))
        out["ai_people"] = await _set_switch(
            page, people_css, ai_people
        )
    return out


async def _add_product_tag(page, product: str, timeout_s: float = 20) -> bool:
    """Attach a product tag via the picker sidebar.

    `product` is a URL (→ 使用链接 tab) or a search term (→ 搜索 Pin 图
    tab). The first picker result is selected — the human reviewing the
    Chrome window can reposition/remove the sticker before publishing.
    """
    btn = await page.query_selector(
        '[data-test-id=add-product-tags-button]'
    )
    if not await _click_el(page, btn):
        return False
    await asyncio.sleep(human_delay(800, 1500))

    if product.startswith(("http://", "https://")):
        tab = await _find_el(
            page,
            """() => {
                return [...document.querySelectorAll(
                    'button, [role=button], [role=tab]')]
                    .find(e => /use link|使用链接|by url/i.test(
                        ((e.textContent || '') + ' '
                         + (e.getAttribute('aria-label') || '')).trim()))
                    || null;
            }""",
        )
        await _click_el(page, tab)
        await asyncio.sleep(human_delay(500, 1000))
        inp = await _find_el(
            page,
            """() => {
                return [...document.querySelectorAll(
                    '[id*=asset-picker] input:not([type=hidden]), '
                    + '[data-test-id*=asset-picker] input:not([type=hidden])'
                )].find(i => i.offsetParent !== null) || null;
            }""",
        )
        if not inp:
            return False
        await human_click(page, inp)
        await human_type(page, product)
        await page.keyboard.press("Enter")
    else:
        el = await page.query_selector(
            "#storyboard-product-tags-asset-picker--search-pins-tab"
            "--search-field"
        ) or await page.query_selector(
            "[id*=asset-picker] input:not([type=hidden])"
        )
        if not el:
            return False
        await human_click(page, el)
        await human_type(page, product)
        await page.keyboard.press("Enter")

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        item = await _find_el(
            page,
            """() => {
                const scope = document.querySelector(
                    '[id*=asset-picker], [data-test-id*=asset-picker]')
                    || document;
                const items = [...scope.querySelectorAll(
                    '[role=option], [role=listbox] *, button, '
                    + '[role=button], li'
                )].filter(e => e.offsetParent !== null
                            && e.querySelector('img'));
                return items[0] || null;
            }""",
        )
        if item:
            try:
                if await _click_el(page, item):
                    await asyncio.sleep(human_delay(800, 1500))
                    return True
            except Exception:
                pass
        await asyncio.sleep(1)
    return False


async def _prepublish_check(page) -> dict:
    """Self-check before publishing: which fields got set."""
    return await page.evaluate(
        """() => {
            const grab = frag => {
                const re = new RegExp(frag, 'i');
                const f = [...document.querySelectorAll(
                    'input[type=text], input:not([type]), textarea'
                )].filter(x => x.offsetParent !== null)
                  .find(x => re.test(
                    (x.getAttribute('placeholder') || '')
                    + ' ' + (x.getAttribute('aria-label') || '')
                ));
                return f ? (f.value || f.innerText || null) : null;
            };
            const chk = css => {
                const e = document.querySelector(css);
                return e ? !!e.checked : null;
            };
            const byid = id => {
                const e = document.getElementById(id);
                return e ? (e.value || null) : null;
            };
            const body = document.body ? document.body.innerText : '';
            const descEd = [...document.querySelectorAll(
                '[contenteditable="true"], textarea')]
                .find(f => f.offsetParent !== null
                        && /pin is about|描述你的|description/i.test(
                            (f.getAttribute('aria-label') || '') + ' '
                            + (f.getAttribute('placeholder') || '')));
            const m = body.match(
                /(?:标记主题|tagged topics)[（(](\\d+)[)）]/i);
            const btns = [...document.querySelectorAll(
                'button, [role=button]')];
            const pub = btns.find(b =>
                /^(publish|发布)/i.test((b.textContent || '').trim()));
            const board = btns.find(b =>
                (b.getAttribute('data-test-id') || '')
                    === 'board-dropdown-select-button');
            return {
                title: grab('title|标题|简要描述'),
                description: descEd
                    ? ((descEd.innerText !== undefined
                            ? descEd.innerText : descEd.value) || '')
                        .trim().slice(0, 80)
                    : null,
                link: grab('destination link|链接|添加链接'),
                board: board
                    ? (board.textContent || '').trim().slice(0, 60) : null,
                topics: m ? parseInt(m[1], 10) : null,
                alt_text: byid('storyboardAltText'),
                comments: chk('#CommentSwitch'),
                similar_products: chk('#stelaSwitch'),
                ai_modified: chk('input[id^="pin-draft-ai-disclosure"]'
                                 + ':not([id$="-synthetic-performer"])'),
                ai_people: chk('input[id$="-synthetic-performer"]'),
                product_tags: document.querySelectorAll(
                    '[data-test-id*=product-tag]'
                    + ':not([data-test-id=add-product-tags-button])'
                ).length,
                publish_found: !!pub,
                publish_disabled: pub ? !!pub.disabled : null,
            };
        }"""
    )


async def _dismiss_popups(page) -> None:
    """Close onboarding/feature-tip modals that intercept clicks."""
    el = await _find_el(
        page,
        """() => {
            const wants = ['got it', 'ok', 'okay', 'close', 'not now',
                           'maybe later', 'dismiss', 'done',
                           '知道了', '好的', '关闭', '以后再说', '暂不',
                           '完成', '了解'];
            for (const b of document.querySelectorAll(
                'button, [role=button], [class*=close]'
            )) {
                const t = (b.textContent || '').trim().toLowerCase();
                if (wants.includes(t) && b.offsetParent !== null) {
                    return b;
                }
            }
            return document.querySelector(
                '[aria-label*=close i], [aria-label*=关闭 i], '
                + '[class*=modal] [class*=close], '
                + '[class*=dialog] [class*=close]'
            );
        }""",
    )
    if el:
        try:
            await _click_el(page, el)
        except Exception:
            pass


async def _click_publish(page) -> str | None:
    """Click Publish with the real mouse — no shadow tricks needed."""
    btn = await _find_el(
        page,
        """() => {
            return [...document.querySelectorAll('button, [role=button]')]
                .find(b => /^(publish|发布)/i.test(
                               (b.textContent || '').trim())
                           && !b.disabled) || null;
        }""",
    )
    if await _click_el(page, btn):
        return "mouse"
    return None


async def _wait_published(page, timeout_s: float = 30) -> bool:
    """Success = left the builder URL, or a published toast/URL appears."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if "pin-creation-tool" not in page.url and "pin-builder" not in page.url:
            return True
        try:
            ok = await page.evaluate(
                """() => {
                    const t = document.body ? document.body.innerText || '' : '';
                    return /pin published|your pin is live|published!|已发布|发布成功|已上线/i.test(t);
                }"""
            )
            if ok:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return False


async def _wait_publishable(page, timeout_s: float) -> bool:
    """Wait until the Publish button exists and is enabled."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        ok = await page.evaluate(
            """() => {
                const b = [...document.querySelectorAll(
                    'button, [role=button]')]
                    .find(b => /^(publish|发布)/i.test(
                                   (b.textContent || '').trim()));
                return b ? !b.disabled : false;
            }"""
        )
        if ok:
            return True
        await asyncio.sleep(2)
    return False


# ─── public entry point ─────────────────────────────────


async def publish_pin(
    session,
    kind: str,
    title: str,
    description: str,
    link: str,
    board: str,
    media_path: str,
    *,
    tags: list[str] | None = None,
    alt_text: str | None = None,
    comments: bool = True,
    similar_products: bool = True,
    ai_modified: bool = False,
    ai_people: bool = False,
    product: str = "",
    upload_timeout_s: float = 120,
    auto_click: bool = False,
    confirm_timeout_s: float = 900,
) -> dict:
    """Publish a pin: open builder → upload media → fill → Publish.

    `alt_text=None` defaults to the title. `comments`/`similar_products`
    default to True, matching Pinterest's own defaults — the toggles are
    always set explicitly so `--no-*` flags actually take effect.

    Default is manual-confirm: the form is filled and the human clicks
    Publish in the Chrome window (that click IS the approval). --auto
    attempts the click itself (the button is plain DOM, so it works).
    """
    page = session.page
    result: dict = {
        "success": False, "kind": kind, "title": title, "board": board,
    }

    await session.navigate(PIN_BUILDER_URL)
    if not await _wait_builder(page, timeout_s=120):
        try:
            await session._screenshot("builder_not_mounted")
        except Exception:
            pass
        result.update(error="builder_not_mounted", url=page.url)
        return result

    await _dismiss_popups(page)

    await _set_file(page, media_path)
    if not await _media_ready(page, timeout_s=upload_timeout_s):
        result.update(error="media_upload_timeout")
        return result
    await asyncio.sleep(human_delay(800, 1600))

    got_title = await _set_field(page, "title|标题|简要描述", title)
    if got_title != title:
        await asyncio.sleep(human_delay(400, 900))
        got_title = await _set_field(page, "title|标题|简要描述", title)
    result["title_set"] = got_title == title
    result["title_value"] = got_title
    await asyncio.sleep(human_delay(400, 900))

    desc_chars = await _set_description(page, description)
    result["description_chars"] = desc_chars
    result["description_set"] = desc_chars >= 0
    await asyncio.sleep(human_delay(400, 900))

    if link:
        got_link = await _set_field(
            page, "destination link|链接|添加链接", link
        )
        result["link_set"] = got_link == link
        await asyncio.sleep(human_delay(400, 900))

    if board:
        result["board_set"] = await _pick_board(page, board)
        if "pin-creation-tool" not in page.url:
            result.update(error="left_builder", url=page.url)
            try:
                result["screenshot"] = str(
                    await session._screenshot("left_builder")
                )
            except Exception:
                pass
            return result
        await asyncio.sleep(human_delay(400, 900))

    if tags:
        result["topics_set"] = await _set_interest_tags(page, tags)
        await asyncio.sleep(human_delay(400, 900))

    if product:
        result["product_set"] = await _add_product_tag(page, product)
        await asyncio.sleep(human_delay(400, 900))

    if ai_modified or ai_people:
        result["ai_disclosure"] = await _set_ai_disclosure(
            page, ai_modified or ai_people, ai_people
        )
        await asyncio.sleep(human_delay(400, 900))

    if await _expand_more_options(page):
        alt_chars = await _set_alt_text(
            page, alt_text if alt_text is not None else title
        )
        result["alt_text_chars"] = alt_chars
        result["alt_text_set"] = alt_chars >= 0
        await asyncio.sleep(human_delay(300, 700))
        result["comments"] = await _set_switch(
            page, "#CommentSwitch", comments
        )
        result["similar_products"] = await _set_switch(
            page, "#stelaSwitch", similar_products
        )
        await asyncio.sleep(human_delay(400, 900))
    else:
        result["more_options_expanded"] = False

    result["check"] = await _prepublish_check(page)
    print(f"pre-publish check: {result['check']}", file=sys.stderr)

    # ─── finish: manual-confirm by default ───
    if not await _wait_publishable(page, timeout_s=60):
        result.update(error="publish_button_stayed_disabled", url=page.url)
        try:
            result["screenshot"] = str(
                await session._screenshot("publish_disabled")
            )
        except Exception:
            pass
        return result

    await page.bring_to_front()
    await asyncio.sleep(human_delay(500, 1200))
    await _dismiss_popups(page)
    await asyncio.sleep(human_delay(400, 900))

    if not auto_click:
        print(
            "Pin form is filled — review it in the Chrome window and click "
            f"Publish (waiting, {int(confirm_timeout_s / 60)} min timeout)",
            file=sys.stderr,
        )
        if await _wait_published(page, timeout_s=confirm_timeout_s):
            result.update(success=True, via="manual-confirm", url=page.url)
        else:
            result.update(
                success=False,
                error="manual_confirm_timeout",
                detail="no Publish click before timeout — the filled form "
                       "was closed with the browser",
            )
        return result

    how = await _click_publish(page)
    if not how:
        result.update(error="publish_button_not_found")
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
