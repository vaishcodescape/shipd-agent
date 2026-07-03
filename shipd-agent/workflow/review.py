# Reserve the next submission on the Shipd problem deck, open its review, and clone it.
 
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
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

# The problem deck. Defaults to the reviews page; override if the deck lives
# at a different path.
PROBLEM_DECK_URL = REVIEWS_URL

# Button/label text used to claim a submission. Order = preference.
RESERVE_BUTTON_NAMES = ("Reserve", "Reserve Submission", "Claim")

# Once reserved, how we know we've landed on the review view for the submission.
REVIEW_URL_GLOB = "**/reviews/**"
CHALLENGE_REVIEW_URL_GLOB = "**/challenges/**"

QUICK_SETUP_HEADING = "Quick Setup"
VIEW_SCRIPT_BUTTON = "View Script"

SETUP_SCRIPT_PATTERN = re.compile(
    r"cat <<'EOSCRIPT' \| bash\n(.*?)\nEOSCRIPT",
    re.DOTALL,
)
CLONE_DIR_PATTERN = re.compile(r"^cd (\S+)\s*$", re.MULTILINE)
GIT_CLONE_DIR_PATTERN = re.compile(r"git clone \S+ (\S+)")


def wait_for_problem_deck(page: Page, *, timeout: int = 60_000) -> None:
    """Wait until the deck shows Reserve or Continue controls."""
    deck_ready = page.get_by_role("button", name="Continue →").or_(
        page.get_by_role("button", name=re.compile(r"Reserve", re.I))
    )
    deck_ready.first.wait_for(state="visible", timeout=timeout)


def find_reserve_button(page: Page) -> Locator:
    """Return the first visible, enabled Reserve button on the deck."""
    for name in RESERVE_BUTTON_NAMES:
        buttons = page.get_by_role("button", name=name)
        for i in range(buttons.count()):
            button = buttons.nth(i)
            if button.is_visible() and button.is_enabled():
                return button
    raise RuntimeError(
        "No available Reserve button found on the problem deck. "
        "Either nothing is available to reserve, or the button label "
        f"differs from {RESERVE_BUTTON_NAMES}."
    )


def is_challenge_review_page(page: Page) -> bool:
    return "/challenges/" in page.url and "mode=review" in page.url


def continue_existing_review(page: Page) -> bool:
    """Open an in-progress reservation when the deck shows Continue."""
    if "/reviews" not in page.url:
        goto_page(page, PROBLEM_DECK_URL)
    wait_for_problem_deck(page)
    continue_btn = page.get_by_role("button", name="Continue →")
    if continue_btn.count() and continue_btn.first.is_visible():
        continue_btn.first.click()
        page.wait_for_url(CHALLENGE_REVIEW_URL_GLOB, timeout=30_000)
        page.get_by_text(QUICK_SETUP_HEADING, exact=True).wait_for(
            state="visible",
            timeout=30_000,
        )
        print(f"Continued existing review: {page.url}")
        return True
    return False


def reserve_submission(page: Page) -> None:
    """Reserve the next available submission on the problem deck."""
    if "/reviews" not in page.url:
        goto_page(page, PROBLEM_DECK_URL)
    wait_for_problem_deck(page)

    button = find_reserve_button(page)
    button.scroll_into_view_if_needed()
    button.click()

    # After reserving, the app typically either navigates into the review or
    # swaps the button to a "Review"/"Continue" affordance. Wait for either.
    try:
        page.wait_for_url(REVIEW_URL_GLOB, timeout=15_000)
    except PlaywrightTimeoutError:
        wait_for_problem_deck(page)
    print("Reserved a submission.")


def open_review(page: Page) -> None:
    """Open the reserved submission's review view and leave it ready."""
    if is_challenge_review_page(page):
        page.get_by_text(QUICK_SETUP_HEADING, exact=True).wait_for(
            state="visible",
            timeout=30_000,
        )
        print(f"Review open: {page.url}")
        return

    for name in ("Review", "Continue Review", "Open", "Continue", "Continue →"):
        link = page.get_by_role("link", name=name)
        button = page.get_by_role("button", name=name)
        target = link if link.count() else button
        if target.count() and target.first.is_visible():
            target.first.click()
            break
    else:
        raise RuntimeError(
            "Reserved a submission but could not find a control to open its "
            "review. Check the reserved item's action label."
        )

    page.wait_for_url(CHALLENGE_REVIEW_URL_GLOB, timeout=30_000)

    if not is_challenge_review_page(page):
        raise RuntimeError(
            "Review page did not open. Expected a /challenges/... URL with "
            f"mode=review, got: {page.url}"
        )

    page.get_by_text(QUICK_SETUP_HEADING, exact=True).wait_for(
        state="visible",
        timeout=30_000,
    )
    print(f"Review open: {page.url}")


def open_quick_setup(page: Page) -> None:
    """Scroll to Quick Setup and expand the setup script."""
    heading = page.get_by_text(QUICK_SETUP_HEADING, exact=True)
    heading.wait_for(state="visible", timeout=30_000)
    heading.scroll_into_view_if_needed()
    heading.click()

    view_script = page.get_by_role("button", name=VIEW_SCRIPT_BUTTON)
    view_script.wait_for(state="visible", timeout=15_000)
    view_script.click()

    page.locator("pre").first.wait_for(state="visible", timeout=15_000)


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
            raise RuntimeError(
                f"Clone target already exists: {target_path}. "
                "Remove it or choose a different --clone-dir."
            )

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

        if not continue_existing_review(page):
            reserve_submission(page)
            open_review(page)
        context.storage_state(path=str(auth_state_path))

        cloned_path: Path | None = None
        if clone:
            setup_script = extract_setup_script(page)
            cloned_path = clone_submission_locally(
                setup_script,
                clone_dir=clone_dir or (REPO_ROOT / "submissions"),
            )

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
