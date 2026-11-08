# Local false-positive check for passing agent runs.
#
# Shipd's own FP service was not visible when this was written. This reviews a
# passing run's patch when the page includes one. No patch is a skip, not a pass.

from __future__ import annotations

import re
from typing import Any

from review.agent_runs_checks import parse_agent_run_metrics
from review.schemas import Finding, PhaseResult

_TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:tests?|__tests__|specs?)/|(?:^|/)test_[^/]+$"
    r"|(?:_test|\.test|\.spec)(?:\.[^/]+)$|(?:^|/)[^/]+Test\.java$",
    re.I,
)


def _changed_paths(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            paths.append(line[6:].strip())
        elif line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4 and parts[3].startswith("b/"):
                paths.append(parts[3][2:])
    return paths


def _substantive_added_lines(patch: str) -> list[str]:
    lines: list[str] = []
    for line in patch.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        body = line[1:].strip()
        if not body or body.startswith("#") or body.startswith("//"):
            continue
        lines.append(body)
    return lines


def patch_is_false_pass(patch: str) -> str | None:
    """Return why a passing-run patch did not solve the task, or None."""
    text = (patch or "").strip()
    if not text:
        return "Passing run patch is empty."
    added = _substantive_added_lines(text)
    deleted = _substantive_added_lines("\n".join(
        "+" + line[1:] for line in text.splitlines()
        if line.startswith("-") and not line.startswith("---")
    ))
    if not added and not deleted:
        return "Passing run patch has no substantive change."
    paths = _changed_paths(text)
    if paths and all(_TEST_PATH_RE.search(path) for path in paths):
        return "Passing run patch only edits tests."
    # Only inspect additions in test files for explicit bypasses. A removed
    # production assertion or a deletion-only fix can implement the task.
    current = ""
    test_additions: list[str] = []
    for line in text.splitlines():
        if line.startswith("diff --git "):
            current = ""
        elif line.startswith("+++ b/"):
            current = line[6:].strip()
        elif line.startswith("+") and _TEST_PATH_RE.search(current):
            test_additions.append(line[1:])
    if any(re.search(r"\b(?:pytest\.skip|unittest\.skip|assert\s+True\b)", line)
           for line in test_additions if not line.lstrip().startswith(("#", "//"))):
        return "Passing run patch disables or trivializes a test."
    return None


def evaluate_false_positives(
    agent_runs_data: dict[str, Any] | None,
) -> tuple[PhaseResult, list[Finding]]:
    """Fail when any unhinted passing run's patch is a false pass.

    SKIP when the page did not include a passing-run patch. That skip is not a
    pass of Shipd's FP check.
    """
    data = agent_runs_data or {}
    raw_text = str(data.get("raw_text") or "")
    metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
    patches = list(metrics.get("passing_patches") or [])
    if raw_text and not patches:
        parsed = parse_agent_run_metrics(raw_text)
        patches = list(parsed.get("passing_patches") or [])

    if not patches:
        return (
            PhaseResult(
                status="SKIP",
                summary=(
                    "False-positive check skipped: no passing-run patch was on the page."
                ),
            ),
            [],
        )

    findings: list[Finding] = []
    for item in patches:
        patch = str(item.get("patch") or "")
        reason = patch_is_false_pass(patch)
        if reason is None:
            continue
        agent = item.get("agent") or "an agent"
        findings.append(
            Finding(
                phase="5",
                severity="MAJOR",
                finding="False positive in a passing agent run",
                evidence=f"{agent}: {reason}",
                suggested_fix=(
                    "A passing run has to solve the task. Drop runs that only edit "
                    "tests, delete assertions, or ship an empty patch, then rerun."
                ),
            )
        )
    if findings:
        return (
            PhaseResult(
                status="FAIL",
                summary=f"False-positive check failed: {len(findings)} passing run(s).",
            ),
            findings,
        )
    return (
        PhaseResult(
            status="PASS",
            summary=(
                f"False-positive check found no false pass in {len(patches)} "
                "passing-run patch(es). This is a local reading of the patch, "
                "not Shipd's FP service."
            ),
        ),
        [],
    )
