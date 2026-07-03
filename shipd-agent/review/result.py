# Review completion status — distinguish real reviews from fallbacks.

from __future__ import annotations

INCOMPLETE_SUMMARY_PREFIX = "Review could not complete:"
DRY_RUN_SUMMARY_PREFIX = "Dry run:"


def mark_review_complete(review: dict) -> dict:
    """Tag a successful LLM review (safe to submit and count as completed)."""
    out = dict(review)
    out["review_complete"] = True
    out.pop("review_error", None)
    return out


def mark_review_incomplete(review: dict, *, error: str) -> dict:
    """Tag a fallback / partial review (must not submit or count as completed)."""
    out = dict(review)
    out["review_complete"] = False
    out["review_error"] = error
    return out


def is_review_complete(review: dict | None) -> bool:
    """True only when the agent finished explore + structured finalize successfully."""
    if not review:
        return False

    explicit = review.get("review_complete")
    if explicit is False:
        return False
    if explicit is True:
        return True

    # Backward compatibility for bundles saved before review_complete existed.
    summary = str(review.get("recommendation_summary", "")).strip()
    if summary.startswith(INCOMPLETE_SUMMARY_PREFIX):
        return False
    if summary.startswith(DRY_RUN_SUMMARY_PREFIX):
        return False

    internal = str(review.get("internal_notes", ""))
    if "Structured finalize failed" in internal:
        return False

    return True


def review_failure_reason(review: dict | None) -> str:
    """Human-readable reason when is_review_complete is False."""
    if not review:
        return "No review result produced."

    err = str(review.get("review_error", "")).strip()
    if err:
        return err

    summary = str(review.get("recommendation_summary", "")).strip()
    if summary.startswith(INCOMPLETE_SUMMARY_PREFIX):
        return summary.removeprefix(INCOMPLETE_SUMMARY_PREFIX).strip()

    internal = str(review.get("internal_notes", "")).strip()
    if internal:
        return internal

    return "Review did not complete successfully."
