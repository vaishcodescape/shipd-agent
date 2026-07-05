# Review agent configuration from environment.

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from auth import REPO_ROOT
from review.rubric_defaults import (
    MARS_MAX_EFFECTIVE_LOC,
    MARS_MIN_EFFECTIVE_LOC,
    OLYMPUS_MIN_EFFECTIVE_LOC,
)

# Retired Anthropic IDs still seen in older .env files → current API model IDs.
DEPRECATED_MODEL_ALIASES: dict[str, str] = {
    "claude-opus-4-20250514": "claude-opus-4-8",
    "claude-opus-4-6": "claude-opus-4-8",
    "claude-opus-4-20251101": "claude-opus-4-8",
    "claude-sonnet-4-20250514": "claude-sonnet-4-6",
}

DEFAULT_REVIEW_MODEL = "claude-opus-4-8"
DEFAULT_EXPLORE_MODEL = "claude-sonnet-4-6"

# Token / context budgets (character caps approximate input size; output in tokens).
DEFAULT_REVIEW_MAX_TOOL_STEPS = 15
DEFAULT_REVIEW_TOOL_READ_MAX_CHARS = 12_000
DEFAULT_REVIEW_PHASE0_LOG_MAX_CHARS = 8_000
DEFAULT_REVIEW_EXPLORE_TRANSCRIPT_MAX_CHARS = 8_000
DEFAULT_REVIEW_TOOL_OUTPUT_MAX_CHARS = 600
DEFAULT_REVIEW_SCRAPE_PANEL_MAX_CHARS = 2_500
DEFAULT_REVIEW_FINALIZE_PAYLOAD_MAX_CHARS = 32_000
DEFAULT_REVIEW_EXPLORE_MAX_OUTPUT_TOKENS = 4_096
DEFAULT_REVIEW_FINALIZE_MAX_OUTPUT_TOKENS = 8_192

# Coverage recheck: after the main explore pass, re-prompt the same agent to
# gather evidence for any rubric phase it skipped (bounded, conditional, and
# only fires when a required phase has no evidence).
DEFAULT_REVIEW_COVERAGE_RECHECK = True
DEFAULT_REVIEW_COVERAGE_RECHECK_MAX_STEPS = 6


@dataclass(frozen=True)
class ReviewConfig:
    anthropic_api_key: str
    review_model: str
    review_explore_model: str
    review_phase0: str
    review_phase0_test_timeout: int
    review_phase0_docker_build_timeout: int
    review_dry_run: bool
    review_max_tool_steps: int
    review_rubric_max_chars: int
    review_tool_read_max_chars: int
    review_phase0_log_max_chars: int
    review_explore_transcript_max_chars: int
    review_tool_output_max_chars: int
    review_scrape_panel_max_chars: int
    review_finalize_payload_max_chars: int
    review_explore_max_output_tokens: int
    review_finalize_max_output_tokens: int
    review_coverage_recheck: bool
    review_coverage_recheck_max_steps: int
    review_skip_explore_on_phase0_fail: bool
    rubric_path: str
    olympus_min_loc: int
    mars_min_loc: int
    mars_max_loc: int


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def resolve_model_id(raw: str, *, default: str) -> str:
    """Return a supported model ID, remapping known retired aliases."""
    model = raw.strip() or default
    resolved = DEPRECATED_MODEL_ALIASES.get(model, model)
    if resolved != model:
        print(
            f"WARNING: REVIEW model {model!r} is retired; using {resolved!r} instead.",
            flush=True,
        )
    return resolved


def get_review_config(*, dry_run_override: bool | None = None) -> ReviewConfig:
    """Load review settings from .env and environment."""
    load_dotenv(REPO_ROOT / ".env")

    dry_run_env = _truthy(os.getenv("REVIEW_DRY_RUN", "0"))
    dry_run = dry_run_override if dry_run_override is not None else dry_run_env

    review_model = resolve_model_id(
        os.getenv("REVIEW_MODEL", DEFAULT_REVIEW_MODEL),
        default=DEFAULT_REVIEW_MODEL,
    )
    explore_raw = os.getenv("REVIEW_EXPLORE_MODEL", "").strip()
    explore_model = resolve_model_id(
        explore_raw or DEFAULT_EXPLORE_MODEL,
        default=DEFAULT_EXPLORE_MODEL,
    )

    return ReviewConfig(
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
        review_model=review_model,
        review_explore_model=explore_model,
        review_phase0=os.getenv("REVIEW_PHASE0", "full").strip().lower(),
        review_phase0_test_timeout=int(
            os.getenv("REVIEW_PHASE0_TEST_TIMEOUT", "600")
        ),
        review_phase0_docker_build_timeout=int(
            os.getenv("REVIEW_PHASE0_DOCKER_BUILD_TIMEOUT", "600")
        ),
        review_dry_run=dry_run,
        review_max_tool_steps=int(
            os.getenv("REVIEW_MAX_TOOL_STEPS", str(DEFAULT_REVIEW_MAX_TOOL_STEPS))
        ),
        review_rubric_max_chars=int(os.getenv("REVIEW_RUBRIC_MAX_CHARS", "16000")),
        review_tool_read_max_chars=int(
            os.getenv(
                "REVIEW_TOOL_READ_MAX_CHARS",
                str(DEFAULT_REVIEW_TOOL_READ_MAX_CHARS),
            )
        ),
        review_phase0_log_max_chars=int(
            os.getenv(
                "REVIEW_PHASE0_LOG_MAX_CHARS",
                str(DEFAULT_REVIEW_PHASE0_LOG_MAX_CHARS),
            )
        ),
        review_explore_transcript_max_chars=int(
            os.getenv(
                "REVIEW_EXPLORE_TRANSCRIPT_MAX_CHARS",
                str(DEFAULT_REVIEW_EXPLORE_TRANSCRIPT_MAX_CHARS),
            )
        ),
        review_tool_output_max_chars=int(
            os.getenv(
                "REVIEW_TOOL_OUTPUT_MAX_CHARS",
                str(DEFAULT_REVIEW_TOOL_OUTPUT_MAX_CHARS),
            )
        ),
        review_scrape_panel_max_chars=int(
            os.getenv(
                "REVIEW_SCRAPE_PANEL_MAX_CHARS",
                str(DEFAULT_REVIEW_SCRAPE_PANEL_MAX_CHARS),
            )
        ),
        review_finalize_payload_max_chars=int(
            os.getenv(
                "REVIEW_FINALIZE_PAYLOAD_MAX_CHARS",
                str(DEFAULT_REVIEW_FINALIZE_PAYLOAD_MAX_CHARS),
            )
        ),
        review_explore_max_output_tokens=int(
            os.getenv(
                "REVIEW_EXPLORE_MAX_OUTPUT_TOKENS",
                str(DEFAULT_REVIEW_EXPLORE_MAX_OUTPUT_TOKENS),
            )
        ),
        review_finalize_max_output_tokens=int(
            os.getenv(
                "REVIEW_FINALIZE_MAX_OUTPUT_TOKENS",
                str(DEFAULT_REVIEW_FINALIZE_MAX_OUTPUT_TOKENS),
            )
        ),
        review_coverage_recheck=_truthy(
            os.getenv(
                "REVIEW_COVERAGE_RECHECK",
                "1" if DEFAULT_REVIEW_COVERAGE_RECHECK else "0",
            )
        ),
        review_coverage_recheck_max_steps=int(
            os.getenv(
                "REVIEW_COVERAGE_RECHECK_MAX_STEPS",
                str(DEFAULT_REVIEW_COVERAGE_RECHECK_MAX_STEPS),
            )
        ),
        # Default off: even when Phase 0 fails, the rubric requires evaluating
        # phases 1-6 so contributor feedback covers the whole submission.
        review_skip_explore_on_phase0_fail=_truthy(
            os.getenv("REVIEW_SKIP_EXPLORE_ON_PHASE0_FAIL", "0")
        ),
        rubric_path=str(REPO_ROOT / "shipd-rubric.md"),
        olympus_min_loc=int(
            os.getenv(
                "REVIEW_OLYMPUS_MIN_LOC",
                os.getenv("REVIEW_OLYMPUS_MAX_LOC", str(OLYMPUS_MIN_EFFECTIVE_LOC)),
            )
        ),
        mars_min_loc=int(
            os.getenv("REVIEW_MARS_MIN_LOC", str(MARS_MIN_EFFECTIVE_LOC))
        ),
        mars_max_loc=int(
            os.getenv("REVIEW_MARS_MAX_LOC", str(MARS_MAX_EFFECTIVE_LOC))
        ),
    )
