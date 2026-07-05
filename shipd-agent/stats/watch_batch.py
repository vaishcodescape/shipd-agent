# Persist multi-review batch progress for resume after stop or failure.

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from auth import REPO_ROOT

BATCH_PATH = REPO_ROOT / "logs" / "watch-batch.json"
VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_options() -> dict[str, Any]:
    return {
        "review": False,
        "submit": False,
        "clone": True,
        "cleanup": None,
        "separate_steps": False,
        "cooldown_every": 5,
        "cooldown_sec": 3600,
    }


def load_batch(path: Path = BATCH_PATH) -> dict[str, Any] | None:
    """Return saved batch state, or None if missing or invalid."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if int(data.get("version", 0)) != VERSION:
        return None
    max_runs = int(data.get("max_runs", 0))
    completed = int(data.get("completed_runs", 0))
    if max_runs <= 0 or completed < 0 or completed > max_runs:
        return None
    saved_options = data.get("options")
    if isinstance(saved_options, dict):
        data["options"] = {
            **_empty_options(),
            **saved_options,
        }
    else:
        data["options"] = _empty_options()
    return data


def save_batch(state: dict[str, Any], path: Path = BATCH_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def clear_batch(path: Path = BATCH_PATH) -> None:
    if path.is_file():
        path.unlink()


def get_active_batch(path: Path = BATCH_PATH) -> dict[str, Any] | None:
    """Return in-progress batch state, clearing it when the batch is complete."""
    batch = load_batch(path)
    if batch is None:
        return None
    if int(batch["completed_runs"]) >= int(batch["max_runs"]):
        clear_batch(path)
        return None
    return batch


def start_batch(
    *,
    max_runs: int,
    quest: str,
    interval_sec: int,
    options: dict[str, Any] | None = None,
    path: Path = BATCH_PATH,
) -> dict[str, Any]:
    """Begin a new batch, replacing any previous resume state."""
    if max_runs <= 0:
        raise ValueError(f"max_runs must be positive, got {max_runs}")
    now = _now_iso()
    state: dict[str, Any] = {
        "version": VERSION,
        "max_runs": int(max_runs),
        "completed_runs": 0,
        "quest": quest.strip().lower(),
        "interval_sec": int(interval_sec),
        "options": {**_empty_options(), **(options or {})},
        "last_run_status": None,
        "last_failed_review_url": "",
        "started_at": now,
        "updated_at": now,
    }
    save_batch(state, path)
    return state


def options_compatible(
    batch: dict[str, Any],
    *,
    quest: str,
    interval_sec: int,
    options: dict[str, Any],
) -> bool:
    """Return True when caller flags match the saved batch."""
    if batch.get("quest") != quest.strip().lower():
        return False
    if int(batch.get("interval_sec", 0)) != int(interval_sec):
        return False
    saved = batch.get("options") or {}
    for key, value in options.items():
        if saved.get(key) != value:
            return False
    return True


def record_run_complete(
    status: str,
    *,
    review_url: str = "",
    path: Path = BATCH_PATH,
) -> dict[str, Any] | None:
    """Increment completed runs after one review cycle finishes."""
    batch = load_batch(path)
    if batch is None:
        return None

    batch["completed_runs"] = int(batch.get("completed_runs", 0)) + 1
    batch["last_run_status"] = status
    if status == "fail" and review_url.strip():
        batch["last_failed_review_url"] = review_url.strip()
    batch["updated_at"] = _now_iso()

    if int(batch["completed_runs"]) >= int(batch["max_runs"]):
        clear_batch(path)
        return batch

    save_batch(batch, path)
    return batch


def next_run_number(batch: dict[str, Any]) -> int:
    return int(batch.get("completed_runs", 0)) + 1


def remaining_runs(batch: dict[str, Any]) -> int:
    return int(batch["max_runs"]) - int(batch.get("completed_runs", 0))


def format_resume_message(batch: dict[str, Any]) -> str:
    completed = int(batch.get("completed_runs", 0))
    total = int(batch["max_runs"])
    nxt = completed + 1
    return (
        f"Resuming batch: {completed}/{total} completed, "
        f"next review is {nxt}/{total}"
    )


def is_batch_complete(batch: dict[str, Any] | None) -> bool:
    if batch is None:
        return True
    return int(batch.get("completed_runs", 0)) >= int(batch.get("max_runs", 0))
