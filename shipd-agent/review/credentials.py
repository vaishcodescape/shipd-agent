# Persistent, per-user storage for LLM API keys.
#
# Keys go to the OS keychain via `keyring` (macOS Keychain, Windows Credential
# Locker, Secret Service on Linux). When no secure keyring backend is
# available, they fall back to a JSON file outside the repo:
#   ${SHIPD_CONFIG_DIR:-${XDG_CONFIG_HOME:-~/.config}/shipd-agent}/credentials
# with the directory at 0700 and the file at 0600.
#
# Resolution order for a provider's key: its env var(s) (including values the
# repo .env exported) > stored key. Keys are never written to the repo .env,
# never exported to the environment, and never printed (see mask_secret).
#
# SHIPD_CREDENTIAL_STORE=auto|keyring|file picks the backend (default auto).

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from review.llm_providers import ProviderSpec

SERVICE_NAME = "shipd-agent"
CREDENTIALS_FILENAME = "credentials"
_FILE_VERSION = 1
# keyring backends below this priority are plaintext/encrypted-file stores
# (e.g. keyrings.alt) or the fail/null backends — not an OS keychain.
_MIN_SECURE_KEYRING_PRIORITY = 1
_warned_missing_keyring = False


class CredentialStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedKey:
    value: str
    source: str  # human-readable origin, safe to print (never the key itself)


def _warn(message: str) -> None:
    print(f"WARNING: {message}", file=sys.stderr, flush=True)


def config_dir() -> Path:
    override = os.getenv("SHIPD_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    xdg = os.getenv("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "shipd-agent"


def credentials_path() -> Path:
    return config_dir() / CREDENTIALS_FILENAME


def _store_mode() -> str:
    mode = os.getenv("SHIPD_CREDENTIAL_STORE", "auto").strip().lower() or "auto"
    if mode not in {"auto", "keyring", "file"}:
        raise CredentialStoreError(
            f"SHIPD_CREDENTIAL_STORE must be auto, keyring or file (got {mode!r})."
        )
    return mode


def _keyring_backend() -> Any | None:
    """Return a secure keyring backend, or None to use the file fallback."""
    mode = _store_mode()
    if mode == "file":
        return None
    try:
        import keyring
    except ImportError:
        if mode == "keyring":
            raise CredentialStoreError(
                "SHIPD_CREDENTIAL_STORE=keyring but the 'keyring' package is not "
                "installed (run ./run.sh --setup)."
            ) from None
        global _warned_missing_keyring
        if not _warned_missing_keyring:
            _warned_missing_keyring = True
            _warn(
                "The 'keyring' package is not installed (run ./run.sh --setup), so "
                f"API keys use the per-user file {credentials_path()} instead of the "
                "OS keychain."
            )
        return None
    try:
        backend = keyring.get_keyring()
        priority = float(getattr(backend, "priority", 0))
    except Exception:
        backend, priority = None, 0.0
    if backend is not None and priority >= _MIN_SECURE_KEYRING_PRIORITY:
        return backend
    if mode == "keyring":
        raise CredentialStoreError(
            "SHIPD_CREDENTIAL_STORE=keyring but no OS keychain backend is available."
        )
    return None


def keyring_label(backend: Any) -> str:
    name = getattr(backend, "name", "") or type(backend).__name__
    if "macos" in type(backend).__module__.lower() or "macOS" in str(name):
        return "macOS Keychain"
    return f"system keyring ({name})"


def describe_store() -> str:
    """Where store_key() would save a key right now (safe to print)."""
    try:
        backend = _keyring_backend()
    except CredentialStoreError as exc:
        return f"unavailable: {exc}"
    if backend is not None:
        return keyring_label(backend)
    return f"file {credentials_path()} (0600)"


# --- file fallback ---------------------------------------------------------


def _read_file_store() -> dict[str, str]:
    path = credentials_path()
    try:
        st = path.stat()
    except FileNotFoundError:
        return {}
    if stat.S_IMODE(st.st_mode) & 0o077:
        _warn(f"{path} was readable by other users; tightening permissions to 0600.")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError) as exc:
        raise CredentialStoreError(f"Could not read {path}: {exc}") from None
    keys = data.get("keys", {}) if isinstance(data, dict) else {}
    return {str(k): str(v) for k, v in keys.items() if isinstance(v, str) and v}


def _write_file_store(keys: dict[str, str]) -> None:
    path = credentials_path()
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    if not keys:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    payload = json.dumps({"version": _FILE_VERSION, "keys": keys}, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".credentials.", dir=directory)
    try:
        os.chmod(tmp_name, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


# --- public API --------------------------------------------------------------


def get_stored_key(provider: str) -> ResolvedKey | None:
    """Return the stored key for ``provider`` (keychain first, then file)."""
    backend = _keyring_backend()
    if backend is not None:
        try:
            value = backend.get_password(SERVICE_NAME, provider)
        except Exception as exc:
            _warn(f"Could not read the {keyring_label(backend)}: {exc}")
            value = None
        if value:
            return ResolvedKey(value=value, source=keyring_label(backend))
    value = _read_file_store().get(provider)
    if value:
        return ResolvedKey(value=value, source=f"file {credentials_path()}")
    return None


def store_key(provider: str, value: str) -> str:
    """Persist ``value`` for ``provider``; return where it was stored."""
    value = value.strip()
    if not value:
        raise CredentialStoreError("Refusing to store an empty API key.")
    if any(ch.isspace() for ch in value):
        raise CredentialStoreError("API keys cannot contain whitespace.")
    backend = _keyring_backend()
    if backend is not None:
        try:
            backend.set_password(SERVICE_NAME, provider, value)
        except Exception as exc:
            if _store_mode() == "keyring":
                raise CredentialStoreError(
                    f"Could not save to the {keyring_label(backend)}: {exc}"
                ) from None
            _warn(
                f"Could not save to the {keyring_label(backend)} ({exc}); "
                "using the per-user credentials file instead."
            )
        else:
            # Drop any older plaintext copy so the keychain is the only one.
            keys = _read_file_store()
            if keys.pop(provider, None) is not None:
                _write_file_store(keys)
            return keyring_label(backend)
    keys = _read_file_store()
    keys[provider] = value
    _write_file_store(keys)
    return f"file {credentials_path()} (0600)"


def delete_key(provider: str) -> list[str]:
    """Remove the stored key for ``provider``; return the locations cleared."""
    removed: list[str] = []
    backend = _keyring_backend()
    if backend is not None:
        try:
            if backend.get_password(SERVICE_NAME, provider):
                backend.delete_password(SERVICE_NAME, provider)
                removed.append(keyring_label(backend))
        except Exception as exc:
            _warn(f"Could not remove the key from the {keyring_label(backend)}: {exc}")
    keys = _read_file_store()
    if keys.pop(provider, None) is not None:
        _write_file_store(keys)
        removed.append(f"file {credentials_path()}")
    return removed


def env_api_key(spec: ProviderSpec) -> ResolvedKey | None:
    for env_name in spec.api_key_env:
        value = os.getenv(env_name, "").strip()
        if value:
            return ResolvedKey(value=value, source=f"env {env_name}")
    return None


def resolve_api_key(spec: ProviderSpec) -> ResolvedKey | None:
    """Env var(s) first, then the stored key. Never raises for a bad store."""
    from_env = env_api_key(spec)
    if from_env is not None:
        return from_env
    try:
        return get_stored_key(spec.name)
    except CredentialStoreError as exc:
        _warn(str(exc))
        return None


def mask_secret(value: str) -> str:
    """Printable stand-in for a secret: only the last 4 chars of a long key."""
    value = value or ""
    if len(value) < 16:
        return "****"
    return f"****{value[-4:]} ({len(value)} chars)"
