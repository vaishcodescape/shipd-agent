# Playwright scrape helpers for Shipd review page.

from __future__ import annotations

import re
from typing import Any

from playwright.sync_api import Locator, TimeoutError as PlaywrightTimeoutError

from review.agent_runs_checks import parse_agent_run_metrics

DEFAULT_TIMEOUT_MS = 8_000
_UNAVAILABLE_MSG = "not available — run via orchestrator with browser session"

_HOLISTIC_EMPTY: dict[str, Any] = {
    "available": False,
    "status": None,
    "checklist_summary": "",
    "reviewer_notes": "",
    "raw_text": "",
}

_AGENT_RUNS_EMPTY: dict[str, Any] = {
    "available": False,
    "pass_rate": "",
    "summary": "",
    "failure_patterns": "",
    "raw_text": "",
}

_RELATED_EMPTY: dict[str, Any] = {
    "available": False,
    "entries": "",
    "tags": "",
    "raw_text": "",
}

_STATUS_RE = re.compile(r"\b(PASS|FAIL)\b")
_CHECKLIST_COUNT_RE = re.compile(
    r"(\d+\s+pass(?:\s*,\s*\d+\s+fail)?|\d+\s+fail(?:\s*,\s*\d+\s+pass)?)",
    re.I,
)
_REVIEWER_NOTES_SPLIT_RE = re.compile(r"Reviewer Notes\s*:?\s*", re.I)
_PASS_RATE_RE = re.compile(
    r"(\d+\s*/\s*\d+\s*(?:pass|passed|runs?)?|\d+\s*%\s*pass|\d+\s+of\s+\d+\s+pass)",
    re.I,
)
_SIMILARITY_RE = re.compile(r"(\d+\s*%\s*similar|\d+\s*%\s*match|similarity[:\s]+\d+%?)", re.I)
_DUPLICATE_TAG_RE = re.compile(r"\b(duplicate|older|overlap|prior submission)\b", re.I)


def parse_holistic_check_from_text(raw_text: str) -> dict[str, Any]:
    """Parse holistic check content from section plain text (unit-testable)."""
    if not raw_text or not raw_text.strip():
        return dict(_HOLISTIC_EMPTY)

    text = raw_text.strip()
    status: str | None = None
    status_match = _STATUS_RE.search(text)
    if status_match:
        status = status_match.group(1).upper()

    checklist_summary = ""
    checklist_match = _CHECKLIST_COUNT_RE.search(text)
    if checklist_match:
        checklist_summary = checklist_match.group(1).strip()
    elif re.search(r"CHECKLIST", text, re.I):
        checklist_lines: list[str] = []
        in_checklist = False
        for line in text.splitlines():
            stripped = line.strip()
            if re.fullmatch(r"CHECKLIST", stripped, re.I):
                in_checklist = True
                continue
            if in_checklist:
                if re.search(r"Reviewer Notes", stripped, re.I):
                    break
                if stripped:
                    checklist_lines.append(stripped)
        checklist_summary = "\n".join(checklist_lines).strip()

    reviewer_notes = ""
    notes_parts = _REVIEWER_NOTES_SPLIT_RE.split(text, maxsplit=1)
    if len(notes_parts) > 1:
        notes_body = notes_parts[1].strip()
        stop_markers = (
            re.compile(r"^Re-run\b", re.I),
            re.compile(r"^CHECKLIST\b", re.I),
        )
        note_lines: list[str] = []
        paragraph: list[str] = []
        for line in notes_body.splitlines():
            stripped = line.strip()
            if any(marker.search(stripped) for marker in stop_markers):
                break
            if not stripped:
                if paragraph:
                    note_lines.append(" ".join(paragraph))
                    paragraph = []
                continue
            paragraph.append(stripped)
        if paragraph:
            note_lines.append(" ".join(paragraph))
        reviewer_notes = "\n\n".join(note_lines).strip()

    available = bool(status or checklist_summary or reviewer_notes or "Holistic Check" in text)
    return {
        "available": available,
        "status": status if status in ("PASS", "FAIL") else ("UNKNOWN" if available else None),
        "checklist_summary": checklist_summary,
        "reviewer_notes": reviewer_notes,
        "raw_text": text,
    }


def parse_agent_runs_from_text(raw_text: str) -> dict[str, Any]:
    """Parse agent run section plain text (unit-testable)."""
    if not raw_text or not raw_text.strip():
        return dict(_AGENT_RUNS_EMPTY)

    text = raw_text.strip()
    pass_rate = ""
    pass_match = _PASS_RATE_RE.search(text)
    if pass_match:
        pass_rate = pass_match.group(1).strip()

    failure_patterns = ""
    failure_lines: list[str] = []
    in_failures = False
    for line in text.splitlines():
        stripped = line.strip()
        if re.search(r"failure pattern|common fail|failed because", stripped, re.I):
            in_failures = True
            continue
        if in_failures and stripped:
            if re.search(r"^(LOC|Median|Related|Holistic)", stripped, re.I):
                break
            failure_lines.append(stripped)
    if failure_lines:
        failure_patterns = "\n".join(failure_lines).strip()

    summary_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.fullmatch(r"Agent Runs?", stripped, re.I):
            continue
        if re.search(r"^(failure pattern|common fail)", stripped, re.I):
            break
        summary_lines.append(stripped)
        if len(summary_lines) >= 12:
            break
    summary = "\n".join(summary_lines).strip()

    metrics = parse_agent_run_metrics(text)
    available = bool(
        pass_rate
        or summary
        or failure_patterns
        or metrics.get("median_loc") is not None
        or re.search(r"Agent Runs?", text, re.I)
    )
    return {
        "available": available,
        "pass_rate": pass_rate,
        "summary": summary,
        "failure_patterns": failure_patterns,
        "raw_text": text,
        "metrics": metrics,
    }


def parse_related_submissions_from_text(raw_text: str) -> dict[str, Any]:
    """Parse related submissions section plain text (unit-testable)."""
    if not raw_text or not raw_text.strip():
        return dict(_RELATED_EMPTY)

    text = raw_text.strip()
    entries: list[str] = []
    tags: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.fullmatch(r"Related Submissions?", stripped, re.I):
            continue
        if _SIMILARITY_RE.search(stripped) or re.search(r"\b\d+%\b", stripped):
            entries.append(stripped)
        tag_match = _DUPLICATE_TAG_RE.search(stripped)
        if tag_match:
            tag = tag_match.group(1).lower()
            if tag not in tags:
                tags.append(tag)

    available = bool(
        entries
        or tags
        or re.search(r"Related Submissions?", text, re.I)
        or _SIMILARITY_RE.search(text)
    )
    return {
        "available": available,
        "entries": "\n".join(entries).strip(),
        "tags": ", ".join(tags),
        "raw_text": text,
    }


def _find_section_by_heading(page: Any, heading_pattern: str | re.Pattern[str]) -> Locator | None:
    pattern = (
        heading_pattern
        if isinstance(heading_pattern, re.Pattern)
        else re.compile(heading_pattern, re.I)
    )
    heading = page.get_by_role("heading", name=pattern)
    if not heading.count():
        heading = page.get_by_text(pattern)
    if not heading.count():
        return None

    heading = heading.first
    try:
        heading.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
        heading.scroll_into_view_if_needed()
    except PlaywrightTimeoutError:
        return None

    section = heading.locator(
        "xpath=ancestor::*[self::section or self::div or self::article][1]"
    )
    if section.count():
        return section.first

    parent = heading.locator("xpath=..")
    return parent if parent.count() else None


def _find_holistic_section(page: Any) -> Locator | None:
    return _find_section_by_heading(page, r"Holistic Check")


def _expand_checklist_if_collapsed(section: Locator) -> None:
    checklist = section.get_by_text(re.compile(r"^CHECKLIST$", re.I))
    if not checklist.count():
        return

    expand_patterns = (
        re.compile(r"expand", re.I),
        re.compile(r"show", re.I),
        re.compile(r"chevron", re.I),
        re.compile(r"more", re.I),
    )
    checklist_block = checklist.first.locator(
        "xpath=ancestor::*[self::div or self::section][1]"
    )
    scope = checklist_block if checklist_block.count() else section
    for pattern in expand_patterns:
        button = scope.get_by_role("button", name=pattern)
        if button.count() and button.first.is_visible():
            try:
                button.first.click(timeout=2_000)
            except PlaywrightTimeoutError:
                pass
            return

    disclosure = scope.locator("[aria-expanded='false']")
    if disclosure.count() and disclosure.first.is_visible():
        try:
            disclosure.first.click(timeout=2_000)
        except PlaywrightTimeoutError:
            pass


def _extract_reviewer_notes(section: Locator) -> str:
    notes_heading = section.get_by_text(re.compile(r"Reviewer Notes", re.I))
    if not notes_heading.count():
        return ""

    heading = notes_heading.first
    container = heading.locator("xpath=following-sibling::*[1]")
    if container.count():
        text = container.first.inner_text(timeout=DEFAULT_TIMEOUT_MS).strip()
        if text:
            return text

    paragraphs: list[str] = []
    sibling = heading.locator("xpath=following-sibling::p")
    for idx in range(sibling.count()):
        line = sibling.nth(idx).inner_text(timeout=2_000).strip()
        if line:
            paragraphs.append(line)
    if paragraphs:
        return "\n\n".join(paragraphs)

    parent = heading.locator("xpath=..")
    if parent.count():
        text = parent.first.inner_text(timeout=DEFAULT_TIMEOUT_MS).strip()
        split = _REVIEWER_NOTES_SPLIT_RE.split(text, maxsplit=1)
        if len(split) > 1:
            return split[1].strip()
    return ""


def _scrape_section_text(page: Any, heading_pattern: str | re.Pattern[str]) -> str:
    section = _find_section_by_heading(page, heading_pattern)
    if section is None:
        return ""
    try:
        return section.inner_text(timeout=DEFAULT_TIMEOUT_MS).strip()
    except (PlaywrightTimeoutError, AttributeError, TypeError):
        return ""


def scrape_holistic_check(page: Any) -> dict[str, Any]:
    """Scrape the Shipd Holistic Check section from the review page."""
    if page is None:
        return dict(_HOLISTIC_EMPTY)

    try:
        section = _find_holistic_section(page)
        if section is None:
            return dict(_HOLISTIC_EMPTY)

        _expand_checklist_if_collapsed(section)

        raw_text = section.inner_text(timeout=DEFAULT_TIMEOUT_MS).strip()
        parsed = parse_holistic_check_from_text(raw_text)

        reviewer_notes = parsed.get("reviewer_notes") or _extract_reviewer_notes(section)
        if reviewer_notes:
            parsed["reviewer_notes"] = reviewer_notes

        parsed["raw_text"] = raw_text
        parsed["available"] = bool(
            parsed.get("status")
            or parsed.get("checklist_summary")
            or parsed.get("reviewer_notes")
            or raw_text
        )
        if parsed["available"] and parsed.get("status") not in ("PASS", "FAIL"):
            parsed["status"] = "UNKNOWN"
        return parsed
    except (PlaywrightTimeoutError, AttributeError, TypeError):
        return dict(_HOLISTIC_EMPTY)


def scrape_agent_runs(page: Any) -> dict[str, Any]:
    """Scrape agent run results, pass rates, and failure patterns from the review page."""
    if page is None:
        return dict(_AGENT_RUNS_EMPTY)

    try:
        raw_text = _scrape_section_text(page, r"Agent Runs?")
        if not raw_text:
            return dict(_AGENT_RUNS_EMPTY)
        parsed = parse_agent_runs_from_text(raw_text)
        parsed["raw_text"] = raw_text
        parsed["available"] = bool(
            parsed.get("pass_rate")
            or parsed.get("summary")
            or parsed.get("failure_patterns")
            or raw_text
        )
        return parsed
    except (PlaywrightTimeoutError, AttributeError, TypeError):
        return dict(_AGENT_RUNS_EMPTY)


def scrape_related_submissions(page: Any) -> dict[str, Any]:
    """Scrape related/duplicate submissions if visible on the review page."""
    if page is None:
        return dict(_RELATED_EMPTY)

    try:
        raw_text = _scrape_section_text(page, r"Related Submissions?")
        if not raw_text:
            return dict(_RELATED_EMPTY)
        parsed = parse_related_submissions_from_text(raw_text)
        parsed["raw_text"] = raw_text
        parsed["available"] = bool(
            parsed.get("entries")
            or parsed.get("tags")
            or raw_text
        )
        return parsed
    except (PlaywrightTimeoutError, AttributeError, TypeError):
        return dict(_RELATED_EMPTY)


def _holistic_to_scrape_strings(holistic: dict[str, Any]) -> dict[str, str]:
    if not holistic.get("available"):
        return {
            "holistic_check_available": "false",
            "holistic_check_status": "",
            "holistic_check_checklist": "",
            "holistic_check_reviewer_notes": "",
            "holistic_check_raw": holistic.get("raw_text") or "",
        }
    return {
        "holistic_check_available": "true",
        "holistic_check_status": str(holistic.get("status") or "UNKNOWN"),
        "holistic_check_checklist": str(holistic.get("checklist_summary") or ""),
        "holistic_check_reviewer_notes": str(holistic.get("reviewer_notes") or ""),
        "holistic_check_raw": str(holistic.get("raw_text") or ""),
    }


def _agent_runs_to_string(data: dict[str, Any]) -> str:
    if not data.get("available"):
        raw = str(data.get("raw_text") or "").strip()
        return raw or _UNAVAILABLE_MSG
    parts: list[str] = []
    if data.get("pass_rate"):
        parts.append(f"Pass rate: {data['pass_rate']}")
    if data.get("summary"):
        parts.append(data["summary"])
    if data.get("failure_patterns"):
        parts.append(f"Failure patterns:\n{data['failure_patterns']}")
    return "\n\n".join(parts).strip() or str(data.get("raw_text") or _UNAVAILABLE_MSG)


def _related_submissions_to_string(data: dict[str, Any]) -> str:
    if not data.get("available"):
        raw = str(data.get("raw_text") or "").strip()
        return raw or _UNAVAILABLE_MSG
    parts: list[str] = []
    if data.get("entries"):
        parts.append(data["entries"])
    if data.get("tags"):
        parts.append(f"Tags: {data['tags']}")
    return "\n\n".join(parts).strip() or str(data.get("raw_text") or _UNAVAILABLE_MSG)


def scrape_context_for_prompts(
    *,
    holistic: dict[str, Any],
    agent_runs: dict[str, Any],
    related: dict[str, Any],
) -> dict[str, str]:
    """Flatten structured scrape results into prompt-ready string fields."""
    return {
        "agent_runs": _agent_runs_to_string(agent_runs),
        "related_submissions": _related_submissions_to_string(related),
        **_holistic_to_scrape_strings(holistic),
    }


def scrape_review_page_context(page: Any) -> dict[str, Any]:
    """Scrape all review-page panels; returns structured data plus prompt strings."""
    holistic = scrape_holistic_check(page)
    agent_runs = scrape_agent_runs(page)
    related = scrape_related_submissions(page)
    prompt_strings = scrape_context_for_prompts(
        holistic=holistic,
        agent_runs=agent_runs,
        related=related,
    )
    return {
        "holistic_check": holistic,
        "agent_runs_data": agent_runs,
        "related_submissions_data": related,
        **prompt_strings,
    }


def scrape_review_page(page: Any) -> dict[str, str]:
    """Scrape agent runs / related submissions / holistic check from the review page."""
    ctx = scrape_review_page_context(page)
    return {
        "agent_runs": ctx["agent_runs"],
        "related_submissions": ctx["related_submissions"],
        "holistic_check_available": ctx["holistic_check_available"],
        "holistic_check_status": ctx["holistic_check_status"],
        "holistic_check_checklist": ctx["holistic_check_checklist"],
        "holistic_check_reviewer_notes": ctx["holistic_check_reviewer_notes"],
        "holistic_check_raw": ctx["holistic_check_raw"],
    }


def unavailable_scrape_context() -> dict[str, str]:
    """Scrape context when no Playwright page is available (CLI / separate steps)."""
    return {
        "agent_runs": _UNAVAILABLE_MSG,
        "related_submissions": _UNAVAILABLE_MSG,
        "holistic_check_available": "false",
        "holistic_check_status": "",
        "holistic_check_checklist": "",
        "holistic_check_reviewer_notes": "",
        "holistic_check_raw": _UNAVAILABLE_MSG,
    }


def unavailable_scrape_page_context() -> dict[str, Any]:
    """Structured scrape context when no Playwright page is available."""
    return {
        "holistic_check": dict(_HOLISTIC_EMPTY),
        "agent_runs_data": dict(_AGENT_RUNS_EMPTY),
        "related_submissions_data": dict(_RELATED_EMPTY),
        **unavailable_scrape_context(),
    }
