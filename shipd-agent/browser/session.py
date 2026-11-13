# Lightweight Playwright lifecycle for low-resource background runs.

from __future__ import annotations

import gc
import os
import signal
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
)

DEFAULT_PAGE_TIMEOUT_MS = 20_000
DEFAULT_NAVIGATION_TIMEOUT_MS = 30_000

# Skip images, fonts, and media — enough for Shipd UI automation, much lighter.
_BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})

# Chromium flags tuned for headless background use on Apple Silicon Macs.
LIGHTWEIGHT_CHROMIUM_ARGS = (
    "--disable-dev-shm-usage",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-sync",
    "--disable-translate",
    "--disable-notifications",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--disable-features=TranslateUI,BlinkGenPropertyTrees",
    "--metrics-recording-only",
    "--no-first-run",
    "--mute-audio",
)

DEFAULT_PROCESS_NICE = 10
DEFAULT_WATCH_INTERVAL_SEC = 60


def background_priority(*, nice: int | None = None) -> None:
    """Lower CPU priority so daily work stays responsive (macOS/Linux)."""
    if nice is None:
        raw = os.getenv("PROCESS_NICE", str(DEFAULT_PROCESS_NICE)).strip()
        nice = int(raw) if raw else DEFAULT_PROCESS_NICE
    if nice <= 0:
        return
    try:
        os.nice(nice)
    except (OSError, ValueError):
        pass


def configure_lightweight_page(page: Page) -> None:
    """Block heavy assets; automation only needs DOM and scripts."""

    def handle_route(route) -> None:
        if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
            route.abort()
        else:
            route.continue_()

    page.route("**/*", handle_route)


def launch_lightweight_browser(
    playwright: Playwright,
    *,
    headless: bool = True,
):
    """Launch Chromium with resource-friendly defaults."""
    args = list(LIGHTWEIGHT_CHROMIUM_ARGS)
    if headless:
        args.append("--disable-gpu")
    return playwright.chromium.launch(headless=headless, args=args)


def create_browser_context(
    browser: Browser,
    *,
    auth_state_path: Path,
) -> BrowserContext:
    """Restore session state and use a modest viewport to save memory."""
    context_kwargs: dict = {
        "viewport": {"width": 1024, "height": 768},
        "device_scale_factor": 1,
    }
    if auth_state_path.exists():
        context_kwargs["storage_state"] = str(auth_state_path)
    return browser.new_context(**context_kwargs)


def release_browser(
    *,
    page: Page | None = None,
    context: BrowserContext | None = None,
    browser: Browser | None = None,
) -> None:
    """Close Playwright objects in order and encourage memory reclamation."""
    if page is not None:
        try:
            page.close()
        except Exception:
            pass
    if context is not None:
        try:
            context.close()
        except Exception:
            pass
    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass
    gc.collect()


@dataclass
class BrowserSession:
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page


@contextmanager
def managed_browser(
    *,
    headless: bool = True,
    auth_state_path: Path,
    lightweight: bool = True,
) -> Iterator[BrowserSession]:
    """Open one page/context/browser; always tear down on exit."""
    playwright = sync_playwright().start()
    browser: Browser | None = None
    context: BrowserContext | None = None
    page: Page | None = None
    try:
        browser = launch_lightweight_browser(playwright, headless=headless)
        context = create_browser_context(browser, auth_state_path=auth_state_path)
        page = context.new_page()
        page.set_default_timeout(DEFAULT_PAGE_TIMEOUT_MS)
        page.set_default_navigation_timeout(DEFAULT_NAVIGATION_TIMEOUT_MS)
        if lightweight:
            configure_lightweight_page(page)
        yield BrowserSession(
            playwright=playwright,
            browser=browser,
            context=context,
            page=page,
        )
    finally:
        release_browser(page=page, context=context, browser=browser)
        playwright.stop()


class ShutdownWatcher:
    """Cooperative shutdown for long-running watch loops."""

    def __init__(self) -> None:
        self.requested = False
        self._previous: dict[int, Any] = {}

    def install(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            self._previous[sig] = signal.getsignal(sig)
            signal.signal(sig, self._handle)

    def restore(self) -> None:
        for sig, handler in self._previous.items():
            signal.signal(sig, handler)

    def _handle(self, signum: int, _frame) -> None:
        self.requested = True

    def sleep(self, seconds: float) -> bool:
        """Sleep in 1s slices. Returns True when shutdown was requested."""
        if seconds <= 0:
            return self.requested
        deadline = time.monotonic() + seconds
        while not self.requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(1.0, remaining))
        return self.requested
