# Local content hash and agent line-up recorded with a review.
#
# Shipd ties results to the exact submission and the current agent line-up.
# The page does not expose that id, so this only detects edits on the clone
# and changes to the configured line-up between two local reviews.

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from review.review_bundles import PENDING_SUBMIT_PATH
from review.rubric_defaults import EXPECTED_AGENT_MODELS

_HASH_FILES = (
    "problem.md",
    "PROBLEM.md",
    "description.md",
    "prompt.md",
    "title.md",
    "title.txt",
    "TITLE",
    "test.patch",
    "solution.patch",
    "Dockerfile",
    "test.sh",
)


def _file_digest(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(data).hexdigest()


def content_fingerprint(
    repo_path: Path,
    *,
    agent_lineup: dict[str, str] | None = None,
) -> dict[str, Any]:
    repo_path = Path(repo_path)
    parts: list[str] = []
    present: list[str] = []
    for name in _HASH_FILES:
        digest = _file_digest(repo_path / name)
        if not digest:
            continue
        present.append(name)
        parts.append(f"{name}:{digest}")
    lineup = dict(agent_lineup or EXPECTED_AGENT_MODELS)
    payload = "\n".join(parts)
    return {
        "content_hash": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "files": present,
        "agent_lineup": lineup,
    }


def staleness_note(
    repo_path: Path,
    current: dict[str, Any],
    *,
    previous_path: Path = PENDING_SUBMIT_PATH,
) -> str:
    """Note when the previous bundle for this clone no longer matches."""
    if not previous_path.is_file():
        return ""
    try:
        data = json.loads(previous_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    previous_repo = str(data.get("repo_path") or "")
    if not previous_repo or Path(previous_repo).resolve() != Path(repo_path).resolve():
        return ""
    review = data.get("review") if isinstance(data.get("review"), dict) else {}
    previous = review.get("submission_fingerprint")
    if not isinstance(previous, dict) or not previous.get("content_hash"):
        return ""
    notes: list[str] = []
    if previous.get("content_hash") != current.get("content_hash"):
        notes.append(
            "Content changed since the last review of this clone. "
            "Platform results tied to the old prompt, patches, or harness are stale."
        )
    if previous.get("agent_lineup") != current.get("agent_lineup"):
        notes.append(
            "The configured agent line-up changed. Older rollouts do not count "
            "toward the current bar, and the false-positive check has to be rerun."
        )
    return " ".join(notes)
