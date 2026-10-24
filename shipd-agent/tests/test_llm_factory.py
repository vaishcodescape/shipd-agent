# Tests for the review chat-model factory (review/llm.py) and its wiring into
# review/graph.py. Constructors are patched or only build request payloads —
# no network calls and no tokens spent.

from __future__ import annotations

import dataclasses
import unittest
import warnings
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from review import graph as g
from review import llm
from review.config import ReviewConfig
from review.llm_providers import PROVIDERS
from review.schemas import ReviewResult

KEY = "sk-test-0123456789abcdefFACTORY"


def make_config(provider: str = "anthropic", **overrides) -> ReviewConfig:
    spec = PROVIDERS[provider]
    values = dict(
        anthropic_api_key=KEY if provider == "anthropic" else "",
        review_model=spec.default_finalize_model or "vendor/model",
        review_explore_model=spec.default_explore_model or "vendor/model",
        llm_provider=provider,
        llm_api_key=KEY,
        llm_api_key_source=f"env {spec.primary_key_env}",
        llm_base_url="",
        llm_reasoning_effort="",
        llm_structured_output_method="",
        review_phase0="full",
        review_phase0_test_timeout=600,
        review_phase0_docker_build_timeout=600,
        review_dry_run=False,
        review_max_tool_steps=15,
        review_rubric_max_chars=16000,
        review_tool_read_max_chars=12000,
        review_phase0_log_max_chars=8000,
        review_explore_transcript_max_chars=8000,
        review_tool_output_max_chars=600,
        review_scrape_panel_max_chars=2500,
        review_finalize_payload_max_chars=32000,
        review_explore_max_output_tokens=4096,
        review_finalize_max_output_tokens=8192,
        review_coverage_recheck=True,
        review_coverage_recheck_max_steps=6,
        review_skip_explore_on_phase0_fail=False,
        rubric_path="shipd-rubric.md",
        olympus_min_loc=400,
        mars_min_loc=100,
        mars_max_loc=600,
    )
    # Fill any fields added to ReviewConfig later with neutral values.
    for f in dataclasses.fields(ReviewConfig):
        values.setdefault(f.name, None)
    values.update(overrides)
    return ReviewConfig(**values)


@tool
def read_file(path: str) -> str:
    """Read a file from the repo."""
    return "contents"


HISTORY = [
    SystemMessage(content="system"),
    HumanMessage(content="review this"),
    AIMessage(
        content="",
        tool_calls=[{"name": "read_file", "args": {"path": "a.py"}, "id": "call_1"}],
        additional_kwargs={"reasoning_content": "I should read a.py"},
    ),
    ToolMessage(content="print(1)", tool_call_id="call_1", name="read_file"),
]


def _react_agent(model):
    """The graph's create_react_agent, minus its LangGraph v1 deprecation noise."""
    from langgraph.prebuilt import create_react_agent

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return create_react_agent(model, [read_file])


def explore_payload(cfg: ReviewConfig) -> dict:
    bound = llm.make_chat_model(cfg, "explore").bind_tools([read_file])
    return bound.bound._get_request_payload(HISTORY, **bound.kwargs)


def finalize_payload(cfg: ReviewConfig) -> dict:
    structured = llm.make_chat_model(cfg, "finalize", structured_output=ReviewResult)
    binding = structured.first  # RunnableBinding(model, tools, tool_choice, ...)
    return binding.bound._get_request_payload(HISTORY[:2], **binding.kwargs)


class ConstructorSelectionTests(unittest.TestCase):
    def test_anthropic_uses_chat_anthropic_exactly_as_before(self) -> None:
        with patch("langchain_anthropic.ChatAnthropic") as chat:
            llm.make_chat_model(make_config("anthropic"), "explore")
            chat.assert_called_once_with(
                model="claude-sonnet-4-6", api_key=KEY, max_tokens=4096
            )
            chat.reset_mock()
            llm.make_chat_model(make_config("anthropic"), "finalize", structured_output=ReviewResult)
            chat.assert_called_once_with(model="claude-opus-4-8", api_key=KEY, max_tokens=8192)
            # Library-default structured output (no method) = unchanged behaviour.
            chat.return_value.with_structured_output.assert_called_once_with(ReviewResult)

    def test_openai_uses_responses_api(self) -> None:
        with patch("langchain_openai.ChatOpenAI") as chat:
            llm.make_chat_model(make_config("openai"), "explore")
            kwargs = chat.call_args.kwargs
        self.assertEqual(kwargs["model"], "gpt-6.1-sol")
        self.assertEqual(kwargs["api_key"], KEY)
        self.assertIs(kwargs["use_responses_api"], True)
        self.assertEqual(kwargs["reasoning_effort"], "low")
        self.assertNotIn("base_url", kwargs)

    def test_google_uses_native_gemini_client(self) -> None:
        with patch("langchain_google_genai.ChatGoogleGenerativeAI") as chat:
            llm.make_chat_model(make_config("google"), "finalize", structured_output=ReviewResult)
            kwargs = chat.call_args.kwargs
            method = chat.return_value.with_structured_output.call_args.kwargs["method"]
        self.assertEqual(kwargs["model"], "gemini-4-argon")
        self.assertEqual(kwargs["max_output_tokens"], 8192)
        self.assertIs(kwargs["vertexai"], False)
        self.assertEqual(kwargs["thinking_level"], "low")
        self.assertEqual(method, "json_schema")

    def test_openai_compatible_providers_use_their_base_url(self) -> None:
        expected = {
            "deepseek": "https://api.deepseek.com",
            "moonshot": "https://api.moonshot.ai/v1",
            "xai": "https://api.x.ai/v1",
        }
        for provider, base_url in expected.items():
            with self.subTest(provider=provider):
                fake_class = MagicMock()
                with patch.object(llm, "openai_compatible_class", return_value=fake_class):
                    llm.make_chat_model(make_config(provider), "explore")
                kwargs = fake_class.call_args.kwargs
                self.assertEqual(kwargs["base_url"], base_url)
                self.assertEqual(kwargs["api_key"], KEY)
                self.assertEqual(kwargs["model"], PROVIDERS[provider].default_explore_model)
                self.assertEqual(kwargs["max_tokens"], 4096)

    def test_base_url_override_and_custom_provider(self) -> None:
        cfg = make_config("openai-compatible", llm_base_url="https://gw.example/v1")
        fake_class = MagicMock()
        with patch.object(llm, "openai_compatible_class", return_value=fake_class):
            llm.make_chat_model(cfg, "finalize", structured_output=ReviewResult)
        self.assertEqual(fake_class.call_args.kwargs["base_url"], "https://gw.example/v1")
        self.assertEqual(
            fake_class.return_value.with_structured_output.call_args.kwargs["method"],
            "function_calling",
        )

    def test_reasoning_effort_override_and_model_default(self) -> None:
        with patch("langchain_openai.ChatOpenAI") as chat:
            llm.make_chat_model(make_config("openai", llm_reasoning_effort="high"), "explore")
            self.assertEqual(chat.call_args.kwargs["reasoning_effort"], "high")
            llm.make_chat_model(make_config("openai", llm_reasoning_effort="default"), "explore")
            self.assertNotIn("reasoning_effort", chat.call_args.kwargs)
        # Providers without the knob never receive it.
        fake_class = MagicMock()
        with patch.object(llm, "openai_compatible_class", return_value=fake_class):
            llm.make_chat_model(make_config("deepseek", llm_reasoning_effort="high"), "explore")
        self.assertNotIn("reasoning_effort", fake_class.call_args.kwargs)

    def test_structured_output_method_override(self) -> None:
        cfg = make_config("xai", llm_structured_output_method="json_schema")
        fake_class = MagicMock()
        with patch.object(llm, "openai_compatible_class", return_value=fake_class):
            llm.make_chat_model(cfg, "finalize", structured_output=ReviewResult)
        self.assertEqual(
            fake_class.return_value.with_structured_output.call_args.kwargs["method"],
            "json_schema",
        )


class ConfigErrorTests(unittest.TestCase):
    def test_missing_key_message_names_env_var_and_setup_command(self) -> None:
        cfg = make_config("openai", llm_api_key="", llm_api_key_source="")
        message = llm.llm_config_error(cfg)
        self.assertIn("OPENAI_API_KEY is not set", message)
        self.assertIn("--configure-llm", message)
        with self.assertRaises(llm.LLMConfigError) as ctx:
            llm.make_chat_model(cfg, "explore")
        self.assertIn("OPENAI_API_KEY", str(ctx.exception))

    def test_custom_provider_requires_base_url_and_model(self) -> None:
        cfg = make_config("openai-compatible")
        self.assertIn("REVIEW_LLM_BASE_URL", llm.llm_config_error(cfg))
        cfg = make_config(
            "openai-compatible", review_explore_model="", llm_base_url="https://gw/v1"
        )
        self.assertIn("REVIEW_EXPLORE_MODEL", llm.llm_config_error(cfg))

    def test_missing_package_is_reported(self) -> None:
        cfg = make_config("google")
        with patch("importlib.util.find_spec", return_value=None):
            message = llm.llm_config_error(cfg)
        self.assertIn("langchain-google-genai", message)
        self.assertIn("--setup", message)

    def test_ready_config_has_no_error(self) -> None:
        for provider in ("anthropic", "openai", "google", "deepseek", "moonshot", "xai"):
            with self.subTest(provider=provider):
                self.assertIsNone(llm.llm_config_error(make_config(provider)))


class RequestPayloadTests(unittest.TestCase):
    """Build real clients and inspect the JSON they would send (no network)."""

    def test_deepseek_disables_thinking_and_keeps_max_tokens(self) -> None:
        explore = explore_payload(make_config("deepseek"))
        self.assertEqual(explore["max_tokens"], 4096)
        self.assertNotIn("max_completion_tokens", explore)
        self.assertEqual(explore["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertEqual(explore["messages"][2]["reasoning_content"], "I should read a.py")
        final = finalize_payload(make_config("deepseek"))
        self.assertEqual(final["tool_choice"]["function"]["name"], "ReviewResult")
        self.assertNotIn("parallel_tool_calls", final)
        self.assertEqual(final["max_tokens"], 8192)

    def test_moonshot_uses_max_completion_tokens_and_required_tool_choice(self) -> None:
        explore = explore_payload(make_config("moonshot"))
        self.assertEqual(explore["max_completion_tokens"], 4096)
        self.assertNotIn("max_tokens", explore)
        self.assertEqual(explore["reasoning_effort"], "low")
        self.assertEqual(explore["messages"][2]["reasoning_content"], "I should read a.py")
        final = finalize_payload(make_config("moonshot"))
        self.assertEqual(final["tool_choice"], "required")

    def test_xai_keeps_standard_chat_completions_shape(self) -> None:
        explore = explore_payload(make_config("xai"))
        self.assertEqual(explore["max_tokens"], 4096)
        self.assertNotIn("reasoning_content", explore["messages"][2])
        final = finalize_payload(make_config("xai"))
        self.assertEqual(final["tool_choice"]["function"]["name"], "ReviewResult")

    def test_openai_builds_responses_api_payload(self) -> None:
        explore = explore_payload(make_config("openai"))
        self.assertIn("input", explore)
        self.assertNotIn("messages", explore)
        self.assertEqual(explore["max_output_tokens"], 4096)
        self.assertEqual(explore["reasoning"], {"effort": "low"})
        final = finalize_payload(make_config("openai"))
        self.assertEqual(final["tool_choice"], {"type": "function", "name": "ReviewResult"})

    def test_reasoning_content_is_captured_from_responses(self) -> None:
        chat = llm.make_chat_model(make_config("moonshot"), "explore")
        response = {
            "id": "r1",
            "object": "chat.completion",
            "created": 0,
            "model": "kimi-k3.1-pro",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "done",
                        "reasoning_content": "thought about it",
                    },
                }
            ],
        }
        result = chat._create_chat_result(response)
        message = result.generations[0].message
        self.assertEqual(message.additional_kwargs["reasoning_content"], "thought about it")

    def test_json_mode_puts_schema_in_system_prompt(self) -> None:
        cfg = make_config("deepseek", llm_structured_output_method="json_mode")
        structured = llm.make_chat_model(cfg, "finalize", structured_output=ReviewResult)
        prepared = structured.first.invoke(HISTORY[:2])
        self.assertIsInstance(prepared[0], SystemMessage)
        self.assertTrue(prepared[0].content.startswith("system"))
        self.assertIn("JSON Schema", prepared[0].content)
        self.assertIn("band_ratings", prepared[0].content)
        self.assertEqual(prepared[1], HISTORY[1])


class MockTransportEndToEndTests(unittest.TestCase):
    """Explore ReAct loop + structured finalize over a fake HTTP endpoint."""

    def _attach(self, chat, handler) -> None:
        import httpx
        import openai

        client = openai.OpenAI(
            api_key=KEY,
            base_url=chat.openai_api_base,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            max_retries=0,
        )
        chat.root_client = client
        chat.client = client.chat.completions

    @staticmethod
    def _completion(message: dict) -> dict:
        return {
            "id": "c1",
            "object": "chat.completion",
            "created": 0,
            "model": "kimi-k3.1-pro",
            "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    def test_moonshot_explore_and_finalize(self) -> None:
        import json

        import httpx

        requests: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["authorization"], f"Bearer {KEY}")
            self.assertEqual(request.url.host, "api.moonshot.ai")
            body = json.loads(request.content)
            requests.append(body)
            if "tool_choice" in body and body["tool_choice"] == "required":
                review = {
                    "decision": "approve",
                    "band_ratings": {
                        name: {"score": 3, "confidence": "high", "reasoning": ""}
                        for name in ("problem", "tests", "solution")
                    },
                    "recommendation_summary": "ok",
                    "contributor_feedback": "ok",
                }
                call = {"id": "f1", "type": "function", "function": {
                    "name": "ReviewResult", "arguments": json.dumps(review)}}
                return httpx.Response(200, json=self._completion(
                    {"role": "assistant", "content": "", "tool_calls": [call]}))
            if len(requests) == 1:
                call = {"id": "t1", "type": "function", "function": {
                    "name": "read_file", "arguments": json.dumps({"path": "a.py"})}}
                return httpx.Response(200, json=self._completion({
                    "role": "assistant", "content": "", "tool_calls": [call],
                    "reasoning_content": "need to read a.py"}))
            return httpx.Response(200, json=self._completion(
                {"role": "assistant", "content": "notes: looks fine"}))

        cfg = make_config("moonshot")
        explore = llm.make_chat_model(cfg, "explore")
        self._attach(explore, handler)
        agent = _react_agent(explore)
        out = agent.invoke({"messages": [SystemMessage(content="s"), HumanMessage(content="go")]})
        self.assertEqual(out["messages"][-1].content, "notes: looks fine")
        self.assertEqual(len(requests), 2)
        echoed = [m for m in requests[1]["messages"] if m["role"] == "assistant"][0]
        self.assertEqual(echoed["reasoning_content"], "need to read a.py")
        self.assertEqual(requests[0]["max_completion_tokens"], 4096)

        structured = llm.make_chat_model(cfg, "finalize", structured_output=ReviewResult)
        self._attach(structured.first.bound, handler)
        review = structured.invoke([SystemMessage(content="s"), HumanMessage(content="score")])
        self.assertIsInstance(review, ReviewResult)
        self.assertEqual(review.decision, "approve")
        self.assertEqual(requests[-1]["max_completion_tokens"], 8192)
        self.assertNotIn("parallel_tool_calls", requests[-1])


    def test_openai_responses_text_is_flat_but_items_replayed(self) -> None:
        import json

        import httpx

        requests: list[dict] = []

        def response(output: list) -> dict:
            return {
                "id": f"resp_{len(requests)}", "object": "response", "created_at": 0,
                "status": "completed", "model": "gpt-6.1-sol", "output": output,
                "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
            }

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v1/responses")
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                return httpx.Response(200, json=response([
                    {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "E"},
                    {"type": "function_call", "id": "fc_1", "call_id": "call_1",
                     "name": "read_file", "arguments": json.dumps({"path": "a.py"}),
                     "status": "completed"},
                ]))
            return httpx.Response(200, json=response([
                {"type": "message", "id": f"msg_{len(requests)}", "role": "assistant",
                 "status": "completed",
                 "content": [{"type": "output_text", "text": "Phase 1: PASS\nPhase 2: FAIL",
                              "annotations": []}]},
            ]))

        explore = llm.make_chat_model(make_config("openai"), "explore")
        self._attach(explore, handler)
        agent = _react_agent(explore)
        out = agent.invoke({"messages": [SystemMessage(content="s"), HumanMessage(content="go")]})
        # The graph parses phase verdicts line by line from plain text.
        self.assertEqual(out["messages"][-1].content, "Phase 1: PASS\nPhase 2: FAIL")
        second_types = [item.get("type") for item in requests[1]["input"]]
        self.assertIn("reasoning", second_types)
        self.assertIn("function_call_output", second_types)

        # A follow-up turn (coverage recheck) replays the original message item.
        agent.invoke({"messages": out["messages"] + [HumanMessage(content="recheck")]})
        replayed = [item for item in requests[2]["input"] if item.get("id") == "msg_2"]
        self.assertEqual(len(replayed), 1)
        self.assertEqual(replayed[0]["content"][0]["text"], "Phase 1: PASS\nPhase 2: FAIL")
        for item in requests[2]["input"]:
            self.assertNotIn(llm.CONTENT_BLOCKS_KEY, json.dumps(item))


    def test_gemini_thought_signatures_survive_text_flattening(self) -> None:
        from google.genai import types

        calls: list[dict] = []

        def fake_generate(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                part = types.Part(
                    function_call=types.FunctionCall(name="read_file", args={"path": "a.py"}),
                    thought_signature=b"SIG1",
                )
            else:
                part = types.Part(text="Phase 1: PASS\nPhase 2: FAIL", thought_signature=b"SIG2")
            return types.GenerateContentResponse(
                candidates=[
                    types.Candidate(
                        content=types.Content(role="model", parts=[part]), finish_reason="STOP"
                    )
                ]
            )

        chat = llm.make_chat_model(make_config("google"), "explore")
        with patch.object(chat.client.models, "generate_content", side_effect=fake_generate):
            agent = _react_agent(chat)
            out = agent.invoke({"messages": [SystemMessage(content="s"), HumanMessage(content="go")]})
            self.assertEqual(out["messages"][-1].content, "Phase 1: PASS\nPhase 2: FAIL")
            agent.invoke({"messages": out["messages"] + [HumanMessage(content="recheck")]})

        def signatures(request: dict) -> list:
            return [p.thought_signature for c in request["contents"] for p in c.parts or []]

        self.assertIn(b"SIG1", signatures(calls[1]))
        self.assertIn(b"SIG1", signatures(calls[2]))
        self.assertIn(b"SIG2", signatures(calls[2]))


    def test_anthropic_blocks_replayed_unchanged(self) -> None:
        import json

        import anthropic

        try:  # newer anthropic SDKs ship their own httpx fork
            import httpx2 as httpx_mod
        except ImportError:
            import httpx as httpx_mod

        calls: list[dict] = []

        def handler(request):
            calls.append(json.loads(request.content))
            if len(calls) == 1:
                content = [
                    {"type": "thinking", "thinking": "hmm", "signature": "SIGA"},
                    {"type": "text", "text": "Reading a.py"},
                    {"type": "tool_use", "id": "toolu_1", "name": "read_file",
                     "input": {"path": "a.py"}},
                ]
                stop = "tool_use"
            else:
                content = [{"type": "text", "text": "Phase 1: PASS\nPhase 2: FAIL"}]
                stop = "end_turn"
            return httpx_mod.Response(200, json={
                "id": f"msg_{len(calls)}", "type": "message", "role": "assistant",
                "model": "claude-sonnet-4-6", "content": content, "stop_reason": stop,
                "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1},
            })

        chat = llm.make_chat_model(make_config("anthropic"), "explore")
        try:
            client = anthropic.Client(
                api_key=KEY,
                http_client=httpx_mod.Client(transport=httpx_mod.MockTransport(handler)),
                max_retries=0,
            )
        except TypeError as exc:  # SDK/httpx pairing differs in this environment
            self.skipTest(f"cannot inject a mock transport: {exc}")
        chat.__dict__["_client"] = client  # ChatAnthropic._client is a cached_property

        agent = _react_agent(chat)
        out = agent.invoke({"messages": [SystemMessage(content="s"), HumanMessage(content="go")]})
        self.assertEqual(out["messages"][2].content, "Reading a.py")
        self.assertEqual(out["messages"][-1].content, "Phase 1: PASS\nPhase 2: FAIL")
        assistant = [m for m in calls[1]["messages"] if m["role"] == "assistant"][0]
        self.assertEqual(
            [block["type"] for block in assistant["content"]], ["thinking", "text", "tool_use"]
        )
        self.assertEqual(assistant["content"][0]["signature"], "SIGA")
        self.assertEqual(calls[0]["max_tokens"], 4096)
        self.assertEqual(calls[0]["model"], "claude-sonnet-4-6")


class GraphWiringTests(unittest.TestCase):
    def test_finalize_without_key_falls_back_with_clear_message(self) -> None:
        cfg = make_config("xai", llm_api_key="", llm_api_key_source="")
        out = g.finalize_node({"config": cfg, "quest": "olympus"})["review_result"]
        self.assertEqual(out["decision"], "request_changes")
        self.assertIn("XAI_API_KEY is not set", out["internal_notes"])

    def test_finalize_requests_structured_review_result(self) -> None:
        cfg = make_config("deepseek")
        with patch.object(g, "make_chat_model", side_effect=RuntimeError("stop")) as factory:
            with self.assertRaises(RuntimeError):
                g.finalize_node(
                    {"config": cfg, "quest": "olympus", "phase_results": {}, "findings": []}
                )
        factory.assert_called_once_with(cfg, "finalize", structured_output=ReviewResult)


if __name__ == "__main__":
    unittest.main()
