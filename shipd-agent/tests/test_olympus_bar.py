# False-positive, Auto Review prediction, repo eligibility, and staleness.

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from review.auto_review import (
    AutoReviewItem,
    AutoReviewPrediction,
    local_contradictions,
    run_auto_review_prediction,
)
from review.fp_check import evaluate_false_positives
from review.repo_checks import RepoMetadata, evaluate_repo, evaluate_repo_metadata
from review.staleness import content_fingerprint, staleness_note


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


_SOLUTION_PATCH = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1 @@
-broken
+fixed
"""

_TEST_ONLY_PATCH = """diff --git a/tests/test_app.py b/tests/test_app.py
--- a/tests/test_app.py
+++ b/tests/test_app.py
@@ -1 +1 @@
-assert False
+pytest.skip("skip the check")
"""


class FalsePositiveTests(unittest.TestCase):
    def test_no_patch_is_a_skip(self) -> None:
        phase, findings = evaluate_false_positives(
            {"available": True, "raw_text": "Vega: 1/4 pass\n", "metrics": {}}
        )
        self.assertEqual(phase.status, "SKIP")
        self.assertEqual(findings, [])
        self.assertIn("no passing-run patch", phase.summary)

    def test_a_real_solution_passes(self) -> None:
        phase, findings = evaluate_false_positives(
            {"metrics": {"passing_patches": [{"agent": "Vega", "patch": _SOLUTION_PATCH}]}}
        )
        self.assertEqual(phase.status, "PASS")
        self.assertEqual(findings, [])
        self.assertIn("not Shipd's FP service", phase.summary)

    def test_a_test_only_pass_fails(self) -> None:
        text = "Vega run 1: PASS\n```diff\n" + _TEST_ONLY_PATCH + "\n```"
        phase, findings = evaluate_false_positives({"raw_text": text, "metrics": {}})
        self.assertEqual(phase.status, "FAIL")
        self.assertEqual(findings[0].finding, "False positive in a passing agent run")
        self.assertEqual(findings[0].severity, "MAJOR")


class AutoReviewPredictionTests(unittest.TestCase):
    def test_missing_solution_is_a_major_prediction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "test.patch").write_text("+++ b/tests/test_app.py\n+assert True\n", encoding="utf-8")
            findings = local_contradictions(repo)
            phase, merged = run_auto_review_prediction("dossier", complete=None, local=findings)
        self.assertEqual(phase.status, "FAIL")
        self.assertTrue(merged[0].finding.startswith("Auto Review prediction:"))
        self.assertEqual(merged[0].severity, "MAJOR")

    def test_question_does_not_fail_and_major_does(self) -> None:
        question = AutoReviewPrediction(
            summary="Looks aligned.",
            findings=[AutoReviewItem(severity="QUESTION", finding="Shipd might flag the tone", evidence="speculative")],
        )
        phase, findings = run_auto_review_prediction("dossier", complete=lambda _packet: question)
        self.assertEqual(phase.status, "PASS")
        self.assertEqual(findings[0].severity, "QUESTION")
        self.assertTrue(findings[0].finding.startswith("Auto Review prediction:"))
        self.assertIn("not Shipd's Auto Review verdict", phase.summary)

        major = AutoReviewPrediction(
            summary="The tests demand a field the prompt never mentions.",
            findings=[
                AutoReviewItem(
                    severity="MAJOR",
                    finding="prompt and tests disagree",
                    evidence="test.patch asserts retry; the prompt does not mention retry.",
                )
            ],
        )
        phase, findings = run_auto_review_prediction("dossier", complete=lambda _packet: major)
        self.assertEqual(phase.status, "FAIL")
        self.assertEqual(findings[0].severity, "MAJOR")

    def test_model_errors_skip_when_there_is_no_local_contradiction(self) -> None:
        def boom(_packet: str):
            raise RuntimeError("model down")

        phase, findings = run_auto_review_prediction("dossier", complete=boom)
        self.assertEqual(phase.status, "SKIP")
        self.assertEqual(findings, [])
        self.assertIn("model down", phase.summary)

        phase, findings = run_auto_review_prediction("dossier", complete=lambda _packet: "not a prediction")
        self.assertEqual(phase.status, "SKIP")
        self.assertIn("unexpected shape", phase.summary)


class RepoEligibilityTests(unittest.TestCase):
    def test_offline_or_no_origin_skips_and_states_the_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            phase, findings = evaluate_repo(repo)
        self.assertEqual(phase.status, "SKIP")
        self.assertEqual(findings, [])
        self.assertIn("12", phase.summary)
        self.assertIn("50", phase.summary)

    def test_low_stars_fail_and_a_healthy_repo_passes(self) -> None:
        low, findings = evaluate_repo_metadata(
            RepoMetadata(stars=12, pushed_at="2026-06-01T00:00:00Z", language="Python", license="MIT")
        )
        self.assertEqual(low.status, "FAIL")
        self.assertIn("fewer than 500 stars", findings[0].finding)
        self.assertIn("does not count other people's submissions", findings[0].evidence)

        ok, findings = evaluate_repo_metadata(
            RepoMetadata(stars=800, pushed_at="2026-06-01T00:00:00Z", language="Python", license="Apache-2.0")
        )
        self.assertEqual(ok.status, "PASS")
        self.assertEqual(findings, [])

    def test_fetch_is_used_for_a_github_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _git(repo, "init")
            _git(repo, "remote", "add", "origin", "https://github.com/example/widget.git")
            seen: list[str] = []

            def fetch(slug: str) -> RepoMetadata:
                seen.append(slug)
                return RepoMetadata(error="offline")

            phase, _findings = evaluate_repo(repo, fetch=fetch)
        self.assertEqual(seen, ["example/widget"])
        self.assertEqual(phase.status, "SKIP")
        self.assertIn("offline", phase.summary)


class StalenessTests(unittest.TestCase):
    def test_hash_changes_and_lineup_changes_are_noted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "problem.md").write_text("first\n", encoding="utf-8")
            current = content_fingerprint(repo, agent_lineup={"Vega": "Opus"})
            previous = root / "pending.json"
            previous.write_text(
                json.dumps(
                    {
                        "repo_path": str(repo),
                        "review": {"submission_fingerprint": current},
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(staleness_note(repo, current, previous_path=previous), "")

            (repo / "problem.md").write_text("second\n", encoding="utf-8")
            changed = content_fingerprint(repo, agent_lineup={"Vega": "Opus"})
            note = staleness_note(repo, changed, previous_path=previous)
            self.assertIn("Content changed", note)

            swapped = content_fingerprint(repo, agent_lineup={"Vega": "Other"})
            note = staleness_note(repo, swapped, previous_path=previous)
            self.assertIn("agent line-up changed", note)

            other = root / "other"
            other.mkdir()
            self.assertEqual(staleness_note(other, changed, previous_path=previous), "")


if __name__ == "__main__":
    unittest.main()
