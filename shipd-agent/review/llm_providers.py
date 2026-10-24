# LLM provider registry for the review agent (pure data — no SDK imports).
#
# Adding another OpenAI-compatible provider is a one-entry change: append a
# ProviderSpec with client="openai_compatible", its base_url, key env var and
# default models. review/llm.py builds the client from these fields.
#
# Base URLs, key env vars and model IDs were checked against each provider's
# official API docs (see docs_url) in October 2026.

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

DEFAULT_PROVIDER = "anthropic"

# Client kinds understood by review/llm.py.
CLIENT_ANTHROPIC = "anthropic"  # langchain_anthropic.ChatAnthropic
CLIENT_OPENAI = "openai"  # langchain_openai.ChatOpenAI (Responses API)
CLIENT_OPENAI_COMPATIBLE = "openai_compatible"  # Chat Completions + base_url
CLIENT_GOOGLE_GENAI = "google_genai"  # langchain_google_genai (native Gemini API)

STRUCTURED_OUTPUT_METHODS = ("function_calling", "json_schema", "json_mode")

# REVIEW_REASONING_EFFORT value meaning "send nothing; use the model's default".
REASONING_EFFORT_MODEL_DEFAULT = "default"


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    label: str
    # Env vars checked (in order) for the API key; the first is canonical.
    api_key_env: tuple[str, ...]
    client: str
    default_explore_model: str
    default_finalize_model: str
    suggested_models: tuple[str, ...]
    base_url: str | None = None
    # with_structured_output() method for the finalize role; None keeps the
    # LangChain integration's own default.
    structured_output_method: str | None = "function_calling"
    # Default reasoning effort (thinking level for Gemini); None sends nothing.
    reasoning_effort: str | None = None
    # Whether REVIEW_REASONING_EFFORT can be forwarded to this provider.
    supports_reasoning_effort: bool = False
    # --- OpenAI-compatible (Chat Completions) quirks ---
    max_tokens_param: str = "max_tokens"
    extra_body: Mapping[str, object] | None = None
    # Echo `reasoning_content` back on assistant turns (thinking models that
    # reject or degrade tool-call histories without it).
    preserve_reasoning_content: bool = False
    # Send tool_choice="required" instead of naming the one bound tool.
    named_tool_choice_as_required: bool = False
    # Request params the endpoint does not document (never sent).
    drop_params: tuple[str, ...] = ()
    # Model-ID prefixes owned by this provider (spots a stale REVIEW_MODEL from
    # another provider, e.g. claude-* left in .env after switching to OpenAI).
    model_prefixes: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    docs_url: str = ""
    # True when the user must supply REVIEW_LLM_BASE_URL (generic endpoint).
    requires_base_url: bool = False
    notes: str = ""

    @property
    def primary_key_env(self) -> str:
        return self.api_key_env[0]


def _frozen(mapping: dict) -> Mapping[str, object]:
    return MappingProxyType(mapping)


PROVIDERS: dict[str, ProviderSpec] = {
    spec.name: spec
    for spec in (
        ProviderSpec(
            name="anthropic",
            label="Anthropic (Claude)",
            api_key_env=("ANTHROPIC_API_KEY",),
            client=CLIENT_ANTHROPIC,
            # Unchanged historical defaults (review/config.py) so existing .env
            # files behave exactly as before.
            default_explore_model="claude-sonnet-4-6",
            default_finalize_model="claude-opus-4-8",
            suggested_models=(
                "claude-opus-5-5",
                "claude-sonnet-5-5",
                "claude-haiku-5-5",
                "claude-fable-5-1",
                "claude-opus-4-8",
                "claude-sonnet-4-6",
            ),
            structured_output_method=None,
            model_prefixes=("claude-",),
            aliases=("claude",),
            docs_url="https://platform.claude.com/docs/en/about-claude/models/overview",
        ),
        ProviderSpec(
            name="openai",
            label="OpenAI (GPT)",
            api_key_env=("OPENAI_API_KEY",),
            # GPT-6 models only support tool calling on the Responses API.
            client=CLIENT_OPENAI,
            default_explore_model="gpt-6.1-sol",
            default_finalize_model="gpt-6.1-sol",
            suggested_models=("gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna"),
            reasoning_effort="low",
            supports_reasoning_effort=True,
            model_prefixes=("gpt-", "o1", "o3", "o4"),
            aliases=("gpt", "chatgpt"),
            docs_url="https://developers.openai.com/api/docs/models",
        ),
        ProviderSpec(
            name="google",
            label="Google (Gemini)",
            api_key_env=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
            # Native API: LangChain round-trips Gemini 3+ thought signatures on
            # tool calls, which the OpenAI-compatible endpoint path drops.
            client=CLIENT_GOOGLE_GENAI,
            default_explore_model="gemini-3.8-flash",
            default_finalize_model="gemini-4-argon",
            suggested_models=(
                "gemini-3.8-flash",
                "gemini-4-argon",
                "gemini-3.6-flash",
                "gemini-3.5-flash-lite",
            ),
            structured_output_method="json_schema",
            # Thinking tokens count toward max_output_tokens; "low" is accepted
            # by every listed model and keeps the explore budget usable.
            reasoning_effort="low",
            supports_reasoning_effort=True,
            model_prefixes=("gemini-",),
            aliases=("gemini", "gemini-api"),
            docs_url="https://ai.google.dev/gemini-api/docs/models",
        ),
        ProviderSpec(
            name="deepseek",
            label="DeepSeek",
            api_key_env=("DEEPSEEK_API_KEY",),
            client=CLIENT_OPENAI_COMPATIBLE,
            base_url="https://api.deepseek.com",
            default_explore_model="deepseek-flash",
            default_finalize_model="deepseek-v4-pro",
            suggested_models=("deepseek-flash", "deepseek-v4-pro"),
            # Thinking mode (the API default) rejects forced tool_choice and
            # requires reasoning_content echoes; non-thinking is also cheaper.
            extra_body=_frozen({"thinking": {"type": "disabled"}}),
            preserve_reasoning_content=True,
            drop_params=("parallel_tool_calls",),
            model_prefixes=("deepseek-",),
            docs_url="https://api-docs.deepseek.com/api/create-chat-completion",
        ),
        ProviderSpec(
            name="moonshot",
            label="Moonshot (Kimi)",
            api_key_env=("MOONSHOT_API_KEY",),
            client=CLIENT_OPENAI_COMPATIBLE,
            base_url="https://api.moonshot.ai/v1",
            default_explore_model="kimi-k3.1-pro",
            default_finalize_model="kimi-k3.1-pro",
            suggested_models=("kimi-k3.1-pro", "kimi-k3.1-pro-highspeed", "kimi-k3"),
            # K3 models always think (default effort "max"); keep it low so
            # reasoning does not eat the output-token budget.
            reasoning_effort="low",
            supports_reasoning_effort=True,
            max_tokens_param="max_completion_tokens",
            preserve_reasoning_content=True,
            named_tool_choice_as_required=True,
            drop_params=("parallel_tool_calls",),
            model_prefixes=("kimi-", "moonshot-"),
            aliases=("kimi",),
            docs_url="https://platform.kimi.ai/docs/api/chat",
        ),
        ProviderSpec(
            name="xai",
            label="xAI (Grok)",
            api_key_env=("XAI_API_KEY",),
            client=CLIENT_OPENAI_COMPATIBLE,
            base_url="https://api.x.ai/v1",
            default_explore_model="grok-4.7",
            default_finalize_model="grok-4.7",
            suggested_models=(
                "grok-4.7",
                "grok-4.3",
                "grok-4.20-0309-reasoning",
                "grok-4.20-0309-non-reasoning",
            ),
            model_prefixes=("grok-",),
            aliases=("grok",),
            docs_url="https://docs.x.ai/docs/models",
        ),
        ProviderSpec(
            name="openai-compatible",
            label="Other OpenAI-compatible endpoint (set REVIEW_LLM_BASE_URL)",
            api_key_env=("REVIEW_LLM_API_KEY",),
            client=CLIENT_OPENAI_COMPATIBLE,
            default_explore_model="",
            default_finalize_model="",
            suggested_models=(),
            drop_params=("parallel_tool_calls",),
            aliases=("custom", "openai_compatible"),
            requires_base_url=True,
            notes="Any Chat Completions API with tool calling (gateways, self-hosted servers).",
        ),
    )
}


def provider_names() -> tuple[str, ...]:
    return tuple(PROVIDERS)


def normalize_provider_name(raw: str) -> str:
    """Map a user-supplied provider name or alias to its registry key."""
    name = (raw or "").strip().lower()
    if not name:
        return DEFAULT_PROVIDER
    if name in PROVIDERS:
        return name
    for spec in PROVIDERS.values():
        if name in spec.aliases:
            return spec.name
    valid = ", ".join(provider_names())
    raise ValueError(f"Unknown LLM provider {raw!r}. Choose one of: {valid}.")


def get_provider(raw: str) -> ProviderSpec:
    return PROVIDERS[normalize_provider_name(raw)]


def foreign_model_owner(spec: ProviderSpec, model: str) -> ProviderSpec | None:
    """Return the other provider that obviously owns ``model``, if any.

    Only flags models with another registered provider's prefix while not
    matching this provider's own prefixes (gateways with free-form IDs are
    never flagged).
    """
    if not spec.model_prefixes or not model:
        return None
    lowered = model.lower()
    if lowered.startswith(spec.model_prefixes):
        return None
    for other in PROVIDERS.values():
        if other.name != spec.name and other.model_prefixes and lowered.startswith(
            other.model_prefixes
        ):
            return other
    return None
