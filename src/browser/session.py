"""Browser session management for Reddit.

Handles login, cookie persistence, and session health checks.
"""

import asyncio
import json
from pathlib import Path

# Prefer Patchright — an API-compatible, undetected patch of Playwright. Reddit
# serves a "blocked by network security" page to vanilla Playwright once it does
# automated navigation (CDP traces are detectable); Patchright defeats that.
# Fall back to stock Playwright if Patchright isn't installed.
try:
    from patchright.async_api import async_playwright, Browser, BrowserContext, Page
    _USING_PATCHRIGHT = True
except ImportError:  # pragma: no cover
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page
    _USING_PATCHRIGHT = False

from src.browser.stealth import (
    apply_stealth_scripts,
    get_stealth_context_options,
    get_stealth_launch_args,
    human_click,
    human_delay,
    human_type,
)
from src.config import Config, DATA_DIR, SCREENSHOTS_DIR, prune_screenshots
from src.log import get_logger

log = get_logger("session")


class NetworkBlockedError(Exception):
    """Reddit served its "blocked by network security" 403 wall.

    Unlike SearchBlockedError (search endpoints are *always* blocked for
    automation), this is the velocity/rate block that hits feeds and threads
    when requests come too fast. It is transient — it clears after a pause —
    so navigate() backs off and retries before raising this.
    """


def _is_network_block(status: int | None, body_text: str) -> bool:
    """True if the page is Reddit's network-security 403 wall."""
    if status == 403 and "blocked by network security" in body_text.lower():
        return True
    # Some block responses come back 200 with the wall in the body.
    return "blocked by network security" in body_text.lower()

COOKIES_PATH = DATA_DIR / "cookies.json"

# Map browser-extension sameSite values (Cookie-Editor, Chrome export) to the
# three Playwright accepts. Playwright rejects anything else with a hard error.
_SAMESITE_MAP = {
    "no_restriction": "None",
    "unspecified": "Lax",
    "lax": "Lax",
    "strict": "Strict",
    "none": "None",
    "": "Lax",
}
# Keys Playwright's storage_state cookie schema understands. Extension exports
# carry extras (hostOnly, storeId, session, sameParty…) that must be dropped.
_PW_COOKIE_KEYS = {
    "name", "value", "domain", "path",
    "expires", "httpOnly", "secure", "sameSite",
}


def _normalize_cookies(raw: list[dict]) -> list[dict]:
    """Convert Cookie-Editor / browser-export cookies to Playwright's schema.

    Handles the two incompatibilities that crash new_context():
    - sameSite: extensions use no_restriction/unspecified/lax/strict; Playwright
      only accepts Strict|Lax|None.
    - expirationDate (float seconds) -> expires; and unknown keys are stripped.
    Session cookies (no expiry) are kept without an `expires` field.
    """
    out = []
    for c in raw:
        if not c.get("name") or "domain" not in c:
            continue
        cookie = {k: c[k] for k in _PW_COOKIE_KEYS if k in c}
        cookie["sameSite"] = _SAMESITE_MAP.get(
            str(c.get("sameSite", "")).lower(), "Lax"
        )
        # Extension exports use `expirationDate`; Playwright wants `expires`.
        if "expires" not in cookie and c.get("expirationDate") is not None:
            cookie["expires"] = float(c["expirationDate"])
        out.append(cookie)
    if not out:
        raise ValueError("no usable cookies after normalization")
    return out


class RedditSession:
    """Manages a browser session for Reddit interaction."""

    def __init__(self, config: Config):
        self.config = config
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        # True when launched on the system Google Chrome channel; drives whether
        # contexts spoof the UA (real Chrome must not — see start()).
        self._on_real_chrome: bool = False

    async def start(self, auto_login: bool = True) -> "RedditSession":
        """Launch browser and establish Reddit session.

        auto_login=False skips the scripted login — used by the `login`
        command, which opens the page and waits for a HUMAN to log in.
        """
        log.info("Starting browser session")
        self._playwright = await async_playwright().start()

        # Launch recipe verified to pass Reddit's "network security" block:
        # Patchright (undetected) + system Google Chrome, headful, and crucially
        # NONE of the classic stealth tells — no --disable-blink-features args,
        # no User-Agent override, no manual init scripts. Those are themselves
        # detectable; Patchright + real Chrome present a consistent human
        # fingerprint on their own. We fall back to stock-Playwright stealth
        # args only if we're not on Patchright.
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
                f"Chromium (Reddit may block it)"
            )
            self._on_real_chrome = False
            self._browser = await self._playwright.chromium.launch(
                **get_stealth_launch_args()
            )

        # On Patchright + real Chrome, keep the context minimal and native:
        # spoofing UA / injecting scripts here would re-introduce the tells.
        if _USING_PATCHRIGHT and self._on_real_chrome:
            context_options = {
                "viewport": {"width": 1440, "height": 900},
                "locale": "en-US",
            }
        else:
            context_options = get_stealth_context_options(
                spoof_user_agent=not self._on_real_chrome
            )

        # Load saved cookies if available
        if COOKIES_PATH.exists():
            try:
                raw_cookies = json.loads(COOKIES_PATH.read_text())
                cookies = _normalize_cookies(raw_cookies)
                context_options["storage_state"] = {"cookies": cookies, "origins": []}
                log.info(f"Loaded {len(cookies)} saved cookies")
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                log.warning(f"Failed to load cookies, starting fresh: {e}")

        self._context = await self._browser.new_context(**context_options)
        self._page = await self._context.new_page()
        # Only inject manual stealth scripts when NOT on Patchright (which does
        # its own, more thorough patching — our scripts would be a tell).
        if not _USING_PATCHRIGHT:
            await apply_stealth_scripts(self._page)

        # Check if we're already logged in
        if await self._is_logged_in():
            log.info("Session restored from cookies")
        elif auto_login:
            log.info("Need to log in")
            await self._login()

        return self

    async def _is_logged_in(self) -> bool:
        """Check if the current session is authenticated."""
        try:
            await self._page.goto("https://www.reddit.com", wait_until="domcontentloaded")
            await asyncio.sleep(human_delay(1000, 2000))

            # Check for logged-in indicators
            # Reddit shows username in the header when logged in
            logged_in = await self._page.evaluate("""
                () => {
                    // Check for login button (indicates NOT logged in)
                    const loginBtn = document.querySelector('[data-testid="login-button"]')
                        || document.querySelector('a[href*="login"]');
                    return !loginBtn;
                }
            """)
            return logged_in
        except Exception as e:
            log.warning(f"Login check failed: {e}")
            return False

    async def _login(self) -> None:
        """Log in to Reddit via the browser."""
        log.info(f"Logging in as u/{self.config.reddit_account.username}")

        await self._page.goto(
            "https://www.reddit.com/login", wait_until="domcontentloaded"
        )
        await asyncio.sleep(human_delay(1500, 3000))

        # Fill username
        username_input = await self._page.wait_for_selector(
            'input[name="username"], #loginUsername', timeout=10000
        )
        await human_click(self._page, username_input)
        await asyncio.sleep(human_delay(200, 500))
        await human_type(self._page, self.config.reddit_account.username)
        await asyncio.sleep(human_delay(300, 800))

        # Fill password
        password_input = await self._page.wait_for_selector(
            'input[name="password"], #loginPassword', timeout=5000
        )
        await human_click(self._page, password_input)
        await asyncio.sleep(human_delay(200, 500))
        await human_type(self._page, self.config.reddit_account.password)
        await asyncio.sleep(human_delay(500, 1000))

        # Click login button
        login_btn = await self._page.wait_for_selector(
            'button[type="submit"], button:has-text("Log In")', timeout=5000
        )
        await human_click(self._page, login_btn)

        # Wait for navigation / potential CAPTCHA
        await asyncio.sleep(human_delay(3000, 5000))

        # Reddit sometimes shows a "blocked by network security" banner but
        # leaves the form usable — a human would just click Log In again.
        # Retry once before falling into captcha handling.
        if "/login" in self._page.url and not await self._detect_captcha():
            try:
                retry_btn = await self._page.query_selector(
                    'button[type="submit"], button:has-text("Log In")'
                )
                if retry_btn and await retry_btn.is_visible():
                    log.info("Still on login page — retrying Log In click")
                    await human_click(self._page, retry_btn)
                    await asyncio.sleep(human_delay(4000, 6000))
            except Exception:
                pass

        # Check for CAPTCHA
        if await self._detect_captcha():
            log.warning("CAPTCHA detected during login")
            await self._handle_captcha()

        # Verify login succeeded
        if await self._is_logged_in():
            log.info("Login successful")
            await self._save_cookies()
        else:
            log.error("Login failed — may need manual intervention")
            await self._screenshot("login_failed")
            raise RuntimeError("Reddit login failed")

    async def _detect_captcha(self) -> bool:
        """Check if a CAPTCHA challenge is actually shown on the page.

        Reddit embeds hidden captcha iframes in the login page DOM even when no
        challenge is active, so we require the element to be visible.
        """
        captcha_selectors = [
            'iframe[src*="captcha"]',
            'iframe[src*="recaptcha"]',
            'iframe[src*="hcaptcha"]',
        ]
        for selector in captcha_selectors:
            try:
                for element in await self._page.query_selector_all(selector):
                    if await element.is_visible():
                        return True
            except Exception:
                continue
        return False

    async def _handle_captcha(self) -> None:
        """Attempt to solve a CAPTCHA using vision.

        Takes a screenshot, analyzes it, and tries to interact with the CAPTCHA.
        Falls back to alerting if it can't solve it.
        """
        log.info("Attempting to solve CAPTCHA")
        screenshot_path = await self._screenshot("captcha_challenge")

        # Try clicking through simple CAPTCHAs (checkbox type)
        try:
            checkbox = await self._page.query_selector(
                'iframe[src*="recaptcha"], iframe[src*="hcaptcha"]'
            )
            if checkbox:
                frame = await checkbox.content_frame()
                if frame:
                    check = await frame.query_selector(
                        '.recaptcha-checkbox, [id*="checkbox"]'
                    )
                    if check:
                        await check.click()
                        await asyncio.sleep(human_delay(2000, 4000))
                        log.info("Clicked CAPTCHA checkbox")
                        return
        except Exception as e:
            log.warning(f"CAPTCHA checkbox click failed: {e}")

        # If we can't solve it, take a screenshot and raise
        log.error(
            f"Cannot solve CAPTCHA automatically. Screenshot saved: {screenshot_path}"
        )
        # In the future, this could call a CAPTCHA solving service
        # or use Claude's vision to analyze the image
        raise RuntimeError("CAPTCHA requires manual intervention")



    async def _save_cookies(self) -> None:
        """Save current cookies for session reuse."""
        cookies = await self._context.cookies()
        COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
        COOKIES_PATH.write_text(json.dumps(cookies, indent=2))
        log.info("Cookies saved")

    async def _screenshot(self, name: str) -> Path:
        """Take a screenshot for debugging."""
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        from datetime import datetime

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        path = SCREENSHOTS_DIR / f"{name}_{timestamp}.png"
        await self._page.screenshot(path=str(path))
        log.info(f"Screenshot saved: {path}")
        # Keep the screenshot dir bounded so a long unattended run can't fill disk.
        prune_screenshots()
        return path

    @property
    def page(self) -> Page:
        """Get the current page."""
        if not self._page:
            raise RuntimeError("Session not started")
        return self._page

    async def navigate(self, url: str, *, retries: int = 2,
                       wait_until: str = "domcontentloaded"):
        """Navigate to a URL, recovering from Reddit's transient 403 rate-block.

        Reddit serves a "blocked by network security" 403 when requests arrive
        too fast. It clears after a pause, so on a block we back off with
        exponential jitter and retry. Only after exhausting retries do we raise
        NetworkBlockedError — letting the caller end the pass cleanly so the
        NEXT scheduled pass (after the block clears) can resume. Returns the
        Playwright response.
        """
        page = self.page
        last_resp = None
        for attempt in range(retries + 1):
            last_resp = await page.goto(url, wait_until=wait_until)
            await asyncio.sleep(human_delay(1500, 3000))
            status = last_resp.status if last_resp else None
            try:
                body_head = await page.evaluate(
                    "() => (document.body ? document.body.innerText : '').slice(0, 200)"
                )
            except Exception:
                body_head = ""
            if not _is_network_block(status, body_head):
                return last_resp
            if attempt < retries:
                # Exponential backoff with jitter: ~8s, ~20s. A short, polite
                # pause is usually enough to clear a velocity block.
                backoff = (8 * (2.5 ** attempt)) + human_delay(0, 4000)
                log.warning(
                    f"Network-security block on {url} (status={status}); "
                    f"backing off {backoff:.0f}s (attempt {attempt + 1}/{retries})"
                )
                await asyncio.sleep(backoff)
        raise NetworkBlockedError(
            f"Reddit network-security block persisted for {url} after {retries} retries"
        )

    @property
    def context(self) -> BrowserContext:
        """Get the browser context."""
        if not self._context:
            raise RuntimeError("Session not started")
        return self._context

    async def new_incognito_page(self) -> Page:
        """Create a new page in a fresh context (no cookies).

        Used for shadowban checking — view comments as a logged-out user.
        """
        if _USING_PATCHRIGHT and self._on_real_chrome:
            context = await self._browser.new_context(
                viewport={"width": 1440, "height": 900}, locale="en-US"
            )
        else:
            context = await self._browser.new_context(
                **get_stealth_context_options(spoof_user_agent=not self._on_real_chrome)
            )
        page = await context.new_page()
        if not _USING_PATCHRIGHT:
            await apply_stealth_scripts(page)
        return page

    async def is_healthy(self) -> bool:
        """Quick health check — are we still logged in?"""
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
        log.info("Browser session closed")
