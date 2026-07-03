# Tests for post-review cleanup helpers.

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from workflow.cleanup import (
    DockerSnapshot,
    cleanup_after_review_enabled,
    cleanup_submission_artifacts,
    remove_clone_directory,
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
                return subprocess.CompletedProcess(args, 0, "ctr-old\nctr-new\n", "")
            if args[:3] == ["docker", "images", "-q"]:
                return subprocess.CompletedProcess(args, 0, "img-old\nimg-new\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch("workflow.cleanup.subprocess.run", side_effect=fake_run):
            cleanup_submission_artifacts(clone_dir, docker_state_before=before)

        self.assertIn(["docker", "rm", "-f", "ctr-new"], calls)
        self.assertIn(["docker", "rmi", "-f", "img-new"], calls)
        self.assertNotIn(["docker", "rm", "-f", "ctr-old"], calls)
        self.assertNotIn(["docker", "rmi", "-f", "img-old"], calls)

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

    def _tmpdir(self) -> str:
        import tempfile

        return tempfile.mkdtemp()


if __name__ == "__main__":
    unittest.main()
