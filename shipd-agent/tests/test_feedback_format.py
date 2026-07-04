# Tests for compact author-note formatting.

from __future__ import annotations

import unittest

from review.feedback_format import format_compact_author_note


def _review(
    *,
    feedback: str = "",
    problem_score: int = 3,
    problem_reasoning: str = "",
    tests_score: int = 3,
    tests_reasoning: str = "",
    solution_score: int = 3,
    solution_reasoning: str = "",
) -> dict:
    return {
        "contributor_feedback": feedback,
        "band_ratings": {
            "problem": {
                "score": problem_score,
                "confidence": "high",
                "reasoning": problem_reasoning,
            },
            "tests": {
                "score": tests_score,
                "confidence": "high",
                "reasoning": tests_reasoning,
            },
            "solution": {
                "score": solution_score,
                "confidence": "high",
                "reasoning": solution_reasoning,
            },
        },
    }


class FormatCompactAuthorNoteTests(unittest.TestCase):
    def test_merges_feedback_and_band_reasoning(self) -> None:
        note = format_compact_author_note(
            _review(
                feedback="Problem (2/3): Scope unclear — specify Lark versions.",
                tests_score=2,
                tests_reasoning="Missing edge cases for ambiguous grammars.",
            )
        )
        self.assertEqual(
            note,
            "Problem (2/3): Scope unclear — specify Lark versions.\n"
            "Tests (2/3): Missing edge cases for ambiguous grammars.",
        )

    def test_deduplicates_overlapping_band_reasoning(self) -> None:
        note = format_compact_author_note(
            _review(
                feedback=(
                    "Problem (2/3): Scope unclear — specify which Lark versions are in scope."
                ),
                problem_score=2,
                problem_reasoning=(
                    "Scope unclear — specify which Lark versions are in scope."
                ),
            )
        )
        self.assertEqual(
            note,
            "Problem (2/3): Scope unclear — specify which Lark versions are in scope.",
        )

    def test_skips_band_reasoning_when_prefixed_line_exists(self) -> None:
        note = format_compact_author_note(
            _review(
                feedback="Tests (2/3): Add coverage for empty input.",
                tests_score=2,
                tests_reasoning="Needs more empty-input coverage.",
            )
        )
        self.assertEqual(note, "Tests (2/3): Add coverage for empty input.")

    def test_band_reasoning_only_when_no_feedback(self) -> None:
        note = format_compact_author_note(
            _review(
                solution_score=1,
                solution_reasoning="Implementation doesn't handle nested labels.",
            )
        )
        self.assertEqual(
            note,
            "Solution (1/3): Implementation doesn't handle nested labels.",
        )

    def test_strips_bullets_and_blank_lines(self) -> None:
        note = format_compact_author_note(
            _review(
                feedback="- First issue\n\n* Second issue\n2. Third issue",
            )
        )
        self.assertEqual(note, "First issue\nSecond issue\nThird issue")

    def test_approve_short_feedback_unchanged(self) -> None:
        note = format_compact_author_note(
            _review(feedback="Looks good — minor typo in README."),
        )
        self.assertEqual(note, "Looks good — minor typo in README.")

    def test_ignores_band_reasoning_at_score_three(self) -> None:
        note = format_compact_author_note(
            _review(
                feedback="",
                problem_score=3,
                problem_reasoning="Should not appear",
            )
        )
        self.assertEqual(note, "")


if __name__ == "__main__":
    unittest.main()
