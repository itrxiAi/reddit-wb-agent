"""Browser session for pinterest.com.

Same launch recipe as the other platforms: Patchright (undetected
Playwright patch) + the system Google Chrome, headful — Pinterest
fingerprint-checks automation, so vanilla bundled Chromium is a risk.

There is NO scripted login: Pinterest auth is email/password or OAuth —
only a human can complete it. `pinterest-agent login` opens the login
page and waits for the human; alternatively export cookies from the
user's own browser into data/pinterest/cookies.json (Cookie-Editor JSON
format is normalized automatically). The session cookie `_pinterest_sess`
lasts ~1 year.
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
from src.pinterest.config import (
    PIN_BUILDER_URL,
    PIN_COOKIES_PATH,
    PIN_SCREENSHOTS_DIR,
    PinConfig,
    prune_screenshots,
)

log = get_logger("pinterest.session")


class LoginRequiredError(Exception):
    """Pinterest bounced us to a login wall — cookies dead/absent."""


class VerificationRequiredError(Exception):
    """Pinterest showed a captcha/security challenge."""


def _is_login_url(url: str) -> bool:
    u = url or ""
    return "/login" in u or u.rstrip("/").endswith("pinterest.com")


async def _check_verification(page) -> None:
    """Raise VerificationRequiredError if a captcha wall is showing."""
    try:
        text = await page.evaluate(
            "() => (document.body ? document.body.innerText : '').slice(0, 500)"
        )
    except Exception:
        return
    t = text.lower()
    if "security check" in t or "verify you're" in t or "captcha" in t:
        raise VerificationRequiredError(
            "Pinterest showed a security-verification page — solve it in "
            "the open Chrome window, then retry the command"
        )


class PinSession:
    """Manages a browser session for pinterest.com."""

    def __init__(self, config: PinConfig):
        self.config = config
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._on_real_chrome: bool = False

    async def start(self, require_login: bool = True) -> "PinSession":
        """Launch browser and establish the Pinterest session.

        require_login=False is used by `login`, which opens the page for
        a HUMAN to sign in.
        """
        log.info("Starting Pinterest browser session")
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
                f"Chromium (Pinterest may flag it)"
            )
            self._on_real_chrome = False
            self._browser = await self._playwright.chromium.launch(
                **get_stealth_launch_args()
            )

        # Pinterest is an en-US site; matching locale is the consistent
        # human fingerprint. Patchright + real Chrome: keep context minimal.
        if _USING_PATCHRIGHT and self._on_real_chrome:
            context_options = {
                "viewport": {"width": 1440, "height": 900},
                "locale": "en-US",
                "timezone_id": "America/New_York",
            }
        else:
            context_options = get_stealth_context_options(
                spoof_user_agent=not self._on_real_chrome
            )
            context_options["locale"] = "en-US"
            context_options["timezone_id"] = "America/New_York"

        if PIN_COOKIES_PATH.exists():
            try:
                raw_cookies = json.loads(PIN_COOKIES_PATH.read_text())
                cookies = _normalize_cookies(raw_cookies)
                context_options["storage_state"] = {
                    "cookies": cookies, "origins": [],
                }
                log.info(f"Loaded {len(cookies)} saved Pinterest cookies")
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                log.warning(f"Failed to load Pinterest cookies, fresh start: {e}")

        self._context = await self._browser.new_context(**context_options)
        self._page = await self._context.new_page()
        # Proxied networks can take 15-25s just for the HTML — the default
        # 30s nav timeout false-negatives the login check.
        self._page.set_default_navigation_timeout(120_000)
        self._page.on(
            "requestfailed",
            lambda r: log.warning(
                f"request failed: {r.url[:150]} ({r.failure})"
            ),
        )
        if not _USING_PATCHRIGHT:
            await apply_stealth_scripts(self._page)

        if await self._is_logged_in():
            log.info("Pinterest session restored from cookies")
        elif require_login:
            raise LoginRequiredError(
                "Not logged in to Pinterest. Run `pinterest-agent login` "
                "and sign in in the Chrome window, or export cookies from "
                "your browser (Cookie-Editor → Export JSON) into "
                "data/pinterest/cookies.json."
            )
        return self

    async def _is_logged_in(self) -> bool:
        """True if the pin-creation-tool accepts the session.

        Unauthenticated visits bounce to /login/ — a cheap reliable signal.
        """
        try:
            await self._page.goto(PIN_BUILDER_URL, wait_until="domcontentloaded")
            await asyncio.sleep(human_delay(1500, 3000))
            return not _is_login_url(self._page.url)
        except Exception as e:
            log.warning(f"Pinterest login check failed: {e}")
            return False

    async def _save_cookies(self) -> None:
        """Save current cookies for session reuse."""
        cookies = await self._context.cookies()
        PIN_COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
        PIN_COOKIES_PATH.write_text(json.dumps(cookies, indent=2))
        log.info("Pinterest cookies saved")

    async def _screenshot(self, name: str) -> Path:
        """Take a screenshot for debugging."""
        PIN_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        path = PIN_SCREENSHOTS_DIR / f"{name}_{timestamp}.png"
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
        """Navigate, then fail fast on the login/verification walls."""
        page = self.page
        resp = await page.goto(url, wait_until=wait_until)
        await asyncio.sleep(human_delay(1500, 3000))
        if _is_login_url(page.url):
            raise LoginRequiredError(
                "Pinterest redirected to login — session expired. Re-run "
                "`pinterest-agent login` or refresh "
                "data/pinterest/cookies.json."
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
        log.info("Pinterest browser session closed")
