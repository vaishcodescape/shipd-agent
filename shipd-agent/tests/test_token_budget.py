# Unit tests for token budget helpers.

from __future__ import annotations

import unittest

from review.token_budget import estimate_tokens, truncate_text


class TokenBudgetTests(unittest.TestCase):
    def test_estimate_tokens(self) -> None:
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens("abcd"), 1)
        self.assertEqual(estimate_tokens("a" * 400), 100)

    def test_truncate_text_keeps_short_strings(self) -> None:
        self.assertEqual(truncate_text("hello", 100), "hello")

    def test_truncate_text_preserves_head_and_tail(self) -> None:
        text = "A" * 100 + "MIDDLE" + "B" * 100
        out = truncate_text(text, 80, label="sample")
        self.assertIn("sample truncated", out)
        self.assertTrue(out.startswith("A"))
        self.assertTrue(out.endswith("B"))


if __name__ == "__main__":
    unittest.main()
