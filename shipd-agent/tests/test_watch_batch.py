# Tests for watch-mode batch resume state.

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from stats import watch_batch


class WatchBatchTests(unittest.TestCase):
    def test_start_and_record_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watch-batch.json"
            batch = watch_batch.start_batch(
                max_runs=3,
                quest="olympus",
                interval_sec=60,
                options={"review": True, "submit": True},
                path=path,
            )
            self.assertEqual(batch["completed_runs"], 0)
            self.assertEqual(batch["max_runs"], 3)

            watch_batch.record_run_complete("done", path=path)
            active = watch_batch.get_active_batch(path)
            assert active is not None
            self.assertEqual(active["completed_runs"], 1)
            self.assertEqual(watch_batch.next_run_number(active), 2)

            watch_batch.record_run_complete("fail", review_url="https://shipd.ai/x", path=path)
            active = watch_batch.get_active_batch(path)
            assert active is not None
            self.assertEqual(active["last_failed_review_url"], "https://shipd.ai/x")

            watch_batch.record_run_complete("done", path=path)
            self.assertIsNone(watch_batch.get_active_batch(path))
            self.assertFalse(path.is_file())

    def test_load_invalid_batch_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watch-batch.json"
            path.write_text(json.dumps({"version": 99}), encoding="utf-8")
            self.assertIsNone(watch_batch.load_batch(path))

    def test_options_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watch-batch.json"
            batch = watch_batch.start_batch(
                max_runs=2,
                quest="mars",
                interval_sec=120,
                options={"review": True, "clone": True, "separate_steps": False},
                path=path,
            )
            self.assertTrue(
                watch_batch.options_compatible(
                    batch,
                    quest="mars",
                    interval_sec=120,
                    options={"review": True, "clone": True, "separate_steps": False},
                )
            )
            self.assertFalse(
                watch_batch.options_compatible(
                    batch,
                    quest="olympus",
                    interval_sec=120,
                    options={"review": True, "clone": True, "separate_steps": False},
                )
            )

    def test_load_legacy_options_backfills_cooldown_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watch-batch.json"
            path.write_text(
                json.dumps(
                    {
                        "version": watch_batch.VERSION,
                        "max_runs": 80,
                        "completed_runs": 6,
                        "quest": "olympus",
                        "interval_sec": 0,
                        "options": {
                            "review": True,
                            "submit": True,
                            "clone": True,
                            "cleanup": None,
                            "separate_steps": False,
                        },
                    }
                ),
                encoding="utf-8",
            )
            batch = watch_batch.load_batch(path)
            assert batch is not None
            self.assertTrue(
                watch_batch.options_compatible(
                    batch,
                    quest="olympus",
                    interval_sec=0,
                    options={
                        "review": True,
                        "submit": True,
                        "clone": True,
                        "cleanup": None,
                        "separate_steps": False,
                        "cooldown_every": 5,
                        "cooldown_sec": 3600,
                    },
                )
            )


if __name__ == "__main__":
    unittest.main()
