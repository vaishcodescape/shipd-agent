# Unit tests for review page text parsing (no browser required).

from __future__ import annotations

import unittest

from review.scrape import (
    parse_agent_runs_from_text,
    parse_holistic_check_from_text,
    parse_related_submissions_from_text,
    scrape_context_for_prompts,
)


SAMPLE_HOLISTIC_TEXT = """
Holistic Check
Automated check of problem quality, fairness, and agent failure patterns.
PASS
CHECKLIST
12 pass
Reviewer Notes
The problem appears ready for agent evaluation. Agent runs show a 3/10 pass
rate on Olympus, suggesting solvability concerns for weaker models.

Fairness: tests align with stated requirements; no obvious leakage detected.
Re-run
""".strip()

SAMPLE_AGENT_RUNS_TEXT = """
Agent Runs
Olympus evaluation complete.
3/10 pass on claude-sonnet
Median solution LOC: 142 lines
Failure patterns
Agents fail on edge case handling for empty input.
Timeout on large payloads.
""".strip()

SAMPLE_RELATED_TEXT = """
Related Submissions
PR #4821 — 87% similar — duplicate tag
Older submission from same author — 62% match
""".strip()


class HolisticCheckParserTests(unittest.TestCase):
    def test_parses_status_checklist_and_notes(self) -> None:
        result = parse_holistic_check_from_text(SAMPLE_HOLISTIC_TEXT)

        self.assertTrue(result["available"])
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["checklist_summary"], "12 pass")
        self.assertIn("3/10 pass rate", result["reviewer_notes"])
        self.assertIn("Fairness:", result["reviewer_notes"])
        self.assertNotIn("Re-run", result["reviewer_notes"])

    def test_empty_text_not_available(self) -> None:
        result = parse_holistic_check_from_text("")
        self.assertFalse(result["available"])
        self.assertIsNone(result["status"])

    def test_fail_status(self) -> None:
        text = "Holistic Check\nFAIL\nCHECKLIST\n2 pass, 5 fail\nReviewer Notes\nNeeds work."
        result = parse_holistic_check_from_text(text)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("2 pass", result["checklist_summary"])


class AgentRunsParserTests(unittest.TestCase):
    def test_parses_pass_rate_and_failures(self) -> None:
        result = parse_agent_runs_from_text(SAMPLE_AGENT_RUNS_TEXT)

        self.assertTrue(result["available"])
        self.assertIn("3/10", result["pass_rate"])
        self.assertIn("Olympus", result["summary"])
        self.assertIn("empty input", result["failure_patterns"])

    def test_empty_text_not_available(self) -> None:
        result = parse_agent_runs_from_text("")
        self.assertFalse(result["available"])


class RelatedSubmissionsParserTests(unittest.TestCase):
    def test_parses_similarity_and_tags(self) -> None:
        result = parse_related_submissions_from_text(SAMPLE_RELATED_TEXT)

        self.assertTrue(result["available"])
        self.assertIn("87% similar", result["entries"])
        self.assertIn("duplicate", result["tags"])
        self.assertIn("older", result["tags"])

    def test_empty_text_not_available(self) -> None:
        result = parse_related_submissions_from_text("")
        self.assertFalse(result["available"])


class ScrapeContextTests(unittest.TestCase):
    def test_prompt_strings_include_scraped_fields(self) -> None:
        holistic = parse_holistic_check_from_text(SAMPLE_HOLISTIC_TEXT)
        agent_runs = parse_agent_runs_from_text(SAMPLE_AGENT_RUNS_TEXT)
        related = parse_related_submissions_from_text(SAMPLE_RELATED_TEXT)

        ctx = scrape_context_for_prompts(
            holistic=holistic,
            agent_runs=agent_runs,
            related=related,
        )

        self.assertEqual(ctx["holistic_check_available"], "true")
        self.assertEqual(ctx["holistic_check_status"], "PASS")
        self.assertIn("3/10", ctx["agent_runs"])
        self.assertIn("87% similar", ctx["related_submissions"])


if __name__ == "__main__":
    unittest.main()
