# Deterministic Phase 5 checks from scraped agent run stats.

from __future__ import annotations

import re
from typing import Any

from review.rubric_defaults import (
    MARS_MAX_PASS_RATE_PCT,
    MARS_MIN_MEDIAN_LOC,
    MIN_AGENT_RUNS,
    OLYMPUS_MAX_PASS_RATE_PCT,
    OLYMPUS_MIN_MEDIAN_FILES,
    OLYMPUS_MIN_MEDIAN_LOC,
    OLYMPUS_MIN_MEDIAN_MESSAGES,
)
from review.schemas import Finding, PhaseResult

_AGENT_RUN_COUNT_RE = re.compile(
    r"(\d+)\s*/\s*(\d+)\s*(?:agent|runs?)?",
    re.I,
)
_PASS_FRACTION_RE = re.compile(
    r"(\d+)\s*/\s*(\d+)\s*(?:pass|passed|runs?)?",
    re.I,
)
_PASS_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*pass", re.I)
_MEDIAN_LOC_RE = re.compile(
    r"Median\s+(?:solution\s+)?LOC:?\s*([\d.]+)",
    re.I,
)
_MEDIAN_STATS_RE = re.compile(
    r"Median\s+files?:\s*([\d.]+).*?messages?:\s*([\d.]+).*?LOC:\s*([\d.]+)",
    re.I | re.S,
)


def parse_agent_run_metrics(raw_text: str) -> dict[str, Any]:
    """Extract run counts, pass rate, and median stats from agent run panel text."""
    text = (raw_text or "").strip()
    metrics: dict[str, Any] = {
        "completed_runs": None,
        "required_runs": None,
        "passes": None,
        "total_runs": None,
        "pass_rate_pct": None,
        "median_loc": None,
        "median_files": None,
        "median_messages": None,
    }
    if not text:
        return metrics

    agent_match = _AGENT_RUN_COUNT_RE.search(text)
    if agent_match and "agent" in agent_match.group(0).lower():
        metrics["completed_runs"] = int(agent_match.group(1))
        metrics["required_runs"] = int(agent_match.group(2))

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or "agent" in stripped.lower():
            continue
        pass_match = _PASS_FRACTION_RE.search(stripped)
        if pass_match and re.search(r"pass", stripped, re.I):
            passes = int(pass_match.group(1))
            total = int(pass_match.group(2))
            metrics["passes"] = passes
            metrics["total_runs"] = total
            if total:
                metrics["pass_rate_pct"] = round(100.0 * passes / total, 1)
            break

    if metrics["pass_rate_pct"] is None:
        pass_match = _PASS_FRACTION_RE.search(text)
        if pass_match and re.search(r"pass", pass_match.group(0), re.I):
            passes = int(pass_match.group(1))
            total = int(pass_match.group(2))
            metrics["passes"] = passes
            metrics["total_runs"] = total
            if total:
                metrics["pass_rate_pct"] = round(100.0 * passes / total, 1)

    pct_match = _PASS_PCT_RE.search(text)
    if pct_match:
        metrics["pass_rate_pct"] = float(pct_match.group(1))

    stats_match = _MEDIAN_STATS_RE.search(text)
    if stats_match:
        metrics["median_files"] = float(stats_match.group(1))
        metrics["median_messages"] = float(stats_match.group(2))
        metrics["median_loc"] = float(stats_match.group(3))
    else:
        loc_match = _MEDIAN_LOC_RE.search(text)
        if loc_match:
            metrics["median_loc"] = float(loc_match.group(1))

    return metrics


def evaluate_agent_runs_phase5(
    agent_runs_data: dict[str, Any],
    *,
    quest: str,
    min_runs: int = MIN_AGENT_RUNS,
    olympus_max_pass_rate_pct: float = OLYMPUS_MAX_PASS_RATE_PCT,
    mars_max_pass_rate_pct: float = MARS_MAX_PASS_RATE_PCT,
    olympus_min_median_loc: int = OLYMPUS_MIN_MEDIAN_LOC,
    olympus_min_median_files: int = OLYMPUS_MIN_MEDIAN_FILES,
    olympus_min_median_messages: float = OLYMPUS_MIN_MEDIAN_MESSAGES,
    mars_min_median_loc: int = MARS_MIN_MEDIAN_LOC,
) -> tuple[PhaseResult, list[Finding]]:
    """Check agent run completion, difficulty (pass rate), and scope medians."""
    if not agent_runs_data.get("available"):
        return (
            PhaseResult(
                status="SKIP",
                summary="Agent run data not available — manual phase 5 review required.",
            ),
            [],
        )

    raw_text = str(agent_runs_data.get("raw_text", ""))
    metrics = parse_agent_run_metrics(raw_text)
    if metrics.get("median_loc") is None:
        metrics = {**metrics, **agent_runs_data.get("metrics", {})}

    findings: list[Finding] = []
    issues: list[str] = []

    completed = metrics.get("completed_runs")
    required = metrics.get("required_runs")
    if completed is not None and required is not None:
        if completed < min_runs or required < min_runs or completed < required:
            findings.append(
                Finding(
                    phase="5",
                    severity="MAJOR",
                    finding="Insufficient agent runs completed",
                    evidence=f"{completed}/{required} runs (need ≥ {min_runs})",
                    suggested_fix=(
                        f"Wait for at least {min_runs}/{min_runs} agent runs to finish "
                        "before approving."
                    ),
                )
            )
            issues.append(f"runs {completed}/{required} < {min_runs}")

    pass_rate = metrics.get("pass_rate_pct")
    max_pass = (
        olympus_max_pass_rate_pct if quest == "olympus" else mars_max_pass_rate_pct
    )
    if pass_rate is not None and pass_rate > max_pass:
        findings.append(
            Finding(
                phase="5",
                severity="MAJOR",
                finding="Agent pass rate too high for target difficulty",
                evidence=f"pass_rate={pass_rate}% > max {max_pass:.0f}% ({quest})",
                suggested_fix=(
                    f"Tighten problem/tests so pass rate is ≤ {max_pass:.0f}% "
                    "(Hard difficulty bar)."
                ),
            )
        )
        issues.append(f"pass rate {pass_rate}% > {max_pass:.0f}%")

    median_loc = metrics.get("median_loc")
    if median_loc is not None:
        if quest == "olympus" and median_loc < olympus_min_median_loc:
            findings.append(
                Finding(
                    phase="5",
                    severity="MAJOR",
                    finding="Median agent LOC below Olympus long-horizon bar",
                    evidence=(
                        f"median_loc={median_loc} < olympus_min={olympus_min_median_loc}"
                    ),
                    suggested_fix=(
                        f"Expand task scope so median successful agent runs reach "
                        f"≥ {olympus_min_median_loc} LOC."
                    ),
                )
            )
            issues.append(f"median LOC {median_loc} < {olympus_min_median_loc}")
        elif quest == "mars" and median_loc < mars_min_median_loc:
            findings.append(
                Finding(
                    phase="5",
                    severity="MAJOR",
                    finding="Median agent LOC below Mars minimum scope",
                    evidence=f"median_loc={median_loc} < mars_min={mars_min_median_loc}",
                    suggested_fix=(
                        f"Ensure median successful agent runs reach "
                        f"≥ {mars_min_median_loc} LOC."
                    ),
                )
            )
            issues.append(f"median LOC {median_loc} < {mars_min_median_loc}")

    if quest == "olympus":
        median_files = metrics.get("median_files")
        if median_files is not None and median_files < olympus_min_median_files:
            findings.append(
                Finding(
                    phase="5",
                    severity="MAJOR",
                    finding="Median agent file count below Olympus long-horizon bar",
                    evidence=(
                        f"median_files={median_files} < olympus_min="
                        f"{olympus_min_median_files}"
                    ),
                    suggested_fix=(
                        f"Expand scope so median successful runs touch "
                        f"≥ {olympus_min_median_files} files."
                    ),
                )
            )
            issues.append(f"median files {median_files} < {olympus_min_median_files}")

        median_messages = metrics.get("median_messages")
        if (
            median_messages is not None
            and median_messages < olympus_min_median_messages
        ):
            findings.append(
                Finding(
                    phase="5",
                    severity="MAJOR",
                    finding="Median agent messages below Olympus long-horizon bar",
                    evidence=(
                        f"median_messages={median_messages} < olympus_min="
                        f"{olympus_min_median_messages}"
                    ),
                    suggested_fix=(
                        f"Increase task complexity so median successful runs reach "
                        f"≥ {olympus_min_median_messages:.0f} messages."
                    ),
                )
            )
            issues.append(
                f"median messages {median_messages} < {olympus_min_median_messages}"
            )

    if findings:
        return (
            PhaseResult(
                status="FAIL",
                summary="Agent run platform checks failed: " + "; ".join(issues),
            ),
            findings,
        )

    summary_parts = []
    if pass_rate is not None:
        summary_parts.append(f"pass rate {pass_rate}%")
    if median_loc is not None:
        summary_parts.append(f"median LOC {median_loc}")
    if completed is not None and required is not None:
        summary_parts.append(f"runs {completed}/{required}")
    summary = (
        "Agent run platform checks passed"
        + (": " + ", ".join(summary_parts) if summary_parts else ".")
    )
    return PhaseResult(status="PASS", summary=summary), []
