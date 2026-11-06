"""Issue #1: evidence reads must never start paid platform evaluations."""
from __future__ import annotations

import os
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

from review.page_guard import CompletedReviewError, ensure_review_not_completed
from review.scrape import scrape_review_page_context
from workflow.review import _click_into_review, open_quick_setup, reserve_and_open_review
from workflow.submit import _click_in_form_submit, _find_and_mark, _mark_in_form_submit


class NavigationSafetyTests(unittest.TestCase):
    def test_batch_stops_instead_of_retrying_completed_submission(self):
        from auth import AuthConfig
        from workflow import orchestrator

        batch = {'max_runs': 10}
        with ExitStack() as stack:
            mocks = {
                name: stack.enter_context(patch.object(orchestrator, name))
                for name in ('_prepare_watch_batch', 'ShutdownWatcher', 'run_workflow',
                             'watch_batch', 'session_stats', 'log_message', 'run_clock_out')
            }
            mocks['_prepare_watch_batch'].return_value = (10, batch)
            mocks['ShutdownWatcher'].return_value.requested = False
            mocks['watch_batch'].get_active_batch.return_value = batch
            mocks['watch_batch'].next_run_number.return_value = 1
            mocks['run_workflow'].side_effect = [
                CompletedReviewError('Approved'), AssertionError('retried completed review'),
            ]
            result = orchestrator.run_watch_loop(
                quest='olympus', config=AuthConfig(), headless=True,
                auth_state_path=Path('unused-auth'), clone_dir=Path('unused-clones'),
                clone=True, review=True, submit=False, cleanup=False,
                interval_sec=0, cooldown_every=0, cooldown_sec=0, max_runs=10,
                log_file=None,
            )
            self.assertEqual(result, 1)
            mocks['run_workflow'].assert_called_once()
            mocks['watch_batch'].record_run_complete.assert_not_called()
            mocks['ShutdownWatcher'].return_value.restore.assert_called_once()

    def test_spa_navigation_timeout_never_replays_entry_click(self):
        page, control = MagicMock(), MagicMock()
        page.expect_navigation.return_value.__exit__.side_effect = PlaywrightTimeoutError('SPA')
        with patch('workflow.review.wait_for_review_ready') as ready:
            _click_into_review(page, control)
        control.click.assert_called_once()
        ready.assert_called_once_with(page)

    def test_continued_completed_review_is_blocked(self):
        page = MagicMock()
        with patch('workflow.review.continue_existing_review', return_value=True), patch(
            'workflow.review.ensure_review_not_completed',
            side_effect=CompletedReviewError('Approved'),
        ), patch('workflow.review.find_reserve_button') as reserve:
            with self.assertRaises(CompletedReviewError):
                reserve_and_open_review(page)
        reserve.assert_not_called()


class BrowserActionSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(headless=True)
        except Exception as exc:
            cls.pw.stop()
            if os.environ.get('SHIPD_REQUIRE_BROWSER') == '1':
                raise
            raise unittest.SkipTest(f'Chromium unavailable: {exc}')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()

    def tearDown(self):
        self.page.close()

    def test_42_panel_reads_never_click_run_or_disclosure_controls(self):
        self.page.set_content('''
            <script>window.paidRuns = 0; window.disclosures = 0;</script>
            <section><h2>Holistic Check</h2><div>CHECKLIST
              <button onclick="paidRuns++">Run more Nova agents</button>
              <button onclick="paidRuns++">Show and run Nova</button>
              <button aria-expanded="false" onclick="paidRuns++">Evaluate</button>
              <button onclick="disclosures++">Expand checklist</button>
            </div><p>Too Many Requirements: PASS</p></section>
            <section><h2>Agent Runs</h2><p>Nova: 1/4 pass</p>
              <p>Vega: 0/4 pass</p><p>Orion: 0/4 pass</p>
              <button onclick="paidRuns++">Re-run</button></section>
            <section><h2>Related Submissions</h2><p>No related submissions</p></section>
        ''')
        for _ in range(42):
            result = scrape_review_page_context(self.page)
            self.assertTrue(result['agent_runs_data']['available'])
        self.assertEqual(self.page.evaluate('paidRuns'), 0)
        self.assertEqual(self.page.evaluate('disclosures'), 0)

    def test_completed_badges_block_but_approve_controls_do_not(self):
        for status in ('Approved', 'Rejected', 'Submission approved', 'Review completed'):
            with self.subTest(status=status):
                self.page.set_content(f'<header><span>{status}</span></header>')
                with self.assertRaises(CompletedReviewError):
                    ensure_review_not_completed(self.page)
        self.page.set_content('''<button>Approve</button><p>Tests must pass before this
            submission can be approved.</p><span hidden>Approved</span>''')
        ensure_review_not_completed(self.page)

    def test_completed_page_blocks_setup_and_submit_before_any_click(self):
        self.page.set_content('''<span>Approved</span>
            <button onclick="window.clicks++">View Script</button>
            <button onclick="window.clicks++">Submit Review</button>
            <script>window.clicks = 0;</script>''')
        for action in (open_quick_setup, _click_in_form_submit):
            with self.subTest(action=action.__name__):
                with self.assertRaises(CompletedReviewError):
                    action(self.page)
        self.assertEqual(self.page.evaluate('clicks'), 0)

    def test_form_target_does_not_match_same_text_inside_run_button(self):
        self.page.set_content('''
          <button onclick="window.paidRuns++"><span>Approve</span> Nova evaluation</button>
          <script>window.paidRuns = 0; window.formClicks = 0;</script>
          <form>
            <button type="button" onclick="formClicks++">Approve</button>
            <span>Meets quality standards</span>
            <button type="button">Submit Review</button>
          </form>
        ''')
        result = _find_and_mark(self.page, ['Approve'], require_button=True)
        self.assertTrue(result['ok'], result)
        self.page.locator('[data-shipd-agent-target="1"]').click()
        self.assertEqual(self.page.evaluate('paidRuns'), 0)
        self.assertEqual(self.page.evaluate('formClicks'), 1)

    def test_missing_review_form_never_marks_page_controls(self):
        self.page.set_content('<button>Approve</button><button>Submit</button>')
        self.assertFalse(_find_and_mark(self.page, ['Approve'])['ok'])
        self.assertFalse(_mark_in_form_submit(self.page)['ok'])
        self.assertEqual(self.page.locator('[data-shipd-agent-target]').count(), 0)
        self.assertEqual(self.page.locator('[data-shipd-agent-submit]').count(), 0)
