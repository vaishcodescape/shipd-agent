# Tests for review completion detection.

from __future__ import annotations

import unittest

from review.result import (
    is_review_complete,
    mark_review_complete,
    mark_review_incomplete,
    review_failure_reason,
)


class ReviewCompleteTests(unittest.TestCase):
    def test_explicit_complete(self) -> None:
        review = mark_review_complete({"decision": "approve"})
        self.assertTrue(is_review_complete(review))

    def test_explicit_incomplete(self) -> None:
        review = mark_review_incomplete(
            {"decision": "request_changes"},
            error="Structured finalize failed: 400",
        )
        self.assertFalse(is_review_complete(review))
        self.assertIn("400", review_failure_reason(review))

    def test_legacy_fallback_summary(self) -> None:
        review = {
            "decision": "request_changes",
            "recommendation_summary": (
                "Review could not complete: Structured finalize failed: bad model"
            ),
        }
        self.assertFalse(is_review_complete(review))

    def test_real_review_without_flag(self) -> None:
        review = {
            "decision": "request_changes",
            "recommendation_summary": "Tests need stronger edge-case coverage.",
            "internal_notes": "",
        }
        self.assertTrue(is_review_complete(review))


if __name__ == "__main__":
    unittest.main()
