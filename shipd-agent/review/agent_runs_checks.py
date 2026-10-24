# Deterministic Phase 5 checks from scraped agent run stats.

from __future__ import annotations

import re
from typing import Any

from review.rubric_defaults import (
    AGENT_TYPES,
    MARS_MIN_MEDIAN_LOC,
    OLYMPUS_MIN_MEDIAN_FILES,
    OLYMPUS_MIN_MEDIAN_LOC,
    OLYMPUS_MIN_MEDIAN_MESSAGES,
    REQUIRED_PASSING_RUNS,
    RUNS_PER_AGENT_TYPE,
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

# Per-agent summaries: "Nova: 1/4 pass", "Orion 0 of 4 passed".
_AGENT_FRACTION_RE = re.compile(r"(\d+)\s*(?:/|of)\s*(\d+)\s*(?:runs?\s*)?pass", re.I)
# Per-agent counts: "Vega — 4 runs, 0 passed".
_RUNS_COUNT_RE = re.compile(r"(\d+)\s*runs?\b", re.I)
_PASSES_COUNT_RE = re.compile(r"(\d+)\s*pass(?:ed|es|ing)?\b", re.I)
# Per-run status badges: "PASS FAIL FAIL FAIL", "✅ ❌ ❌ ❌" ("pass rate" is not a run).
_RUN_STATUS_RE = re.compile(
    r"\b(pass(?:ed)?|fail(?:ed)?)\b(?!\s*rate)|([✓✔✅])|([✗✘❌])",
    re.I,
)
# A row describing one run ("Nova run 2: FAIL", "#3 PASS") counts once even if
# its detail text repeats a status word.
_SINGLE_RUN_ROW_RE = re.compile(r"\b(?:run|attempt)\s*#?\s*\d+\b|#\d+\b", re.I)
_SECTION_END_RE = re.compile(
    r"^(?:median|failure pattern|common fail|loc\b|related|holistic)",
    re.I,
)


def _agent_type_pattern(agent_types: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile(r"\b(" + "|".join(map(re.escape, agent_types)) + r")\b", re.I)


def _summary_counts(segment: str) -> tuple[int, int] | None:
    """(runs, passes) from an explicit per-agent summary, else None."""
    fraction = _AGENT_FRACTION_RE.search(segment)
    if fraction:
        return int(fraction.group(2)), int(fraction.group(1))
    runs = _RUNS_COUNT_RE.search(segment)
    passes = _PASSES_COUNT_RE.search(segment)
    if runs and passes:
        return int(runs.group(1)), int(passes.group(1))
    return None


def _status_tokens(segment: str) -> list[bool]:
    """Per-run outcomes in reading order (True = pass)."""
    outcomes: list[bool] = []
    for match in _RUN_STATUS_RE.finditer(segment):
        if match.group(1):
            outcomes.append(match.group(1).lower().startswith("pass"))
        else:
            outcomes.append(bool(match.group(2)))
    return outcomes


def parse_agent_type_runs(
    raw_text: str,
    *,
    agent_types: tuple[str, ...] = AGENT_TYPES,
) -> dict[str, dict[str, int]]:
    """Per-agent-type run and pass counts from the Agent Runs panel text.

    Understands a summary per agent ("Nova: 1/4 pass", "Vega — 4 runs, 0 passed",
    "Orion 0 of 4 passed", several agents on one line), per-run badges on the
    agent's line ("Nova PASS FAIL FAIL FAIL"), one row per run ("Nova run 2: FAIL"),
    and an agent heading followed by run rows ("Run 1: PASS"). Agent types that
    never appear in the text are absent from the result.
    """
    canonical = {name.lower(): name for name in agent_types}
    pattern = _agent_type_pattern(agent_types)
    summaries: dict[str, tuple[int, int]] = {}
    rows: dict[str, list[bool]] = {}
    current: str | None = None

    for line in (raw_text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        matches = list(pattern.finditer(stripped))
        if matches:
            # Several agents on one line is a compact summary or a legend, never
            # the heading of a run-row section.
            current = canonical[matches[0].group(1).lower()] if len(matches) == 1 else None
            for index, match in enumerate(matches):
                end = matches[index + 1].start() if index + 1 < len(matches) else len(stripped)
                segment = stripped[match.start():end]
                name = canonical[match.group(1).lower()]
                counts = _summary_counts(segment)
                if counts is not None:
                    summaries[name] = counts
                    continue
                tokens = _status_tokens(segment)
                if _SINGLE_RUN_ROW_RE.search(segment):
                    tokens = tokens[:1]
                rows.setdefault(name, []).extend(tokens)
            continue
        if current is None:
            continue
        if _SECTION_END_RE.search(stripped):
            current = None
            continue
        tokens = _status_tokens(stripped)
        if tokens:
            rows.setdefault(current, []).append(tokens[0])

    result: dict[str, dict[str, int]] = {}
    for name in agent_types:
        if name in summaries:
            runs, passes = summaries[name]
        elif rows.get(name):
            runs, passes = len(rows[name]), sum(rows[name])
        else:
            continue
        result[name] = {"runs": runs, "passes": passes}
    return result


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
        "per_agent": {},
    }
    if not text:
        return metrics

    # Aggregate figures come from lines that don't name an agent type, so a
    # per-agent "Nova: 1/4 pass" is never mistaken for the overall result.
    agent_pattern = _agent_type_pattern(AGENT_TYPES)
    aggregate_text = "\n".join(
        line for line in text.splitlines() if not agent_pattern.search(line)
    )

    agent_match = _AGENT_RUN_COUNT_RE.search(aggregate_text)
    if agent_match and "agent" in agent_match.group(0).lower():
        metrics["completed_runs"] = int(agent_match.group(1))
        metrics["required_runs"] = int(agent_match.group(2))

    for line in aggregate_text.splitlines():
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
        pass_match = _PASS_FRACTION_RE.search(aggregate_text)
        if pass_match and re.search(r"pass", pass_match.group(0), re.I):
            passes = int(pass_match.group(1))
            total = int(pass_match.group(2))
            metrics["passes"] = passes
            metrics["total_runs"] = total
            if total:
                metrics["pass_rate_pct"] = round(100.0 * passes / total, 1)

    pct_match = _PASS_PCT_RE.search(aggregate_text)
    if pct_match:
        metrics["pass_rate_pct"] = float(pct_match.group(1))

    stats_match = _MEDIAN_STATS_RE.search(aggregate_text)
    if stats_match:
        metrics["median_files"] = float(stats_match.group(1))
        metrics["median_messages"] = float(stats_match.group(2))
        metrics["median_loc"] = float(stats_match.group(3))
    else:
        loc_match = _MEDIAN_LOC_RE.search(aggregate_text)
        if loc_match:
            metrics["median_loc"] = float(loc_match.group(1))

    per_agent = parse_agent_type_runs(text)
    metrics["per_agent"] = per_agent
    if per_agent and metrics["passes"] is None:
        passes = sum(v["passes"] for v in per_agent.values())
        total = sum(v["runs"] for v in per_agent.values())
        metrics["passes"] = passes
        metrics["total_runs"] = total
        if total:
            metrics["pass_rate_pct"] = round(100.0 * passes / total, 1)

    return metrics


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def evaluate_agent_runs_phase5(
    agent_runs_data: dict[str, Any],
    *,
    quest: str,
    agent_types: tuple[str, ...] = AGENT_TYPES,
    runs_per_agent: int = RUNS_PER_AGENT_TYPE,
    required_passes: int = REQUIRED_PASSING_RUNS,
    olympus_min_median_loc: int = OLYMPUS_MIN_MEDIAN_LOC,
    olympus_min_median_files: int = OLYMPUS_MIN_MEDIAN_FILES,
    olympus_min_median_messages: float = OLYMPUS_MIN_MEDIAN_MESSAGES,
    mars_min_median_loc: int = MARS_MIN_MEDIAN_LOC,
) -> tuple[PhaseResult, list[Finding]]:
    """Check agent-type run coverage, the exactly-one-pass rule, and scope medians.

    Returns SKIP when the panel is missing or none of its figures could be read,
    so an unparseable panel never produces a deterministic PASS.
    """
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
    agents_label = ", ".join(agent_types[:-1]) + f", and {agent_types[-1]}"
    required_runs = runs_per_agent * len(agent_types)

    per_agent: dict[str, dict[str, int]] = metrics.get("per_agent") or {}
    breakdown = ", ".join(
        f"{name} {per_agent[name]['passes']}/{per_agent[name]['runs']}"
        for name in agent_types
        if name in per_agent
    )

    completed = metrics.get("completed_runs")
    required = metrics.get("required_runs")
    runs_done: int | None
    if per_agent:
        runs_done = sum(v["runs"] for v in per_agent.values())
        incomplete = [
            name
            for name in agent_types
            if per_agent.get(name, {}).get("runs", 0) != runs_per_agent
        ]
        if incomplete:
            counts = ", ".join(
                f"{name} {per_agent.get(name, {}).get('runs', 0)}/{runs_per_agent}"
                for name in agent_types
            )
            findings.append(
                Finding(
                    phase="5",
                    severity="MAJOR",
                    finding="Agent runs incomplete for required agent types",
                    evidence=(
                        f"runs per agent: {counts} (need {runs_per_agent} each of "
                        f"{agents_label})"
                    ),
                    suggested_fix=(
                        f"Run each of {agents_label} {runs_per_agent} times "
                        f"({required_runs} runs) before approving."
                    ),
                )
            )
            issues.append("runs " + ", ".join(
                f"{name} {per_agent.get(name, {}).get('runs', 0)}/{runs_per_agent}"
                for name in incomplete
            ))
        passes = sum(v["passes"] for v in per_agent.values())
    else:
        runs_done = completed if completed is not None else metrics.get("total_runs")
        short = runs_done is not None and runs_done < required_runs
        unfinished = completed is not None and required is not None and completed < required
        if short or unfinished:
            findings.append(
                Finding(
                    phase="5",
                    severity="MAJOR",
                    finding="Insufficient agent runs completed",
                    evidence=(
                        f"{runs_done}/{required if unfinished else required_runs} runs "
                        f"(need {runs_per_agent} each of {agents_label} = {required_runs})"
                    ),
                    suggested_fix=(
                        f"Run each of {agents_label} {runs_per_agent} times "
                        f"({required_runs} runs) before approving."
                    ),
                )
            )
            issues.append(f"runs {runs_done}/{required_runs}")
        passes = metrics.get("passes")

    if passes is not None:
        detail = f" ({breakdown})" if breakdown else ""
        if passes < required_passes:
            findings.append(
                Finding(
                    phase="5",
                    severity="MAJOR",
                    finding="No agent run passed",
                    evidence=(
                        f"{passes} passing runs{detail}; need exactly {required_passes} "
                        "from a single agent type"
                    ),
                    suggested_fix=(
                        "Localise the ambiguous prompt sentence or unfair test blocking every "
                        f"agent — exactly {_plural(required_passes, 'run')} should pass."
                    ),
                )
            )
            issues.append(f"{passes} passing runs < {required_passes}")
        elif passes > required_passes:
            passing_types = [
                name for name in agent_types if per_agent.get(name, {}).get("passes", 0) > 0
            ]
            spread = (
                f"; passes span {' and '.join(passing_types)}"
                if len(passing_types) > 1
                else ""
            )
            findings.append(
                Finding(
                    phase="5",
                    severity="MAJOR",
                    finding="Too many passing agent runs",
                    evidence=(
                        f"{passes} passing runs{detail}{spread}; need exactly "
                        f"{required_passes} from a single agent type"
                    ),
                    suggested_fix=(
                        "Tighten the problem/tests so only one Nova/Vega/Orion run passes "
                        "(the task is too easy for the target difficulty)."
                    ),
                )
            )
            issues.append(f"{passes} passing runs > {required_passes}")

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

    if passes is None and runs_done is None and median_loc is None:
        return (
            PhaseResult(
                status="SKIP",
                summary=(
                    "Agent Runs panel found but no run or pass counts could be read — "
                    "manual phase 5 review required."
                ),
            ),
            [],
        )

    summary_parts = []
    if breakdown:
        summary_parts.append(f"passes per agent {breakdown}")
    elif passes is not None:
        summary_parts.append(
            f"{_plural(passes, 'passing run')} of {runs_done if runs_done is not None else '?'} "
            f"(per-agent {'/'.join(agent_types)} split not shown on panel)"
        )
    if median_loc is not None:
        summary_parts.append(f"median LOC {median_loc}")
    summary = (
        "Agent run platform checks passed"
        + (": " + ", ".join(summary_parts) if summary_parts else ".")
    )
    return PhaseResult(status="PASS", summary=summary), []
