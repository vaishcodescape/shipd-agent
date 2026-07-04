# Tests for review model configuration.

from __future__ import annotations

import unittest
from unittest.mock import patch

from review.config import (
    DEFAULT_EXPLORE_MODEL,
    DEFAULT_REVIEW_MODEL,
    resolve_model_id,
)


class ResolveModelIdTests(unittest.TestCase):
    def test_passthrough_current_model(self) -> None:
        self.assertEqual(
            resolve_model_id("claude-opus-4-8", default=DEFAULT_REVIEW_MODEL),
            "claude-opus-4-8",
        )

    def test_remaps_retired_opus_snapshot(self) -> None:
        with patch("builtins.print"):
            resolved = resolve_model_id(
                "claude-opus-4-20250514",
                default=DEFAULT_REVIEW_MODEL,
            )
        self.assertEqual(resolved, "claude-opus-4-8")

    def test_empty_uses_default(self) -> None:
        self.assertEqual(
            resolve_model_id("", default=DEFAULT_EXPLORE_MODEL),
            DEFAULT_EXPLORE_MODEL,
        )


if __name__ == "__main__":
    unittest.main()
