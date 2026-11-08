"""Regression cases discovered by exercising the complete review workflow."""

import json
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from review.agent_runs_checks import parse_agent_run_metrics
from review.auto_review import (
    AutoReviewItem,
    AutoReviewPrediction,
    apply_prediction,
    build_auto_review_dossier,
    prediction_from_model,
    run_auto_review,
)
from review.fp_check import patch_is_false_pass
from review.harness_checks import (
    evaluate_dockerfile,
    evaluate_python_install,
    evaluate_test_harness,
)
from review.repo_checks import RepoMetadata, _slug, evaluate_repo_metadata, fetch_repo_metadata
from review.review_phases import _check_patch_apply, _validate_junit_xml
from review.staleness import content_fingerprint, staleness_note


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.mark.parametrize("replacement", ["changed\n", None])
def test_applied_patch_modifying_or_deleting_existing_file(tmp_path, replacement):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "Test")
    source = tmp_path / "source.txt"
    source.write_text("base\n")
    git(tmp_path, "add", "source.txt")
    git(tmp_path, "commit", "-m", "base")
    if replacement is None:
        source.unlink()
    else:
        source.write_text(replacement)
    (tmp_path / "test.patch").write_bytes(git(tmp_path, "diff").stdout)
    ok, detail = _check_patch_apply(tmp_path, "test.patch")
    assert ok, detail
    assert "reverse check passed" in detail


@pytest.mark.parametrize("content", [
    "testsuite crashed before producing XML", "<testsuite>",
    '<html><p>testsuite</p></html>', '<testsuites><testsuite></testsuites>',
])
def test_junit_requires_a_valid_xml_document(tmp_path, content):
    path = tmp_path / "result.xml"
    path.write_text(content)
    ok, _ = _validate_junit_xml(path)
    assert not ok


def test_grouped_hinted_run_does_not_count_as_a_solve():
    metrics = parse_agent_run_metrics("""Vega
Run 1: PASS hinted
Run 2: FAIL
Run 3: FAIL
Run 4: FAIL
Run 5: FAIL
Nova: 0/4 pass
Orion: 0/4 pass
Nova average sequence length: 450000
""")
    assert metrics["per_agent"]["Vega"] == {"runs": 4, "passes": 0}
    assert metrics["hints_marked"]


def test_hinted_run_continuation_does_not_inflate_nova_sequence_length():
    metrics = parse_agent_run_metrics("""Nova
Run 1: FAIL hinted
Sequence length: 900000
Run 2: FAIL unhinted
Sequence length: 300000
""")
    assert metrics["nova_sequence_average"] == 300000


def test_hinted_runs_are_excluded_from_the_overall_pass_rate():
    metrics = parse_agent_run_metrics("""1/13 passed
Vega: 1/5 pass
Vega run 1: PASS hinted
Vega run 2: FAIL
Vega run 3: FAIL
Vega run 4: FAIL
Vega run 5: FAIL
Nova: 0/4 pass
Orion: 0/4 pass
""")
    assert metrics["passes"] == 0
    assert metrics["total_runs"] == 12
    assert metrics["pass_rate_pct"] == 0


def test_sequence_length_on_hinted_summary_is_not_used():
    metrics = parse_agent_run_metrics("Nova hinted average sequence length: 900000")
    assert metrics["nova_sequence_average"] is None


def test_patch_contents_are_not_agent_run_metadata():
    metrics = parse_agent_run_metrics("""Vega run 1: PASS
```diff
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1 @@
-old
+message = 'Nova PASS unfair task verifier error cheat'
```
Nova: 0/4 pass
Orion: 0/4 pass
""")
    assert not metrics["unfair_runs"]
    assert not metrics["blocker_runs"]
    assert not metrics["cheat_labeled"]
    assert len(metrics["passing_patches"]) == 1


GOOD_DOCKERFILE = """FROM python:3.12 AS base
WORKDIR /app
RUN useradd -m -u 1000 model
USER model
ENTRYPOINT ["bash"]
"""


@pytest.mark.parametrize("install", [
    "RUN pip install -r requirements.txt",
    "RUN pip install --requirement requirements.txt",
    "RUN pip install -e .",
    "RUN pip install --index-url https://pypi.org/simple requests==2.32.0",
])
def test_dependency_files_options_and_local_projects_are_not_unpinned_packages(install):
    findings = evaluate_dockerfile(GOOD_DOCKERFILE + install + "\n")
    assert not any("unpinned package" in finding.finding for finding in findings)


@pytest.mark.parametrize("dockerfile", [
    GOOD_DOCKERFILE.replace("FROM python:3.12", "FROM --platform=linux/amd64 python:3.12"),
    GOOD_DOCKERFILE + "FROM base AS final\n",
    GOOD_DOCKERFILE.replace("python:3.12", "python@sha256:" + "a" * 64),
])
def test_supported_pinned_docker_from_forms(dockerfile):
    assert evaluate_dockerfile(dockerfile) == []


def test_multiline_build_commands_are_checked():
    findings = evaluate_dockerfile(GOOD_DOCKERFILE + "RUN echo ready && \\\n+    pytest\n")
    assert any("runs tests during the build" in finding.finding for finding in findings)


@pytest.mark.parametrize("install", ["RUN pip install .\n", "RUN python -m pip install .\n"])
def test_noneditable_local_python_installs_are_detected(install):
    assert evaluate_python_install(install, "")
    assert not evaluate_python_install(install, "Run the tests with pytest.")


def test_removing_bad_test_settings_does_not_create_new_findings():
    findings = evaluate_test_harness(
        '#!/bin/sh\ncase "$1" in base|new) pytest ;; esac\n',
        executable=True,
        test_patch='--- a/tests/test_app.py\n+++ b/tests/test_app.py\n@@ -1 +1 @@\n-# shipd --fail-fast --tb=no\n+assert result == expected\n',
        solution_patch="",
    )
    assert findings == []


def test_executable_file_check_is_not_pytest_fail_fast():
    findings = evaluate_test_harness(
        '#!/bin/sh\n[ -x ./runner ] || exit 1\ncase "$1" in base|new) ./runner ;; esac\n',
        executable=True, test_patch="", solution_patch="",
    )
    assert not any("fail-fast" in finding.finding for finding in findings)


@pytest.mark.parametrize("patch", [
    'diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +0,0 @@\n-raise RuntimeError("bug")\n',
    'diff --git a/src/inspector.py b/src/inspector.py\n--- a/src/inspector.py\n+++ b/src/inspector.py\n@@ -1 +1 @@\n-return 0\n+return 1\n',
    'diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-assert value > 0\n+assert value >= 0\n',
])
def test_legitimate_production_edits_are_not_false_passes(patch):
    assert patch_is_false_pass(patch) is None


@pytest.mark.parametrize("value", [[], None, "invalid", 7])
def test_malformed_previous_bundle_cannot_crash_review(tmp_path, value):
    path = tmp_path / "previous.json"
    path.write_text(json.dumps(value))
    assert staleness_note(tmp_path, content_fingerprint(tmp_path), previous_path=path) == ""


def test_bundle_without_clone_identity_is_not_used_for_staleness(tmp_path):
    path = tmp_path / "previous.json"
    path.write_text(json.dumps({"review": {"submission_fingerprint": {"content_hash": "other"}}}))
    assert staleness_note(tmp_path, content_fingerprint(tmp_path), previous_path=path) == ""


@pytest.mark.parametrize("origin", [
    "https://notgithub.com/org/repo.git", "https://example.com/github.com/org/repo",
    "https://github.com.evil.example/org/repo", "https://github.com/org/repo/extra",
])
def test_non_github_remotes_are_not_queried_as_github_repositories(origin):
    assert _slug(origin) == ""


@pytest.mark.parametrize("date", ["not-a-date", "2026-06-01T00:00:00"])
def test_repo_dates_cannot_crash_or_silently_pass_without_verification(date):
    phase, _ = evaluate_repo_metadata(
        RepoMetadata(stars=800, pushed_at=date, language="Python", license="MIT"),
        now=datetime(2026, 11, 8, tzinfo=timezone.utc),
    )
    assert phase.status == ("SKIP" if date == "not-a-date" else "PASS")


def test_unexpected_github_json_skips_instead_of_crashing(monkeypatch):
    monkeypatch.setattr("review.repo_checks.subprocess.run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="[]", stderr="",
    ))
    assert fetch_repo_metadata("example/repo").error


def test_auto_review_reads_artifact_tails_and_finding_evidence(tmp_path):
    (tmp_path / "problem.md").write_text("Context. " * 600 + "Required tail behavior.")
    (tmp_path / "test.patch").write_text("# context\n" * 600 + "+assert tail_behavior()\n")
    dossier = build_auto_review_dossier(
        quest="olympus", repo_path=tmp_path, phase_results={},
        findings=[{"phase": "3", "severity": "MAJOR", "finding": "Mismatch", "evidence": "tests/test_tail.py:42", "suggested_fix": "Align the contract."}],
    )
    assert "Required tail behavior." in dossier
    assert "+assert tail_behavior()" in dossier
    assert "tests/test_tail.py:42" in dossier


def test_oversized_auto_review_skips_before_calling_model(tmp_path):
    (tmp_path / "problem.md").write_text("x" * 10000)
    def complete(_packet):
        pytest.fail("An oversized dossier must not be sent to the model")
    phase, _ = run_auto_review(
        config=SimpleNamespace(review_finalize_payload_max_chars=5000),
        repo_path=tmp_path, draft={}, complete=complete,
    )
    assert phase.status == "SKIP"
    assert "budget" in phase.summary.lower()


def test_empty_structured_auto_review_response_is_not_a_success():
    assert prediction_from_model({}) is None


def test_auto_review_cannot_block_approval_with_blank_evidence():
    phase, findings = apply_prediction(AutoReviewPrediction(findings=[
        AutoReviewItem(severity="MAJOR", finding="Maybe inconsistent", evidence="  "),
    ]))
    assert phase.status == "PASS"
    assert findings[0].severity == "QUESTION"
