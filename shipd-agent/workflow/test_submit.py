# Unit tests for submit form locators (no browser required).

from __future__ import annotations

import re
import unittest
from unittest.mock import MagicMock

from workflow.submit import _band_section, _pick_tightest_band_section, _section_has_score_buttons


def _button_locator(*, count: int = 0) -> MagicMock:
    loc = MagicMock()
    loc.count.return_value = count
    return loc


def _section_with_buttons(button_counts: dict[str | re.Pattern[str], int]) -> MagicMock:
    """Mock a section whose get_by_role('button', name=...) returns given counts."""

    def get_by_role(role, name=None, **kwargs):  # noqa: ANN001, ARG001
        if role != "button":
            return _button_locator(count=0)
        if isinstance(name, re.Pattern):
            for key, cnt in button_counts.items():
                if isinstance(key, re.Pattern) and name.pattern == key.pattern:
                    return _button_locator(count=cnt)
            return _button_locator(count=0)
        return _button_locator(count=button_counts.get(name, 0))

    section = MagicMock()
    section.get_by_role.side_effect = get_by_role
    return section


class SectionHasScoreButtonsTests(unittest.TestCase):
    def test_detects_label_button(self) -> None:
        section = _section_with_buttons({"Failing": 1})
        self.assertTrue(_section_has_score_buttons(section))

    def test_detects_numeric_prefix_button(self) -> None:
        section = _section_with_buttons({re.compile(r"^2\b"): 1})
        self.assertTrue(_section_has_score_buttons(section))

    def test_rejects_confidence_only_section(self) -> None:
        section = _section_with_buttons({"Low": 1, "Med": 1, "High": 1})
        self.assertFalse(_section_has_score_buttons(section))


class BandSectionTests(unittest.TestCase):
    def test_skips_shallow_ancestor_without_score_buttons(self) -> None:
        page = MagicMock()
        heading = MagicMock()
        heading.count.return_value = 1
        heading.first = heading
        heading.wait_for = MagicMock()
        heading.scroll_into_view_if_needed = MagicMock()

        shallow = _section_with_buttons({"Low": 1, "Med": 1, "High": 1})
        deep = _section_with_buttons(
            {
                "Low": 1,
                "Med": 1,
                "High": 1,
                "Minor": 1,
                re.compile(r"^2\b"): 1,
            }
        )

        ancestors = MagicMock()
        ancestors.count.return_value = 2
        ancestors.nth.side_effect = lambda i: [shallow, deep][i]
        heading.locator.return_value = ancestors

        page.get_by_role.return_value = heading

        section = _band_section(page, "Problem Description")
        self.assertIs(section, deep)
        ancestors.nth.assert_any_call(1)

    def test_prefers_smallest_score_button_container(self) -> None:
        wide = _section_with_buttons(
            {
                "Low": 1,
                "Med": 1,
                "High": 1,
                "Minor": 1,
                re.compile(r"^2\b"): 1,
            }
        )
        tight = _section_with_buttons(
            {
                "Low": 1,
                "Med": 1,
                "High": 1,
                "Minor": 1,
                re.compile(r"^2\b"): 1,
            }
        )

        def count_buttons(section: MagicMock, total: int) -> None:
            all_buttons = _button_locator(count=total)

            def get_by_role(role, name=None, **kwargs):  # noqa: ANN001, ARG001
                if role == "button" and name is None:
                    return all_buttons
                if role != "button":
                    return _button_locator(count=0)
                if isinstance(name, re.Pattern):
                    for key, cnt in section._button_counts.items():  # type: ignore[attr-defined]
                        if isinstance(key, re.Pattern) and name.pattern == key.pattern:
                            return _button_locator(count=cnt)
                    return _button_locator(count=0)
                return _button_locator(count=section._button_counts.get(name, 0))  # type: ignore[attr-defined]

            section._button_counts = {  # type: ignore[attr-defined]
                "Low": 1,
                "Med": 1,
                "High": 1,
                "Minor": 1,
                re.compile(r"^2\b"): 1,
            }
            section.get_by_role.side_effect = get_by_role

        count_buttons(wide, 134)
        count_buttons(tight, 7)
        chosen = _pick_tightest_band_section([wide, tight])
        self.assertIs(chosen, tight)

    def test_raises_when_no_score_buttons_found(self) -> None:
        page = MagicMock()
        heading = MagicMock()
        heading.count.return_value = 1
        heading.first = heading
        heading.wait_for = MagicMock()
        heading.scroll_into_view_if_needed = MagicMock()

        shallow = _section_with_buttons({"Low": 1})

        ancestors = MagicMock()
        ancestors.count.return_value = 1
        ancestors.nth.return_value = shallow
        heading.locator.return_value = ancestors

        fallback = MagicMock()
        fallback.count.return_value = 0
        filtered = MagicMock()
        filtered.count.return_value = 0
        fallback.filter.return_value = filtered

        page.get_by_role.return_value = heading
        page.locator.return_value = fallback

        with self.assertRaisesRegex(RuntimeError, "score buttons"):
            _band_section(page, "Problem Description")


if __name__ == "__main__":
    unittest.main()
