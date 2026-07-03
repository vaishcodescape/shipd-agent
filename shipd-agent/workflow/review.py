# Reserve the next submission on the Shipd problem deck, open its review, and clone it.
 
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import (
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from auth import (
    AUTH_STATE_PATH,
    REPO_ROOT,
    REVIEWS_URL,
    AuthConfig,
    ensure_signed_in,
    goto_page,
    load_auth_config,
    managed_browser,
)
from workflow.cleanup import remove_clone_directory

# The problem deck. Defaults to the reviews page; override if the deck lives
# at a different path.
PROBLEM_DECK_URL = REVIEWS_URL

# Button/label text used to claim a submission. Order = preference.
RESERVE_BUTTON_NAMES = ("Reserve", "Reserve Submission", "Claim")

# Controls that open a reserved submission's review. Exact match only.
REVIEW_ENTRY_NAMES = ("Continue →", "Continue Review", "Open")

# Once reserved, how we know we've landed on the review view for the submission.
POST_RESERVE_TIMEOUT_MS = 10_000
REVIEW_READY_TIMEOUT_MS = 45_000
DECK_TIMEOUT_MS = 30_000

QUICK_SETUP_HEADING = "Quick Setup"
QUICK_SETUP_HEADING_RE = re.compile(r"Quick Setup", re.I)
VIEW_SCRIPT_BUTTON = "View Script"

SETUP_SCRIPT_PATTERN = re.compile(
    r"cat <<'EOSCRIPT' \| bash\n(.*?)\nEOSCRIPT",
    re.DOTALL,
)
CLONE_DIR_PATTERN = re.compile(r"^cd (\S+)\s*$", re.MULTILINE)
GIT_CLONE_DIR_PATTERN = re.compile(r"git clone \S+ (\S+)")


def wait_for_problem_deck(page: Page, *, timeout: int = DECK_TIMEOUT_MS) -> None:
    """Wait until the deck shows Reserve or Continue controls."""
    deck_ready = page.get_by_role("button", name="Continue →", exact=True).or_(
        page.get_by_role("button", name=re.compile(r"^Reserve", re.I))
    )
    deck_ready.first.wait_for(state="visible", timeout=timeout)


def find_reserve_button(page: Page) -> Locator:
    """Return the first visible, enabled Reserve button on the deck."""
    candidates = page.get_by_role("button", name=RESERVE_BUTTON_NAMES[0])
    for name in RESERVE_BUTTON_NAMES[1:]:
        candidates = candidates.or_(page.get_by_role("button", name=name))
    for i in range(candidates.count()):
        button = candidates.nth(i)
        if button.is_visible() and button.is_enabled():
            return button
    raise RuntimeError(
        "No available Reserve button found on the problem deck. "
        "Either nothing is available to reserve, or the button label "
        f"differs from {RESERVE_BUTTON_NAMES}."
    )


def is_challenge_review_page(page: Page) -> bool:
    return "/challenges/" in page.url


def _review_ready_locator(page: Page) -> Locator:
    """Signals that the challenge review view has loaded."""
    return (
        page.get_by_role("heading", name=QUICK_SETUP_HEADING_RE)
        .or_(page.get_by_text(QUICK_SETUP_HEADING_RE))
        .or_(page.get_by_role("button", name=VIEW_SCRIPT_BUTTON))
        .or_(page.get_by_role("heading", name=re.compile(r"Holistic Check", re.I)))
    )


def _review_signal_visible(page: Page) -> bool:
    locator = _review_ready_locator(page)
    if locator.count() == 0:
        return False
    target = locator.first
    if not target.is_visible():
        try:
            target.scroll_into_view_if_needed(timeout=2_000)
        except PlaywrightTimeoutError:
            return False
    return target.is_visible()


def is_review_ready(page: Page) -> bool:
    """True when the review UI is present and usable."""
    return _review_signal_visible(page)


def wait_for_review_ready(page: Page, *, timeout: int = REVIEW_READY_TIMEOUT_MS) -> None:
    """Wait until the review view loads, scrolling to reveal below-fold content."""
    deadline = time.monotonic() + timeout / 1000
    last_url = page.url
    signals = _review_ready_locator(page)

    while time.monotonic() < deadline:
        if _review_signal_visible(page):
            return

        try:
            page.wait_for_url("**/challenges/**", timeout=500, wait_until="commit")
        except PlaywrightTimeoutError:
            pass

        if page.url != last_url:
            last_url = page.url
            page.wait_for_load_state("domcontentloaded", timeout=5_000)

        try:
            signals.first.wait_for(state="attached", timeout=500)
            signals.first.scroll_into_view_if_needed(timeout=2_000)
            if signals.first.is_visible():
                return
        except PlaywrightTimeoutError:
            pass

        page.evaluate(
            "window.scrollBy(0, Math.max(window.innerHeight * 0.75, 400))"
        )
        page.wait_for_timeout(250)

    raise RuntimeError(
        "Review page did not show Quick Setup or other review sections "
        f"within {timeout // 1000}s. Current URL: {page.url}"
    )


def _post_reserve_signals(page: Page) -> Locator:
    """Locators that indicate reserve succeeded (review open or entry control)."""
    signals = _review_ready_locator(page).or_(
        page.get_by_role("button", name="Continue →", exact=True)
    )
    for name in REVIEW_ENTRY_NAMES[1:]:
        signals = signals.or_(page.get_by_role("button", name=name, exact=True))
        signals = signals.or_(page.get_by_role("link", name=name, exact=True))
    return signals


def wait_for_post_reserve(page: Page, *, timeout: int = POST_RESERVE_TIMEOUT_MS) -> None:
    """Wait until reserve settles — exits as soon as Continue or Quick Setup appears."""
    _post_reserve_signals(page).first.wait_for(state="visible", timeout=timeout)


def find_review_entry_control(page: Page) -> Locator | None:
    """Return the visible, enabled control that opens a reserved review."""
    # Exact names only — substring "Review" matches the sidebar "Review Queue" tab.
    continue_buttons = page.get_by_role("button", name="Continue →", exact=True)
    for i in range(continue_buttons.count() - 1, -1, -1):
        button = continue_buttons.nth(i)
        if button.is_visible() and button.is_enabled():
            return button

    for name in REVIEW_ENTRY_NAMES[1:]:
        for role in ("button", "link"):
            targets = page.get_by_role(role, name=name, exact=True)
            for i in range(targets.count()):
                target = targets.nth(i)
                if target.is_visible() and target.is_enabled():
                    return target
    return None


def _click_into_review(page: Page, control: Locator) -> None:
    """Click a deck entry control and wait for the challenge review route."""
    try:
        with page.expect_navigation(timeout=15_000, wait_until="commit"):
            control.click()
    except PlaywrightTimeoutError:
        control.click()
    wait_for_review_ready(page)


def _open_review_from_deck(page: Page) -> None:
    """Click through from the problem deck into the review view."""
    if is_review_ready(page):
        return

    if is_challenge_review_page(page):
        wait_for_review_ready(page)
        return

    control = find_review_entry_control(page)
    if control is None:
        raise RuntimeError(
            "Reserved a submission but could not find a control to open its "
            f"review. Expected one of {REVIEW_ENTRY_NAMES}."
        )

    _click_into_review(page, control)


def continue_existing_review(page: Page) -> bool:
    """Open an in-progress reservation when the deck shows Continue."""
    if "/reviews" not in page.url:
        goto_page(page, PROBLEM_DECK_URL)
    wait_for_problem_deck(page)
    continue_btn = page.get_by_role("button", name="Continue →", exact=True)
    if continue_btn.count() and continue_btn.first.is_visible():
        _click_into_review(page, continue_btn.first)
        print(f"Continued existing review: {page.url}")
        return True
    return False


def reserve_submission(page: Page) -> None:
    """Reserve the next available submission on the problem deck."""
    if "/reviews" not in page.url:
        goto_page(page, PROBLEM_DECK_URL)
    wait_for_problem_deck(page)

    find_reserve_button(page).click()
    wait_for_post_reserve(page)
    print("Reserved a submission.")


def open_review(page: Page) -> None:
    """Open the reserved submission's review view and leave it ready."""
    _open_review_from_deck(page)
    print(f"Review open: {page.url}")


def reserve_and_open_review(page: Page) -> None:
    """Reserve (or continue) and open the review in one pass."""
    if continue_existing_review(page):
        return

    if "/reviews" not in page.url:
        goto_page(page, PROBLEM_DECK_URL)
    wait_for_problem_deck(page)

    find_reserve_button(page).click()
    wait_for_post_reserve(page)
    print("Reserved a submission.")
    _open_review_from_deck(page)
    print(f"Review open: {page.url}")


def open_quick_setup(page: Page) -> None:
    """Scroll to Quick Setup and expand the setup script."""
    pre = page.locator("pre").first
    if pre.count() > 0 and pre.is_visible():
        script = pre.inner_text().strip()
        if script.startswith("cat <<'EOSCRIPT' | bash"):
            return

    view_script = page.get_by_role("button", name=VIEW_SCRIPT_BUTTON)
    if view_script.count() > 0:
        try:
            view_script.first.scroll_into_view_if_needed(timeout=2_000)
        except PlaywrightTimeoutError:
            pass
        if view_script.first.is_visible():
            view_script.first.click()
            pre.wait_for(state="visible", timeout=10_000)
            return

    heading = page.get_by_role("heading", name=QUICK_SETUP_HEADING_RE)
    if heading.count() == 0:
        heading = page.get_by_text(QUICK_SETUP_HEADING_RE)
    heading.first.wait_for(state="attached", timeout=15_000)
    heading.first.scroll_into_view_if_needed()
    heading.first.click()

    view_script.wait_for(state="visible", timeout=10_000)
    view_script.first.click()
    pre.wait_for(state="visible", timeout=10_000)


def extract_setup_script(page: Page) -> str:
    """Return the Quick Setup shell command from the expanded script block."""
    open_quick_setup(page)
    script_block = page.locator("pre").first.inner_text().strip()
    if not script_block.startswith("cat <<'EOSCRIPT' | bash"):
        raise RuntimeError(
            "Quick Setup script block did not match the expected format."
        )
    return script_block


def resolve_clone_directory(setup_script: str) -> str | None:
    match = CLONE_DIR_PATTERN.search(setup_script)
    if match:
        return match.group(1)
    match = GIT_CLONE_DIR_PATTERN.search(setup_script)
    if match:
        return match.group(1)
    inner = SETUP_SCRIPT_PATTERN.search(setup_script)
    if inner:
        match = GIT_CLONE_DIR_PATTERN.search(inner.group(1))
        if match:
            return match.group(1)
    return None


def clone_submission_locally(setup_script: str, *, clone_dir: Path) -> Path:
    """Run the Quick Setup script and return the cloned project directory."""
    clone_dir.mkdir(parents=True, exist_ok=True)
    target_name = resolve_clone_directory(setup_script)
    if target_name:
        target_path = clone_dir / target_name
        if target_path.exists():
            print(f"Removing stale clone target before Quick Setup: {target_path}")
            remove_clone_directory(target_path)

    print(f"Running Quick Setup in {clone_dir}...")
    subprocess.run(
        ["bash", "-c", setup_script],
        cwd=clone_dir,
        check=True,
    )

    if target_name:
        cloned = clone_dir / target_name
        if cloned.is_dir():
            print(f"Cloned submission to: {cloned}")
            return cloned

    raise RuntimeError(
        "Quick Setup finished but the cloned project directory was not found."
    )


def run_reserve_and_review(
    *,
    config: AuthConfig | None = None,
    headless: bool = True,
    auth_state_path: Path = AUTH_STATE_PATH,
    clone_dir: Path | None = None,
    clone: bool = True,
    quest: str = "olympus",
) -> Path | None:
    config = config or AuthConfig()
    auth_state_path.parent.mkdir(parents=True, exist_ok=True)

    with managed_browser(
        headless=headless,
        auth_state_path=auth_state_path,
        lightweight=headless,
    ) as session:
        page = session.page
        context = session.context

        ensure_signed_in(page, PROBLEM_DECK_URL, config, headed=not headless)
        context.storage_state(path=str(auth_state_path))

        reserve_and_open_review(page)
        context.storage_state(path=str(auth_state_path))

        cloned_path: Path | None = None
        if clone:
            setup_script = extract_setup_script(page)
            cloned_path = clone_submission_locally(
                setup_script,
                clone_dir=clone_dir or (REPO_ROOT / "submissions"),
            )

        from review.review_io import save_session_meta

        save_session_meta(
            review_url=page.url,
            quest=quest,
            repo_path=cloned_path,
        )
        print(f"Session meta saved (review_url={page.url}).")

        if not headless:
            print("Press Enter to close the browser...")
            input()

        return cloned_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Reserve the next available submission on the Shipd problem deck, "
            "open its review, and clone it locally via Quick Setup. "
            "Runs headless by default."
        ),
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Open a visible browser window (default is headless background mode).",
    )
    parser.add_argument(
        "--auth-state",
        type=Path,
        default=AUTH_STATE_PATH,
        help="Path to save/load Playwright auth state.",
    )
    parser.add_argument(
        "--clone-dir",
        type=Path,
        default=None,
        help=(
            "Directory where Quick Setup clones the submission "
            "(default: ./submissions or SUBMISSIONS_DIR from .env)."
        ),
    )
    parser.add_argument(
        "--no-clone",
        action="store_true",
        help="Open the review page without running Quick Setup locally.",
    )
    parser.add_argument(
        "--quest",
        choices=("olympus", "mars"),
        default="olympus",
        help="Quest name for session meta (default: olympus).",
    )
    return parser.parse_args()


def main() -> int:
    config = load_auth_config()

    submissions_dir = os.getenv("SUBMISSIONS_DIR", "").strip()
    default_clone_dir = Path(submissions_dir) if submissions_dir else REPO_ROOT / "submissions"

    args = parse_args()
    try:
        run_reserve_and_review(
            config=config,
            headless=not args.headed,
            auth_state_path=args.auth_state,
            clone_dir=args.clone_dir or default_clone_dir,
            clone=not args.no_clone,
            quest=args.quest,
        )
    except (
        PlaywrightTimeoutError,
        RuntimeError,
        ValueError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"Reserve/review failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
