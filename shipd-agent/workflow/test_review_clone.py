# Tests for clone behavior when a stale submission directory exists.

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workflow.review import clone_submission_locally


SETUP_SCRIPT = """cat <<'EOSCRIPT' | bash
git clone https://example.com/repo.git stale-submission
EOSCRIPT"""


class CloneSubmissionTests(unittest.TestCase):
    def test_removes_stale_target_before_quick_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clone_dir = Path(tmp)
            stale = clone_dir / "stale-submission"
            stale.mkdir()
            (stale / "old.txt").write_text("leftover", encoding="utf-8")

            def fake_quick_setup(*args, **kwargs):  # type: ignore[no-untyped-def]
                (clone_dir / "stale-submission").mkdir()
                return None

            with patch("workflow.review.subprocess.run", side_effect=fake_quick_setup):
                cloned = clone_submission_locally(
                    SETUP_SCRIPT,
                    clone_dir=clone_dir,
                )

            self.assertEqual(cloned, stale)
            self.assertTrue(stale.is_dir())
            self.assertFalse((stale / "old.txt").exists())


if __name__ == "__main__":
    unittest.main()
