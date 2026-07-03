# Live activity log for the review agent — shows what the agent is doing.

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from pathlib import Path

_lock = threading.Lock()
_log_file: Path | None = None

_env_log = os.getenv("LOG_FILE", "").strip()
if _env_log:
    _log_file = Path(_env_log)


def set_activity_log_file(path: Path | str | None) -> None:
    """Mirror activity lines into the orchestrator run log."""
    global _log_file
    _log_file = Path(path) if path else None


def log_activity(message: str, *, category: str = "agent") -> None:
    """Print a timestamped one-line activity update (and append to run log)."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{timestamp}] [{category}] {message}"
    with _lock:
        print(line, flush=True)
        if _log_file is not None:
            try:
                _log_file.parent.mkdir(parents=True, exist_ok=True)
                with _log_file.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass


class timed_step:
    """Context manager logging start/finish (with duration) of a step."""

    def __init__(self, label: str, *, category: str = "agent") -> None:
        self.label = label
        self.category = category
        self._start = 0.0

    def __enter__(self) -> "timed_step":
        self._start = time.monotonic()
        log_activity(f"{self.label}…", category=self.category)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        elapsed = time.monotonic() - self._start
        if exc is not None:
            log_activity(
                f"{self.label} FAILED after {elapsed:.1f}s: {exc}",
                category=self.category,
            )
        else:
            log_activity(
                f"{self.label} done ({elapsed:.1f}s)", category=self.category
            )
