# Tests for post-review cleanup helpers.

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from workflow.cleanup import (
    REVIEW_IMAGE_PREFIX,
    DockerSnapshot,
    cleanup_after_review_enabled,
    cleanup_review_docker_resources,
    cleanup_submission_artifacts,
    docker_snapshot_from_dict,
    docker_snapshot_to_dict,
    main,
    remove_clone_directory,
    run_cleanup_from_session_meta,
    snapshot_docker_state,
)


class CleanupConfigTests(unittest.TestCase):
    def test_cleanup_enabled_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(cleanup_after_review_enabled())

    def test_cleanup_disabled_via_env(self) -> None:
        with patch.dict(os.environ, {"CLEANUP_AFTER_REVIEW": "0"}, clear=True):
            self.assertFalse(cleanup_after_review_enabled())


class CleanupArtifactTests(unittest.TestCase):
    def test_removes_clone_directory(self) -> None:
        clone_dir = Path(self._tmpdir()) / "submission-repo"
        clone_dir.mkdir(parents=True)
        (clone_dir / "README.md").write_text("test", encoding="utf-8")

        self.assertTrue(remove_clone_directory(clone_dir))
        self.assertFalse(clone_dir.exists())

    def test_cleanup_uses_remove_clone_directory(self) -> None:
        clone_dir = Path(self._tmpdir()) / "submission-repo"
        clone_dir.mkdir(parents=True)

        cleanup_submission_artifacts(clone_dir)

        self.assertFalse(clone_dir.exists())

    def test_removes_new_docker_resources(self) -> None:
        clone_dir = Path(self._tmpdir()) / "missing-clone"
        before = DockerSnapshot(
            image_ids=frozenset({"img-old"}),
            container_ids=frozenset({"ctr-old"}),
        )
        calls: list[list[str]] = []

        def fake_run(args: list[str], **kwargs):  # type: ignore[no-untyped-def]
            calls.append(args)
            if args[:3] == ["docker", "ps", "-aq"]:
                if len(args) > 3 and args[3] == "--filter":
                    return subprocess.CompletedProcess(args, 0, "", "")
                return subprocess.CompletedProcess(args, 0, "ctr-old\nctr-new\n", "")
            if args[:3] == ["docker", "images", "-q"]:
                if len(args) > 3 and args[3] == "--filter":
                    return subprocess.CompletedProcess(
                        args, 0, "img-review-a\nimg-review-b\n", ""
                    )
                return subprocess.CompletedProcess(args, 0, "img-old\nimg-new\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch("workflow.cleanup.subprocess.run", side_effect=fake_run):
            cleanup_submission_artifacts(clone_dir, docker_state_before=before)

        self.assertIn(["docker", "rm", "-f", "ctr-new"], calls)
        self.assertIn(["docker", "rmi", "-f", "img-new"], calls)
        self.assertIn(["docker", "rmi", "-f", "img-review-a"], calls)
        self.assertIn(["docker", "rmi", "-f", "img-review-b"], calls)
        self.assertNotIn(["docker", "rm", "-f", "ctr-old"], calls)
        self.assertNotIn(["docker", "rmi", "-f", "img-old"], calls)

    def test_removes_review_images_without_snapshot(self) -> None:
        calls: list[list[str]] = []

        def fake_run(args: list[str], **kwargs):  # type: ignore[no-untyped-def]
            calls.append(args)
            if args[:3] == ["docker", "images", "-q"]:
                return subprocess.CompletedProcess(args, 0, "img-review\n", "")
            if args[:3] == ["docker", "ps", "-aq"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch("workflow.cleanup.subprocess.run", side_effect=fake_run):
            cleanup_review_docker_resources()

        self.assertIn(
            ["docker", "images", "-q", "--filter", f"reference={REVIEW_IMAGE_PREFIX}*"],
            calls,
        )
        self.assertIn(["docker", "rmi", "-f", "img-review"], calls)

    def test_cleanup_without_clone_still_removes_docker(self) -> None:
        calls: list[list[str]] = []

        def fake_run(args: list[str], **kwargs):  # type: ignore[no-untyped-def]
            calls.append(args)
            if args[:3] == ["docker", "images", "-q"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[:3] == ["docker", "ps", "-aq"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch("workflow.cleanup.subprocess.run", side_effect=fake_run):
            cleanup_submission_artifacts(None)

        self.assertTrue(
            any(
                args[:3] == ["docker", "images", "-q"]
                and args[3:5] == ["--filter", f"reference={REVIEW_IMAGE_PREFIX}*"]
                for args in calls
            )
        )

    def test_snapshot_docker_state(self) -> None:
        def fake_run(args: list[str], **kwargs):  # type: ignore[no-untyped-def]
            if args[:3] == ["docker", "images", "-q"]:
                return subprocess.CompletedProcess(args, 0, "img-a\n", "")
            if args[:3] == ["docker", "ps", "-aq"]:
                return subprocess.CompletedProcess(args, 0, "ctr-a\n", "")
            raise AssertionError(f"unexpected docker call: {args}")

        with patch("workflow.cleanup.subprocess.run", side_effect=fake_run):
            snapshot = snapshot_docker_state()

        self.assertEqual(snapshot.image_ids, frozenset({"img-a"}))
        self.assertEqual(snapshot.container_ids, frozenset({"ctr-a"}))

    def test_docker_snapshot_roundtrip(self) -> None:
        snapshot = DockerSnapshot(
            image_ids=frozenset({"img-a", "img-b"}),
            container_ids=frozenset({"ctr-a"}),
        )
        restored = docker_snapshot_from_dict(docker_snapshot_to_dict(snapshot))
        self.assertEqual(restored, snapshot)

    def test_removes_containers_for_review_images(self) -> None:
        calls: list[list[str]] = []

        def fake_run(args: list[str], **kwargs):  # type: ignore[no-untyped-def]
            calls.append(args)
            if args[:3] == ["docker", "images", "-q"]:
                if len(args) > 3 and args[3] == "--filter":
                    return subprocess.CompletedProcess(args, 0, "img-review\n", "")
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[:3] == ["docker", "ps", "-aq"]:
                if len(args) > 3 and args[3] == "--filter":
                    return subprocess.CompletedProcess(args, 0, "ctr-review\n", "")
                return subprocess.CompletedProcess(args, 0, "", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch("workflow.cleanup.subprocess.run", side_effect=fake_run):
            cleanup_review_docker_resources()

        self.assertIn(["docker", "rm", "-f", "ctr-review"], calls)

    def test_run_cleanup_from_session_meta(self) -> None:
        import json
        import tempfile

        clone_dir = Path(tempfile.mkdtemp()) / "submission-repo"
        clone_dir.mkdir(parents=True)
        meta_dir = Path(tempfile.mkdtemp())
        meta_path = meta_dir / "session-meta.json"
        meta_path.write_text(
            json.dumps(
                {
                    "review_url": "https://shipd.example/challenges/1",
                    "quest": "olympus",
                    "repo_path": str(clone_dir),
                    "docker_snapshot_before": docker_snapshot_to_dict(
                        DockerSnapshot(
                            image_ids=frozenset({"img-old"}),
                            container_ids=frozenset({"ctr-old"}),
                        )
                    ),
                }
            ),
            encoding="utf-8",
        )

        def fake_run(args: list[str], **kwargs):  # type: ignore[no-untyped-def]
            if args[:3] == ["docker", "images", "-q"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[:3] == ["docker", "ps", "-aq"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch("workflow.cleanup.subprocess.run", side_effect=fake_run):
            run_cleanup_from_session_meta(session_meta_path=meta_path)

        self.assertFalse(clone_dir.exists())

    def test_main_skips_when_cleanup_disabled(self) -> None:
        with patch.dict(os.environ, {"CLEANUP_AFTER_REVIEW": "0"}, clear=True):
            with patch("workflow.cleanup.cleanup_submission_artifacts") as mocked:
                with patch("sys.argv", ["cleanup.py", "--path", "/tmp/x"]):
                    self.assertEqual(main(), 0)
                mocked.assert_not_called()

    def _tmpdir(self) -> str:
        import tempfile

        return tempfile.mkdtemp()


if __name__ == "__main__":
    unittest.main()
