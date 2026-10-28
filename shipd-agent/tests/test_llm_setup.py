# Tests for the LLM setup CLI (review/llm_setup.py) and its scripts/shipd.sh
# integration: hidden key prompts, secure storage, masked --check output, and
# fail-fast behaviour without a TTY. Temp HOME/config/.env only; no network.

from __future__ import annotations

import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from review import config as config_module
from review import credentials, llm_setup

KEY = "sk-test-0123456789abcdefCLISECRET"
REPO_ROOT = Path(__file__).resolve().parents[2]


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo_root = self.tmp / "repo"
        self.repo_root.mkdir()
        self.env_file = self.repo_root / ".env"
        self.env = {
            "HOME": str(self.tmp / "home"),
            "SHIPD_CREDENTIAL_STORE": "file",
            "SHIPD_CONFIG_DIR": str(self.tmp / "config"),
        }
        self._patches = [
            patch.dict(os.environ, self.env, clear=True),
            patch.object(config_module, "REPO_ROOT", self.repo_root),
            patch.object(llm_setup, "REPO_ROOT", self.repo_root),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def run_cli(self, *argv: str, interactive: bool = False, inputs=(), secret: str = ""):
        out, err = io.StringIO(), io.StringIO()
        answers = iter(inputs)
        with (
            patch.object(llm_setup, "_interactive", return_value=interactive),
            patch("builtins.input", side_effect=lambda prompt="": next(answers)),
            patch.object(llm_setup.getpass, "getpass", return_value=secret) as getpass_mock,
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = llm_setup.main(list(argv))
        self.getpass_mock = getpass_mock
        return code, out.getvalue(), err.getvalue()


class EnsureKeyTests(CliTestCase):
    def test_non_interactive_missing_key_fails_fast_with_guidance(self) -> None:
        os.environ["REVIEW_PROVIDER"] = "openai"
        code, out, err = self.run_cli("ensure-key", interactive=False)
        self.assertEqual(code, 1)
        self.assertIn("OPENAI_API_KEY is not set", out)
        self.assertIn("--configure-llm", out)
        self.assertIn("No terminal", out)
        self.getpass_mock.assert_not_called()

    def test_interactive_prompt_stores_key_hidden(self) -> None:
        os.environ["REVIEW_PROVIDER"] = "deepseek"
        code, out, err = self.run_cli("ensure-key", interactive=True, secret=KEY)
        self.assertEqual(code, 0, out + err)
        self.getpass_mock.assert_called_once()
        self.assertEqual(credentials.get_stored_key("deepseek").value, KEY)
        self.assertNotIn(KEY, out + err)
        self.assertIn("deepseek-flash", out)
        # Never written to the repo .env.
        self.assertFalse(self.env_file.exists())

    def test_interactive_empty_answer_aborts(self) -> None:
        code, out, err = self.run_cli("ensure-key", interactive=True, secret="")
        self.assertEqual(code, 1)
        self.assertIsNone(credentials.get_stored_key("anthropic"))

    def test_env_key_needs_no_prompt(self) -> None:
        os.environ["ANTHROPIC_API_KEY"] = KEY
        code, out, err = self.run_cli("ensure-key", interactive=True)
        self.assertEqual(code, 0)
        self.getpass_mock.assert_not_called()
        self.assertIn("env ANTHROPIC_API_KEY", out)
        self.assertNotIn(KEY, out + err)

    def test_invalid_provider_is_a_clean_error(self) -> None:
        os.environ["REVIEW_PROVIDER"] = "nope"
        code, out, err = self.run_cli("ensure-key")
        self.assertEqual(code, 2)
        self.assertIn("Unknown LLM provider", err)


class OnboardTests(CliTestCase):
    def onboard(self, *argv: str, **kwargs):
        return self.run_cli("onboard", "--env-file", str(self.env_file), *argv, **kwargs)

    def test_env_key_asks_nothing(self) -> None:
        os.environ["ANTHROPIC_API_KEY"] = KEY
        code, out, err = self.onboard(interactive=True)
        self.assertEqual((code, out), (0, ""))
        self.getpass_mock.assert_not_called()

    def test_stored_key_asks_nothing(self) -> None:
        credentials.store_key("anthropic", KEY)
        code, out, err = self.onboard(interactive=True)
        self.assertEqual((code, out), (0, ""))
        self.getpass_mock.assert_not_called()

    def test_no_terminal_leaves_the_missing_key_to_preflight(self) -> None:
        code, out, err = self.onboard(interactive=False)
        self.assertEqual((code, out), (0, ""))
        self.getpass_mock.assert_not_called()
        self.assertFalse(self.env_file.exists())

    def test_first_run_saves_settings_without_asking_and_stores_key(self) -> None:
        stale = {
            "REVIEW_PROVIDER": "xai",
            "REVIEW_LLM_BASE_URL": "https://old-gateway.example/v1",
        }
        self.env_file.write_text(
            "AUTH_EMAIL=user@example.com\n"
            + "".join(f"{key}={value}\n" for key, value in stale.items()),
            encoding="utf-8",
        )
        os.environ.update(stale)  # scripts/shipd.sh exports .env before onboard
        code, out, err = self.onboard(
            interactive=True,
            inputs=["openai", "", ""],  # provider, explore, finalize; no save prompt
            secret=KEY,
        )
        self.assertEqual(code, llm_setup.ONBOARD_SAVED_SETTINGS, out + err)
        self.assertIn("First run", out)
        self.assertIn("starting the agent", out)
        text = self.env_file.read_text(encoding="utf-8")
        self.assertIn("AUTH_EMAIL=user@example.com", text)
        self.assertIn("REVIEW_PROVIDER=openai\n", text)
        self.assertIn("REVIEW_EXPLORE_MODEL=gpt-6.1-sol\n", text)
        self.assertNotIn("REVIEW_LLM_BASE_URL", text)
        self.assertEqual(os.environ["REVIEW_PROVIDER"], "openai")
        self.assertNotIn("REVIEW_LLM_BASE_URL", os.environ)
        self.assertEqual(credentials.get_stored_key("openai").value, KEY)
        self.assertNotIn(KEY, text + out + err)

    def test_first_run_with_existing_key_for_chosen_provider_asks_no_key(self) -> None:
        credentials.store_key("google", KEY)
        code, out, err = self.onboard(interactive=True, inputs=["gemini", "", "", "n"])
        self.assertEqual(code, llm_setup.ONBOARD_SAVED_SETTINGS, out + err)
        self.getpass_mock.assert_not_called()
        self.assertIn("REVIEW_PROVIDER=google\n", self.env_file.read_text(encoding="utf-8"))

    def test_first_run_without_key_saves_nothing(self) -> None:
        code, out, err = self.onboard(interactive=True, inputs=["", "", ""], secret="")
        self.assertEqual(code, 1)
        self.assertIn("ERROR: ANTHROPIC_API_KEY is not set", out)
        self.assertFalse(self.env_file.exists())
        self.assertIsNone(credentials.get_stored_key("anthropic"))

    def test_key_only_skips_the_provider_menu(self) -> None:
        os.environ["REVIEW_PROVIDER"] = "deepseek"
        code, out, err = self.onboard("--key-only", interactive=True, secret=KEY)
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("LLM provider:", out)
        self.assertIn("no API key found for DeepSeek", out)
        self.assertEqual(credentials.get_stored_key("deepseek").value, KEY)
        self.assertFalse(self.env_file.exists())
        self.assertNotIn(KEY, out + err)

    def test_invalid_provider_is_a_clean_error(self) -> None:
        os.environ["REVIEW_PROVIDER"] = "nope"
        code, out, err = self.onboard(interactive=True)
        self.assertEqual(code, 2)
        self.assertIn("Unknown LLM provider", err)


class StatusTests(CliTestCase):
    def test_status_masks_key(self) -> None:
        os.environ.update(REVIEW_PROVIDER="xai", XAI_API_KEY=KEY)
        code, out, err = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertNotIn(KEY, out + err)
        self.assertIn(KEY[-4:], out)
        self.assertIn("LLM provider: xAI (Grok) [xai]", out)
        self.assertIn("https://api.x.ai/v1", out)

    def test_status_masks_stored_key(self) -> None:
        credentials.store_key("moonshot", KEY)
        os.environ["REVIEW_PROVIDER"] = "kimi"
        code, out, err = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertNotIn(KEY, out + err)
        self.assertIn("configured via file", out)

    def test_status_missing_key_warns(self) -> None:
        os.environ["REVIEW_PROVIDER"] = "google"
        code, out, err = self.run_cli("status")
        self.assertEqual(code, 1)
        self.assertIn("LLM API key: not configured (GEMINI_API_KEY)", out)
        self.assertIn("WARNING:", out)


class ForgetTests(CliTestCase):
    def test_forget_removes_stored_key(self) -> None:
        credentials.store_key("openai", KEY)
        os.environ["REVIEW_PROVIDER"] = "openai"
        code, out, err = self.run_cli("forget")
        self.assertEqual(code, 0)
        self.assertIn("Removed the stored OpenAI (GPT) key", out)
        self.assertIsNone(credentials.get_stored_key("openai"))
        code, out, err = self.run_cli("forget")
        self.assertIn("No stored OpenAI (GPT) key", out)

    def test_forget_notes_env_var_still_in_use(self) -> None:
        credentials.store_key("anthropic", KEY)
        os.environ["ANTHROPIC_API_KEY"] = "sk-env-0123456789abcdef"
        code, out, err = self.run_cli("forget")
        self.assertIn("ANTHROPIC_API_KEY is still set", out)
        self.assertNotIn("sk-env-0123456789abcdef", out)

    def test_forget_all(self) -> None:
        credentials.store_key("openai", KEY)
        credentials.store_key("xai", KEY)
        code, out, err = self.run_cli("forget", "--all")
        self.assertEqual(code, 0)
        self.assertIsNone(credentials.get_stored_key("openai"))
        self.assertIsNone(credentials.get_stored_key("xai"))


class ConfigureTests(CliTestCase):
    def test_requires_terminal(self) -> None:
        code, out, err = self.run_cli("configure", "--env-file", str(self.env_file))
        self.assertEqual(code, 2)
        self.assertIn("interactive terminal", err)

    def test_full_flow_writes_settings_but_not_key(self) -> None:
        self.env_file.write_text(
            "AUTH_EMAIL=user@example.com\n"
            "# keep this comment\n"
            "REVIEW_MODEL=claude-opus-4-8\n"
            "REVIEW_LLM_BASE_URL=https://old-gateway.example/v1\n",
            encoding="utf-8",
        )
        code, out, err = self.run_cli(
            "configure",
            "--env-file",
            str(self.env_file),
            interactive=True,
            inputs=["openai", "", "gpt-6-astra", ""],  # provider, explore, finalize, save
            secret=KEY,
        )
        self.assertEqual(code, 0, out + err)
        text = self.env_file.read_text(encoding="utf-8")
        self.assertIn("REVIEW_PROVIDER=openai\n", text)
        self.assertIn("REVIEW_EXPLORE_MODEL=gpt-6.1-sol\n", text)
        self.assertIn("REVIEW_MODEL=gpt-6-astra\n", text)
        self.assertIn("AUTH_EMAIL=user@example.com", text)
        self.assertIn("# keep this comment", text)
        # A base URL meant for another provider must not receive the OpenAI key.
        self.assertNotIn("REVIEW_LLM_BASE_URL", text)
        self.assertNotIn(KEY, text)
        self.assertNotIn(KEY, out + err)
        self.assertEqual(credentials.get_stored_key("openai").value, KEY)
        cred_file = credentials.credentials_path()
        self.assertEqual(stat.S_IMODE(cred_file.stat().st_mode), 0o600)

    def test_existing_stored_key_kept_unless_replaced(self) -> None:
        credentials.store_key("xai", KEY)
        code, out, err = self.run_cli(
            "configure",
            "--env-file",
            str(self.env_file),
            interactive=True,
            inputs=["grok", "2", "1", "n", "y"],  # provider, explore #2, finalize #1, replace?, save
        )
        self.assertEqual(code, 0, out + err)
        self.getpass_mock.assert_not_called()
        self.assertEqual(credentials.get_stored_key("xai").value, KEY)
        self.assertNotIn(KEY, out)
        text = self.env_file.read_text(encoding="utf-8")
        self.assertIn("REVIEW_PROVIDER=xai\n", text)
        self.assertIn("REVIEW_EXPLORE_MODEL=grok-4.3\n", text)
        self.assertIn("REVIEW_MODEL=grok-4.7\n", text)
        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode) & 0o077, 0)

    def test_custom_provider_prompts_for_base_url(self) -> None:
        code, out, err = self.run_cli(
            "configure",
            "--env-file",
            str(self.env_file),
            interactive=True,
            inputs=["custom", "vendor/model-x", "", "https://gw.example/v1/", ""],
            secret=KEY,
        )
        self.assertEqual(code, 0, out + err)
        text = self.env_file.read_text(encoding="utf-8")
        self.assertIn("REVIEW_PROVIDER=openai-compatible\n", text)
        self.assertIn("REVIEW_MODEL=vendor/model-x\n", text)
        self.assertIn("REVIEW_LLM_BASE_URL=https://gw.example/v1\n", text)


def _shell_test_skip_reason() -> str | None:
    if shutil.which("bash") is None:
        return "bash not available"
    # shipd.sh loads the repo .env and activates .venv; only run hermetically.
    for name in (".env", ".venv"):
        if (REPO_ROOT / name).exists():
            return f"repo has a local {name}; shell test needs a clean checkout"
    return None


@unittest.skipIf(_shell_test_skip_reason() is not None, _shell_test_skip_reason() or "")
class ShellIntegrationTests(unittest.TestCase):
    """Drive scripts/shipd.sh end to end with a sandboxed HOME and key store."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        shim = bin_dir / "python3"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
        self.env = {
            "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "HOME": str(self.tmp / "home"),
            "SHIPD_CREDENTIAL_STORE": "file",
            "SHIPD_CONFIG_DIR": str(self.tmp / "config"),
            "SHIPD_PLAIN": "1",
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_sh(self, *args: str, **extra_env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(REPO_ROOT / "run.sh"), *args, "--log-file", str(self.tmp / "run.log")],
            env={**self.env, **extra_env},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_check_shows_provider_and_masks_key(self) -> None:
        proc = self.run_sh("--check", "--provider", "openai", OPENAI_API_KEY=KEY)
        output = proc.stdout + proc.stderr
        self.assertIn("LLM provider: OpenAI (GPT) [openai]", output)
        self.assertIn("LLM API key: configured via env OPENAI_API_KEY", output)
        self.assertNotIn(KEY, output)
        self.assertNotIn(KEY, (self.tmp / "run.log").read_text(encoding="utf-8"))

    def test_review_run_without_key_fails_fast_when_not_a_tty(self) -> None:
        proc = self.run_sh("--once", "--no-submit", "--provider", "deepseek")
        output = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, output)
        self.assertIn("DEEPSEEK_API_KEY is not set", output)
        self.assertIn("Review LLM is not ready", output)

    def test_configure_requires_tty_and_forget_removes_key(self) -> None:
        proc = self.run_sh("--configure-llm")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("interactive terminal", proc.stderr)

        with patch.dict(os.environ, self.env, clear=True):
            credentials.store_key("moonshot", KEY)
        proc = self.run_sh("--forget-llm-key", "--provider", "kimi")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Removed the stored Moonshot (Kimi) key", proc.stdout)
        with patch.dict(os.environ, self.env, clear=True):
            self.assertIsNone(credentials.get_stored_key("moonshot"))


@unittest.skipIf(shutil.which("bash") is None or not hasattr(os, "fork"), "needs bash and a pty")
class ShellFirstRunTests(unittest.TestCase):
    """First-run LLM setup through ./run.sh on a pseudo-terminal.

    Runs a copy of the launcher and package so the repo's .env, logs and batch
    state are never touched. A failing `docker` shim stops preflight before any
    Shipd login and records the environment the run would have used.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        shutil.copy2(REPO_ROOT / "run.sh", self.repo / "run.sh")
        shutil.copytree(REPO_ROOT / "scripts", self.repo / "scripts")
        shutil.copytree(
            REPO_ROOT / "shipd-agent",
            self.repo / "shipd-agent",
            ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"),
        )
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.run_env_file = self.tmp / "run-env"
        for name, body in (
            ("python3", f'exec "{sys.executable}" "$@"'),
            ("docker", f'env > "{self.run_env_file}"; exit 1'),
        ):
            shim = bin_dir / name
            shim.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
            shim.chmod(0o755)
        self.env = {
            "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "HOME": str(self.tmp / "home"),
            "SHIPD_CREDENTIAL_STORE": "file",
            "SHIPD_CONFIG_DIR": str(self.tmp / "config"),
            "SHIPD_PLAIN": "1",
        }
        self.pid = 0
        self.fd = -1

    def tearDown(self) -> None:
        if self.pid:
            try:
                os.kill(self.pid, 9)
                os.waitpid(self.pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass
        if self.fd >= 0:
            os.close(self.fd)
        self._tmp.cleanup()

    def spawn(self, *args: str, **extra_env: str) -> None:
        import pty
        import warnings

        bash = shutil.which("bash")
        argv = [bash, str(self.repo / "run.sh"), *args]
        env = {**self.env, **extra_env}
        with warnings.catch_warnings():
            # Threads left by other tests trigger forkpty()'s multi-threaded
            # warning; the child does nothing but exec.
            warnings.simplefilter("ignore", DeprecationWarning)
            pid, fd = pty.fork()
        if pid == 0:  # child: the pty is its controlling terminal (/dev/tty)
            try:
                os.execve(bash, argv, env)
            finally:
                os._exit(127)
        self.pid, self.fd = pid, fd
        self.transcript = ""
        self.cursor = 0

    def _read(self, timeout: float) -> bool:
        import select

        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return True
        try:
            chunk = os.read(self.fd, 4096)
        except OSError:  # EIO on Linux once the child has exited
            chunk = b""
        self.transcript += chunk.decode("utf-8", errors="replace")
        return bool(chunk)

    def expect(self, text: str, timeout: float = 60) -> None:
        """Wait for ``text`` after the previous match."""
        import time

        deadline = time.monotonic() + timeout
        while (index := self.transcript.find(text, self.cursor)) < 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._read(remaining):
                self.fail(f"never saw {text!r}; transcript:\n{self.transcript}")
        self.cursor = index + len(text)

    def send(self, line: str) -> None:
        os.write(self.fd, f"{line}\n".encode())

    def finish(self) -> int:
        while self._read(60):
            pass
        _, status = os.waitpid(self.pid, 0)
        self.pid = 0
        return os.waitstatus_to_exitcode(status)

    def test_new_user_sets_up_llm_first_then_the_run_continues(self) -> None:
        env_file = self.repo / ".env"
        env_file.write_text(
            "AUTH_EMAIL=user@example.com\n"
            "REVIEW_PROVIDER=openai-compatible\n"
            "REVIEW_LLM_BASE_URL=https://old-gateway.example/v1\n",
            encoding="utf-8",
        )
        self.spawn("--no-submit")
        self.expect("First run: set up the review LLM")
        self.expect("Choose a provider (number or name) [7]:")
        self.send("openai")
        self.expect("Explore model [gpt-6.1-sol]:")
        self.send("")
        self.expect("Finalize model [gpt-6.1-sol]:")
        self.send("")
        self.expect("OPENAI_API_KEY: ")
        self.send(KEY)  # sent once the hidden prompt is up, so it is never echoed
        self.expect("starting the agent")
        self.expect("How many reviews would you like to do today?")
        self.send("1")
        # Preflight sees the new provider: .env was reloaded after setup.
        self.expect("LLM: OpenAI (GPT)")
        self.expect("Docker daemon is not running")
        self.assertEqual(self.finish(), 1)

        text = env_file.read_text(encoding="utf-8")
        self.assertIn("REVIEW_PROVIDER=openai\n", text)
        self.assertIn("AUTH_EMAIL=user@example.com", text)
        self.assertNotIn("REVIEW_LLM_BASE_URL", text)
        # The removed gateway URL must not linger in the run's environment,
        # or the OpenAI key would be sent there.
        run_env = self.run_env_file.read_text(encoding="utf-8")
        self.assertIn("REVIEW_PROVIDER=openai\n", run_env)
        self.assertNotIn("old-gateway", run_env)
        log = (self.repo / "logs" / "run.log").read_text(encoding="utf-8")
        for output in (self.transcript, log, text):
            self.assertNotIn(KEY, output)
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(credentials.get_stored_key("openai").value, KEY)

    def test_configured_user_goes_straight_to_the_run(self) -> None:
        self.spawn("--no-submit", ANTHROPIC_API_KEY=KEY)
        self.expect("How many reviews would you like to do today?")
        self.assertNotIn("First run", self.transcript)
        self.send("1")
        self.expect("LLM: Anthropic (Claude)")
        self.expect("Docker daemon is not running")
        self.assertEqual(self.finish(), 1)
        self.assertNotIn(KEY, self.transcript)
        self.assertFalse((self.repo / ".env").exists())


if __name__ == "__main__":
    unittest.main()
