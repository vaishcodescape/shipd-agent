# Effective solution LOC analysis from solution.patch or changed files.

from __future__ import annotations

import re
from pathlib import Path

from review.context import discover_artifacts, resolve_repo_path

# File extensions treated as code for comment detection.
_CODE_EXTENSIONS = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".go",
        ".rs",
        ".java",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
        ".cs",
        ".rb",
        ".php",
        ".swift",
        ".kt",
        ".scala",
        ".sh",
        ".bash",
        ".zsh",
        ".sql",
        ".yaml",
        ".yml",
        ".toml",
        ".json",
        ".md",
        ".html",
        ".css",
        ".scss",
        ".vue",
        ".lua",
        ".r",
        ".m",
        ".mm",
    }
)


_PATCH_FILE_HEADER = re.compile(r"^\+\+\+\s+b?/?(.+)$")


def _strip_inline_comment(line: str, ext: str) -> str:
    """Remove trailing comments for common languages (best-effort)."""
    stripped = line.strip()
    if ext in {".py", ".sh", ".bash", ".zsh", ".yaml", ".yml", ".toml", ".rb", ".r"}:
        if "#" in stripped:
            before, _, after = stripped.partition("#")
            if not before.endswith("\\"):
                return before.rstrip()
    if ext in {
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".go",
        ".rs",
        ".java",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
        ".cs",
        ".php",
        ".swift",
        ".kt",
        ".scala",
        ".css",
        ".scss",
        ".vue",
    }:
        if stripped.startswith("//"):
            return ""
        if "//" in stripped:
            return stripped.split("//", 1)[0].rstrip()
    if ext in {".html", ".xml"}:
        if "<!--" in stripped:
            return stripped.split("<!--", 1)[0].rstrip()
    if ext == ".sql":
        if "--" in stripped:
            return stripped.split("--", 1)[0].rstrip()
    return stripped


def _is_comment_only(line: str, ext: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if ext in {".py", ".sh", ".bash", ".zsh", ".yaml", ".yml", ".toml", ".rb", ".r"}:
        return stripped.startswith("#")
    if ext in {
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".go",
        ".rs",
        ".java",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
        ".cs",
        ".php",
        ".swift",
        ".kt",
        ".scala",
        ".css",
        ".scss",
        ".vue",
    }:
        return stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*")
    if ext in {".html", ".xml"}:
        return stripped.startswith("<!--")
    if ext == ".sql":
        return stripped.startswith("--")
    return False


def _is_substantive_line(line: str, ext: str) -> bool:
    """True when an added line counts toward effective LOC."""
    if _is_comment_only(line, ext):
        return False
    content = _strip_inline_comment(line, ext).strip()
    if not content:
        return False
    # Block comments spanning one line
    if content in {"*/", "*/;"}:
        return False
    return True


def _ext_for_path(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return suffix if suffix in _CODE_EXTENSIONS else ".txt"


def count_substantive_lines_from_patch(patch_text: str) -> tuple[int, list[str], dict[str, int]]:
    """Count substantive added lines from unified diff text."""
    per_file: dict[str, int] = {}
    current_file = ""
    current_ext = ".txt"
    total = 0

    for raw_line in patch_text.splitlines():
        if raw_line.startswith("+++ "):
            match = _PATCH_FILE_HEADER.match(raw_line)
            if match:
                current_file = match.group(1).strip()
                if current_file in ("/dev/null", "dev/null"):
                    current_file = ""
                else:
                    current_ext = _ext_for_path(current_file)
            continue

        if not raw_line.startswith("+") or raw_line.startswith("+++"):
            continue
        if raw_line.startswith("+++") or raw_line.startswith("---"):
            continue

        added = raw_line[1:]
        if not current_file:
            current_file = "(unknown)"
            current_ext = ".txt"

        if _is_substantive_line(added, current_ext):
            total += 1
            per_file[current_file] = per_file.get(current_file, 0) + 1

    files_analyzed = sorted(per_file.keys())
    return total, files_analyzed, per_file


def compute_effective_loc(
    repo_path: Path | str,
    solution_patch_path: str | Path | None = None,
) -> dict:
    """
    Count substantive solution LOC (exclude blanks, comments, pure formatting).

    Prefers parsing solution.patch hunks; falls back to artifact discovery.
    """
    repo_path = resolve_repo_path(Path(repo_path))
    notes: list[str] = []

    if solution_patch_path is not None:
        patch_path = Path(solution_patch_path)
        if not patch_path.is_absolute():
            patch_path = repo_path / patch_path
    else:
        artifacts = discover_artifacts(repo_path)
        rel = artifacts.get("solution.patch")
        if rel is None:
            return {
                "effective_loc": 0,
                "files_analyzed": [],
                "method": "none",
                "notes": "solution.patch not found",
                "per_file": {},
            }
        patch_path = repo_path / rel

    if not patch_path.is_file():
        return {
            "effective_loc": 0,
            "files_analyzed": [],
            "method": "none",
            "notes": f"solution patch missing: {patch_path}",
            "per_file": {},
        }

    try:
        patch_text = patch_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {
            "effective_loc": 0,
            "files_analyzed": [],
            "method": "none",
            "notes": f"could not read patch: {exc}",
            "per_file": {},
        }

    if not patch_text.strip():
        return {
            "effective_loc": 0,
            "files_analyzed": [],
            "method": "solution.patch",
            "notes": "solution.patch is empty",
            "per_file": {},
        }

    total, files, per_file = count_substantive_lines_from_patch(patch_text)
    rel_patch = patch_path.relative_to(repo_path) if patch_path.is_relative_to(repo_path) else patch_path.name
    notes.append(f"Parsed {rel_patch}: {total} substantive added lines across {len(files)} file(s).")

    return {
        "effective_loc": total,
        "files_analyzed": files,
        "method": "solution.patch",
        "notes": " ".join(notes),
        "per_file": per_file,
    }


def format_loc_analysis(
    loc_info: dict,
    *,
    quest: str,
    olympus_min: int,
    mars_min: int,
    mars_max: int,
) -> str:
    """Human-readable loc_analysis string for ReviewResult."""
    loc = loc_info.get("effective_loc", 0)
    method = loc_info.get("method", "none")
    if method == "none":
        return loc_info.get("notes", "LOC analysis skipped — no solution.patch.")

    files = loc_info.get("files_analyzed") or []
    file_note = f" Files: {', '.join(files)}." if files else ""
    threshold_note = (
        f" Thresholds — Olympus min {olympus_min}, Mars {mars_min}–{mars_max}."
    )
    return (
        f"Effective solution LOC: {loc} ({method}).{file_note}{threshold_note} "
        f"{loc_info.get('notes', '').strip()}"
    ).strip()
