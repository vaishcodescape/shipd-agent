# Tests for Phase 0 patch apply checks and test.sh contract.

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from review.review_phases import (
    _check_patch_apply,
    _validate_junit_xml,
    run_phase0,
    run_phase0_fast,
    run_phase0_tests,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o755)


def _minimal_junit(path: Path, *, failures: int = 0) -> None:
    path.write_text(
        '<?xml version="1.0"?><testsuite name="t" tests="1" failures="{f}"/>'.format(
            f=failures
        ),
        encoding="utf-8",
    )


class PatchApplyCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        _git(self.repo, "init")
        _git(self.repo, "config", "user.email", "test@example.com")
        _git(self.repo, "config", "user.name", "Test")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        _git(self.repo, "add", "README.md")
        _git(self.repo, "commit", "-m", "base")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_forward_apply_on_clean_tree(self) -> None:
        patch = self.repo / "test.patch"
        patch.write_text(
            "--- /dev/null\n+++ b/new_file.txt\n@@ -0,0 +1 @@\n+added\n",
            encoding="utf-8",
        )
        ok, msg = _check_patch_apply(self.repo, "test.patch")
        self.assertTrue(ok)
        self.assertIn("git apply --check OK", msg)

    def test_already_applied_patch_passes_reverse_check(self) -> None:
        patch = self.repo / "test.patch"
        patch.write_text(
            "--- /dev/null\n+++ b/conftest.py\n@@ -0,0 +1 @@\n+# test\n",
            encoding="utf-8",
        )
        _git(self.repo, "apply", "test.patch")

        ok, msg = _check_patch_apply(self.repo, "test.patch")
        self.assertTrue(ok)
        self.assertIn("reverse check passed", msg)

    def test_genuine_apply_failure_still_fails(self) -> None:
        patch = self.repo / "test.patch"
        patch.write_text(
            "--- README.md\n+++ README.md\n@@ -1 +1 @@\n-base\n+changed\n",
            encoding="utf-8",
        )
        (self.repo / "README.md").write_text("different base\n", encoding="utf-8")

        ok, msg = _check_patch_apply(self.repo, "test.patch")
        self.assertFalse(ok)
        self.assertIn("FAILED", msg)

    def test_run_phase0_passes_when_quick_setup_applied_test_patch(self) -> None:
        patch = self.repo / "test.patch"
        patch.write_text(
            "--- /dev/null\n+++ b/test.sh\n@@ -0,0 +1 @@\n+#!/bin/sh\n",
            encoding="utf-8",
        )
        solution = self.repo / "solution.patch"
        solution.write_text(
            "--- /dev/null\n+++ b/solution.txt\n@@ -0,0 +1 @@\n+done\n",
            encoding="utf-8",
        )
        (self.repo / "Dockerfile").write_text("FROM ubuntu\n", encoding="utf-8")
        _git(self.repo, "apply", "test.patch")
        _git(self.repo, "apply", "solution.patch")

        result = run_phase0_fast(self.repo)
        self.assertFalse(result.critical_fail, msg=result.phase0_log)
        self.assertEqual(result.status, "PASS")
        patch_findings = [
            f for f in result.findings if "does not apply cleanly" in f.finding
        ]
        self.assertEqual(patch_findings, [])


class JUnitValidationTests(unittest.TestCase):
    def test_valid_testsuite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.xml"
            _minimal_junit(path)
            ok, msg = _validate_junit_xml(path)
            self.assertTrue(ok)
            self.assertIn("JUnit XML OK", msg)

    def test_missing_file_fails(self) -> None:
        ok, msg = _validate_junit_xml(Path("/nonexistent/out.xml"))
        self.assertFalse(ok)
        self.assertIn("not produced", msg)


class Phase0TestRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        _git(self.repo, "init")
        _git(self.repo, "config", "user.email", "test@example.com")
        _git(self.repo, "config", "user.name", "Test")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        _git(self.repo, "add", "README.md")
        _git(self.repo, "commit", "-m", "base")

        self.test_sh = self.repo / "test.sh"
        _write_executable(
            self.test_sh,
            """#!/bin/bash
set -e
OUTPUT=""
MODE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output_path) OUTPUT="$2"; shift 2 ;;
    base|new) MODE="$1"; shift ;;
    *) shift ;;
  esac
done
if [[ -f .solution_applied ]]; then
  FAIL_NEW=0
else
  FAIL_NEW=1
fi
if [[ "$MODE" == "new" && "$FAIL_NEW" -eq 1 ]]; then
  echo '<?xml version="1.0"?><testsuite name="new" tests="1" failures="1"/>' > "$OUTPUT"
  exit 1
fi
echo '<?xml version="1.0"?><testsuite name="t" tests="1" failures="0"/>' > "$OUTPUT"
exit 0
""",
        )

        self.test_patch = self.repo / "test.patch"
        self.test_patch.write_text(
            "--- /dev/null\n+++ b/tests_new.txt\n@@ -0,0 +1 @@\n+new test marker\n",
            encoding="utf-8",
        )
        self.solution_patch = self.repo / "solution.patch"
        self.solution_patch.write_text(
            "--- /dev/null\n+++ b/.solution_applied\n@@ -0,0 +1 @@\n+1\n",
            encoding="utf-8",
        )
        (self.repo / "Dockerfile").write_text("FROM ubuntu\n", encoding="utf-8")
        _git(self.repo, "apply", "test.patch")

        self.artifacts = {
            "test.sh": "test.sh",
            "test.patch": "test.patch",
            "solution.patch": "solution.patch",
            "Dockerfile": "Dockerfile",
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_run_phase0_tests_passes_contract(self) -> None:
        log, findings, critical = run_phase0_tests(
            self.repo,
            artifacts=self.artifacts,
            timeout=30,
            force_host_tests=True,
        )
        self.assertFalse(critical)
        self.assertEqual(findings, [])
        self.assertIn("Without solution", log)
        self.assertIn("With solution", log)
        self.assertIn("Phase 0 test contract: PASS", log)
        self.assertFalse((self.repo / ".solution_applied").exists())

    def test_run_phase0_tests_fails_when_new_passes_without_solution(self) -> None:
        _write_executable(
            self.test_sh,
            """#!/bin/bash
OUTPUT=""
MODE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output_path) OUTPUT="$2"; shift 2 ;;
    base|new) MODE="$1"; shift ;;
    *) shift ;;
  esac
done
echo '<?xml version="1.0"?><testsuite name="t" tests="1" failures="0"/>' > "$OUTPUT"
exit 0
""",
        )
        _, findings, critical = run_phase0_tests(
            self.repo,
            artifacts=self.artifacts,
            timeout=30,
            force_host_tests=True,
        )
        self.assertTrue(critical)
        self.assertTrue(
            any("new PASS without solution" in f.finding for f in findings)
        )

    def test_run_phase0_full_integrates_tests(self) -> None:
        result = run_phase0(self.repo, run_tests=True, test_timeout=30, force_host_tests=True)
        self.assertEqual(result.status, "PASS")
        self.assertIn("test.sh runs", result.phase0_log)
        self.assertIn("Docker test.sh base/new contract verified", result.summary)

    def test_run_phase0_fast_skips_tests(self) -> None:
        result = run_phase0(self.repo, run_tests=False)
        self.assertIn("tests not run", result.summary)
        self.assertNotIn("=== test.sh runs", result.phase0_log)


if __name__ == "__main__":
    unittest.main()
