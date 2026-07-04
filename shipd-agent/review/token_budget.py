# Helpers for bounding prompt and tool payload size (token-oriented budgets).

from __future__ import annotations

# Rough chars-per-token for English-ish code/logs (good enough for logging caps).
CHARS_PER_TOKEN_ESTIMATE = 4


def estimate_tokens(text: str) -> int:
    """Approximate token count without calling a tokenizer."""
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN_ESTIMATE)


def truncate_text(
    text: str,
    max_chars: int,
    *,
    label: str = "text",
) -> str:
    """Keep head and tail when truncating long strings."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    marker = f"… [{label} truncated; {len(text):,} chars total] …"
    body_budget = max(max_chars - len(marker), 200)
    head = text[: body_budget // 2]
    tail = text[-(body_budget - len(head)) :]
    return head + marker + tail
