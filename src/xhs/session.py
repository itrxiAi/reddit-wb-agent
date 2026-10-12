"""Browser session for the Xiaohongshu creator platform.

Same launch recipe as the reddit session: Patchright (undetected Playwright
patch) + the system Google Chrome, headful — XHS signature-gates its API and
watches automation fingerprints, so vanilla bundled Chromium is a risk.

There is NO scripted login: XHS auth is QR-scan / SMS code, which only a human
can complete. `xhs-agent login` opens the login page and waits for the human;
alternatively export cookies from the user's own browser into
data/xhs/cookies.json (Cookie-Editor JSON format is normalized automatically).
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path

try:
    from patchright.async_api import async_playwright, Browser, BrowserContext, Page
    _USING_PATCHRIGHT = True
except ImportError:  # pragma: no cover
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page
    _USING_PATCHRIGHT = False

from src.browser.session import _normalize_cookies
from src.browser.stealth import (
    apply_stealth_scripts,
    get_stealth_context_options,
    get_stealth_launch_args,
    human_delay,
)
from src.log import get_logger
from src.xhs.config import (
    LOGIN_URL,
    PUBLISH_URL,
    XHS_COOKIES_PATH,
    XHS_SCREENSHOTS_DIR,
    XhsConfig,
    prune_screenshots,
)

log = get_logger("xhs.session")


class LoginRequiredError(Exception):
    """XHS redirected us to the login page — session cookies are dead/absent."""


class VerificationRequiredError(Exception):
    """XHS showed an anti-bot verification (滑块/安全验证) page."""


def _is_login_url(url: str) -> bool:
    return "/login" in (url or "")


async def _check_verification(page) -> None:
    """Raise VerificationRequiredError if a captcha/verify wall is showing."""
    try:
        text = await page.evaluate(
            "() => (document.body ? document.body.innerText : '').slice(0, 500)"
        )
    except Exception:
        return
    if "安全验证" in text or "拖动滑块" in text or "请完成验证" in text:
        raise VerificationRequiredError(
            "XHS showed a security-verification page — solve it in the open "
            "Chrome window, then retry the command"
        )


class XhsSession:
    """Manages a browser session for creator.xiaohongshu.com."""

    def __init__(self, config: XhsConfig):
        self.config = config
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._on_real_chrome: bool = False

    async def start(self, require_login: bool = True) -> "XhsSession":
        """Launch browser and establish the XHS session.

        require_login=False is used by `login`, which opens the page for a
        HUMAN to scan the QR code.
        """
        log.info("Starting XHS browser session")
        self._playwright = await async_playwright().start()

        launch_kwargs: dict = {"channel": "chrome", "headless": False}
        if not _USING_PATCHRIGHT:
            launch_kwargs.update(get_stealth_launch_args(channel="chrome"))
        try:
            self._browser = await self._playwright.chromium.launch(**launch_kwargs)
            self._on_real_chrome = True
            engine = "Patchright" if _USING_PATCHRIGHT else "Playwright"
            log.info(f"Launched system Google Chrome via {engine} (channel=chrome)")
        except Exception as e:
            log.warning(
                f"Chrome channel unavailable ({e}); falling back to bundled "
                f"Chromium (XHS may flag it)"
            )
            self._on_real_chrome = False
            self._browser = await self._playwright.chromium.launch(
                **get_stealth_launch_args()
            )

        # On Patchright + real Chrome keep the context minimal/native — extra
        # spoofing re-introduces the tells. XHS is a zh-CN site; matching that
        # locale is the consistent human fingerprint.
        if _USING_PATCHRIGHT and self._on_real_chrome:
            context_options = {
                "viewport": {"width": 1440, "height": 900},
                "locale": "zh-CN",
                "timezone_id": "Asia/Shanghai",
            }
        else:
            context_options = get_stealth_context_options(
                spoof_user_agent=not self._on_real_chrome
            )
            context_options["locale"] = "zh-CN"
            context_options["timezone_id"] = "Asia/Shanghai"

        if XHS_COOKIES_PATH.exists():
            try:
                raw_cookies = json.loads(XHS_COOKIES_PATH.read_text())
                cookies = _normalize_cookies(raw_cookies)
                context_options["storage_state"] = {
                    "cookies": cookies, "origins": [],
                }
                log.info(f"Loaded {len(cookies)} saved XHS cookies")
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                log.warning(f"Failed to load XHS cookies, starting fresh: {e}")

        self._context = await self._browser.new_context(**context_options)
        self._page = await self._context.new_page()
        if not _USING_PATCHRIGHT:
            await apply_stealth_scripts(self._page)

        if await self._is_logged_in():
            log.info("XHS session restored from cookies")
        elif require_login:
            raise LoginRequiredError(
                "Not logged in to Xiaohongshu. Run `xhs-agent login` and scan "
                "the QR code, or export cookies from your browser "
                "(Cookie-Editor → Export JSON) into data/xhs/cookies.json."
            )
        return self

    async def _is_logged_in(self) -> bool:
        """True if creator.xiaohongshu.com accepts the session.

        Unauthenticated visits to the creator platform are bounced to
        /login — a cheap, reliable signal.
        """
        try:
            await self._page.goto(PUBLISH_URL, wait_until="domcontentloaded")
            await asyncio.sleep(human_delay(1500, 3000))
            return not _is_login_url(self._page.url)
        except Exception as e:
            log.warning(f"XHS login check failed: {e}")
            return False

    async def _save_cookies(self) -> None:
        """Save current cookies for session reuse."""
        cookies = await self._context.cookies()
        XHS_COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
        XHS_COOKIES_PATH.write_text(json.dumps(cookies, indent=2))
        log.info("XHS cookies saved")

    async def _screenshot(self, name: str) -> Path:
        """Take a screenshot for debugging."""
        XHS_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        path = XHS_SCREENSHOTS_DIR / f"{name}_{timestamp}.png"
        await self._page.screenshot(path=str(path))
        log.info(f"Screenshot saved: {path}")
        prune_screenshots()
        return path

    @property
    def page(self) -> Page:
        if not self._page:
            raise RuntimeError("Session not started")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if not self._context:
            raise RuntimeError("Session not started")
        return self._context

    async def navigate(self, url: str, *, wait_until: str = "domcontentloaded"):
        """Navigate, then fail fast on the two XHS walls.

        Unlike reddit there is no transient rate-block worth retrying here —
        the blocks that matter are login expiry and the verification page,
        both surfaced as typed errors for the caller to report.
        """
        page = self.page
        resp = await page.goto(url, wait_until=wait_until)
        await asyncio.sleep(human_delay(1500, 3000))
        if _is_login_url(page.url):
            raise LoginRequiredError(
                "XHS redirected to login — session expired. Re-run "
                "`xhs-agent login` or refresh data/xhs/cookies.json."
            )
        await _check_verification(page)
        return resp

    async def is_healthy(self) -> bool:
        """Quick health check — still logged in?"""
        try:
            return await self._is_logged_in()
        except Exception:
            return False

    async def close(self) -> None:
        """Clean up browser resources."""
        if self._context:
            await self._save_cookies()
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        log.info("XHS browser session closed")
