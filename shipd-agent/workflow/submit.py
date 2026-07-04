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

OTHER_NOTES_PATTERN = re.compile(
    r"outside the rubric|sent to the author|internal note",
    re.I,
)

REASON_FIELD_PATTERN = re.compile(
    r"below\s*3|what kept it|one line|reason required|reason|explain why|"
    r"required when|not clean|score.*below",
    re.I,
)

CONFIDENCE_LABELS = ("Low", "Med", "High")

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
        if (/(^|\\s)(bg-(primary|accent|secondary|amber|orange|yellow|emerald|green|blue|destructive))/.test(cls)) {
          return true;
        }
        if (/(^|\\s)(ring-2|ring-primary|border-primary|border-amber|border-orange)/.test(cls)) {
          return true;
        }
        if (/(^|\\s)text-foreground/.test(cls) &&
            !/(^|\\s)text-muted-foreground/.test(cls) &&
            /font-(semibold|bold|medium)/.test(cls)) {
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
      .filter((item) => {
        if (!args.requireButton) return true;
        const tag = item.clickable.tagName
          ? item.clickable.tagName.toLowerCase() : '';
        const role = item.clickable.getAttribute('role') || '';
        return tag === 'button' || role === 'button';
      })
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


# Locate the per-band reason field revealed when a band scores below 3.
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
  const reasonPattern = /below\\s*3|what kept it|reason|explain why|required when|not clean|score.*below/i;
  const nearbyReasonText = (el) => {
    let node = el;
    for (let depth = 0; depth < 5 && node; depth++) {
      const text = norm(node.innerText || node.textContent || '');
      if (text.length > 0 && text.length < 500 && reasonPattern.test(text)) {
        return true;
      }
      const prev = node.previousElementSibling;
      if (prev) {
        const prevText = norm(prev.innerText || prev.textContent || '');
        if (prevText.length > 0 && prevText.length < 300 &&
            reasonPattern.test(prevText)) {
          return true;
        }
      }
      node = node.parentElement;
    }
    return false;
  };
  const isReasonField = (t) => {
    const ph = t.placeholder || t.getAttribute('placeholder') || '';
    const ariaLabel = t.getAttribute('aria-label') || '';
    const name = t.getAttribute('name') || '';
    if (/outside the rubric|sent to the author|internal note/i.test(ph) ||
        /outside the rubric|sent to the author|internal note/i.test(ariaLabel)) {
      return false;
    }
    if (reasonPattern.test(ph) || reasonPattern.test(ariaLabel) ||
        reasonPattern.test(name)) {
      return true;
    }
    const labelledBy = t.getAttribute('aria-labelledby') || '';
    if (labelledBy) {
      for (const id of labelledBy.split(/\\s+/)) {
        const ref = document.getElementById(id);
        if (ref && reasonPattern.test(ref.innerText || ref.textContent || '')) {
          return true;
        }
      }
    }
    const id = t.id;
    if (id) {
      const label = document.querySelector('label[for="' + id + '"]');
      if (label && reasonPattern.test(label.innerText || '')) return true;
    }
    let prev = t.previousElementSibling;
    for (let i = 0; i < 4 && prev; i++) {
      if (reasonPattern.test(prev.innerText || prev.textContent || '')) {
        return true;
      }
      prev = prev.previousElementSibling;
    }
    return nearbyReasonText(t);
  };
  const isSectionHeading = (el, headingNorm) => {
    if (!isVisible(el) || norm(el.innerText) !== headingNorm) return false;
    const tag = el.tagName ? el.tagName.toLowerCase() : '';
    const role = el.getAttribute('role') || '';
    if (/^h[1-6]$/.test(tag) || role === 'heading') return true;
    // Prefer compact labels; skip large containers whose innerText includes
    // score/confidence controls.
    const text = norm(el.innerText);
    return text.length <= headingNorm.length + 8;
  };
  const leafMost = (elements) =>
    elements.filter(
      (el) => !elements.some((o) => o !== el && el.contains(o))
    );

  const sectionHeadings = (args.sectionHeadings || []).map(norm);
  const headingNorm = norm(args.heading);
  let labels = leafMost(
    Array.from(document.body.querySelectorAll('*'))
      .filter((el) => isVisible(el) && norm(el.innerText) === headingNorm)
  );
  if (!labels.length) {
    return { ok: false, reason: 'band label not found: ' + args.heading };
  }

  const scorePatterns = (args.scorePatterns || []).map(norm);
  const confidenceTargets = (args.confidenceTargets || []).map(norm);
  const matchesScoreText = (text) =>
    scorePatterns.some((p) => text === p || text.startsWith(p));
  const isInteractive = (el) => {
    if (!el || !el.getAttribute) return false;
    const tag = el.tagName ? el.tagName.toLowerCase() : '';
    const role = el.getAttribute('role') || '';
    if (tag === 'button' || tag === 'a' || tag === 'label' ||
        ['button', 'radio', 'checkbox', 'tab', 'option'].includes(role)) {
      return true;
    }
    if (el.getAttribute('tabindex') === '0') return true;
    return window.getComputedStyle(el).cursor === 'pointer';
  };
  const closestClickable = (el) => {
    let node = el;
    while (node && node !== document.body) {
      if (isInteractive(node)) return node;
      node = node.parentElement;
    }
    return el;
  };
  const fieldSelector =
    'textarea, [contenteditable="true"], [role="textbox"], input[type="text"]';
  const isOtherNotesField = (t) => {
    const ph = t.placeholder || t.getAttribute('placeholder') || '';
    const ariaLabel = t.getAttribute('aria-label') || '';
    return /outside the rubric|sent to the author|internal note/i.test(ph) ||
      /outside the rubric|sent to the author|internal note/i.test(ariaLabel);
  };
  const pickReasonField = (inputs) => {
    const visible = inputs.filter((el) => isVisible(el) && !isOtherNotesField(el));
    const match = visible.find(isReasonField);
    if (match) return match;
    const nearRequired = visible.find((el) => {
      let node = el.parentElement;
      for (let d = 0; d < 6 && node; d++) {
        const text = (node.innerText || node.textContent || '').toLowerCase();
        if (text.includes('reason required') || reasonPattern.test(text)) {
          return true;
        }
        node = node.parentElement;
      }
      return false;
    });
    if (nearRequired) return nearRequired;
    if (visible.length === 1) return visible[0];
    const textareas = visible.filter(
      (el) => (el.tagName || '').toLowerCase() === 'textarea'
    );
    if (textareas.length === 1) return textareas[0];
    if (textareas.length > 1) return textareas[textareas.length - 1];
    return null;
  };
  const scopeFromLabel = (label) => {
    let node = label.parentElement;
    while (node && node !== document.body) {
      const scoreLeaves = leafMost(
        Array.from(node.querySelectorAll('*'))
          .filter((el) =>
            isVisible(el) && matchesScoreText(norm(el.innerText))
          )
      );
      const scoreClickables = [];
      for (const el of scoreLeaves) {
        const cell = closestClickable(el);
        if (!scoreClickables.includes(cell)) scoreClickables.push(cell);
      }
      if (scoreClickables.length >= 4) {
        let scope = node;
        const parent = node.parentElement;
        if (parent && parent !== document.body) {
          const parentScoreLeaves = leafMost(
            Array.from(parent.querySelectorAll('*'))
              .filter((el) =>
                isVisible(el) && matchesScoreText(norm(el.innerText))
              )
          );
          const parentScoreClickables = [];
          for (const el of parentScoreLeaves) {
            const cell = closestClickable(el);
            if (!parentScoreClickables.includes(cell)) {
              parentScoreClickables.push(cell);
            }
          }
          const confInParent = leafMost(
            Array.from(parent.querySelectorAll('*'))
              .filter((el) => {
                if (!isVisible(el)) return false;
                const t = norm(el.innerText);
                return confidenceTargets.includes(t) || t === 'medium';
              })
          );
          if (parentScoreClickables.length >= 4 && confInParent.length >= 3) {
            scope = parent;
          }
        }
        return scope;
      }
      node = node.parentElement;
    }
    return null;
  };
  const findBandScope = () => {
    for (const label of labels) {
      const scope = scopeFromLabel(label);
      if (scope) return scope;
    }
    return null;
  };

  const scope = findBandScope();
  if (scope) {
    const chosen = pickReasonField(
      Array.from(scope.querySelectorAll(fieldSelector))
    );
    if (chosen) {
      chosen.setAttribute(MARK, '1');
      return {
        ok: true,
        placeholder: chosen.placeholder || chosen.getAttribute('placeholder') || '',
        tag: chosen.tagName,
        via: 'scope',
      };
    }
  }

  const label2 = labels[0];
  const allSections = leafMost(
    Array.from(document.body.querySelectorAll('*'))
      .filter((el) =>
        sectionHeadings.some((h) => isSectionHeading(el, h))
      )
  ).sort((a, b) =>
    (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1
  );
  const startIdx = allSections.findIndex((el) => el === label2);
  const nextSection = startIdx >= 0 ? allSections[startIdx + 1] : null;

  const inBandRange = (el) => {
    if (!(label2.compareDocumentPosition(el) &
        Node.DOCUMENT_POSITION_FOLLOWING)) {
      return false;
    }
    if (nextSection && (nextSection.compareDocumentPosition(el) &
        Node.DOCUMENT_POSITION_FOLLOWING)) {
      return false;
    }
    return true;
  };

  const textInputs = Array.from(
    document.body.querySelectorAll(fieldSelector)
  ).filter((t) => isVisible(t) && inBandRange(t));
  const chosen = pickReasonField(textInputs);
  if (chosen) {
    chosen.setAttribute(MARK, '1');
    return {
      ok: true,
      placeholder: chosen.placeholder || chosen.getAttribute('placeholder') || '',
      tag: chosen.tagName,
      via: 'range',
    };
  }
  return {
    ok: false,
    reason: 'no reason textarea between "' + args.heading + '" and next section',
    candidates: textInputs.map((t) => ({
      tag: t.tagName,
      placeholder: t.placeholder || t.getAttribute('placeholder') || '',
      ariaLabel: t.getAttribute('aria-label') || '',
      role: t.getAttribute('role') || '',
    })),
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
      if (/(^|\\s)(bg-(primary|accent|secondary|amber|orange|yellow|emerald|green|blue|destructive))/.test(cls)) {
        return true;
      }
      if (/(^|\\s)(ring-2|ring-primary|border-primary|border-amber|border-orange)/.test(cls)) {
        return true;
      }
      if (/(^|\\s)text-foreground/.test(cls) &&
          !/(^|\\s)text-muted-foreground/.test(cls) &&
          /font-(semibold|bold|medium)/.test(cls)) {
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

# Read/write text on textarea, contenteditable, or role=textbox fields.
_JS_READ_FIELD_TEXT = """
(el) => {
  if (!el) return '';
  const tag = el.tagName ? el.tagName.toLowerCase() : '';
  if (tag === 'textarea' || tag === 'input') return el.value || '';
  return (el.innerText || el.textContent || '').trim();
}
"""

_JS_WRITE_FIELD_TEXT = """
(args) => {
  const el = args.el;
  const text = args.text || '';
  if (!el) return { ok: false, len: 0 };
  el.scrollIntoView({ block: 'center', inline: 'nearest' });
  el.focus();
  const tag = el.tagName ? el.tagName.toLowerCase() : '';
  if (tag === 'textarea' || tag === 'input') {
    const proto = tag === 'textarea'
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, text);
  } else {
    el.textContent = text;
  }
  el.dispatchEvent(new InputEvent('input', { bubbles: true, data: text }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  el.dispatchEvent(new Event('blur', { bubbles: true }));
  let read = '';
  if (tag === 'textarea' || tag === 'input') read = el.value || '';
  else read = (el.innerText || el.textContent || '').trim();
  return { ok: read.trim() === text.trim(), len: read.trim().length };
}
"""

# Snapshot Shipd review-form state for validation and submit-button diagnostics.
_JS_FORM_VALIDATION_STATE = """
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
    return window.getComputedStyle(el).cursor === 'pointer';
  };
  const isNodeSelected = (el) => {
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
      if (/(^|\\s)(bg-(primary|accent|secondary|amber|orange|yellow|emerald|green|blue|destructive))/.test(cls)) {
        return true;
      }
      if (/(^|\\s)(ring-2|ring-primary|border-primary|border-amber|border-orange)/.test(cls)) {
        return true;
      }
      if (/(^|\\s)text-foreground/.test(cls) &&
          !/(^|\\s)text-muted-foreground/.test(cls) &&
          /font-(semibold|bold|medium)/.test(cls)) {
        return true;
      }
      node = node.parentElement;
    }
    return false;
  };
  const readField = (el) => {
    if (!el) return '';
    const tag = el.tagName ? el.tagName.toLowerCase() : '';
    if (tag === 'textarea' || tag === 'input') return (el.value || '').trim();
    return (el.innerText || el.textContent || '').trim();
  };
  const reasonPattern = /below\\s*3|what kept it|one line|reason required|reason|explain why|required when|not clean|score.*below/i;
  const isReasonField = (t) => {
    const ph = t.placeholder || t.getAttribute('placeholder') || '';
    const ariaLabel = t.getAttribute('aria-label') || '';
    const name = t.getAttribute('name') || '';
    if (/outside the rubric|sent to the author|internal note/i.test(ph) ||
        /outside the rubric|sent to the author|internal note/i.test(ariaLabel)) {
      return false;
    }
    if (reasonPattern.test(ph) || reasonPattern.test(ariaLabel) ||
        reasonPattern.test(name)) {
      return true;
    }
    let node = t.parentElement;
    for (let depth = 0; depth < 4 && node; depth++) {
      const text = norm(node.innerText || node.textContent || '');
      if (text.includes('reason required') || reasonPattern.test(text)) {
        return true;
      }
      node = node.parentElement;
    }
    return false;
  };
  const sectionHeadings = (args.sectionHeadings || []).map(norm);
  const scorePatterns = (args.scorePatterns || []).map(norm);
  const confidenceTargets = (args.confidenceTargets || []).map(norm);
  const decisionLabels = (args.decisionLabels || []).map(norm);

  const leafMost = (elements) =>
    elements.filter(
      (el) => !elements.some((o) => o !== el && el.contains(o))
    );
  const isSectionHeading = (el, headingNorm) => {
    if (!isVisible(el) || norm(el.innerText) !== headingNorm) return false;
    const tag = el.tagName ? el.tagName.toLowerCase() : '';
    const role = el.getAttribute('role') || '';
    if (/^h[1-6]$/.test(tag) || role === 'heading') return true;
    const text = norm(el.innerText);
    return text.length <= headingNorm.length + 8;
  };
  const scoreCellPatterns = (args.scoreCellPatterns || []).map(norm);
  // Match _JS_VERIFY_BAND_SCORES: score cells often render digit and label as
  // separate child elements, so full-cell patterns alone miss the scope row.
  const matchesScoreText = (text) =>
    scorePatterns.some((p) => text === p || text.startsWith(p));
  const normalizeConfidenceText = (text) => {
    const t = norm(text);
    if (t === 'med' || t === 'medium') return 'medium';
    return t;
  };
  const closestClickable = (el) => {
    let node = el;
    while (node && node !== document.body) {
      if (isInteractive(node)) return node;
      node = node.parentElement;
    }
    return el;
  };
  const styleSignature = (el) => {
    const s = window.getComputedStyle(el);
    return [s.backgroundColor, s.borderTopColor, s.boxShadow].join('|');
  };
  // Among sibling toggle cells, the one styled unlike all the others is the
  // selected one (framework-agnostic; no reliance on Tailwind class names).
  const styleOutlier = (cells) => {
    if (cells.length < 2) return null;
    const groups = new Map();
    for (const cell of cells) {
      const sig = styleSignature(cell);
      if (!groups.has(sig)) groups.set(sig, []);
      groups.get(sig).push(cell);
    }
    if (groups.size < 2) return null;
    let biggest = 0;
    const singles = [];
    for (const members of groups.values()) {
      biggest = Math.max(biggest, members.length);
      if (members.length === 1) singles.push(members[0]);
    }
    return biggest >= 2 && singles.length === 1 ? singles[0] : null;
  };
  const parseScoreFromText = (text) => {
    const pipeMatch = text.match(/^([0-3])\\s*\\|/);
    if (pipeMatch) return parseInt(pipeMatch[1], 10);
    for (const p of scorePatterns) {
      if (text === p || text.startsWith(p)) {
        const numMatch = p.match(/^([0-3])/);
        if (numMatch) return parseInt(numMatch[1], 10);
      }
    }
    const leading = text.match(/^([0-3])\\b/);
    if (leading) return parseInt(leading[1], 10);
    return null;
  };
  const findBandScope = (headingNorm) => {
    const labels = leafMost(
      Array.from(document.body.querySelectorAll('*'))
        .filter((el) => isVisible(el) && norm(el.innerText) === headingNorm)
    );
    if (!labels.length) return null;
    const scopeFromLabel = (label) => {
      let node = label.parentElement;
      while (node && node !== document.body) {
        const scoreLeaves = leafMost(
          Array.from(node.querySelectorAll('*'))
            .filter((el) =>
              isVisible(el) && matchesScoreText(norm(el.innerText))
            )
        );
        const scoreClickables = [];
        for (const el of scoreLeaves) {
          const cell = closestClickable(el);
          if (!scoreClickables.includes(cell)) scoreClickables.push(cell);
        }
        if (scoreClickables.length >= 4) {
          let scope = node;
          const parent = node.parentElement;
          if (parent && parent !== document.body) {
            const parentScoreLeaves = leafMost(
              Array.from(parent.querySelectorAll('*'))
                .filter((el) =>
                  isVisible(el) && matchesScoreText(norm(el.innerText))
                )
            );
            const parentScoreClickables = [];
            for (const el of parentScoreLeaves) {
              const cell = closestClickable(el);
              if (!parentScoreClickables.includes(cell)) {
                parentScoreClickables.push(cell);
              }
            }
            const confInParent = leafMost(
              Array.from(parent.querySelectorAll('*'))
                .filter((el) => {
                  if (!isVisible(el)) return false;
                  const t = norm(el.innerText);
                  return confidenceTargets.includes(t) || t === 'medium';
                })
            );
            if (parentScoreClickables.length >= 4 && confInParent.length >= 3) {
              scope = parent;
            }
          }
          return scope;
        }
        node = node.parentElement;
      }
      return null;
    };
    for (const label of labels) {
      const scope = scopeFromLabel(label);
      if (scope) return scope;
    }
    return null;
  };
  const findBandReasonLen = (heading) => {
    const headingNorm = norm(heading);
    const labels = leafMost(
      Array.from(document.body.querySelectorAll('*'))
        .filter((el) => isVisible(el) && norm(el.innerText) === headingNorm)
    );
    if (!labels.length) return 0;
    const label = labels[0];
    const allSections = leafMost(
      Array.from(document.body.querySelectorAll('*'))
        .filter((el) =>
          sectionHeadings.some((h) => isSectionHeading(el, h))
        )
    ).sort((a, b) =>
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
    const textInputs = Array.from(
      document.body.querySelectorAll(
        'textarea, [contenteditable="true"], [role="textbox"]'
      )
    ).filter((el) => {
      if (!isVisible(el) || !inBandRange(el)) return false;
      const ph = el.placeholder || el.getAttribute('placeholder') || '';
      const ariaLabel = el.getAttribute('aria-label') || '';
      if (/outside the rubric|sent to the author|internal note/i.test(ph) ||
          /outside the rubric|sent to the author|internal note/i.test(ariaLabel)) {
        return false;
      }
      return true;
    });
    let reasonField = textInputs.find(isReasonField);
    if (!reasonField) {
      reasonField = textInputs.find((el) => {
        let node = el.parentElement;
        for (let d = 0; d < 6 && node; d++) {
          const t = norm(node.innerText || node.textContent || '');
          if (t.includes('reason required')) return true;
          node = node.parentElement;
        }
        return false;
      });
    }
    if (!reasonField && textInputs.length === 1) reasonField = textInputs[0];
    return reasonField ? readField(reasonField).length : 0;
  };

  const bandState = (heading) => {
    const headingNorm = norm(heading);
    const scope = findBandScope(headingNorm);
    if (!scope) {
      const hasLabel = Array.from(document.body.querySelectorAll('*'))
        .some((el) => isVisible(el) && norm(el.innerText) === headingNorm);
      return {
        heading,
        found: hasLabel,
        score: null,
        confidence: null,
        reasonLen: 0,
      };
    }

    let score = null;
    const scoreNodes = leafMost(
      Array.from(scope.querySelectorAll('*'))
        .filter((el) =>
          isVisible(el) && matchesScoreText(norm(el.innerText))
        )
    );
    for (const el of scoreNodes) {
      const scoreVal = parseScoreFromText(norm(el.innerText));
      if (scoreVal !== null && isNodeSelected(el)) {
        score = scoreVal;
        break;
      }
    }
    if (score === null) {
      const scoreCells = [];
      for (const el of scoreNodes) {
        const cell = closestClickable(el);
        if (!scoreCells.includes(cell)) scoreCells.push(cell);
      }
      const outlier = styleOutlier(scoreCells);
      if (outlier) score = parseScoreFromText(norm(outlier.innerText));
    }

    let confidence = null;
    const confNodes = leafMost(
      Array.from(scope.querySelectorAll('*'))
        .filter((el) => {
          if (!isVisible(el)) return false;
          const t = norm(el.innerText);
          return confidenceTargets.includes(t) || t === 'medium';
        })
    );
    for (const el of confNodes) {
      const text = norm(el.innerText);
      if (isNodeSelected(el)) {
        confidence = normalizeConfidenceText(text);
        break;
      }
    }
    if (confidence === null) {
      const confCells = [];
      for (const el of confNodes) {
        const cell = closestClickable(el);
        if (!confCells.includes(cell)) confCells.push(cell);
      }
      const outlier = styleOutlier(confCells);
      if (outlier) {
        confidence = normalizeConfidenceText(norm(outlier.innerText));
      }
    }

    const reasonFields = Array.from(
      scope.querySelectorAll(
        'textarea, [contenteditable="true"], [role="textbox"]'
      )
    ).filter((el) => {
      if (!isVisible(el)) return false;
      const ph = el.placeholder || el.getAttribute('placeholder') || '';
      const ariaLabel = el.getAttribute('aria-label') || '';
      if (/outside the rubric|sent to the author|internal note/i.test(ph) ||
          /outside the rubric|sent to the author|internal note/i.test(ariaLabel)) {
        return false;
      }
      return true;
    });
    let reasonLen = 0;
    const reasonField = reasonFields.find(isReasonField) ||
      (reasonFields.length === 1 ? reasonFields[0] : null);
    if (reasonField) reasonLen = readField(reasonField).length;
    if (reasonLen < 5) {
      reasonLen = findBandReasonLen(heading);
    }

    return { heading, found: true, score, confidence, reasonLen };
  };

  const bands = (args.headings || []).map(bandState);
  let decision = null;
  for (const label of decisionLabels) {
    const nodes = Array.from(document.body.querySelectorAll('*'))
      .filter((el) => isVisible(el) && norm(el.innerText).startsWith(label));
    if (nodes.some(isNodeSelected)) {
      decision = label;
      break;
    }
  }

  let authorNoteLen = 0;
  for (const hint of args.authorNoteHints || []) {
    const hintNorm = norm(hint);
    const labels = Array.from(document.body.querySelectorAll('*'))
      .filter((el) => isVisible(el) && norm(el.innerText).startsWith(hintNorm));
    for (const label of labels) {
      let node = label.parentElement;
      while (node && node !== document.body) {
        const field = node.querySelector('textarea');
        if (field && isVisible(field)) {
          authorNoteLen = readField(field).length;
          break;
        }
        node = node.parentElement;
      }
      if (authorNoteLen) break;
    }
    if (authorNoteLen) break;
  }

  let submitHint = '';
  const submitButtons = Array.from(document.body.querySelectorAll('button'))
    .filter((el) => isVisible(el) &&
      /^submit(\\s+review)?$/i.test(norm(el.innerText)));
  // The form opener ("Submit Review") matches this pattern too and sits
  // above the form; the real in-form Submit renders last in the DOM.
  const submitButton = submitButtons.length
    ? submitButtons[submitButtons.length - 1]
    : null;
  if (submitButton) {
    let node = submitButton.parentElement;
    for (let depth = 0; depth < 4 && node; depth++) {
      const text = (node.innerText || '').trim();
      if (/before submitting|add a reason|score all three|confidence|author note/i.test(text)) {
        const lines = text.split('\\n').map((l) => l.trim()).filter(Boolean);
        submitHint = lines.find((l) =>
          /before submitting|add a reason|score all three|confidence|author note/i.test(l)
        ) || '';
        if (submitHint) break;
      }
      node = node.parentElement;
    }
  }

  const submitDisabled = submitButton ? !!submitButton.disabled : true;

  return { bands, decision, authorNoteLen, submitHint, submitDisabled };
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


def _score_cell_patterns() -> list[str]:
    """Full-cell text variants only (digit + label). Score cells render the
    number and label as separate child elements, so bare "2" / "Minor"
    fragments must not count as cells when locating a band's score row."""
    patterns: list[str] = []
    for score, label in SCORE_LABELS.items():
        patterns.extend(
            [f"{score} | {label}", f"{score}|{label}", f"{score} {label}"]
        )
    return patterns


def _band_section_headings() -> list[str]:
    return list(BAND_HEADINGS.values()) + ["Other notes"]


def _form_state_eval_args() -> dict[str, Any]:
    return {
        "headings": list(BAND_HEADINGS.values()),
        "sectionHeadings": _band_section_headings(),
        "scorePatterns": _score_patterns(),
        "scoreCellPatterns": _score_cell_patterns(),
        "confidenceTargets": list(CONFIDENCE_UI.values()),
        "decisionLabels": list(DECISION_LABELS.values()),
        "authorNoteHints": list(AUTHOR_NOTE_LABELS),
    }


def _read_form_state(page: Page) -> dict[str, Any]:
    return page.evaluate(_JS_FORM_VALIDATION_STATE, _form_state_eval_args())


def _form_validation_issues(
    state: dict[str, Any],
    band_ratings: dict[str, Any],
    review: dict[str, Any],
) -> list[str]:
    """Return human-readable reasons the Shipd form is incomplete."""
    issues: list[str] = []
    decision = state.get("decision")
    expected_decision = DECISION_LABELS.get(_normalize_decision(str(review["decision"])))
    if not decision:
        issues.append(f"decision not selected (expected {expected_decision!r})")

    bands_by_heading = {b["heading"]: b for b in state.get("bands") or []}
    for band_key, heading in BAND_HEADINGS.items():
        band = band_ratings.get(band_key) or {}
        expected_score = int(band.get("score", -1))
        expected_conf = _normalize_confidence(str(band.get("confidence", "")))
        snapshot = bands_by_heading.get(heading) or {}
        if not snapshot.get("found"):
            issues.append(f"{heading}: section not found")
            continue
        actual_score = snapshot.get("score")
        if actual_score is None:
            issues.append(f"{heading}: no score selected (expected {expected_score})")
        elif actual_score != expected_score:
            issues.append(
                f"{heading}: score {actual_score} selected "
                f"(expected {expected_score})"
            )
        actual_conf = snapshot.get("confidence")
        if actual_conf is None:
            issues.append(
                f"{heading}: confidence not selected (expected {expected_conf})"
            )
        elif actual_conf != expected_conf:
            issues.append(
                f"{heading}: confidence {actual_conf!r} selected "
                f"(expected {expected_conf})"
            )
        if expected_score < 3:
            reason_len = int(snapshot.get("reasonLen") or 0)
            if reason_len < 5:
                issues.append(
                    f"{heading}: reason missing or too short "
                    f"({reason_len} chars, score {expected_score} < 3)"
                )

    note = format_compact_author_note(review)
    if note and int(state.get("authorNoteLen") or 0) < 5:
        issues.append(
            f"author note missing or too short "
            f"({state.get('authorNoteLen', 0)} chars)"
        )
    return issues


def _format_validation_diagnostics(
    state: dict[str, Any],
    issues: list[str],
) -> str:
    parts = [f"issues: {'; '.join(issues)}"]
    hint = str(state.get("submitHint") or "").strip()
    if hint:
        parts.append(f"Shipd hint: {hint!r}")
    band_bits = []
    for band in state.get("bands") or []:
        band_bits.append(
            f"{band.get('heading')}: score={band.get('score')} "
            f"conf={band.get('confidence')} reasonLen={band.get('reasonLen')}"
        )
    if band_bits:
        parts.append("bands: " + " | ".join(band_bits))
    parts.append(f"decision={state.get('decision')!r}")
    parts.append(f"authorNoteLen={state.get('authorNoteLen')}")
    parts.append(f"submitDisabled={state.get('submitDisabled')}")
    return " — ".join(parts)


def _read_field_text(page: Page, field: Locator) -> str:
    handle = field.element_handle()
    if handle is None:
        return ""
    try:
        return str(page.evaluate(_JS_READ_FIELD_TEXT, handle) or "").strip()
    finally:
        handle.dispose()


def _write_field_text(page: Page, field: Locator, text: str) -> int:
    """Fill a textarea or contenteditable and return persisted character count."""
    field.scroll_into_view_if_needed(timeout=5_000)
    field.click(timeout=5_000)
    handle = field.element_handle()
    if handle is None:
        raise RuntimeError("submit: could not resolve field handle for fill.")
    try:
        result = page.evaluate(
            _JS_WRITE_FIELD_TEXT,
            {"el": handle, "text": text},
        )
    finally:
        handle.dispose()
    if not isinstance(result, dict):
        raise RuntimeError("submit: field write returned unexpected result.")
    if not result.get("ok"):
        field.press("ControlOrMeta+a")
        field.type(text, delay=10)
        page.wait_for_timeout(200)
        persisted = _read_field_text(page, field)
        if persisted.strip() != text.strip():
            raise RuntimeError(
                "submit: field text did not persist after fill "
                f"(expected {len(text)} chars, got {len(persisted)})."
            )
        return len(persisted)
    return int(result.get("len") or 0)


def _verify_band_filled(
    page: Page,
    heading: str,
    *,
    score: int,
    confidence: str,
    require_reason: bool,
    check_score: bool = True,
    check_confidence: bool = True,
    log: LogFn = _noop_log,
) -> None:
    deadline = time.monotonic() + 2.5
    last_issues: list[str] = []
    last_band: dict[str, Any] = {}
    while True:
        state = _read_form_state(page)
        band = next(
            (b for b in state.get("bands") or [] if b.get("heading") == heading),
            {},
        )
        last_band = band
        issues: list[str] = []
        if not band.get("found"):
            issues.append("section not found")
        if check_score:
            if band.get("score") is None:
                issues.append("no score selected")
            elif int(band["score"]) != score:
                issues.append(f"score {band['score']} selected (expected {score})")
        if check_confidence:
            expected_conf = _normalize_confidence(confidence)
            if band.get("confidence") is None:
                issues.append("confidence not selected")
            elif band.get("confidence") != expected_conf:
                issues.append(
                    f"confidence {band.get('confidence')!r} (expected {expected_conf})"
                )
        if require_reason and int(band.get("reasonLen") or 0) < 5:
            issues.append(f"reason too short ({band.get('reasonLen', 0)} chars)")
        if not issues:
            if check_score or check_confidence or require_reason:
                log(
                    f"submit: {heading} verified "
                    f"(score={band.get('score')}, conf={band.get('confidence')}, "
                    f"reasonLen={band.get('reasonLen', 0)})"
                )
            return
        last_issues = issues
        if time.monotonic() >= deadline:
            break
        page.wait_for_timeout(250)

    raise RuntimeError(
        f"submit: {heading} not registered on form — "
        + "; ".join(last_issues)
    )


def _validate_band_ratings(
    page: Page,
    band_ratings: dict[str, Any],
    *,
    log: LogFn = _noop_log,
) -> list[str]:
    state = _read_form_state(page)
    issues: list[str] = []
    bands_by_heading = {b["heading"]: b for b in state.get("bands") or []}
    for band_key, heading in BAND_HEADINGS.items():
        band = band_ratings.get(band_key) or {}
        expected_score = int(band.get("score", -1))
        expected_conf = _normalize_confidence(str(band.get("confidence", "")))
        snapshot = bands_by_heading.get(heading) or {}
        if not snapshot.get("found"):
            issues.append(f"{heading}: section not found")
            continue
        actual_score = snapshot.get("score")
        if actual_score is None:
            issues.append(f"{heading}: no score selected (expected {expected_score})")
        elif actual_score != expected_score:
            issues.append(
                f"{heading}: score {actual_score} selected "
                f"(expected {expected_score})"
            )
        actual_conf = snapshot.get("confidence")
        if actual_conf is None:
            issues.append(
                f"{heading}: confidence not selected (expected {expected_conf})"
            )
        elif actual_conf != expected_conf:
            issues.append(
                f"{heading}: confidence {actual_conf!r} selected "
                f"(expected {expected_conf})"
            )
        if expected_score < 3:
            reason_len = int(snapshot.get("reasonLen") or 0)
            if reason_len < 5:
                issues.append(
                    f"{heading}: reason missing or too short "
                    f"({reason_len} chars, score {expected_score} < 3)"
                )
    if issues:
        log(
            "submit: band validation failed — "
            + _format_validation_diagnostics(state, issues)
        )
    else:
        log("submit: all band ratings verified on form")
    return issues


def _validate_submit_form(
    page: Page,
    band_ratings: dict[str, Any],
    review: dict[str, Any],
    *,
    log: LogFn = _noop_log,
) -> list[str]:
    state = _read_form_state(page)
    issues = _form_validation_issues(state, band_ratings, review)
    if issues:
        log(f"submit: form validation failed — {_format_validation_diagnostics(state, issues)}")
    else:
        log("submit: form validation passed")
    return issues


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


def _click_band_score_playwright(page: Page, heading: str, score: int) -> bool:
    if score not in SCORE_LABELS:
        raise ValueError(f"Band score must be 0-3, got {score!r}")

    idx = _band_index(heading)
    label = SCORE_LABELS[score]
    candidates = (
        f"{score} | {label}",
        f"{score} {label}",
        str(score),
        label,
        f"{score}\n{label}",
        f"{score}\n| {label}",
    )
    for name in candidates:
        button = page.get_by_role("button", name=name)
        if button.count() > idx and button.nth(idx).is_visible():
            button.nth(idx).scroll_into_view_if_needed()
            button.nth(idx).click()
            return True

    pattern = re.compile(rf"^{score}\b")
    button = page.get_by_role("button", name=pattern)
    if button.count() > idx and button.nth(idx).is_visible():
        button.nth(idx).scroll_into_view_if_needed()
        button.nth(idx).click()
        return True
    return False


def _band_index(heading: str) -> int:
    headings = list(BAND_HEADINGS.values())
    try:
        return headings.index(heading)
    except ValueError as exc:
        raise ValueError(f"Unknown band heading: {heading!r}") from exc


def _click_band_confidence_playwright(page: Page, heading: str, confidence: str) -> bool:
    """Click confidence within the band card anchored by heading."""
    ui_label = CONFIDENCE_UI[_normalize_confidence(confidence)]
    idx = _band_index(heading)
    buttons = page.get_by_role("button", name=ui_label, exact=True)
    if buttons.count() <= idx:
        buttons = page.get_by_role(
            "button",
            name=re.compile(rf"^{re.escape(ui_label)}$", re.I),
        )
    if buttons.count() <= idx:
        return False
    button = buttons.nth(idx)
    if not button.is_visible():
        return False
    button.scroll_into_view_if_needed()
    button.click()
    page.wait_for_timeout(250)
    cls = button.get_attribute("class") or ""
    return "bg-primary" in cls or "text-primary-foreground" in cls


def capture_failure(page: Page, label: str, *, log: LogFn = _noop_log) -> None:
    """Save a full-page screenshot + control dump for a failed submit step."""
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shot = DEBUG_DIR / f"{stamp}-{label}.png"
        page.screenshot(path=str(shot), full_page=True)
        dump: dict[str, Any] = {
            "label": label,
            "url": page.url,
            "buttons": [
                b.inner_text(timeout=500).strip().replace("\n", " | ")
                for b in page.get_by_role("button").all()[:60]
            ],
            "band_textareas": _collect_band_textarea_dump(page),
        }
        try:
            dump["formState"] = _read_form_state(page)
        except Exception as exc:
            dump["formStateError"] = str(exc)
        (DEBUG_DIR / f"{stamp}-{label}.json").write_text(
            json.dumps(dump, indent=2), encoding="utf-8"
        )
        log(f"submit: failure snapshot saved to {shot}")
    except Exception as exc:  # snapshot must never mask the original error
        log(f"submit: WARNING could not capture failure snapshot: {exc}")


def _collect_band_textarea_dump(page: Page) -> dict[str, Any]:
    """DOM summary of text inputs near each band section (for debug snapshots)."""
    try:
        return page.evaluate(
            """
            (headings) => {
              const norm = (t) => (t || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const isVisible = (el) => {
                const rect = el.getBoundingClientRect();
                if (rect.width < 1 || rect.height < 1) return false;
                const style = window.getComputedStyle(el);
                return style.visibility !== 'hidden' && style.display !== 'none';
              };
              const out = {};
              for (const heading of headings) {
                const headingNorm = norm(heading);
                const labels = Array.from(document.body.querySelectorAll('*'))
                  .filter((el) => isVisible(el) && norm(el.innerText) === headingNorm)
                  .filter((el) =>
                    !Array.from(document.body.querySelectorAll('*'))
                      .some((o) => o !== el && el.contains(o) && isVisible(o) &&
                        norm(o.innerText) === headingNorm)
                  );
                if (!labels.length) {
                  out[heading] = { headingFound: false, fields: [] };
                  continue;
                }
                const label = labels[0];
                const fields = Array.from(
                  document.body.querySelectorAll(
                    'textarea, [contenteditable="true"], [role="textbox"]'
                  )
                )
                  .filter((el) => isVisible(el) &&
                    (label.compareDocumentPosition(el) &
                      Node.DOCUMENT_POSITION_FOLLOWING))
                  .slice(0, 6)
                  .map((el) => ({
                    tag: el.tagName,
                    placeholder: el.placeholder ||
                      el.getAttribute('placeholder') || '',
                    ariaLabel: el.getAttribute('aria-label') || '',
                    role: el.getAttribute('role') || '',
                    valueLen: (el.value || el.innerText || '').length,
                  }));
                out[heading] = { headingFound: true, fields };
              }
              return out;
            }
            """,
            list(BAND_HEADINGS.values()),
        )
    except Exception as exc:
        return {"error": str(exc)}


def _find_and_mark(
    page: Page,
    targets: list[str],
    *,
    scope_heading: str | None = None,
    match: str = "exact",
    check_visual: bool = False,
    require_interactive: bool = False,
    require_button: bool = False,
) -> dict:
    return page.evaluate(
        _JS_FIND_AND_MARK,
        {
            "targets": targets,
            "scopeHeading": scope_heading,
            "match": match,
            "checkVisual": check_visual,
            "requireInteractive": require_interactive,
            "requireButton": require_button,
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
    require_button: bool = False,
) -> dict:
    """Locate by text via JS, then click with trusted Playwright events."""
    result = _find_and_mark(
        page,
        targets,
        scope_heading=scope_heading,
        match=match,
        require_button=require_button,
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

    Skips the click when aria/data-state or visible styling already marks the
    target selected (score cells behave like toggles — a second click can
    deselect). Retries until the selected state is observed or attempts are
    exhausted.
    """
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


def _find_band_reason_js(page: Page, heading: str) -> dict[str, Any]:
    return page.evaluate(
        _JS_FIND_BAND_REASON,
        {
            "heading": heading,
            "sectionHeadings": _band_section_headings(),
            "scorePatterns": _score_patterns(),
            "confidenceTargets": list(CONFIDENCE_UI.values()),
        },
    )


def _wait_band_reason_field(
    page: Page,
    heading: str,
    *,
    timeout_ms: int = 4_000,
) -> Locator | None:
    """Wait for the per-band reason input after scoring below 3."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        field = _find_band_reason_playwright(page, heading)
        if field is not None:
            return field
        try:
            section = _band_section(page, heading)
        except (RuntimeError, PlaywrightTimeoutError):
            page.wait_for_timeout(200)
            continue
        field = section.get_by_placeholder(REASON_FIELD_PATTERN)
        if field.count():
            try:
                if field.first.is_visible():
                    return field.first
            except PlaywrightTimeoutError:
                pass
        reason_label = section.get_by_text(REASON_FIELD_PATTERN)
        if reason_label.count():
            container = reason_label.first.locator("xpath=ancestor::*[position()<=5]")
            for i in range(container.count()):
                block = container.nth(i)
                for selector in (
                    "textarea",
                    "[contenteditable='true']",
                    "[role='textbox']",
                    "input[type='text']",
                ):
                    candidate = block.locator(selector)
                    if candidate.count():
                        try:
                            if candidate.first.is_visible():
                                return candidate.first
                        except PlaywrightTimeoutError:
                            continue
        page.wait_for_timeout(250)
    return None


def _reason_field_attrs(field: Locator) -> tuple[str, str, str]:
    placeholder = field.get_attribute("placeholder") or ""
    aria_label = field.get_attribute("aria-label") or ""
    name = field.get_attribute("name") or ""
    return placeholder, aria_label, name


def _is_other_notes_field(field: Locator) -> bool:
    placeholder, aria_label, name = _reason_field_attrs(field)
    combined = " ".join((placeholder, aria_label, name))
    return bool(OTHER_NOTES_PATTERN.search(combined))


def _looks_like_reason_field(field: Locator) -> bool:
    if _is_other_notes_field(field):
        return False
    placeholder, aria_label, name = _reason_field_attrs(field)
    combined = " ".join((placeholder, aria_label, name))
    return bool(REASON_FIELD_PATTERN.search(combined))


def _field_near_reason_label(field: Locator) -> bool:
    """True when ancestor/sibling text marks this as the band reason input."""
    if _looks_like_reason_field(field):
        return True
    try:
        return bool(
            field.evaluate(
                """(el) => {
                  const reasonPattern =
                    /below\\s*3|what kept it|one line|reason required|reason|explain why|required when|not clean|score.*below/i;
                  let node = el.parentElement;
                  for (let d = 0; d < 6 && node; d++) {
                    const text = (node.innerText || node.textContent || '')
                      .toLowerCase();
                    if (text.length > 0 && text.length < 500 &&
                        (text.includes('reason required') ||
                         reasonPattern.test(text))) {
                      return true;
                    }
                    node = node.parentElement;
                  }
                  return false;
                }"""
            )
        )
    except PlaywrightTimeoutError:
        return False


def _pick_band_reason_candidate(candidates: list[Locator]) -> Locator | None:
    """Choose the band reason field from visible inputs in a band section."""
    non_other = [c for c in candidates if not _is_other_notes_field(c)]
    for candidate in non_other:
        if _field_near_reason_label(candidate):
            return candidate
    if len(non_other) == 1:
        return non_other[0]
    textareas = [
        c
        for c in non_other
        if (c.evaluate("el => (el.tagName || '').toLowerCase()") or "")
        == "textarea"
    ]
    if len(textareas) == 1:
        return textareas[0]
    if len(textareas) > 1:
        return textareas[-1]
    return None


def _find_band_reason_playwright(page: Page, heading: str) -> Locator | None:
    """Playwright fallback when JS band-reason discovery fails."""
    try:
        section = _band_section(page, heading)
    except (RuntimeError, PlaywrightTimeoutError):
        return None

    selectors = (
        "textarea",
        "[contenteditable='true']",
        "[role='textbox']",
        "input[type='text']",
    )
    candidates: list[Locator] = []
    for selector in selectors:
        locator = section.locator(selector)
        for i in range(locator.count()):
            candidate = locator.nth(i)
            try:
                if candidate.is_visible():
                    candidates.append(candidate)
            except PlaywrightTimeoutError:
                continue

    return _pick_band_reason_candidate(candidates)


def _band_form_snapshot(
    page: Page, heading: str
) -> dict[str, Any]:
    state = _read_form_state(page)
    return next(
        (b for b in state.get("bands") or [] if b.get("heading") == heading),
        {},
    )


def _click_band_score(
    page: Page,
    heading: str,
    score: int,
    *,
    log: LogFn = _noop_log,
) -> None:
    """Select a band score via Playwright locators, falling back to text match."""
    band = _band_form_snapshot(page, heading)
    if band.get("score") == score:
        log(f"submit: {heading} score {score} already selected — skipping")
        return
    try:
        if _click_band_score_playwright(page, heading, score):
            page.wait_for_timeout(350)
            band = _band_form_snapshot(page, heading)
            if band.get("score") == score:
                log(
                    f"submit: {heading} score {score} ({SCORE_LABELS[score]}) "
                    "via Playwright"
                )
                return
            log(
                f"submit: Playwright score click for {heading!r} did not "
                "register — using text-match fallback"
            )
    except (RuntimeError, PlaywrightTimeoutError, ValueError) as exc:
        log(
            f"submit: Playwright score click failed for {heading!r} "
            f"({exc}); using text-match fallback"
        )

    _click_selectable(
        page,
        _score_targets(score),
        scope_heading=heading,
        log=log,
        description=f"{heading} score {score} ({SCORE_LABELS[score]})",
        require_interactive=True,
    )


def _click_band_confidence(
    page: Page,
    heading: str,
    confidence: str,
    *,
    log: LogFn = _noop_log,
) -> None:
    """Select band confidence via Playwright locators, falling back to text match."""
    expected_conf = _normalize_confidence(confidence)
    ui_label = CONFIDENCE_UI[expected_conf]
    band = _band_form_snapshot(page, heading)
    if band.get("confidence") == expected_conf:
        log(f"submit: {heading} confidence {ui_label} already selected — skipping")
        return
    try:
        if _click_band_confidence_playwright(page, heading, str(confidence)):
            band = _band_form_snapshot(page, heading)
            if band.get("confidence") == _normalize_confidence(confidence):
                log(f"submit: {heading} confidence {ui_label} via Playwright")
                return
            log(
                f"submit: Playwright confidence click for {heading!r} did not "
                "register — using text-match fallback"
            )
    except (RuntimeError, PlaywrightTimeoutError, ValueError) as exc:
        log(
            f"submit: Playwright confidence click failed for {heading!r} "
            f"({exc}); using text-match fallback"
        )

    _click_selectable(
        page,
        [ui_label],
        scope_heading=heading,
        log=log,
        description=f"{heading} confidence {ui_label}",
        require_interactive=True,
    )


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

    _click_band_score(page, heading, score, log=log)
    page.wait_for_timeout(200)

    _click_band_confidence(page, heading, str(confidence), log=log)

    if score < 3:
        page.wait_for_timeout(600)
        for attempt in range(1, 5):
            info = _find_band_reason_js(page, heading)
            if info.get("ok"):
                _unmark(page)
                break
            if _wait_band_reason_field(page, heading, timeout_ms=1_500) is not None:
                break
            log(
                f"submit: {heading} reason field not visible after score {score} "
                f"(attempt {attempt}/4) — re-clicking score"
            )
            _click_band_score(page, heading, score, log=log)
            page.wait_for_timeout(800)
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
    """Fill the required reason field for a band scored below 3."""
    text = " ".join(str(reasoning).split()).strip()
    if not text:
        text = f"Scored {score}/3 — see author note for details."

    deadline = time.monotonic() + 12.0
    info: dict[str, Any] = {}
    field: Locator | None = None
    while time.monotonic() < deadline:
        info = _find_band_reason_js(page, heading)
        if info.get("ok"):
            candidate = page.locator(MARK_SELECTOR).first
            try:
                candidate.wait_for(state="visible", timeout=1_500)
                field = candidate
                break
            except PlaywrightTimeoutError:
                _unmark(page)
        else:
            field = _wait_band_reason_field(page, heading)
            if field is not None:
                info = {"ok": True, "via": "placeholder"}
                break
            field = _find_band_reason_playwright(page, heading)
            if field is not None:
                info = {"ok": True, "via": "playwright"}
                break
        page.wait_for_timeout(300)

    if field is None:
        candidates = info.get("candidates")
        detail = info.get("reason", "unknown")
        if candidates:
            detail = f"{detail}; candidates={json.dumps(candidates)[:400]}"
        raise RuntimeError(
            f"submit: {heading} scored {score} (<3) but its reason "
            f"textarea was not found: {detail}. "
            "Shipd will not enable Submit without it."
        )

    field.scroll_into_view_if_needed(timeout=5_000)
    field.click()
    field.fill(text)
    read_back = _read_field_text(page, field)
    _unmark(page)
    if len(read_back.strip()) < 5:
        raise RuntimeError(
            f"submit: reason for {heading} did not persist after fill "
            f"({len(read_back.strip())} chars)."
        )
    via = info.get("via") or ("playwright" if info.get("soleCandidate") else "js")
    log(
        f"submit: {heading} reason filled ({len(read_back.strip())} chars, "
        f"score {score} < 3, via {via})"
    )


def _confidence_visually_selected(
    page: Page,
    heading: str,
    confidence: str,
) -> bool:
    """True when the band confidence segment looks selected in the DOM."""
    ui_label = CONFIDENCE_UI[_normalize_confidence(confidence)]
    info = _find_and_mark(
        page,
        [ui_label],
        scope_heading=heading,
        check_visual=True,
        require_interactive=True,
    )
    selected = bool(info.get("selected"))
    _unmark(page)
    return selected


def _ensure_all_band_confidences(
    page: Page,
    band_ratings: dict[str, Any],
    *,
    log: LogFn = _noop_log,
) -> None:
    """Re-assert confidence only for bands the form shows as unset or wrong.

    Never blind-clicks: the segments are toggles, so re-clicking an
    already-correct confidence would deselect it.
    """
    state = _read_form_state(page)
    bands_by_heading = {b.get("heading"): b for b in state.get("bands") or []}
    for band_key, heading in BAND_HEADINGS.items():
        band = band_ratings.get(band_key) or {}
        confidence = band.get("confidence")
        if confidence is None:
            continue
        expected = _normalize_confidence(str(confidence))
        snapshot = bands_by_heading.get(heading) or {}
        if snapshot.get("confidence") == expected:
            continue
        if _confidence_visually_selected(page, heading, str(confidence)):
            log(
                f"submit: {heading} confidence {CONFIDENCE_UI[expected]} "
                "already selected visually — skipping"
            )
            continue
        _click_band_confidence(page, heading, str(confidence), log=log)


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

        info = _find_band_reason_js(page, heading)
        if not info.get("ok"):
            field = _wait_band_reason_field(page, heading, timeout_ms=2_000)
            if field is None:
                field = _find_band_reason_playwright(page, heading)
            if field is None:
                candidates = info.get("candidates")
                detail = info.get("reason", "unknown")
                if candidates:
                    detail = f"{detail}; candidates={json.dumps(candidates)[:400]}"
                raise RuntimeError(
                    f"submit: {heading} scored {score} (<3) but its reason "
                    f"textarea was not found: {detail}. "
                    "Shipd will not enable Submit without it."
                )
            existing = _read_field_text(page, field)
        else:
            field = page.locator(MARK_SELECTOR).first
            existing = _read_field_text(page, field)
            _unmark(page)
        if len(existing.strip()) >= 5:
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
        if not re.search(REASON_FIELD_PATTERN, placeholder):
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
                require_button=True,
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


def _submit_button(page: Page) -> Locator | None:
    """The in-form Submit button, or None when no visible match exists.

    The form opener ("Submit Review") matches SUBMIT_BUTTON_PATTERN too and
    can stay visible above the open form; the real in-form Submit renders
    after the form content, so prefer the last visible match over the first.
    """
    buttons = page.get_by_role("button", name=SUBMIT_BUTTON_PATTERN)
    candidate: Locator | None = None
    for i in range(buttons.count()):
        button = buttons.nth(i)
        try:
            if button.is_visible():
                candidate = button
        except PlaywrightTimeoutError:
            continue
    return candidate


def _submit_form_open(page: Page) -> bool:
    """True while the submit-review form (decision cards) is on the page."""
    return any(
        page.get_by_text(marker, exact=True).count()
        for marker in SUBMIT_FORM_MARKERS
    )


def _confirm_submit_dialog(page: Page, *, log: LogFn = _noop_log) -> bool:
    """Click the confirm button if Shipd raised a dialog after Submit."""
    dialogs = page.locator("[role='dialog'], [role='alertdialog']")
    for i in range(dialogs.count()):
        dialog = dialogs.nth(i)
        try:
            if not dialog.is_visible():
                continue
            confirm = dialog.get_by_role(
                "button", name=re.compile(r"^(confirm|submit|yes)", re.I)
            )
            if (
                confirm.count()
                and confirm.first.is_visible()
                and confirm.first.is_enabled()
            ):
                confirm.first.click()
                log("submit: confirmed submission dialog")
                return True
        except PlaywrightTimeoutError:
            continue
    return False


def _on_review_page(page: Page) -> bool:
    return "/challenges/" in page.url


def _wait_submit_enabled(
    page: Page,
    *,
    timeout_sec: float = 15.0,
    band_ratings: dict[str, Any] | None = None,
    review: dict[str, Any] | None = None,
    log: LogFn = _noop_log,
) -> bool:
    """Wait for the final Submit Review button to become enabled."""
    deadline = time.monotonic() + timeout_sec
    last_diag = ""
    while time.monotonic() < deadline:
        if not _on_review_page(page):
            log("submit: left review page while waiting for Submit button")
            return False
        if not _submit_form_open(page):
            # Only the opener can match now — don't treat it as the form's
            # Submit button.
            log("submit: review form closed while waiting for Submit button")
            return False
        button = _submit_button(page)
        if button is not None and button.is_enabled():
            return True
        if band_ratings is not None and review is not None:
            state = _read_form_state(page)
            issues = _form_validation_issues(state, band_ratings, review)
            last_diag = _format_validation_diagnostics(state, issues)
        else:
            state = _read_form_state(page)
            hint = str(state.get("submitHint") or "").strip()
            last_diag = f"submitDisabled={state.get('submitDisabled')}"
            if hint:
                last_diag += f" — Shipd hint: {hint!r}"
        page.wait_for_timeout(300)
    log(f"submit: Submit Review still disabled after {timeout_sec:.0f}s — {last_diag}")
    return False


def _wait_submit_confirmation(
    page: Page,
    *,
    timeout_sec: float = 20.0,
    log: LogFn = _noop_log,
) -> str:
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
        # A confirm dialog keeps the form visible until acknowledged.
        _confirm_submit_dialog(page, log=log)
        # Form gone (decision cards unmounted) is the strongest signal the
        # submission went through — the opener button may still be visible.
        if not _submit_form_open(page):
            return "confirmed"
        button = _submit_button(page)
        if button is None:
            return "confirmed"
        try:
            if not button.is_enabled():
                return "confirmed"
        except PlaywrightTimeoutError:
            return "confirmed"
        page.wait_for_timeout(400)
    return "unconfirmed"


def _finalize_submission(
    page: Page,
    *,
    band_ratings: dict[str, Any] | None = None,
    review: dict[str, Any] | None = None,
    log: LogFn = _noop_log,
) -> bool:
    if not _wait_submit_enabled(
        page,
        band_ratings=band_ratings,
        review=review,
        log=log,
    ):
        state = _read_form_state(page)
        issues = (
            _form_validation_issues(state, band_ratings or {}, review or {})
            if band_ratings and review
            else ["Submit Review button disabled"]
        )
        raise RuntimeError(
            "Submit Review button never became enabled — "
            + _format_validation_diagnostics(state, issues)
            + ". Check the failure snapshot in logs/debug-submit."
        )

    button = _submit_button(page)
    if button is None:
        raise RuntimeError(
            "Submit Review button disappeared after form validation — "
            "check the failure snapshot in logs/debug-submit."
        )
    button.scroll_into_view_if_needed()
    try:
        button.click(timeout=10_000)
    except PlaywrightTimeoutError:
        # Sticky footers/toasts can intercept the pointer; force skips the
        # actionability wait but still sends trusted events.
        log("submit: click intercepted — retrying with force")
        button.click(timeout=5_000, force=True)
    log("submit: Submit Review clicked; waiting for confirmation")

    outcome = _wait_submit_confirmation(page, log=log)
    if outcome == "confirmed":
        log("submit: review submission confirmed")
        return True
    log(
        "submit: WARNING could not confirm submission "
        "(Submit button still enabled after 20s)"
    )
    capture_failure(page, "submit-unconfirmed", log=log)
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
    _ensure_all_band_confidences(page, band_ratings, log=log)

    if review.get("downgrade_to_mars") and quest == "olympus":
        _set_downgrade_to_mars(page, enabled=True, log=log)

    if _wait_submit_enabled(
        page,
        timeout_sec=15.0,
        band_ratings=band_ratings,
        review=review,
        log=log,
    ):
        log("submit: form ready — Submit Review button enabled")
        return

    state = _read_form_state(page)
    issues = _form_validation_issues(state, band_ratings, review)
    log(f"submit: form validation failed — {_format_validation_diagnostics(state, issues)}")
    raise RuntimeError(
        "Submit Review button never became enabled — "
        + _format_validation_diagnostics(state, issues)
        + ". Check the failure snapshot in logs/debug-submit."
    )


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
                enabled = _wait_submit_enabled(
                    page,
                    timeout_sec=8.0,
                    band_ratings=band_ratings,
                    review=review,
                    log=log,
                )
                log(
                    "submit: dry fill complete — Submit button "
                    f"{'enabled' if enabled else 'NOT enabled'}"
                )
                if not enabled:
                    capture_failure(page, "dry-fill-submit-disabled", log=log)
                return enabled

            if not _wait_submit_enabled(
                page,
                band_ratings=band_ratings,
                review=review,
                log=log,
            ):
                if attempt == 1 and not _on_review_page(page) and target_url:
                    log("submit: page bounced to the queue while waiting; retrying")
                    continue
                state = _read_form_state(page)
                issues = _form_validation_issues(state, band_ratings, review)
                raise RuntimeError(
                    "Submit Review button never became enabled — "
                    + _format_validation_diagnostics(state, issues)
                    + ". Check the failure snapshot in logs/debug-submit."
                )
            return _finalize_submission(
                page,
                band_ratings=band_ratings,
                review=review,
                log=log,
            )

        raise RuntimeError("submit: form fill did not survive page navigation.")
    except Exception:
        capture_failure(page, "submit-failed", log=log)
        raise
