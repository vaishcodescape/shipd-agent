# Remove cloned submissions and Docker artifacts after review.

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


REVIEW_IMAGE_PREFIX = "shipd-review-"


@dataclass(frozen=True)
class DockerSnapshot:
    image_ids: frozenset[str]
    container_ids: frozenset[str]


def docker_snapshot_to_dict(snapshot: DockerSnapshot) -> dict[str, list[str]]:
    return {
        "image_ids": sorted(snapshot.image_ids),
        "container_ids": sorted(snapshot.container_ids),
    }


def docker_snapshot_from_dict(data: dict[str, Any] | None) -> DockerSnapshot | None:
    if not data:
        return None
    return DockerSnapshot(
        image_ids=frozenset(str(item) for item in data.get("image_ids", [])),
        container_ids=frozenset(
            str(item) for item in data.get("container_ids", [])
        ),
    )


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


def _review_image_ids() -> set[str]:
    """Image IDs for Phase 0 review builds (tagged shipd-review-*)."""
    return _docker_ids(
        ["images", "-q", "--filter", f"reference={REVIEW_IMAGE_PREFIX}*"]
    )


def _containers_for_images(image_ids: set[str]) -> set[str]:
    containers: set[str] = set()
    for image_id in image_ids:
        containers |= _docker_ids(["ps", "-aq", "--filter", f"ancestor={image_id}"])
    return containers


def cleanup_review_docker_resources(
    *,
    docker_state_before: DockerSnapshot | None = None,
    log: Callable[[str], None] | None = None,
) -> None:
    """Remove review Docker images/containers (including leftovers from failed runs)."""
    emit = log or print

    review_images = _review_image_ids()
    containers_to_remove = _containers_for_images(review_images)

    if docker_state_before is not None:
        before_containers = set(docker_state_before.container_ids)
        before_images = set(docker_state_before.image_ids)
        containers_to_remove |= _docker_ids(["ps", "-aq"]) - before_containers
        review_images |= _docker_ids(["images", "-q"]) - before_images

    _remove_docker_ids(
        containers_to_remove,
        command=["docker", "rm", "-f"],
        label="container",
        log=emit,
    )
    _remove_docker_ids(
        review_images,
        command=["docker", "rmi", "-f"],
        label="image",
        log=emit,
    )


def cleanup_submission_artifacts(
    cloned_path: Path | None,
    *,
    docker_state_before: DockerSnapshot | None = None,
    log: Callable[[str], None] | None = None,
) -> None:
    """Delete the cloned repo and Docker resources from this review run."""
    emit = log or print

    if cloned_path is not None:
        remove_clone_directory(cloned_path, log=emit)

    cleanup_review_docker_resources(
        docker_state_before=docker_state_before,
        log=emit,
    )


def run_cleanup_from_session_meta(
    *,
    session_meta_path: Path | None = None,
    log: Callable[[str], None] | None = None,
) -> None:
    """Load clone path and pre-review Docker snapshot from session meta."""
    from review.review_io import SESSION_META_PATH, load_session_meta

    meta = load_session_meta(session_meta_path or SESSION_META_PATH)
    repo_path_raw = str(meta.get("repo_path", "")).strip()
    cloned_path = Path(repo_path_raw) if repo_path_raw else None
    docker_state_before = docker_snapshot_from_dict(
        meta.get("docker_snapshot_before")
    )
    cleanup_submission_artifacts(
        cloned_path,
        docker_state_before=docker_state_before,
        log=log,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Remove the cloned submission directory and Docker artifacts "
            "created during a review run."
        ),
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=None,
        help="Cloned submission directory to remove.",
    )
    parser.add_argument(
        "--from-session-meta",
        action="store_true",
        help=(
            "Load repo_path and docker_snapshot_before from "
            "reviews/session-meta.json."
        ),
    )
    parser.add_argument(
        "--session-meta",
        type=Path,
        default=None,
        help="Alternate session-meta.json path (with --from-session-meta).",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Skip cleanup (overrides CLEANUP_AFTER_REVIEW).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.no_cleanup or not cleanup_after_review_enabled():
        print("Post-review cleanup disabled; skipping.")
        return 0

    if args.from_session_meta:
        try:
            run_cleanup_from_session_meta(session_meta_path=args.session_meta)
        except (FileNotFoundError, ValueError) as exc:
            print(f"WARNING: Cleanup skipped: {exc}", file=sys.stderr)
            return 1
        return 0

    cleanup_submission_artifacts(args.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
