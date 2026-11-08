# Unit tests for effective LOC analysis (no browser required).

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from review.downgrade import apply_downgrade_logic, evaluate_loc_phase4
from review.loc import compute_effective_loc, count_substantive_lines_from_patch
from review.rubric_defaults import (
    MARS_MAX_EFFECTIVE_LOC,
    MARS_MIN_EFFECTIVE_LOC,
    OLYMPUS_MIN_EFFECTIVE_LOC,
)
from review.schemas import ReviewResult, BandRatings, BandRating


SAMPLE_PATCH = """\
diff --git a/src/foo.py b/src/foo.py
index 1111111..2222222 100644
--- a/src/foo.py
+++ b/src/foo.py
@@ -1,5 +1,12 @@
 import os
+# comment line should not count
+
 def foo():
-    return 1
+    # inline comment stripped
+    x = 2
+    return x
+
+# another comment
""".strip()


class EffectiveLocTests(unittest.TestCase):
    def test_counts_substantive_added_lines_only(self) -> None:
        total, files, per_file = count_substantive_lines_from_patch(SAMPLE_PATCH)
        self.assertEqual(total, 2)
        self.assertEqual(files, ["src/foo.py"])
        self.assertEqual(per_file["src/foo.py"], 2)

    def test_compute_from_repo_patch_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            patch_path = repo / "solution.patch"
            patch_path.write_text(SAMPLE_PATCH, encoding="utf-8")
            info = compute_effective_loc(repo, solution_patch_path="solution.patch")
            self.assertEqual(info["effective_loc"], 2)
            self.assertEqual(info["method"], "solution.patch")
            self.assertIn("src/foo.py", info["files_analyzed"])

    def test_missing_patch_returns_none_method(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            info = compute_effective_loc(Path(tmp))
            self.assertEqual(info["method"], "none")
            self.assertEqual(info["effective_loc"], 0)

    def test_olympus_long_horizon_passes(self) -> None:
        loc_info = {
            "method": "solution.patch",
            "effective_loc": 450,
            "files_analyzed": ["a.py"],
        }
        phase, findings, _ = evaluate_loc_phase4(
            loc_info,
            quest="olympus",
            olympus_min_loc=OLYMPUS_MIN_EFFECTIVE_LOC,
            mars_min_loc=MARS_MIN_EFFECTIVE_LOC,
            mars_max_loc=MARS_MAX_EFFECTIVE_LOC,
        )
        self.assertEqual(phase.status, "PASS")
        self.assertEqual(findings, [])

    def test_olympus_below_old_floor_still_passes(self) -> None:
        loc_info = {
            "method": "solution.patch",
            "effective_loc": 200,
            "files_analyzed": ["a.py"],
        }
        phase, findings, summary = evaluate_loc_phase4(
            loc_info,
            quest="olympus",
            olympus_min_loc=OLYMPUS_MIN_EFFECTIVE_LOC,
            mars_min_loc=MARS_MIN_EFFECTIVE_LOC,
            mars_max_loc=MARS_MAX_EFFECTIVE_LOC,
        )
        self.assertEqual(phase.status, "PASS")
        self.assertEqual(findings, [])
        self.assertIn("no solution-LOC floor", summary)

    def test_apply_downgrade_does_not_set_flag_from_loc(self) -> None:
        loc_info = {
            "method": "solution.patch",
            "effective_loc": 200,
            "files_analyzed": ["a.py"],
        }
        review = ReviewResult(
            decision="approve",
            band_ratings=BandRatings(
                problem=BandRating(score=3, confidence="high"),
                tests=BandRating(score=3, confidence="high"),
                solution=BandRating(score=3, confidence="high"),
            ),
            recommendation_summary="Looks good",
            contributor_feedback="Nice work",
        )
        updated = apply_downgrade_logic(
            review,
            loc_info,
            quest="olympus",
            olympus_min_loc=OLYMPUS_MIN_EFFECTIVE_LOC,
            mars_min_loc=MARS_MIN_EFFECTIVE_LOC,
            mars_max_loc=MARS_MAX_EFFECTIVE_LOC,
        )
        self.assertFalse(updated.downgrade_to_mars)
        self.assertEqual(updated.decision, "approve")

    def test_apply_downgrade_clears_a_loc_only_flag(self) -> None:
        loc_info = {
            "method": "solution.patch",
            "effective_loc": 200,
            "files_analyzed": ["a.py"],
        }
        review = ReviewResult(
            decision="approve",
            band_ratings=BandRatings(
                problem=BandRating(score=3, confidence="high"),
                tests=BandRating(score=3, confidence="high"),
                solution=BandRating(score=3, confidence="high"),
            ),
            recommendation_summary="Looks good",
            contributor_feedback="Nice work",
            downgrade_to_mars=True,
            suggested_tags=["Lines of code"],
            other_notes="Effective LOC is below the Olympus long-horizon minimum.",
        )
        updated = apply_downgrade_logic(
            review,
            loc_info,
            quest="olympus",
            olympus_min_loc=OLYMPUS_MIN_EFFECTIVE_LOC,
            mars_min_loc=MARS_MIN_EFFECTIVE_LOC,
            mars_max_loc=MARS_MAX_EFFECTIVE_LOC,
        )
        self.assertFalse(updated.downgrade_to_mars)
        self.assertNotIn("Lines of code", updated.suggested_tags)
        self.assertEqual(updated.decision, "approve")


if __name__ == "__main__":
    unittest.main()
