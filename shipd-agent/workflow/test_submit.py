# Unit tests for submit form helpers (no browser required).

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from workflow.submit import (
    _normalize_confidence,
    _normalize_decision,
    _score_targets,
    submit_review,
)


class NormalizeDecisionTests(unittest.TestCase):
    def test_canonical_values(self) -> None:
        self.assertEqual(_normalize_decision("approve"), "approve")
        self.assertEqual(_normalize_decision("request_changes"), "request_changes")
        self.assertEqual(_normalize_decision("reject"), "reject")

    def test_aliases_and_formatting(self) -> None:
        self.assertEqual(_normalize_decision("Request Changes"), "request_changes")
        self.assertEqual(_normalize_decision("request-changes"), "request_changes")
        self.assertEqual(_normalize_decision("changes_requested"), "request_changes")
        self.assertEqual(_normalize_decision("Approved"), "approve")
        self.assertEqual(_normalize_decision("REJECTED"), "reject")

    def test_unknown_decision_raises(self) -> None:
        with self.assertRaises(ValueError):
            _normalize_decision("maybe")


class NormalizeConfidenceTests(unittest.TestCase):
    def test_canonical_values(self) -> None:
        for value in ("low", "medium", "high"):
            self.assertEqual(_normalize_confidence(value), value)

    def test_med_alias(self) -> None:
        self.assertEqual(_normalize_confidence("med"), "medium")
        self.assertEqual(_normalize_confidence(" Med "), "medium")

    def test_unknown_confidence_raises(self) -> None:
        with self.assertRaises(ValueError):
            _normalize_confidence("certain")


class ScoreTargetsTests(unittest.TestCase):
    def test_most_specific_variant_first(self) -> None:
        self.assertEqual(
            _score_targets(0),
            ["0 | Failing", "0|Failing", "0 Failing", "0\n| Failing", "0\nFailing", "Failing", "0"],
        )
        self.assertEqual(
            _score_targets(3),
            ["3 | Clean", "3|Clean", "3 Clean", "3\n| Clean", "3\nClean", "Clean", "3"],
        )


class SubmitReviewValidationTests(unittest.TestCase):
    def test_requires_decision(self) -> None:
        with self.assertRaisesRegex(ValueError, "decision"):
            submit_review(MagicMock(), {"band_ratings": {}}, quest="olympus")

    def test_requires_band_ratings(self) -> None:
        with self.assertRaisesRegex(ValueError, "band_ratings"):
            submit_review(MagicMock(), {"decision": "approve"}, quest="olympus")


if __name__ == "__main__":
    unittest.main()
