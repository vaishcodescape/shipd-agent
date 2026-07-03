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

    # Olympus quest — long-horizon minimum
    if effective_loc >= olympus_min_loc:
        return (
            PhaseResult(
                status="PASS",
                summary=(
                    f"Effective LOC {effective_loc} meets Olympus long-horizon minimum "
                    f"({olympus_min_loc})."
                ),
            ),
            findings,
            (
                f"Effective LOC {effective_loc} meets Olympus long-horizon minimum "
                f"({olympus_min_loc})."
            ),
        )

    if effective_loc >= mars_min_loc:
        findings.append(
            Finding(
                phase="4",
                severity="MAJOR",
                finding="Solution scope fits Mars better than Olympus",
                evidence=(
                    f"effective_loc={effective_loc} < olympus_min={olympus_min_loc} "
                    f"and ≥ mars_min={mars_min_loc}; files: {file_evidence or 'n/a'}"
                ),
                suggested_fix=(
                    "Expand to Olympus long-horizon scope (≥ "
                    f"{olympus_min_loc} substantive LOC) or accept downgrade to Mars."
                ),
            )
        )
        return (
            PhaseResult(
                status="FAIL",
                summary=(
                    f"Effective LOC {effective_loc} below Olympus long-horizon minimum "
                    f"({olympus_min_loc}) but within Mars scope — downgrade recommended."
                ),
            ),
            findings,
            (
                f"Effective LOC {effective_loc} below Olympus minimum {olympus_min_loc}; "
                f"within Mars range — downgrade to Mars recommended."
            ),
        )

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
                f"Expand substantive solution changes to at least {mars_min_loc} lines."
            ),
        )
    )
    return (
        PhaseResult(
            status="FAIL",
            summary=(
                f"Effective LOC {effective_loc} below Mars minimum ({mars_min_loc}); "
                "not eligible for Mars downgrade."
            ),
        ),
        findings,
        (
            f"Effective LOC {effective_loc} below both Olympus ({olympus_min_loc}) "
            f"and Mars ({mars_min_loc}) minimums."
        ),
    )


def apply_downgrade_logic(
    review: ReviewResult,
    loc_info: dict,
    *,
    quest: str,
    olympus_min_loc: int,
    mars_min_loc: int,
    mars_max_loc: int,
) -> ReviewResult:
    """
    Post-validation downgrade_to_mars and decision adjustments (Olympus only).

    Sets downgrade_to_mars when effective LOC is below Olympus minimum but at
    least Mars minimum scope.
    """
    del mars_max_loc  # downgrade path does not use the Mars ceiling
    if quest != "olympus":
        return review

    method = loc_info.get("method", "none")
    if method == "none":
        return review

    effective_loc = int(loc_info.get("effective_loc", 0))
    updates: dict = {}

    if effective_loc >= olympus_min_loc:
        if review.downgrade_to_mars is None:
            updates["downgrade_to_mars"] = False
        return review.model_copy(update=updates) if updates else review

    if effective_loc >= mars_min_loc:
        updates["downgrade_to_mars"] = True
        downgrade_note = (
            f"Effective solution LOC ({effective_loc}) is below Olympus long-horizon "
            f"minimum ({olympus_min_loc}) but meets Mars minimum scope "
            f"(≥ {mars_min_loc}). Downgrade to Mars recommended."
        )
        if LOC_DOWNGRADE_TAG not in review.suggested_tags:
            updates["suggested_tags"] = [*review.suggested_tags, LOC_DOWNGRADE_TAG]

        other = review.other_notes.strip()
        if downgrade_note not in other:
            updates["other_notes"] = (other + "\n" + downgrade_note).strip() if other else downgrade_note

        internal = review.internal_notes.strip()
        loc_internal = f"LOC downgrade: {downgrade_note}"
        if loc_internal not in internal:
            updates["internal_notes"] = (
                (internal + "\n" + loc_internal).strip() if internal else loc_internal
            )

        feedback = review.contributor_feedback.strip()
        if "Mars" not in feedback and "LOC" not in feedback:
            loc_feedback = (
                f"The solution change is smaller than Olympus long-horizon scope "
                f"({effective_loc} substantive lines vs Olympus minimum {olympus_min_loc}). "
                "This may be better suited as a Mars submission; consider expanding scope "
                "or accepting a Mars downgrade."
            )
            updates["contributor_feedback"] = (
                (feedback + "\n\n" + loc_feedback).strip() if feedback else loc_feedback
            )

        if review.decision == "approve":
            updates["decision"] = "request_changes"
            summary = review.recommendation_summary
            updates["recommendation_summary"] = (
                f"LOC below Olympus long-horizon minimum — downgrade to Mars or expand scope. "
                f"{summary}"
            )
    else:
        updates["downgrade_to_mars"] = False
        small_note = (
            f"Effective LOC {effective_loc} below Mars minimum ({mars_min_loc}); "
            "downgrade to Mars not appropriate."
        )
        internal = review.internal_notes.strip()
        if small_note not in internal:
            updates["internal_notes"] = (
                (internal + "\n" + small_note).strip() if internal else small_note
            )
        if review.decision == "approve":
            updates["decision"] = "request_changes"

    return review.model_copy(update=updates)
