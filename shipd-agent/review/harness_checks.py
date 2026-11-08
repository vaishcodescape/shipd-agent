# Deterministic Dockerfile, test.sh, and test.patch checks.
#
# These sit beside the Phase 2 and Phase 3 review. They do not replace it.

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from review.schemas import Finding

_PINNED_FROM_RE = re.compile(
    r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?\s*$",
    re.I | re.M,
)
_ACCEPTED_BASE_NAMES = (
    "ubuntu",
    "debian",
    "python",
    "node",
    "golang",
    "rust",
    "eclipse-temurin",
    "ruby",
)
_TEST_DURING_BUILD_RE = re.compile(
    r"\b(?:pytest|go\s+test|cargo\s+test|mvn\s+test|npm\s+test|pnpm\s+test|\./test\.sh)\b",
    re.I,
)
_UNPINNED_PIP_RE = re.compile(
    r"\bpip(?:3)?\s+install\b(?P<args>[^\n;&|]*)",
    re.I | re.M,
)
_WORKDIR_APP_RE = re.compile(r"^\s*WORKDIR\s+/?app\s*$", re.I | re.M)
_USER_MODEL_RE = re.compile(r"^\s*USER\s+(?:model(?::1000)?|1000)\s*$", re.I | re.M)
_UID_1000_RE = re.compile(r"\b(?:useradd|adduser)\b[^\n]*\b1000\b|\b1000:1000\b", re.I)
_SHELL_ENTRY_RE = re.compile(
    r"^\s*(?:ENTRYPOINT|CMD)\b[^\n]*(?:bash|/bin/sh)\b",
    re.I | re.M,
)
_INSTALL_RE = re.compile(
    r"\b(?:pip(?:3)?\s+install|apt-get\s+install|npm\s+install|yarn\s+add|cargo\s+install)\b",
    re.I,
)
_BANNED_RE = re.compile(r"\b(?:shipd|datacurve)\b", re.I)
_FAIL_FAST_RE = re.compile(r"(?:^|\s)(?:-x|--exitfirst|--fail-fast|-failfast)(?:\s|$)", re.I)
_HIDDEN_TB_RE = re.compile(r"--tb=(?:no|line)\b|2>/dev/null", re.I)
_LONG_ASSERT_RE = re.compile(r"assert\b.{0,40}==\s+(['\"][^'\"]{120,}|\"\"\"|''')", re.I)
_PREDICTABLE_TEST_RE = re.compile(
    r"(?:^|/)(?:"
    r"test_[a-z0-9_]+\.py|"
    r"[a-z0-9_]+_test\.py|"
    r"[a-z0-9_]+_test\.go|"
    r"[a-z0-9_]+\.test\.[jt]sx?|"
    r"[A-Z][A-Za-z0-9]*Test\.java"
    r")$"
)
_TESTY_NAME_RE = re.compile(r"test|spec", re.I)
_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.M)
_HOW_TESTS_RUN_RE = re.compile(
    r"\b(?:pytest|unittest|test\.sh|how tests run|tests are run|run the tests)\b",
    re.I,
)

def _logical_lines(text: str) -> str:
    """Join Docker/shell continuations, ignoring whole-line comments."""
    text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    return re.sub(r"\\\s*\n\s*", " ", text)


def _pip_installs(text: str):
    """Yield package/local requirements separately from pip option values."""
    value_options = {
        "-r", "--requirement", "-c", "--constraint", "-i", "--index-url",
        "--extra-index-url", "-f", "--find-links", "--trusted-host", "--target",
        "-t", "--prefix", "--root", "--cache-dir", "--timeout", "--retries",
        "--only-binary", "--no-binary", "--config-settings", "--platform",
        "--python-version", "--implementation", "--abi",
    }
    for run in re.finditer(r"^\s*RUN\b(.*)$", _logical_lines(text), re.I | re.M):
        for match in _UNPINNED_PIP_RE.finditer(run.group(1)):
            try:
                tokens = iter(shlex.split(match.group("args"), comments=True))
            except ValueError:
                continue  # Leave unsupported shell syntax to the semantic review.
            requirements: list[str] = []
            editable: list[str] = []
            for token in tokens:
                if token in value_options:
                    next(tokens, None)
                elif token in {"-e", "--editable"}:
                    editable.append(next(tokens, ""))
                elif token.startswith("--editable="):
                    editable.append(token.split("=", 1)[1])
                elif not token.startswith("-"):
                    requirements.append(token)
            yield requirements, editable


def _finding(phase: str, severity: str, title: str, evidence: str, fix: str) -> Finding:
    return Finding(
        phase=phase,
        severity=severity,  # type: ignore[arg-type]
        finding=title,
        evidence=evidence,
        suggested_fix=fix,
    )


def evaluate_dockerfile(text: str) -> list[Finding]:
    """Graded Dockerfile rules. Empty text yields nothing (the file is missing)."""
    if not (text or "").strip():
        return []
    text = _logical_lines(text)
    findings: list[Finding] = []
    images = list(_PINNED_FROM_RE.finditer(text))
    if not images:
        findings.append(
            _finding(
                "2",
                "MAJOR",
                "Dockerfile has no FROM image",
                "No FROM instruction.",
                "Start from a pinned accepted base image.",
            )
        )
    stages: set[str] = set()
    for match in images:
        image = match.group(1)
        inherited = image.lower() in stages
        if match.group(2):
            stages.add(match.group(2).lower())
        if inherited:
            continue
        base, _, digest = image.partition("@")
        name, _, tag = base.rsplit("/", 1)[-1].partition(":")
        pinned = bool(re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest)) or bool(tag and tag != "latest" and "$" not in tag)
        if name not in _ACCEPTED_BASE_NAMES or not pinned:
            findings.append(
                _finding(
                    "2",
                    "MAJOR",
                    "Dockerfile base image is not a pinned accepted image",
                    f"FROM {image}",
                    "Use a pinned tag of ubuntu, debian, python, node, golang, rust, "
                    "eclipse-temurin, or ruby. Do not use latest.",
                )
            )
        if name == "python" and re.search(r"apt-get\s+install[^\n]*\b(?:python3|pip)\b", text, re.I):
            findings.append(
                _finding(
                    "2",
                    "MAJOR",
                    "Dockerfile reinstalls a tool the base image already provides",
                    "python base image also apt-installs python3 or pip.",
                    "Do not reinstall interpreters or package managers that the base image includes.",
                )
            )
    if not _WORKDIR_APP_RE.search(text):
        findings.append(
            _finding(
                "2",
                "MAJOR",
                "Dockerfile workdir is not /app",
                "WORKDIR /app is required.",
                "Set WORKDIR /app.",
            )
        )
    for run in re.finditer(r"^\s*RUN\b(.*)$", text, re.I | re.M):
        if _TEST_DURING_BUILD_RE.search(run.group(1)):
            findings.append(
                _finding(
                    "2",
                    "MAJOR",
                    "Dockerfile runs tests during the build",
                    run.group(0).strip()[:240],
                    "Install dependencies at build time. Run tests only from test.sh.",
                )
            )
            break
    for packages, _editable in _pip_installs(text):
        unpinned = [pkg for pkg in packages
                    if not pkg.startswith((".", "/", "git+", "http://", "https://"))
                    and "@" not in pkg and not re.search(r"===?[^*<>=!~]+$", pkg)]
        if unpinned:
            findings.append(
                _finding(
                    "2",
                    "MAJOR",
                    "Dockerfile installs an unpinned package",
                    "Unpinned: " + ", ".join(unpinned),
                    "Pin pip packages with == or an exact @ version.",
                )
            )
            break
    if not (_USER_MODEL_RE.search(text) and _UID_1000_RE.search(text)):
        findings.append(
            _finding(
                "2",
                "MAJOR",
                "Dockerfile does not run as model:1000",
                "Expected a uid 1000 user named model and USER model.",
                "Create the model user with uid 1000 and set USER model.",
            )
        )
    if not _SHELL_ENTRY_RE.search(text):
        findings.append(
            _finding(
                "2",
                "MAJOR",
                "Dockerfile entrypoint is not an interactive shell",
                "ENTRYPOINT or CMD should launch bash or sh, not the tests.",
                "Use an interactive shell entrypoint such as bash.",
            )
        )
    return findings


def evaluate_python_install(dockerfile: str, prompt: str) -> list[Finding]:
    if not any(package in {".", "/app", "/app/"}
               for packages, _editable in _pip_installs(dockerfile)
               for package in packages):
        return []
    if _HOW_TESTS_RUN_RE.search(prompt or ""):
        return []
    return [
        _finding(
            "2",
            "MAJOR",
            "Python install is not editable and the prompt does not say how tests run",
            "pip install of . or /app is missing -e.",
            "Install the project with pip install -e, or state in the prompt how tests run.",
        )
    ]


def evaluate_test_harness(test_sh: str, *, executable: bool, test_patch: str, solution_patch: str) -> list[Finding]:
    findings: list[Finding] = []
    script = test_sh or ""
    if script and not re.search(r"\bbase\b", script):
        findings.append(
            _finding(
                "2",
                "MAJOR",
                "test.sh has no base mode",
                "base and new must both be selectable.",
                "Dispatch ./test.sh base to the existing tests and ./test.sh new to the added tests.",
            )
        )
    if script and not re.search(r"\bnew\b", script):
        findings.append(
            _finding(
                "2",
                "MAJOR",
                "test.sh has no new mode",
                "base and new must both be selectable.",
                "Add a new mode that runs only the tests from test.patch.",
            )
        )
    if script and not executable:
        findings.append(
            _finding(
                "2",
                "MAJOR",
                "test.sh is not executable",
                "The executable bit is off.",
                "chmod +x test.sh.",
            )
        )
    if script and _INSTALL_RE.search(script):
        findings.append(
            _finding(
                "2",
                "MAJOR",
                "test.sh installs packages",
                "Package installs belong in the image build, not test.sh.",
                "Remove pip, apt, npm, yarn, and cargo installs from test.sh.",
            )
        )
    # Removed lines describe the old tests, not the submitted harness. Retain
    # context and additions so fixing a bad setting does not recreate it.
    patch_content = "\n".join(
        line[1:] if line.startswith(("+", " ")) else line
        for line in (test_patch or "").splitlines()
        if not line.startswith(("-", "+++", "diff --git", "@@"))
    )
    blob = script + "\n" + patch_content
    banned = _BANNED_RE.search(blob)
    if banned:
        findings.append(
            _finding(
                "3",
                "MAJOR",
                "Test patch contains a banned marker",
                f"Found {banned.group(0)!r}.",
                "Remove shipd and datacurve markers from the tests and test.sh.",
            )
        )
    fail_fast = any(
        _FAIL_FAST_RE.search(line)
        and (re.search(r"--exitfirst|--fail-fast|-failfast", line)
             or re.search(r"\bpytest\b", line))
        for line in blob.splitlines() if not line.lstrip().startswith("#")
    )
    if fail_fast:
        findings.append(
            _finding(
                "3",
                "MAJOR",
                "Tests use a fail-fast flag",
                "Fail-fast hides later failures.",
                "Run the whole suite. Drop -x, --exitfirst, and --fail-fast.",
            )
        )
    if _HIDDEN_TB_RE.search(blob):
        findings.append(
            _finding(
                "3",
                "MAJOR",
                "Tests hide failure diagnostics",
                "A traceback is suppressed (--tb=no, --tb=line, or 2>/dev/null).",
                "Show the assertion failure. Do not hide diagnostics.",
            )
        )
    if _LONG_ASSERT_RE.search(patch_content):
        findings.append(
            _finding(
                "3",
                "MAJOR",
                "Tests pin an oversized expected output",
                "An assertion compares against a very long literal.",
                "Assert the behaviour, not a full snapshot of the output.",
            )
        )
    solution_paths = set(_DIFF_FILE_RE.findall(solution_patch or ""))
    for path in _DIFF_FILE_RE.findall(test_patch or ""):
        if path in {"test.sh", "Dockerfile"}:
            continue
        if _TESTY_NAME_RE.search(path):
            if not _PREDICTABLE_TEST_RE.search(path):
                findings.append(
                    _finding(
                        "3",
                        "MAJOR",
                        "Test file name is not predictable",
                        path,
                        "Name tests test_*.py, *_test.py, *_test.go, *.test.js, or *Test.java.",
                    )
                )
            continue
        if path in solution_paths or path.startswith("src/"):
            findings.append(
                _finding(
                    "3",
                    "MAJOR",
                    "Test patch contains solution code",
                    path,
                    "Keep implementation changes in solution.patch. test.patch should add tests only.",
                )
            )
    return findings


def evaluate_harness(repo_path: Path, *, prompt: str = "") -> list[Finding]:
    dockerfile_path = repo_path / "Dockerfile"
    test_sh_path = repo_path / "test.sh"
    dockerfile = dockerfile_path.read_text(encoding="utf-8", errors="replace") if dockerfile_path.is_file() else ""
    test_sh = test_sh_path.read_text(encoding="utf-8", errors="replace") if test_sh_path.is_file() else ""
    test_patch = ""
    solution_patch = ""
    if (repo_path / "test.patch").is_file():
        test_patch = (repo_path / "test.patch").read_text(encoding="utf-8", errors="replace")
    if (repo_path / "solution.patch").is_file():
        solution_patch = (repo_path / "solution.patch").read_text(encoding="utf-8", errors="replace")
    executable = test_sh_path.is_file() and os.access(test_sh_path, os.X_OK)
    findings = evaluate_dockerfile(dockerfile)
    findings.extend(evaluate_python_install(dockerfile, prompt))
    if test_sh or test_patch:
        findings.extend(
            evaluate_test_harness(
                test_sh,
                executable=executable,
                test_patch=test_patch,
                solution_patch=solution_patch,
            )
        )
    return findings
