# Track review outcomes across a batch or watch-mode session.

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from auth import REPO_ROOT

STATS_PATH = REPO_ROOT / "logs" / "session-stats.json"

VALID_DECISIONS = frozenset({"approve", "request_changes", "reject"})
VALID_QUESTS = frozenset({"mars", "olympus"})

_DECISION_COUNT_KEYS = {
    "approve": "approved",
    "request_changes": "request_changes",
    "reject": "rejected",
}


def _empty_quest_counts() -> dict[str, int]:
    return {
        "approved": 0,
        "request_changes": 0,
        "rejected": 0,
        "total_completed": 0,
    }


def _empty_stats() -> dict[str, Any]:
    return {
        "started_at": None,
        "counts": {
            "approved": 0,
            "request_changes": 0,
            "rejected": 0,
            "failed": 0,
            "total_completed": 0,
        },
        "per_quest": {quest: _empty_quest_counts() for quest in sorted(VALID_QUESTS)},
        "reviews": [],
    }


def normalize_quest(quest: str) -> str:
    """Map quest strings to mars or olympus."""
    normalized = quest.strip().lower()
    if normalized in VALID_QUESTS:
        return normalized
    raise ValueError(f"Unknown quest: {quest!r}")


def _load() -> dict[str, Any]:
    if STATS_PATH.is_file():
        data = json.loads(STATS_PATH.read_text(encoding="utf-8"))
        per_quest = data.setdefault("per_quest", {})
        for quest in VALID_QUESTS:
            per_quest.setdefault(quest, _empty_quest_counts())
        return data
    return _empty_stats()


def _save(data: dict[str, Any]) -> None:
    STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def reset_session() -> None:
    """Clear session counters and start a fresh tracking window."""
    data = _empty_stats()
    data["started_at"] = datetime.now(timezone.utc).isoformat()
    _save(data)


def normalize_decision(decision: str) -> str:
    """Map decision strings to approve, request_changes, or reject."""
    normalized = decision.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in VALID_DECISIONS:
        return normalized

    aliases = {
        "approved": "approve",
        "accept": "approve",
        "pass": "approve",
        "requestchange": "request_changes",
        "changes_requested": "request_changes",
        "revise": "request_changes",
        "rejected": "reject",
        "decline": "reject",
    }
    mapped = aliases.get(normalized)
    if mapped in VALID_DECISIONS:
        return mapped

    raise ValueError(f"Unknown review decision: {decision!r}")


def record_decision(
    decision: str,
    repo_path: Path | str = "",
    review_url: str = "",
    quest: str = "olympus",
) -> None:
    """Record a completed review decision and append it to the session log."""
    normalized = normalize_decision(decision)
    quest_key = normalize_quest(quest)
    data = _load()
    counts = data["counts"]
    count_key = _DECISION_COUNT_KEYS[normalized]
    counts[count_key] = int(counts.get(count_key, 0)) + 1
    counts["total_completed"] = int(counts.get("total_completed", 0)) + 1

    quest_counts = data["per_quest"].setdefault(quest_key, _empty_quest_counts())
    quest_counts[count_key] = int(quest_counts.get(count_key, 0)) + 1
    quest_counts["total_completed"] = int(quest_counts.get("total_completed", 0)) + 1

    data["reviews"].append(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "decision": normalized,
            "quest": quest_key,
            "repo_path": str(repo_path),
            "review_url": review_url,
        }
    )
    _save(data)


def record_failure() -> None:
    """Increment the failed-workflow counter (review did not complete)."""
    data = _load()
    counts = data["counts"]
    counts["failed"] = int(counts.get("failed", 0)) + 1
    _save(data)


def get_summary() -> dict[str, int]:
    """Return current session counters."""
    counts = _load()["counts"]
    return {
        "approved": int(counts.get("approved", 0)),
        "request_changes": int(counts.get("request_changes", 0)),
        "rejected": int(counts.get("rejected", 0)),
        "failed": int(counts.get("failed", 0)),
        "total_completed": int(counts.get("total_completed", 0)),
    }


def format_running_totals() -> str:
    """Short running-totals line for after each watch-mode cycle."""
    summary = get_summary()
    return (
        f"Session stats: Approved {summary['approved']}, "
        f"Request changes {summary['request_changes']}, "
        f"Rejected {summary['rejected']}, "
        f"Failed {summary['failed']}"
    )


def format_summary_log() -> str:
    """Human-readable session summary including completed review count."""
    summary = get_summary()
    return (
        f"{format_running_totals()} "
        f"({summary['total_completed']} completed)"
    )


def get_quest_summaries() -> dict[str, dict[str, int]]:
    """Return per-quest decision counters for quests with completed reviews."""
    data = _load()
    summaries: dict[str, dict[str, int]] = {}
    for quest, counts in data.get("per_quest", {}).items():
        total = int(counts.get("total_completed", 0))
        if total <= 0:
            continue
        summaries[quest] = {
            "approved": int(counts.get("approved", 0)),
            "request_changes": int(counts.get("request_changes", 0)),
            "rejected": int(counts.get("rejected", 0)),
            "total_completed": total,
        }
    return summaries


def format_clock_out_message() -> str:
    """Format per-quest lines for the Shipd clock-out notes field."""
    summaries = get_quest_summaries()
    lines: list[str] = []
    for quest in sorted(summaries):
        summary = summaries[quest]
        changes_requested = summary["request_changes"] + summary["rejected"]
        lines.append(
            f"Reviewed {summary['total_completed']} {quest} submissions, "
            f"{changes_requested} changes requested, "
            f"{summary['approved']} approved."
        )
    return "\n".join(lines)
