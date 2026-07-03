# Fill and submit the Shipd review form from a structured review dict.
#
# Strategy: Shipd's form controls (decision cards, band score cells,
# confidence segments, tag chips) are custom React components that are not
# reliably exposed as ARIA buttons. Elements are located in the DOM by
# normalized innerText with JavaScript, marked with a data attribute, then
# clicked through Playwright so the app receives trusted pointer events.

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import (
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from review.feedback_format import format_compact_author_note

LogFn = Callable[[str], None]

DEBUG_DIR = Path(__file__).resolve().parent.parent.parent / "logs" / "debug-submit"

MARK_ATTR = "data-shipd-agent-target"
MARK_SELECTOR = f"[{MARK_ATTR}='1']"

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

SUBMIT_BUTTON_PATTERN = re.compile(r"^Submit( Review)?$", re.I)

# Find a clickable element by normalized innerText, optionally scoped to the
# nearest container around a label (band heading), and mark it for Playwright.
_JS_FIND_AND_MARK = """
(args) => {
  const MARK = 'data-shipd-agent-target';
  document.querySelectorAll('[' + MARK + ']')
    .forEach((el) => el.removeAttribute(MARK));

  const norm = (t) => (t || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const isVisible = (el) => {
    const rect = el.getBoundingClientRect();
    if (rect.width < 1 || rect.height < 1) return false;
    const style = window.getComputedStyle(el);
    return style.visibility !== 'hidden' && style.display !== 'none';
  };
  const targets = args.targets.map(norm);
  const bestTargetIndex = (el) => {
    const text = norm(el.innerText);
    if (!text || text.length > 200) return 999;
    if (args.match === 'prefix') {
      for (let i = 0; i < targets.length; i++) {
        if (text.startsWith(targets[i])) return i;
      }
      return 999;
    }
    const idx = targets.indexOf(text);
    return idx >= 0 ? idx : 999;
  };
  const matches = (el) => bestTargetIndex(el) < 999;
  const isInteractive = (el) => {
    if (!el || !el.getAttribute) return false;
    const tag = el.tagName ? el.tagName.toLowerCase() : '';
    const role = el.getAttribute('role') || '';
    if (tag === 'button' || tag === 'a' || tag === 'label' ||
        ['button', 'radio', 'checkbox', 'tab', 'option'].includes(role)) {
      return true;
    }
    if (el.getAttribute('tabindex') === '0') return true;
    const style = window.getComputedStyle(el);
    return style.cursor === 'pointer';
  };
  const resolveClickable = (target, limit) => {
    let clickable = target;
    let node = target;
    while (node && node !== limit) {
      if (isInteractive(node)) {
        clickable = node;
        break;
      }
      node = node.parentElement;
    }
    return clickable;
  };
  const isNodeSelected = (el, checkVisual) => {
    let node = el;
    while (node && node !== document.body) {
      for (const attr of ['aria-pressed', 'aria-checked', 'aria-selected']) {
        if (node.getAttribute && node.getAttribute(attr) === 'true') return true;
      }
      const state = node.getAttribute ? node.getAttribute('data-state') : '';
      if (state && ['on', 'active', 'checked', 'selected'].includes(state)) {
        return true;
      }
      if (checkVisual) {
        const cls = String(node.className || '');
        if (/(^|\\s)(bg-|border-(?!border\\b)|ring-)/.test(cls) &&
            !/(^|\\s)hover:/.test(cls)) {
          return true;
        }
      }
      node = node.parentElement;
    }
    return false;
  };

  let scopes = [document.body];
  if (args.scopeHeading) {
    const headingNorm = norm(args.scopeHeading);
    let labels = Array.from(document.body.querySelectorAll('*'))
      .filter((el) => isVisible(el) && norm(el.innerText) === headingNorm);
    labels = labels.filter(
      (el) => !labels.some((o) => o !== el && el.contains(o))
    );
    if (!labels.length) {
      return { ok: false, reason: 'label not found: ' + args.scopeHeading };
    }
    scopes = [];
    for (const label of labels) {
      let node = label.parentElement;
      while (node && node !== document.body) {
        const found = Array.from(node.querySelectorAll('*'))
          .some((el) => isVisible(el) && matches(el));
        if (found) { scopes.push(node); break; }
        node = node.parentElement;
      }
    }
    if (!scopes.length) {
      return {
        ok: false,
        reason: 'no container near "' + args.scopeHeading + '" contains ' +
          JSON.stringify(args.targets),
      };
    }
  }

  for (const scope of scopes) {
    let candidates = Array.from(scope.querySelectorAll('*'))
      .filter((el) => isVisible(el) && matches(el));
    if (!candidates.length) continue;
    candidates = candidates.filter(
      (el) => !candidates.some((o) => o !== el && el.contains(o))
    );
    const limit = scope.parentElement || document.body;
    const ranked = candidates
      .map((el) => ({
        el,
        clickable: resolveClickable(el, limit),
        targetIdx: bestTargetIndex(el),
      }))
      .filter((item) =>
        !args.requireInteractive || isInteractive(item.clickable)
      )
      .sort((a, b) => {
        if (a.targetIdx !== b.targetIdx) return a.targetIdx - b.targetIdx;
        const aInteractive = isInteractive(a.clickable) ? 0 : 1;
        const bInteractive = isInteractive(b.clickable) ? 0 : 1;
        return aInteractive - bInteractive;
      });
    if (!ranked.length) continue;

    const { clickable } = ranked[0];
    const clsStr = String(clickable.className || '');

    clickable.setAttribute(MARK, '1');
    return {
      ok: true,
      tag: clickable.tagName,
      role: clickable.getAttribute('role') || '',
      text: norm(clickable.innerText).slice(0, 80),
      selected: isNodeSelected(clickable, args.checkVisual),
      cls: clsStr.slice(0, 300),
      disabled: !!clickable.disabled,
    };
  }
  return {
    ok: false,
    reason: 'no visible element matching ' + JSON.stringify(args.targets) +
      (args.scopeHeading ? ' near "' + args.scopeHeading + '"' : ''),
  };
}
"""


# Locate the per-band reason textarea revealed when a band scores below 3.
# Search between this band heading and the next section heading so we do not
# accidentally grab the author-note textarea further down the form.
_JS_FIND_BAND_REASON = """
(args) => {
  const MARK = 'data-shipd-agent-target';
  document.querySelectorAll('[' + MARK + ']')
    .forEach((el) => el.removeAttribute(MARK));

  const norm = (t) => (t || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const isVisible = (el) => {
    const rect = el.getBoundingClientRect();
    if (rect.width < 1 || rect.height < 1) return false;
    const style = window.getComputedStyle(el);
    return style.visibility !== 'hidden' && style.display !== 'none';
  };
  const isReasonField = (t) => {
    const ph = t.placeholder || '';
    const labelledBy = t.getAttribute('aria-labelledby') || '';
    if (/below 3|what kept it|reason/i.test(ph)) return true;
    if (/reason/i.test(t.getAttribute('aria-label') || '')) return true;
    const id = t.id;
    if (id) {
      const label = document.querySelector('label[for="' + id + '"]');
      if (label && /reason/i.test(label.innerText || '')) return true;
    }
    let prev = t.previousElementSibling;
    for (let i = 0; i < 3 && prev; i++) {
      if (/reason/i.test(prev.innerText || '')) return true;
      prev = prev.previousElementSibling;
    }
    return false;
  };

  const sectionHeadings = (args.sectionHeadings || []).map(norm);
  const headingNorm = norm(args.heading);
  let labels = Array.from(document.body.querySelectorAll('*'))
    .filter((el) => isVisible(el) && norm(el.innerText) === headingNorm);
  labels = labels.filter(
    (el) => !labels.some((o) => o !== el && el.contains(o))
  );
  if (!labels.length) {
    return { ok: false, reason: 'band label not found: ' + args.heading };
  }

  const label = labels[0];
  const allSections = Array.from(document.body.querySelectorAll('*'))
    .filter((el) => isVisible(el) &&
      sectionHeadings.includes(norm(el.innerText)))
    .sort((a, b) =>
      (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1
    );
  const startIdx = allSections.findIndex((el) => el === label);
  const nextSection = startIdx >= 0 ? allSections[startIdx + 1] : null;

  const inBandRange = (el) => {
    if (!(label.compareDocumentPosition(el) &
        Node.DOCUMENT_POSITION_FOLLOWING)) {
      return false;
    }
    if (nextSection && (nextSection.compareDocumentPosition(el) &
        Node.DOCUMENT_POSITION_FOLLOWING)) {
      return false;
    }
    return true;
  };

  const areas = Array.from(document.body.querySelectorAll('textarea'))
    .filter((t) => isVisible(t) && inBandRange(t));
  const match = areas.find(isReasonField);
  if (match) {
    match.setAttribute(MARK, '1');
    return { ok: true, placeholder: match.placeholder || '' };
  }
  return {
    ok: false,
    reason: 'no reason textarea between "' + args.heading + '" and next section',
    candidates: areas.length,
  };
}
"""

# Verify each band section has a score button in a selected state.
_JS_VERIFY_BAND_SCORES = """
(args) => {
  const norm = (t) => (t || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const isVisible = (el) => {
    const rect = el.getBoundingClientRect();
    if (rect.width < 1 || rect.height < 1) return false;
    const style = window.getComputedStyle(el);
    return style.visibility !== 'hidden' && style.display !== 'none';
  };
  const isInteractive = (el) => {
    if (!el || !el.getAttribute) return false;
    const tag = el.tagName ? el.tagName.toLowerCase() : '';
    const role = el.getAttribute('role') || '';
    if (tag === 'button' || tag === 'a' ||
        ['button', 'radio', 'checkbox', 'tab', 'option'].includes(role)) {
      return true;
    }
    if (el.getAttribute('tabindex') === '0') return true;
    const style = window.getComputedStyle(el);
    return style.cursor === 'pointer';
  };
  const isNodeOrAncestorSelected = (el) => {
    let node = el;
    while (node && node !== document.body) {
      for (const attr of ['aria-pressed', 'aria-checked', 'aria-selected']) {
        if (node.getAttribute && node.getAttribute(attr) === 'true') return true;
      }
      const state = node.getAttribute ? node.getAttribute('data-state') : '';
      if (state && ['on', 'active', 'checked', 'selected'].includes(state)) {
        return true;
      }
      const cls = String(node.className || '');
      // Shipd highlights the active score on the clickable wrapper.
      if (/(^|\\s)(bg-|border-(?!border\\b)|ring-)/.test(cls) &&
          !/(^|\\s)hover:/.test(cls)) {
        return true;
      }
      node = node.parentElement;
    }
    return false;
  };
  const scorePatterns = (args.scorePatterns || []).map(norm);
  const missing = [];

  for (const heading of args.headings) {
    const headingNorm = norm(heading);
    let labels = Array.from(document.body.querySelectorAll('*'))
      .filter((el) => isVisible(el) && norm(el.innerText) === headingNorm);
    labels = labels.filter(
      (el) => !labels.some((o) => o !== el && el.contains(o))
    );
    if (!labels.length) {
      missing.push(heading + ': heading not found');
      continue;
    }
    const label = labels[0];
    let scoped = false;
    let node = label.parentElement;
    while (node && node !== document.body) {
      const scoreButtons = Array.from(node.querySelectorAll('*'))
        .filter((el) => {
          if (!isVisible(el)) return false;
          const text = norm(el.innerText);
          return scorePatterns.some((p) => text === p || text.startsWith(p));
        })
        .filter((el) => !Array.from(node.querySelectorAll('*'))
          .some((o) => o !== el && el.contains(o) && isVisible(o) &&
            scorePatterns.some((p) =>
              norm(o.innerText) === p || norm(o.innerText).startsWith(p)
            )
          )
        );
      if (scoreButtons.length >= 4) {
        scoped = true;
        const interactive = scoreButtons.filter((el) => {
          let node = el;
          while (node && node !== label.parentElement) {
            if (isInteractive(node)) return true;
            node = node.parentElement;
          }
          return false;
        });
        const cells = interactive.length >= 4 ? interactive : scoreButtons;
        if (!cells.some(isNodeOrAncestorSelected)) {
          missing.push(heading + ': no score selected');
        }
        break;
      }
      node = node.parentElement;
    }
    if (!scoped) {
      missing.push(heading + ': score row not found');
    }
  }
  return { ok: missing.length === 0, missing };
}
"""


def _noop_log(message: str) -> None:
    print(f"[submit] {message}", flush=True)


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


def _score_targets(score: int) -> list[str]:
    """Text variants for a band score cell, most specific first."""
    label = SCORE_LABELS[score]
    return [
        f"{score} | {label}",
        f"{score}|{label}",
        f"{score} {label}",
        f"{score}\n| {label}",
        f"{score}\n{label}",
        label,
        str(score),
    ]


def _band_section_headings() -> list[str]:
    return list(BAND_HEADINGS.values()) + ["Other notes"]


def _score_patterns() -> list[str]:
    patterns: list[str] = []
    for score, label in SCORE_LABELS.items():
        patterns.extend(_score_targets(score))
    return patterns


def _section_has_score_buttons(section: Locator) -> bool:
    """True when a band container includes 0–3 score buttons."""
    for label in SCORE_LABELS.values():
        if section.get_by_role("button", name=label).count():
            return True
    for score in SCORE_LABELS:
        if section.get_by_role(
            "button", name=re.compile(rf"^{score}\b")
        ).count():
            return True
    return False


def _pick_tightest_band_section(candidates: list[Locator]) -> Locator | None:
    """Prefer the smallest container that still has 0–3 score buttons."""
    best: Locator | None = None
    best_count = float("inf")
    for section in candidates:
        if not _section_has_score_buttons(section):
            continue
        count = section.get_by_role("button").count()
        if count < best_count:
            best = section
            best_count = count
    return best


def _band_section(page: Page, heading: str) -> Locator:
    """Locate the band rating block by its section heading."""
    heading_loc = page.get_by_role("heading", name=heading)
    if not heading_loc.count():
        heading_loc = page.get_by_text(heading, exact=True)
    heading_loc = heading_loc.first
    heading_loc.wait_for(state="visible", timeout=15_000)
    heading_loc.scroll_into_view_if_needed()

    ancestors = heading_loc.locator(
        "xpath=ancestor::*[self::section or self::div or self::fieldset]"
    )
    ancestor_sections = [ancestors.nth(i) for i in range(ancestors.count())]
    section = _pick_tightest_band_section(ancestor_sections)
    if section is not None:
        return section

    following = heading_loc.locator(
        "xpath=following::*[self::section or self::div or self::fieldset]"
    )
    fallback_sections = [following.nth(i) for i in range(min(following.count(), 8))]
    section = _pick_tightest_band_section(fallback_sections)
    if section is not None:
        return section

    raise RuntimeError(
        f"Could not find band section with score buttons for {heading!r}."
    )


def _click_band_score_playwright(section: Locator, score: int) -> None:
    if score not in SCORE_LABELS:
        raise ValueError(f"Band score must be 0-3, got {score!r}")

    label = SCORE_LABELS[score]
    candidates = (
        str(score),
        f"{score} {label}",
        f"{score} | {label}",
        label,
        f"{score}\n{label}",
        f"{score}\n| {label}",
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


def _click_band_confidence_playwright(section: Locator, confidence: str) -> None:
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
    if not button.count():
        raise RuntimeError(
            f"Could not find confidence control {ui_label!r} in band section."
        )
    button.first.scroll_into_view_if_needed()
    button.first.click()


def capture_failure(page: Page, label: str, *, log: LogFn = _noop_log) -> None:
    """Save a full-page screenshot + control dump for a failed submit step."""
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shot = DEBUG_DIR / f"{stamp}-{label}.png"
        page.screenshot(path=str(shot), full_page=True)
        dump = {
            "label": label,
            "url": page.url,
            "buttons": [
                b.inner_text(timeout=500).strip().replace("\n", " | ")
                for b in page.get_by_role("button").all()[:60]
            ],
        }
        (DEBUG_DIR / f"{stamp}-{label}.json").write_text(
            json.dumps(dump, indent=2), encoding="utf-8"
        )
        log(f"submit: failure snapshot saved to {shot}")
    except Exception as exc:  # snapshot must never mask the original error
        log(f"submit: WARNING could not capture failure snapshot: {exc}")


def _find_and_mark(
    page: Page,
    targets: list[str],
    *,
    scope_heading: str | None = None,
    match: str = "exact",
    check_visual: bool = False,
    require_interactive: bool = False,
) -> dict:
    return page.evaluate(
        _JS_FIND_AND_MARK,
        {
            "targets": targets,
            "scopeHeading": scope_heading,
            "match": match,
            "checkVisual": check_visual,
            "requireInteractive": require_interactive,
        },
    )


def _click_target(
    page: Page,
    targets: list[str],
    *,
    scope_heading: str | None = None,
    match: str = "exact",
    log: LogFn = _noop_log,
    description: str = "",
) -> dict:
    """Locate by text via JS, then click with trusted Playwright events."""
    result = _find_and_mark(
        page, targets, scope_heading=scope_heading, match=match
    )
    if not result.get("ok"):
        raise RuntimeError(
            f"submit: could not locate {description or targets}: "
            f"{result.get('reason', 'unknown')}"
        )
    element = page.locator(MARK_SELECTOR).first
    element.scroll_into_view_if_needed(timeout=5_000)
    element.click(timeout=5_000)
    page.evaluate(
        f"document.querySelectorAll('[{MARK_ATTR}]')"
        f".forEach((el) => el.removeAttribute('{MARK_ATTR}'))"
    )
    log(
        f"submit: clicked {description or targets[0]} "
        f"(<{result.get('tag', '?').lower()}> {result.get('text', '')!r})"
    )
    return result


def _unmark(page: Page) -> None:
    page.evaluate(
        f"document.querySelectorAll('[{MARK_ATTR}]')"
        f".forEach((el) => el.removeAttribute('{MARK_ATTR}'))"
    )


def _click_selectable(
    page: Page,
    targets: list[str],
    *,
    scope_heading: str | None = None,
    match: str = "exact",
    log: LogFn = _noop_log,
    description: str = "",
    attempts: int = 3,
    require_interactive: bool = False,
) -> None:
    """Click a toggle/segmented control and verify the selection registered.

    Skips the click only when aria/data-state already marks the target selected
    (score cells behave like toggles — a second click can deselect). Retries
    until the selected state is observed or attempts are exhausted.
    """
    info = _find_and_mark(
        page,
        targets,
        scope_heading=scope_heading,
        match=match,
        require_interactive=require_interactive,
    )
    if not info.get("ok"):
        raise RuntimeError(
            f"submit: could not locate {description or targets}: "
            f"{info.get('reason', 'unknown')}"
        )
    if info.get("selected"):
        _unmark(page)
        log(f"submit: {description or targets[0]} already selected — skipping")
        return

    for attempt in range(1, attempts + 1):
        element = page.locator(MARK_SELECTOR).first
        element.scroll_into_view_if_needed(timeout=5_000)
        element.click(timeout=5_000)
        page.wait_for_timeout(350)

        # Re-locate: React may have re-rendered the control after the click.
        info = _find_and_mark(
            page,
            targets,
            scope_heading=scope_heading,
            match=match,
            check_visual=True,
            require_interactive=require_interactive,
        )
        if not info.get("ok"):
            raise RuntimeError(
                f"submit: {description or targets} disappeared after click: "
                f"{info.get('reason', 'unknown')}"
            )
        if info.get("selected"):
            _unmark(page)
            log(
                f"submit: {description or targets[0]} selected "
                f"(<{info.get('tag', '?').lower()}> attempt {attempt})"
            )
            return
        log(
            f"submit: {description or targets[0]} click did not register "
            f"(attempt {attempt}/{attempts}) — retrying"
        )

    _unmark(page)
    raise RuntimeError(
        f"submit: {description or targets} never showed a selected state "
        f"after {attempts} clicks."
    )


def _verify_band_scores(page: Page, *, log: LogFn = _noop_log) -> None:
    """Raise when any band is missing a selected 0–3 score."""
    result = page.evaluate(
        _JS_VERIFY_BAND_SCORES,
        {
            "headings": list(BAND_HEADINGS.values()),
            "scorePatterns": _score_patterns(),
        },
    )
    if result.get("ok"):
        log("submit: all band scores verified on form")
        return
    missing = result.get("missing") or ["unknown"]
    raise RuntimeError(
        "submit: band scores not registered on form — "
        + "; ".join(str(item) for item in missing)
    )


def _ensure_submit_review_form(page: Page, *, log: LogFn = _noop_log) -> None:
    """Open the submit-review form if the decision cards are not visible."""
    for marker in SUBMIT_FORM_MARKERS:
        if page.get_by_text(marker, exact=True).count():
            log("submit: review form already open")
            return

    for name in OPEN_SUBMIT_BUTTONS:
        opener = page.get_by_role("button", name=name).or_(
            page.get_by_role("link", name=name)
        )
        if opener.count() and opener.first.is_visible():
            opener.first.scroll_into_view_if_needed()
            opener.first.click()
            log(f"submit: opened review form via {name!r}")
            break
    else:
        tab = page.get_by_role("tab", name=re.compile(r"submit|review", re.I))
        if tab.count() and tab.first.is_visible():
            tab.first.click()
            log("submit: opened review form via tab")

    page.get_by_text(SUBMIT_FORM_MARKERS[0], exact=True).first.wait_for(
        state="attached",
        timeout=30_000,
    )
    log("submit: review form ready (decision cards present)")


def _click_decision(page: Page, decision: str, *, log: LogFn = _noop_log) -> None:
    normalized = _normalize_decision(decision)
    label = DECISION_LABELS[normalized]
    _click_selectable(
        page,
        [label],
        match="prefix",
        log=log,
        description=f"decision card {label!r}",
    )


def _fill_band(
    page: Page,
    heading: str,
    *,
    score: int,
    confidence: str,
    reasoning: str = "",
    log: LogFn = _noop_log,
) -> None:
    if score not in SCORE_LABELS:
        raise ValueError(f"Band score must be 0-3, got {score!r}")

    used_playwright = False
    try:
        section = _band_section(page, heading)
        _click_band_score_playwright(section, score)
        page.wait_for_timeout(350)
        section = _band_section(page, heading)
        _click_band_confidence_playwright(section, str(confidence))
        used_playwright = True
        log(
            f"submit: {heading} score {score} ({SCORE_LABELS[score]}) "
            f"and confidence via Playwright"
        )
    except (RuntimeError, PlaywrightTimeoutError, ValueError) as exc:
        log(
            f"submit: Playwright band controls failed for {heading!r} "
            f"({exc}); using text-match fallback"
        )

    if not used_playwright:
        _click_selectable(
            page,
            _score_targets(score),
            scope_heading=heading,
            log=log,
            description=f"{heading} score {score} ({SCORE_LABELS[score]})",
            require_interactive=True,
        )

        ui_label = CONFIDENCE_UI[_normalize_confidence(confidence)]
        try:
            _click_selectable(
                page,
                [ui_label],
                scope_heading=heading,
                log=log,
                description=f"{heading} confidence {ui_label}",
                require_interactive=True,
            )
        except RuntimeError as exc:
            # Confidence is secondary — do not abort the whole submission.
            log(f"submit: WARNING confidence for {heading!r} not set: {exc}")

    if score >= 3:
        return

    _fill_band_reason(
        page,
        heading,
        reasoning=reasoning,
        score=score,
        log=log,
    )


def _fill_band_reason(
    page: Page,
    heading: str,
    *,
    reasoning: str,
    score: int,
    log: LogFn = _noop_log,
) -> None:
    """Fill the required reason textarea for a band scored below 3."""
    text = " ".join(str(reasoning).split()).strip()
    if not text:
        text = f"Scored {score}/3 — see author note for details."

    deadline = time.monotonic() + 5.0
    info: dict[str, Any] = {}
    while time.monotonic() < deadline:
        info = page.evaluate(
            _JS_FIND_BAND_REASON,
            {
                "heading": heading,
                "sectionHeadings": _band_section_headings(),
            },
        )
        if info.get("ok"):
            break
        page.wait_for_timeout(250)

    if not info.get("ok"):
        raise RuntimeError(
            f"submit: {heading} scored {score} (<3) but its reason "
            f"textarea was not found: {info.get('reason', 'unknown')}. "
            "Shipd will not enable Submit without it."
        )

    field = page.locator(MARK_SELECTOR).first
    field.scroll_into_view_if_needed(timeout=5_000)
    field.click()
    field.fill(text)
    filled = field.input_value()
    _unmark(page)
    if filled.strip() != text.strip():
        raise RuntimeError(
            f"submit: reason for {heading} did not persist after fill."
        )
    log(
        f"submit: {heading} reason filled ({len(text)} chars, "
        f"score {score} < 3)"
    )


def _fill_band_ratings(
    page: Page,
    band_ratings: dict[str, Any],
    *,
    log: LogFn = _noop_log,
) -> None:
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
        _fill_band(
            page,
            heading,
            score=int(score),
            confidence=str(confidence),
            reasoning=str(band.get("reasoning", "")),
            log=log,
        )

    _verify_band_scores(page, log=log)


def _fill_band_reasons(
    page: Page,
    band_ratings: dict[str, Any],
    *,
    log: LogFn = _noop_log,
) -> None:
    """Backfill any band reason fields that were not filled during scoring."""
    for band_key, heading in BAND_HEADINGS.items():
        band = band_ratings.get(band_key) or {}
        score = int(band.get("score", 3))
        if score >= 3:
            continue

        info = page.evaluate(
            _JS_FIND_BAND_REASON,
            {
                "heading": heading,
                "sectionHeadings": _band_section_headings(),
            },
        )
        if not info.get("ok"):
            raise RuntimeError(
                f"submit: {heading} scored {score} (<3) but its reason "
                f"textarea was not found: {info.get('reason', 'unknown')}. "
                "Shipd will not enable Submit without it."
            )

        field = page.locator(MARK_SELECTOR).first
        existing = field.input_value().strip()
        _unmark(page)
        if existing:
            log(f"submit: {heading} reason already filled — skipping")
            continue

        _fill_band_reason(
            page,
            heading,
            reasoning=str(band.get("reasoning", "")),
            score=score,
            log=log,
        )


def _author_note_field(page: Page):
    for label in AUTHOR_NOTE_LABELS:
        field = page.get_by_label(label, exact=False)
        if field.count():
            return field.first

    result = _find_and_mark(
        page, list(AUTHOR_NOTE_LABELS), match="prefix"
    )
    if result.get("ok"):
        marked = page.locator(MARK_SELECTOR).first
        container = marked.locator("xpath=ancestor::*[position()<=3]")
        for i in range(container.count()):
            field = container.nth(i).locator("textarea")
            if field.count():
                return field.first

    field = page.get_by_role("textbox", name=re.compile(r"note.*author", re.I))
    if field.count():
        return field.first

    # Last resort: first textarea that is not a band-reason field.
    areas = page.locator("textarea")
    for i in range(areas.count()):
        candidate = areas.nth(i)
        placeholder = candidate.get_attribute("placeholder") or ""
        if not re.search(r"below 3|reason", placeholder, re.I):
            return candidate
    return areas.first


def _fill_author_note(
    page: Page,
    review: dict[str, Any],
    *,
    log: LogFn = _noop_log,
) -> None:
    note = format_compact_author_note(review)
    if not note:
        log("submit: no author note to fill")
        return

    field = _author_note_field(page)
    field.wait_for(state="visible", timeout=15_000)
    field.scroll_into_view_if_needed()
    field.click()
    field.fill(note)

    filled = field.input_value()
    if filled.strip() != note.strip():
        raise RuntimeError(
            "submit: author note did not persist after fill "
            f"(expected {len(note)} chars, field has {len(filled)})."
        )
    log(f"submit: author note filled ({len(note)} chars)")


def _click_suggested_tags(
    page: Page,
    tags: list[Any],
    *,
    log: LogFn = _noop_log,
) -> None:
    for tag in tags:
        tag_text = str(tag).strip()
        if not tag_text:
            continue
        try:
            _click_target(
                page,
                [tag_text],
                log=log,
                description=f"tag {tag_text!r}",
            )
        except RuntimeError:
            log(f"submit: tag {tag_text!r} not found on form (skipped)")


def _set_downgrade_to_mars(
    page: Page,
    *,
    enabled: bool,
    log: LogFn = _noop_log,
) -> None:
    checkbox = page.get_by_role(
        "checkbox",
        name=re.compile(r"downgrade.*mars", re.I),
    )
    if not checkbox.count():
        checkbox = page.get_by_label(re.compile(r"downgrade.*mars", re.I))

    if checkbox.count():
        box = checkbox.first
        box.scroll_into_view_if_needed()
        if box.is_checked() != enabled:
            box.click()
            log(f"submit: downgrade-to-Mars set to {enabled}")
        return

    if enabled:
        try:
            _click_target(
                page,
                ["Downgrade to Mars"],
                match="prefix",
                log=log,
                description="Downgrade to Mars toggle",
            )
        except RuntimeError as exc:
            log(f"submit: WARNING downgrade-to-Mars control not found: {exc}")


def _submit_button(page: Page):
    return page.get_by_role("button", name=SUBMIT_BUTTON_PATTERN)


def _on_review_page(page: Page) -> bool:
    return "/challenges/" in page.url


def _wait_submit_enabled(page: Page, *, timeout_sec: float = 15.0) -> bool:
    """Wait for the final Submit Review button to become enabled."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if not _on_review_page(page):
            return False
        button = _submit_button(page)
        if button.count() and button.first.is_visible() and button.first.is_enabled():
            return True
        page.wait_for_timeout(300)
    return False


def _wait_submit_confirmation(page: Page, *, timeout_sec: float = 20.0) -> str:
    """After clicking Submit, wait for evidence the review was accepted.

    Returns "confirmed" when the form is gone (navigation or re-render) or a
    success message appears; "unconfirmed" when nothing observable changed.
    """
    start_url = page.url
    success_text = page.get_by_text(
        re.compile(r"review (submitted|received)|thank(s| you)", re.I)
    )
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if page.url != start_url:
            return "confirmed"
        if success_text.count():
            return "confirmed"
        button = _submit_button(page)
        if not button.count():
            return "confirmed"
        try:
            first = button.first
            if not first.is_visible() or not first.is_enabled():
                return "confirmed"
        except PlaywrightTimeoutError:
            return "confirmed"
        page.wait_for_timeout(400)
    return "unconfirmed"


def _finalize_submission(page: Page, *, log: LogFn = _noop_log) -> bool:
    if not _wait_submit_enabled(page):
        raise RuntimeError(
            "Submit Review button never became enabled — the form did not "
            "register all inputs (Shipd requires all three band scores). "
            "Check the failure snapshot in logs/debug-submit."
        )

    button = _submit_button(page).first
    button.scroll_into_view_if_needed()
    button.click()
    log("submit: Submit Review clicked; waiting for confirmation")

    outcome = _wait_submit_confirmation(page)
    if outcome == "confirmed":
        log("submit: review submission confirmed")
        return True
    log(
        "submit: WARNING could not confirm submission "
        "(Submit button still enabled after 20s)"
    )
    return False


def _fill_submit_form(
    page: Page,
    review: dict[str, Any],
    band_ratings: dict[str, Any],
    *,
    quest: str,
    log: LogFn,
) -> None:
    _ensure_submit_review_form(page, log=log)
    _click_decision(page, str(review["decision"]), log=log)
    _fill_band_ratings(page, band_ratings, log=log)
    _fill_band_reasons(page, band_ratings, log=log)
    _fill_author_note(page, review, log=log)
    _click_suggested_tags(page, list(review.get("suggested_tags") or []), log=log)

    if review.get("downgrade_to_mars") and quest == "olympus":
        _set_downgrade_to_mars(page, enabled=True, log=log)


def _recover_review_page(page: Page, review_url: str, *, log: LogFn) -> None:
    """Navigate back to the review after Shipd bounced us to the queue."""
    log(f"submit: page left the review ({page.url}) — reopening {review_url}")
    page.goto(review_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1_500)
    if not _on_review_page(page):
        raise RuntimeError(
            "Shipd keeps redirecting to the review queue — the reservation "
            "for this submission is no longer active (reservations are "
            "released when the reviewing browser session closes). Re-reserve "
            "and review again with submit enabled in the same session."
        )


def submit_review(
    page: Page,
    review: dict[str, Any],
    *,
    quest: str,
    finalize: bool = True,
    log: LogFn = _noop_log,
    review_url: str = "",
) -> bool:
    """Fill the Shipd submit-review form from a structured review dict.

    Returns True when the submission was confirmed (or, with finalize=False,
    when the form was fully filled and the Submit button became enabled).
    Raises RuntimeError with a debug snapshot on any unrecoverable step.
    """
    if not review.get("decision"):
        raise ValueError("review dict must include 'decision'.")
    band_ratings = review.get("band_ratings")
    if not isinstance(band_ratings, dict):
        raise ValueError("review dict must include 'band_ratings'.")

    target_url = review_url.strip() or (
        page.url if _on_review_page(page) else ""
    )

    try:
        # One recovery pass: if Shipd bounces the page to the queue mid-fill,
        # reopen the review and refill (all fill steps are idempotent).
        for attempt in range(1, 3):
            if not _on_review_page(page):
                if not target_url:
                    raise RuntimeError(
                        f"submit: not on a review page ({page.url}) and no "
                        "review_url provided to recover with."
                    )
                _recover_review_page(page, target_url, log=log)

            try:
                _fill_submit_form(
                    page, review, band_ratings, quest=quest, log=log
                )
            except (RuntimeError, PlaywrightTimeoutError) as exc:
                if attempt == 1 and not _on_review_page(page) and target_url:
                    log(f"submit: fill interrupted by navigation ({exc}); retrying")
                    continue
                raise

            if not finalize:
                enabled = _wait_submit_enabled(page, timeout_sec=8.0)
                log(
                    "submit: dry fill complete — Submit button "
                    f"{'enabled' if enabled else 'NOT enabled'}"
                )
                if not enabled:
                    capture_failure(page, "dry-fill-submit-disabled", log=log)
                return enabled

            if not _wait_submit_enabled(page):
                if attempt == 1 and not _on_review_page(page) and target_url:
                    log("submit: page bounced to the queue while waiting; retrying")
                    continue
                raise RuntimeError(
                    "Submit Review button never became enabled — the form did "
                    "not register all inputs (Shipd requires all three band "
                    "scores and a reason for any band below 3). Check the "
                    "failure snapshot in logs/debug-submit."
                )
            return _finalize_submission(page, log=log)

        raise RuntimeError("submit: form fill did not survive page navigation.")
    except Exception:
        capture_failure(page, "submit-failed", log=log)
        raise
