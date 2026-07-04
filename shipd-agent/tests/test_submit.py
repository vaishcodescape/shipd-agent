# Unit tests for submit form helpers (no browser required).

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from workflow.submit import (
    REASON_FIELD_PATTERN,
    _band_section_headings,
    _ensure_all_band_confidences,
    _field_near_reason_label,
    _find_band_reason_js,
    _format_validation_diagnostics,
    _form_validation_issues,
    _looks_like_reason_field,
    _normalize_confidence,
    _normalize_decision,
    _pick_band_reason_candidate,
    _score_cell_patterns,
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


class BandSectionHeadingsTests(unittest.TestCase):
    def test_includes_other_notes_boundary(self) -> None:
        headings = _band_section_headings()
        self.assertIn("Problem Description", headings)
        self.assertIn("Tests", headings)
        self.assertIn("Solution & Code", headings)
        self.assertIn("Other notes", headings)


class ReasonFieldPatternTests(unittest.TestCase):
    def test_matches_common_placeholders(self) -> None:
        for sample in (
            "Explain why this scored below 3",
            "What kept it from a Clean score?",
            "Reason required when not clean",
            "One line — what kept it below 3",
            "Reason Required",
        ):
            self.assertRegex(sample, REASON_FIELD_PATTERN)

    def test_does_not_match_author_note(self) -> None:
        self.assertIsNone(
            REASON_FIELD_PATTERN.search("Feedback for the author")
        )


class FormValidationTests(unittest.TestCase):
    def _sample_review(self) -> dict:
        return {
            "decision": "request_changes",
            "band_ratings": {
                "problem": {"score": 2, "confidence": "medium", "reasoning": "x" * 10},
                "tests": {"score": 2, "confidence": "high", "reasoning": "y" * 10},
                "solution": {"score": 3, "confidence": "medium", "reasoning": ""},
            },
            "contributor_feedback": "Author note body here.",
        }

    def test_detects_missing_band_reason(self) -> None:
        state = {
            "decision": "Request Changes",
            "authorNoteLen": 20,
            "bands": [
                {
                    "heading": "Problem Description",
                    "found": True,
                    "score": 2,
                    "confidence": "medium",
                    "reasonLen": 0,
                },
                {
                    "heading": "Tests",
                    "found": True,
                    "score": 2,
                    "confidence": "high",
                    "reasonLen": 12,
                },
                {
                    "heading": "Solution & Code",
                    "found": True,
                    "score": 3,
                    "confidence": "medium",
                    "reasonLen": 0,
                },
            ],
        }
        review = self._sample_review()
        issues = _form_validation_issues(
            state, review["band_ratings"], review
        )
        self.assertTrue(
            any("Problem Description" in issue and "reason" in issue for issue in issues)
        )

    def test_passes_when_form_complete(self) -> None:
        state = {
            "decision": "Request Changes",
            "authorNoteLen": 20,
            "bands": [
                {
                    "heading": "Problem Description",
                    "found": True,
                    "score": 2,
                    "confidence": "medium",
                    "reasonLen": 12,
                },
                {
                    "heading": "Tests",
                    "found": True,
                    "score": 2,
                    "confidence": "high",
                    "reasonLen": 12,
                },
                {
                    "heading": "Solution & Code",
                    "found": True,
                    "score": 3,
                    "confidence": "medium",
                    "reasonLen": 0,
                },
            ],
        }
        review = self._sample_review()
        issues = _form_validation_issues(
            state, review["band_ratings"], review
        )
        self.assertEqual(issues, [])

    def test_diagnostics_include_shipd_hint(self) -> None:
        diag = _format_validation_diagnostics(
            {
                "submitHint": "Add a reason for Problem Description (scored below 3).",
                "bands": [],
                "decision": None,
                "authorNoteLen": 0,
                "submitDisabled": True,
            },
            ["Problem Description: reason missing or too short (0 chars, score 2 < 3)"],
        )
        self.assertIn("Shipd hint", diag)
        self.assertIn("Problem Description", diag)


class ScoreCellPatternsTests(unittest.TestCase):
    def test_only_full_cell_variants(self) -> None:
        patterns = _score_cell_patterns()
        self.assertIn("2 | Minor", patterns)
        self.assertIn("2 Minor", patterns)
        # Bare fragments must not count as cells: each cell renders its
        # number and label as separate child elements.
        self.assertNotIn("Minor", patterns)
        self.assertNotIn("2", patterns)


class EnsureAllBandConfidencesTests(unittest.TestCase):
    def _state(self, problem: str | None, tests: str | None, solution: str | None) -> dict:
        return {
            "bands": [
                {"heading": "Problem Description", "found": True, "confidence": problem},
                {"heading": "Tests", "found": True, "confidence": tests},
                {"heading": "Solution & Code", "found": True, "confidence": solution},
            ],
        }

    def _ratings(self) -> dict:
        return {
            "problem": {"score": 2, "confidence": "medium"},
            "tests": {"score": 1, "confidence": "high"},
            "solution": {"score": 2, "confidence": "low"},
        }

    def test_skips_bands_already_correct(self) -> None:
        page = MagicMock()
        page.evaluate.return_value = self._state("medium", "high", "low")
        with patch("workflow.submit._click_band_confidence") as click:
            _ensure_all_band_confidences(page, self._ratings(), log=lambda _: None)
        click.assert_not_called()

    def test_reclicks_only_mismatched_band(self) -> None:
        page = MagicMock()
        page.evaluate.return_value = self._state("medium", "medium", "low")
        with patch("workflow.submit._click_band_confidence") as click:
            _ensure_all_band_confidences(page, self._ratings(), log=lambda _: None)
        self.assertEqual(click.call_count, 1)
        self.assertEqual(click.call_args.args[1], "Tests")

    def test_reclicks_unset_band(self) -> None:
        page = MagicMock()
        page.evaluate.return_value = self._state(None, "high", "low")
        with patch("workflow.submit._click_band_confidence") as click:
            _ensure_all_band_confidences(page, self._ratings(), log=lambda _: None)
        self.assertEqual(click.call_count, 1)
        self.assertEqual(click.call_args.args[1], "Problem Description")

    def test_skips_when_reader_misses_but_visual_selected(self) -> None:
        page = MagicMock()
        page.evaluate.return_value = self._state(None, "high", "low")
        with patch(
            "workflow.submit._confidence_visually_selected",
            side_effect=lambda _page, heading, _conf: heading == "Problem Description",
        ):
            with patch("workflow.submit._click_band_confidence") as click:
                _ensure_all_band_confidences(page, self._ratings(), log=lambda _: None)
        click.assert_not_called()


class LooksLikeReasonFieldTests(unittest.TestCase):
    def test_placeholder_match(self) -> None:
        field = MagicMock()
        field.get_attribute.side_effect = lambda name: {
            "placeholder": "Explain why this scored below 3",
            "aria-label": "",
            "name": "",
        }.get(name, "")
        self.assertTrue(_looks_like_reason_field(field))

    def test_no_match(self) -> None:
        field = MagicMock()
        field.get_attribute.return_value = ""
        self.assertFalse(_looks_like_reason_field(field))


class FindBandReasonJsTests(unittest.TestCase):
    def test_passes_score_patterns_for_scope_detection(self) -> None:
        page = MagicMock()
        page.evaluate.return_value = {"ok": True, "via": "scope"}
        result = _find_band_reason_js(page, "Problem Description")
        self.assertTrue(result["ok"])
        args = page.evaluate.call_args.args[1]
        self.assertIn("scorePatterns", args)
        self.assertIn("2 | Minor", args["scorePatterns"])
        self.assertIn("confidenceTargets", args)


class PickBandReasonCandidateTests(unittest.TestCase):
    def test_prefers_field_with_reason_label(self) -> None:
        other = MagicMock()
        reason = MagicMock()
        with patch("workflow.submit._is_other_notes_field", return_value=False):
            with patch(
                "workflow.submit._field_near_reason_label",
                side_effect=lambda f: f is reason,
            ):
                picked = _pick_band_reason_candidate([other, reason])
        self.assertIs(picked, reason)

    def test_picks_last_textarea_when_ambiguous(self) -> None:
        input_box = MagicMock()
        ta1 = MagicMock()
        ta2 = MagicMock()
        ta1.evaluate.return_value = "textarea"
        ta2.evaluate.return_value = "textarea"
        input_box.evaluate.return_value = "input"
        with patch("workflow.submit._is_other_notes_field", return_value=False):
            with patch("workflow.submit._field_near_reason_label", return_value=False):
                picked = _pick_band_reason_candidate([input_box, ta1, ta2])
        self.assertIs(picked, ta2)

    def test_single_non_other_candidate(self) -> None:
        only = MagicMock()
        with patch("workflow.submit._is_other_notes_field", return_value=False):
            with patch("workflow.submit._field_near_reason_label", return_value=False):
                only.evaluate.return_value = "div"
                picked = _pick_band_reason_candidate([only])
        self.assertIs(picked, only)


class FieldNearReasonLabelTests(unittest.TestCase):
    def test_detects_reason_required_in_ancestor(self) -> None:
        field = MagicMock()
        field.get_attribute.return_value = ""
        field.evaluate.return_value = True
        self.assertTrue(_field_near_reason_label(field))

    def test_false_when_no_nearby_label(self) -> None:
        field = MagicMock()
        field.get_attribute.return_value = ""
        field.evaluate.return_value = False
        self.assertFalse(_field_near_reason_label(field))


if __name__ == "__main__":
    unittest.main()
