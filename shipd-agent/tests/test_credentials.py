# Tests for persistent LLM API key storage (review/credentials.py).
# Uses a temp HOME / config dir and an in-memory keyring backend — the real
# OS keychain is never touched.

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import keyring
from keyring.backend import KeyringBackend
from keyring.backends import fail
from keyring.errors import PasswordDeleteError, PasswordSetError

from review import credentials
from review.llm_providers import PROVIDERS

KEY = "sk-test-0123456789abcdefSECRET"


class MemoryKeyring(KeyringBackend):
    """In-memory stand-in for the macOS Keychain."""

    priority = 10  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.items:
            raise PasswordDeleteError("not found")
        del self.items[(service, username)]


class BrokenKeyring(MemoryKeyring):
    def set_password(self, service: str, username: str, password: str) -> None:
        raise PasswordSetError("keychain locked")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class CredentialTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config_dir = self.tmp / "config" / "shipd-agent"
        self.env = {
            "HOME": str(self.tmp / "home"),
            "SHIPD_CONFIG_DIR": str(self.config_dir),
            "SHIPD_CREDENTIAL_STORE": "file",
        }
        self._env_patch = patch.dict(os.environ, self.env, clear=True)
        self._env_patch.start()
        self._stderr = patch("review.credentials._warn")
        self.warn = self._stderr.start()

    def tearDown(self) -> None:
        self._stderr.stop()
        self._env_patch.stop()
        self._tmp.cleanup()

    @property
    def cred_file(self) -> Path:
        return self.config_dir / "credentials"


class FileStoreTests(CredentialTestCase):
    def test_round_trip_and_permissions(self) -> None:
        location = credentials.store_key("openai", KEY)
        self.assertIn(str(self.cred_file), location)
        self.assertEqual(_mode(self.cred_file), 0o600)
        self.assertEqual(_mode(self.config_dir), 0o700)

        stored = credentials.get_stored_key("openai")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.value, KEY)
        self.assertTrue(stored.source.startswith("file "))
        self.assertNotIn(KEY, stored.source)

        self.assertIsNone(credentials.get_stored_key("deepseek"))
        self.assertEqual(credentials.delete_key("openai"), [f"file {self.cred_file}"])
        self.assertIsNone(credentials.get_stored_key("openai"))
        self.assertFalse(self.cred_file.exists())  # last key removed → file gone

    def test_multiple_providers_coexist(self) -> None:
        credentials.store_key("openai", KEY)
        credentials.store_key("xai", KEY + "x")
        credentials.delete_key("openai")
        self.assertEqual(credentials.get_stored_key("xai").value, KEY + "x")
        self.assertEqual(_mode(self.cred_file), 0o600)

    def test_loose_permissions_are_tightened(self) -> None:
        credentials.store_key("openai", KEY)
        os.chmod(self.cred_file, 0o644)
        self.assertEqual(credentials.get_stored_key("openai").value, KEY)
        self.assertEqual(_mode(self.cred_file), 0o600)
        self.warn.assert_called()

    def test_default_location_is_outside_repo_under_home(self) -> None:
        with patch.dict(os.environ, {"SHIPD_CONFIG_DIR": ""}):
            os.environ.pop("XDG_CONFIG_HOME", None)
            with patch("pathlib.Path.home", return_value=self.tmp / "home"):
                path = credentials.credentials_path()
        self.assertEqual(path, self.tmp / "home" / ".config" / "shipd-agent" / "credentials")
        with patch.dict(os.environ, {"SHIPD_CONFIG_DIR": "", "XDG_CONFIG_HOME": str(self.tmp / "xdg")}):
            self.assertEqual(
                credentials.credentials_path(), self.tmp / "xdg" / "shipd-agent" / "credentials"
            )

    def test_rejects_empty_or_whitespace_keys(self) -> None:
        with self.assertRaises(credentials.CredentialStoreError):
            credentials.store_key("openai", "   ")
        with self.assertRaises(credentials.CredentialStoreError):
            credentials.store_key("openai", "sk-abc def")
        self.assertFalse(self.cred_file.exists())

    def test_missing_keyring_package_warns_and_uses_file(self) -> None:
        os.environ["SHIPD_CREDENTIAL_STORE"] = "auto"
        with (
            patch.dict(sys.modules, {"keyring": None}),
            patch.object(credentials, "_warned_missing_keyring", False),
        ):
            self.assertIn("file", credentials.store_key("openai", KEY))
            self.warn.assert_called_once()
            self.assertIn("--setup", self.warn.call_args.args[0])
            os.environ["SHIPD_CREDENTIAL_STORE"] = "keyring"
            with self.assertRaises(credentials.CredentialStoreError):
                credentials.store_key("openai", KEY)
        self.assertEqual(_mode(self.cred_file), 0o600)

    def test_invalid_store_mode(self) -> None:
        with patch.dict(os.environ, {"SHIPD_CREDENTIAL_STORE": "cloud"}):
            with self.assertRaises(credentials.CredentialStoreError):
                credentials.store_key("openai", KEY)


class KeyringStoreTests(CredentialTestCase):
    def setUp(self) -> None:
        super().setUp()
        os.environ["SHIPD_CREDENTIAL_STORE"] = "auto"
        self.backend = MemoryKeyring()
        self._kr = patch.object(keyring, "get_keyring", return_value=self.backend)
        self._kr.start()

    def tearDown(self) -> None:
        self._kr.stop()
        super().tearDown()

    def test_round_trip_uses_keyring_not_file(self) -> None:
        location = credentials.store_key("moonshot", KEY)
        self.assertIn("keyring", location)
        self.assertEqual(self.backend.items[(credentials.SERVICE_NAME, "moonshot")], KEY)
        self.assertFalse(self.cred_file.exists())

        stored = credentials.get_stored_key("moonshot")
        self.assertEqual(stored.value, KEY)
        self.assertIn("keyring", stored.source)

        removed = credentials.delete_key("moonshot")
        self.assertEqual(len(removed), 1)
        self.assertEqual(self.backend.items, {})
        self.assertIsNone(credentials.get_stored_key("moonshot"))

    def test_storing_in_keyring_drops_old_plaintext_copy(self) -> None:
        with patch.dict(os.environ, {"SHIPD_CREDENTIAL_STORE": "file"}):
            credentials.store_key("openai", "sk-old-0123456789abcdef")
        self.assertTrue(self.cred_file.exists())
        credentials.store_key("openai", KEY)
        self.assertFalse(self.cred_file.exists())
        self.assertEqual(credentials.get_stored_key("openai").value, KEY)

    def test_describe_store_names_keyring(self) -> None:
        self.assertIn("keyring", credentials.describe_store())

    def test_fail_backend_falls_back_to_file(self) -> None:
        with patch.object(keyring, "get_keyring", return_value=fail.Keyring()):
            location = credentials.store_key("openai", KEY)
            self.assertIn("file", location)
            self.assertEqual(credentials.get_stored_key("openai").value, KEY)
        self.assertEqual(_mode(self.cred_file), 0o600)

    def test_keyring_mode_requires_a_real_backend(self) -> None:
        os.environ["SHIPD_CREDENTIAL_STORE"] = "keyring"
        with patch.object(keyring, "get_keyring", return_value=fail.Keyring()):
            with self.assertRaises(credentials.CredentialStoreError):
                credentials.store_key("openai", KEY)
        self.assertFalse(self.cred_file.exists())

    def test_keyring_write_error_falls_back_to_file_in_auto_mode(self) -> None:
        with patch.object(keyring, "get_keyring", return_value=BrokenKeyring()):
            location = credentials.store_key("openai", KEY)
        self.assertIn("file", location)
        self.assertEqual(_mode(self.cred_file), 0o600)
        self.warn.assert_called()

    def test_macos_backend_label(self) -> None:
        mac = type("Keyring", (), {"__module__": "keyring.backends.macOS", "name": "macOS"})()
        self.assertEqual(credentials.keyring_label(mac), "macOS Keychain")


class ResolutionTests(CredentialTestCase):
    def test_env_beats_stored(self) -> None:
        spec = PROVIDERS["deepseek"]
        credentials.store_key("deepseek", "sk-stored-0123456789abcdef")
        self.assertEqual(credentials.resolve_api_key(spec).value, "sk-stored-0123456789abcdef")
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": KEY}):
            resolved = credentials.resolve_api_key(spec)
        self.assertEqual(resolved.value, KEY)
        self.assertEqual(resolved.source, "env DEEPSEEK_API_KEY")

    def test_whitespace_env_value_is_ignored(self) -> None:
        with patch.dict(os.environ, {"XAI_API_KEY": "   "}):
            self.assertIsNone(credentials.resolve_api_key(PROVIDERS["xai"]))

    def test_broken_store_does_not_raise_during_resolution(self) -> None:
        with patch.dict(os.environ, {"SHIPD_CREDENTIAL_STORE": "bogus"}):
            self.assertIsNone(credentials.resolve_api_key(PROVIDERS["openai"]))

    def test_mask_secret_hides_key(self) -> None:
        masked = credentials.mask_secret(KEY)
        self.assertNotIn(KEY, masked)
        self.assertNotIn(KEY[:-4], masked)
        self.assertTrue(masked.startswith("****"))
        self.assertEqual(credentials.mask_secret("short"), "****")
        self.assertEqual(credentials.mask_secret(""), "****")


if __name__ == "__main__":
    unittest.main()
