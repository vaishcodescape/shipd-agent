# Chat-model factory for the review graph's two LLM roles.
#
#   explore  — ReAct tool-calling agent (create_react_agent)
#   finalize — single structured-output call returning ReviewResult
#
# The provider, models and API key come from ReviewConfig (review/config.py);
# provider quirks live in review/llm_providers.py. SDK integrations are imported
# lazily so only the selected provider's package has to be importable.

from __future__ import annotations

import functools
import importlib.util
import json
from typing import TYPE_CHECKING, Any, Literal

from review.llm_providers import (
    CLIENT_ANTHROPIC,
    CLIENT_GOOGLE_GENAI,
    CLIENT_OPENAI,
    CLIENT_OPENAI_COMPATIBLE,
    REASONING_EFFORT_MODEL_DEFAULT,
    ProviderSpec,
    get_provider,
)

if TYPE_CHECKING:
    from review.config import ReviewConfig

Role = Literal["explore", "finalize"]

CONFIGURE_HINT = "run ./run.sh --configure-llm"


class LLMConfigError(RuntimeError):
    """The configured provider cannot be used (missing key, model or package)."""


# Python package (import name, pip name) each client kind needs.
CLIENT_PACKAGES: dict[str, tuple[str, str]] = {
    CLIENT_ANTHROPIC: ("langchain_anthropic", "langchain-anthropic"),
    CLIENT_OPENAI: ("langchain_openai", "langchain-openai"),
    CLIENT_OPENAI_COMPATIBLE: ("langchain_openai", "langchain-openai"),
    CLIENT_GOOGLE_GENAI: ("langchain_google_genai", "langchain-google-genai"),
}


def missing_api_key_message(config: ReviewConfig) -> str | None:
    """Error text when the configured provider has no API key, else None."""
    if config.llm_api_key:
        return None
    spec = get_provider(config.llm_provider)
    env_names = " or ".join(spec.api_key_env)
    return (
        f"{spec.primary_key_env} is not set: no API key for {spec.label}. "
        f"Set {env_names} (environment or .env) or {CONFIGURE_HINT} to store one."
    )


def llm_config_error(config: ReviewConfig) -> str | None:
    """First reason the configured LLM cannot be built (key, model, URL, package)."""
    key_error = missing_api_key_message(config)
    if key_error:
        return key_error
    spec = get_provider(config.llm_provider)
    for role, env_name in (("explore", "REVIEW_EXPLORE_MODEL"), ("finalize", "REVIEW_MODEL")):
        if not role_model(config, role):  # type: ignore[arg-type]
            return (
                f"No {role} model configured for {spec.label}: set {env_name} "
                f"or {CONFIGURE_HINT}."
            )
    if spec.requires_base_url and not config.llm_base_url:
        return (
            f"{spec.label} needs REVIEW_LLM_BASE_URL (the endpoint's "
            "OpenAI-compatible base URL, usually ending in /v1)."
        )
    module, package = CLIENT_PACKAGES[spec.client]
    if importlib.util.find_spec(module) is None:
        return str(_import_error(spec, package))
    return None


def role_model(config: ReviewConfig, role: Role) -> str:
    return config.review_explore_model if role == "explore" else config.review_model


def _max_tokens(config: ReviewConfig, role: Role) -> int:
    if role == "explore":
        return config.review_explore_max_output_tokens
    return config.review_finalize_max_output_tokens


def _reasoning_effort(config: ReviewConfig, spec: ProviderSpec) -> str | None:
    if not spec.supports_reasoning_effort:
        return None
    effort = config.llm_reasoning_effort or spec.reasoning_effort or ""
    if not effort or effort == REASONING_EFFORT_MODEL_DEFAULT:
        return None
    return effort


def _import_error(spec: ProviderSpec, package: str) -> LLMConfigError:
    return LLMConfigError(
        f"{spec.label} needs the '{package}' package. Run ./run.sh --setup "
        f"(or pip install {package})."
    )


@functools.lru_cache(maxsize=1)
def openai_compatible_class() -> type:
    """BaseChatOpenAI subclass for non-OpenAI Chat Completions endpoints."""
    from langchain_core.messages import AIMessage
    from langchain_openai.chat_models.base import BaseChatOpenAI

    def _message_reasoning(message: Any) -> str | None:
        if message is None:
            return None
        if isinstance(message, dict):
            value = message.get("reasoning_content")
        else:
            value = getattr(message, "reasoning_content", None)
            if value is None:
                extra = getattr(message, "model_extra", None) or {}
                value = extra.get("reasoning_content")
        return value if isinstance(value, str) and value else None

    class OpenAICompatibleChat(BaseChatOpenAI):
        """Chat Completions client for OpenAI-compatible providers.

        Unlike ChatOpenAI it keeps ``max_tokens`` (or the provider's own name for
        it) instead of OpenAI's ``max_completion_tokens`` rewrite, can echo
        ``reasoning_content`` back on assistant turns for thinking models, and
        can send ``tool_choice="required"`` where named tool choice is rejected.
        """

        max_tokens_param: str = "max_tokens"
        preserve_reasoning_content: bool = False
        named_tool_choice_as_required: bool = False

        @property
        def _llm_type(self) -> str:
            return "openai-compatible-chat"

        def _get_request_payload(
            self, input_: Any, *, stop: list[str] | None = None, **kwargs: Any
        ) -> dict:
            payload = super()._get_request_payload(input_, stop=stop, **kwargs)
            if "messages" not in payload:  # Responses API payload; leave as is
                return payload
            if self.max_tokens_param != "max_tokens" and "max_tokens" in payload:
                payload[self.max_tokens_param] = payload.pop("max_tokens")
            if self.named_tool_choice_as_required and isinstance(
                payload.get("tool_choice"), dict
            ):
                # One bound tool (structured output) makes these equivalent.
                payload["tool_choice"] = "required"
            if self.preserve_reasoning_content:
                source = self._convert_input(input_).to_messages()
                for original, sent in zip(source, payload["messages"]):
                    if not isinstance(original, AIMessage):
                        continue
                    reasoning = original.additional_kwargs.get("reasoning_content")
                    if reasoning and sent.get("role") == "assistant":
                        sent["reasoning_content"] = reasoning
            return payload

        def _create_chat_result(
            self, response: Any, generation_info: dict | None = None
        ) -> Any:
            result = super()._create_chat_result(response, generation_info)
            if not self.preserve_reasoning_content or not result.generations:
                return result
            if isinstance(response, dict):
                choices = response.get("choices") or []
                message = choices[0].get("message") if choices else None
            else:
                choices = getattr(response, "choices", None) or []
                message = choices[0].message if choices else None
            reasoning = _message_reasoning(message)
            if reasoning:
                result.generations[0].message.additional_kwargs[
                    "reasoning_content"
                ] = reasoning
            return result

    return OpenAICompatibleChat


# additional_kwargs key holding a reply's original content blocks while the
# review graph works with plain text (see text_content_class).
CONTENT_BLOCKS_KEY = "__shipd_content_blocks__"


def _blocks_text(content: list) -> str:
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") in ("text", "output_text"):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _flatten_reply(generation: Any) -> None:
    from langchain_core.messages import AIMessage

    message = getattr(generation, "message", None)
    if isinstance(message, AIMessage) and isinstance(message.content, list):
        message.additional_kwargs[CONTENT_BLOCKS_KEY] = message.content
        message.content = _blocks_text(message.content)
        generation.text = message.content


def _restore_blocks(messages: Any) -> Any:
    from langchain_core.messages import AIMessage

    if not isinstance(messages, list):
        return messages
    restored = []
    for message in messages:
        if isinstance(message, AIMessage) and CONTENT_BLOCKS_KEY in message.additional_kwargs:
            extra = dict(message.additional_kwargs)
            blocks = extra.pop(CONTENT_BLOCKS_KEY)
            message = message.model_copy(update={"content": blocks, "additional_kwargs": extra})
        restored.append(message)
    return restored


@functools.lru_cache(maxsize=None)
def text_content_class(base: Any) -> Any:
    """Subclass of chat model ``base`` whose replies carry plain-text content.

    Responses-API GPT models, Gemini 3+ and thinking Claude models return
    content as a list of blocks (ids, thought signatures, reasoning). The review
    graph reads ``message.content`` as text (transcripts, phase-verdict parsing),
    so each reply's text is exposed as a string while the original blocks are
    stashed and put back before the next request; providers receive exactly
    what they returned. Plain-string replies are left untouched.
    """
    if not isinstance(base, type):  # a patched constructor in tests
        return base

    class TextContentChat(base):  # type: ignore[misc, valid-type]
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[no-untyped-def]
            result = super()._generate(
                _restore_blocks(messages), stop=stop, run_manager=run_manager, **kwargs
            )
            for generation in result.generations:
                _flatten_reply(generation)
            return result

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[no-untyped-def]
            result = await super()._agenerate(
                _restore_blocks(messages), stop=stop, run_manager=run_manager, **kwargs
            )
            for generation in result.generations:
                _flatten_reply(generation)
            return result

        def _stream(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[no-untyped-def]
            yield from super()._stream(
                _restore_blocks(messages), stop=stop, run_manager=run_manager, **kwargs
            )

        async def _astream(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[no-untyped-def]
            async for chunk in super()._astream(
                _restore_blocks(messages), stop=stop, run_manager=run_manager, **kwargs
            ):
                yield chunk

    TextContentChat.__name__ = TextContentChat.__qualname__ = base.__name__
    return TextContentChat


def _build_chat_model(config: ReviewConfig, spec: ProviderSpec, role: Role) -> Any:
    model = role_model(config, role)
    if not model:
        env_name = "REVIEW_EXPLORE_MODEL" if role == "explore" else "REVIEW_MODEL"
        raise LLMConfigError(
            f"No {role} model configured for {spec.label}: set {env_name} "
            f"or {CONFIGURE_HINT}."
        )
    api_key = config.llm_api_key
    max_tokens = _max_tokens(config, role)
    base_url = config.llm_base_url or spec.base_url
    effort = _reasoning_effort(config, spec)

    if spec.client == CLIENT_ANTHROPIC:
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError:
            raise _import_error(spec, "langchain-anthropic") from None
        kwargs: dict[str, Any] = {}
        if config.llm_base_url:
            kwargs["base_url"] = config.llm_base_url
        return text_content_class(ChatAnthropic)(
            model=model, api_key=api_key, max_tokens=max_tokens, **kwargs
        )

    if spec.client == CLIENT_GOOGLE_GENAI:
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError:
            raise _import_error(spec, "langchain-google-genai") from None
        kwargs = {}
        if config.llm_base_url:
            kwargs["base_url"] = config.llm_base_url
        if effort:
            kwargs["thinking_level"] = effort
        return text_content_class(ChatGoogleGenerativeAI)(
            model=model,
            api_key=api_key,
            max_output_tokens=max_tokens,
            vertexai=False,
            **kwargs,
        )

    if spec.client == CLIENT_OPENAI:
        try:
            from langchain_openai import ChatOpenAI
        except ImportError:
            raise _import_error(spec, "langchain-openai") from None
        kwargs = {}
        if base_url:
            kwargs["base_url"] = base_url
        if effort:
            kwargs["reasoning_effort"] = effort
        # Current GPT models only allow tool calling on the Responses API.
        return text_content_class(ChatOpenAI)(
            model=model,
            api_key=api_key,
            max_tokens=max_tokens,
            use_responses_api=True,
            **kwargs,
        )

    if spec.client == CLIENT_OPENAI_COMPATIBLE:
        if not base_url:
            raise LLMConfigError(
                f"{spec.label} needs REVIEW_LLM_BASE_URL (the endpoint's "
                "OpenAI-compatible /v1 base URL)."
            )
        try:
            chat_class = text_content_class(openai_compatible_class())
        except ImportError:
            raise _import_error(spec, "langchain-openai") from None
        kwargs = {}
        if effort:
            kwargs["reasoning_effort"] = effort
        if spec.extra_body:
            kwargs["extra_body"] = json.loads(json.dumps(dict(spec.extra_body)))
        if spec.drop_params:
            kwargs["disabled_params"] = {name: None for name in spec.drop_params}
        return chat_class(
            model=model,
            api_key=api_key,
            base_url=base_url,
            max_tokens=max_tokens,
            max_tokens_param=spec.max_tokens_param,
            preserve_reasoning_content=spec.preserve_reasoning_content,
            named_tool_choice_as_required=spec.named_tool_choice_as_required,
            **kwargs,
        )

    raise LLMConfigError(f"Unsupported client kind {spec.client!r} for {spec.name}.")


def structured_output_method(config: ReviewConfig) -> str | None:
    """Method passed to with_structured_output (None = integration default)."""
    spec = get_provider(config.llm_provider)
    return config.llm_structured_output_method or spec.structured_output_method


def _json_mode_instructions(schema: type) -> str:
    return (
        "Respond with a single JSON object (no prose, no code fences) that "
        "validates against this JSON Schema:\n"
        + json.dumps(schema.model_json_schema(), separators=(",", ":"))
    )


def make_chat_model(
    config: ReviewConfig,
    role: Role,
    *,
    structured_output: type | None = None,
) -> Any:
    """Build the chat model for ``role`` from ``config``.

    With ``structured_output`` (a Pydantic model) the result is a runnable that
    returns an instance of it, using the provider's structured-output method.
    Raises LLMConfigError when the provider is missing a key, model or package.
    """
    config_error = llm_config_error(config)
    if config_error:
        raise LLMConfigError(config_error)
    spec = get_provider(config.llm_provider)
    llm = _build_chat_model(config, spec, role)
    if structured_output is None:
        return llm

    method = structured_output_method(config)
    if method is None:
        return llm.with_structured_output(structured_output)
    structured = llm.with_structured_output(structured_output, method=method)
    if method != "json_mode":
        return structured

    # json_mode only guarantees *a* JSON object; spell out the schema in the
    # system prompt.
    from langchain_core.messages import SystemMessage
    from langchain_core.runnables import RunnableLambda

    instructions = _json_mode_instructions(structured_output)

    def _with_schema(messages: Any) -> Any:
        if not isinstance(messages, list):
            return messages
        first = messages[0] if messages else None
        if isinstance(first, SystemMessage) and isinstance(first.content, str):
            merged = SystemMessage(content=f"{first.content}\n\n{instructions}")
            return [merged, *messages[1:]]
        return [SystemMessage(content=instructions), *messages]

    return RunnableLambda(_with_schema) | structured


def describe_llm(config: ReviewConfig) -> str:
    """One-line, secret-free summary for logs."""
    spec = get_provider(config.llm_provider)
    return (
        f"provider={spec.name}, explore={config.review_explore_model}, "
        f"finalize={config.review_model}"
    )
