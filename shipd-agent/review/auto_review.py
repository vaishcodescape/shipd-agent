# Predicted Auto Review. Shipd's real criteria are not published.
#
# The stage runs last and reads the submission as a whole. Every finding is
# labeled a prediction. A concrete disagreement among the prompt, tests, and
# solution is MAJOR. Speculative notes are QUESTION and do not fail the stage.

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from review.schemas import Finding, PhaseResult

PHASE_KEY = "auto_review"
PREDICTION_PREFIX = "Auto Review prediction: "
_PROMPT_FILES = ("problem.md", "PROBLEM.md", "description.md", "prompt.md")
# The shared review budget reserves two scoring calls for finalize; this stage
# gets its own two (first try plus one retry) on top, under the same token cap.
AUTO_REVIEW_CALLS = 2

SYSTEM_PROMPT = (
    "You predict Shipd Auto Review, the last stage before submission. Its real "
    "criteria are not published. Judge the submission as a whole and look for "
    "places where the parts disagree:\n"
    "- the prompt promises behavior that no test checks;\n"
    "- the tests pin names, formats, or values the prompt never states and an "
    "agent could not discover from the repository;\n"
    "- the solution does more or less than the prompt asks;\n"
    "- the agent run outcomes contradict the claimed difficulty or fairness.\n"
    "Severity MAJOR only for a concrete contradiction you can cite from the "
    "dossier (quote the file or panel). Everything speculative is QUESTION. "
    "Do not repeat earlier findings unless the parts disagree in a new way. "
    "Do not invent Shipd's 7-criterion Task Quality rubric or any other "
    "unpublished checklist. Do not prefix findings; the caller labels them."
)


class AutoReviewItem(BaseModel):
    severity: str = "QUESTION"
    finding: str
    evidence: str
    suggested_fix: str = ""


class AutoReviewPrediction(BaseModel):
    summary: str = ""
    findings: list[AutoReviewItem] = Field(default_factory=list)


def _read(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def local_contradictions(repo_path: Path) -> list[Finding]:
    """Cross-checks that do not depend on Shipd's unpublished rubric."""
    findings: list[Finding] = []
    solution = _read(repo_path / "solution.patch")
    tests = _read(repo_path / "test.patch")
    if tests.strip() and not solution.strip():
        findings.append(
            Finding(
                phase=PHASE_KEY,
                severity="MAJOR",
                finding=PREDICTION_PREFIX + "solution and tests disagree",
                evidence="test.patch adds checks and solution.patch is missing or empty.",
                suggested_fix="Add a solution that makes the new tests pass, or drop the tests that nothing implements.",
            )
        )
    if tests.strip() and not any((repo_path / name).is_file() for name in _PROMPT_FILES):
        findings.append(
            Finding(
                phase=PHASE_KEY,
                severity="QUESTION",
                finding=PREDICTION_PREFIX + "no prompt file to compare the tests against",
                evidence=f"test.patch exists but none of {', '.join(_PROMPT_FILES)} is in the clone.",
                suggested_fix="Confirm the task prompt is part of the submission so the tests can be checked against it.",
            )
        )
    return findings


def build_auto_review_dossier(
    *,
    quest: str,
    repo_path: Path,
    phase_results: dict[str, Any],
    findings: list[dict[str, Any]],
    prompt_analysis: str = "",
    agent_runs: str = "",
    draft: dict[str, Any] | None = None,
    agent_lineup: dict[str, str] | None = None,
    fingerprint: dict[str, Any] | None = None,
    platform_panel: str = "",
) -> str:
    """One packet so the prediction sees the submission together."""
    lines = [
        f"Quest: {quest}",
        "This is a prediction of Shipd Auto Review. The real criteria are not published.",
        "Report a MAJOR finding only for a concrete disagreement among the prompt, tests, solution, and runs.",
        "Use QUESTION for anything speculative. Do not invent the 7-criterion Task Quality rubric.",
    ]
    if fingerprint and fingerprint.get("content_hash"):
        lines.append(
            f"Content judged: sha256 {fingerprint['content_hash'][:16]} over "
            f"{', '.join(fingerprint.get('files') or []) or 'no tracked files'}."
        )
    if agent_lineup:
        lineup = ", ".join(f"{agent} = {model}" for agent, model in agent_lineup.items())
        lines.append(f"Agent line-up (expected, unverified): {lineup}.")
    if draft:
        bands = draft.get("band_ratings") or {}
        scores = ", ".join(
            f"{name} {band.get('score')}" for name, band in bands.items() if isinstance(band, dict)
        )
        lines.append(
            f"Draft review decision: {draft.get('decision', 'unknown')}"
            + (f"; band scores: {scores}" if scores else "")
            + "."
        )
    lines += [
        "",
        "Prompt analysis:",
        prompt_analysis or "(none)",
        "",
        "Agent runs:",
        agent_runs or "(none)",
        "",
    ]
    if platform_panel.strip():
        lines += [
            "Auto Review panel as shown on the page (may be stale or partial):",
            platform_panel,
            "",
        ]
    lines.append("Phase results:")
    for key in sorted(phase_results):
        item = phase_results[key]
        if isinstance(item, dict):
            lines.append(f"  {key}: {item.get('status')} — {item.get('summary', '')}")
    if findings:
        lines.append("Earlier findings:")
        for finding in findings:
            lines.append(
                f"  phase {finding.get('phase')} {finding.get('severity')}: {finding.get('finding')}"
            )
            lines.append(f"    Evidence: {finding.get('evidence', '')}")
            lines.append(f"    Suggested fix: {finding.get('suggested_fix', '')}")
    lines.append("")
    lines.append("Prompt:")
    for name in _PROMPT_FILES:
        text = _read(repo_path / name)
        if text:
            lines.append(f"File: {name}")
            lines.append(text)
            break
    lines.append("")
    lines.append("test.patch:")
    lines.append(_read(repo_path / "test.patch") or "(missing)")
    lines.append("")
    lines.append("solution.patch:")
    lines.append(_read(repo_path / "solution.patch") or "(missing)")
    return "\n".join(lines)


def prediction_from_model(raw: Any) -> AutoReviewPrediction | None:
    if isinstance(raw, AutoReviewPrediction):
        return raw
    if isinstance(raw, dict):
        if not any(key in raw for key in ("summary", "findings")):
            return None
        try:
            return AutoReviewPrediction.model_validate(raw)
        except Exception:
            return None
    return None


def apply_prediction(
    prediction: AutoReviewPrediction | None,
    *,
    local: list[Finding] | None = None,
    error: str = "",
) -> tuple[PhaseResult, list[Finding]]:
    """Turn a model prediction into a phase result. Local contradictions stay MAJOR."""
    findings = list(local or [])
    if prediction is None:
        summary = (
            "Auto Review prediction did not run"
            + (f": {error}" if error else ".")
            + " This is not Shipd's Auto Review verdict."
        )
        status = "FAIL" if any(item.severity in {"BLOCKER", "MAJOR"} for item in findings) else "SKIP"
        return PhaseResult(status=status, summary=summary), findings

    for item in prediction.findings:
        severity = item.severity.strip().upper()
        if severity not in {"MAJOR", "QUESTION", "MINOR", "BLOCKER"}:
            severity = "QUESTION"
        if severity == "BLOCKER":
            severity = "MAJOR"
        if severity == "MAJOR" and not item.evidence.strip():
            severity = "QUESTION"
        text = item.finding.strip()
        if not text.lower().startswith(PREDICTION_PREFIX.lower()):
            text = PREDICTION_PREFIX + text
        findings.append(
            Finding(
                phase=PHASE_KEY,
                severity=severity,  # type: ignore[arg-type]
                finding=text,
                evidence=item.evidence,
                suggested_fix=item.suggested_fix,
            )
        )
    blocking = any(item.severity in {"BLOCKER", "MAJOR"} for item in findings)
    summary = prediction.summary.strip() or "Auto Review prediction finished."
    if not summary.lower().startswith("auto review prediction"):
        summary = "Auto Review prediction: " + summary
    summary += " This is not Shipd's Auto Review verdict."
    return (
        PhaseResult(status="FAIL" if blocking else "PASS", summary=summary),
        findings,
    )


Completer = Callable[[str], Any]


def run_auto_review_prediction(
    dossier: str,
    *,
    complete: Completer | None,
    local: list[Finding] | None = None,
) -> tuple[PhaseResult, list[Finding]]:
    if complete is None:
        return apply_prediction(None, local=local, error="no model configured")
    try:
        raw = complete(dossier)
    except Exception as exc:
        return apply_prediction(None, local=local, error=str(exc)[:300])
    return apply_prediction(prediction_from_model(raw), local=local, error="model returned an unexpected shape")


def model_completer(config: Any, *, budget: Any = None) -> Completer:
    """Structured call on the auto_review model role, retried once on bad output."""
    from langchain_core.messages import HumanMessage, SystemMessage

    from review.deep import BudgetCallback, ReviewBudgetExceeded
    from review.llm import make_chat_model

    def complete(packet: str) -> Any:
        model = make_chat_model(config, "auto_review", structured_output=AutoReviewPrediction)
        invoke_config = None
        if budget is not None:
            limit = budget.total_limit + AUTO_REVIEW_CALLS
            invoke_config = {"callbacks": [BudgetCallback(budget, limit)]}
        human = packet
        last: Exception | None = None
        for _attempt in range(AUTO_REVIEW_CALLS):
            try:
                raw = model.invoke(
                    [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=human)],
                    config=invoke_config,
                )
            except ReviewBudgetExceeded:
                raise
            except Exception as exc:
                last = exc
                human = f"{packet}\n\nPrevious output failed validation (fix it): {str(exc)[:1000]}"
                continue
            if prediction_from_model(raw) is not None:
                return raw
            last = ValueError("model returned an unexpected shape")
            human = f"{packet}\n\nPrevious output did not match the AutoReviewPrediction schema."
        raise last or RuntimeError("Auto Review prediction failed")

    return complete


def run_auto_review(
    *,
    config: Any,
    repo_path: Path,
    draft: dict[str, Any],
    quest: str = "olympus",
    prompt_analysis: str = "",
    agent_runs: str = "",
    platform_panel: str = "",
    budget: Any = None,
    complete: Completer | None = None,
) -> tuple[PhaseResult, list[Finding]]:
    """Build the dossier from the draft review and run the prediction."""
    from review.staleness import content_fingerprint

    lineup = getattr(config, "expected_agent_models", None) or None
    dossier = build_auto_review_dossier(
        quest=quest,
        repo_path=repo_path,
        phase_results=draft.get("phase_results") or {},
        findings=list(draft.get("findings") or []),
        prompt_analysis=prompt_analysis,
        agent_runs=agent_runs,
        draft=draft,
        agent_lineup=lineup,
        fingerprint=content_fingerprint(repo_path, agent_lineup=lineup),
        platform_panel=platform_panel,
    )
    local = local_contradictions(repo_path)
    limit = getattr(config, "review_finalize_payload_max_chars", None) or 32000
    if len(dossier) > limit:
        return apply_prediction(
            None, local=local,
            error=f"complete dossier exceeds the {limit:,}-character payload budget; no artifacts were truncated",
        )
    if complete is None:
        complete = model_completer(config, budget=budget)
    return run_auto_review_prediction(dossier, complete=complete, local=local)
