# CLI for choosing the review LLM and managing its stored API key.
#
# Used by scripts/shipd.sh:
#   configure   ./run.sh --configure-llm   interactive provider/model/key setup
#   ensure-key  preflight before a review run (prompts only on a TTY)
#   status      ./run.sh --check           masked, secret-free summary
#   forget      ./run.sh --forget-llm-key  remove a stored key
#
# Keys are read with getpass (hidden), stored via review.credentials (OS
# keychain, else a 0600 per-user file), and never printed, logged, written to
# the repo .env, or accepted as command-line arguments.

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

from auth import REPO_ROOT
from review import credentials
from review.config import ReviewConfig, get_review_config, resolve_review_models
from review.llm import llm_config_error, missing_api_key_message
from review.llm_providers import (
    PROVIDERS,
    ProviderSpec,
    get_provider,
    normalize_provider_name,
)

_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")
_URL_RE = re.compile(r"^https?://\S+$")


def _err(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        answer = ""
    return answer or default


def _confirm(prompt: str, *, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = _ask(f"{prompt} ({hint})").lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def _read_secret(prompt: str) -> str:
    try:
        return getpass.getpass(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        _err("")
        return ""


def _choose_provider(current: str) -> ProviderSpec:
    specs = list(PROVIDERS.values())
    print("\nLLM provider:")
    for index, spec in enumerate(specs, start=1):
        marker = "  (current)" if spec.name == current else ""
        print(f"  {index}) {spec.label} [{spec.name}]{marker}")
    default_index = next(
        (str(i) for i, spec in enumerate(specs, start=1) if spec.name == current), "1"
    )
    while True:
        answer = _ask("Choose a provider (number or name)", default_index)
        if answer.isdigit() and 1 <= int(answer) <= len(specs):
            return specs[int(answer) - 1]
        try:
            return get_provider(answer)
        except ValueError as exc:
            print(f"  {exc}")


def _choose_model(spec: ProviderSpec, role: str, default: str) -> str:
    options = list(spec.suggested_models)
    if default and default not in options:
        options.insert(0, default)
    print(f"\n{role.capitalize()} model for {spec.label}:")
    for index, model in enumerate(options, start=1):
        marker = "  (default)" if model == default else ""
        print(f"  {index}) {model}{marker}")
    print("  …or type any other model ID for this provider.")
    while True:
        answer = _ask(f"{role.capitalize()} model", default)
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        if _MODEL_ID_RE.match(answer):
            return answer
        print("  Enter a list number or a model ID (letters, digits, . _ : / @ + -).")


def _choose_base_url(spec: ProviderSpec, current: str) -> str:
    if not spec.requires_base_url:
        return ""
    print(f"\n{spec.notes}")
    while True:
        answer = _ask("Base URL (OpenAI-compatible, e.g. https://host/v1)", current)
        if _URL_RE.match(answer):
            return answer.rstrip("/")
        print("  Enter an http(s) URL.")


def _prompt_and_store_key(spec: ProviderSpec) -> bool:
    print(
        f"Paste your {spec.label} API key. Input is hidden; it will be saved to "
        f"{credentials.describe_store()} (not to .env)."
    )
    for _ in range(3):
        value = _read_secret(f"{spec.primary_key_env}: ")
        if not value:
            print("  No key entered.")
            return False
        try:
            location = credentials.store_key(spec.name, value)
        except credentials.CredentialStoreError as exc:
            print(f"  {exc}")
            continue
        print(f"Saved the {spec.label} API key to {location}.")
        return True
    return False


def _write_env_settings(env_file: Path, values: dict[str, str], remove: list[str]) -> None:
    from dotenv import dotenv_values, set_key, unset_key

    existing = dotenv_values(env_file) if env_file.exists() else {}
    for key, value in values.items():
        set_key(str(env_file), key, value, quote_mode="never")
    for key in remove:
        if key in existing:
            unset_key(str(env_file), key)


def cmd_configure(args: argparse.Namespace) -> int:
    if not _interactive():
        _err(
            "ERROR: --configure-llm needs an interactive terminal. In scripts, set "
            "REVIEW_PROVIDER / REVIEW_EXPLORE_MODEL / REVIEW_MODEL and the "
            "provider's API key env var instead."
        )
        return 2

    env_file = Path(args.env_file).expanduser()
    load_dotenv(env_file)  # no-op for keys the caller already exported
    try:
        current = normalize_provider_name(os.getenv("REVIEW_PROVIDER", ""))
    except ValueError as exc:
        print(f"WARNING: {exc} Starting from the default provider.")
        current = normalize_provider_name("")

    print("Configure the review LLM (provider, models, API key).")
    print(f"API keys are stored in: {credentials.describe_store()}")

    spec = _choose_provider(current)
    if spec.name == current:
        default_explore, default_finalize = resolve_review_models(spec)
    else:
        default_explore = spec.default_explore_model
        default_finalize = spec.default_finalize_model
    explore = _choose_model(spec, "explore", default_explore)
    finalize = _choose_model(spec, "finalize", default_finalize or explore)
    base_url = _choose_base_url(spec, os.getenv("REVIEW_LLM_BASE_URL", "").strip())

    print("")
    from_env = credentials.env_api_key(spec)
    if from_env is not None:
        print(
            f"Using {from_env.source.removeprefix('env ')} from the environment/.env "
            "(it takes precedence over stored keys); nothing to store."
        )
    else:
        stored = None
        try:
            stored = credentials.get_stored_key(spec.name)
        except credentials.CredentialStoreError as exc:
            print(f"WARNING: {exc}")
        if stored is not None:
            print(
                f"A {spec.label} key is already stored in {stored.source} "
                f"({credentials.mask_secret(stored.value)})."
            )
            if _confirm("Replace it?", default=False):
                _prompt_and_store_key(spec)
        elif not _prompt_and_store_key(spec):
            print(
                f"WARNING: no {spec.label} key stored; reviews will fail until you "
                f"set {spec.primary_key_env} or rerun ./run.sh --configure-llm."
            )

    settings = {
        "REVIEW_PROVIDER": spec.name,
        "REVIEW_EXPLORE_MODEL": explore,
        "REVIEW_MODEL": finalize,
    }
    remove: list[str] = []
    if spec.requires_base_url:
        settings["REVIEW_LLM_BASE_URL"] = base_url
    else:
        # Never send this provider's key to a base URL chosen for another one.
        remove.append("REVIEW_LLM_BASE_URL")
    print("")
    for key, value in settings.items():
        print(f"  {key}={value}")
    if _confirm(f"Save these settings to {env_file}?", default=True):
        _write_env_settings(env_file, settings, remove)
        print(f"Saved to {env_file} (no API key is written there).")
    else:
        print("Not saved. Pass --provider/--model/--explore-model or set the env vars.")
    print("Run ./run.sh --check to verify.")
    return 0


def _summary_lines(config: ReviewConfig) -> list[str]:
    spec = get_provider(config.llm_provider)
    lines = [
        f"LLM provider: {spec.label} [{spec.name}]",
        f"LLM explore model: {config.review_explore_model or '(not set)'}",
        f"LLM finalize model: {config.review_model or '(not set)'}",
    ]
    base_url = config.llm_base_url or spec.base_url
    if base_url:
        lines.append(f"LLM base URL: {base_url}")
    if config.llm_api_key:
        lines.append(
            f"LLM API key: configured via {config.llm_api_key_source} "
            f"({credentials.mask_secret(config.llm_api_key)})"
        )
    else:
        lines.append(f"LLM API key: not configured ({spec.primary_key_env})")
    lines.append(f"LLM key store: {credentials.describe_store()}")
    return lines


def _load_config() -> ReviewConfig | None:
    try:
        return get_review_config()
    except ValueError as exc:
        _err(f"ERROR: {exc}")
        return None


def cmd_status(args: argparse.Namespace) -> int:
    config = _load_config()
    if config is None:
        return 2
    for line in _summary_lines(config):
        print(line)
    problem = llm_config_error(config)
    if problem:
        print(f"WARNING: {problem}")
        return 1
    return 0


def cmd_ensure_key(args: argparse.Namespace) -> int:
    config = _load_config()
    if config is None:
        return 2
    spec = get_provider(config.llm_provider)
    if missing_api_key_message(config) and _interactive():
        _err(f"No API key found for {spec.label} (needed for the review agent).")
        # Guidance goes to stderr and the hidden prompt to the TTY, so a caller
        # capturing stdout still shows the interaction.
        _err(
            f"Paste it below to store it in {credentials.describe_store()} for "
            "future runs (input is hidden; leave empty to abort)."
        )
        value = _read_secret(f"{spec.primary_key_env}: ")
        if value:
            try:
                location = credentials.store_key(spec.name, value)
            except credentials.CredentialStoreError as exc:
                _err(f"ERROR: {exc}")
                return 1
            _err(f"Saved the {spec.label} API key to {location}.")
            config = _load_config()
            if config is None:
                return 2
    problem = llm_config_error(config)
    if problem:
        if missing_api_key_message(config) and not _interactive():
            problem += " (No terminal to prompt for it.)"
        print(f"ERROR: {problem}")
        return 1
    print(
        f"LLM: {spec.label} — explore={config.review_explore_model}, "
        f"finalize={config.review_model}, key via {config.llm_api_key_source}"
    )
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    load_dotenv(REPO_ROOT / ".env")
    if args.all:
        names = list(PROVIDERS)
    else:
        try:
            names = [normalize_provider_name(os.getenv("REVIEW_PROVIDER", ""))]
        except ValueError as exc:
            _err(f"ERROR: {exc}")
            return 2
    status = 0
    for name in names:
        spec = PROVIDERS[name]
        try:
            removed = credentials.delete_key(name)
        except credentials.CredentialStoreError as exc:
            _err(f"ERROR: {exc}")
            status = 1
            continue
        if removed:
            print(f"Removed the stored {spec.label} key from {', '.join(removed)}.")
        elif not args.all:
            print(f"No stored {spec.label} key to remove.")
        still = credentials.env_api_key(spec)
        if still is not None:
            print(
                f"Note: {still.source.removeprefix('env ')} is still set in the "
                "environment/.env and will keep being used."
            )
    return status


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shipd-llm",
        description="Choose the review LLM provider/models and manage stored API keys.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    configure = sub.add_parser("configure", help="Interactive provider/model/key setup.")
    configure.add_argument(
        "--env-file",
        default=str(REPO_ROOT / ".env"),
        help="Where to save REVIEW_PROVIDER / REVIEW_*MODEL (default: repo .env).",
    )
    configure.set_defaults(func=cmd_configure)
    sub.add_parser("status", help="Show the LLM setup (keys masked).").set_defaults(
        func=cmd_status
    )
    sub.add_parser(
        "ensure-key", help="Exit 0 when the LLM is usable; prompt for a key on a TTY."
    ).set_defaults(func=cmd_ensure_key)
    forget = sub.add_parser("forget", help="Remove the stored key for REVIEW_PROVIDER.")
    forget.add_argument("--all", action="store_true", help="Remove keys for every provider.")
    forget.set_defaults(func=cmd_forget)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        _err("\nCancelled.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
