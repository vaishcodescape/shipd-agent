# Review agent configuration from environment.

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from auth import REPO_ROOT
from review.rubric_defaults import MARS_MAX_EFFECTIVE_LOC, OLYMPUS_MAX_EFFECTIVE_LOC


@dataclass(frozen=True)
class ReviewConfig:
    anthropic_api_key: str
    review_model: str
    review_explore_model: str
    review_phase0: str
    review_dry_run: bool
    review_max_tool_steps: int
    review_rubric_max_chars: int
    review_skip_explore_on_phase0_fail: bool
    rubric_path: str
    olympus_max_loc: int
    mars_max_loc: int


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_review_config(*, dry_run_override: bool | None = None) -> ReviewConfig:
    """Load review settings from .env and environment."""
    load_dotenv(REPO_ROOT / ".env")

    dry_run_env = _truthy(os.getenv("REVIEW_DRY_RUN", "0"))
    dry_run = dry_run_override if dry_run_override is not None else dry_run_env

    review_model = os.getenv(
        "REVIEW_MODEL",
        "claude-opus-4-8",
    ).strip()
    explore_model = os.getenv("REVIEW_EXPLORE_MODEL", "").strip() or review_model

    return ReviewConfig(
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
        review_model=review_model,
        review_explore_model=explore_model,
        review_phase0=os.getenv("REVIEW_PHASE0", "fast").strip().lower(),
        review_dry_run=dry_run,
        review_max_tool_steps=int(os.getenv("REVIEW_MAX_TOOL_STEPS", "20")),
        review_rubric_max_chars=int(os.getenv("REVIEW_RUBRIC_MAX_CHARS", "16000")),
        review_skip_explore_on_phase0_fail=_truthy(
            os.getenv("REVIEW_SKIP_EXPLORE_ON_PHASE0_FAIL", "1")
        ),
        rubric_path=str(REPO_ROOT / "shipd-rubric.md"),
        olympus_max_loc=int(
            os.getenv("REVIEW_OLYMPUS_MAX_LOC", str(OLYMPUS_MAX_EFFECTIVE_LOC))
        ),
        mars_max_loc=int(
            os.getenv("REVIEW_MARS_MAX_LOC", str(MARS_MAX_EFFECTIVE_LOC))
        ),
    )
