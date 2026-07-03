# Clock in on Shipd time logs and return to the reviews page.

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from auth import (
    AUTH_STATE_PATH,
    REVIEWS_URL,
    AuthConfig,
    ensure_signed_in,
    goto_page,
    load_auth_config,
    managed_browser,
)

QUEST_LABELS = {
    "olympus": "Olympus",
    "mars": "Mars",
}
QUEST_TIME_LOGS_URL = {
    "olympus": "https://shipd.ai/quests/olympus/time-logs",
    "mars": "https://shipd.ai/quests/mars/time-logs",
}
# Backwards-compatible default for callers that omit quest.
TIME_LOGS_URL = QUEST_TIME_LOGS_URL["olympus"]


def time_logs_url(quest: str) -> str:
    """Return the Shipd time-logs URL for a quest."""
    return QUEST_TIME_LOGS_URL[parse_quest(quest)]
QUEST_COMBOBOX_PATTERN = re.compile(
    r"Olympus|Mars|— \(ad-hoc\)|Diamond",
    re.IGNORECASE,
)


def wait_for_time_logs(page: Page, *, timeout: int = 20_000) -> None:
    clock_controls = page.get_by_role("button", name="Clock In", exact=True).or_(
        page.get_by_role("button", name="Clock Out", exact=True)
    )
    clock_controls.first.wait_for(state="visible", timeout=timeout)


def parse_quest(value: str) -> str:
    quest = value.strip().lower()
    if quest not in QUEST_LABELS:
        raise ValueError(f"Quest must be one of: {', '.join(QUEST_LABELS)}")
    return quest


def quest_combobox(page: Page):
    for combobox in page.get_by_role("combobox").all():
        if QUEST_COMBOBOX_PATTERN.search(combobox.inner_text()):
            return combobox
    raise RuntimeError("Quest selector not found on the time logs page.")


def select_quest(page: Page, quest: str) -> None:
    quest_label = QUEST_LABELS[quest]
    combobox = quest_combobox(page)
    combobox.wait_for(state="visible", timeout=10_000)

    if quest_label.lower() in combobox.inner_text().lower():
        return

    combobox.click()
    option = page.get_by_role("option", name=quest_label, exact=True)
    option.wait_for(state="visible", timeout=5_000)
    option.click()


def clock_in(page: Page, quest: str) -> None:
    clock_out_btn = page.get_by_role("button", name="Clock Out", exact=True)
    if clock_out_btn.count() and clock_out_btn.is_visible():
        current_quest = quest_combobox(page).inner_text().lower()
        if quest in current_quest:
            print(f"Already clocked in for {QUEST_LABELS[quest]}.")
            return

        clock_out_btn.click()
        page.get_by_role("button", name="Clock In", exact=True).wait_for(
            state="visible",
            timeout=10_000,
        )

    select_quest(page, quest)

    clock_in_btn = page.get_by_role("button", name="Clock In", exact=True)
    clock_in_btn.wait_for(state="visible", timeout=10_000)
    clock_in_btn.click()

    page.get_by_role("button", name="Clock Out", exact=True).wait_for(
        state="visible",
        timeout=10_000,
    )
    print(f"Clocked in for {QUEST_LABELS[quest]}.")


def _clock_out_button(page: Page):
    """Return the visible stop/clock-out control, if any."""
    for name in ("Clock Out", "Stop"):
        button = page.get_by_role("button", name=name, exact=True)
        if button.count() and button.first.is_visible():
            return button.first
    return None


def _fill_clock_out_notes(page: Page, message: str) -> bool:
    """Fill a notes/comment field in the clock-out flow."""
    if not message.strip():
        return False

    candidates = [
        page.get_by_role(
            "textbox",
            name=re.compile(r"note|comment|description|message|summary", re.IGNORECASE),
        ),
        page.get_by_placeholder(
            re.compile(r"note|comment|description|message|summary", re.IGNORECASE),
        ),
        page.locator("textarea"),
    ]
    for locator in candidates:
        if locator.count() and locator.first.is_visible():
            locator.first.fill(message)
            return True
    return False


def _confirm_clock_out(page: Page) -> None:
    """Submit a clock-out dialog when a follow-up confirm button appears."""
    for name in ("Confirm", "Save", "Submit", "Clock Out", "Stop"):
        confirm = page.get_by_role("button", name=name, exact=True)
        if confirm.count() and confirm.first.is_visible():
            confirm.first.click()
            return


def clock_out(page: Page, message: str = "", *, quest: str = "olympus") -> bool:
    """Stop the active clock and optionally add a session summary note.

    Returns True when a running clock was stopped. Returns False when the
    clock was already stopped or the stop control was not found.
    """
    logs_url = time_logs_url(quest)
    if f"/quests/{parse_quest(quest)}/time-logs" not in page.url:
        goto_page(page, logs_url)

    wait_for_time_logs(page)

    stop_button = _clock_out_button(page)
    if stop_button is None:
        print("WARNING: Not clocked in; Clock Out / Stop button not visible.")
        return False

    stop_button.click()

    notes_locator = page.locator("textarea").or_(
        page.get_by_role(
            "textbox",
            name=re.compile(r"note|comment|description|message|summary", re.IGNORECASE),
        )
    )
    try:
        notes_locator.first.wait_for(state="visible", timeout=2_000)
    except PlaywrightTimeoutError:
        pass

    filled = _fill_clock_out_notes(page, message)
    if message.strip() and not filled:
        print(
            "WARNING: Notes field not found; clock stopped without embedded message."
        )

    _confirm_clock_out(page)

    try:
        page.get_by_role("button", name="Clock In", exact=True).wait_for(
            state="visible",
            timeout=15_000,
        )
    except PlaywrightTimeoutError:
        pass

    print("Clocked out.")
    return True


def return_to_reviews(page: Page) -> None:
    back_link = page.get_by_role("link", name="Back to Reviews")
    if back_link.count() and back_link.first.is_visible():
        back_link.first.click()
    else:
        goto_page(page, REVIEWS_URL)

    try:
        page.wait_for_url("**/reviews**", timeout=10_000, wait_until="commit")
    except PlaywrightTimeoutError:
        pass

    deck_ready = page.get_by_role("button", name="Continue →", exact=True).or_(
        page.get_by_role("button", name=re.compile(r"Reserve", re.I))
    )
    deck_ready.first.wait_for(state="visible", timeout=15_000)
    print(f"Back on reviews: {page.url}")


def run_clock_in(
    *,
    quest: str,
    config: AuthConfig | None = None,
    auth_state_path: Path = AUTH_STATE_PATH,
) -> None:
    config = config or AuthConfig()
    auth_state_path.parent.mkdir(parents=True, exist_ok=True)

    with managed_browser(
        headless=True,
        auth_state_path=auth_state_path,
        lightweight=True,
    ) as session:
        page = session.page
        context = session.context

        ensure_signed_in(page, time_logs_url(quest), config)
        context.storage_state(path=str(auth_state_path))

        wait_for_time_logs(page)
        clock_in(page, quest)
        return_to_reviews(page)
        context.storage_state(path=str(auth_state_path))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Clock in on Shipd time logs for Olympus or Mars, then return to "
            "reviews. Runs headless with no visible browser window."
        ),
    )
    parser.add_argument(
        "--quest",
        choices=sorted(QUEST_LABELS),
        default="olympus",
        help="Quest to clock hours for (default: olympus).",
    )
    parser.add_argument(
        "--auth-state",
        type=Path,
        default=AUTH_STATE_PATH,
        help="Path to save/load Playwright auth state.",
    )
    return parser.parse_args()


def main() -> int:
    config = load_auth_config()

    args = parse_args()
    try:
        run_clock_in(
            quest=args.quest,
            config=config,
            auth_state_path=args.auth_state,
        )
    except (PlaywrightTimeoutError, RuntimeError, ValueError) as exc:
        print(f"Clock-in failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
