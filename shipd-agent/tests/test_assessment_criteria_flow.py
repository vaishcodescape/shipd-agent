# End-to-end tests for the 2026-10 assessment criteria through the review graph:
# the task prompt bars (1,000 words, Too Many Requirements) and the agent run
# bar (Nova, Vega, Orion × 4 runs, exactly one passing run).
#
# The finalize LLM is faked (no API calls); it approves everything, so any
# FAIL/request_changes in the output comes from the deterministic checks. The
# browser cases scrape a synthetic Shipd review page with the real scraper; they
# skip without chromium unless SHIPD_REQUIRE_BROWSER=1 (set in CI).

from __future__ import annotations

import dataclasses
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from review import graph as g
from review.config import get_review_config
from review.prompts import FINALIZE_PHASE_INSTRUCTIONS, build_unified_review_user_prompt
from review.schemas import BandRating, BandRatings, PhaseResult, ReviewResult
from review.scrape import parse_agent_runs_from_text

try:
    from playwright.sync_api import sync_playwright

    _PW_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - environment without playwright
    sync_playwright = None  # type: ignore[assignment]
    _PW_IMPORT_ERROR = exc

FOCUSED_PROMPT = (
    "The CSV exporter drops rows whose quoted fields contain embedded newlines.\n\n"
    "Exported files must preserve those rows exactly as they appear in the source table.\n"
)
# Over the word limit and a bullet laundry of independent asks.
SPRAWLING_PROMPT = (
    "Improve the exporter.\n\n"
    + "\n".join(f"- Support option number {i} in the exporter." for i in range(1, 13))
    + "\n\n"
    + " ".join(["context"] * 1000)
)

ONE_PASS_ROWS = ("Nova: 1/4 pass", "Vega: 0/4 pass", "Orion: 0/4 pass")
TWO_PASS_ROWS = ("Nova: 1/4 pass", "Vega: 1/4 pass", "Orion: 0/4 pass")


def _approving_review() -> ReviewResult:
    band = BandRating(score=3, confidence="high", reasoning="Clean.")
    return ReviewResult(
        decision="approve",
        band_ratings=BandRatings(problem=band, tests=band, solution=band),
        phase_results={
            str(i): PhaseResult(status="PASS", summary=f"Reviewer phase {i} ok.")
            for i in range(7)
        },
        findings=[],
        recommendation_summary="Ready.",
        contributor_feedback="Looks good.",
        repo_eligible=True,
        solvability_ok=True,
    )


def _fake_chat(review: ReviewResult) -> MagicMock:
    """Stand-in for ChatAnthropic whose structured output is ``review``."""
    chat = MagicMock()
    structured = chat.return_value.with_structured_output.return_value
    structured.invoke.return_value = review
    return chat


def _config(**overrides):
    base = dataclasses.replace(
        get_review_config(),
        anthropic_api_key="test-key",
        review_dry_run=False,
        review_phase0="fast",
        review_skip_explore_on_phase0_fail=True,
    )
    return dataclasses.replace(base, **overrides)


def _finding_names(result: dict, phase: str) -> list[str]:
    return [f["finding"] for f in result["findings"] if f["phase"] == phase]


class _RepoCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_prompt(self, text: str) -> None:
        (self.repo / "problem.md").write_text(text, encoding="utf-8")


class Phase0KeepsScrapeFindingsTests(_RepoCase):
    def test_agent_run_findings_survive_phase0(self) -> None:
        # Regression: Phase 0 used to overwrite state["findings"], dropping the
        # agent run findings recorded at scrape time.
        panel = "Agent Runs\n" + "\n".join(TWO_PASS_ROWS)
        state = {
            "quest": "olympus",
            "agent_runs_data": parse_agent_runs_from_text(panel),
            "phase_results": {},
            "findings": [],
        }
        state.update(g._run_agent_runs_analysis(state))
        state.update(
            {"repo_path": str(self.repo), "config": _config(), "submission_summary": {}}
        )
        out = g._run_phase0_into_state(state)
        self.assertIn("Too many passing agent runs", _finding_names(out, "5"))
        self.assertEqual(out["phase_results"]["5"]["status"], "FAIL")

    def test_unreadable_agent_panel_leaves_phase5_to_reviewer(self) -> None:
        state = {
            "quest": "olympus",
            "agent_runs_data": parse_agent_runs_from_text("Agent Runs\nLoading…"),
            "phase_results": {},
            "findings": [],
        }
        self.assertEqual(g._run_agent_runs_analysis(state), {})


class FinalizeAndValidateTests(_RepoCase):
    def _run(self, *, prompt: str, agent_rows: tuple[str, ...]) -> tuple[dict, MagicMock]:
        self.write_prompt(prompt)
        config = _config()
        panel = "Agent Runs\n12/12 agent runs complete.\n" + "\n".join(agent_rows)
        panel += "\nMedian files: 5, messages: 146.5, LOC: 420"
        state: dict = {
            "repo_path": str(self.repo),
            "quest": "olympus",
            "review_url": "",
            "config": config,
            "submission_summary": {},
            "agent_runs_data": parse_agent_runs_from_text(panel),
            "scrape_context": {"agent_runs": panel, "holistic_check_available": "false"},
            "phase_results": {"0": {"status": "PASS", "summary": "Phase 0 ok."}},
            "findings": [],
            "loc_info": {},
        }
        state.update(g._run_agent_runs_analysis(state))
        state.update(g._run_prompt_analysis(state))
        chat = _fake_chat(_approving_review())
        with patch.object(g, "ChatAnthropic", chat):
            state.update(g.finalize_node(state))
        return g.validate_node(state)["review_result"], chat

    def test_new_criteria_override_an_approving_reviewer(self) -> None:
        result, _ = self._run(prompt=SPRAWLING_PROMPT, agent_rows=TWO_PASS_ROWS)

        self.assertEqual(result["decision"], "request_changes")
        self.assertEqual(result["phase_results"]["1"]["status"], "FAIL")
        # The reviewer's wider phase 1 judgement is kept alongside the verdict.
        self.assertIn("Reviewer phase 1 ok.", result["phase_results"]["1"]["summary"])
        self.assertEqual(result["phase_results"]["5"]["status"], "FAIL")
        self.assertEqual(
            _finding_names(result, "1"),
            [
                "Task prompt exceeds the 1,000-word limit",
                "Too many independent requirements in task prompt",
            ],
        )
        self.assertEqual(_finding_names(result, "5"), ["Too many passing agent runs"])
        self.assertEqual(result["band_ratings"]["problem"]["score"], 1)

    def test_compliant_submission_stays_approved(self) -> None:
        result, chat = self._run(prompt=FOCUSED_PROMPT, agent_rows=ONE_PASS_ROWS)

        self.assertEqual(result["decision"], "approve")
        self.assertEqual(result["phase_results"]["1"]["summary"], "Reviewer phase 1 ok.")
        self.assertEqual(result["phase_results"]["5"]["status"], "PASS")
        self.assertEqual(result["band_ratings"]["problem"]["score"], 3)
        self.assertEqual(_finding_names(result, "1") + _finding_names(result, "5"), [])

        # The finalize model was given the deterministic task prompt analysis.
        messages = chat.return_value.with_structured_output.return_value.invoke.call_args[0][0]
        self.assertIn("words (limit 1000)", messages[1].content)


class PromptTextTests(unittest.TestCase):
    def test_explore_prompt_states_new_bars(self) -> None:
        text = build_unified_review_user_prompt(
            quest="olympus",
            repo_path="/repo",
            commit=None,
            phase0_log="",
            phase0_status="PASS",
            agent_runs="not available",
            related_submissions="not available",
            prompt_analysis="Task prompt (problem.md): 812 words (limit 1000).",
        )
        self.assertIn("Nova, Vega, and Orion must each run 4 times (12 runs total)", text)
        self.assertIn("exactly 1 run must pass", text)
        self.assertIn("at most 1000 words", text)
        self.assertIn("Too Many Requirements", text)
        self.assertIn("812 words (limit 1000)", text)
        self.assertNotIn("pass rate ≤", text)

    def test_finalize_instructions_state_new_bars(self) -> None:
        self.assertIn("Nova, Vega, and Orion must each run 4 times", FINALIZE_PHASE_INSTRUCTIONS)
        self.assertIn("At most 1,000 words", FINALIZE_PHASE_INSTRUCTIONS)
        self.assertNotIn("At least 10 agent runs", FINALIZE_PHASE_INSTRUCTIONS)

    def test_finalize_system_prompt_carries_new_bars(self) -> None:
        # The rubric body is middle-truncated at the default cap, so the bars
        # must survive in the preamble the finalize model always receives.
        excerpt = g._load_rubric_excerpt(get_review_config().rubric_path, max_chars=16_000)
        self.assertIn("Nova, Vega, and Orion must each run 4 times", excerpt)
        self.assertIn("Too Many Requirements", excerpt)
        self.assertIn("At most 1,000 words", excerpt)

    def test_rubric_document_states_new_bars(self) -> None:
        rubric = Path(get_review_config().rubric_path).read_text(encoding="utf-8")
        for phrase in ("Too Many Requirements", "1,000 words", "Nova", "Vega", "Orion"):
            self.assertIn(phrase, rubric)
        self.assertNotIn("10/10", rubric)


def _review_page_html(*, agent_rows: tuple[str, ...], requirements_check: str) -> str:
    rows = "".join(f"<div>{row}</div>" for row in agent_rows)
    return f"""<!doctype html><html><body><main>
<section><h2>Holistic Check</h2>
  <p>Automated check of problem quality, fairness, and agent failure patterns.</p>
  <div>PASS</div>
  <div>CHECKLIST</div>
  <ul><li>Clear scope: PASS</li><li>Too Many Requirements: {requirements_check}</li></ul>
  <div>Reviewer Notes</div><p>The problem appears ready for agent evaluation.</p>
</section>
<section><h2>Agent Runs</h2>
  <div>12/12 agent runs complete.</div>{rows}
  <div>Median files: 5, messages: 146.5, LOC: 420</div>
</section>
<section><h2>Related Submissions</h2><div>No related submissions.</div></section>
</main></body></html>"""


@unittest.skipIf(sync_playwright is None, f"playwright unavailable: {_PW_IMPORT_ERROR}")
class ReviewGraphOnScrapedPageTests(_RepoCase):
    """Real scraper + real graph against a synthetic Shipd review page."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._pw = sync_playwright().start()
        try:
            cls._browser = cls._pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - browser binary missing
            cls._pw.stop()
            if os.environ.get("SHIPD_REQUIRE_BROWSER") == "1":
                raise
            raise unittest.SkipTest(f"chromium unavailable: {exc}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._browser.close()
        cls._pw.stop()

    def _review(self, *, config, prompt: str, agent_rows: tuple[str, ...], check: str) -> dict:
        self.write_prompt(prompt)
        page = self._browser.new_page()
        try:
            page.set_content(
                _review_page_html(agent_rows=agent_rows, requirements_check=check)
            )
            chat = _fake_chat(_approving_review())
            with patch.object(g, "ChatAnthropic", chat):
                return g.run_review_graph(
                    repo_path=str(self.repo),
                    quest="olympus",
                    review_url="https://shipd.ai/review/test",
                    config=config,
                    page=page,
                )
        finally:
            page.close()

    def test_dry_run_reports_every_failed_bar(self) -> None:
        result = self._review(
            config=_config(review_dry_run=True),
            prompt=SPRAWLING_PROMPT,
            agent_rows=TWO_PASS_ROWS,
            check="FAIL",
        )
        self.assertEqual(result["phase_results"]["1"]["status"], "FAIL")
        self.assertEqual(result["phase_results"]["5"]["status"], "FAIL")
        self.assertEqual(
            _finding_names(result, "1"),
            ["Task prompt exceeds the 1,000-word limit", "Shipd flagged Too Many Requirements"],
        )
        self.assertEqual(_finding_names(result, "5"), ["Too many passing agent runs"])

    def test_dry_run_passes_a_compliant_submission(self) -> None:
        result = self._review(
            config=_config(review_dry_run=True),
            prompt=FOCUSED_PROMPT,
            agent_rows=ONE_PASS_ROWS,
            check="PASS",
        )
        self.assertEqual(result["phase_results"]["1"]["status"], "PASS")
        self.assertEqual(result["phase_results"]["5"]["status"], "PASS")
        self.assertIn("Nova 1/4", result["phase_results"]["5"]["summary"])
        self.assertEqual(_finding_names(result, "1") + _finding_names(result, "5"), [])

    def test_full_graph_caps_and_reports_through_finalize(self) -> None:
        result = self._review(
            config=_config(),
            prompt=SPRAWLING_PROMPT,
            agent_rows=TWO_PASS_ROWS,
            check="FAIL",
        )
        self.assertTrue(result.get("review_complete"))
        self.assertEqual(result["decision"], "request_changes")
        self.assertEqual(result["phase_results"]["1"]["status"], "FAIL")
        self.assertEqual(result["phase_results"]["5"]["status"], "FAIL")
        self.assertIn("Too many passing agent runs", _finding_names(result, "5"))
        self.assertIn("Shipd flagged Too Many Requirements", _finding_names(result, "1"))
        self.assertEqual(result["band_ratings"]["problem"]["score"], 1)


if __name__ == "__main__":
    unittest.main()
