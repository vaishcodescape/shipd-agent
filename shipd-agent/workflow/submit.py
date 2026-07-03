# Fill and submit the Shipd review form from a structured review dict.

from __future__ import annotations

import re
from typing import Any

from playwright.sync_api import (
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

DECISION_LABELS: dict[str, str] = {
    "approve": "Approve",
    "request_changes": "Request Changes",
    "reject": "Reject",
}

BAND_HEADINGS: dict[str, str] = {
    "problem": "Problem Description",
    "tests": "Tests",
    "solution": "Solution & Code",
}

SCORE_LABELS: dict[int, str] = {
    0: "Failing",
    1: "Weak",
    2: "Minor",
    3: "Clean",
}

CONFIDENCE_UI: dict[str, str] = {
    "low": "Low",
    "medium": "Med",
    "high": "High",
}

AUTHOR_NOTE_LABELS = (
    "Note — sent to the author",
    "Note - sent to the author",
    "sent to the author",
)

SUBMIT_FORM_MARKERS = (
    "Meets quality standards",
    "Needs changes before acceptance",
    "Does not meet requirements",
)

OPEN_SUBMIT_BUTTONS = (
    "Submit Review",
    "Submit review",
    "Write Review",
    "Write review",
)


def _normalize_decision(decision: str) -> str:
    normalized = decision.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in DECISION_LABELS:
        return normalized
    aliases = {
        "approved": "approve",
        "changes_requested": "request_changes",
        "rejected": "reject",
    }
    mapped = aliases.get(normalized)
    if mapped in DECISION_LABELS:
        return mapped
    raise ValueError(f"Unknown review decision: {decision!r}")


def _normalize_confidence(confidence: str) -> str:
    normalized = confidence.strip().lower()
    if normalized in CONFIDENCE_UI:
        return normalized
    if normalized == "med":
        return "medium"
    raise ValueError(f"Unknown confidence level: {confidence!r}")


def _band_section(page: Page, heading: str) -> Locator:
    """Locate the band rating block by its section heading."""
    heading_loc = page.get_by_role("heading", name=heading)
    if not heading_loc.count():
        heading_loc = page.get_by_text(heading, exact=True)
    heading_loc = heading_loc.first
    heading_loc.wait_for(state="visible", timeout=15_000)
    heading_loc.scroll_into_view_if_needed()

    section = heading_loc.locator(
        "xpath=ancestor::*[self::section or self::div or self::fieldset][1]"
    )
    if section.count():
        return section.first

    return page.locator("div, section, fieldset").filter(
        has=page.get_by_text(heading, exact=True)
    ).first


def _click_button_in_scope(
    scope: Locator | Page,
    names: tuple[str, ...],
    *,
    pattern: re.Pattern[str] | None = None,
) -> None:
    for name in names:
        button = scope.get_by_role("button", name=name)
        if button.count() and button.first.is_visible():
            button.first.scroll_into_view_if_needed()
            button.first.click()
            return
    if pattern is not None:
        button = scope.get_by_role("button", name=pattern)
        if button.count() and button.first.is_visible():
            button.first.scroll_into_view_if_needed()
            button.first.click()
            return
    raise RuntimeError(
        f"No matching button found for names={names!r} pattern={pattern!r}"
    )


def _ensure_submit_review_form(page: Page) -> None:
    """Open submit-review form if decision cards are not visible."""
    for marker in SUBMIT_FORM_MARKERS:
        if page.get_by_text(marker, exact=True).count():
            return

    for label in DECISION_LABELS.values():
        button = page.get_by_role("button", name=label)
        if button.count() and button.first.is_visible():
            return

    for name in OPEN_SUBMIT_BUTTONS:
        opener = page.get_by_role("button", name=name).or_(
            page.get_by_role("link", name=name)
        )
        if opener.count() and opener.first.is_visible():
            opener.first.scroll_into_view_if_needed()
            opener.first.click()
            break
    else:
        tab = page.get_by_role("tab", name=re.compile(r"submit|review", re.I))
        if tab.count() and tab.first.is_visible():
            tab.first.click()

    page.get_by_role("button", name="Approve").first.wait_for(
        state="visible",
        timeout=30_000,
    )


def _click_decision(page: Page, decision: str) -> None:
    normalized = _normalize_decision(decision)
    label = DECISION_LABELS[normalized]
    button = page.get_by_role("button", name=label)
    if not button.count():
        pattern = re.compile(rf"^{re.escape(label)}$", re.I)
        button = page.get_by_role("button", name=pattern)
    button.first.scroll_into_view_if_needed()
    button.first.click()


def _click_band_score(section: Locator, score: int) -> None:
    if score not in SCORE_LABELS:
        raise ValueError(f"Band score must be 0-3, got {score!r}")

    label = SCORE_LABELS[score]
    candidates = (
        str(score),
        f"{score} {label}",
        label,
        f"{score}\n{label}",
    )
    for name in candidates:
        button = section.get_by_role("button", name=name)
        if button.count() and button.first.is_visible():
            button.first.scroll_into_view_if_needed()
            button.first.click()
            return

    pattern = re.compile(rf"^{score}\b")
    button = section.get_by_role("button", name=pattern)
    if button.count() and button.first.is_visible():
        button.first.scroll_into_view_if_needed()
        button.first.click()
        return

    raise RuntimeError(
        f"Could not find score button {score} ({label}) in band section."
    )


def _click_band_confidence(section: Locator, confidence: str) -> None:
    normalized = _normalize_confidence(confidence)
    ui_label = CONFIDENCE_UI[normalized]
    button = section.get_by_role("button", name=ui_label)
    if not button.count():
        button = section.get_by_role(
            "button",
            name=re.compile(rf"^{re.escape(ui_label)}$", re.I),
        )
    if not button.count():
        button = section.get_by_text(ui_label, exact=True)
    button.first.scroll_into_view_if_needed()
    button.first.click()


def _fill_band_ratings(page: Page, band_ratings: dict[str, Any]) -> None:
    for band_key, heading in BAND_HEADINGS.items():
        band = band_ratings.get(band_key)
        if not isinstance(band, dict):
            raise ValueError(f"Missing band_ratings[{band_key!r}]")

        score = band.get("score")
        confidence = band.get("confidence")
        if score is None or confidence is None:
            raise ValueError(
                f"band_ratings[{band_key!r}] requires score and confidence."
            )

        section = _band_section(page, heading)
        _click_band_score(section, int(score))
        _click_band_confidence(section, str(confidence))


def _build_author_note(review: dict[str, Any]) -> str:
    parts: list[str] = []
    feedback = str(review.get("contributor_feedback", "")).strip()
    if feedback:
        parts.append(feedback)

    band_ratings = review.get("band_ratings", {})
    for band_key, heading in BAND_HEADINGS.items():
        band = band_ratings.get(band_key, {})
        if not isinstance(band, dict):
            continue
        score = band.get("score")
        reasoning = str(band.get("reasoning", "")).strip()
        if score is not None and int(score) < 3 and reasoning:
            parts.append(f"{heading} ({score}/3): {reasoning}")

    return "\n\n".join(parts).strip()


def _author_note_field(page: Page) -> Locator:
    for label in AUTHOR_NOTE_LABELS:
        field = page.get_by_label(label, exact=False)
        if field.count():
            return field.first

    field = page.get_by_role(
        "textbox",
        name=re.compile(r"note.*author", re.I),
    )
    if field.count():
        return field.first

    field = page.locator("textarea").filter(
        has=page.get_by_text(re.compile(r"sent to the author", re.I))
    )
    if field.count():
        return field.first

    return page.locator("textarea").first


def _fill_author_note(page: Page, review: dict[str, Any]) -> None:
    note = _build_author_note(review)
    if not note:
        return

    field = _author_note_field(page)
    field.wait_for(state="visible", timeout=15_000)
    field.scroll_into_view_if_needed()
    field.click()
    field.fill(note)


def _click_suggested_tags(page: Page, tags: list[Any]) -> None:
    if not tags:
        return

    for tag in tags:
        tag_text = str(tag).strip()
        if not tag_text:
            continue

        button = page.get_by_role("button", name=tag_text)
        if not button.count():
            button = page.get_by_role(
                "button",
                name=re.compile(re.escape(tag_text), re.I),
            )
        if not button.count():
            button = page.get_by_text(tag_text, exact=True)
        if button.count() and button.first.is_visible():
            button.first.scroll_into_view_if_needed()
            button.first.click()


def _set_downgrade_to_mars(page: Page, *, enabled: bool) -> None:
    checkbox = page.get_by_role(
        "checkbox",
        name=re.compile(r"downgrade.*mars", re.I),
    )
    if not checkbox.count():
        checkbox = page.get_by_label(re.compile(r"downgrade.*mars", re.I))
    if not checkbox.count():
        return

    checkbox = checkbox.first
    checkbox.scroll_into_view_if_needed()
    if checkbox.is_checked() != enabled:
        checkbox.click()


def _finalize_submission(page: Page) -> None:
    for name in ("Submit Review", "Submit review", "Submit"):
        button = page.get_by_role("button", name=name)
        if (
            button.count()
            and button.first.is_visible()
            and button.first.is_enabled()
        ):
            button.first.scroll_into_view_if_needed()
            button.first.click()
            return


def submit_review(page: Page, review: dict[str, Any], *, quest: str) -> None:
    """Fill the Shipd submit-review form from a structured review dict."""
    if not review.get("decision"):
        raise ValueError("review dict must include 'decision'.")

    _ensure_submit_review_form(page)
    _click_decision(page, str(review["decision"]))

    band_ratings = review.get("band_ratings")
    if not isinstance(band_ratings, dict):
        raise ValueError("review dict must include 'band_ratings'.")
    _fill_band_ratings(page, band_ratings)

    _fill_author_note(page, review)
    _click_suggested_tags(page, list(review.get("suggested_tags") or []))

    if review.get("downgrade_to_mars") and quest == "olympus":
        _set_downgrade_to_mars(page, enabled=True)

    try:
        _finalize_submission(page)
    except PlaywrightTimeoutError:
        pass
