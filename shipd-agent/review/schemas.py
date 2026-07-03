# Pydantic models for structured Shipd review output.

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Decision = Literal["approve", "request_changes", "reject"]
Confidence = Literal["high", "medium", "low"]
PhaseStatus = Literal["PASS", "FAIL", "SKIP"]
Severity = Literal["BLOCKER", "MAJOR", "MINOR", "QUESTION"]


class BandRating(BaseModel):
    score: int = Field(ge=0, le=3)
    confidence: Confidence
    reasoning: str = ""

    @model_validator(mode="after")
    def reasoning_required_when_low_score(self) -> BandRating:
        if self.score < 3 and not self.reasoning.strip():
            raise ValueError(f"reasoning required when score is {self.score}")
        return self


class BandRatings(BaseModel):
    problem: BandRating
    tests: BandRating
    solution: BandRating


class PhaseResult(BaseModel):
    status: PhaseStatus
    summary: str


class Finding(BaseModel):
    phase: str
    severity: Severity
    finding: str
    evidence: str
    suggested_fix: str = ""


class ReviewResult(BaseModel):
    decision: Decision
    band_ratings: BandRatings
    quality: int | None = Field(default=None, ge=1, le=3)
    difficulty: int | None = Field(default=None, ge=1, le=3)
    repo_eligible: bool | None = None
    solvability_ok: bool | None = None
    phase_results: dict[str, PhaseResult] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)
    loc_analysis: str = ""
    agent_run_notes: str = "not available"
    related_submissions_notes: str = "not available"
    holistic_check_notes: str = "not available"
    other_notes: str = ""
    suggested_tags: list[str] = Field(default_factory=list)
    recommendation_summary: str
    contributor_feedback: str
    internal_notes: str = ""
    downgrade_to_mars: bool | None = None

    @field_validator("phase_results", mode="before")
    @classmethod
    def coerce_phase_results(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        out: dict[str, PhaseResult | dict] = {}
        for key, item in value.items():
            if isinstance(item, PhaseResult):
                out[str(key)] = item
            elif isinstance(item, dict):
                out[str(key)] = PhaseResult(**item)
            else:
                out[str(key)] = item
        return out

    def to_submit_dict(self) -> dict:
        """Dict compatible with shipd_submit.submit_review()."""
        data = self.model_dump(mode="json")
        phase_results = {
            key: {"status": pr["status"], "summary": pr["summary"]}
            for key, pr in data.get("phase_results", {}).items()
        }
        data["phase_results"] = phase_results
        if data.get("downgrade_to_mars") is None:
            data.pop("downgrade_to_mars", None)
        return data
