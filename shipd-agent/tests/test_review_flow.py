# Unit tests for review deck locators (no browser required).

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, PropertyMock, patch

from workflow.review import (
    REVIEW_ENTRY_NAMES,
    find_review_entry_control,
    resolve_clone_directory,
)


class ReviewEntryControlTests(unittest.TestCase):
    def test_entry_names_exclude_bare_review(self) -> None:
        self.assertNotIn("Review", REVIEW_ENTRY_NAMES)
        self.assertNotIn("Continue", REVIEW_ENTRY_NAMES)

    def test_prefers_exact_continue_over_review_queue_tab(self) -> None:
        page = MagicMock()
        continue_btn = MagicMock()
        continue_btn.is_visible.return_value = True
        continue_btn.is_enabled.return_value = True

        continue_locator = MagicMock()
        continue_locator.count.return_value = 1
        continue_locator.nth.return_value = continue_btn

        def get_by_role(role, name, exact=False):  # noqa: ANN001
            if role == "button" and name == "Continue →" and exact:
                return continue_locator
            return MagicMock(count=MagicMock(return_value=0))

        page.get_by_role.side_effect = get_by_role

        control = find_review_entry_control(page)
        self.assertIs(control, continue_btn)
        page.get_by_role.assert_any_call("button", name="Continue →", exact=True)

    def test_does_not_fall_back_to_review_queue_tab(self) -> None:
        page = MagicMock()
        no_continue = MagicMock(count=MagicMock(return_value=0))

        review_queue_tab = MagicMock()
        review_queue_tab.is_visible.return_value = True
        review_queue_tab.is_enabled.return_value = True
        review_queue_locator = MagicMock()
        review_queue_locator.count.return_value = 1
        review_queue_locator.nth.return_value = review_queue_tab

        def get_by_role(role, name, exact=False):  # noqa: ANN001
            if role == "button" and name == "Continue →" and exact:
                return no_continue
            if role == "button" and name == "Review" and not exact:
                return review_queue_locator
            return MagicMock(count=MagicMock(return_value=0))

        page.get_by_role.side_effect = get_by_role

        control = find_review_entry_control(page)
        self.assertIsNone(control)


class ResolveCloneDirectoryTests(unittest.TestCase):
    def test_git_clone_pattern(self) -> None:
        script = "git clone https://example.com/r.git my-repo"
        self.assertEqual(resolve_clone_directory(script), "my-repo")


if __name__ == "__main__":
    unittest.main()
