# Unit tests for Phase 1 task prompt checks: the 1,000-word limit and the
# "Too Many Requirements" check (heuristic count + Shipd's own Holistic flag).

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from review.config import get_review_config
from review.prompt_checks import (
    analyze_task_prompt,
    count_words,
    evaluate_prompt_phase1,
    extract_requirements,
    find_task_prompt_path,
    format_prompt_analysis,
    platform_too_many_requirements_flag,
)

FOCUSED_PROMPT = """
The CSV exporter drops rows whose quoted fields contain embedded newlines.

Exported files must preserve those rows exactly as they appear in the source table.
Rows without embedded newlines should be written unchanged.
""".strip()

# Rubric anti-pattern: "bullet laundry of micro-requests" — 12 asks under a vague
# umbrella line that is not itself a requirement.
SPRAWLING_PROMPT = "Improve the exporter.\n\n" + "\n".join(
    f"- Support option number {i} in the exporter." for i in range(1, 13)
)


def _words(n: int) -> str:
    return " ".join(["word"] * n)


class _RepoCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()


class CountWordsTests(unittest.TestCase):
    def test_counts_words_not_markup(self) -> None:
        self.assertEqual(count_words("# Title\n\n- one `two` three — four."), 5)

    def test_empty(self) -> None:
        self.assertEqual(count_words(""), 0)


class ExtractRequirementsTests(unittest.TestCase):
    def test_focused_prose_has_few_requirements(self) -> None:
        self.assertEqual(len(extract_requirements(FOCUSED_PROMPT)), 2)

    def test_each_list_item_is_a_requirement(self) -> None:
        self.assertEqual(len(extract_requirements(SPRAWLING_PROMPT)), 12)

    def test_modals_in_one_sentence_count_separately(self) -> None:
        text = "The parser must accept tabs and must reject NUL bytes."
        self.assertEqual(len(extract_requirements(text)), 2)

    def test_imperative_sentence_counts_once(self) -> None:
        self.assertEqual(len(extract_requirements("Add a --dry-run flag to the CLI.")), 1)

    def test_background_prose_is_not_a_requirement(self) -> None:
        text = "The exporter was added in 2019. It writes CSV files to disk."
        self.assertEqual(extract_requirements(text), [])

    def test_code_fences_and_headings_are_ignored(self) -> None:
        text = (
            "## Requirements\n\nThe flag must be documented.\n\n"
            "```text\nThe output must be sorted.\n- Add a retry flag.\n```\n"
        )
        self.assertEqual(extract_requirements(text), ["The flag must be documented."])


class PlatformFlagTests(unittest.TestCase):
    def test_flag_on_same_line(self) -> None:
        text = "CHECKLIST\nClear scope: PASS\nToo Many Requirements: FAIL\n"
        self.assertEqual(
            platform_too_many_requirements_flag(text), "Too Many Requirements: FAIL"
        )

    def test_flag_as_separate_badge_line(self) -> None:
        text = "CHECKLIST\nToo Many Requirements\nFAIL\nReviewer Notes\n"
        self.assertIsNotNone(platform_too_many_requirements_flag(text))

    def test_passing_check_is_not_a_flag(self) -> None:
        self.assertIsNone(platform_too_many_requirements_flag("Too Many Requirements: PASS"))
        self.assertIsNone(platform_too_many_requirements_flag("Too Many Requirements ✅"))
        self.assertIsNone(platform_too_many_requirements_flag("Holistic Check\nPASS\n12 pass"))


class FindTaskPromptPathTests(_RepoCase):
    def test_prefers_explicit_prompt_file(self) -> None:
        (self.repo / "README.md").write_text(_words(5000), encoding="utf-8")
        (self.repo / "problem.md").write_text(FOCUSED_PROMPT, encoding="utf-8")
        self.assertEqual(find_task_prompt_path(self.repo), self.repo / "problem.md")

    def test_never_measures_the_project_readme(self) -> None:
        # A cloned project's own README is not the task prompt.
        (self.repo / "README.md").write_text(_words(5000), encoding="utf-8")
        self.assertIsNone(find_task_prompt_path(self.repo))


class EvaluatePromptPhase1Tests(_RepoCase):
    def _evaluate(self, text: str, *, holistic: str = "", **limits):
        (self.repo / "problem.md").write_text(text, encoding="utf-8")
        analysis = analyze_task_prompt(self.repo, holistic_text=holistic)
        return analysis, *evaluate_prompt_phase1(analysis, **limits)

    def test_focused_prompt_passes(self) -> None:
        _, phase, findings = self._evaluate(FOCUSED_PROMPT)
        self.assertEqual(phase.status, "PASS")
        self.assertEqual(findings, [])

    def test_exactly_1000_words_passes(self) -> None:
        _, phase, findings = self._evaluate(_words(1000))
        self.assertEqual(phase.status, "PASS")
        self.assertEqual(findings, [])

    def test_1001_words_fails(self) -> None:
        _, phase, findings = self._evaluate(_words(1001))
        self.assertEqual(phase.status, "FAIL")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].phase, "1")
        self.assertEqual(findings[0].severity, "MAJOR")
        self.assertIn("1001 words (limit 1000)", findings[0].evidence)

    def test_too_many_requirements_fails(self) -> None:
        _, phase, findings = self._evaluate(SPRAWLING_PROMPT)
        self.assertEqual(phase.status, "FAIL")
        self.assertEqual(
            [f.finding for f in findings], ["Too many independent requirements in task prompt"]
        )
        self.assertIn("~12 independent requirements", findings[0].evidence)

    def test_platform_flag_fails_even_for_a_short_prompt(self) -> None:
        _, phase, findings = self._evaluate(
            FOCUSED_PROMPT, holistic="Too Many Requirements: FAIL"
        )
        self.assertEqual(phase.status, "FAIL")
        self.assertEqual([f.finding for f in findings], ["Shipd flagged Too Many Requirements"])

    def test_platform_flag_and_heuristic_report_once(self) -> None:
        _, _, findings = self._evaluate(
            SPRAWLING_PROMPT, holistic="Too Many Requirements: FAIL"
        )
        self.assertEqual([f.finding for f in findings], ["Shipd flagged Too Many Requirements"])
        self.assertIn("~12 independent requirements", findings[0].evidence)

    def test_long_and_sprawling_reports_both(self) -> None:
        _, _, findings = self._evaluate(SPRAWLING_PROMPT + "\n\n" + _words(1000))
        self.assertEqual(len(findings), 2)

    def test_limits_are_configurable(self) -> None:
        _, phase, _ = self._evaluate(FOCUSED_PROMPT, max_words=10)
        self.assertEqual(phase.status, "FAIL")
        _, phase, _ = self._evaluate(SPRAWLING_PROMPT, max_requirements=20)
        self.assertEqual(phase.status, "PASS")

    def test_no_prompt_file_returns_no_verdict(self) -> None:
        analysis = analyze_task_prompt(self.repo)
        phase, findings = evaluate_prompt_phase1(analysis)
        self.assertIsNone(phase)
        self.assertEqual(findings, [])
        self.assertIn("not found", format_prompt_analysis(analysis))

    def test_platform_flag_without_prompt_file_still_fails(self) -> None:
        analysis = analyze_task_prompt(self.repo, holistic_text="Too Many Requirements: FAIL")
        phase, findings = evaluate_prompt_phase1(analysis)
        self.assertEqual(phase.status, "FAIL")
        self.assertEqual(len(findings), 1)

    def test_analysis_text_reports_counts(self) -> None:
        analysis, _, _ = self._evaluate(FOCUSED_PROMPT)
        text = format_prompt_analysis(analysis)
        self.assertIn("problem.md", text)
        self.assertIn(f"{analysis.word_count} words (limit 1000)", text)
        self.assertIn("~2 independent requirements", text)


class PromptLimitConfigTests(unittest.TestCase):
    def test_defaults_match_shipd_bars(self) -> None:
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("REVIEW_MAX_PROMPT_WORDS", "REVIEW_MAX_PROMPT_REQUIREMENTS")
        }
        with patch.dict(os.environ, env, clear=True), patch("review.config.load_dotenv"):
            config = get_review_config()
        self.assertEqual((config.max_prompt_words, config.max_prompt_requirements), (1000, 10))

    def test_env_overrides(self) -> None:
        overrides = {"REVIEW_MAX_PROMPT_WORDS": "800", "REVIEW_MAX_PROMPT_REQUIREMENTS": "6"}
        with patch.dict(os.environ, overrides), patch("review.config.load_dotenv"):
            config = get_review_config()
        self.assertEqual((config.max_prompt_words, config.max_prompt_requirements), (800, 6))


if __name__ == "__main__":
    unittest.main()
