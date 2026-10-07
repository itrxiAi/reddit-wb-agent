"""Anti-detection configuration for the browser.

Uses Playwright launch options to minimize bot detection fingerprints.
For stronger stealth, consider nodriver or camoufox as drop-in replacements.
"""

import asyncio
import random

# Common user agents for Chrome on macOS/Windows
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

# Common viewport sizes
VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1366, "height": 768},
    {"width": 1280, "height": 720},
]

# Timezones to match user agents
TIMEZONES = [
    "America/New_York",
    "America/Chicago",
    "America/Los_Angeles",
    "America/Denver",
    "Europe/London",
]


def get_stealth_launch_args(channel: str | None = None) -> dict:
    """Get Playwright launch arguments that minimize detection.

    Reddit hard-blocks Playwright's BUNDLED Chromium fingerprint with a
    "network policy" 403 even from a clean residential IP. Launching the
    system-installed Google Chrome (channel="chrome") presents a genuine
    fingerprint that passes the gate. When a channel is given we pass it
    through; otherwise we fall back to bundled Chromium.
    """
    viewport = random.choice(VIEWPORTS)

    args: dict = {
        "headless": True,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-infobars",
            "--no-first-run",
            "--no-default-browser-check",
            f"--window-size={viewport['width']},{viewport['height']}",
        ],
    }
    if channel:
        args["channel"] = channel
    return args


def get_stealth_context_options(spoof_user_agent: bool = True) -> dict:
    """Get browser context options for stealth.

    When running on the real Chrome channel, set spoof_user_agent=False: an
    overridden UA string that disagrees with Chrome's automatically-sent
    Sec-CH-UA client hints is itself a strong bot signal (the inconsistency
    triggered Reddit's block in testing). Letting real Chrome present its own
    native, self-consistent UA passes the gate.
    """
    viewport = random.choice(VIEWPORTS)
    timezone = random.choice(TIMEZONES)

    options: dict = {
        "viewport": viewport,
        "locale": "en-US",
        "timezone_id": timezone,
        "permissions": ["geolocation"],
        "geolocation": {"longitude": -73.935242, "latitude": 40.730610},
        "color_scheme": "light",
        "java_script_enabled": True,
    }
    if spoof_user_agent:
        options["user_agent"] = random.choice(USER_AGENTS)
    return options


async def apply_stealth_scripts(page) -> None:
    """Inject JavaScript to mask automation indicators.

    This overrides common detection vectors:
    - navigator.webdriver
    - chrome.runtime
    - Permissions API
    - Plugin/language enumeration
    """
    await page.add_init_script("""
        // Remove webdriver flag
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined,
        });

        // Mock chrome runtime
        window.chrome = {
            runtime: {},
        };

        // Mock permissions
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) =>
            parameters.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : originalQuery(parameters);

        // Mock plugins
        Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5],
        });

        // Mock languages
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-US', 'en'],
        });

        // Mock hardware concurrency
        Object.defineProperty(navigator, 'hardwareConcurrency', {
            get: () => 8,
        });

        // Mock device memory
        Object.defineProperty(navigator, 'deviceMemory', {
            get: () => 8,
        });
    """)


def human_delay(min_ms: int = 500, max_ms: int = 2000) -> float:
    """Generate a human-like delay in seconds.

    Uses a slightly skewed distribution to mimic real typing/clicking patterns.
    """
    # Skew toward the lower end but occasionally be slow
    base = random.uniform(min_ms, max_ms)
    # 10% chance of a longer "thinking" pause
    if random.random() < 0.1:
        base *= random.uniform(1.5, 3.0)
    return base / 1000.0


def human_typing_delay() -> float:
    """Delay between keystrokes when typing."""
    return random.uniform(0.03, 0.12)


# ─── human input emulation ────────────────────────────
# Behavioral detection watches input *dynamics*, not just timing: real mice
# travel in curves and emit dozens of mousemove events, real wheels produce
# many small deltas, real typists burst and occasionally mistype. The helpers
# below turn every mechanical call into something that looks like a person.

_mouse_pos = (0.0, 0.0)


async def human_move(page, x: float, y: float) -> None:
    """Move the mouse to (x, y) along a jittered quadratic curve.

    Playwright's mouse.move teleports when called once; stepping through a
    bezier path emits the stream of mousemove events a real pointer makes.
    """
    global _mouse_pos
    x0, y0 = _mouse_pos
    dx, dy = x - x0, y - y0
    dist = max(1.0, (dx * dx + dy * dy) ** 0.5)

    # Control point displaced perpendicular-ish to the straight line.
    cx = (x0 + x) / 2 + random.uniform(-0.4, 0.4) * dy + random.uniform(-50, 50)
    cy = (y0 + y) / 2 - random.uniform(-0.4, 0.4) * dx + random.uniform(-50, 50)

    steps = max(10, min(60, int(dist / random.uniform(15, 30))))
    for i in range(1, steps + 1):
        t = i / steps
        # Ease-in-out so the pointer accelerates then decelerates on target.
        te = t * t * (3 - 2 * t)
        px = (1 - te) ** 2 * x0 + 2 * (1 - te) * te * cx + te * te * x
        py = (1 - te) ** 2 * y0 + 2 * (1 - te) * te * cy + te * te * y
        await page.mouse.move(
            px + random.uniform(-1.5, 1.5), py + random.uniform(-1.5, 1.5)
        )
        await asyncio.sleep(random.uniform(0.004, 0.018))
    _mouse_pos = (x, y)
    # Small hover/settle before whatever comes next.
    await asyncio.sleep(human_delay(60, 220))


async def human_click(page, element) -> None:
    """Move to a random point inside the element, then click via real mouse."""
    bbox = await element.bounding_box()
    if not bbox:
        await element.click()
        return
    x = bbox["x"] + bbox["width"] * random.uniform(0.25, 0.75)
    y = bbox["y"] + bbox["height"] * random.uniform(0.25, 0.75)
    await human_move(page, x, y)
    await page.mouse.click(x, y)


async def human_scroll(page, delta: int | None = None) -> None:
    """Scroll via real wheel events in many small steps, not one jump.

    window.scrollBy produces zero wheel/touch events — a classic bot tell.
    mouse.wheel emits the real event stream; humans scroll in short bursts
    of ~40-160px deltas.
    """
    if delta is None:
        delta = random.randint(400, 900)
    direction = 1 if delta >= 0 else -1
    remaining = abs(delta)
    while remaining > 0:
        step = min(remaining, random.randint(40, 160))
        await page.mouse.wheel(0, direction * step)
        remaining -= step
        await asyncio.sleep(random.uniform(0.02, 0.09))
    # Little pause while eyes catch up.
    await asyncio.sleep(human_delay(250, 800))


async def human_type(page, text: str) -> None:
    """Type with rhythm — pauses at word boundaries, rare typo + backspace."""
    for ch in text:
        if ch.isalpha() and random.random() < 0.006:
            await page.keyboard.type(
                random.choice("abcdefghijklmnopqrstuvwxyz"),
                delay=human_typing_delay() * 1000,
            )
            await asyncio.sleep(random.uniform(0.15, 0.45))
            await page.keyboard.press("Backspace")
            await asyncio.sleep(random.uniform(0.1, 0.3))
        await page.keyboard.type(ch, delay=human_typing_delay() * 1000)
        # Occasional longer pause between words/sentences.
        if ch in " .,!?" and random.random() < 0.08:
            await asyncio.sleep(random.uniform(0.3, 0.9))
