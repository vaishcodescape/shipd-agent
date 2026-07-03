# Phase-aware prompts for the Shipd review LangGraph pipeline.

from __future__ import annotations

from review.rubric_defaults import (
    APPROVE_CHECKLIST,
    APPROVE_MIN_BAND_SCORE,
    BAND_SCORE_MEANINGS,
    DECISION_GUARDS_PROMPT,
    LOC_THRESHOLDS_PROMPT,
    MARS_MAX_EFFECTIVE_LOC,
    OLYMPUS_MAX_EFFECTIVE_LOC,
    PHASE0_PASS_CRITERIA,
)

_BAND_SCORE_GUIDE = "; ".join(
    f"{score}={meaning.split('—', 1)[0].strip()}" for score, meaning in sorted(BAND_SCORE_MEANINGS.items())
)
_PHASE0_CRITERIA = "; ".join(PHASE0_PASS_CRITERIA)

PHASE_CHECKLIST = f"""
Work through rubric phases **in order** (0 through 6):

| Phase | Focus |
|-------|--------|
| 0 | Artifacts present, git HEAD, patch apply --check (use `run_phase0_checks`, `list_artifacts`, `check_patch_apply`); pass criteria: {_PHASE0_CRITERIA} |
| 1 | Problem description quality (concise, no AI slop, repo fit, duplicates) |
| 2 | Dockerfile & test.sh harness (minimal Dockerfile, base/new split) |
| 3 | Test quality (coverage, determinism, behaviour not implementation, no network) |
| 4 | Solution & code quality (requirements, repo patterns, LOC discipline — approve min band {APPROVE_MIN_BAND_SCORE}; see LOC thresholds below) |
| 5 | Agent runs & solvability (use agent_runs + Holistic AI Check; SKIP if not available) |
| 6 | Holistic / platform (related submissions, Holistic AI Check fairness notes, Mars vs Olympus fit, eligibility) |

Band scores (0–3): {_BAND_SCORE_GUIDE}
"""

UNIFIED_REVIEW_SYSTEM_PROMPT = """You are a Shipd submission reviewer evaluating **all rubric phases 0–6** in one coherent pass.

Rules:
- Evaluate phases 0 → 6 systematically. Phase 0 combines mechanical checks with your review judgment.
- Use `run_phase0_checks`, `list_artifacts`, and `check_patch_apply` for Phase 0 mechanics; initial results are in the user prompt.
- **Start with** problem description, test.patch, solution.patch, test.sh, and Dockerfile — avoid redundant reads.
- Tag every issue with phase number (0–6) and severity (BLOCKER, MAJOR, MINOR, QUESTION).
- Do not invent paths, line numbers, or command results — verify with tools.
- Do not approve anything; your job is evidence collection for the finalize step.
- Use `read_holistic_check` or `read_shipd_review_panel` to re-read Shipd page panels when needed.
- For phase 5: **read** Holistic AI Check reviewer notes (agent pass rates, solvability) and agent_runs context before findings; cite pass rates and failure patterns in evidence when available.
- For phase 6: **read** Holistic AI Check fairness/readiness notes and related_submissions context; note duplicate/overlap risk with similarity scores/tags.
- When Holistic AI Check status is PASS with explicit checklist passes, do not contradict without your own evidence.
- Band ratings alignment: phase 1 → problem band; phases 2–3 → tests band; phase 4 → solution band.
- **LOC discipline (Phase 4):** deterministic effective LOC is computed at review start from `solution.patch`.
  Compare against quest thresholds. Do not contradict the precomputed `loc_analysis` unless you re-run
  `compute_effective_loc` and find an error.
- **Olympus downgrade:** when effective LOC exceeds the Olympus max but stays within the Mars max,
  recommend `downgrade_to_mars: true` and note Mars fit in `other_notes` / contributor feedback.
- Stay within the tool budget — finish with a summary once key artifacts are inspected.

When done, end with a structured summary:

## Phase summaries
For each phase 0–6: PASS / FAIL / SKIP and one-line rationale.

## Findings
List BLOCKER/MAJOR/MINOR/QUESTION items with phase, file:line evidence, suggested fix.

## Band evidence
Brief notes supporting problem, tests, and solution band scores (0–3).
"""

# Backward-compatible alias
EXPLORE_SYSTEM_PROMPT = UNIFIED_REVIEW_SYSTEM_PROMPT

_HOLISTIC_UNAVAILABLE = (
    "not available — run via orchestrator with browser session"
)


def build_holistic_check_prompt_section(scrape: dict[str, str]) -> str:
    """Format scraped Holistic AI Check data for LLM prompts."""
    if scrape.get("holistic_check_available") != "true":
        raw = scrape.get("holistic_check_raw", "").strip()
        if raw:
            return f"Holistic AI Check: {raw}"
        return f"Holistic AI Check: {_HOLISTIC_UNAVAILABLE}"

    status = scrape.get("holistic_check_status") or "UNKNOWN"
    checklist = scrape.get("holistic_check_checklist") or "(none)"
    notes = scrape.get("holistic_check_reviewer_notes") or "(none)"
    return f"""## Holistic AI Check (from Shipd)
Status: {status}
Checklist: {checklist}
Reviewer Notes: {notes}

Use this when evaluating phases 5–6 (agent runs, solvability, fairness) and overall readiness.
Incorporate status, checklist, and reviewer notes into findings and contributor_feedback when relevant.
Do not contradict an explicit PASS checklist without evidence from your own artifact review."""


def build_unified_review_user_prompt(
    *,
    quest: str,
    repo_path: str,
    commit: str | None,
    phase0_log: str,
    phase0_status: str,
    agent_runs: str,
    related_submissions: str,
    holistic_check: str = "",
    loc_analysis: str = "",
    olympus_max_loc: int = OLYMPUS_MAX_EFFECTIVE_LOC,
    mars_max_loc: int = MARS_MAX_EFFECTIVE_LOC,
    max_tool_steps: int = 20,
) -> str:
    holistic_section = holistic_check or _HOLISTIC_UNAVAILABLE
    loc_section = loc_analysis.strip() or "LOC analysis not yet run."
    loc_thresholds = LOC_THRESHOLDS_PROMPT.format(
        olympus_max=olympus_max_loc,
        mars_max=mars_max_loc,
    )
    return f"""Review this Shipd **{quest}** submission across **all phases 0–6**.

Repo: {repo_path}
Commit: {commit or "unknown"}

Initial Phase 0 mechanical checks (run at review start — verify or re-run with tools):
Status: **{phase0_status}**

Phase 0 log:
{phase0_log}

## Precomputed effective LOC (Phase 4)
{loc_section}

{loc_thresholds}

{holistic_section}

Agent runs context (read before phase 5 feedback): {agent_runs}
Related submissions context (read before phase 6 feedback): {related_submissions}

Read the Shipd page context above before providing feedback. Cite agent pass rates,
failure patterns, similarity scores, and holistic reviewer notes in findings when available.

Tool budget: ~{max_tool_steps} tool steps — inspect key artifacts first, then summarize.

{PHASE_CHECKLIST}

Use tools to inspect problem description, test.patch, solution.patch, test.sh, and Dockerfile.
Use `compute_effective_loc` to re-check LOC if needed. Evaluate every phase 0–6.
Reference phase numbers in every finding.
If Phase 0 mechanical checks failed, still complete phases 1–6 for contributor feedback.
"""


# Backward-compatible alias
def build_explore_user_prompt(**kwargs) -> str:
    return build_unified_review_user_prompt(**kwargs)


_APPROVE_CHECKLIST_TEXT = "\n".join(f"  {i}. {item}" for i, item in enumerate(APPROVE_CHECKLIST, 1))

FINALIZE_PHASE_INSTRUCTIONS = f"""
**phase_results requirements (mandatory):**
- Include keys "0" through "6", each with {{status: PASS|FAIL|SKIP, summary: str}}.
- Phase "0" mechanical status must align with `phase0_result` from deterministic checks;
  incorporate explore notes for Phase 0 context in the summary.
- Populate phases 1–6 from your rubric evaluation and explore_notes; never omit a key.
- Use SKIP only when data is genuinely unavailable (e.g. phase 5 with no agent runs).

**findings:** every item must include a `phase` field ("0"–"6").

**band_ratings:** problem ← phase 1; tests ← phases 2–3; solution ← phase 4.
- Scores 0–3: {_BAND_SCORE_GUIDE}
- Approve requires each band score ≥ {APPROVE_MIN_BAND_SCORE}; none at 0 or 1.

**Effective LOC (Phase 4 — deterministic):**
- `loc_analysis` and `loc_info` are precomputed from `solution.patch` at review start.
- Thresholds: Olympus max {OLYMPUS_MAX_EFFECTIVE_LOC} substantive LOC → downgrade consideration;
  Mars max {MARS_MAX_EFFECTIVE_LOC} → hard ceiling.
- Populate `loc_analysis` with the precomputed summary unless you re-ran `compute_effective_loc`.
- For **Olympus** quest: set `downgrade_to_mars: true` when effective LOC exceeds Olympus max
  but stays within Mars max; `false` when within Olympus max; do not downgrade when above Mars max.
- Phase `"4"` status from deterministic LOC check is authoritative for LOC limits; incorporate
  your code-quality findings into phase 4 summary and solution band without reversing LOC PASS/FAIL.

**Shipd page context (when provided):**
- `agent_run_notes` ← agent runs scrape (pass rates, failure patterns, LOC hints).
- `related_submissions_notes` ← related submissions scrape (similarity, duplicate tags).
- `holistic_check_notes` ← Holistic AI Check status, checklist, reviewer notes.
- Use scraped data in phases 5–6; populate these fields from evidence, not invention.

**Approve checklist (must all be true):**
{_APPROVE_CHECKLIST_TEXT}

**Decision guards:**
{DECISION_GUARDS_PROMPT}

**contributor_feedback:** Fills the author note textarea — keep it **compact** (minimal lines, no fluff):
- One issue per line; no paragraphs, preamble, or filler.
- Prefer `Band (score/3): brief actionable issue` when tied to a band (Problem, Tests, Solution).
- Approve with only minor notes: 1–2 lines max.
- Band `reasoning` for scores < 3 is appended on submit; avoid repeating the same point in both fields.
Example:
Problem (2/3): Scope unclear — specify which Lark versions are in scope.
Tests (2/3): Missing edge cases for ambiguous grammars.
Solution (1/3): Implementation doesn't handle nested labels.
"""
