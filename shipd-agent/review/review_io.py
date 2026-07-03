# Save/load agent review bundles for Playwright submit automation.

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from auth import REPO_ROOT

REVIEWS_DIR = REPO_ROOT / "reviews"
PENDING_SUBMIT_PATH = REVIEWS_DIR / "pending-submit.json"
SESSION_META_PATH = REVIEWS_DIR / "session-meta.json"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def save_session_meta(
    *,
    review_url: str,
    quest: str,
    repo_path: Path | str | None = None,
    path: Path = SESSION_META_PATH,
) -> Path:
    """Persist the active Shipd review URL after reserve/open."""
    payload = {
        "review_url": review_url.strip(),
        "quest": quest.strip().lower(),
        "repo_path": str(repo_path) if repo_path else "",
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(path, payload)
    return path


def load_session_meta(path: Path = SESSION_META_PATH) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Session meta not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not str(data.get("review_url", "")).strip():
        raise ValueError(f"Session meta missing review_url: {path}")
    return data


def save_review_bundle(
    review: dict[str, Any],
    *,
    review_url: str,
    quest: str,
    repo_path: Path | str | None = None,
    path: Path = PENDING_SUBMIT_PATH,
) -> Path:
    """Write agent output plus submit context for submit_from_json.py."""
    payload = {
        "review_url": review_url.strip(),
        "quest": quest.strip().lower(),
        "repo_path": str(repo_path) if repo_path else "",
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "review": dict(review),
    }
    _write_json(path, payload)
    return path


def load_review_bundle(path: Path) -> tuple[dict[str, Any], str, str, str]:
    """Return (review_dict, review_url, quest, repo_path)."""
    if not path.is_file():
        raise FileNotFoundError(f"Review JSON not found: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))

    if isinstance(data.get("review"), dict):
        review = data["review"]
        review_url = str(data.get("review_url", "")).strip()
        quest = str(data.get("quest", "olympus")).strip().lower()
        repo_path = str(data.get("repo_path", "")).strip()
    else:
        # Raw agent dict with optional top-level context fields.
        review = {
            k: v
            for k, v in data.items()
            if k not in {"review_url", "quest", "repo_path", "saved_at"}
        }
        review_url = str(data.get("review_url", "")).strip()
        quest = str(data.get("quest", "olympus")).strip().lower()
        repo_path = str(data.get("repo_path", "")).strip()

    if not review.get("decision"):
        raise ValueError(f"Review JSON missing decision: {path}")
    if not review_url:
        raise ValueError(
            "Review JSON missing review_url "
            f"(needed for Playwright submit): {path}"
        )
    return review, review_url, quest, repo_path
