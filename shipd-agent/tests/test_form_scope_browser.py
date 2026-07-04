# Browser-level regression test for Shipd band-scope detection.
#
# The unit tests in test_submit.py mock ``page.evaluate`` and therefore never
# exercise the in-page JavaScript that scopes each rubric band. That is exactly
# how the "all three bands collapse onto one scope" bug shipped undetected:
# every band read back the first band's score/confidence, so Submit stayed
# disabled with a "Confidence" hint (see logs/debug-submit/*-submit-failed.json).
#
# This test loads the REAL ``_JS_FORM_VALIDATION_STATE`` against a synthetic DOM
# that reproduces the Shipd form structure (per-band wrapper holding a score row
# whose digit + label render as separate children, a confidence row, and a
# reason textarea). Before the fix it collapses; after the fix each band is read
# independently.

from __future__ import annotations

import unittest

from workflow.submit import (
    _JS_FORM_VALIDATION_STATE,
    _click_band_confidence,
    _click_band_score,
    _fill_band_reason,
    _form_state_eval_args,
    _read_form_state,
)

try:
    from playwright.sync_api import sync_playwright

    _PW_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - environment without playwright
    sync_playwright = None  # type: ignore[assignment]
    _PW_IMPORT_ERROR = exc


# Distinct reason texts so a collapsed scope (identical reasonLen everywhere)
# is detectable independently of the score/confidence readings.
PROBLEM_REASON = "Problem statement is specific but the harness does not build."
TESTS_REASON = "Test harness does not compile."
SOLUTION_REASON = (
    "Effective LOC meets the Olympus minimum; contingent on a green "
    "harness build in CI before the solution band can be trusted."
)
AUTHOR_NOTE = "Downgrade candidate — harness fails to build against base tree."


def _score_row(selected: int) -> str:
    labels = {0: "Failing", 1: "Weak", 2: "Minor", 3: "Clean"}
    cells = []
    for value, label in labels.items():
        cls = "cell bg-primary" if value == selected else "cell"
        # Digit and label render as separate children, mirroring the real form.
        cells.append(
            f'<button class="{cls}"><span class="num">{value}</span>'
            f'<span class="lbl">{label}</span></button>'
        )
    return '<div class="score-row">' + "".join(cells) + "</div>"


def _conf_row(selected: str) -> str:
    segments = []
    for label in ("Low", "Med", "High"):
        cls = "seg bg-primary" if label == selected else "seg"
        segments.append(f'<button class="{cls}">{label}</button>')
    return '<div class="conf-row">' + "".join(segments) + "</div>"


def _band(heading: str, score: int, conf: str, reason: str) -> str:
    return (
        '<div class="band">'
        f'<div class="heading">{heading}</div>'
        f"{_conf_row(conf)}"
        f"{_score_row(score)}"
        '<textarea placeholder="One line — what kept it below 3.">'
        f"{reason}</textarea>"
        "</div>"
    )


def _build_form_html() -> str:
    bands = (
        _band("Problem Description", 2, "Med", PROBLEM_REASON)
        + _band("Tests", 1, "High", TESTS_REASON)
        + _band("Solution & Code", 2, "Med", SOLUTION_REASON)
    )
    return f"""
    <html><head><style>
      body {{ width: 900px; font-family: sans-serif; }}
      button {{ display: inline-block; padding: 6px 10px; margin: 2px; }}
      .bg-primary {{ background: #2563eb; color: #fff; }}
      .heading {{ font-weight: 600; margin-top: 12px; }}
      textarea {{ display: block; width: 400px; height: 40px; }}
    </style></head>
    <body>
      <div class="bg-card text-card-foreground">
        <div class="p-6 pt-0 space-y-6">
          <div class="decision-row">
            <div class="decision"><button>Approve</button>
              <span>Meets quality standards</span></div>
            <div class="decision"><button class="bg-primary">Request Changes</button>
              <span>Needs changes before acceptance</span></div>
            <div class="decision"><button>Reject</button>
              <span>Does not meet requirements</span></div>
          </div>
          {bands}
          <div class="other-notes">
            <div>Other notes</div>
            <div class="note-label">Note — sent to the author</div>
            <textarea placeholder="Anything outside the rubric — difficulty, LOC, repo fit, AI slop…">{AUTHOR_NOTE}</textarea>
          </div>
          <button class="submit">Submit Review</button>
        </div>
      </div>
    </body></html>
    """


@unittest.skipIf(sync_playwright is None, f"playwright unavailable: {_PW_IMPORT_ERROR}")
class BandScopeBrowserTests(unittest.TestCase):
    """Each rubric band must resolve to its own DOM scope, not a shared one."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._pw = sync_playwright().start()
        try:
            cls._browser = cls._pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - browser binary missing
            cls._pw.stop()
            raise unittest.SkipTest(f"chromium unavailable: {exc}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._browser.close()
        cls._pw.stop()

    def _read_state(self) -> dict:
        page = self._browser.new_page(viewport={"width": 900, "height": 1200})
        try:
            page.set_content(_build_form_html())
            return page.evaluate(_JS_FORM_VALIDATION_STATE, _form_state_eval_args())
        finally:
            page.close()

    def test_each_band_reads_its_own_score_and_confidence(self) -> None:
        state = self._read_state()
        bands = {b["heading"]: b for b in state.get("bands") or []}
        self.assertEqual(set(bands), {"Problem Description", "Tests", "Solution & Code"})

        # The exact regression from the failed run: Tests must read 1 / high,
        # not Problem Description's 2 / medium.
        self.assertEqual(bands["Tests"]["score"], 1)
        self.assertEqual(bands["Tests"]["confidence"], "high")
        self.assertEqual(bands["Problem Description"]["score"], 2)
        self.assertEqual(bands["Problem Description"]["confidence"], "medium")
        self.assertEqual(bands["Solution & Code"]["score"], 2)
        self.assertEqual(bands["Solution & Code"]["confidence"], "medium")

    def test_bands_do_not_collapse_onto_one_reason(self) -> None:
        state = self._read_state()
        lens = {b["heading"]: b["reasonLen"] for b in state.get("bands") or []}
        # Collapse manifests as every band reporting the first band's reason
        # length. The three reasons have distinct lengths, so a healthy scope
        # yields three distinct values.
        self.assertEqual(
            len(set(lens.values())),
            3,
            f"bands collapsed onto one scope — reasonLens={lens}",
        )
        self.assertEqual(lens["Tests"], len(TESTS_REASON))
        self.assertEqual(lens["Problem Description"], len(PROBLEM_REASON))

    def test_decision_and_author_note_read_from_form(self) -> None:
        state = self._read_state()
        self.assertEqual(state.get("decision"), "request changes")
        self.assertGreaterEqual(state.get("authorNoteLen") or 0, 5)


# ---------------------------------------------------------------------------
# Interactive form: exercises the positional CLICK path end to end (locate by
# document-order position, click, read the selection back) on a real browser,
# including the exact "second band (Tests) never registers a score" regression.
# ---------------------------------------------------------------------------

def _interactive_score_row(band_index: int) -> str:
    labels = {0: "Failing", 1: "Weak", 2: "Minor", 3: "Clean"}
    cells = []
    for value, label in labels.items():
        # Digit and label stack as block children, so innerText is "2\nMinor"
        # (norm -> "2 minor"), mirroring the real Shipd cell.
        cells.append(
            f'<button data-val="{value}" class="cell">'
            f'<div class="num">{value}</div><div class="lbl">{label}</div></button>'
        )
    return f'<div class="score-row" data-band="{band_index}">' + "".join(cells) + "</div>"


def _interactive_conf_row() -> str:
    segs = [f'<button class="seg">{lbl}</button>' for lbl in ("Low", "Med", "High")]
    return '<div class="conf-row">' + "".join(segs) + "</div>"


def _interactive_band(heading: str, band_index: int) -> str:
    return (
        '<div class="band">'
        f'<div class="heading">{heading}</div>'
        f"{_interactive_conf_row()}"
        f"{_interactive_score_row(band_index)}"
        '<textarea class="reason" style="display:none" '
        'placeholder="One line — what kept it below 3."></textarea>'
        "</div>"
    )


# Radio-within-row selection; the reason textarea appears only when score < 3,
# matching the real form (that gating is why a mis-scoped band left Submit off).
_INTERACTIVE_SCRIPT = """
() => {
  document.querySelectorAll('.score-row').forEach((row) => {
    const band = row.closest('.band');
    const reason = band.querySelector('.reason');
    row.querySelectorAll('button').forEach((btn) => {
      btn.addEventListener('click', () => {
        const wasSel = btn.classList.contains('bg-primary');
        row.querySelectorAll('button').forEach((b) => b.classList.remove('bg-primary'));
        if (wasSel) { if (reason) reason.style.display = 'none'; return; }
        btn.classList.add('bg-primary');
        const val = parseInt(btn.getAttribute('data-val'), 10);
        if (reason) reason.style.display = val < 3 ? 'block' : 'none';
      });
    });
  });
  document.querySelectorAll('.conf-row').forEach((row) => {
    row.querySelectorAll('button').forEach((btn) => {
      btn.addEventListener('click', () => {
        const wasSel = btn.classList.contains('bg-primary');
        row.querySelectorAll('button').forEach((b) => b.classList.remove('bg-primary'));
        if (!wasSel) btn.classList.add('bg-primary');
      });
    });
  });
}
"""


def _build_interactive_form() -> str:
    bands = (
        _interactive_band("Problem Description", 0)
        + _interactive_band("Tests", 1)
        + _interactive_band("Solution & Code", 2)
    )
    return f"""
    <html><head><style>
      body {{ width: 900px; font-family: sans-serif; }}
      button {{ display: inline-block; padding: 6px 10px; margin: 2px; }}
      .num, .lbl {{ display: block; }}
      .bg-primary {{ background: #2563eb; color: #fff; }}
      .heading {{ font-weight: 600; margin-top: 12px; }}
      textarea {{ display: block; width: 400px; height: 40px; }}
    </style></head>
    <body>
      <div class="bg-card text-card-foreground">
        <div class="p-6 pt-0 space-y-6">
          <div class="decision-row">
            <div class="decision"><button>Approve</button>
              <span>Meets quality standards</span></div>
            <div class="decision"><button>Request Changes</button>
              <span>Needs changes before acceptance</span></div>
            <div class="decision"><button>Reject</button>
              <span>Does not meet requirements</span></div>
          </div>
          {bands}
          <button class="submit">Submit Review</button>
        </div>
      </div>
    </body></html>
    """


@unittest.skipIf(sync_playwright is None, f"playwright unavailable: {_PW_IMPORT_ERROR}")
class PositionalClickBrowserTests(unittest.TestCase):
    """Positional locate+click must land on the right band and read back true."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._pw = sync_playwright().start()
        try:
            cls._browser = cls._pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - browser binary missing
            cls._pw.stop()
            raise unittest.SkipTest(f"chromium unavailable: {exc}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._browser.close()
        cls._pw.stop()

    def _new_page(self):
        page = self._browser.new_page(viewport={"width": 900, "height": 1200})
        page.set_content(_build_interactive_form())
        page.evaluate(_INTERACTIVE_SCRIPT)
        return page

    def _bands(self, page) -> dict:
        return {b["heading"]: b for b in _read_form_state(page).get("bands") or []}

    def test_clicking_tests_band_registers_its_own_score(self) -> None:
        # The exact production failure: Problem Description fills, then the
        # Tests (second) band's score never registered -> Submit stayed off.
        page = self._new_page()
        try:
            _click_band_score(page, "Problem Description", 2)
            _click_band_score(page, "Tests", 1)
            bands = self._bands(page)
            self.assertEqual(bands["Problem Description"]["score"], 2)
            self.assertEqual(bands["Tests"]["score"], 1)
            self.assertIsNone(bands["Solution & Code"]["score"])
        finally:
            page.close()

    def test_all_bands_same_score_read_independently(self) -> None:
        # A shared/collapsed scope would let one click bleed across bands.
        page = self._new_page()
        try:
            for heading in ("Problem Description", "Tests", "Solution & Code"):
                _click_band_score(page, heading, 2)
            bands = self._bands(page)
            for heading in ("Problem Description", "Tests", "Solution & Code"):
                self.assertEqual(bands[heading]["score"], 2, heading)
        finally:
            page.close()

    def test_confidence_lands_on_the_targeted_band(self) -> None:
        page = self._new_page()
        try:
            _click_band_confidence(page, "Tests", "high")
            _click_band_confidence(page, "Solution & Code", "low")
            bands = self._bands(page)
            self.assertEqual(bands["Tests"]["confidence"], "high")
            self.assertEqual(bands["Solution & Code"]["confidence"], "low")
            self.assertIsNone(bands["Problem Description"]["confidence"])
        finally:
            page.close()

    def test_reason_fills_the_right_band_after_low_score(self) -> None:
        page = self._new_page()
        try:
            _click_band_score(page, "Tests", 1)
            _fill_band_reason(
                page, "Tests", reasoning="Harness does not compile.", score=1
            )
            bands = self._bands(page)
            self.assertGreaterEqual(bands["Tests"]["reasonLen"], 5)
            self.assertEqual(bands["Problem Description"]["reasonLen"], 0)
            self.assertEqual(bands["Solution & Code"]["reasonLen"], 0)
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
