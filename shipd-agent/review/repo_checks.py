# Deterministic repo eligibility. Offline, this skips rather than guessing.

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from review.rubric_defaults import OLYMPUS_MIN_REPO_STARS
from review.schemas import Finding, PhaseResult

_PERMISSIVE = {
    "mit",
    "apache-2.0",
    "bsd-2-clause",
    "bsd-3-clause",
    "isc",
    "mpl-2.0",
    "unlicense",
    "0bsd",
}
_LIMIT_NOTE = (
    "Shipd allows 12 submissions of a repo per contributor and 50 overall. "
    "This check does not count other people's submissions."
)


@dataclass
class RepoMetadata:
    stars: int | None = None
    pushed_at: str | None = None
    language: str | None = None
    license: str | None = None
    error: str = ""


def _origin(repo_path: Path) -> str:
    """Origin of this directory when it is the git root. Parent repos do not count."""
    repo_path = Path(repo_path).resolve()
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != repo_path:
            return ""
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _slug(origin: str) -> str:
    text = origin.strip()
    if text.startswith("git@github.com:"):
        path = text[len("git@github.com:"):]
    else:
        parsed = urlsplit(text)
        if parsed.hostname != "github.com" or parsed.scheme not in {"https", "http", "ssh", "git"}:
            return ""
        if parsed.query or parsed.fragment:
            return ""
        path = parsed.path.lstrip("/")
    path = path.rstrip("/").removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", path):
        return ""
    return path


def fetch_repo_metadata(slug: str) -> RepoMetadata:
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{slug}",
                "--jq",
                "{stars: .stargazers_count, pushed: .pushed_at, language: .language, license: .license.spdx_id}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return RepoMetadata(error=str(exc))
    if result.returncode != 0:
        return RepoMetadata(error=(result.stderr or result.stdout or "gh api failed").strip()[:300])
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return RepoMetadata(error=str(exc))
    if not isinstance(payload, dict):
        return RepoMetadata(error="GitHub metadata was not an object")
    stars = payload.get("stars")
    if not isinstance(stars, int) or isinstance(stars, bool) or stars < 0:
        return RepoMetadata(error="GitHub metadata has no valid star count")
    if any(payload.get(key) is not None and not isinstance(payload[key], str)
           for key in ("pushed", "language", "license")):
        return RepoMetadata(error="GitHub metadata has invalid field types")
    return RepoMetadata(
        stars=payload.get("stars"),
        pushed_at=payload.get("pushed"),
        language=payload.get("language"),
        license=payload.get("license"),
    )


def evaluate_repo_metadata(
    meta: RepoMetadata,
    *,
    now: datetime | None = None,
    min_stars: int = OLYMPUS_MIN_REPO_STARS,
) -> tuple[PhaseResult, list[Finding]]:
    if meta.error or meta.stars is None:
        return (
            PhaseResult(
                status="SKIP",
                summary="Repo eligibility skipped: " + (meta.error or "no GitHub metadata") + f" {_LIMIT_NOTE}",
            ),
            [],
        )
    findings: list[Finding] = []
    unknown_date = False
    if meta.stars < min_stars:
        findings.append(
            Finding(
                phase="6",
                severity="MAJOR",
                finding="Repository has fewer than 500 stars",
                evidence=f"{meta.stars} stars (minimum {min_stars}). {_LIMIT_NOTE}",
                suggested_fix="Choose a public repository with at least 500 stars.",
            )
        )
    if meta.pushed_at:
        moment = now or datetime.now(timezone.utc)
        try:
            pushed = datetime.fromisoformat(meta.pushed_at.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            pushed = None
            unknown_date = True
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        if pushed is not None and pushed.tzinfo is None:
            pushed = pushed.replace(tzinfo=timezone.utc)
        if pushed is not None and (moment - pushed).days > 365:
            findings.append(
                Finding(
                    phase="6",
                    severity="MAJOR",
                    finding="Repository has no commit in the past year",
                    evidence=f"pushed_at={meta.pushed_at}. {_LIMIT_NOTE}",
                    suggested_fix="Use a repository with a commit in the past year.",
                )
            )
    else:
        findings.append(
            Finding(
                phase="6",
                severity="MAJOR",
                finding="Repository push date is missing",
                evidence=_LIMIT_NOTE,
                suggested_fix="Confirm the repository has a commit in the past year.",
            )
        )
    license_id = (meta.license or "").lower()
    if license_id in {"", "null", "none", "noassertion"}:
        findings.append(
            Finding(
                phase="6",
                severity="MAJOR",
                finding="Repository license is missing",
                evidence=f"license={meta.license!r}. {_LIMIT_NOTE}",
                suggested_fix="Use a permissive license.",
            )
        )
    elif license_id not in _PERMISSIVE:
        findings.append(
            Finding(
                phase="6",
                severity="MAJOR",
                finding="Repository license is not a known permissive license",
                evidence=f"license={meta.license}. {_LIMIT_NOTE}",
                suggested_fix="Use a permissive license such as MIT, Apache-2.0, or BSD.",
            )
        )
    if not meta.language:
        findings.append(
            Finding(
                phase="6",
                severity="MAJOR",
                finding="Repository language is missing",
                evidence=_LIMIT_NOTE,
                suggested_fix="Use a repository whose language GitHub can detect.",
            )
        )
    if findings:
        return (
            PhaseResult(status="FAIL", summary="Repo eligibility failed. " + _LIMIT_NOTE),
            findings,
        )
    if unknown_date:
        return (
            PhaseResult(status="SKIP", summary="Repo eligibility unverified: invalid push date. " + _LIMIT_NOTE),
            [],
        )
    return (
        PhaseResult(
            status="PASS",
            summary=(
                f"Repo eligibility passed: {meta.stars} stars, {meta.language}, "
                f"license {meta.license}. {_LIMIT_NOTE}"
            ),
        ),
        [],
    )


def evaluate_repo(
    repo_path: Path,
    *,
    fetch: Callable[[str], RepoMetadata] | None = None,
) -> tuple[PhaseResult, list[Finding]]:
    origin = _origin(repo_path)
    slug = _slug(origin)
    if not slug:
        return (
            PhaseResult(
                status="SKIP",
                summary="Repo eligibility skipped: no GitHub origin. " + _LIMIT_NOTE,
            ),
            [],
        )
    meta = (fetch or fetch_repo_metadata)(slug)
    return evaluate_repo_metadata(meta)
