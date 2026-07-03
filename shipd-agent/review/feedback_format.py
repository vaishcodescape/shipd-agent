# Compact author-note formatting for the Shipd review form.

from __future__ import annotations

import re
from typing import Any

BAND_LABELS: dict[str, str] = {
    "problem": "Problem",
    "tests": "Tests",
    "solution": "Solution",
}
BAND_ORDER = ("problem", "tests", "solution")

_MAX_LINE_LEN = 220
_DEDUP_OVERLAP = 0.55


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def _word_set(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _strip_bullet(line: str) -> str:
    line = re.sub(r"^[-*•]\s+", "", line)
    line = re.sub(r"^\d+[.)]\s+", "", line)
    return line.strip()


def _compact_line(line: str, *, max_len: int = _MAX_LINE_LEN) -> str:
    line = re.sub(r"\s+", " ", line.strip())
    if len(line) > max_len:
        return line[: max_len - 3].rstrip() + "..."
    return line


def _is_duplicate(candidate: str, existing: list[str]) -> bool:
    norm_c = _normalize_text(candidate)
    if not norm_c:
        return True

    words_c = _word_set(candidate)
    for line in existing:
        norm_e = _normalize_text(line)
        if norm_c in norm_e or norm_e in norm_c:
            return True
        words_e = _word_set(line)
        if not words_e or not words_c:
            continue
        overlap = len(words_c & words_e) / min(len(words_c), len(words_e))
        if overlap >= _DEDUP_OVERLAP:
            return True
    return False


def _band_prefix(label: str, score: int) -> str:
    return f"{label} ({score}/3):"


def _has_band_line(lines: list[str], label: str, score: int) -> bool:
    prefix = _band_prefix(label, score)
    return any(line.startswith(prefix) for line in lines)


def _parse_feedback_lines(feedback: str) -> list[str]:
    lines: list[str] = []
    for raw in feedback.splitlines():
        line = _strip_bullet(raw.strip())
        if not line:
            continue
        compact = _compact_line(line)
        if not _is_duplicate(compact, lines):
            lines.append(compact)

    if lines:
        return lines

    paragraph = _compact_line(feedback)
    if not paragraph:
        return []

    if len(paragraph) > _MAX_LINE_LEN and ". " in paragraph:
        for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
            sentence = _compact_line(sentence)
            if sentence and not _is_duplicate(sentence, lines):
                lines.append(sentence)
        return lines

    return [paragraph]


def format_compact_author_note(review: dict[str, Any]) -> str:
    """Build a minimal author note from feedback and band reasoning."""
    feedback = str(review.get("contributor_feedback", "")).strip()
    lines = _parse_feedback_lines(feedback)

    band_ratings = review.get("band_ratings", {})
    if not isinstance(band_ratings, dict):
        return "\n".join(lines).strip()

    for band_key in BAND_ORDER:
        band = band_ratings.get(band_key, {})
        if not isinstance(band, dict):
            continue

        score = band.get("score")
        reasoning = str(band.get("reasoning", "")).strip()
        if score is None or int(score) >= 3 or not reasoning:
            continue

        label = BAND_LABELS[band_key]
        score_int = int(score)
        if _has_band_line(lines, label, score_int):
            continue

        compact_reason = _compact_line(reasoning)
        if _is_duplicate(compact_reason, lines):
            continue

        lines.append(f"{_band_prefix(label, score_int)} {compact_reason}")

    return "\n".join(lines).strip()
