# Review agent configuration from environment.

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

from auth import REPO_ROOT
from review.credentials import resolve_api_key
from review.llm_providers import (
    STRUCTURED_OUTPUT_METHODS,
    ProviderSpec,
    foreign_model_owner,
    get_provider,
)
from review.rubric_defaults import (
    MARS_MAX_EFFECTIVE_LOC,
    MARS_MIN_EFFECTIVE_LOC,
    MAX_PROMPT_REQUIREMENTS,
    MAX_PROMPT_WORDS,
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
    anthropic_api_key: str = field(repr=False)
    review_model: str
    review_explore_model: str
    # LLM provider (review/llm_providers.py); review_model is the finalize
    # model and review_explore_model the explore model for this provider.
    llm_provider: str
    llm_api_key: str = field(repr=False)
    llm_api_key_source: str
    llm_base_url: str
    llm_reasoning_effort: str
    llm_structured_output_method: str
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
    max_prompt_words: int = MAX_PROMPT_WORDS
    max_prompt_requirements: int = MAX_PROMPT_REQUIREMENTS


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


def _model_from_env(spec: ProviderSpec, env_name: str, *, default: str) -> str:
    """Read a model env var for ``spec``, ignoring another provider's model ID."""
    raw = os.getenv(env_name, "").strip()
    owner = foreign_model_owner(spec, raw)
    if owner is not None:
        print(
            f"WARNING: {env_name}={raw!r} is a {owner.label} model but the "
            f"review provider is {spec.name}; using "
            f"{default or 'the explore model'!r} instead.",
            flush=True,
        )
        raw = ""
    return resolve_model_id(raw, default=default)


def resolve_review_models(spec: ProviderSpec) -> tuple[str, str]:
    """Return (explore_model, finalize_model) for ``spec`` from the environment.

    REVIEW_EXPLORE_MODEL / REVIEW_MODEL override the provider defaults; the
    finalize model falls back to the explore model when the provider has no
    default of its own.
    """
    explore = _model_from_env(
        spec, "REVIEW_EXPLORE_MODEL", default=spec.default_explore_model
    )
    finalize = _model_from_env(
        spec, "REVIEW_MODEL", default=spec.default_finalize_model or explore
    )
    return explore, finalize or explore


def _structured_output_method_from_env() -> str:
    method = os.getenv("REVIEW_STRUCTURED_OUTPUT_METHOD", "").strip().lower()
    if method and method not in STRUCTURED_OUTPUT_METHODS:
        raise ValueError(
            "REVIEW_STRUCTURED_OUTPUT_METHOD must be one of "
            f"{', '.join(STRUCTURED_OUTPUT_METHODS)} (got {method!r})."
        )
    return method


def get_review_config(*, dry_run_override: bool | None = None) -> ReviewConfig:
    """Load review settings from .env and environment."""
    load_dotenv(REPO_ROOT / ".env")

    dry_run_env = _truthy(os.getenv("REVIEW_DRY_RUN", "0"))
    dry_run = dry_run_override if dry_run_override is not None else dry_run_env

    provider = get_provider(os.getenv("REVIEW_PROVIDER", ""))
    explore_model, review_model = resolve_review_models(provider)
    # Provider env var(s) first, then the key saved by ./run.sh --configure-llm.
    resolved_key = resolve_api_key(provider)
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if provider.name == "anthropic" and resolved_key is not None:
        anthropic_api_key = resolved_key.value

    return ReviewConfig(
        anthropic_api_key=anthropic_api_key,
        review_model=review_model,
        review_explore_model=explore_model,
        llm_provider=provider.name,
        llm_api_key=resolved_key.value if resolved_key else "",
        llm_api_key_source=resolved_key.source if resolved_key else "",
        llm_base_url=os.getenv("REVIEW_LLM_BASE_URL", "").strip(),
        llm_reasoning_effort=os.getenv("REVIEW_REASONING_EFFORT", "").strip().lower(),
        llm_structured_output_method=_structured_output_method_from_env(),
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
        max_prompt_words=int(
            os.getenv("REVIEW_MAX_PROMPT_WORDS", str(MAX_PROMPT_WORDS))
        ),
        max_prompt_requirements=int(
            os.getenv("REVIEW_MAX_PROMPT_REQUIREMENTS", str(MAX_PROMPT_REQUIREMENTS))
        ),
    )
