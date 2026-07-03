#!/usr/bin/env python3
"""Probe Shipd submit-review form controls (dry run, no final submit)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

# workflow/review.py shadows the review package when this script is run as
# `python workflow/debug_submit.py` (sys.path[0] is workflow/).
_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from auth import AUTH_STATE_PATH, ensure_signed_in, load_auth_config, managed_browser, goto_page
from review.review_io import SESSION_META_PATH, load_session_meta
from workflow.submit import (
    BAND_HEADINGS,
    DECISION_LABELS,
    SUBMIT_FORM_MARKERS,
    _click_decision,
    _ensure_submit_review_form,
    _fill_band,
    submit_review,
)

DEBUG_DIR = Path(__file__).resolve().parent.parent.parent / "logs" / "debug-submit"

SAMPLE_REVIEW = {
    "decision": "request_changes",
    "band_ratings": {
        "problem": {"score": 2, "confidence": "medium", "reasoning": "probe test"},
        "tests": {"score": 3, "confidence": "high", "reasoning": ""},
        "solution": {"score": 2, "confidence": "low", "reasoning": "probe test"},
    },
    "contributor_feedback": "Automated submit probe — safe to ignore.",
    "suggested_tags": [],
}


def _dump_buttons(page, label: str) -> list[str]:
    names: list[str] = []
    for btn in page.get_by_role("button").all():
        try:
            text = btn.inner_text(timeout=1_000).strip().replace("\n", " | ")
        except Exception:
            text = "?"
        if text:
            names.append(text)
    print(f"\n[{label}] {len(names)} buttons:")
    for name in names[:40]:
        print(f"  - {name!r}")
    return names


def _probe_decision_locators(page) -> dict:
    results = {}
    for key, label in DECISION_LABELS.items():
        exact = page.get_by_role("button", name=label, exact=True)
        loose = page.get_by_role("button", name=label)
        results[key] = {
            "label": label,
            "exact_count": exact.count(),
            "loose_count": loose.count(),
            "loose_first": loose.first.inner_text().strip() if loose.count() else None,
        }
    return results


def run_probe(*, headless: bool = True) -> int:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    config = load_auth_config()

    with managed_browser(
        headless=headless,
        auth_state_path=AUTH_STATE_PATH,
        lightweight=headless,
    ) as session:
        page = session.page

        if SESSION_META_PATH.is_file():
            meta = load_session_meta()
            url = meta["review_url"]
            print(f"Using session meta URL: {url}")
            ensure_signed_in(page, url, config, headed=not headless)
            if url not in page.url:
                goto_page(page, url)
        else:
            print("No session meta — opening review via reserve flow...")
            from workflow.review import reserve_and_open_review

            ensure_signed_in(page, "https://shipd.ai/quests/olympus/reviews", config)
            reserve_and_open_review(page)
            url = page.url

        page.screenshot(path=str(DEBUG_DIR / "01-review-page.png"), full_page=True)
        _dump_buttons(page, "before submit form")

        markers = {
            m: page.get_by_text(m, exact=True).count() for m in SUBMIT_FORM_MARKERS
        }
        print(f"\nSubmit form markers visible: {markers}")

        decision_probe = _probe_decision_locators(page)
        print(f"\nDecision locator probe:\n{json.dumps(decision_probe, indent=2)}")

        try:
            _ensure_submit_review_form(page)
            print("\n✓ _ensure_submit_review_form succeeded")
        except Exception as exc:
            print(f"\n✗ _ensure_submit_review_form failed: {exc}", file=sys.stderr)
            page.screenshot(path=str(DEBUG_DIR / "02-form-failed.png"), full_page=True)
            return 1

        page.screenshot(path=str(DEBUG_DIR / "02-form-open.png"), full_page=True)
        _dump_buttons(page, "after ensure form")

        try:
            _click_decision(page, "request_changes")
            print("✓ _click_decision(request_changes) succeeded")
        except Exception as exc:
            print(f"✗ _click_decision failed: {exc}", file=sys.stderr)
            return 1

        page.screenshot(path=str(DEBUG_DIR / "03-decision-clicked.png"), full_page=True)

        for band_key, heading in BAND_HEADINGS.items():
            band = SAMPLE_REVIEW["band_ratings"][band_key]
            try:
                _fill_band(
                    page,
                    heading,
                    score=int(band["score"]),
                    confidence=str(band["confidence"]),
                )
                print(f"✓ band {band_key} score={band['score']} conf={band['confidence']}")
            except Exception as exc:
                print(f"✗ band {band_key} failed: {exc}", file=sys.stderr)
                page.screenshot(
                    path=str(DEBUG_DIR / f"04-band-{band_key}-failed.png"),
                    full_page=True,
                )
                return 1

        page.screenshot(path=str(DEBUG_DIR / "04-bands-filled.png"), full_page=True)

        try:
            submit_review(page, SAMPLE_REVIEW, quest="olympus", finalize=False)
            print("✓ submit_review(finalize=False) full flow succeeded")
        except Exception as exc:
            print(f"✗ submit_review failed: {exc}", file=sys.stderr)
            return 1

        page.screenshot(path=str(DEBUG_DIR / "05-form-filled.png"), full_page=True)

        finalize_btn = page.get_by_role("button", name="Submit Review")
        if not finalize_btn.count():
            finalize_btn = page.get_by_role("button", name="Submit")
        finalize_visible = finalize_btn.count() and finalize_btn.first.is_visible()
        finalize_enabled = (
            finalize_visible and finalize_btn.first.is_enabled()
        )
        print(
            f"\nFinal Submit button: visible={finalize_visible} "
            f"enabled={finalize_enabled}"
        )

    print(f"\nProbe complete — screenshots in {DEBUG_DIR}")
    return 0


if __name__ == "__main__":
    headed = "--headed" in sys.argv
    raise SystemExit(run_probe(headless=not headed))
