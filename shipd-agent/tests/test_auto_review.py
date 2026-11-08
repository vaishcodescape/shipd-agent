# Auto Review prediction stage: dossier, model call, graph wiring, and the
# configurable model and agent line-up. No network; models are scripted.

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from review import auto_review as ar
from review import graph as g
from review import llm
from review.auto_review import AutoReviewItem, AutoReviewPrediction, apply_prediction
from review.config import get_review_config
from review.deep import ReviewBudget
from review.schemas import BandRating, BandRatings, PhaseResult, ReviewResult
from review.staleness import content_fingerprint
from test_llm_config import FAKE_KEY, IsolatedEnvTestCase
from test_llm_factory import make_config

_TEST_PATCH = "+++ b/tests/test_app.py\n+def test_retry():\n+    assert client.retries == 3\n"
_SOLUTION_PATCH = "+++ b/src/app.py\n+retries = 3\n"


def _write_repo(root: Path, *, prompt: bool = True, solution: bool = True) -> Path:
    if prompt:
        (root / "problem.md").write_text("Add retries to the client.\n", encoding="utf-8")
    (root / "test.patch").write_text(_TEST_PATCH, encoding="utf-8")
    if solution:
        (root / "solution.patch").write_text(_SOLUTION_PATCH, encoding="utf-8")
    return root


def _draft(decision: str = "approve") -> dict:
    band = BandRating(score=3, confidence="high", reasoning="")
    review = ReviewResult(
        decision=decision,  # type: ignore[arg-type]
        band_ratings=BandRatings(problem=band, tests=band, solution=band),
        phase_results={str(i): PhaseResult(status="PASS", summary="ok") for i in range(7)},
        recommendation_summary="Looks good.",
        contributor_feedback="Nice work.",
    )
    return {**review.to_submit_dict(), "review_complete": True}


def _prediction(severity: str) -> AutoReviewPrediction:
    return AutoReviewPrediction(
        summary="The tests demand a retry count the prompt never states.",
        findings=[
            AutoReviewItem(
                severity=severity,
                finding="prompt and tests disagree on the retry count",
                evidence="test.patch asserts retries == 3; problem.md gives no number.",
            )
        ],
    )


class ApplyPredictionTests(unittest.TestCase):
    def test_blocker_becomes_major_and_fails(self) -> None:
        phase, findings = apply_prediction(_prediction("BLOCKER"))
        self.assertEqual(phase.status, "FAIL")
        self.assertEqual(findings[0].severity, "MAJOR")
        self.assertEqual(findings[0].phase, ar.PHASE_KEY)

    def test_unknown_and_lowercase_severities(self) -> None:
        _, findings = apply_prediction(_prediction("critical"))
        self.assertEqual(findings[0].severity, "QUESTION")
        phase, findings = apply_prediction(_prediction("major"))
        self.assertEqual(findings[0].severity, "MAJOR")
        self.assertEqual(phase.status, "FAIL")

    def test_labels_are_always_present(self) -> None:
        phase, findings = apply_prediction(_prediction("QUESTION"))
        self.assertEqual(phase.status, "PASS")
        self.assertTrue(findings[0].finding.startswith(ar.PREDICTION_PREFIX))
        self.assertTrue(phase.summary.startswith("Auto Review prediction"))
        self.assertIn("not Shipd's Auto Review verdict", phase.summary)

    def test_missing_prompt_is_a_question(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            findings = ar.local_contradictions(_write_repo(Path(tmp), prompt=False))
        self.assertEqual([f.severity for f in findings], ["QUESTION"])
        phase, _ = apply_prediction(None, local=findings, error="model down")
        self.assertEqual(phase.status, "SKIP")


class DossierTests(unittest.TestCase):
    def test_dossier_carries_draft_lineup_fingerprint_and_panel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _write_repo(Path(tmp))
            lineup = {"Vega": "Model V"}
            fingerprint = content_fingerprint(repo, agent_lineup=lineup)
            text = ar.build_auto_review_dossier(
                quest="olympus",
                repo_path=repo,
                phase_results=_draft()["phase_results"],
                findings=[],
                draft=_draft(),
                agent_lineup=lineup,
                fingerprint=fingerprint,
                platform_panel="Auto Review: not run",
            )
        self.assertIn("Draft review decision: approve", text)
        self.assertIn("Vega = Model V", text)
        self.assertIn("expected, unverified", text)
        self.assertIn(fingerprint["content_hash"][:16], text)
        self.assertIn("Auto Review: not run", text)
        self.assertIn("Add retries to the client.", text)


class _ScriptedModel:
    """Stands in for make_chat_model(...).with_structured_output(...)."""

    def __init__(self, replies: list) -> None:
        self.replies = list(replies)
        self.calls: list = []

    def invoke(self, messages, config=None):
        self.calls.append((messages, config))
        for callback in (config or {}).get("callbacks", []):
            run_id = object()
            callback.on_chat_model_start({}, [messages], run_id=run_id)
            message = AIMessage(content="", usage_metadata={"input_tokens": 5, "output_tokens": 5, "total_tokens": 10})
            callback.on_llm_end(LLMResult(generations=[[ChatGeneration(message=message)]]), run_id=run_id)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class ModelCompleterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = make_config(review_auto_review_model="claude-haiku-5-5")

    def test_uses_auto_review_role_and_retries_once(self) -> None:
        model = _ScriptedModel([ValueError("bad json"), _prediction("MAJOR")])
        with patch("review.llm.make_chat_model", return_value=model) as factory:
            raw = ar.model_completer(self.config)("dossier")
        self.assertEqual(factory.call_args.args[1], "auto_review")
        self.assertIs(factory.call_args.kwargs["structured_output"], AutoReviewPrediction)
        self.assertEqual(len(model.calls), 2)
        self.assertIn("failed validation", model.calls[1][0][1].content)
        self.assertEqual(raw.findings[0].severity, "MAJOR")

    def test_counts_against_the_shared_budget_with_its_own_calls(self) -> None:
        budget = ReviewBudget(1, 0, 100_000)
        for _ in range(budget.total_limit):
            budget._started.add(object())
        model = _ScriptedModel([_prediction("QUESTION")])
        with patch("review.llm.make_chat_model", return_value=model):
            ar.model_completer(self.config, budget=budget)("dossier")
        self.assertEqual(budget.tokens, 10)

    def test_budget_exhaustion_is_a_skip(self) -> None:
        budget = ReviewBudget(1, 0, 10)
        budget.tokens = 10
        model = _ScriptedModel([_prediction("MAJOR")])
        with patch("review.llm.make_chat_model", return_value=model):
            phase, findings = ar.run_auto_review_prediction(
                "dossier", complete=ar.model_completer(self.config, budget=budget)
            )
        self.assertEqual(phase.status, "SKIP")
        self.assertIn("budget", phase.summary)
        self.assertEqual(findings, [])

    def test_role_model_falls_back_to_finalize(self) -> None:
        self.assertEqual(llm.role_model(self.config, "auto_review"), "claude-haiku-5-5")
        fallback = make_config(review_auto_review_model="")
        self.assertEqual(llm.role_model(fallback, "auto_review"), fallback.review_model)
        self.assertEqual(llm._max_tokens(fallback, "auto_review"), fallback.review_finalize_max_output_tokens)


class AutoReviewNodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = _write_repo(Path(self._tmp.name))
        self.config = replace(get_review_config(), review_dry_run=False, review_auto_review=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _state(self, config=None, draft=None) -> dict:
        return {
            "config": config or self.config,
            "repo_path": str(self.repo),
            "quest": "olympus",
            "review_result": draft or _draft(),
            "scrape_context": {},
            "loc_info": {},
        }

    def _run(self, state: dict, prediction) -> dict:
        with patch.object(ar, "model_completer", return_value=lambda _packet: prediction):
            state = {**state, **g.auto_review_node(state)}
        return g.validate_node(state)["review_result"]

    def test_major_prediction_turns_approve_into_request_changes(self) -> None:
        out = self._run(self._state(), _prediction("MAJOR"))
        self.assertEqual(out["decision"], "request_changes")
        self.assertEqual(out["phase_results"]["auto_review"]["status"], "FAIL")
        self.assertTrue(any(f["phase"] == "auto_review" and f["severity"] == "MAJOR" for f in out["findings"]))
        self.assertIn("not Shipd's Auto Review verdict", out["contributor_feedback"])
        self.assertIs(out.get("review_complete"), True)

    def test_question_prediction_keeps_approve(self) -> None:
        out = self._run(self._state(), _prediction("QUESTION"))
        self.assertEqual(out["decision"], "approve")
        self.assertEqual(out["phase_results"]["auto_review"]["status"], "PASS")

    def test_predictions_survive_recorded_evidence_reconciliation(self) -> None:
        for severity in ("MAJOR", "QUESTION"):
            with self.subTest(severity=severity):
                state = self._state()
                state["review_evidence"] = {"phases": {
                    str(i): {"status": "PASS", "summary": "Verified", "findings": []}
                    for i in range(1, 7)
                }}
                state["phase_results"] = {"0": {"status": "PASS", "summary": "Mechanical checks passed"}}
                out = self._run(state, _prediction(severity))
                predictions = [f for f in out["findings"] if f["phase"] == "auto_review"]
                self.assertEqual(len(predictions), 1)
                self.assertEqual(predictions[0]["severity"], severity)
                self.assertIn("retries == 3", predictions[0]["evidence"])
                self.assertEqual(out["decision"], "request_changes" if severity == "MAJOR" else "approve")

    def test_disabled_dry_run_and_incomplete_skip_the_stage(self) -> None:
        disabled = replace(self.config, review_auto_review=False)
        dry = replace(self.config, review_dry_run=True)
        incomplete = {**_draft(), "review_complete": False, "review_error": "provider down"}
        for state in (
            self._state(config=disabled),
            self._state(config=dry),
            self._state(draft=incomplete),
        ):
            with patch.object(ar, "model_completer") as completer:
                self.assertEqual(g.auto_review_node(state), {})
            completer.assert_not_called()
        out = g.validate_node(self._state(config=disabled))["review_result"]
        self.assertEqual(out["decision"], "approve")
        self.assertNotIn("auto_review", out["phase_results"])

    def test_model_error_is_a_skip_not_a_coverage_gap(self) -> None:
        def boom(_packet: str):
            raise RuntimeError("model down")

        state = self._state()
        with patch.object(ar, "model_completer", return_value=boom):
            state = {**state, **g.auto_review_node(state)}
        out = g.validate_node(state)["review_result"]
        self.assertEqual(out["phase_results"]["auto_review"]["status"], "SKIP")
        self.assertEqual(out["decision"], "approve")
        self.assertNotIn("Coverage gap", out["recommendation_summary"])


class AutoReviewConfigTests(IsolatedEnvTestCase):
    def test_auto_review_model_defaults_to_finalize(self) -> None:
        config = self.load(ANTHROPIC_API_KEY=FAKE_KEY, REVIEW_MODEL="claude-opus-4-8")
        self.assertTrue(config.review_auto_review)
        self.assertEqual(config.review_auto_review_model, "claude-opus-4-8")
        self.assertEqual(config.review_auto_review_max_output_tokens, 4096)

    def test_auto_review_settings_from_env(self) -> None:
        config = self.load(
            ANTHROPIC_API_KEY=FAKE_KEY,
            REVIEW_AUTO_REVIEW="0",
            REVIEW_AUTO_REVIEW_MODEL="claude-haiku-5-5",
            REVIEW_AUTO_REVIEW_MAX_OUTPUT_TOKENS="2048",
        )
        self.assertFalse(config.review_auto_review)
        self.assertEqual(config.review_auto_review_model, "claude-haiku-5-5")
        self.assertEqual(config.review_auto_review_max_output_tokens, 2048)

    def test_foreign_auto_review_model_is_ignored(self) -> None:
        config = self.load(
            ANTHROPIC_API_KEY=FAKE_KEY,
            REVIEW_MODEL="claude-opus-4-8",
            REVIEW_AUTO_REVIEW_MODEL="gpt-6.1-sol",
        )
        self.assertEqual(config.review_auto_review_model, "claude-opus-4-8")

    def test_per_agent_models_override_defaults_and_json(self) -> None:
        default = self.load(ANTHROPIC_API_KEY=FAKE_KEY).expected_agent_models
        config = self.load(
            ANTHROPIC_API_KEY=FAKE_KEY,
            REVIEW_AGENT_MODELS='{"Vega": "json-vega", "Orion": "json-orion", "Nova": "json-nova"}',
            REVIEW_VEGA_MODEL="Opus 6 (xhigh)",
            REVIEW_NOVA_MODEL="Gemini 5",
        )
        self.assertEqual(
            config.expected_agent_models,
            {"Vega": "Opus 6 (xhigh)", "Orion": "json-orion", "Nova": "Gemini 5"},
        )
        repo = self.tmp / "clone"
        repo.mkdir()
        (repo / "test.patch").write_text(_TEST_PATCH, encoding="utf-8")
        before = content_fingerprint(repo, agent_lineup=default)
        after = content_fingerprint(repo, agent_lineup=config.expected_agent_models)
        self.assertEqual(before["content_hash"], after["content_hash"])
        self.assertNotEqual(before["agent_lineup"], after["agent_lineup"])


if __name__ == "__main__":
    unittest.main()
