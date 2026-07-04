# Unit tests for submit form helpers (no browser required).

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from workflow.submit import (
    REASON_FIELD_PATTERN,
    SUBMIT_BUTTON_PATTERN,
    _band_section_headings,
    _click_in_form_submit,
    _ensure_all_band_confidences,
    _field_near_reason_label,
    _fill_band_sequential,
    _fill_submit_form,
    _find_band_reason_js,
    _format_validation_diagnostics,
    _form_validation_issues,
    _looks_like_reason_field,
    _mark_band_scope,
    _mark_in_form_submit,
    _normalize_confidence,
    _normalize_decision,
    _pick_band_reason_candidate,
    _repair_form_gaps,
    _score_cell_patterns,
    _score_targets,
    _submit_button_actually_enabled,
    _submit_button_diagnostics,
    _verify_decision_selected,
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


class VerifyDecisionSelectedTests(unittest.TestCase):
    """The form-state JS reports the selected decision normalized to lowercase
    (e.g. "approve" / "request changes"); verification must match it
    case-insensitively rather than against the mixed-case display label."""

    def test_accepts_normalized_lowercase_decision(self) -> None:
        page = MagicMock()
        page.evaluate.return_value = {"decision": "approve"}
        _verify_decision_selected(page, "approve", log=lambda _: None)

    def test_accepts_multiword_decision(self) -> None:
        page = MagicMock()
        page.evaluate.return_value = {"decision": "request changes"}
        _verify_decision_selected(page, "request_changes", log=lambda _: None)

    def test_raises_when_decision_absent(self) -> None:
        page = MagicMock()
        page.evaluate.return_value = {"decision": None}
        with patch("workflow.submit.time.monotonic", side_effect=[0.0, 100.0]):
            with self.assertRaisesRegex(RuntimeError, "not registered"):
                _verify_decision_selected(page, "approve", log=lambda _: None)

    def test_raises_on_wrong_decision(self) -> None:
        page = MagicMock()
        page.evaluate.return_value = {"decision": "reject"}
        with patch("workflow.submit.time.monotonic", side_effect=[0.0, 100.0]):
            with self.assertRaisesRegex(RuntimeError, "not registered"):
                _verify_decision_selected(page, "approve", log=lambda _: None)


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
    def test_passes_score_cell_patterns_for_scope_detection(self) -> None:
        page = MagicMock()
        page.evaluate.return_value = {"ok": True, "via": "scope"}
        result = _find_band_reason_js(page, "Problem Description")
        self.assertTrue(result["ok"])
        args = page.evaluate.call_args.args[1]
        self.assertIn("scoreCellPatterns", args)
        self.assertIn("2 | Minor", args["scoreCellPatterns"])
        self.assertIn("confidenceTargets", args)
        self.assertNotIn("scorePatterns", args)


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


class RepairFormGapsTests(unittest.TestCase):
    def _complete_state(self) -> dict:
        return {
            "decision": "Request Changes",
            "authorNoteLen": 20,
            "submitDisabled": False,
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

    def test_no_op_when_form_complete(self) -> None:
        page = MagicMock()
        with patch("workflow.submit._read_form_state", return_value=self._complete_state()):
            with patch("workflow.submit._click_band_score") as score:
                with patch("workflow.submit._click_band_confidence") as conf:
                    _repair_form_gaps(
                        page,
                        self._sample_review()["band_ratings"],
                        self._sample_review(),
                        log=lambda _: None,
                    )
        score.assert_not_called()
        conf.assert_not_called()

    def test_repairs_only_missing_reason(self) -> None:
        state = self._complete_state()
        state["bands"][0]["reasonLen"] = 0
        page = MagicMock()
        with patch("workflow.submit._read_form_state", side_effect=[state, self._complete_state()]):
            with patch("workflow.submit._fill_band_reason") as fill_reason:
                with patch("workflow.submit._click_band_score") as score:
                    _repair_form_gaps(
                        page,
                        self._sample_review()["band_ratings"],
                        self._sample_review(),
                        log=lambda _: None,
                    )
        fill_reason.assert_called_once()
        score.assert_not_called()


class SubmitButtonPatternTests(unittest.TestCase):
    def test_matches_review_and_feedback_labels(self) -> None:
        for label in ("Submit Review", "Submit review", "Submit feedback"):
            self.assertRegex(label, SUBMIT_BUTTON_PATTERN)


class SubmitButtonDiagnosticsTests(unittest.TestCase):
    def test_mark_in_form_submit_returns_locator_metadata(self) -> None:
        page = MagicMock()
        page.evaluate.return_value = {
            "ok": True,
            "text": "Submit Review",
            "disabled": False,
            "box": {"x": 10, "y": 20, "w": 100, "h": 32},
        }
        page.locator.return_value.first.bounding_box.return_value = {
            "x": 10,
            "y": 20,
            "width": 100,
            "height": 32,
        }
        page.locator.return_value.first.get_attribute.return_value = None
        page.locator.return_value.first.is_enabled.return_value = True
        diag = _submit_button_diagnostics(page)
        self.assertTrue(diag["found"])
        self.assertEqual(diag["text"], "Submit Review")
        self.assertFalse(diag["disabled"])

    def test_submit_button_diagnostics_when_missing(self) -> None:
        page = MagicMock()
        page.evaluate.return_value = {"ok": False, "reason": "no submit button found"}
        diag = _submit_button_diagnostics(page)
        self.assertFalse(diag["found"])

    def test_submit_button_actually_enabled_requires_shipd_not_disabled(self) -> None:
        page = MagicMock()
        with patch(
            "workflow.submit._submit_button_diagnostics",
            return_value={"found": True, "disabled": False, "text": "Submit Review"},
        ):
            with patch(
                "workflow.submit._read_form_state",
                return_value={"submitDisabled": True, "submitHint": "Confidence"},
            ):
                ready, diag = _submit_button_actually_enabled(page)
        self.assertFalse(ready)
        self.assertTrue(diag.get("submitDisabled"))


class ClickInFormSubmitTests(unittest.TestCase):
    def test_click_logs_strategy_and_confirms_dialog(self) -> None:
        page = MagicMock()
        logs: list[str] = []
        button = page.locator.return_value.first
        with patch(
            "workflow.submit._submit_button_diagnostics",
            return_value={
                "found": True,
                "text": "Submit Review",
                "disabled": False,
                "ariaDisabled": False,
                "box": {"x": 1, "y": 2},
            },
        ):
            with patch("workflow.submit._confirm_submit_dialog", return_value=True) as confirm:
                _click_in_form_submit(page, log=logs.append)
        button.click.assert_called_once()
        confirm.assert_called_once()
        self.assertTrue(any("playwright" in line for line in logs))


class BandScopeTests(unittest.TestCase):
    def test_mark_band_scope_passes_score_cell_patterns(self) -> None:
        page = MagicMock()
        page.evaluate.return_value = {"ok": True, "scoreCells": 4}
        result = _mark_band_scope(page, "Tests")
        self.assertTrue(result["ok"])
        args = page.evaluate.call_args.args[1]
        self.assertIn("scoreCellPatterns", args)
        self.assertIn("2 | Minor", args["scoreCellPatterns"])


class RepairSubmitDisabledTests(unittest.TestCase):
    def test_repair_reclicks_confidence_when_shipd_hint_says_confidence(self) -> None:
        state = {
            "decision": "Request Changes",
            "authorNoteLen": 20,
            "submitDisabled": True,
            "submitHint": "Confidence",
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
        review = {
            "decision": "request_changes",
            "band_ratings": {
                "problem": {"score": 2, "confidence": "medium", "reasoning": "x" * 10},
                "tests": {"score": 2, "confidence": "high", "reasoning": "y" * 10},
                "solution": {"score": 3, "confidence": "medium", "reasoning": ""},
            },
            "contributor_feedback": "Author note body here.",
        }
        page = MagicMock()
        cleared = {**state, "submitDisabled": False}
        with patch(
            "workflow.submit._read_form_state",
            side_effect=[state, cleared],
        ):
            with patch("workflow.submit._ensure_all_band_confidences") as ensure:
                with patch("workflow.submit._click_band_score") as score:
                    _repair_form_gaps(
                        page,
                        review["band_ratings"],
                        review,
                        log=lambda _: None,
                    )
        ensure.assert_called_once()
        score.assert_not_called()


class FillBandSequentialTests(unittest.TestCase):
    def test_score_confidence_reason_order_for_low_score(self) -> None:
        page = MagicMock()
        order: list[str] = []

        def log(msg: str) -> None:
            order.append(msg)

        with patch("workflow.submit._scroll_band_into_view"):
            with patch("workflow.submit._click_band_score") as score:
                with patch("workflow.submit._verify_band_filled") as verify:
                    with patch(
                        "workflow.submit._wait_and_verify_band_reason_field"
                    ) as wait_reason:
                        with patch("workflow.submit._click_band_confidence") as conf:
                            with patch("workflow.submit._fill_band_reason") as fill_reason:
                                with patch(
                                    "workflow.submit._band_form_snapshot",
                                    return_value={
                                        "score": 2,
                                        "confidence": "medium",
                                        "reasonLen": 12,
                                    },
                                ):
                                    _fill_band_sequential(
                                        page,
                                        "Problem Description",
                                        score=2,
                                        confidence="medium",
                                        reasoning="reason text",
                                        log=log,
                                    )

        score.assert_called_once()
        conf.assert_called_once()
        wait_reason.assert_called_once()
        fill_reason.assert_called_once()
        self.assertEqual(verify.call_count, 3)
        verify_calls = verify.call_args_list
        self.assertFalse(verify_calls[0].kwargs.get("check_confidence"))
        self.assertFalse(verify_calls[1].kwargs.get("check_score"))
        self.assertTrue(verify_calls[2].kwargs.get("require_reason"))
        self.assertTrue(any("complete" in msg for msg in order))

    def test_skips_reason_steps_for_clean_score(self) -> None:
        page = MagicMock()
        with patch("workflow.submit._scroll_band_into_view"):
            with patch("workflow.submit._click_band_score"):
                with patch("workflow.submit._verify_band_filled"):
                    with patch(
                        "workflow.submit._wait_and_verify_band_reason_field"
                    ) as wait_reason:
                        with patch("workflow.submit._click_band_confidence"):
                            with patch("workflow.submit._fill_band_reason") as fill_reason:
                                with patch(
                                    "workflow.submit._band_form_snapshot",
                                    return_value={
                                        "score": 3,
                                        "confidence": "high",
                                        "reasonLen": 0,
                                    },
                                ):
                                    _fill_band_sequential(
                                        page,
                                        "Solution & Code",
                                        score=3,
                                        confidence="high",
                                        log=lambda _: None,
                                    )
        wait_reason.assert_not_called()
        fill_reason.assert_not_called()


class FillSubmitFormSequentialTests(unittest.TestCase):
    def _sample_review(self) -> dict:
        return {
            "decision": "request_changes",
            "band_ratings": {
                "problem": {"score": 2, "confidence": "medium", "reasoning": "x" * 10},
                "tests": {"score": 3, "confidence": "high", "reasoning": ""},
                "solution": {"score": 3, "confidence": "medium", "reasoning": ""},
            },
            "contributor_feedback": "Author note body here.",
            "suggested_tags": ["tag-a"],
        }

    def test_steps_run_in_order_with_step_logging(self) -> None:
        page = MagicMock()
        page.get_by_text.return_value.count.return_value = 1
        logs: list[str] = []

        def log(msg: str) -> None:
            logs.append(msg)

        with patch("workflow.submit._ensure_submit_review_form"):
            with patch("workflow.submit._click_decision"):
                with patch("workflow.submit._verify_decision_selected"):
                    with patch("workflow.submit._fill_band_sequential") as fill_band:
                        with patch("workflow.submit._fill_author_note"):
                            with patch("workflow.submit._verify_author_note"):
                                with patch("workflow.submit._click_suggested_tags"):
                                    with patch("workflow.submit._validate_submit_form", return_value=[]):
                                        with patch("workflow.submit._ensure_all_band_confidences"):
                                            with patch(
                                                "workflow.submit._wait_submit_enabled",
                                                return_value=True,
                                            ):
                                                _fill_submit_form(
                                                page,
                                                self._sample_review(),
                                                self._sample_review()["band_ratings"],
                                                quest="olympus",
                                                log=log,
                                            )

        self.assertEqual(fill_band.call_count, 3)
        step_logs = [line for line in logs if line.startswith("submit: Step")]
        self.assertIn("submit: Step 1/9 — open review form", step_logs)
        self.assertIn("submit: Step 2/9 — select decision", step_logs)
        self.assertIn("submit: Step 3/9 — fill band ratings sequentially", step_logs)
        self.assertIn("submit: Step 4/9 — fill author note", step_logs)
        self.assertIn("submit: Step 5/9 — click suggested tags", step_logs)
        self.assertIn("submit: Step 7/9 — final validation", step_logs)
        self.assertIn("submit: Step 8/9 — wait for Submit button enabled", step_logs)
        self.assertLess(
            step_logs.index("submit: Step 2/9 — select decision"),
            step_logs.index("submit: Step 3/9 — fill band ratings sequentially"),
        )
        self.assertLess(
            step_logs.index("submit: Step 4/9 — fill author note"),
            step_logs.index("submit: Step 7/9 — final validation"),
        )


if __name__ == "__main__":
    unittest.main()
