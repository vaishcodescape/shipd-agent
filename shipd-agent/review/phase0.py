# Phase 0 fast tier — mechanical artifact and patch checks.

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from review.context import discover_artifacts, get_git_commit, resolve_repo_path
from review.schemas import Finding, PhaseResult


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
    patch_path = repo_path / rel_path
    if not patch_path.is_file():
        return False, f"{rel_path}: file not found"
    code, output = _run_git(["apply", "--check", str(patch_path)], repo_path)
    if code == 0:
        return True, f"{rel_path}: git apply --check OK"
    return False, f"{rel_path}: git apply --check FAILED\n{output}"


def check_patch_apply(repo_path: Path, rel_path: str) -> tuple[bool, str]:
    """Public wrapper for git apply --check on a patch relative path."""
    return _check_patch_apply(repo_path, rel_path)


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

    log_lines.append("\n=== Skipped in v1 fast tier ===")
    log_lines.append("  test.sh base/new runs, Docker build, JUnit XML — deferred to v2")

    if critical_fail:
        status = "FAIL"
        summary = "Critical Phase 0 checks failed (missing artifacts or patch apply)."
    elif findings:
        status = "FAIL"
        summary = "Phase 0 completed with failures."
    else:
        status = "PASS"
        summary = "Artifacts present; patches apply cleanly (fast tier only)."

    return Phase0Result(
        status=status,
        summary=summary,
        phase0_log="\n".join(log_lines),
        findings=findings,
        critical_fail=critical_fail,
        commit=commit,
        artifacts=artifacts,
    )


def phase0_to_phase_result(result: Phase0Result) -> PhaseResult:
    return PhaseResult(status=result.status, summary=result.summary)  # type: ignore[arg-type]
