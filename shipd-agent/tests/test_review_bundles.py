# Tests for review JSON bundle save/load.

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from review.review_bundles import load_review_bundle, save_review_bundle


SAMPLE_REVIEW = {
    "decision": "request_changes",
    "band_ratings": {
        "problem": {"score": 2, "confidence": "medium", "reasoning": "unclear"},
        "tests": {"score": 3, "confidence": "high", "reasoning": ""},
        "solution": {"score": 2, "confidence": "low", "reasoning": "issues"},
    },
    "recommendation_summary": "Needs work",
    "contributor_feedback": "Please clarify requirements.",
}


class ReviewBundleTests(unittest.TestCase):
    def test_round_trip_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bundle.json"
            save_review_bundle(
                SAMPLE_REVIEW,
                review_url="https://shipd.ai/quests/olympus/challenges/abc?mode=review",
                quest="olympus",
                repo_path="/tmp/submission",
                path=path,
            )
            review, url, quest, repo = load_review_bundle(path)
            self.assertEqual(review["decision"], "request_changes")
            self.assertIn("/challenges/", url)
            self.assertEqual(quest, "olympus")
            self.assertEqual(repo, "/tmp/submission")

    def test_load_flat_agent_json_with_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "flat.json"
            payload = {
                "review_url": "https://shipd.ai/quests/mars/challenges/x?mode=review",
                "quest": "mars",
                **SAMPLE_REVIEW,
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            review, url, quest, _repo = load_review_bundle(path)
            self.assertEqual(review["decision"], "request_changes")
            self.assertEqual(quest, "mars")
            self.assertIn("mars", url)


if __name__ == "__main__":
    unittest.main()
