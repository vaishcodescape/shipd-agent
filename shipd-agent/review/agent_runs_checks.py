# Deterministic Phase 5 checks from scraped agent run stats.

from __future__ import annotations

import re
from typing import Any

from review.rubric_defaults import (
    AGENT_TYPES,
    MARS_MIN_MEDIAN_LOC,
    MAX_CHEAT_RATE,
    MAX_SOLVE_RATE,
    MIN_UNHINTED_RUNS,
    NOVA_MIN_AVG_SEQUENCE_TOKENS,
    NOVA_PAYOUT_SEQUENCE_CAP,
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

    for line in _DIFF_FENCE_RE.sub("", raw_text or "").splitlines():
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
    text = _DIFF_FENCE_RE.sub("", raw_text or "").strip()
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
    notes = _annotate_rollouts(raw_text or "", per_agent)
    if notes["per_agent"] is not None:
        per_agent = notes["per_agent"]
    metrics["per_agent"] = per_agent
    metrics["hints_marked"] = notes["hints_marked"]
    metrics["nova_sequence_average"] = notes["nova_sequence_average"]
    metrics["sequence_length_labeled"] = notes["sequence_length_labeled"]
    metrics["cheat_rate"] = notes["cheat_rate"]
    metrics["cheat_labeled"] = notes["cheat_labeled"]
    metrics["unfair_runs"] = notes["unfair_runs"]
    metrics["unfair_labeled"] = notes["unfair_labeled"]
    metrics["blocker_runs"] = notes["blocker_runs"]
    metrics["blocker_labeled"] = notes["blocker_labeled"]
    metrics["passing_patches"] = notes["passing_patches"]
    if per_agent and (metrics["passes"] is None or notes["per_agent"] is not None):
        passes = sum(v["passes"] for v in per_agent.values())
        total = sum(v["runs"] for v in per_agent.values())
        metrics["passes"] = passes
        metrics["total_runs"] = total
        metrics["pass_rate_pct"] = round(100.0 * passes / total, 1) if total else None

    return metrics


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _parse_token_amount(number: str, suffix: str | None) -> int:
    value = float(number.replace(",", ""))
    if suffix:
        value *= 1000
    return int(value)


# "400K sequence length" or "sequence length: 450,000". A bare token count does not match.
_SEQUENCE_LENGTH_RE = re.compile(
    r"sequence[\s_-]*length(?:\s*(?:average|avg))?\s*[:=]?\s*([\d,]+(?:\.\d+)?)\s*([kK])?"
    r"|([\d,]+(?:\.\d+)?)\s*([kK])?\s+(?:tokens?\s+of\s+)?sequence[\s_-]*length",
    re.I,
)
_CHEAT_PCT_RE = re.compile(r"cheat(?:\s*rate)?\s*[:=]?\s*(\d+(?:\.\d+)?)\s*%", re.I)
_CHEAT_FRAC_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s+(?:runs?\s+)?cheat", re.I)
_CHEAT_WORD_RE = re.compile(r"\bcheat(?:ed|ing)?\b", re.I)
_UNFAIR_WORD_RE = re.compile(
    r"\bunfair\b|\bbroken task\b|\btask (?:is |was )?broken\b",
    re.I,
)
_UNFAIR_CLEAR_RE = re.compile(
    r"\bno\b[^\n]{0,40}\bunfair\b|\b0 unfair\b|\bunfair\b[^\n]{0,24}\b(?:none|0)\b",
    re.I,
)
_BLOCK_WORD_RE = re.compile(
    r"environment(?:al)? blocker|verifier (?:issue|error|blocker)"
    r"|blocked by (?:the )?(?:environment|verifier)",
    re.I,
)
_BLOCK_CLEAR_RE = re.compile(
    r"\bno\b[^\n]{0,80}(?:environment(?:al)? blocker|verifier (?:issue|error|blocker))",
    re.I,
)
_DIFF_FENCE_RE = re.compile(r"```diff\s*\n(.*?)```", re.S)
_HINTED_RUN_RE = re.compile(r"\bhinted\b", re.I)
_UNHINTED_WORD_RE = re.compile(r"\bunhinted\b", re.I)


def _sequence_length_in(text: str) -> int | None:
    match = _SEQUENCE_LENGTH_RE.search(text or "")
    if not match:
        return None
    if match.group(1):
        return _parse_token_amount(match.group(1), match.group(2))
    return _parse_token_amount(match.group(3), match.group(4))


def _is_hinted_segment(segment: str) -> bool:
    if _UNHINTED_WORD_RE.search(segment):
        return False
    return _HINTED_RUN_RE.search(segment) is not None


def _annotate_rollouts(
    text: str,
    per_agent: dict[str, dict[str, int]],
    *,
    agent_types: tuple[str, ...] = AGENT_TYPES,
) -> dict[str, Any]:
    """Hint exclusion, Nova sequence length, and rollout integrity flags.

    Sequence length counts only when the text labels it as sequence length.
    A generic token total is ignored so it cannot feed the 400K bar.
    """
    # Patches are evidence for the false-pass check, not panel metadata. A
    # string added by an agent must not become a status, token count, or flag.
    patch_text = text
    text = _DIFF_FENCE_RE.sub("", text or "")
    canonical = {name.lower(): name for name in agent_types}
    pattern = _agent_type_pattern(agent_types)
    hints_marked = False
    excluded: set[str] = set()
    unhinted_rows: dict[str, list[bool]] = {}
    nova_lengths: list[int] = []
    explicit_average: int | None = None
    current: str | None = None
    current_run_hinted = False

    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        matches = list(pattern.finditer(stripped))
        agent_on_line = canonical[matches[0].group(1).lower()] if len(matches) == 1 else None
        if len(matches) == 1:
            current = agent_on_line
        elif len(matches) > 1:
            current = None
        if _SECTION_END_RE.search(stripped) and not matches:
            current = None
        if matches or _SINGLE_RUN_ROW_RE.search(stripped) or current is None:
            current_run_hinted = _is_hinted_segment(stripped)

        length = _sequence_length_in(stripped)
        agent_for_length = agent_on_line or current
        if length is not None and not current_run_hinted and not _is_hinted_segment(stripped):
            if re.search(r"\b(?:average|avg)\b", stripped, re.I) and (
                agent_for_length == "Nova" or re.search(r"\bNova\b", stripped, re.I)
            ):
                explicit_average = length
            elif agent_for_length == "Nova":
                nova_lengths.append(length)

        if not matches:
            if current and _is_hinted_segment(stripped):
                hints_marked = True
                if _SINGLE_RUN_ROW_RE.search(stripped) or _status_tokens(stripped):
                    excluded.add(current)
            continue
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(stripped)
            segment = stripped[match.start():end]
            name = canonical[match.group(1).lower()]
            if not _is_hinted_segment(segment):
                continue
            hints_marked = True
            if not (_SINGLE_RUN_ROW_RE.search(segment) or _status_tokens(segment)):
                continue
            excluded.add(name)
            # Hinted rows are dropped. Unhinted siblings are counted below.

    if excluded:
        # Rebuild from the same walker, keeping only unhinted status rows for
        # agents that had at least one hinted run. Summaries include hinted
        # runs, so they cannot be the bar.
        current = None
        for line in (text or "").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            matches = list(pattern.finditer(stripped))
            if matches:
                current = canonical[matches[0].group(1).lower()] if len(matches) == 1 else None
                for index, match in enumerate(matches):
                    end = matches[index + 1].start() if index + 1 < len(matches) else len(stripped)
                    segment = stripped[match.start():end]
                    name = canonical[match.group(1).lower()]
                    if name not in excluded or _is_hinted_segment(segment):
                        continue
                    if _summary_counts(segment) is not None and not _SINGLE_RUN_ROW_RE.search(segment):
                        continue
                    tokens = _status_tokens(segment)
                    if not tokens:
                        continue
                    if _SINGLE_RUN_ROW_RE.search(segment):
                        tokens = tokens[:1]
                    unhinted_rows.setdefault(name, []).extend(tokens)
                continue
            if current not in excluded:
                continue
            if _SECTION_END_RE.search(stripped) or _is_hinted_segment(stripped):
                if _SECTION_END_RE.search(stripped):
                    current = None
                continue
            tokens = _status_tokens(stripped)
            if tokens:
                unhinted_rows.setdefault(current, []).append(tokens[0])

    override: dict[str, dict[str, int]] | None = None
    if excluded:
        override = {name: dict(stats) for name, stats in per_agent.items()}
        for name in excluded:
            rows = unhinted_rows.get(name, [])
            override[name] = {"runs": len(rows), "passes": sum(rows)}

    if explicit_average is not None:
        nova_average: float | None = float(explicit_average)
    elif nova_lengths:
        nova_average = sum(nova_lengths) / len(nova_lengths)
    else:
        nova_average = None

    cheat_rate: float | None = None
    cheat_labeled = _CHEAT_WORD_RE.search(text or "") is not None
    pct = _CHEAT_PCT_RE.search(text or "")
    frac = _CHEAT_FRAC_RE.search(text or "")
    if pct:
        cheat_rate = float(pct.group(1)) / 100.0
    elif frac:
        total = int(frac.group(2))
        cheat_rate = (int(frac.group(1)) / total) if total else None
    elif cheat_labeled:
        flagged = 0
        considered = 0
        for line in (text or "").splitlines():
            if not _SINGLE_RUN_ROW_RE.search(line) and not _status_tokens(line):
                continue
            if _is_hinted_segment(line):
                continue
            considered += 1
            if _CHEAT_WORD_RE.search(line):
                flagged += 1
        if considered:
            cheat_rate = flagged / considered

    unfair_labeled = _UNFAIR_WORD_RE.search(text or "") is not None or _UNFAIR_CLEAR_RE.search(text or "") is not None
    if _UNFAIR_CLEAR_RE.search(text or "") and not any(
        _UNFAIR_WORD_RE.search(line) and not _UNFAIR_CLEAR_RE.search(line)
        for line in (text or "").splitlines()
    ):
        unfair_runs = 0
        unfair_labeled = True
    else:
        unfair_runs = sum(
            1
            for line in (text or "").splitlines()
            if _UNFAIR_WORD_RE.search(line) and not _UNFAIR_CLEAR_RE.search(line)
        )

    blocker_labeled = (
        _BLOCK_WORD_RE.search(text or "") is not None
        or _BLOCK_CLEAR_RE.search(text or "") is not None
    )
    if _BLOCK_CLEAR_RE.search(text or "") and not any(
        _BLOCK_WORD_RE.search(line) and not _BLOCK_CLEAR_RE.search(line)
        for line in (text or "").splitlines()
    ):
        blocker_runs = 0
        blocker_labeled = True
    else:
        blocker_runs = sum(
            1
            for line in (text or "").splitlines()
            if _BLOCK_WORD_RE.search(line) and not _BLOCK_CLEAR_RE.search(line)
        )

    passing_patches: list[dict[str, str]] = []
    for match in _DIFF_FENCE_RE.finditer(patch_text or ""):
        before = (patch_text or "")[: match.start()]
        previous = ""
        for line in reversed(before.splitlines()):
            if line.strip():
                previous = line.strip()
                break
        if not previous or _is_hinted_segment(previous):
            continue
        if not re.search(r"\bpass(?:ed)?\b|[✓✔✅]", previous, re.I):
            continue
        agent_match = pattern.search(previous)
        agent = canonical[agent_match.group(1).lower()] if agent_match else ""
        passing_patches.append({"agent": agent, "patch": match.group(1).strip()})

    if re.search(r"\bno hinted\b|\b0 hinted\b|\bhinted runs:\s*none\b", text or "", re.I):
        hints_marked = True

    return {
        "hints_marked": hints_marked or bool(excluded),
        "per_agent": override,
        "nova_sequence_average": nova_average,
        "sequence_length_labeled": nova_average is not None,
        "cheat_rate": cheat_rate,
        "cheat_labeled": cheat_labeled,
        "unfair_runs": unfair_runs,
        "unfair_labeled": unfair_labeled,
        "blocker_runs": blocker_runs,
        "blocker_labeled": blocker_labeled,
        "passing_patches": passing_patches,
    }


def _rate_label(passes: int, runs: int) -> str:
    pct = 100.0 * passes / runs if runs else 0.0
    text = f"{pct:.0f}" if abs(pct - round(pct)) < 0.05 else f"{pct:.1f}"
    return f"{passes}/{runs} = {text}%"


def evaluate_agent_runs_phase5(
    agent_runs_data: dict[str, Any],
    *,
    quest: str,
    agent_types: tuple[str, ...] = AGENT_TYPES,
    min_unhinted_runs: int = MIN_UNHINTED_RUNS,
    max_solve_rate: float = MAX_SOLVE_RATE,
    nova_min_sequence_tokens: int = NOVA_MIN_AVG_SEQUENCE_TOKENS,
    nova_payout_sequence_cap: int = NOVA_PAYOUT_SEQUENCE_CAP,
    mars_min_median_loc: int = MARS_MIN_MEDIAN_LOC,
    max_cheat_rate: float = MAX_CHEAT_RATE,
    nova_agent: str = "Nova",
) -> tuple[PhaseResult, list[Finding]]:
    """Check unhinted coverage, the one-model solve rate, and Nova sequence length.

    Returns SKIP when the panel is missing, unreadable, or only an aggregate
    total. An unreadable panel never produces a deterministic PASS. Olympus
    also SKIPs when the solve bar is met but Nova sequence length is not labeled.
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
    stored = agent_runs_data.get("metrics") or {}
    if metrics.get("median_loc") is None and stored.get("median_loc") is not None:
        metrics["median_loc"] = stored["median_loc"]
    if not metrics.get("sequence_length_labeled") and stored.get("sequence_length_labeled"):
        metrics["nova_sequence_average"] = stored.get("nova_sequence_average")
        metrics["sequence_length_labeled"] = True

    findings: list[Finding] = []
    issues: list[str] = []
    agents_label = ", ".join(agent_types[:-1]) + f", and {agent_types[-1]}"
    per_agent: dict[str, dict[str, int]] = metrics.get("per_agent") or {}
    breakdown = ", ".join(
        f"{name} {per_agent[name]['passes']}/{per_agent[name]['runs']}"
        for name in agent_types
        if name in per_agent
    )

    if not per_agent:
        if (
            metrics.get("passes") is None
            and metrics.get("total_runs") is None
            and metrics.get("completed_runs") is None
            and not metrics.get("sequence_length_labeled")
        ):
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
        return (
            PhaseResult(
                status="SKIP",
                summary=(
                    "Per-agent unhinted counts are not on the panel — not a pass."
                ),
            ),
            [],
        )

    incomplete = [
        name
        for name in agent_types
        if per_agent.get(name, {}).get("runs", 0) < min_unhinted_runs
    ]
    if incomplete:
        counts = ", ".join(
            f"{name} {per_agent.get(name, {}).get('runs', 0)}/{min_unhinted_runs}"
            for name in agent_types
        )
        findings.append(
            Finding(
                phase="5",
                severity="MAJOR",
                finding="Agent runs incomplete for required agent types",
                evidence=(
                    f"unhinted runs per agent: {counts} (need at least "
                    f"{min_unhinted_runs} each of {agents_label})"
                ),
                suggested_fix=(
                    f"Finish at least {min_unhinted_runs} unhinted runs of each of "
                    f"{agents_label}. Hinted runs do not count."
                ),
            )
        )
        issues.append("runs " + ", ".join(
            f"{name} {per_agent.get(name, {}).get('runs', 0)}/{min_unhinted_runs}"
            for name in incomplete
        ))

    solvers: list[tuple[str, float, int, int]] = []
    for name in agent_types:
        stats = per_agent.get(name) or {}
        runs = int(stats.get("runs", 0))
        passes = int(stats.get("passes", 0))
        if runs and passes > 0:
            solvers.append((name, passes / runs, passes, runs))

    if len(solvers) == 0:
        findings.append(
            Finding(
                phase="5",
                severity="MAJOR",
                finding="No model solved the task",
                evidence=(
                    f"0 unhinted passes ({breakdown})"
                    if breakdown
                    else "0 unhinted passes"
                ),
                suggested_fix=(
                    "Localise the ambiguous prompt sentence or unfair test that blocks "
                    "every model. Exactly one model should solve it, at 25% or below."
                ),
            )
        )
        issues.append("no model solved the task")
    elif len(solvers) > 1:
        names = " and ".join(item[0] for item in solvers)
        findings.append(
            Finding(
                phase="5",
                severity="MAJOR",
                finding="More than one model solved the task",
                evidence=f"{names} both have unhinted passes ({breakdown})",
                suggested_fix=(
                    "Tighten the task until only one of "
                    f"{agents_label} solves it, and the other two stay at 0%."
                ),
            )
        )
        issues.append(f"solvers {names}")
    else:
        name, rate, passes, runs = solvers[0]
        if rate > max_solve_rate:
            findings.append(
                Finding(
                    phase="5",
                    severity="MAJOR",
                    finding="Solve rate above 25%",
                    evidence=(
                        f"{name} {_rate_label(passes, runs)} "
                        f"(maximum {max_solve_rate:.0%}; others at 0%)"
                    ),
                    suggested_fix=(
                        "Make the task harder so the solving model succeeds on at most "
                        "25% of its unhinted runs."
                    ),
                )
            )
            issues.append(f"{name} {_rate_label(passes, runs)}")
        elif quest == "olympus" and name.lower() == nova_agent.lower():
            findings.append(
                Finding(
                    phase="5",
                    severity="MINOR",
                    finding="Nova-only solve needs confirmation",
                    evidence=(
                        f"{name} {_rate_label(passes, runs)}; Vega and Orion are at 0%. "
                        "The criteria panel allows any one model, but The Process says an "
                        "unhinted Nova solve means the task is too easy."
                    ),
                    suggested_fix=(
                        "Treat this as a warning until Shipd confirms whether a Nova-only "
                        "solve passes. Do not describe it as a clear pass."
                    ),
                )
            )

    long_horizon_unverified = False
    sequence_average = metrics.get("nova_sequence_average")
    if quest == "olympus":
        if not metrics.get("sequence_length_labeled") or sequence_average is None:
            long_horizon_unverified = True
        elif float(sequence_average) <= nova_min_sequence_tokens:
            findings.append(
                Finding(
                    phase="5",
                    severity="MAJOR",
                    finding="Nova sequence length below the long-horizon bar",
                    evidence=(
                        f"Nova average sequence length {float(sequence_average):,.0f} "
                        f"is not above {nova_min_sequence_tokens:,}"
                    ),
                    suggested_fix=(
                        "Widen the task so Nova's average sequence length "
                        f"(prompt, output, and compacted tokens) is above "
                        f"{nova_min_sequence_tokens:,}."
                    ),
                )
            )
            issues.append(
                f"Nova sequence length {float(sequence_average):,.0f} "
                f"<= {nova_min_sequence_tokens:,}"
            )
    elif quest == "mars":
        median_loc = metrics.get("median_loc")
        if median_loc is not None and median_loc < mars_min_median_loc:
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

    if metrics.get("cheat_labeled") and metrics.get("cheat_rate") is not None:
        cheat_rate = float(metrics["cheat_rate"])
        if cheat_rate >= max_cheat_rate:
            findings.append(
                Finding(
                    phase="5",
                    severity="MAJOR",
                    finding="Cheat rate is not below 20%",
                    evidence=(
                        f"cheat rate {cheat_rate:.0%} across unhinted runs "
                        f"(must be below {max_cheat_rate:.0%})"
                    ),
                    suggested_fix="Remove runs that cheat and keep the cheat rate below 20%.",
                )
            )
            issues.append(f"cheat rate {cheat_rate:.0%}")
    if metrics.get("unfair_labeled") and int(metrics.get("unfair_runs") or 0) > 0:
        findings.append(
            Finding(
                phase="5",
                severity="MAJOR",
                finding="Task flagged unfair or broken",
                evidence=f"{metrics['unfair_runs']} run(s) flagged the task as unfair or broken",
                suggested_fix="Fix the prompt or tests so no run calls the task unfair or broken.",
            )
        )
        issues.append("unfair task")
    if metrics.get("blocker_labeled") and int(metrics.get("blocker_runs") or 0) > 0:
        findings.append(
            Finding(
                phase="5",
                severity="MAJOR",
                finding="Run blocked by environment or verifier",
                evidence=(
                    f"{metrics['blocker_runs']} run(s) blocked by the environment or verifier"
                ),
                suggested_fix="Clear the environment or verifier blocker and rerun those agents.",
            )
        )
        issues.append("environment blocker")

    blocking = [item for item in findings if item.severity in {"BLOCKER", "MAJOR"}]
    unverified: list[str] = []
    if not metrics.get("cheat_labeled"):
        unverified.append("cheat rate")
    if not metrics.get("unfair_labeled"):
        unverified.append("fair-task flags")
    if not metrics.get("blocker_labeled"):
        unverified.append("environment or verifier blockers")

    def _notes() -> str:
        parts: list[str] = []
        if not metrics.get("hints_marked"):
            parts.append(
                "Hint markers were not on the panel; visible runs were treated as unhinted."
            )
        if unverified:
            parts.append("Not on the page: " + ", ".join(unverified) + ".")
        if (
            quest == "olympus"
            and metrics.get("sequence_length_labeled")
            and sequence_average is not None
            and float(sequence_average) > nova_min_sequence_tokens
        ):
            average = float(sequence_average)
            parts.append(
                f"Nova average sequence length {average:,.0f} "
                f"(scope share rises through {nova_payout_sequence_cap:,.0f})."
            )
        return " ".join(parts)

    if blocking:
        return (
            PhaseResult(
                status="FAIL",
                summary="Agent run platform checks failed: " + "; ".join(issues),
            ),
            findings,
        )

    if long_horizon_unverified:
        summary = (
            "Solve bar met"
            + (f" ({breakdown})" if breakdown else "")
            + ". Long horizon unverified: Nova sequence length was not labeled on the panel."
        )
        extra = _notes()
        if extra:
            summary = f"{summary} {extra}"
        return PhaseResult(status="SKIP", summary=summary), findings

    summary = "Agent run platform checks passed"
    if breakdown:
        summary += f": passes per agent {breakdown}"
    extra = _notes()
    if extra:
        summary += ". " + extra
    return PhaseResult(status="PASS", summary=summary), findings
