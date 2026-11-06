"""Conservative, read-only guard for an already completed review page."""

from __future__ import annotations

import re
from typing import Any


class CompletedReviewError(RuntimeError):
    """The displayed submission is already approved, rejected, or reviewed."""


# Exact status text only: never match the form's "Approve" button or prose
# such as "tests must pass before the submission can be approved".
_COMPLETED_STATUS = re.compile(
    r"^(?:(?:submission|review)\s*:?\s*)?(?:approved|rejected)"
    r"$|^(?:review completed|review submitted|already reviewed)$",
    re.I,
)


def ensure_review_not_completed(page: Any) -> None:
    """Stop on visible completion labels before cloning, reviewing or posting.

    This intentionally errs on the side of stopping if the page exposes an
    ambiguous completion badge. Absence of a badge is not proof of ownership
    or of an active reservation.
    """
    statuses = page.get_by_text(_COMPLETED_STATUS, exact=True)
    for index in range(statuses.count()):
        status = statuses.nth(index)
        if status.is_visible():
            raise CompletedReviewError(
                "Submission already completed "
                f"({status.inner_text().strip()}). Stopping before further actions; "
                "open an active reservation in Shipd before retrying."
            )
