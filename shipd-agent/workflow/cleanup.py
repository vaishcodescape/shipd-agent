# Remove cloned submissions and Docker artifacts after review.

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class DockerSnapshot:
    image_ids: frozenset[str]
    container_ids: frozenset[str]


def cleanup_after_review_enabled() -> bool:
    """True when post-review cleanup is enabled (default: on)."""
    return os.getenv("CLEANUP_AFTER_REVIEW", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _docker_ids(args: list[str]) -> set[str]:
    try:
        result = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set()
    if result.returncode != 0:
        return set()
    lines = result.stdout.splitlines()
    return {line.strip() for line in lines if line.strip()}


def snapshot_docker_state() -> DockerSnapshot:
    """Capture Docker image and container IDs before Quick Setup runs."""
    return DockerSnapshot(
        image_ids=frozenset(_docker_ids(["images", "-q"])),
        container_ids=frozenset(_docker_ids(["ps", "-aq"])),
    )


def _remove_docker_ids(
    ids: set[str],
    *,
    command: list[str],
    label: str,
    log: Callable[[str], None],
) -> None:
    if not ids:
        return
    for item_id in sorted(ids):
        result = subprocess.run(
            [*command, item_id],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if result.returncode == 0:
            log(f"Removed Docker {label}: {item_id}")
        else:
            detail = (
                result.stderr or result.stdout or "unknown error"
            ).strip()
            log(
                f"WARNING: Could not remove Docker {label} "
                f"{item_id}: {detail}"
            )


def remove_clone_directory(
    cloned_path: Path,
    *,
    log: Callable[[str], None] | None = None,
) -> bool:
    """Remove a cloned submission directory if present."""
    emit = log or print
    if cloned_path.is_dir():
        shutil.rmtree(cloned_path)
        emit(f"Removed cloned submission: {cloned_path}")
        return True
    if cloned_path.exists():
        emit(
            "WARNING: Clone path is not a directory, "
            f"skipping removal: {cloned_path}"
        )
    return False


def cleanup_submission_artifacts(
    cloned_path: Path,
    *,
    docker_state_before: DockerSnapshot | None = None,
    log: Callable[[str], None] | None = None,
) -> None:
    """Delete the cloned repo and Docker resources created since snapshot."""
    emit = log or print

    remove_clone_directory(cloned_path, log=emit)

    if docker_state_before is None:
        return

    before_containers = set(docker_state_before.container_ids)
    before_images = set(docker_state_before.image_ids)
    new_containers = _docker_ids(["ps", "-aq"]) - before_containers
    new_images = _docker_ids(["images", "-q"]) - before_images

    _remove_docker_ids(
        new_containers,
        command=["docker", "rm", "-f"],
        label="container",
        log=emit,
    )
    _remove_docker_ids(
        new_images,
        command=["docker", "rmi", "-f"],
        label="image",
        log=emit,
    )
