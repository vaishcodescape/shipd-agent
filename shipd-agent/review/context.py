# Artifact discovery and submission context for the review agent.

from __future__ import annotations

import subprocess
from pathlib import Path

ARTIFACT_NAMES = (
    "test.patch",
    "solution.patch",
    "test.sh",
    "Dockerfile",
)

PROBLEM_CANDIDATES = (
    "problem.md",
    "PROBLEM.md",
    "README.md",
    "description.md",
    "prompt.md",
)


def resolve_repo_path(repo_path: Path) -> Path:
    resolved = repo_path.resolve()
    if not resolved.is_dir():
        raise ValueError(f"repo_path is not a directory: {repo_path}")
    return resolved


def discover_artifacts(repo_path: Path) -> dict[str, str | None]:
    """Find standard Shipd submission artifacts by glob/name."""
    repo_path = resolve_repo_path(repo_path)
    found: dict[str, str | None] = {name: None for name in ARTIFACT_NAMES}

    for name in ARTIFACT_NAMES:
        direct = repo_path / name
        if direct.is_file():
            found[name] = str(direct.relative_to(repo_path))
            continue
        matches = sorted(repo_path.rglob(name))
        if matches:
            found[name] = str(matches[0].relative_to(repo_path))

    return found


def find_problem_text_path(repo_path: Path) -> Path | None:
    repo_path = resolve_repo_path(repo_path)
    for name in PROBLEM_CANDIDATES:
        candidate = repo_path / name
        if candidate.is_file():
            return candidate
    readme_matches = sorted(repo_path.glob("**/README.md"))
    return readme_matches[0] if readme_matches else None


def read_problem_excerpt(repo_path: Path, *, max_chars: int = 4000) -> str:
    path = find_problem_text_path(repo_path)
    if path is None:
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n… [truncated]"


def build_file_tree(repo_path: Path, *, max_depth: int = 2, max_entries: int = 200) -> str:
    repo_path = resolve_repo_path(repo_path)
    lines: list[str] = []
    count = 0

    def walk(directory: Path, depth: int) -> None:
        nonlocal count
        if depth > max_depth or count >= max_entries:
            return
        try:
            entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return
        for entry in entries:
            if entry.name.startswith(".git"):
                continue
            rel = entry.relative_to(repo_path)
            prefix = "  " * depth
            suffix = "/" if entry.is_dir() else ""
            lines.append(f"{prefix}{rel}{suffix}")
            count += 1
            if count >= max_entries:
                lines.append(f"{prefix}… [truncated at {max_entries} entries]")
                return
            if entry.is_dir():
                walk(entry, depth + 1)

    walk(repo_path, 0)
    return "\n".join(lines) if lines else "(empty repo)"


def get_git_commit(repo_path: Path) -> str | None:
    repo_path = resolve_repo_path(repo_path)
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    commit = result.stdout.strip()
    return commit or None


def build_submission_summary(
    repo_path: Path,
    *,
    quest: str,
    review_url: str = "",
) -> dict:
    repo_path = resolve_repo_path(repo_path)
    artifacts = discover_artifacts(repo_path)
    return {
        "repo_path": str(repo_path),
        "commit": get_git_commit(repo_path),
        "quest": quest,
        "review_url": review_url,
        "artifacts": artifacts,
        "problem_excerpt": read_problem_excerpt(repo_path),
        "file_tree": build_file_tree(repo_path),
    }
