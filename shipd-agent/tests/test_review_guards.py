# Unit tests for review accuracy guards: deterministic band-score capping,
# coverage-gap flagging. Deep Agents provenance tests live in test_deep_review.py.
# These exercise review/graph.py, which previously had no test coverage — the
# blind spot that let phases/factors get silently skipped.

from __future__ import annotations

import unittest

from review import graph as g
from review.config import get_review_config
from review.schemas import BandRating, BandRatings, Finding, PhaseResult, ReviewResult


def _bands(problem: int = 2, tests: int = 2, solution: int = 2) -> BandRatings:
    return BandRatings(
        problem=BandRating(score=problem, confidence="high", reasoning="note"),
        tests=BandRating(score=tests, confidence="high", reasoning="note"),
        solution=BandRating(score=solution, confidence="high", reasoning="note"),
    )


def _review(
    *,
    decision: str = "approve",
    bands: BandRatings | None = None,
    phases: dict[str, PhaseResult] | None = None,
    findings: list[Finding] | None = None,
) -> ReviewResult:
    phase_results = {str(i): PhaseResult(status="PASS", summary="ok") for i in range(7)}
    if phases:
        phase_results.update(phases)
    return ReviewResult(
        decision=decision,  # type: ignore[arg-type]
        band_ratings=bands or _bands(),
        phase_results=phase_results,
        findings=findings or [],
        recommendation_summary="Summary.",
        contributor_feedback="Feedback.",
    )


class BandCapTests(unittest.TestCase):
    def _phase_results(self, review: ReviewResult) -> dict[str, PhaseResult]:
        return {k: v for k, v in review.phase_results.items()}

    def test_phase4_fail_caps_solution_band(self) -> None:
        review = _review(phases={"4": PhaseResult(status="FAIL", summary="LOC 150 < 400")})
        capped, reasons = g._cap_bands_to_deterministic(review, self._phase_results(review))
        self.assertIsNotNone(capped)
        self.assertEqual(capped.solution.score, 1)
        self.assertTrue(capped.solution.reasoning.strip())
        # Sibling bands are untouched.
        self.assertEqual(capped.problem.score, 2)
        self.assertEqual(capped.tests.score, 2)
        self.assertTrue(any("solution" in r for r in reasons))

    def test_phase1_fail_caps_problem_band(self) -> None:
        review = _review(phases={"1": PhaseResult(status="FAIL", summary="vague")})
        capped, _ = g._cap_bands_to_deterministic(review, self._phase_results(review))
        self.assertEqual(capped.problem.score, 1)
        self.assertEqual(capped.solution.score, 2)

    def test_major_finding_caps_governing_band(self) -> None:
        review = _review(
            findings=[Finding(phase="3", severity="MAJOR", finding="flaky", evidence="x")]
        )
        capped, _ = g._cap_bands_to_deterministic(review, self._phase_results(review))
        self.assertEqual(capped.tests.score, 1)  # tests band ← phases 2/3

    def test_all_pass_leaves_bands_unchanged(self) -> None:
        review = _review()
        capped, reasons = g._cap_bands_to_deterministic(review, self._phase_results(review))
        self.assertIsNone(capped)
        self.assertEqual(reasons, [])

    def test_never_raises_a_low_score(self) -> None:
        review = _review(
            bands=_bands(solution=0),
            phases={"4": PhaseResult(status="FAIL", summary="x")},
        )
        capped, _ = g._cap_bands_to_deterministic(review, self._phase_results(review))
        # Score already <= 1, so it is left alone (never raised to 1).
        self.assertIsNone(capped)


class ValidateNodeCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = get_review_config()

    def _state(self, review: ReviewResult, scrape: dict | None = None) -> dict:
        raw = review.to_submit_dict()
        raw["review_complete"] = True
        return {
            "review_result": raw,
            "config": self.config,
            "quest": "olympus",
            "scrape_context": scrape or {},
            "loc_info": {},
        }

    def test_evaluable_skip_forces_request_changes_but_stays_complete(self) -> None:
        review = _review(phases={"3": PhaseResult(status="SKIP", summary="not evaluated")})
        out = g.validate_node(self._state(review))["review_result"]
        self.assertEqual(out["decision"], "request_changes")
        self.assertIs(out.get("review_complete"), True)
        self.assertEqual(out["phase_results"]["3"]["status"], "SKIP")
        self.assertIn("Phase 3", out["internal_notes"])

    def test_phase5_skip_without_data_is_not_a_gap(self) -> None:
        review = _review(phases={"5": PhaseResult(status="SKIP", summary="no agent data")})
        scrape = {
            "agent_runs": "not available",
            "related_submissions": "not available",
            "holistic_check_available": "false",
        }
        out = g.validate_node(self._state(review, scrape))["review_result"]
        self.assertEqual(out["decision"], "approve")

    def test_phase5_skip_with_data_is_a_gap(self) -> None:
        review = _review(phases={"5": PhaseResult(status="SKIP", summary="skipped")})
        scrape = {
            "agent_runs": "10/10 runs, 15% pass rate",
            "related_submissions": "not available",
            "holistic_check_available": "false",
        }
        out = g.validate_node(self._state(review, scrape))["review_result"]
        self.assertEqual(out["decision"], "request_changes")


if __name__ == "__main__":
    unittest.main()
