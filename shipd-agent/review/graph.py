# LangGraph review agent
from __future__ import annotations

import time
from typing import Annotated, Any, TypedDict

from review.llm import llm_config_error, make_chat_model
from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

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
from review.prompt_checks import (
    analyze_task_prompt,
    evaluate_prompt_phase1,
    format_prompt_analysis,
)
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
from review.deep import ReviewBudget, ReviewBudgetExceeded, run_deep_review
from review.evidence import EvidenceLedger, fit_finalize_payload, panel_text_available


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
    review_evidence: dict
    evidence_gaps: list[str]
    review_budget: Any
    force_request_changes: bool
    holistic_check: dict[str, Any]
    agent_runs_data: dict[str, Any]
    scrape_context: dict[str, str]
    loc_info: dict
    loc_analysis: str
    prompt_analysis: str
    prompt_body: str
    repo_check_notes: str
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
    "auto_review": "Auto Review (prediction)",
}

# Phases always evaluable from the cloned repo (their artifacts are local).
_ARTIFACT_PHASES: tuple[str, ...] = ("1", "2", "3")


def _scrape_value_present(value: Any) -> bool:
    """True when a scraped panel string carries real data (not a placeholder)."""
    return panel_text_available(value)


def _agent_runs_available(scrape: dict[str, str]) -> bool:
    return _scrape_value_present(scrape.get("agent_runs"))


def _platform_data_available(scrape: dict[str, str]) -> bool:
    return (
        _scrape_value_present(scrape.get("related_submissions"))
        or scrape.get("holistic_check_available") == "true"
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
                "submission_criteria": ctx.get("submission_criteria", ""),
                "auto_review_panel": ctx.get("auto_review_panel", ""),
                "fp_check_panel": ctx.get("fp_check_panel", ""),
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


def _agent_run_kwargs(state: ReviewState) -> dict[str, Any]:
    config = state.get("config")
    if config is None:
        return {}
    kwargs: dict[str, Any] = {}
    for name in (
        "min_unhinted_runs",
        "max_solve_rate",
        "nova_min_sequence_tokens",
        "nova_payout_sequence_cap",
        "max_cheat_rate",
    ):
        value = getattr(config, name, None)
        if value is not None:
            kwargs[name] = value
    return kwargs


def _run_agent_runs_analysis(state: ReviewState) -> dict:
    """Run deterministic Phase 5 checks, including the local false-positive pass."""
    from review.fp_check import evaluate_false_positives

    quest = state.get("quest", "olympus")
    agent_runs_data = state.get("agent_runs_data") or {}
    phase5_result, phase5_findings = evaluate_agent_runs_phase5(
        agent_runs_data,
        quest=quest,
        **_agent_run_kwargs(state),
    )
    if phase5_result.status == "SKIP" and "manual phase 5" in phase5_result.summary:
        # Nothing measurable — leave phase 5 to the reviewer instead of
        # stamping a deterministic verdict over it.
        return {}

    fp_result, fp_findings = evaluate_false_positives(agent_runs_data)
    phase5_findings = [*phase5_findings, *fp_findings]
    if fp_result.status == "FAIL":
        phase5_result = PhaseResult(
            status="FAIL",
            summary=f"{phase5_result.summary} {fp_result.summary}".strip(),
        )
    elif fp_result.status == "SKIP" and phase5_result.status == "PASS":
        phase5_result = PhaseResult(
            status="PASS",
            summary=f"{phase5_result.summary} {fp_result.summary}".strip(),
        )
    elif fp_result.status == "PASS" and phase5_result.status == "PASS":
        phase5_result = PhaseResult(
            status="PASS",
            summary=f"{phase5_result.summary} {fp_result.summary}".strip(),
        )

    phase_results = dict(state.get("phase_results", {}))
    phase_results["5"] = phase5_result.model_dump()
    findings = list(state.get("findings", []))
    findings.extend(f.model_dump() for f in phase5_findings)
    return {"phase_results": phase_results, "findings": findings}


def _run_prompt_analysis(state: ReviewState) -> dict:
    """Run deterministic Phase 1 task prompt checks (word limit, Too Many Requirements)."""
    from pathlib import Path

    config: ReviewConfig = state["config"]
    scrape = state.get("scrape_context") or {}
    analysis = analyze_task_prompt(
        Path(state["repo_path"]),
        holistic_text=scrape.get("holistic_check_raw", ""),
    )
    limits = {
        "max_words": config.max_prompt_words,
        "max_requirements": config.max_prompt_requirements,
    }
    out: dict[str, Any] = {
        "prompt_analysis": format_prompt_analysis(analysis, **limits),
        "prompt_body": analysis.body,
    }
    phase1_result, phase1_findings = evaluate_prompt_phase1(analysis, **limits)
    if phase1_result is not None:
        phase_results = dict(state.get("phase_results", {}))
        phase_results["1"] = phase1_result.model_dump()
        findings = list(state.get("findings", []))
        findings.extend(f.model_dump() for f in phase1_findings)
        out.update({"phase_results": phase_results, "findings": findings})
    return out


def _run_static_checks(state: ReviewState) -> dict:
    """Dockerfile, test-patch, and repo checks that do not need the model."""
    from pathlib import Path

    from review.harness_checks import evaluate_harness
    from review.repo_checks import evaluate_repo

    repo_path = Path(state["repo_path"])
    prompt = str(state.get("prompt_body") or "")
    harness = evaluate_harness(repo_path, prompt=prompt)
    repo_phase, repo_findings = evaluate_repo(repo_path)
    findings = list(state.get("findings", []))
    phase_results = dict(state.get("phase_results", {}))
    for finding in harness:
        findings.append(finding.model_dump())
    for phase in ("2", "3"):
        phase_findings = [item for item in harness if item.phase == phase and item.severity in {"BLOCKER", "MAJOR"}]
        if phase_findings:
            phase_results[phase] = {
                "status": "FAIL",
                "summary": "Harness prechecks failed: "
                + "; ".join(item.finding for item in phase_findings),
            }
    if repo_phase.status == "FAIL":
        phase_results["6"] = repo_phase.model_dump()
        findings.extend(item.model_dump() for item in repo_findings)
    elif repo_phase.status == "SKIP":
        notes = str(state.get("repo_check_notes") or "")
        state_note = repo_phase.summary
        if state_note not in notes:
            return {
                "phase_results": phase_results,
                "findings": findings,
                "repo_check_notes": state_note,
            }
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
            flakiness_repeats=config.flakiness_repeats
            if config.flakiness_repeats is not None
            else 6,
        )
    log_activity(
        f"Phase 0 result: {phase0.status} — {phase0.summary}", category="phase0"
    )
    phase_results = dict(state.get("phase_results", {}))
    phase_results["0"] = phase0_to_phase_result(phase0).model_dump()
    # Keep findings recorded before Phase 0 (scrape-time agent run checks).
    findings = [
        *state.get("findings", []),
        *(f.model_dump() for f in phase0.findings),
    ]
    out = {
        "phase0_result": phase0,
        "phase0_log": phase0.phase0_log,
        "phase_results": phase_results,
        "findings": findings,
        "force_request_changes": phase0.critical_fail,
    }
    out.update(_run_loc_analysis({**state, **out}))
    out.update(_run_prompt_analysis({**state, **out}))
    out.update(_run_static_checks({**state, **out}))
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
    # Deterministic task prompt (1) and agent run (5) verdicts need no LLM.
    for key in ("1", "5"):
        det = state.get("phase_results", {}).get(key)
        if det:
            phase_results[key] = PhaseResult(**det)
            findings.extend(
                f for f in state.get("findings", [])
                if isinstance(f, dict) and f.get("phase") == key
            )

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
    """Run mechanical checks, then the primary Deep Agents evidence reviewer."""
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

    llm_error = llm_config_error(config)
    if llm_error:
        return {
            **phase0_updates,
            "error": llm_error,
            "explore_notes": "Unified review skipped: LLM provider not configured.",
        }

    from pathlib import Path

    repo_path = Path(state["repo_path"])
    phase0_result = phase0_updates.get("phase0_result")
    # Playwright's sync API is greenlet-bound to the main thread, but LangGraph
    # runs parallel tool calls on worker threads — never hand the live page to
    # explore tools ("cannot switch to a different thread"). scrape_node already
    # captured every panel on the main thread; tools serve that cache.
    scrape = state.get("scrape_context", {})
    panels = {
        "agent_runs": scrape.get("agent_runs", ""),
        "related_submissions": scrape.get("related_submissions", ""),
        "holistic_check": build_holistic_check_prompt_section(scrape)
        if scrape.get("holistic_check_available") == "true" else "",
    }
    ledger = EvidenceLedger(
        repo_path, state.get("submission_summary", {}).get("artifacts", {}),
        panels, phase0_updates.get("phase0_log", ""),
        max_phase_chars=max(1000, min(4000, (config.review_finalize_payload_max_chars - 8000) // 6)),
    )
    budget = ReviewBudget(
        config.review_max_tool_steps,
        config.review_coverage_recheck_max_steps if config.review_coverage_recheck else 0,
        config.review_max_total_tokens,
    )
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
        evidence=ledger,
    )

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
        prompt_analysis=phase0_updates.get("prompt_analysis", ""),
        olympus_min_loc=config.olympus_min_loc,
        mars_min_loc=config.mars_min_loc,
        mars_max_loc=config.mars_max_loc,
        max_prompt_words=config.max_prompt_words,
        max_prompt_requirements=config.max_prompt_requirements,
        max_tool_steps=config.review_max_tool_steps,
    )
    log_activity(
        f"explore prompt ~{estimate_tokens(user_prompt):,} tokens (est.)",
        category="review",
    )

    try:
        output = run_deep_review(
            model=make_chat_model(config, "explore"), tools=tools,
            prompt=user_prompt, ledger=ledger, config=config, budget=budget,
        )
        return {**phase0_updates, **output, "review_budget": budget}
    except Exception as exc:
        return {
            **phase0_updates, "review_budget": budget,
            "review_evidence": ledger.snapshot(), "evidence_gaps": ledger.gaps(),
            "error": f"Deep Agents review failed: {exc}",
            "explore_notes": f"Deep Agents review error: {exc}",
        }


def finalize_node(state: ReviewState) -> dict:
    if state.get("config").review_dry_run:
        return {"review_result": _dry_run_result(state)}

    if state.get("error"):
        return {"review_result": _fallback_result(state, state["error"])}

    config: ReviewConfig = state["config"]
    llm_error = llm_config_error(config)
    if llm_error:
        return {
            "review_result": _fallback_result(state, llm_error),
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
        "review_evidence": state.get("review_evidence", {}),
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
        "prompt_analysis": state.get("prompt_analysis", ""),
        "max_prompt_words": config.max_prompt_words,
        "max_prompt_requirements": config.max_prompt_requirements,
        "olympus_min_loc": config.olympus_min_loc,
        "mars_min_loc": config.mars_min_loc,
        "mars_max_loc": config.mars_max_loc,
    }
    try:
        human_content = fit_finalize_payload(human_payload, config.review_finalize_payload_max_chars)
    except ValueError as exc:
        return {"review_result": _fallback_result(state, str(exc))}

    system = (
        "You are the Shipd autonomous review agent. Follow shipd-rubric.md exactly.\n"
        "Evaluate all rubric phases 0–6 and output structured ReviewResult fields.\n"
        "Structured review_evidence contains validated source citations and phase reports. "
        "Use these reports for scoring; do not invent findings or treat prose as coverage.\n"
        "Do not emit an auto_review phase; a later stage fills it in.\n"
        f"--- rubric excerpt ---\n{rubric}"
    )
    log_activity(
        f"finalize input ~{estimate_tokens(system + human_content):,} tokens (est.)",
        category="review",
    )

    try:
        llm = make_chat_model(config, "finalize", structured_output=ReviewResult)
    except Exception as exc:
        return {"review_result": _fallback_result(state, f"Cannot initialize final scorer: {exc}")}

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
                ],
                config={"callbacks": [state["review_budget"].callback(finalize=True)]}
                if state.get("review_budget") else None,
            )
            break
        except Exception as exc:
            last_error = exc
            log_activity(
                f"finalize attempt {attempt + 1} failed: {exc}", category="review"
            )
            if isinstance(exc, ReviewBudgetExceeded):
                break
            human_content += f"\n\nPrevious validation error (fix output): {str(exc)[:2000]}"

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

    phase1_det = state.get("phase_results", {}).get("1")
    if phase1_det and phase1_det.get("status") == "FAIL":
        merged_phases = dict(loc_updates.get("phase_results", review.phase_results))
        reviewer_phase1 = merged_phases.get("1")
        reviewer_summary = reviewer_phase1.summary.strip() if reviewer_phase1 else ""
        merged_phases["1"] = PhaseResult(
            status="FAIL",
            summary=" ".join(
                part for part in (phase1_det.get("summary", ""), reviewer_summary) if part
            ),
        )
        loc_updates["phase_results"] = merged_phases
    phase1_findings = [
        f for f in state.get("findings", [])
        if isinstance(f, dict)
        and f.get("phase") == "1"
        and ("1", f.get("finding", "")) not in existing_finding_keys
    ]
    if phase1_findings:
        base_findings = loc_updates.get("findings", review.findings)
        loc_updates["findings"] = [
            *base_findings,
            *[Finding(**f) for f in phase1_findings],
        ]

    for phase_key in ("2", "3", "6"):
        deterministic = state.get("phase_results", {}).get(phase_key)
        if not deterministic or deterministic.get("status") != "FAIL":
            continue
        merged_phases = dict(loc_updates.get("phase_results", review.phase_results))
        merged_phases[phase_key] = PhaseResult(**deterministic)
        loc_updates["phase_results"] = merged_phases
        extra = [
            f for f in state.get("findings", [])
            if isinstance(f, dict)
            and f.get("phase") == phase_key
            and (phase_key, f.get("finding", "")) not in existing_finding_keys
        ]
        if extra:
            base_findings = loc_updates.get("findings", review.findings)
            loc_updates["findings"] = [
                *base_findings,
                *[Finding(**f) for f in extra],
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

    review = _apply_evidence_reports(review, state)
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
        return {"review_result": _with_review_audit(
            mark_review_incomplete(result_dict, error=error), state,
        )}

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
    gap_phases = sorted(set(state.get("evidence_gaps", [])) | {
        key
        for key in sorted(evaluable)
        if phase_status.get(key, {}).get("status") == "SKIP"
    })
    if gap_phases:
        review = _flag_coverage_gaps(review, gap_phases)
        result_dict = review.to_submit_dict()
        log_activity(
            "validate: coverage gap on phase(s) "
            f"{', '.join(gap_phases)} — forcing request_changes and flagging",
            category="review",
        )

    return {"review_result": _with_review_audit(mark_review_complete(result_dict), state)}


def _with_review_audit(result: dict, state: ReviewState) -> dict:
    if "review_evidence" in state:
        result["review_evidence"] = state["review_evidence"]
    if state.get("review_budget") is not None:
        result["review_usage"] = state["review_budget"].snapshot()
    repo = state.get("repo_path")
    if repo:
        from pathlib import Path

        from review.staleness import content_fingerprint, staleness_note

        config = state.get("config")
        lineup = getattr(config, "expected_agent_models", None)
        fingerprint = content_fingerprint(Path(repo), agent_lineup=lineup)
        result["submission_fingerprint"] = fingerprint
        note = staleness_note(Path(repo), fingerprint)
        if note:
            internal = str(result.get("internal_notes") or "").strip()
            result["internal_notes"] = f"{internal}\n{note}".strip() if internal else note
    return result


def _apply_evidence_reports(review: ReviewResult, state: ReviewState) -> ReviewResult:
    """Scorer assigns bands; recorded evidence and mechanical checks own facts."""
    if "review_evidence" not in state:
        return review
    reports = state["review_evidence"].get("phases", {})
    phases = dict(review.phase_results)
    findings = [Finding.model_validate(f) for f in state.get("findings", [])]
    for phase in map(str, range(1, 7)):
        report = reports.get(phase)
        phases[phase] = PhaseResult(
            status=report["status"] if report else "SKIP",
            summary=report["summary"] if report else "No verified phase evidence report recorded.",
        )
        for finding in report.get("findings", []) if report else []:
            evidence = "\n".join(
                f"{c['path']}" + (f":{c['start_line']}-{c['end_line']}" if c['start_line'] else "")
                + f" [{c['source_id']}]: {c['excerpt']}" for c in finding["citations"]
            )
            findings.append(Finding(
                phase=phase, severity=finding["severity"], finding=finding["finding"],
                evidence=evidence, suggested_fix=finding.get("suggested_fix", ""),
            ))
    for phase, deterministic in state.get("phase_results", {}).items():
        status = deterministic.get("status") if isinstance(deterministic, dict) else None
        if phase in {"0", "auto_review"} or status == "FAIL" or (phase == "5" and status == "SKIP"):
            phases[phase] = PhaseResult.model_validate(deterministic)
    unique: dict[tuple[str, str], Finding] = {}
    severity_rank = {"BLOCKER": 4, "MAJOR": 3, "MINOR": 2, "QUESTION": 1}
    for finding in findings:
        key = (finding.phase, finding.finding)
        previous = unique.get(key)
        if previous is None or severity_rank[finding.severity] > severity_rank[previous.severity]:
            unique[key] = finding
    return review.model_copy(update={"phase_results": phases, "findings": list(unique.values())})


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
    return _with_review_audit(mark_review_incomplete(result.to_submit_dict(), error=reason), state)


def auto_review_node(state: ReviewState) -> dict:
    """Last stage: predict Auto Review from the submission as a whole."""
    from pathlib import Path

    from review.auto_review import PHASE_KEY, run_auto_review
    from review.llm import role_model

    raw = state.get("review_result")
    config = state.get("config")
    if not raw or config is None or config.review_dry_run:
        return {}
    if not getattr(config, "review_auto_review", True):
        return {}
    # A provider failure leaves the review incomplete. Do not replace that
    # result with a prediction. Phase failures still reach this node, because
    # finalize marks those reviews complete.
    if raw.get("review_complete") is False:
        return {}

    scrape = state.get("scrape_context") or {}
    log_activity(
        f"auto review: predicting with {role_model(config, 'auto_review')}",
        category="review",
    )
    phase, findings = run_auto_review(
        config=config,
        repo_path=Path(state["repo_path"]),
        draft=raw,
        quest=state.get("quest", "olympus"),
        prompt_analysis=str(state.get("prompt_analysis") or ""),
        agent_runs=str(scrape.get("agent_runs") or ""),
        platform_panel=str(scrape.get("auto_review_panel") or ""),
        budget=state.get("review_budget"),
    )
    log_activity(f"auto review: {phase.status}", category="review")

    review = ReviewResult.model_validate(raw)
    phases = dict(review.phase_results)
    phases[PHASE_KEY] = phase
    merged = list(review.findings)
    seen = {(item.phase, item.finding) for item in merged}
    for finding in findings:
        if (finding.phase, finding.finding) not in seen:
            merged.append(finding)
    updates: dict[str, Any] = {"phase_results": phases, "findings": merged}
    if phase.status == "FAIL":
        feedback = review.contributor_feedback.strip()
        line = (
            "Predicted Auto Review issues are listed as \"Auto Review prediction\" "
            "findings. They are a prediction, not Shipd's Auto Review verdict."
        )
        if line not in feedback:
            updates["contributor_feedback"] = f"{feedback}\n{line}".strip()
    review = review.model_copy(update=updates)
    # This stage runs after scoring, so its findings have no earlier ledger
    # report. Keep them in authoritative state for validate's reconciliation.
    return {
        "review_result": {**raw, **review.to_submit_dict()},
        "phase_results": {**state.get("phase_results", {}), PHASE_KEY: phase.model_dump()},
        "findings": [
            *[f for f in state.get("findings", []) if f.get("phase") != PHASE_KEY],
            *[f.model_dump() for f in findings],
        ],
    }


def build_review_graph():
    graph = StateGraph(ReviewState)
    graph.add_node("scrape", scrape_node)
    graph.add_node("unified_review", unified_review_node)
    graph.add_node("finalize", finalize_node)
    graph.add_node("auto_review", auto_review_node)
    graph.add_node("validate", validate_node)

    graph.add_edge(START, "scrape")
    graph.add_edge("scrape", "unified_review")
    graph.add_edge("unified_review", "finalize")
    graph.add_edge("finalize", "auto_review")
    graph.add_edge("auto_review", "validate")
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
