# Deterministic downgrade_to_mars logic from effective LOC.

from __future__ import annotations

from review.rubric_defaults import LOC_DOWNGRADE_TAG
from review.schemas import Finding, PhaseResult, ReviewResult


def evaluate_loc_phase4(
    loc_info: dict,
    *,
    quest: str,
    olympus_min_loc: int,
    mars_min_loc: int,
    mars_max_loc: int,
) -> tuple[PhaseResult, list[Finding], str]:
    """
    Deterministic Phase 4 LOC check.

    Returns (phase_result, findings, loc_analysis_summary_fragment).
    """
    method = loc_info.get("method", "none")
    effective_loc = int(loc_info.get("effective_loc", 0))

    if method == "none":
        return (
            PhaseResult(
                status="SKIP",
                summary="LOC check skipped — solution.patch unavailable.",
            ),
            [],
            "LOC analysis skipped — no solution.patch.",
        )

    findings: list[Finding] = []
    files = loc_info.get("files_analyzed") or []
    file_evidence = ", ".join(files[:5])
    if len(files) > 5:
        file_evidence += f" (+{len(files) - 5} more)"

    if quest == "mars":
        if effective_loc < mars_min_loc:
            findings.append(
                Finding(
                    phase="4",
                    severity="MAJOR",
                    finding="Solution below Mars minimum scope",
                    evidence=(
                        f"effective_loc={effective_loc} < mars_min={mars_min_loc}; "
                        f"files: {file_evidence or 'n/a'}"
                    ),
                    suggested_fix=(
                        f"Expand the solution to at least {mars_min_loc} substantive "
                        "lines (median successful agent runs bar for Mars)."
                    ),
                )
            )
            return (
                PhaseResult(
                    status="FAIL",
                    summary=(
                        f"Effective LOC {effective_loc} below Mars minimum ({mars_min_loc})."
                    ),
                ),
                findings,
                f"Effective LOC {effective_loc} below Mars minimum {mars_min_loc}.",
            )
        if effective_loc > mars_max_loc:
            findings.append(
                Finding(
                    phase="4",
                    severity="MAJOR",
                    finding="Solution exceeds Mars effective LOC limit",
                    evidence=(
                        f"effective_loc={effective_loc} > mars_max={mars_max_loc}; "
                        f"files: {file_evidence or 'n/a'}"
                    ),
                    suggested_fix=(
                        f"Reduce substantive solution changes to ≤ {mars_max_loc} lines "
                        "(exclude blanks and comments)."
                    ),
                )
            )
            return (
                PhaseResult(
                    status="FAIL",
                    summary=(
                        f"Effective LOC {effective_loc} exceeds Mars limit ({mars_max_loc})."
                    ),
                ),
                findings,
                f"Effective LOC {effective_loc} exceeds Mars maximum {mars_max_loc}.",
            )
        return (
            PhaseResult(
                status="PASS",
                summary=(
                    f"Effective LOC {effective_loc} within Mars scope "
                    f"({mars_min_loc}–{mars_max_loc})."
                ),
            ),
            findings,
            (
                f"Effective LOC {effective_loc} within Mars scope "
                f"({mars_min_loc}–{mars_max_loc})."
            ),
        )

    # Olympus has no solution-LOC floor. The count is information for the reviewer.
    del olympus_min_loc, mars_min_loc
    return (
        PhaseResult(
            status="PASS",
            summary=(
                f"Effective LOC {effective_loc}. Olympus has no solution-LOC floor; "
                "long horizon is Nova sequence length."
            ),
        ),
        findings,
        (
            f"Effective LOC {effective_loc}. Olympus has no solution-LOC floor."
        ),
    )


def _loc_only_downgrade(review: ReviewResult) -> bool:
    """True when a Mars downgrade is explained only by the retired LOC floor."""
    blob = " ".join(
        [
            review.other_notes,
            review.internal_notes,
            review.recommendation_summary,
            review.contributor_feedback,
            " ".join(review.suggested_tags),
        ]
    ).lower()
    loc_signals = (
        "lines of code" in blob
        or "effective loc" in blob
        or "substantive line" in blob
        or "long-horizon minimum" in blob
        or "olympus minimum" in blob
        or "solution-loc" in blob
    )
    other_fit = any(
        phrase in blob
        for phrase in (
            "repo fit",
            "wrong difficulty",
            "not an olympus task",
            "mars-level expectation",
        )
    )
    return loc_signals and not other_fit


def apply_downgrade_logic(
    review: ReviewResult,
    loc_info: dict,
    *,
    quest: str,
    olympus_min_loc: int,
    mars_min_loc: int,
    mars_max_loc: int,
) -> ReviewResult:
    """Clear a LOC-only Mars downgrade. Olympus no longer uses a LOC floor.

    Mars size limits are enforced by evaluate_loc_phase4, not here.
    """
    del olympus_min_loc, mars_min_loc, mars_max_loc, loc_info
    if quest != "olympus" or not review.downgrade_to_mars:
        return review
    if not _loc_only_downgrade(review):
        return review

    note = "LOC is not an Olympus bar; cleared a LOC-only Mars downgrade."
    updates: dict = {"downgrade_to_mars": False}
    tags = [tag for tag in review.suggested_tags if tag != LOC_DOWNGRADE_TAG]
    if tags != list(review.suggested_tags):
        updates["suggested_tags"] = tags
    internal = review.internal_notes.strip()
    updates["internal_notes"] = f"{internal}\n{note}".strip() if internal else note
    return review.model_copy(update=updates)
