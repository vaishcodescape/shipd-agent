# Tests for session stats persistence.

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stats import session_stats


class SessionStatsTests(unittest.TestCase):
    def test_record_decision_persists_across_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session-stats.json"
            with patch.object(session_stats, "STATS_PATH", path):
                session_stats.reset_session()
                session_stats.record_decision(
                    "approve",
                    repo_path="/tmp/repo",
                    review_url="https://shipd.ai/x",
                    quest="olympus",
                )
                summary = session_stats.get_summary()
                self.assertEqual(summary["approved"], 1)
                self.assertEqual(summary["total_completed"], 1)

                reloaded = session_stats.get_summary()
                self.assertEqual(reloaded["approved"], 1)


if __name__ == "__main__":
    unittest.main()
