# Values sourced from shipd-rubric.md — update when rubric changes.

from __future__ import annotations

from typing import Final

# --- Shipd band ratings (Problem · Tests · Solution) ---

BAND_SCORE_MIN: Final[int] = 0
BAND_SCORE_MAX: Final[int] = 3

# rubric: "Score | Meaning (Shipd guide)" table
BAND_SCORE_MEANINGS: Final[dict[int, str]] = {
    3: "Clean — section is perfect; no comments needed",
    2: "Minor issues — not the cleanest; could approve or ask for small fixes",
    1: "Weak — must be fixed before approval",
    0: "Failing — totally bad; used for extreme rejection cases only",
}

CONFIDENCE_LEVELS: Final[tuple[str, ...]] = ("high", "medium", "low")

# rubric: "Rating 3 with medium/low confidence → re-check; likely 2 or request_changes"
CONFIDENCE_REVIEW_THRESHOLD_SCORE: Final[int] = 3

# rubric: "reasoning required in JSON when any band score is < 3"
BAND_REASONING_REQUIRED_BELOW: Final[int] = 3

# --- Decision rules & approve checklist ---

# rubric: "request_changes — Default when anything substantive is wrong"
DECISION_DEFAULT: Final[str] = "request_changes"

# rubric: Approve checklist #2 — "Each band score ≥ 2, none at 0 or 1"
APPROVE_MIN_BAND_SCORE: Final[int] = 2
APPROVE_BLOCKING_BAND_SCORES: Final[tuple[int, ...]] = (0, 1)

# rubric: Approve checklist (must all be true)
APPROVE_CHECKLIST: Final[tuple[str, ...]] = (
    "Phase 0 PASS (mechanical contract verified)",
    "Each band score ≥ 2, none at 0 or 1",
    "Prefer all bands 3 with high confidence; if any band is 2, issues are truly minor/optional",
    "No open MAJOR/BLOCKER findings requiring author fixes",
    "Olympus: repo_eligible and solvability_ok true; Mars: quality/difficulty appropriate",
    "No confirmed duplicate",
)

# rubric: Decision rules — approve conditions (summary)
DECISION_APPROVE_SUMMARY: Final[str] = (
    "Submission meets the full quality bar: Phase 0 passes; all band scores ≥ 2 with none "
    "at 0 or 1; prefer all 3 with high confidence; no MAJOR/BLOCKER findings requiring "
    "author action; Olympus repo eligible and solvability OK; Mars quality/difficulty appropriate."
)

# rubric: Decision rules — reject conditions (rare / extreme only)
DECISION_REJECT_SUMMARY: Final[str] = (
    "Confirmed duplicate; wrong task or repo entirely; malicious content; or no credible fix path "
    "(patches irreconcilable, problem and tests fundamentally contradict)."
)

# rubric: "BLOCKER ≠ automatic reject" — usually request_changes
BLOCKER_DEFAULT_DECISION: Final[str] = "request_changes"

# rubric: "When uncertain" / "Borderline" / "Between approve and request_changes"
UNCERTAIN_DEFAULT_DECISION: Final[str] = "request_changes"

# --- Finding severity (Hard rules §6) ---

SEVERITY_LEVELS: Final[tuple[str, ...]] = ("BLOCKER", "MAJOR", "MINOR", "QUESTION")

# rubric: "BLOCKER findings usually → request_changes, not reject, on Mars and Olympus"
SEVERITY_BLOCK_APPROVE: Final[tuple[str, ...]] = ("BLOCKER", "MAJOR")

# --- Phases ---

PHASE_IDS: Final[tuple[str, ...]] = ("0", "1", "2", "3", "4", "5", "6")
PHASE_COUNT: Final[int] = 7

# rubric: Phase 0 — Setup & ground truth pass criteria (summaries for prompts)
PHASE0_PASS_CRITERIA: Final[tuple[str, ...]] = (
    "Commit: HEAD at stated base; tree matches post-patch setup in log",
    "Patch apply: test.patch and solution.patch apply cleanly",
    "Tests without solution: base PASS, new FAIL for missing behaviour (not harness breakage)",
    "Tests with solution: base and new PASS after solution applied",
    "Network independence: no outbound calls at run time; deps at build time only",
    "JUnit XML: valid output; no silent skips; real failures surfaced",
    "Dockerfile: minimal; /bin/bash entrypoint; builds without patches; deps at build time",
)

# rubric: "Phase 0 failure → BLOCKER finding(s). Default request_changes"
PHASE0_FAIL_DEFAULT_DECISION: Final[str] = "request_changes"

# --- Effective LOC (Phase 4 / Phase 6 Mars vs Olympus) ---

# rubric: Phase 4 — "LOC discipline: estimate substantive solution LOC (exclude blanks,
# dead code, doc inflation, unrelated churn). Compare to median agent solution LOC…"
# Rubric does not specify numeric limits; operational defaults below.
OLYMPUS_MAX_EFFECTIVE_LOC: Final[int] = 150
MARS_MAX_EFFECTIVE_LOC: Final[int] = 300

# rubric: Phase 6 — "if submission fits Mars-level expectations better than Olympus,
# note downgrade suggestion… prefer request_changes over reject"
LOC_DOWNGRADE_TAG: Final[str] = "Lines of code"

# --- Mars mode-specific fields ---

MARS_QUALITY_MIN: Final[int] = 1
MARS_QUALITY_MAX: Final[int] = 3
MARS_DIFFICULTY_MIN: Final[int] = 1
MARS_DIFFICULTY_MAX: Final[int] = 3
# rubric: "difficulty (1–3; usually 2, rarely 3)"
MARS_DIFFICULTY_TYPICAL: Final[int] = 2

# --- Olympus mode-specific fields ---

# rubric: Phase 6 — "Olympus eligibility: public, 500+ stars, recent commit,
# permissive license, production-grade, allowed language → repo_eligible"
OLYMPUS_MIN_REPO_STARS: Final[int] = 500

# rubric: Phase 5 — "For Olympus: solvability_ok — at least one agent could solve"
OLYMPUS_SOLVABILITY_MIN_PASSES: Final[int] = 1

# --- Suggested tags (Required output format) ---

# rubric: "suggested_tags: strings from Shipd internal tag buttons when applicable
# (Difficulty / scope, Lines of code, Repo fit, AI slop, Duplicate / overlapping, Already solved)"
SUGGESTED_TAGS: Final[tuple[str, ...]] = (
    "Difficulty / scope",
    "Lines of code",
    "Repo fit",
    "AI slop",
    "Duplicate / overlapping",
    "Already solved",
)

# --- Prompt helpers ---

LOC_THRESHOLDS_PROMPT: Final[str] = (
    "LOC thresholds: Olympus max {olympus_max} substantive lines; Mars max {mars_max}. "
    "Count excludes blanks and comment-only lines from solution.patch added hunks. "
    "For Olympus: if effective LOC > {olympus_max} and ≤ {mars_max}, flag Mars fit and "
    "recommend downgrade (downgrade_to_mars: true). If > {mars_max}, flag bloat — "
    "downgrade not appropriate. For Mars: if effective LOC > {mars_max}, flag as MAJOR "
    "Phase 4 finding."
)

DECISION_GUARDS_PROMPT: Final[str] = (
    "Never approve if any phase status is FAIL. "
    "Never approve if any finding severity is BLOCKER. "
    "Never approve if Phase 0 status is FAIL or any band score is 0 or 1. "
    "Never approve with open MAJOR findings requiring author fixes. "
    "Olympus: never approve if repo_eligible or solvability_ok is false. "
    "Default to request_changes when uncertain."
)
