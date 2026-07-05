# LangGraph review agent
from __future__ import annotations

import json
import re
import time
from typing import Annotated, Any, TypedDict

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import create_react_agent

from review.activity import log_activity, timed_step
from review.config import ReviewConfig
from review.rubric_defaults import (
    APPROVE_BLOCKING_BAND_SCORES,
    APPROVE_MIN_BAND_SCORE,
    SEVERITY_BLOCK_APPROVE,
)
from review.context import build_submission_summary
from review.agent_runs_checks import evaluate_agent_runs_phase5
from review.downgrade import apply_downgrade_logic, evaluate_loc_phase4
from review.loc import compute_effective_loc, format_loc_analysis
from review.review_phases import (
    any_phase_failed,
    dicts_to_phase_results,
    dry_run_phase_results,
    ensure_all_phase_results,
    merge_deterministic_phase0,
    phase0_to_phase_result,
    run_phase0,
)
from review.prompts import (
    FINALIZE_PHASE_INSTRUCTIONS,
    PHASE_CHECKLIST,
    UNIFIED_REVIEW_SYSTEM_PROMPT,
    build_holistic_check_prompt_section,
    build_unified_review_user_prompt,
)
from review.result import (
    mark_review_complete,
    mark_review_incomplete,
    review_failure_reason,
)
from review.schemas import (
    BandRating,
    BandRatings,
    Finding,
    PhaseResult,
    ReviewResult,
)
from review.scrape import (
    scrape_review_page_context,
    unavailable_scrape_context,
    unavailable_scrape_page_context,
)
from review.tools import make_review_tools
from review.token_budget import estimate_tokens, truncate_text


class ReviewState(TypedDict, total=False):
    repo_path: str
    quest: str
    review_url: str
    page: Any
    config: ReviewConfig
    submission_summary: dict
    phase0_result: Any
    phase0_log: str
    phase_results: dict[str, dict]
    findings: list[dict]
    explore_messages: Annotated[list[BaseMessage], add_messages]
    explore_notes: str
    force_request_changes: bool
    holistic_check: dict[str, Any]
    agent_runs_data: dict[str, Any]
    scrape_context: dict[str, str]
    loc_info: dict
    loc_analysis: str
    review_result: dict
    error: str


def _holistic_check_notes(scrape: dict[str, str]) -> str:
    """Human-readable holistic check summary for ReviewResult fields."""
    if scrape.get("holistic_check_available") != "true":
        return scrape.get(
            "holistic_check_raw",
            "not available — run via orchestrator with browser session",
        )
    parts = [
        f"Status: {scrape.get('holistic_check_status', 'UNKNOWN')}",
    ]
    checklist = scrape.get("holistic_check_checklist", "").strip()
    if checklist:
        parts.append(f"Checklist: {checklist}")
    notes = scrape.get("holistic_check_reviewer_notes", "").strip()
    if notes:
        parts.append(f"Reviewer Notes: {notes}")
    return "\n".join(parts)


def _load_rubric_excerpt(rubric_path: str, *, max_chars: int = 16_000) -> str:
    """Load rubric with decision rules prepended; truncate middle if needed."""
    preamble = (
        f"{FINALIZE_PHASE_INSTRUCTIONS.strip()}\n\n"
        f"{PHASE_CHECKLIST.strip()}\n\n"
        "--- shipd-rubric.md ---\n"
    )
    try:
        text = open(rubric_path, encoding="utf-8").read()
    except OSError:
        return preamble + "(shipd-rubric.md not found)"
    body_budget = max(max_chars - len(preamble), 4000)
    if len(text) <= body_budget:
        return preamble + text
    head = text[: body_budget // 2]
    tail = text[-body_budget // 2 :]
    return preamble + head + "\n\n… [middle truncated] …\n\n" + tail


def _default_band(score: int = 1, *, confidence: str = "high", reasoning: str) -> BandRating:
    return BandRating(score=score, confidence=confidence, reasoning=reasoning)  # type: ignore[arg-type]


# --- Phase coverage detection (keeps the explore agent from skipping phases) ---

_PHASE_LABELS: dict[str, str] = {
    "0": "setup / ground truth",
    "1": "problem description",
    "2": "harness (Dockerfile/test.sh)",
    "3": "tests",
    "4": "solution / LOC",
    "5": "agent runs",
    "6": "platform / related",
}

# Phases always evaluable from the cloned repo (their artifacts are local).
_ARTIFACT_PHASES: tuple[str, ...] = ("1", "2", "3")

_PHASE_STATUS_RE = re.compile(
    r"(?:phase\s*)?([0-6])\b[^\n]*?\b(PASS|FAIL|SKIP)\b", re.IGNORECASE
)


def _scrape_value_present(value: Any) -> bool:
    """True when a scraped panel string carries real data (not a placeholder)."""
    text = str(value or "").strip().lower()
    return bool(text) and text not in {"not available", "none", "n/a", ""}


def _agent_runs_available(scrape: dict[str, str]) -> bool:
    return _scrape_value_present(scrape.get("agent_runs"))


def _platform_data_available(scrape: dict[str, str]) -> bool:
    return (
        _scrape_value_present(scrape.get("related_submissions"))
        or scrape.get("holistic_check_available") == "true"
    )


def _required_coverage_phases(scrape: dict[str, str]) -> set[str]:
    """Phases the agent must have evidence for, given what data is available."""
    required = set(_ARTIFACT_PHASES)
    if _agent_runs_available(scrape):
        required.add("5")
    if _platform_data_available(scrape):
        required.add("6")
    return required


def _final_assistant_text(messages: list[BaseMessage]) -> str:
    """Text of the last assistant message without tool calls (the summary)."""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and not msg.tool_calls:
            text = msg.content if isinstance(msg.content, str) else str(msg.content)
            if text.strip():
                return text.strip()
    return ""


def _parse_phase_verdicts(summary: str) -> dict[str, str]:
    """Map phase number -> PASS/FAIL/SKIP from the agent's coverage block."""
    verdicts: dict[str, str] = {}
    for line in summary.splitlines():
        match = _PHASE_STATUS_RE.search(line)
        if match:
            verdicts.setdefault(match.group(1), match.group(2).upper())
    return verdicts


def _explore_tool_phase_evidence(messages: list[BaseMessage]) -> set[str]:
    """Phases the agent gathered tool evidence for (independent of its summary)."""
    evidence: set[str] = set()
    for msg in messages:
        if not (isinstance(msg, AIMessage) and msg.tool_calls):
            continue
        for call in msg.tool_calls:
            name = call.get("name", "")
            args = call.get("args", {}) or {}
            path = str(args.get("path", "")).lower()
            if name == "get_submission_summary":
                evidence.add("1")
            elif name == "compute_effective_loc":
                evidence.add("4")
            elif name in ("read_holistic_check", "read_shipd_review_panel"):
                evidence.update({"5", "6"})
            elif name in ("run_phase0_checks", "check_patch_apply", "get_git_info"):
                evidence.add("0")
            elif name == "read_file":
                if any(k in path for k in ("problem", "prompt", "statement", "readme")):
                    evidence.add("1")
                if "dockerfile" in path or "test.sh" in path:
                    evidence.add("2")
                if "test" in path and "patch" in path:
                    evidence.update({"2", "3"})
                elif "test" in path:
                    evidence.add("3")
                if "solution" in path:
                    evidence.add("4")
    return evidence


def _explore_coverage_gaps(
    messages: list[BaseMessage], scrape: dict[str, str]
) -> list[str]:
    """Required phases with neither a PASS/FAIL verdict nor tool evidence."""
    required = _required_coverage_phases(scrape)
    verdicts = _parse_phase_verdicts(_final_assistant_text(messages))
    evidence = _explore_tool_phase_evidence(messages)
    gaps = []
    for phase in sorted(required):
        covered = verdicts.get(phase) in ("PASS", "FAIL") or phase in evidence
        if not covered:
            gaps.append(phase)
    return gaps


_COVERAGE_PHASE_INSTRUCTIONS: dict[str, str] = {
    "1": "Phase 1 (problem description): read the problem statement and judge clarity, repo fit, and AI-slop.",
    "2": "Phase 2 (harness): read the Dockerfile and test.sh; check minimal image and the base/new split.",
    "3": "Phase 3 (tests): read test.patch; check coverage, determinism, no network, and behaviour-not-implementation.",
    "5": "Phase 5 (agent runs): call read_holistic_check and read_shipd_review_panel('Agent Runs'); cite pass rate and medians.",
    "6": "Phase 6 (platform): call read_shipd_review_panel('Related Submissions') and read_holistic_check; note duplicate/overlap and Mars-vs-Olympus fit.",
}


def _build_coverage_followup_prompt(gaps: list[str]) -> str:
    steps = "\n".join(
        f"- {_COVERAGE_PHASE_INSTRUCTIONS.get(p, f'Phase {p}: inspect the relevant artifacts.')}"
        for p in gaps
    )
    return (
        "Coverage check: you have not gathered evidence for rubric phase(s) "
        f"{', '.join(gaps)}. Do not skip a phase whose data exists. Inspect them now:\n"
        f"{steps}\n\n"
        "Use tools to read the artifacts, then re-emit the '## Phase coverage' block "
        "with a `Phase N: PASS|FAIL|SKIP` line for every phase 0–6."
    )


def scrape_node(state: ReviewState) -> dict:
    """Scrape Shipd review page panels before LLM review when a page is available."""
    page = state.get("page")
    if page is not None:
        with timed_step("scraping Shipd review page panels", category="review"):
            ctx = scrape_review_page_context(page)
        out = {
            "holistic_check": ctx.get("holistic_check", {}),
            "agent_runs_data": ctx.get("agent_runs_data", {}),
            "scrape_context": {
                "agent_runs": ctx.get("agent_runs", "not available"),
                "related_submissions": ctx.get("related_submissions", "not available"),
                "holistic_check_available": ctx.get("holistic_check_available", "false"),
                "holistic_check_status": ctx.get("holistic_check_status", ""),
                "holistic_check_checklist": ctx.get("holistic_check_checklist", ""),
                "holistic_check_reviewer_notes": ctx.get(
                    "holistic_check_reviewer_notes", ""
                ),
                "holistic_check_raw": ctx.get("holistic_check_raw", ""),
            },
        }
        out.update(
            _run_agent_runs_analysis(
                {
                    **state,
                    "agent_runs_data": out["agent_runs_data"],
                    "phase_results": dict(state.get("phase_results", {})),
                    "findings": list(state.get("findings", [])),
                }
            )
        )
        return out

    if state.get("scrape_context"):
        return {}

    stub = unavailable_scrape_page_context()
    return {
        "holistic_check": stub.get("holistic_check", {}),
        "agent_runs_data": stub.get("agent_runs_data", {}),
        "scrape_context": unavailable_scrape_context(),
    }


def _run_agent_runs_analysis(state: ReviewState) -> dict:
    """Run deterministic Phase 5 agent run platform checks when scraped."""
    quest = state.get("quest", "olympus")
    agent_runs_data = state.get("agent_runs_data") or {}
    phase5_result, phase5_findings = evaluate_agent_runs_phase5(
        agent_runs_data,
        quest=quest,
    )
    phase_results = dict(state.get("phase_results", {}))
    phase_results["5"] = phase5_result.model_dump()
    findings = list(state.get("findings", []))
    findings.extend(f.model_dump() for f in phase5_findings)
    return {"phase_results": phase_results, "findings": findings}


def _run_loc_analysis(state: ReviewState) -> dict:
    """Run deterministic effective LOC analysis and Phase 4 LOC check."""
    from pathlib import Path

    config: ReviewConfig = state["config"]
    repo_path = Path(state["repo_path"])
    quest = state.get("quest", "olympus")
    loc_info = compute_effective_loc(repo_path)
    phase4_result, loc_findings, _ = evaluate_loc_phase4(
        loc_info,
        quest=quest,
        olympus_min_loc=config.olympus_min_loc,
        mars_min_loc=config.mars_min_loc,
        mars_max_loc=config.mars_max_loc,
    )
    loc_analysis = format_loc_analysis(
        loc_info,
        quest=quest,
        olympus_min=config.olympus_min_loc,
        mars_min=config.mars_min_loc,
        mars_max=config.mars_max_loc,
    )
    phase_results = dict(state.get("phase_results", {}))
    phase_results["4"] = phase4_result.model_dump()
    findings = list(state.get("findings", []))
    findings.extend(f.model_dump() for f in loc_findings)
    return {
        "loc_info": loc_info,
        "phase_results": phase_results,
        "findings": findings,
        "loc_analysis": loc_analysis,
    }


def _run_phase0_into_state(state: ReviewState) -> dict:
    """Run deterministic Phase 0 checks and populate state fields."""
    from pathlib import Path

    repo_path = Path(state["repo_path"])
    summary = state.get("submission_summary", {})
    config: ReviewConfig = state["config"]
    with timed_step(
        f"Phase 0 checks ({config.review_phase0} tier) on {repo_path.name}",
        category="phase0",
    ):
        phase0 = run_phase0(
            repo_path,
            artifacts=summary.get("artifacts"),
            commit=summary.get("commit"),
            run_tests=config.review_phase0 != "fast",
            test_timeout=config.review_phase0_test_timeout,
            build_timeout=config.review_phase0_docker_build_timeout,
        )
    log_activity(
        f"Phase 0 result: {phase0.status} — {phase0.summary}", category="phase0"
    )
    phase_results = dict(state.get("phase_results", {}))
    phase_results["0"] = phase0_to_phase_result(phase0).model_dump()
    findings = [f.model_dump() for f in phase0.findings]
    out = {
        "phase0_result": phase0,
        "phase0_log": phase0.phase0_log,
        "phase_results": phase_results,
        "findings": findings,
        "force_request_changes": phase0.critical_fail,
    }
    out.update(_run_loc_analysis({**state, **out}))
    return out


def _dry_run_result(state: ReviewState) -> dict:
    phase0 = state.get("phase0_result")
    summary = "Dry run: Phase 0 deterministic checks only; LLM review skipped."
    if phase0 and getattr(phase0, "critical_fail", False):
        summary = (
            "Dry run: Phase 0 critical failures detected; LLM review skipped."
        )

    findings = []
    if phase0:
        findings = [f.model_dump() for f in getattr(phase0, "findings", [])]

    phase0_dict = state.get("phase_results", {}).get("0", {})
    phase_results = dry_run_phase_results(phase0_dict)
    phase4_dict = state.get("phase_results", {}).get("4")
    if phase4_dict:
        phase_results["4"] = PhaseResult(**phase4_dict)

    quest = state.get("quest", "olympus")
    loc_analysis = state.get("loc_analysis") or format_loc_analysis(
        state.get("loc_info") or {},
        quest=quest,
        olympus_min=state.get("config").olympus_min_loc,
        mars_min=state.get("config").mars_min_loc,
        mars_max=state.get("config").mars_max_loc,
    )
    result = ReviewResult(
        decision="request_changes",
        band_ratings=BandRatings(
            problem=_default_band(
                reasoning="Dry run — band not evaluated; re-run without --dry-run."
            ),
            tests=_default_band(
                reasoning="Dry run — band not evaluated; re-run without --dry-run."
            ),
            solution=_default_band(
                reasoning="Dry run — band not evaluated; re-run without --dry-run."
            ),
        ),
        phase_results=phase_results,
        findings=[Finding(**f) for f in findings],
        loc_analysis=loc_analysis,
        recommendation_summary=summary,
        contributor_feedback=(
            "Dry run mode: no LLM review was performed. "
            "Fix any Phase 0 issues noted in phase_results before a full review."
        ),
        internal_notes="REVIEW_DRY_RUN=1 or --dry-run",
        agent_run_notes=state.get("scrape_context", {}).get("agent_runs", "not available"),
        related_submissions_notes=state.get("scrape_context", {}).get(
            "related_submissions", "not available"
        ),
        holistic_check_notes=_holistic_check_notes(state.get("scrape_context", {})),
    )
    if quest == "olympus" and state.get("loc_info"):
        result = apply_downgrade_logic(
            result,
            state["loc_info"],
            quest=quest,
            olympus_min_loc=state["config"].olympus_min_loc,
            mars_min_loc=state["config"].mars_min_loc,
            mars_max_loc=state["config"].mars_max_loc,
        )
    if quest == "olympus":
        result = result.model_copy(update={"repo_eligible": None, "solvability_ok": None})
    else:
        result = result.model_copy(update={"quality": None, "difficulty": None})
    return mark_review_incomplete(result.to_submit_dict(), error="REVIEW_DRY_RUN")


def unified_review_node(state: ReviewState) -> dict:
    """Run Phase 0 checks, then a single LLM explore pass for phases 0–6."""
    phase0_updates = _run_phase0_into_state(state)
    config: ReviewConfig = state["config"]

    if config.review_dry_run:
        return {
            **phase0_updates,
            "explore_notes": "Skipped LLM review: dry run mode.",
        }

    if (
        config.review_skip_explore_on_phase0_fail
        and phase0_updates.get("force_request_changes")
    ):
        return {
            **phase0_updates,
            "explore_notes": (
                "Skipped LLM review: Phase 0 critical FAIL "
                "(REVIEW_SKIP_EXPLORE_ON_PHASE0_FAIL). "
                "Finalize from Phase 0 findings and artifact paths."
            ),
        }

    if not config.anthropic_api_key:
        return {
            **phase0_updates,
            "error": "ANTHROPIC_API_KEY is not set.",
            "explore_notes": "Unified review skipped: missing API key.",
        }

    from pathlib import Path

    repo_path = Path(state["repo_path"])
    phase0_result = phase0_updates.get("phase0_result")
    # Playwright's sync API is greenlet-bound to the main thread, but LangGraph
    # runs parallel tool calls on worker threads — never hand the live page to
    # explore tools ("cannot switch to a different thread"). scrape_node already
    # captured every panel on the main thread; tools serve that cache.
    tools = make_review_tools(
        repo_path,
        quest=state["quest"],
        review_url=state.get("review_url", ""),
        cached_summary=state.get("submission_summary"),
        page=None,
        cached_scrape=state.get("scrape_context"),
        cached_holistic=state.get("holistic_check"),
        cached_phase0=phase0_result,
        config=config,
    )
    llm = ChatAnthropic(
        model=config.review_explore_model,
        api_key=config.anthropic_api_key,
        max_tokens=config.review_explore_max_output_tokens,
    )
    agent = create_react_agent(llm, tools)

    summary = state.get("submission_summary", {})
    scrape = state.get("scrape_context", {})
    holistic_section = build_holistic_check_prompt_section(scrape)
    phase0_status = phase0_updates.get("phase_results", {}).get("0", {}).get(
        "status", "UNKNOWN"
    )
    loc_analysis = phase0_updates.get("loc_analysis", "")
    panel_cap = config.review_scrape_panel_max_chars
    phase0_log = truncate_text(
        phase0_updates.get("phase0_log", ""),
        config.review_phase0_log_max_chars,
        label="phase0 log",
    )
    user_prompt = build_unified_review_user_prompt(
        quest=state["quest"],
        repo_path=str(summary.get("repo_path", repo_path)),
        commit=summary.get("commit"),
        phase0_log=phase0_log,
        phase0_status=phase0_status,
        agent_runs=truncate_text(
            scrape.get("agent_runs", "not available"),
            panel_cap,
            label="agent runs",
        ),
        related_submissions=truncate_text(
            scrape.get("related_submissions", "not available"),
            panel_cap,
            label="related submissions",
        ),
        holistic_check=holistic_section,
        loc_analysis=loc_analysis,
        olympus_min_loc=config.olympus_min_loc,
        mars_min_loc=config.mars_min_loc,
        mars_max_loc=config.mars_max_loc,
        max_tool_steps=config.review_max_tool_steps,
    )
    log_activity(
        f"explore prompt ~{estimate_tokens(user_prompt):,} tokens (est.)",
        category="review",
    )

    log_activity(
        f"explore agent starting (model {config.review_explore_model}, "
        f"budget ~{config.review_max_tool_steps} tool steps, "
        "phases 0–6)",
        category="review",
    )
    started = time.monotonic()
    messages: list[BaseMessage] = []
    budget_exhausted = False
    # Each ReAct tool step spends two graph super-steps (agent node + tools
    # node), plus one final agent turn with no tool calls: 2N + 1 to finish
    # a full budget. Headroom covers the prebuilt remaining_steps fallback.
    recursion_limit = 2 * config.review_max_tool_steps + 5
    try:
        for chunk in agent.stream(
            {
                "messages": [
                    SystemMessage(content=UNIFIED_REVIEW_SYSTEM_PROMPT),
                    HumanMessage(content=user_prompt),
                ]
            },
            config={"recursion_limit": recursion_limit},
            stream_mode="values",
        ):
            new_messages = chunk.get("messages", [])
            for msg in new_messages[len(messages):]:
                _log_agent_message(msg)
            messages = new_messages
    except GraphRecursionError:
        # Budget exhausted mid-loop: keep the partial transcript and let
        # finalize score from what was gathered instead of failing the run.
        budget_exhausted = True
        log_activity(
            "explore budget exhausted (recursion limit "
            f"{recursion_limit}) — salvaging partial transcript",
            category="review",
        )
    except Exception as exc:
        return {
            **phase0_updates,
            "error": f"Explore phase failed: {exc}",
            "explore_notes": f"Unified review agent error: {exc}",
        }

    if budget_exhausted and not any(
        isinstance(m, AIMessage) for m in messages
    ):
        return {
            **phase0_updates,
            "error": "Explore phase hit the recursion limit before any agent output.",
            "explore_notes": "Unified review agent produced no messages.",
        }

    # Coverage recheck: if the agent skipped a phase whose data exists, re-prompt
    # the SAME conversation to gather that evidence before finalize scores it.
    # Only runs when budget remained (a fresh exhaustion would just re-trip) and
    # is bounded by its own small step budget.
    if (
        config.review_coverage_recheck
        and not budget_exhausted
        and any(isinstance(m, AIMessage) for m in messages)
    ):
        gaps = _explore_coverage_gaps(messages, scrape)
        if gaps:
            log_activity(
                "coverage recheck: no evidence for phase(s) "
                f"{', '.join(gaps)} — re-exploring "
                f"(≤{config.review_coverage_recheck_max_steps} steps)",
                category="review",
            )
            recheck_limit = 2 * config.review_coverage_recheck_max_steps + 3
            try:
                for chunk in agent.stream(
                    {"messages": messages + [HumanMessage(content=_build_coverage_followup_prompt(gaps))]},
                    config={"recursion_limit": recheck_limit},
                    stream_mode="values",
                ):
                    new_messages = chunk.get("messages", [])
                    for msg in new_messages[len(messages):]:
                        _log_agent_message(msg)
                    messages = new_messages
                remaining = _explore_coverage_gaps(messages, scrape)
                if remaining:
                    log_activity(
                        "coverage recheck: phase(s) "
                        f"{', '.join(remaining)} still uncovered — validate will flag",
                        category="review",
                    )
            except GraphRecursionError:
                log_activity(
                    "coverage recheck hit its step limit — using best-effort evidence",
                    category="review",
                )
            except Exception as exc:
                log_activity(f"coverage recheck failed: {exc}", category="review")
        else:
            log_activity(
                "coverage recheck: all required phases have evidence",
                category="review",
            )

    elapsed = time.monotonic() - started
    tool_calls = sum(
        len(m.tool_calls) for m in messages
        if isinstance(m, AIMessage) and m.tool_calls
    )
    log_activity(
        f"explore agent finished in {elapsed:.1f}s ({tool_calls} tool calls)",
        category="review",
    )
    notes = _summarize_explore_messages(
        messages,
        max_chars=config.review_explore_transcript_max_chars,
        tool_output_max_chars=config.review_tool_output_max_chars,
    )
    if budget_exhausted:
        notes = (
            "WARNING: the explore agent ran out of its tool-step budget before "
            "writing final notes; below is a partial transcript. Score phases "
            "from the evidence present and use lower confidence where coverage "
            "is missing.\n\n" + notes
        )
    log_activity(
        f"explore transcript ~{estimate_tokens(notes):,} tokens (est.)",
        category="review",
    )
    return {
        **phase0_updates,
        "explore_messages": messages,
        "explore_notes": notes,
    }


def finalize_node(state: ReviewState) -> dict:
    if state.get("config").review_dry_run:
        return {"review_result": _dry_run_result(state)}

    if state.get("error"):
        return {"review_result": _fallback_result(state, state["error"])}

    config: ReviewConfig = state["config"]
    if not config.anthropic_api_key:
        return {
            "review_result": _fallback_result(state, "ANTHROPIC_API_KEY is not set."),
        }

    rubric = _load_rubric_excerpt(
        config.rubric_path,
        max_chars=config.review_rubric_max_chars,
    )
    summary = state.get("submission_summary", {})
    quest = state.get("quest", "olympus")
    scrape = state.get("scrape_context", {})

    phase0_result = state.get("phase_results", {}).get("0", {})
    human_payload = {
        "quest": quest,
        "review_url": state.get("review_url", ""),
        "repo_path": summary.get("repo_path"),
        "commit": summary.get("commit"),
        "artifacts": summary.get("artifacts"),
        "phase0_log": truncate_text(
            state.get("phase0_log", ""),
            config.review_phase0_log_max_chars,
            label="phase0 log",
        ),
        "phase0_result": phase0_result,
        "phase_results_so_far": state.get("phase_results", {}),
        "findings_so_far": state.get("findings", []),
        "explore_notes": state.get("explore_notes", ""),
        "holistic_ai_check": build_holistic_check_prompt_section(scrape),
        "agent_runs": truncate_text(
            scrape.get("agent_runs", "not available"),
            config.review_scrape_panel_max_chars,
            label="agent runs",
        ),
        "related_submissions": truncate_text(
            scrape.get("related_submissions", "not available"),
            config.review_scrape_panel_max_chars,
            label="related submissions",
        ),
        "force_request_changes": state.get("force_request_changes", False),
        "loc_analysis": state.get("loc_analysis", ""),
        "loc_info": state.get("loc_info", {}),
        "olympus_min_loc": config.olympus_min_loc,
        "mars_min_loc": config.mars_min_loc,
        "mars_max_loc": config.mars_max_loc,
    }
    human_content = truncate_text(
        json.dumps(human_payload, indent=2),
        config.review_finalize_payload_max_chars,
        label="finalize payload",
    )

    system = (
        "You are the Shipd autonomous review agent. Follow shipd-rubric.md exactly.\n"
        "Evaluate all rubric phases 0–6 and output structured ReviewResult fields.\n"
        f"--- rubric excerpt ---\n{rubric}"
    )
    log_activity(
        f"finalize input ~{estimate_tokens(system + human_content):,} tokens (est.)",
        category="review",
    )

    llm = ChatAnthropic(
        model=config.review_model,
        api_key=config.anthropic_api_key,
        max_tokens=config.review_finalize_max_output_tokens,
    ).with_structured_output(ReviewResult)

    log_activity(
        f"finalize: structuring review with {config.review_model} "
        "(rubric phases 0–6, band scores, findings)",
        category="review",
    )
    started = time.monotonic()
    last_error: Exception | None = None
    review: ReviewResult | None = None
    for attempt in range(2):
        try:
            review = llm.invoke(
                [
                    SystemMessage(content=system),
                    HumanMessage(content=human_content),
                ]
            )
            break
        except Exception as exc:
            last_error = exc
            log_activity(
                f"finalize attempt {attempt + 1} failed: {exc}", category="review"
            )
            human_content += f"\n\nPrevious validation error (fix output): {exc}"

    if review is not None:
        log_activity(
            f"finalize done in {time.monotonic() - started:.1f}s "
            f"(decision={review.decision})",
            category="review",
        )

    if review is None:
        return {
            "review_result": _fallback_result(
                state,
                f"Structured finalize failed: {last_error}",
            ),
        }

    merged_phases = dicts_to_phase_results(
        merge_deterministic_phase0(
            {k: v for k, v in review.phase_results.items()},
            phase0_result,
        )
    )
    review = review.model_copy(update={"phase_results": merged_phases})

    updates: dict[str, Any] = {}
    if state.get("force_request_changes") and review.decision == "approve":
        updates["decision"] = "request_changes"
        updates["recommendation_summary"] = (
            "Phase 0 critical failure — cannot approve. "
            + review.recommendation_summary
        )
    if updates:
        review = review.model_copy(update=updates)

    scrape = state.get("scrape_context", {})
    scrape_updates: dict[str, Any] = {}
    if scrape:
        if not review.holistic_check_notes.strip() or review.holistic_check_notes == "not available":
            scrape_updates["holistic_check_notes"] = _holistic_check_notes(scrape)
        agent_runs = scrape.get("agent_runs", "")
        if (
            not review.agent_run_notes.strip()
            or review.agent_run_notes == "not available"
        ) and agent_runs and agent_runs != "not available":
            scrape_updates["agent_run_notes"] = agent_runs
        related = scrape.get("related_submissions", "")
        if (
            not review.related_submissions_notes.strip()
            or review.related_submissions_notes == "not available"
        ) and related and related != "not available":
            scrape_updates["related_submissions_notes"] = related
    if scrape_updates:
        review = review.model_copy(update=scrape_updates)

    loc_info = state.get("loc_info") or {}
    loc_analysis = state.get("loc_analysis") or format_loc_analysis(
        loc_info,
        quest=quest,
        olympus_min=config.olympus_min_loc,
        mars_min=config.mars_min_loc,
        mars_max=config.mars_max_loc,
    )
    loc_updates: dict[str, Any] = {}
    if not review.loc_analysis.strip():
        loc_updates["loc_analysis"] = loc_analysis
    elif loc_analysis and loc_analysis not in review.loc_analysis:
        loc_updates["loc_analysis"] = f"{review.loc_analysis.strip()}\n{loc_analysis}".strip()

    phase4_det = state.get("phase_results", {}).get("4")
    if phase4_det:
        merged_phases = dict(review.phase_results)
        merged_phases["4"] = PhaseResult(**phase4_det)
        loc_updates["phase_results"] = merged_phases

    existing_finding_keys = {
        (f.phase, f.finding) for f in review.findings
    }
    loc_findings = [
        f for f in state.get("findings", [])
        if isinstance(f, dict)
        and f.get("phase") == "4"
        and ("4", f.get("finding", "")) not in existing_finding_keys
    ]
    if loc_findings:
        loc_updates["findings"] = [
            *review.findings,
            *[Finding(**f) for f in loc_findings],
        ]

    phase5_det = state.get("phase_results", {}).get("5")
    if phase5_det:
        merged_phases = dict(loc_updates.get("phase_results", review.phase_results))
        merged_phases["5"] = PhaseResult(**phase5_det)
        loc_updates["phase_results"] = merged_phases
        phase5_findings = [
            f for f in state.get("findings", [])
            if isinstance(f, dict)
            and f.get("phase") == "5"
            and ("5", f.get("finding", "")) not in existing_finding_keys
        ]
        if phase5_findings:
            base_findings = loc_updates.get("findings", review.findings)
            loc_updates["findings"] = [
                *base_findings,
                *[Finding(**f) for f in phase5_findings],
            ]

    if loc_updates:
        review = review.model_copy(update=loc_updates)

    return {"review_result": mark_review_complete(review.to_submit_dict())}


def _flag_coverage_gaps(review: ReviewResult, gap_phases: list[str]) -> ReviewResult:
    """Flag phases that could not be evaluated and force request_changes.

    Policy (chosen with the user): never block a submit on a coverage gap —
    submit a review that admits the gap and asks for changes, rather than
    silently skipping the factor or discarding the whole review.
    """
    names = ", ".join(f"Phase {p} ({_PHASE_LABELS.get(p, p)})" for p in gap_phases)
    note = (
        f"Coverage gap — not evaluated: {names}. "
        "Treated as request_changes (flagged, not silently skipped)."
    )
    updates: dict[str, Any] = {}
    # A gap can never support approve; escalate approve → request_changes but
    # leave an existing reject alone (reject is the stronger verdict).
    if review.decision == "approve":
        updates["decision"] = "request_changes"

    internal = review.internal_notes.strip()
    updates["internal_notes"] = f"{internal}\n{note}".strip() if internal else note

    feedback_line = (
        f"Note: {names} could not be fully evaluated automatically — "
        "please double-check before relying on this review."
    )
    feedback = review.contributor_feedback.strip()
    if feedback_line not in feedback:
        updates["contributor_feedback"] = (
            f"{feedback}\n{feedback_line}".strip() if feedback else feedback_line
        )

    updates["recommendation_summary"] = (
        f"Coverage gap ({names}). {review.recommendation_summary}".strip()
    )
    return review.model_copy(update=updates)


def validate_node(state: ReviewState) -> dict:
    raw = state.get("review_result")
    if not raw:
        return {"review_result": _fallback_result(state, "No review result produced.")}

    try:
        review = ReviewResult.model_validate(raw)
    except Exception as exc:
        return {"review_result": _fallback_result(state, f"Invalid review result: {exc}")}

    review = _apply_rubric_guards(review, state)

    config: ReviewConfig = state["config"]
    loc_info = state.get("loc_info") or {}
    if loc_info:
        review = apply_downgrade_logic(
            review,
            loc_info,
            quest=state.get("quest", "olympus"),
            olympus_min_loc=config.olympus_min_loc,
            mars_min_loc=config.mars_min_loc,
            mars_max_loc=config.mars_max_loc,
        )
        if not review.loc_analysis.strip():
            review = review.model_copy(
                update={
                    "loc_analysis": format_loc_analysis(
                        loc_info,
                        quest=state.get("quest", "olympus"),
                        olympus_min=config.olympus_min_loc,
                        mars_min=config.mars_min_loc,
                        mars_max=config.mars_max_loc,
                    )
                }
            )

    result_dict = review.to_submit_dict()
    if raw.get("review_complete") is False:
        error = str(raw.get("review_error", "")).strip() or review_failure_reason(raw)
        return {
            "review_result": mark_review_incomplete(result_dict, error=error),
        }

    # Coverage gaps: a phase whose data exists must not come back SKIP. Phases
    # 1-3 (problem, harness, tests) and 4 (solution/LOC) are always evaluable
    # from the cloned repo; 5/6 only when their panels were scraped. Rather than
    # blocking, flag the gap and force request_changes so a review still ships.
    scrape = state.get("scrape_context", {})
    evaluable = set(_ARTIFACT_PHASES) | {"4"}
    if _agent_runs_available(scrape):
        evaluable.add("5")
    if _platform_data_available(scrape):
        evaluable.add("6")
    phase_status = result_dict.get("phase_results", {})
    gap_phases = [
        key
        for key in sorted(evaluable)
        if phase_status.get(key, {}).get("status") == "SKIP"
    ]
    if gap_phases:
        review = _flag_coverage_gaps(review, gap_phases)
        result_dict = review.to_submit_dict()
        log_activity(
            "validate: coverage gap on phase(s) "
            f"{', '.join(gap_phases)} — forcing request_changes and flagging",
            category="review",
        )

    return {"review_result": mark_review_complete(result_dict)}


# Band ← governing rubric phase(s): a FAIL there caps the band score.
_BAND_GOVERNING_PHASES: dict[str, tuple[str, ...]] = {
    "problem": ("1",),
    "tests": ("2", "3"),
    "solution": ("4",),
}


def _augment_reasoning(existing: str, note: str) -> str:
    existing = (existing or "").strip()
    if not existing:
        return note
    if note in existing:
        return existing
    return f"{existing} {note}"


def _cap_bands_to_deterministic(
    review: ReviewResult, phase_results: dict[str, PhaseResult]
) -> tuple[BandRatings | None, list[str]]:
    """Lower band scores that contradict a FAIL phase or a blocking finding.

    Deterministic verdicts (LOC/Phase 4, agent runs, Phase 0 test contract) must
    show up in the visible band score — not just internal notes — so a LOC FAIL
    can't sit next to a "Solution 3/Clean" band. Only ever lowers, never raises.
    """
    failed = set(any_phase_failed(phase_results))
    blocking_by_phase: dict[str, set[str]] = {}
    for finding in review.findings:
        if finding.severity in SEVERITY_BLOCK_APPROVE:
            blocking_by_phase.setdefault(str(finding.phase), set()).add(finding.severity)

    band_updates: dict[str, BandRating] = {}
    reasons: list[str] = []
    for band_name, phases in _BAND_GOVERNING_PHASES.items():
        band = getattr(review.band_ratings, band_name)
        if band.score <= 1:
            continue
        fail_hit = [p for p in phases if p in failed]
        block_hit = sorted({s for p in phases for s in blocking_by_phase.get(p, set())})
        if fail_hit:
            cause = f"phase {'/'.join(fail_hit)} FAIL"
        elif block_hit:
            cause = f"open {'/'.join(block_hit)} finding"
        else:
            continue
        reason = _augment_reasoning(
            band.reasoning, f"Score capped at 1 — {cause} (deterministic)."
        )
        band_updates[band_name] = band.model_copy(update={"score": 1, "reasoning": reason})
        reasons.append(f"{band_name} band capped to 1 ({cause})")

    if not band_updates:
        return None, []
    return review.band_ratings.model_copy(update=band_updates), reasons


def _apply_rubric_guards(review: ReviewResult, state: ReviewState) -> ReviewResult:
    updates: dict[str, Any] = {}
    reasons: list[str] = []

    phase_results = dicts_to_phase_results(ensure_all_phase_results(review.phase_results))
    updates["phase_results"] = phase_results

    # Force deterministic FAIL/blocking verdicts into the visible band scores
    # before the decision checks below read them.
    capped_bands, band_reasons = _cap_bands_to_deterministic(review, phase_results)
    if capped_bands is not None:
        review = review.model_copy(update={"band_ratings": capped_bands})
        reasons.extend(band_reasons)

    failed_phases = any_phase_failed(phase_results)
    if failed_phases and review.decision == "approve":
        reasons.append(f"phase(s) FAIL: {', '.join(failed_phases)}")
        updates["decision"] = "request_changes"

    for band_name in ("problem", "tests", "solution"):
        band = getattr(review.band_ratings, band_name)
        if band.score in APPROVE_BLOCKING_BAND_SCORES and review.decision == "approve":
            reasons.append(f"{band_name} band score {band.score}")
            updates["decision"] = "request_changes"
        elif band.score < APPROVE_MIN_BAND_SCORE and review.decision == "approve":
            reasons.append(f"{band_name} band score {band.score} below minimum {APPROVE_MIN_BAND_SCORE}")
            updates["decision"] = "request_changes"

    quest = state.get("quest", "olympus")
    if quest == "olympus":
        if review.repo_eligible is False and review.decision == "approve":
            reasons.append("repo not eligible")
            updates["decision"] = "request_changes"
        if review.solvability_ok is False and review.decision == "approve":
            reasons.append("solvability concerns")
            updates["decision"] = "request_changes"
    elif quest == "mars":
        if review.quality is None or review.difficulty is None:
            notes = review.internal_notes
            updates["internal_notes"] = (
                notes + "\nMars quest: quality/difficulty should be set."
            ).strip()

    blocking_findings = [
        f for f in review.findings if f.severity in SEVERITY_BLOCK_APPROVE
    ]
    if blocking_findings and review.decision == "approve":
        severities = sorted({f.severity for f in blocking_findings})
        reasons.append(f"open {'/'.join(severities)} findings")
        updates["decision"] = "request_changes"

    if reasons:
        summary = review.recommendation_summary
        guard_note = "Guard rails: " + "; ".join(reasons)
        updates["recommendation_summary"] = f"{guard_note}. {summary}"
        internal = review.internal_notes
        updates["internal_notes"] = (internal + "\n" + guard_note).strip()

    if updates:
        return review.model_copy(update=updates)
    return review


def _fallback_result(state: ReviewState, reason: str) -> dict:
    quest = state.get("quest", "olympus")
    phase_results_raw = ensure_all_phase_results(state.get("phase_results", {}))
    phase_results = {
        k: PhaseResult(**v) if isinstance(v, dict) else v
        for k, v in phase_results_raw.items()
    }
    findings_raw = state.get("findings", [])
    findings = [Finding(**f) if isinstance(f, dict) else f for f in findings_raw]

    scrape = state.get("scrape_context", {})
    result = ReviewResult(
        decision="request_changes",
        band_ratings=BandRatings(
            problem=_default_band(reasoning=f"Review incomplete: {reason}"),
            tests=_default_band(reasoning=f"Review incomplete: {reason}"),
            solution=_default_band(reasoning=f"Review incomplete: {reason}"),
        ),
        phase_results=phase_results,
        findings=findings,
        recommendation_summary=f"Review could not complete: {reason}",
        contributor_feedback=(
            "The automated review could not finish. "
            "Please verify Phase 0 artifacts and re-run the review."
        ),
        internal_notes=reason,
        agent_run_notes=scrape.get("agent_runs", "not available"),
        related_submissions_notes=scrape.get("related_submissions", "not available"),
        holistic_check_notes=_holistic_check_notes(scrape),
    )
    if quest == "olympus":
        result = result.model_copy(update={"repo_eligible": None, "solvability_ok": None})
    return mark_review_incomplete(result.to_submit_dict(), error=reason)


def _log_agent_message(msg: BaseMessage) -> None:
    """Live-log a single explore agent message (tool call, result, or note)."""
    if isinstance(msg, AIMessage):
        for tc in msg.tool_calls or []:
            try:
                args = json.dumps(tc.get("args", {}), default=str)
            except (TypeError, ValueError):
                args = str(tc.get("args", ""))
            if len(args) > 200:
                args = args[:200] + "…"
            log_activity(f"→ {tc['name']}({args})", category="review")
        if not msg.tool_calls:
            text = msg.content if isinstance(msg.content, str) else str(msg.content)
            preview = " ".join(text.split())[:180]
            log_activity(
                f"agent notes ({len(text)} chars): {preview}…"
                if len(text) > 180
                else f"agent notes: {preview}",
                category="review",
            )
    elif isinstance(msg, ToolMessage):
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        preview = " ".join(content.split())[:160]
        suffix = "…" if len(content) > 160 else ""
        log_activity(
            f"← {msg.name or 'tool'} ({len(content)} chars): {preview}{suffix}",
            category="review",
        )


def _summarize_explore_messages(
    messages: list[BaseMessage],
    *,
    max_chars: int = 8_000,
    tool_output_max_chars: int = 600,
) -> str:
    """Condense the explore transcript for the finalize step.

    Human prompts are dropped (phase0 log, rubric, and scrape context are
    passed to finalize separately) and tool outputs are truncated so the
    finalize call stays small and fast. The agent's final phase-by-phase
    summary is preserved verbatim — it is emitted last, so a naive
    whole-transcript truncation would drop exactly the conclusions finalize
    needs and phases would read back as SKIP.
    """
    # Locate the final assistant summary (last AIMessage without tool calls).
    final_idx: int | None = None
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if isinstance(msg, AIMessage) and not msg.tool_calls:
            text = msg.content if isinstance(msg.content, str) else str(msg.content)
            if text.strip():
                final_idx = i
                break

    final_summary = ""
    if final_idx is not None:
        fmsg = messages[final_idx]
        final_summary = (
            fmsg.content if isinstance(fmsg.content, str) else str(fmsg.content)
        ).strip()

    parts: list[str] = []
    for i, msg in enumerate(messages):
        if i == final_idx or isinstance(msg, HumanMessage):
            continue
        if isinstance(msg, AIMessage):
            text = msg.content if isinstance(msg.content, str) else str(msg.content)
            if msg.tool_calls:
                tools = ", ".join(tc["name"] for tc in msg.tool_calls)
                parts.append(f"Assistant [tools: {tools}]: {text}")
            else:
                parts.append(f"Assistant: {text}")
        elif isinstance(msg, ToolMessage):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            if len(content) > tool_output_max_chars:
                content = content[:tool_output_max_chars] + "… [tool output truncated]"
            parts.append(f"Tool[{msg.name or 'tool'}]: {content}")
        else:
            parts.append(str(msg.content))

    prefix = "\n".join(parts)

    if not final_summary:
        if len(prefix) > max_chars:
            return prefix[:max_chars] + "\n… [explore transcript truncated]"
        return prefix

    summary_block = "## Final phase summary (verbatim)\n" + final_summary
    # If the summary alone fills the budget, keep it (head+tail) over the chatter.
    if len(summary_block) >= max_chars:
        return truncate_text(summary_block, max_chars, label="final summary")

    prefix_budget = max_chars - len(summary_block) - 2
    if len(prefix) > prefix_budget:
        prefix = prefix[:prefix_budget] + "\n… [earlier explore steps truncated]"
    return f"{prefix}\n\n{summary_block}" if prefix else summary_block


def build_review_graph():
    graph = StateGraph(ReviewState)
    graph.add_node("scrape", scrape_node)
    graph.add_node("unified_review", unified_review_node)
    graph.add_node("finalize", finalize_node)
    graph.add_node("validate", validate_node)

    graph.add_edge(START, "scrape")
    graph.add_edge("scrape", "unified_review")
    graph.add_edge("unified_review", "finalize")
    graph.add_edge("finalize", "validate")
    graph.add_edge("validate", END)
    return graph.compile()


def run_review_graph(
    *,
    repo_path: str,
    quest: str,
    review_url: str,
    config: ReviewConfig,
    page: Any = None,
    scrape_context: dict[str, str] | None = None,
) -> dict:
    from pathlib import Path

    path = Path(repo_path)
    summary = build_submission_summary(path, quest=quest, review_url=review_url)
    initial: ReviewState = {
        "repo_path": str(path.resolve()),
        "quest": quest,
        "review_url": review_url,
        "page": page,
        "config": config,
        "submission_summary": summary,
        "phase_results": {},
        "findings": [],
        "scrape_context": scrape_context or {},
    }
    graph = build_review_graph()
    final = graph.invoke(initial)
    result = final.get("review_result")
    if not result:
        return _fallback_result(final, final.get("error", "Graph produced no result"))
    return result
