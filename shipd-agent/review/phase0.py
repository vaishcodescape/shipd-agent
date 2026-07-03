# Phase 0 — mechanical artifact, patch, and test.sh verification.

from __future__ import annotations

import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from review.activity import log_activity
from review.context import discover_artifacts, get_git_commit, resolve_repo_path
from review.docker_tests import (
    DEFAULT_BUILD_TIMEOUT_SEC,
    TestRunner,
    create_test_runner,
)
from review.schemas import Finding, PhaseResult

DEFAULT_TEST_TIMEOUT_SEC = 600


@dataclass
class Phase0Result:
    status: str
    summary: str
    phase0_log: str
    findings: list[Finding] = field(default_factory=list)
    critical_fail: bool = False
    commit: str | None = None
    artifacts: dict[str, str | None] = field(default_factory=dict)


def _run_git(args: list[str], repo_path: Path) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return 1, str(exc)
    output = (result.stdout + result.stderr).strip()
    return result.returncode, output


def _check_patch_apply(repo_path: Path, rel_path: str) -> tuple[bool, str]:
    """Verify a patch applies to the repo.

    Quick Setup leaves patches applied on the working tree while HEAD stays at
    the base commit. Forward ``git apply --check`` then fails with "already
    exists"; accept that when reverse apply --check succeeds on the same tree.
    """
    patch_path = repo_path / rel_path
    if not patch_path.is_file():
        return False, f"{rel_path}: file not found"
    code, output = _run_git(["apply", "--check", str(patch_path)], repo_path)
    if code == 0:
        return True, f"{rel_path}: git apply --check OK"
    if "already exists" in output.lower():
        rev_code, rev_output = _run_git(
            ["apply", "--check", "--reverse", str(patch_path)],
            repo_path,
        )
        if rev_code == 0:
            return (
                True,
                f"{rel_path}: git apply --check OK "
                f"(patch already applied; reverse check passed)",
            )
        output = f"{output}\nreverse apply --check FAILED\n{rev_output}"
    return False, f"{rel_path}: git apply --check FAILED\n{output}"


def check_patch_apply(repo_path: Path, rel_path: str) -> tuple[bool, str]:
    """Public wrapper for git apply --check on a patch relative path."""
    return _check_patch_apply(repo_path, rel_path)


def _patch_file_nonempty(repo_path: Path, rel_path: str | None) -> bool:
    if not rel_path:
        return False
    path = repo_path / rel_path
    return path.is_file() and path.stat().st_size > 0


def _is_patch_applied(repo_path: Path, rel_path: str) -> bool:
    patch_path = repo_path / rel_path
    if not patch_path.is_file() or patch_path.stat().st_size == 0:
        return False
    code, _ = _run_git(["apply", "--check", "--reverse", str(patch_path)], repo_path)
    return code == 0


def _apply_patch(repo_path: Path, rel_path: str) -> tuple[bool, str]:
    if _is_patch_applied(repo_path, rel_path):
        return True, f"{rel_path}: already applied"
    patch_path = repo_path / rel_path
    code, output = _run_git(["apply", str(patch_path)], repo_path)
    if code == 0:
        return True, f"{rel_path}: applied"
    return False, f"{rel_path}: apply FAILED\n{output}"


def _reverse_patch(repo_path: Path, rel_path: str) -> tuple[bool, str]:
    if not _is_patch_applied(repo_path, rel_path):
        return True, f"{rel_path}: not applied"
    patch_path = repo_path / rel_path
    code, output = _run_git(["apply", "--reverse", str(patch_path)], repo_path)
    if code == 0:
        return True, f"{rel_path}: reversed"
    return False, f"{rel_path}: reverse FAILED\n{output}"


def _validate_junit_xml(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, f"JUnit XML not produced: {path}"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, f"Cannot read JUnit XML {path}: {exc}"
    lowered = text.lower()
    if "testsuite" not in lowered and "testsuites" not in lowered:
        return False, f"Invalid JUnit XML (no testsuite element): {path}"
    return True, f"JUnit XML OK: {path}"


def _run_test_sh(
    repo_path: Path,
    test_sh_rel: str,
    mode: str,
    output_path: Path,
    *,
    timeout: int,
) -> tuple[int, str]:
    test_sh = repo_path / test_sh_rel
    if not test_sh.is_file():
        return 127, f"test.sh not found at {test_sh_rel}"
    if not test_sh.stat().st_mode & 0o111:
        test_sh.chmod(test_sh.stat().st_mode | 0o755)
    try:
        result = subprocess.run(
            ["bash", str(test_sh), "--output_path", str(output_path), mode],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, f"test.sh {mode} timed out after {timeout}s"
    except FileNotFoundError as exc:
        return 127, str(exc)
    output = (result.stdout + result.stderr).strip()
    header = f"./test.sh --output_path {output_path} {mode} → exit {result.returncode}"
    return result.returncode, f"{header}\n{output}" if output else header


def _restore_patch_state(
    repo_path: Path,
    *,
    test_rel: str | None,
    solution_rel: str | None,
    test_was_applied: bool,
    solution_was_applied: bool,
) -> list[str]:
    """Return repo patch state to the pre-test baseline."""
    lines: list[str] = []
    if solution_rel and _patch_file_nonempty(repo_path, solution_rel):
        currently = _is_patch_applied(repo_path, solution_rel)
        if solution_was_applied and not currently:
            ok, msg = _apply_patch(repo_path, solution_rel)
            lines.append(f"  restore solution: {msg}")
        elif not solution_was_applied and currently:
            ok, msg = _reverse_patch(repo_path, solution_rel)
            lines.append(f"  restore solution: {msg}")
    if test_rel and _patch_file_nonempty(repo_path, test_rel):
        currently = _is_patch_applied(repo_path, test_rel)
        if test_was_applied and not currently:
            ok, msg = _apply_patch(repo_path, test_rel)
            lines.append(f"  restore test.patch: {msg}")
        elif not test_was_applied and currently:
            ok, msg = _reverse_patch(repo_path, test_rel)
            lines.append(f"  restore test.patch: {msg}")
    return lines


def run_phase0_tests(
    repo_path: Path,
    *,
    artifacts: dict[str, str | None],
    timeout: int = DEFAULT_TEST_TIMEOUT_SEC,
    build_timeout: int = DEFAULT_BUILD_TIMEOUT_SEC,
    test_runner: TestRunner | None = None,
    force_host_tests: bool = False,
) -> tuple[str, list[Finding], bool]:
    """
    Run rubric Phase 0 test.sh checks inside Docker (--network none).

    base/new without solution, then with solution. Falls back to explicit
    ``test_runner`` or ``force_host_tests`` for unit tests only.

    Returns (log_section, findings, critical_fail).
    """
    repo_path = resolve_repo_path(repo_path)
    log_lines = ["=== test.sh runs (Phase 0 — Docker, --network none) ==="]
    findings: list[Finding] = []
    critical_fail = False

    test_sh_rel = artifacts.get("test.sh")
    test_rel = artifacts.get("test.patch")
    solution_rel = artifacts.get("solution.patch")
    dockerfile_rel = artifacts.get("Dockerfile")

    if test_runner is None:
        if force_host_tests:
            from review.docker_tests import HostTestRunner

            test_runner = HostTestRunner(repo_path)
            log_lines.append(
                "  (host test.sh — force_host_tests; unit tests only, not rubric-compliant)"
            )
        else:
            runner, discovery_log = create_test_runner(
                repo_path,
                dockerfile_rel=dockerfile_rel,
                build_timeout=build_timeout,
                run_timeout=timeout,
            )
            log_lines.append(discovery_log)
            if runner is None:
                critical_fail = True
                if not dockerfile_rel:
                    msg = "Dockerfile artifact missing — Phase 0 requires containerized tests"
                    log_lines.append(f"  FAIL — {msg}")
                    findings.append(
                        Finding(
                            phase="0",
                            severity="BLOCKER",
                            finding="Missing Dockerfile for Phase 0 Docker verification",
                            evidence=discovery_log,
                            suggested_fix=(
                                "Add a minimal Dockerfile with /bin/bash entrypoint; "
                                "tests must run inside the container with --network none."
                            ),
                        )
                    )
                else:
                    msg = "Docker harness unavailable (CLI missing or compose/Dockerfile not found)"
                    log_lines.append(f"  FAIL — {msg}")
                    findings.append(
                        Finding(
                            phase="0",
                            severity="BLOCKER",
                            finding="Cannot run Phase 0 tests in Docker",
                            evidence=discovery_log,
                            suggested_fix="Ensure Docker is installed and Dockerfile builds successfully.",
                        )
                    )
                return "\n".join(log_lines), findings, critical_fail
            test_runner = runner

    if not test_sh_rel:
        log_lines.append("  SKIPPED — test.sh artifact missing")
        return "\n".join(log_lines), findings, critical_fail

    if not test_rel:
        log_lines.append("  SKIPPED — test.patch missing (cannot establish harness)")
        findings.append(
            Finding(
                phase="0",
                severity="BLOCKER",
                finding="Cannot run Phase 0 tests without test.patch",
                evidence="test.patch artifact missing",
                suggested_fix="Add test.patch to the submission.",
            )
        )
        return "\n".join(log_lines), findings, True

    test_was_applied = _is_patch_applied(repo_path, test_rel)
    solution_was_applied = (
        _is_patch_applied(repo_path, solution_rel) if solution_rel else False
    )
    has_solution = _patch_file_nonempty(repo_path, solution_rel)

    log_activity(
        "test contract: applying patches for base/new runs "
        f"(test.patch applied={test_was_applied}, "
        f"solution applied={solution_was_applied})",
        category="phase0",
    )

    try:
        if not test_was_applied:
            ok, msg = _apply_patch(repo_path, test_rel)
            log_activity(f"apply test.patch: {msg}", category="phase0")
            log_lines.append(f"  prep: {msg}")
            if not ok:
                findings.append(
                    Finding(
                        phase="0",
                        severity="BLOCKER",
                        finding="test.patch could not be applied for test runs",
                        evidence=msg,
                        suggested_fix="Ensure test.patch applies to the base commit.",
                    )
                )
                return "\n".join(log_lines), findings, True

        if has_solution and solution_was_applied:
            ok, msg = _reverse_patch(repo_path, solution_rel)
            log_lines.append(f"  prep (without solution): {msg}")
            if not ok:
                findings.append(
                    Finding(
                        phase="0",
                        severity="BLOCKER",
                        finding="Could not reverse solution.patch for baseline test run",
                        evidence=msg,
                        suggested_fix="Reset working tree to test.patch only before review.",
                    )
                )
                return "\n".join(log_lines), findings, True

        from review.docker_tests import DockerTestRunner

        if isinstance(test_runner, DockerTestRunner):
            built, build_log = test_runner.ensure_built()
            log_lines.append(build_log)
            if not built:
                critical_fail = True
                findings.append(
                    Finding(
                        phase="0",
                        severity="BLOCKER",
                        finding="Docker build failed for Phase 0 verification",
                        evidence=build_log[:2000],
                        suggested_fix=(
                            "Fix Dockerfile so the image builds cleanly; "
                            "dependencies must install at build time."
                        ),
                    )
                )
                return "\n".join(log_lines), findings, critical_fail

        with tempfile.TemporaryDirectory(prefix="shipd-phase0-") as tmp:
            tmp_path = Path(tmp)

            log_lines.append("\n--- Without solution ---")
            base_xml = tmp_path / "base.xml"
            new_xml = tmp_path / "new.xml"

            base_code, base_out = test_runner.run_test_sh(
                test_sh_rel, "base", base_xml, timeout=timeout
            )
            log_lines.append(base_out)
            base_xml_ok, base_xml_msg = _validate_junit_xml(base_xml)
            log_lines.append(f"  {base_xml_msg}")

            if base_code != 0:
                critical_fail = True
                findings.append(
                    Finding(
                        phase="0",
                        severity="BLOCKER",
                        finding="test.sh base FAIL without solution (expected PASS)",
                        evidence=base_out[:2000],
                        suggested_fix="Fix base test harness or repo regression.",
                    )
                )
            elif not base_xml_ok:
                critical_fail = True
                findings.append(
                    Finding(
                        phase="0",
                        severity="BLOCKER",
                        finding="test.sh base did not produce valid JUnit XML",
                        evidence=base_xml_msg,
                        suggested_fix="Ensure test.sh writes valid JUnit XML to --output_path.",
                    )
                )

            new_code, new_out = test_runner.run_test_sh(
                test_sh_rel, "new", new_xml, timeout=timeout
            )
            log_lines.append(new_out)
            new_xml_ok, new_xml_msg = _validate_junit_xml(new_xml)
            log_lines.append(f"  {new_xml_msg}")

            if new_code == 0:
                critical_fail = True
                findings.append(
                    Finding(
                        phase="0",
                        severity="BLOCKER",
                        finding="test.sh new PASS without solution (expected FAIL)",
                        evidence=new_out[:2000],
                        suggested_fix=(
                            "New tests must fail before the solution is applied "
                            "(missing behaviour, not harness breakage)."
                        ),
                    )
                )
            elif not new_xml_ok and new_code != 0:
                findings.append(
                    Finding(
                        phase="0",
                        severity="MAJOR",
                        finding="test.sh new run did not produce valid JUnit XML",
                        evidence=new_xml_msg,
                        suggested_fix="Ensure failures are surfaced in JUnit XML output.",
                    )
                )

            if has_solution:
                log_lines.append("\n--- With solution ---")
                ok, msg = _apply_patch(repo_path, solution_rel)
                log_activity(f"apply solution.patch: {msg}", category="phase0")
                log_lines.append(f"  prep: {msg}")
                if not ok:
                    critical_fail = True
                    findings.append(
                        Finding(
                            phase="0",
                            severity="BLOCKER",
                            finding="solution.patch could not be applied for test runs",
                            evidence=msg,
                            suggested_fix="Ensure solution.patch applies after test.patch.",
                        )
                    )
                else:
                    sol_base_xml = tmp_path / "sol_base.xml"
                    sol_new_xml = tmp_path / "sol_new.xml"

                    for mode, xml_path in (("base", sol_base_xml), ("new", sol_new_xml)):
                        code, out = test_runner.run_test_sh(
                            test_sh_rel, mode, xml_path, timeout=timeout
                        )
                        log_lines.append(out)
                        xml_ok, xml_msg = _validate_junit_xml(xml_path)
                        log_lines.append(f"  {xml_msg}")
                        if code != 0:
                            critical_fail = True
                            findings.append(
                                Finding(
                                    phase="0",
                                    severity="BLOCKER",
                                    finding=f"test.sh {mode} FAIL with solution (expected PASS)",
                                    evidence=out[:2000],
                                    suggested_fix="Fix solution or test harness so all tests pass.",
                                )
                            )
                        elif not xml_ok:
                            critical_fail = True
                            findings.append(
                                Finding(
                                    phase="0",
                                    severity="BLOCKER",
                                    finding=f"test.sh {mode} with solution: invalid JUnit XML",
                                    evidence=xml_msg,
                                    suggested_fix="Ensure test.sh writes valid JUnit XML.",
                                )
                            )
            else:
                log_lines.append("\n--- With solution ---")
                log_lines.append("  SKIPPED — solution.patch empty or missing")

    finally:
        restore_lines = _restore_patch_state(
            repo_path,
            test_rel=test_rel,
            solution_rel=solution_rel,
            test_was_applied=test_was_applied,
            solution_was_applied=solution_was_applied,
        )
        if restore_lines:
            log_lines.append("\n=== Restore patch state ===")
            log_lines.extend(restore_lines)

    if not critical_fail and not findings:
        log_lines.append("\nPhase 0 test contract: PASS")
        log_activity("test contract: PASS (base/new verified)", category="phase0")
    else:
        log_activity(
            f"test contract: {'CRITICAL FAIL' if critical_fail else 'issues'} — "
            f"{len(findings)} finding(s)",
            category="phase0",
        )

    return "\n".join(log_lines), findings, critical_fail


def run_phase0_fast(
    repo_path: Path,
    *,
    artifacts: dict[str, str | None] | None = None,
    commit: str | None = None,
) -> Phase0Result:
    """Run v1 Phase 0: artifact presence, HEAD, patch apply --check."""
    repo_path = resolve_repo_path(repo_path)
    log_lines: list[str] = []
    findings: list[Finding] = []
    critical_fail = False

    if artifacts is None:
        artifacts = discover_artifacts(repo_path)
    if commit is None:
        commit = get_git_commit(repo_path)
    missing = sorted(name for name, rel in artifacts.items() if rel is None)
    log_activity(
        "artifact discovery: "
        + (f"missing {', '.join(missing)}" if missing else "all present"),
        category="phase0",
    )
    log_lines.append("=== Artifact discovery ===")
    for name, rel in artifacts.items():
        status = rel if rel else "MISSING"
        log_lines.append(f"  {name}: {status}")
        if rel is None:
            findings.append(
                Finding(
                    phase="0",
                    severity="BLOCKER",
                    finding=f"Missing required artifact: {name}",
                    evidence=f"glob search under {repo_path}",
                    suggested_fix=f"Add {name} to the submission.",
                )
            )
            critical_fail = True

    log_lines.append("\n=== Git commit ===")
    if commit:
        log_lines.append(f"  HEAD: {commit}")
    else:
        log_lines.append("  HEAD: unavailable (not a git repo or git error)")
        findings.append(
            Finding(
                phase="0",
                severity="MAJOR",
                finding="Could not resolve git HEAD",
                evidence="git rev-parse HEAD failed",
                suggested_fix="Ensure the submission is a valid git checkout.",
            )
        )

    log_lines.append("\n=== Patch apply --check ===")
    patch_jobs: dict[str, str] = {}
    for patch_name in ("test.patch", "solution.patch"):
        rel = artifacts.get(patch_name)
        if rel is not None:
            patch_jobs[patch_name] = rel

    patch_results: dict[str, tuple[bool, str]] = {}
    if patch_jobs:
        with ThreadPoolExecutor(max_workers=len(patch_jobs)) as pool:
            futures = {
                pool.submit(_check_patch_apply, repo_path, rel): name
                for name, rel in patch_jobs.items()
            }
            for future in as_completed(futures):
                name = futures[future]
                patch_results[name] = future.result()

    for patch_name in ("test.patch", "solution.patch"):
        if patch_name not in patch_results:
            continue
        ok, msg = patch_results[patch_name]
        log_activity(
            f"git apply --check {patch_name}: {'OK' if ok else 'FAILED'}",
            category="phase0",
        )
        log_lines.append(f"  {msg}")
        if not ok:
            critical_fail = True
            findings.append(
                Finding(
                    phase="0",
                    severity="BLOCKER",
                    finding=f"{patch_name} does not apply cleanly",
                    evidence=msg,
                    suggested_fix=f"Regenerate {patch_name} against the stated base commit.",
                )
            )

    log_lines.append("\n=== Skipped in fast tier ===")
    log_lines.append(
        "  Docker test.sh base/new runs — enable REVIEW_PHASE0=full"
    )

    if critical_fail:
        status = "FAIL"
        summary = "Critical Phase 0 checks failed (missing artifacts or patch apply)."
    elif findings:
        status = "FAIL"
        summary = "Phase 0 completed with failures."
    else:
        status = "PASS"
        summary = "Artifacts present; patches apply cleanly (fast tier — tests not run)."

    return Phase0Result(
        status=status,
        summary=summary,
        phase0_log="\n".join(log_lines),
        findings=findings,
        critical_fail=critical_fail,
        commit=commit,
        artifacts=artifacts,
    )


def run_phase0(
    repo_path: Path,
    *,
    artifacts: dict[str, str | None] | None = None,
    commit: str | None = None,
    run_tests: bool = False,
    test_timeout: int = DEFAULT_TEST_TIMEOUT_SEC,
    build_timeout: int = DEFAULT_BUILD_TIMEOUT_SEC,
    test_runner: TestRunner | None = None,
    force_host_tests: bool = False,
) -> Phase0Result:
    """Run Phase 0 mechanical checks; optionally run deterministic test.sh contract."""
    result = run_phase0_fast(
        repo_path,
        artifacts=artifacts,
        commit=commit,
    )

    if not run_tests:
        return result

    if result.critical_fail:
        test_log = (
            "\n=== test.sh runs (Phase 0) ===\n"
            "  SKIPPED — prior Phase 0 critical failures (artifacts/patches)"
        )
        return Phase0Result(
            status=result.status,
            summary=result.summary,
            phase0_log=result.phase0_log + "\n" + test_log,
            findings=list(result.findings),
            critical_fail=result.critical_fail,
            commit=result.commit,
            artifacts=result.artifacts,
        )

    test_log, test_findings, test_critical = run_phase0_tests(
        repo_path,
        artifacts=result.artifacts,
        timeout=test_timeout,
        build_timeout=build_timeout,
        test_runner=test_runner,
        force_host_tests=force_host_tests,
    )

    combined_log = result.phase0_log + "\n" + test_log
    combined_findings = list(result.findings) + test_findings
    critical_fail = result.critical_fail or test_critical

    if critical_fail:
        status = "FAIL"
        summary = "Phase 0 failed: mechanical checks or test.sh contract."
    elif combined_findings:
        status = "FAIL"
        summary = "Phase 0 completed with failures."
    else:
        status = "PASS"
        summary = (
            "Phase 0 PASS: artifacts OK, patches apply, "
            "Docker test.sh base/new contract verified (--network none)."
        )

    return Phase0Result(
        status=status,
        summary=summary,
        phase0_log=combined_log,
        findings=combined_findings,
        critical_fail=critical_fail,
        commit=result.commit,
        artifacts=result.artifacts,
    )


def phase0_to_phase_result(result: Phase0Result) -> PhaseResult:
    return PhaseResult(status=result.status, summary=result.summary)  # type: ignore[arg-type]
