"""Run-local, tool-verified evidence shared by the reviewer and its workers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from threading import RLock
from typing import Literal

from pydantic import BaseModel, Field

from review.context import find_problem_text_path
from review.schemas import Severity


class EvidenceReference(BaseModel):
    source_id: str
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class EvidenceFinding(BaseModel):
    severity: Severity
    finding: str = Field(min_length=1, max_length=1000)
    citations: list[EvidenceReference] = Field(min_length=1, max_length=4)
    suggested_fix: str = Field(default="", max_length=800)


class PhaseEvidenceInput(BaseModel):
    phase: Literal["1", "2", "3", "4", "5", "6"]
    status: Literal["PASS", "FAIL", "SKIP"]
    summary: str = Field(min_length=1, max_length=1200)
    citations: list[EvidenceReference] = Field(default_factory=list, max_length=8)
    findings: list[EvidenceFinding] = Field(default_factory=list, max_length=6)


def panel_text_available(value: object) -> bool:
    """Reject CLI/unavailable placeholders while retaining real negative results."""
    text = str(value or "").strip().lower()
    return bool(text) and text not in {"none", "n/a"} and not text.startswith(
        ("not available", "holistic ai check: not available")
    )


class EvidenceLedger:
    """Receipts contain only bytes actually returned by a successful read.

    Reports cite immutable receipts rather than agent-invented paths/lines.
    This verifies provenance and coverage, not the semantic truth of a claim.
    Nothing is persisted across submissions or placed in model-writable state.
    """

    def __init__(self, repo_path: Path, artifacts: dict, panels: dict[str, str],
                 phase0_log: str, *, max_phase_chars: int = 4000):
        self._lock = RLock()
        self._sources: dict[str, dict] = {}
        self._reports: dict[str, dict] = {}
        self.max_phase_chars = max_phase_chars
        problem = find_problem_text_path(repo_path)
        problem_rel = str(problem.relative_to(repo_path)) if problem else None
        self.required_paths = {
            "1": [problem_rel],
            "2": [artifacts.get("Dockerfile"), artifacts.get("test.sh")],
            "3": [artifacts.get("test.patch")],
            "4": [artifacts.get("solution.patch")],
        }
        self.required = {"1", "2", "3", "4"}
        self.panel_paths: dict[str, set[str]] = {"5": set(), "6": set()}
        for key, phase in (("agent_runs", "5"), ("related_submissions", "6"),
                           ("holistic_check", "6")):
            value = panels.get(key, "").strip()
            if panel_text_available(value):
                path = f"platform:{key}"
                self.panel_paths[phase].add(path)
                self.required.add(phase)
                if key == "holistic_check":
                    self.panel_paths["5"].add(path)
        manifest = {**artifacts, "problem_description": problem_rel}
        self.manifest_id = self.add_source("manifest", json.dumps(manifest, indent=2))
        self.phase0_id = self.add_source("phase0_log", phase0_log)

    def add_source(self, path: str, text: str, start_line: int = 1) -> str:
        source_id = "src_" + hashlib.sha256(
            f"{path}\0{start_line}\0{text}".encode()
        ).hexdigest()[:16]
        with self._lock:
            self._sources[source_id] = {
                "source_id": source_id, "path": path, "start_line": start_line,
                "lines": text.splitlines(),
            }
        return source_id

    def source_listing(self) -> str:
        with self._lock:
            return json.dumps({
                "sources": [{k: v for k, v in s.items() if k != "lines"}
                            | {"end_line": s["start_line"] + len(s["lines"]) - 1}
                            for s in self._sources.values()],
                "recorded_phases": sorted(self._reports),
                "phase_reports": self._reports,
                "coverage_gaps": self.gaps(),
            })

    def _resolve(self, ref: EvidenceReference) -> dict:
        source = self._sources.get(ref.source_id)
        if source is None:
            raise ValueError(f"Unknown source_id {ref.source_id}; read the source first.")
        first = source["start_line"]
        last = first + len(source["lines"]) - 1
        start = ref.start_line if ref.start_line is not None else first
        end = ref.end_line if ref.end_line is not None else last
        if source["lines"]:
            if start < first or end > last or end < start:
                raise ValueError(f"Citation outside successfully read lines {first}-{last}.")
            excerpt = "\n".join(source["lines"][start - first:end - first + 1])
        else:
            if ref.start_line is not None or ref.end_line is not None:
                raise ValueError("Empty sources have no line numbers.")
            excerpt = ""
        if len(excerpt) > 1600:
            raise ValueError("Citation exceeds 1600 characters; select a narrower line range.")
        return {"source_id": ref.source_id, "path": source["path"],
                "start_line": start if source["lines"] else None,
                "end_line": end if source["lines"] else None, "excerpt": excerpt}

    def record(self, report: PhaseEvidenceInput) -> str:
        with self._lock:
            if report.status == "SKIP" and report.phase in self.required:
                raise ValueError("This phase has available artifacts/data and cannot be SKIP.")
            citations = [self._resolve(c) for c in report.citations]
            findings = []
            for finding in report.findings:
                findings.append({
                    "severity": finding.severity, "finding": finding.finding,
                    "suggested_fix": finding.suggested_fix,
                    "citations": [self._resolve(c) for c in finding.citations],
                })
            all_citations = citations + [c for f in findings for c in f["citations"]]
            paths = {c["path"] for c in all_citations}
            if report.status != "SKIP":
                if not paths:
                    raise ValueError("PASS/FAIL requires successfully read evidence citations.")
                required_paths = {p or "manifest" for p in self.required_paths.get(report.phase, [])}
                if "manifest" in required_paths and report.status == "PASS":
                    raise ValueError("A phase with a missing required artifact cannot PASS.")
                if required_paths - paths:
                    raise ValueError(f"Read and cite required artifacts: {sorted(required_paths - paths)}")
                if report.phase in self.panel_paths and not (paths & self.panel_paths[report.phase]):
                    raise ValueError("Read and cite the relevant cached platform panel.")
            elif findings:
                raise ValueError("SKIP cannot contain findings.")
            result = {"status": report.status, "summary": report.summary,
                      "citations": citations, "findings": findings}
            if len(json.dumps(result, ensure_ascii=False)) > self.max_phase_chars:
                raise ValueError(f"Phase report exceeds {self.max_phase_chars} characters; narrow excerpts and summarize.")
            # A later investigation may add evidence or escalate PASS to FAIL,
            # but cannot erase accepted citations/findings or undo a FAIL.
            previous = self._reports.get(report.phase)
            if previous and previous != result:
                def entries(items: list[dict]) -> set[str]:
                    return {json.dumps(item, sort_keys=True) for item in items}
                old_citations, new_citations = entries(previous["citations"]), entries(citations)
                old_findings, new_findings = entries(previous["findings"]), entries(findings)
                removes_evidence = not old_citations <= new_citations or not old_findings <= new_findings
                undoes_failure = previous["status"] == "FAIL" and report.status != "FAIL"
                adds_evidence = old_citations < new_citations or old_findings < new_findings
                escalates = previous["status"] == "PASS" and report.status == "FAIL"
                if removes_evidence or undoes_failure or not (adds_evidence or escalates):
                    raise ValueError("Existing phase evidence cannot be overwritten; retain prior citations/findings and add evidence or escalate to FAIL.")
            self._reports[report.phase] = result
            return f"Recorded Phase {report.phase}: {report.status}. Remaining gaps: {self.gaps()}"

    def gaps(self) -> list[str]:
        with self._lock:
            return sorted(self.required - self._reports.keys())

    def snapshot(self) -> dict:
        with self._lock:
            # Return a detached, JSON-serializable audit record.
            return json.loads(json.dumps({"phases": self._reports, "coverage_gaps": self.gaps()}))


def fit_finalize_payload(payload: dict, max_chars: int) -> str:
    """Bound optional context without slicing serialized JSON or evidence."""
    data = dict(payload)
    for key in ("explore_notes", "phase0_log", "holistic_ai_check", "agent_runs",
                "related_submissions", "loc_analysis", "prompt_analysis"):
        encoded = json.dumps(data, ensure_ascii=False)
        if len(encoded) <= max_chars:
            return encoded
        value = data.get(key)
        if isinstance(value, str):
            excess = len(encoded) - max_chars
            keep = max(0, len(value) - excess - 100)
            data[key] = value[:keep] + " [context omitted; structured evidence is authoritative]" if keep else ""
    encoded = json.dumps(data, ensure_ascii=False)
    if len(encoded) > max_chars:
        raise ValueError("Structured review evidence exceeds REVIEW_FINALIZE_PAYLOAD_MAX_CHARS; increase the budget.")
    return encoded
