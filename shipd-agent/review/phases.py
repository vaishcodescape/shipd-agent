# Phase helpers — deterministic Phase 0 and phase_results merging.

from __future__ import annotations

from review.schemas import PhaseResult

PHASE_KEYS: tuple[str, ...] = tuple(str(i) for i in range(7))

DRY_RUN_SKIP_SUMMARY = (
    "Dry run — phase not evaluated; re-run without --dry-run."
)
NOT_EVALUATED_SUMMARY = "Phase not evaluated before review completed."


def phase_result_to_dict(result: PhaseResult | dict) -> dict:
    if isinstance(result, PhaseResult):
        return result.model_dump()
    return dict(result)


def ensure_all_phase_results(
    phase_results: dict[str, PhaseResult | dict] | None,
    *,
    default_status: str = "SKIP",
    default_summary: str = NOT_EVALUATED_SUMMARY,
) -> dict[str, dict]:
    """Ensure phase_results contains keys \"0\" through \"6\"."""
    out: dict[str, dict] = {}
    raw = phase_results or {}
    for key in PHASE_KEYS:
        item = raw.get(key)
        if item is None:
            out[key] = {"status": default_status, "summary": default_summary}
        else:
            out[key] = phase_result_to_dict(item)
    return out


def dicts_to_phase_results(raw: dict[str, dict | PhaseResult]) -> dict[str, PhaseResult]:
    return {
        key: item if isinstance(item, PhaseResult) else PhaseResult(**item)
        for key, item in raw.items()
    }


def merge_deterministic_phase0(
    llm_phase_results: dict[str, PhaseResult | dict] | None,
    deterministic_phase0: dict,
) -> dict[str, dict]:
    """Merge LLM phase_results with deterministic Phase 0 (Phase 0 wins)."""
    merged = ensure_all_phase_results(llm_phase_results)
    merged["0"] = dict(deterministic_phase0)
    return merged


def dry_run_phase_results(phase0: dict) -> dict[str, PhaseResult]:
    """Populate all phase keys for dry-run mode."""
    raw = ensure_all_phase_results(
        {"0": phase0},
        default_status="SKIP",
        default_summary=DRY_RUN_SKIP_SUMMARY,
    )
    raw["0"] = dict(phase0)
    return {key: PhaseResult(**value) for key, value in raw.items()}


def any_phase_failed(phase_results: dict[str, dict | PhaseResult]) -> list[str]:
    """Return phase keys with status FAIL."""
    failed: list[str] = []
    for key in PHASE_KEYS:
        item = phase_results.get(key, {})
        if isinstance(item, PhaseResult):
            status = item.status
        else:
            status = item.get("status")
        if status == "FAIL":
            failed.append(key)
    return failed
