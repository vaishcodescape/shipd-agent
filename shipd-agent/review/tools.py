# Bounded LangChain tools scoped to a submission repo.

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from review.config import ReviewConfig, get_review_config
from review.context import build_submission_summary, discover_artifacts, resolve_repo_path
from review.loc import compute_effective_loc, format_loc_analysis
from review.review_phases import Phase0Result, run_phase0
from review.review_phases import check_patch_apply as git_check_patch_apply
from review.token_budget import truncate_text


class ReadFileInput(BaseModel):
    path: str = Field(description="Relative path within the submission repo")
    max_chars: int = Field(default=12_000, ge=1, le=100_000)


class ListDirectoryInput(BaseModel):
    path: str = Field(default=".", description="Relative directory path")
    max_depth: int = Field(default=2, ge=0, le=4)


class SearchRepoInput(BaseModel):
    pattern: str = Field(description="Regex or literal pattern to search for")
    glob: str = Field(default="*", description="Glob filter, e.g. '*.py'")


class CheckPatchApplyInput(BaseModel):
    patch_name: str = Field(
        description="Patch filename, e.g. test.patch or solution.patch",
    )


class ReadShipdPanelInput(BaseModel):
    panel_name: str = Field(
        description=(
            "Review page panel heading, e.g. 'Holistic Check', "
            "'Agent Runs', or 'Related Submissions'."
        ),
    )


def _safe_path(repo_path: Path, rel_path: str) -> Path:
    repo_path = resolve_repo_path(repo_path)
    candidate = (repo_path / rel_path).resolve()
    try:
        candidate.relative_to(repo_path)
    except ValueError as exc:
        raise ValueError(f"Path escapes repo: {rel_path}") from exc
    return candidate


def make_review_tools(
    repo_path: Path,
    *,
    quest: str,
    review_url: str = "",
    cached_summary: dict | None = None,
    page: Any = None,
    cached_scrape: dict[str, str] | None = None,
    cached_holistic: dict | None = None,
    cached_phase0: Phase0Result | None = None,
    config: ReviewConfig | None = None,
) -> list[StructuredTool]:
    repo_path = resolve_repo_path(repo_path)
    cfg = config or get_review_config()
    read_cap = cfg.review_tool_read_max_chars
    phase0_cap = cfg.review_phase0_log_max_chars

    def read_file(path: str, max_chars: int = read_cap) -> str:
        cap = min(max_chars, read_cap)
        target = _safe_path(repo_path, path)
        if not target.is_file():
            return f"Error: not a file: {path}"
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"Error reading {path}: {exc}"
        if len(text) > cap:
            return truncate_text(text, cap, label=f"read_file {path}")
        return text

    def list_directory(path: str = ".", max_depth: int = 2) -> str:
        root = _safe_path(repo_path, path)
        if not root.is_dir():
            return f"Error: not a directory: {path}"
        lines: list[str] = []

        def walk(directory: Path, depth: int) -> None:
            if depth > max_depth:
                return
            try:
                entries = sorted(
                    directory.iterdir(),
                    key=lambda p: (not p.is_dir(), p.name.lower()),
                )
            except OSError as exc:
                lines.append(f"Error listing {directory}: {exc}")
                return
            for entry in entries:
                if entry.name == ".git":
                    continue
                rel = entry.relative_to(repo_path)
                indent = "  " * depth
                suffix = "/" if entry.is_dir() else ""
                lines.append(f"{indent}{rel}{suffix}")
                if entry.is_dir():
                    walk(entry, depth + 1)

        walk(root, 0)
        return "\n".join(lines) if lines else "(empty)"

    def search_repo(pattern: str, glob: str = "*") -> str:
        try:
            result = subprocess.run(
                [
                    "rg",
                    "--line-number",
                    "--no-heading",
                    "--max-count",
                    "50",
                    "--glob",
                    glob,
                    pattern,
                    ".",
                ],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode in (0, 1):
                output = result.stdout.strip()
                return output or "(no matches)"
            return f"rg error: {result.stderr.strip() or result.stdout.strip()}"
        except FileNotFoundError:
            return _python_search(repo_path, pattern, glob)

    def get_submission_summary() -> str:
        summary = cached_summary or build_submission_summary(
            repo_path,
            quest=quest,
            review_url=review_url,
        )
        lines = [
            f"repo_path: {summary['repo_path']}",
            f"commit: {summary['commit']}",
            f"quest: {summary['quest']}",
            f"review_url: {summary['review_url']}",
            "artifacts:",
        ]
        for name, rel in summary["artifacts"].items():
            lines.append(f"  {name}: {rel or 'MISSING'}")
        if summary.get("problem_excerpt"):
            lines.append("\nproblem excerpt:\n" + str(summary["problem_excerpt"][:2000]))
        lines.append("\nfile tree:\n" + str(summary["file_tree"]))
        return "\n".join(lines)

    def list_artifacts() -> str:
        artifacts = discover_artifacts(repo_path)
        lines = ["Submission artifacts:"]
        for name, rel in artifacts.items():
            status = rel if rel else "MISSING"
            lines.append(f"  {name}: {status}")
        return "\n".join(lines)

    def _format_phase0_result(result: Phase0Result) -> str:
        log = truncate_text(result.phase0_log, phase0_cap, label="phase0 log")
        return (
            f"Phase 0 status: {result.status}\n"
            f"Summary: {result.summary}\n\n"
            f"{log}"
        )

    def run_phase0_checks() -> str:
        # Docker build + 4 test.sh runs take minutes; the deterministic result
        # from review start is authoritative, so serve it from cache.
        if cached_phase0 is not None:
            return (
                "Phase 0 checks (cached — Docker build and test.sh contract "
                "already executed deterministically at review start):\n"
                + _format_phase0_result(cached_phase0)
            )

        summary = cached_summary or build_submission_summary(
            repo_path,
            quest=quest,
            review_url=review_url,
        )
        result = run_phase0(
            repo_path,
            artifacts=summary.get("artifacts"),
            commit=summary.get("commit"),
            run_tests=cfg.review_phase0 != "fast",
            test_timeout=cfg.review_phase0_test_timeout,
            build_timeout=cfg.review_phase0_docker_build_timeout,
        )
        return _format_phase0_result(result)

    def check_patch_apply(patch_name: str) -> str:
        artifacts = (
            cached_phase0.artifacts
            if cached_phase0 is not None and cached_phase0.artifacts
            else discover_artifacts(repo_path)
        )
        rel = artifacts.get(patch_name)
        if rel is None:
            return f"{patch_name}: artifact not found"
        ok, msg = git_check_patch_apply(repo_path, rel)
        status = "OK" if ok else "FAILED"
        return f"{patch_name}: git apply --check {status}\n{msg}"

    def get_git_info() -> str:
        try:
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            ).stdout.strip()
            status = subprocess.run(
                ["git", "status", "--short"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            ).stdout.strip()
            log = subprocess.run(
                ["git", "log", "-5", "--oneline"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            ).stdout.strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
            return f"git info unavailable: {exc}"
        parts = [f"HEAD: {head}"]
        if status:
            parts.append(f"status:\n{status}")
        if log:
            parts.append(f"recent commits:\n{log}")
        return "\n\n".join(parts)

    def compute_effective_loc_tool() -> str:
        info = compute_effective_loc(repo_path)
        analysis = format_loc_analysis(
            info,
            quest=quest,
            olympus_min=cfg.olympus_min_loc,
            mars_min=cfg.mars_min_loc,
            mars_max=cfg.mars_max_loc,
        )
        per_file = info.get("per_file") or {}
        per_file_lines = "\n".join(
            f"  {path}: {count}" for path, count in sorted(per_file.items())
        )
        parts = [
            analysis,
            f"method: {info.get('method')}",
            f"effective_loc: {info.get('effective_loc')}",
        ]
        if per_file_lines:
            parts.append(f"per_file:\n{per_file_lines}")
        return "\n".join(parts)

    tools = [
        StructuredTool.from_function(
            func=read_file,
            name="read_file",
            description="Read a text file from the submission repo (relative path).",
            args_schema=ReadFileInput,
        ),
        StructuredTool.from_function(
            func=list_directory,
            name="list_directory",
            description="List files under a directory in the submission repo.",
            args_schema=ListDirectoryInput,
        ),
        StructuredTool.from_function(
            func=search_repo,
            name="search_repo",
            description="Search the repo with ripgrep (pattern, optional glob).",
            args_schema=SearchRepoInput,
        ),
        StructuredTool.from_function(
            func=get_submission_summary,
            name="get_submission_summary",
            description="Return artifacts, commit, quest, and file tree summary.",
        ),
        StructuredTool.from_function(
            func=list_artifacts,
            name="list_artifacts",
            description="List required submission artifacts and whether each is present.",
        ),
        StructuredTool.from_function(
            func=run_phase0_checks,
            name="run_phase0_checks",
            description=(
                "Get Phase 0 mechanical check results: artifact presence, git "
                "HEAD, patch apply --check, and Docker test.sh base/new runs "
                "(--network none). Served from the deterministic run at review "
                "start — cheap to call."
            ),
        ),
        StructuredTool.from_function(
            func=check_patch_apply,
            name="check_patch_apply",
            description="Run git apply --check on a specific patch file.",
            args_schema=CheckPatchApplyInput,
        ),
        StructuredTool.from_function(
            func=get_git_info,
            name="get_git_info",
            description="Return git HEAD, short status, and recent commits.",
        ),
        StructuredTool.from_function(
            func=compute_effective_loc_tool,
            name="compute_effective_loc",
            description=(
                "Count substantive solution LOC from solution.patch "
                "(excludes blanks and comment-only added lines)."
            ),
        ),
    ]

    if page is not None or cached_scrape is not None or cached_holistic is not None:
        from review.prompts import build_holistic_check_prompt_section
        from review.scrape import (
            parse_agent_runs_from_text,
            parse_holistic_check_from_text,
            parse_related_submissions_from_text,
            scrape_agent_runs,
            scrape_holistic_check,
            scrape_related_submissions,
        )

        def read_holistic_check() -> str:
            if page is not None:
                data = scrape_holistic_check(page)
            elif cached_holistic:
                data = cached_holistic
            elif cached_scrape:
                return build_holistic_check_prompt_section(cached_scrape)
            else:
                return "Holistic AI Check: not available — run via orchestrator with browser session"
            return build_holistic_check_prompt_section(
                {
                    "holistic_check_available": "true" if data.get("available") else "false",
                    "holistic_check_status": str(data.get("status") or ""),
                    "holistic_check_checklist": str(data.get("checklist_summary") or ""),
                    "holistic_check_reviewer_notes": str(data.get("reviewer_notes") or ""),
                    "holistic_check_raw": str(data.get("raw_text") or ""),
                }
            )

        def read_shipd_review_panel(panel_name: str) -> str:
            normalized = panel_name.strip().lower()
            if normalized in ("holistic check", "holistic ai check", "holistic"):
                return read_holistic_check()

            if page is not None:
                if "agent" in normalized:
                    data = scrape_agent_runs(page)
                    if data.get("available"):
                        parts = []
                        if data.get("pass_rate"):
                            parts.append(f"Pass rate: {data['pass_rate']}")
                        if data.get("summary"):
                            parts.append(data["summary"])
                        if data.get("failure_patterns"):
                            parts.append(f"Failure patterns:\n{data['failure_patterns']}")
                        return "\n\n".join(parts) or data.get("raw_text", "")
                    return data.get("raw_text") or "Agent Runs: section not found on page."
                if "related" in normalized:
                    data = scrape_related_submissions(page)
                    if data.get("available"):
                        parts = []
                        if data.get("entries"):
                            parts.append(data["entries"])
                        if data.get("tags"):
                            parts.append(f"Tags: {data['tags']}")
                        return "\n\n".join(parts) or data.get("raw_text", "")
                    return data.get("raw_text") or "Related Submissions: section not found on page."

                from review.scrape import _scrape_section_text

                raw = _scrape_section_text(page, panel_name)
                if raw:
                    if "holistic" in normalized:
                        return build_holistic_check_prompt_section(
                            {
                                **{
                                    k.replace("holistic_check_", ""): v
                                    for k, v in parse_holistic_check_from_text(raw).items()
                                },
                                "holistic_check_available": "true",
                            }
                        )
                    if "agent" in normalized:
                        return parse_agent_runs_from_text(raw).get("raw_text", raw)
                    if "related" in normalized:
                        return parse_related_submissions_from_text(raw).get("raw_text", raw)
                    return raw
                return f"Panel {panel_name!r}: not found on review page."

            if cached_scrape:
                if "agent" in normalized:
                    return cached_scrape.get("agent_runs", "not available")
                if "related" in normalized:
                    return cached_scrape.get("related_submissions", "not available")
                if "holistic" in normalized:
                    return read_holistic_check()
            return "not available — run via orchestrator with browser session"

        tools.extend(
            [
                StructuredTool.from_function(
                    func=read_holistic_check,
                    name="read_holistic_check",
                    description=(
                        "Read the Shipd Holistic AI Check panel (status, checklist, "
                        "reviewer notes). Use for phases 5–6 before giving feedback."
                    ),
                ),
                StructuredTool.from_function(
                    func=read_shipd_review_panel,
                    name="read_shipd_review_panel",
                    description=(
                        "Read a Shipd review page panel by heading "
                        "(Holistic Check, Agent Runs, Related Submissions)."
                    ),
                    args_schema=ReadShipdPanelInput,
                ),
            ]
        )

    return tools


def _python_search(repo_path: Path, pattern: str, glob_pattern: str) -> str:
    try:
        regex = re.compile(pattern)
    except re.error:
        regex = re.compile(re.escape(pattern))

    matches: list[str] = []
    for path in repo_path.rglob(glob_pattern.lstrip("./")):
        if not path.is_file() or ".git" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                rel = path.relative_to(repo_path)
                matches.append(f"{rel}:{line_no}:{line[:200]}")
                if len(matches) >= 50:
                    return "\n".join(matches) + "\n… [truncated at 50 matches]"
    return "\n".join(matches) if matches else "(no matches)"
