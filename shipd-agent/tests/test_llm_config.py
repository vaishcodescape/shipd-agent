# Tests for review LLM provider/model resolution (review/config.py +
# review/llm_providers.py). No network, no real keychain: every test runs with
# a cleared environment, a temp file-backed credential store and a temp repo
# root, so the developer's real .env is never read.

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from review import credentials
from review import config as config_module
from review.config import (
    DEFAULT_EXPLORE_MODEL,
    DEFAULT_REVIEW_MODEL,
    get_review_config,
)
from review.llm_providers import (
    DEFAULT_PROVIDER,
    PROVIDERS,
    foreign_model_owner,
    get_provider,
    normalize_provider_name,
)

FAKE_KEY = "sk-test-0123456789abcdefTESTKEY"


class IsolatedEnvTestCase(unittest.TestCase):
    """Cleared env + temp repo root (.env) + temp file credential store."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo_root = self.tmp / "repo"
        self.repo_root.mkdir()
        self.base_env = {
            "HOME": str(self.tmp / "home"),
            "SHIPD_CREDENTIAL_STORE": "file",
            "SHIPD_CONFIG_DIR": str(self.tmp / "config"),
            "PATH": os.environ.get("PATH", ""),
        }
        self._patches = [
            patch.object(config_module, "REPO_ROOT", self.repo_root),
            patch("builtins.print"),  # silence model-mismatch warnings
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def env(self, **values: str) -> dict[str, str]:
        return {**self.base_env, **values}

    def write_dotenv(self, text: str) -> None:
        (self.repo_root / ".env").write_text(text, encoding="utf-8")

    def load(self, **values: str):
        with patch.dict(os.environ, self.env(**values), clear=True):
            return get_review_config()


class ProviderRegistryTests(unittest.TestCase):
    def test_default_provider_is_anthropic(self) -> None:
        self.assertEqual(DEFAULT_PROVIDER, "anthropic")
        self.assertEqual(normalize_provider_name(""), "anthropic")

    def test_anthropic_defaults_match_historical_config_defaults(self) -> None:
        spec = PROVIDERS["anthropic"]
        self.assertEqual(spec.default_explore_model, DEFAULT_EXPLORE_MODEL)
        self.assertEqual(spec.default_finalize_model, DEFAULT_REVIEW_MODEL)
        self.assertEqual(spec.api_key_env, ("ANTHROPIC_API_KEY",))

    def test_required_providers_are_registered(self) -> None:
        for name in ("anthropic", "openai", "google", "deepseek", "moonshot", "xai"):
            self.assertIn(name, PROVIDERS)

    def test_aliases_and_case(self) -> None:
        cases = {
            "Claude": "anthropic",
            "GPT": "openai",
            "gemini": "google",
            "kimi": "moonshot",
            "grok": "xai",
            " DeepSeek ": "deepseek",
            "custom": "openai-compatible",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_provider_name(raw), expected)

    def test_unknown_provider_lists_valid_names(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            get_provider("mistery-llm")
        message = str(ctx.exception)
        self.assertIn("mistery-llm", message)
        self.assertIn("openai", message)
        self.assertIn("anthropic", message)

    def test_every_provider_has_key_env_and_sane_fields(self) -> None:
        for spec in PROVIDERS.values():
            with self.subTest(provider=spec.name):
                self.assertTrue(spec.api_key_env)
                self.assertTrue(all(e.isupper() for e in spec.api_key_env))
                if not spec.requires_base_url:
                    self.assertTrue(spec.default_explore_model)
                    self.assertIn(spec.default_explore_model, spec.suggested_models)
                if spec.base_url:
                    self.assertTrue(spec.base_url.startswith("https://"))

    def test_foreign_model_owner(self) -> None:
        openai = PROVIDERS["openai"]
        self.assertEqual(foreign_model_owner(openai, "claude-opus-4-8").name, "anthropic")
        self.assertIsNone(foreign_model_owner(openai, "gpt-6.1-sol"))
        # Gateways with free-form IDs are never second-guessed.
        custom = PROVIDERS["openai-compatible"]
        self.assertIsNone(foreign_model_owner(custom, "claude-opus-4-8"))
        self.assertIsNone(foreign_model_owner(openai, ""))


class BackwardCompatibilityTests(IsolatedEnvTestCase):
    def test_anthropic_only_dotenv_behaves_as_before(self) -> None:
        self.write_dotenv(
            f"ANTHROPIC_API_KEY={FAKE_KEY}\n"
            "REVIEW_MODEL=claude-opus-4-8\n"
            "REVIEW_EXPLORE_MODEL=claude-sonnet-4-6\n"
        )
        cfg = self.load()
        self.assertEqual(cfg.llm_provider, "anthropic")
        self.assertEqual(cfg.review_model, "claude-opus-4-8")
        self.assertEqual(cfg.review_explore_model, "claude-sonnet-4-6")
        self.assertEqual(cfg.anthropic_api_key, FAKE_KEY)
        self.assertEqual(cfg.llm_api_key, FAKE_KEY)
        self.assertEqual(cfg.llm_api_key_source, "env ANTHROPIC_API_KEY")
        self.assertEqual(cfg.llm_base_url, "")
        self.assertEqual(cfg.llm_structured_output_method, "")

    def test_no_env_at_all_uses_historical_defaults(self) -> None:
        cfg = self.load()
        self.assertEqual(cfg.llm_provider, "anthropic")
        self.assertEqual(cfg.review_model, DEFAULT_REVIEW_MODEL)
        self.assertEqual(cfg.review_explore_model, DEFAULT_EXPLORE_MODEL)
        self.assertEqual(cfg.llm_api_key, "")
        self.assertEqual(cfg.anthropic_api_key, "")

    def test_retired_anthropic_alias_still_remapped(self) -> None:
        cfg = self.load(REVIEW_MODEL="claude-opus-4-20250514")
        self.assertEqual(cfg.review_model, "claude-opus-4-8")

    def test_process_env_wins_over_dotenv(self) -> None:
        self.write_dotenv("REVIEW_PROVIDER=anthropic\nREVIEW_EXPLORE_MODEL=claude-sonnet-4-6\n")
        cfg = self.load(REVIEW_PROVIDER="openai", REVIEW_EXPLORE_MODEL="gpt-6-luna")
        self.assertEqual(cfg.llm_provider, "openai")
        self.assertEqual(cfg.review_explore_model, "gpt-6-luna")

    def test_config_repr_never_contains_keys(self) -> None:
        cfg = self.load(ANTHROPIC_API_KEY=FAKE_KEY)
        self.assertNotIn(FAKE_KEY, repr(cfg))


class ModelResolutionTests(IsolatedEnvTestCase):
    def test_provider_defaults_when_no_model_env(self) -> None:
        for name, spec in PROVIDERS.items():
            if spec.requires_base_url:
                continue
            with self.subTest(provider=name):
                cfg = self.load(REVIEW_PROVIDER=name)
                self.assertEqual(cfg.llm_provider, name)
                self.assertEqual(cfg.review_explore_model, spec.default_explore_model)
                self.assertEqual(cfg.review_model, spec.default_finalize_model)

    def test_explore_override_keeps_provider_finalize_default(self) -> None:
        cfg = self.load(REVIEW_PROVIDER="deepseek", REVIEW_EXPLORE_MODEL="deepseek-v4-pro")
        self.assertEqual(cfg.review_explore_model, "deepseek-v4-pro")
        self.assertEqual(cfg.review_model, PROVIDERS["deepseek"].default_finalize_model)

    def test_both_models_overridable(self) -> None:
        cfg = self.load(
            REVIEW_PROVIDER="xai",
            REVIEW_EXPLORE_MODEL="grok-4.3",
            REVIEW_MODEL="grok-4.20-0309-reasoning",
        )
        self.assertEqual(cfg.review_explore_model, "grok-4.3")
        self.assertEqual(cfg.review_model, "grok-4.20-0309-reasoning")

    def test_empty_model_env_means_provider_default(self) -> None:
        # shipd.sh blanks these when --provider switches away from .env's provider.
        cfg = self.load(REVIEW_PROVIDER="google", REVIEW_MODEL="", REVIEW_EXPLORE_MODEL="")
        self.assertEqual(cfg.review_explore_model, PROVIDERS["google"].default_explore_model)
        self.assertEqual(cfg.review_model, PROVIDERS["google"].default_finalize_model)

    def test_finalize_falls_back_to_explore_without_provider_default(self) -> None:
        cfg = self.load(
            REVIEW_PROVIDER="openai-compatible",
            REVIEW_EXPLORE_MODEL="vendor/model-x",
            REVIEW_LLM_BASE_URL="https://gateway.example/v1",
        )
        self.assertEqual(cfg.review_explore_model, "vendor/model-x")
        self.assertEqual(cfg.review_model, "vendor/model-x")
        self.assertEqual(cfg.llm_base_url, "https://gateway.example/v1")

    def test_stale_claude_model_ignored_for_other_provider(self) -> None:
        # .env.example used to ship REVIEW_MODEL=claude-opus-4-8 uncommented.
        self.write_dotenv("REVIEW_MODEL=claude-opus-4-8\nREVIEW_EXPLORE_MODEL=claude-sonnet-4-6\n")
        cfg = self.load(REVIEW_PROVIDER="openai")
        self.assertEqual(cfg.review_model, PROVIDERS["openai"].default_finalize_model)
        self.assertEqual(cfg.review_explore_model, PROVIDERS["openai"].default_explore_model)

    def test_invalid_provider_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.load(REVIEW_PROVIDER="not-a-provider")

    def test_invalid_structured_output_method_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.load(REVIEW_STRUCTURED_OUTPUT_METHOD="xml")
        cfg = self.load(REVIEW_STRUCTURED_OUTPUT_METHOD="JSON_MODE")
        self.assertEqual(cfg.llm_structured_output_method, "json_mode")


class KeyPrecedenceTests(IsolatedEnvTestCase):
    def test_stored_key_used_when_env_missing(self) -> None:
        with patch.dict(os.environ, self.env(), clear=True):
            credentials.store_key("openai", "sk-stored-0123456789abcdef")
        cfg = self.load(REVIEW_PROVIDER="openai")
        self.assertEqual(cfg.llm_api_key, "sk-stored-0123456789abcdef")
        self.assertTrue(cfg.llm_api_key_source.startswith("file "))
        # Stored keys never leak into the environment.
        self.assertNotIn("OPENAI_API_KEY", os.environ)

    def test_env_key_wins_over_stored_key(self) -> None:
        with patch.dict(os.environ, self.env(), clear=True):
            credentials.store_key("openai", "sk-stored-0123456789abcdef")
        cfg = self.load(REVIEW_PROVIDER="openai", OPENAI_API_KEY=FAKE_KEY)
        self.assertEqual(cfg.llm_api_key, FAKE_KEY)
        self.assertEqual(cfg.llm_api_key_source, "env OPENAI_API_KEY")

    def test_dotenv_key_wins_over_stored_key(self) -> None:
        with patch.dict(os.environ, self.env(), clear=True):
            credentials.store_key("anthropic", "sk-ant-stored-0123456789abcdef")
        self.write_dotenv(f"ANTHROPIC_API_KEY={FAKE_KEY}\n")
        cfg = self.load()
        self.assertEqual(cfg.llm_api_key, FAKE_KEY)

    def test_stored_anthropic_key_populates_legacy_field(self) -> None:
        with patch.dict(os.environ, self.env(), clear=True):
            credentials.store_key("anthropic", "sk-ant-stored-0123456789abcdef")
        cfg = self.load()
        self.assertEqual(cfg.anthropic_api_key, "sk-ant-stored-0123456789abcdef")

    def test_google_accepts_either_env_name(self) -> None:
        cfg = self.load(REVIEW_PROVIDER="gemini", GOOGLE_API_KEY=FAKE_KEY)
        self.assertEqual(cfg.llm_provider, "google")
        self.assertEqual(cfg.llm_api_key_source, "env GOOGLE_API_KEY")
        cfg = self.load(REVIEW_PROVIDER="gemini", GEMINI_API_KEY=FAKE_KEY, GOOGLE_API_KEY="x" * 20)
        self.assertEqual(cfg.llm_api_key_source, "env GEMINI_API_KEY")

    def test_other_providers_key_is_not_borrowed(self) -> None:
        cfg = self.load(REVIEW_PROVIDER="openai", ANTHROPIC_API_KEY=FAKE_KEY)
        self.assertEqual(cfg.llm_api_key, "")


if __name__ == "__main__":
    unittest.main()
