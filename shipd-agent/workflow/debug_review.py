#!/usr/bin/env python3
"""Debug helper: step through reserve/review and capture page state."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from auth import AUTH_STATE_PATH, AuthConfig, ensure_signed_in, load_auth_config, managed_browser
from workflow.review import (
    PROBLEM_DECK_URL,
    _review_ready_locator,
    continue_existing_review,
    find_reserve_button,
    find_review_entry_control,
    is_review_ready,
    reserve_and_open_review,
    wait_for_post_reserve,
    wait_for_problem_deck,
)

DEBUG_DIR = Path(__file__).resolve().parent.parent.parent / "logs" / "debug-review"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def dump_page_state(page, label: str, out_dir: Path) -> dict:
    """Screenshot + list interactive elements for debugging."""
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = label.replace(" ", "_").replace("/", "-")
    png = out_dir / f"{safe}.png"
    page.screenshot(path=str(png), full_page=True)

    buttons: list[str] = []
    for btn in page.get_by_role("button").all():
        try:
            name = btn.inner_text(timeout=1_000).strip().replace("\n", " ")
        except Exception:
            name = "?"
        if name:
            buttons.append(name)

    headings: list[str] = []
    for h in page.locator("h1, h2, h3, h4").all():
        try:
            text = h.inner_text(timeout=1_000).strip()
        except Exception:
            text = "?"
        if text:
            headings.append(text)

    signals = _review_ready_locator(page)
    state = {
        "label": label,
        "url": page.url,
        "screenshot": str(png),
        "review_ready": is_review_ready(page),
        "signal_count": signals.count(),
        "buttons": buttons[:40],
        "headings": headings[:30],
    }
    (out_dir / f"{safe}.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"[debug] {label}: url={page.url} review_ready={state['review_ready']}")
    print(f"        screenshot={png}")
    return state


def run_debug(*, headless: bool = True, full_flow: bool = False) -> int:
    out_dir = DEBUG_DIR / _stamp()
    config = load_auth_config()

    with managed_browser(
        headless=headless,
        auth_state_path=AUTH_STATE_PATH,
        lightweight=headless,
    ) as session:
        page = session.page
        ensure_signed_in(page, PROBLEM_DECK_URL, config, headed=not headless)
        dump_page_state(page, "01-signed-in", out_dir)

        if full_flow:
            try:
                reserve_and_open_review(page)
                dump_page_state(page, "02-flow-complete", out_dir)
            except Exception as exc:
                dump_page_state(page, "02-flow-failed", out_dir)
                print(f"[debug] reserve_and_open_review failed: {exc}", file=sys.stderr)
                print(f"[debug] Done — output in {out_dir}")
                return 1
            print(f"[debug] SUCCESS — output in {out_dir}")
            return 0

        if continue_existing_review(page):
            dump_page_state(page, "02-continued-existing", out_dir)
            print(f"[debug] Used existing reservation — output in {out_dir}")
            return 0

        wait_for_problem_deck(page)
        dump_page_state(page, "02-deck-ready", out_dir)

        find_reserve_button(page).click()
        wait_for_post_reserve(page)
        dump_page_state(page, "03-after-reserve", out_dir)

        control = find_review_entry_control(page)
        if control is None:
            print("[debug] ERROR: no review entry control after reserve", file=sys.stderr)
            print(f"[debug] Done — output in {out_dir}")
            return 1

        print(f"[debug] clicking control: {control.inner_text().strip()!r}")
        try:
            with page.expect_navigation(timeout=15_000, wait_until="commit"):
                control.click()
        except PlaywrightTimeoutError:
            control.click()
        page.wait_for_timeout(2_000)
        dump_page_state(page, "04-after-continue-2s", out_dir)

        from workflow.review import wait_for_review_ready

        try:
            wait_for_review_ready(page, timeout=45_000)
            dump_page_state(page, "05-review-ready", out_dir)
        except Exception as exc:
            dump_page_state(page, "05-review-timeout", out_dir)
            print(f"[debug] wait_for_review_ready failed: {exc}", file=sys.stderr)
            print(f"[debug] Done — output in {out_dir}")
            return 1

    print(f"[debug] SUCCESS — output in {out_dir}")
    return 0


if __name__ == "__main__":
    headed = "--headed" in sys.argv
    full = "--full" in sys.argv
    raise SystemExit(run_debug(headless=not headed, full_flow=full))
