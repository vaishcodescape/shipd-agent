# Unit tests for agent run platform checks.

from __future__ import annotations

import unittest

from review.agent_runs_checks import evaluate_agent_runs_phase5, parse_agent_run_metrics


OLYMPUS_PANEL = """
Agent Runs
10/10 agent runs complete.
2/10 pass
Median files: 5, messages: 146.5, LOC: 420
""".strip()

MARS_PANEL = """
Agent Runs
10/10 agent
3/10 pass
Median files: 5, messages: 146.5, LOC: 389
""".strip()

LOW_SCOPE_PANEL = """
Agent Runs
10/10 agent
2/10 pass
Median files: 2, messages: 40, LOC: 80
""".strip()


class ParseAgentRunMetricsTests(unittest.TestCase):
    def test_parses_runs_pass_rate_and_medians(self) -> None:
        metrics = parse_agent_run_metrics(OLYMPUS_PANEL)
        self.assertEqual(metrics["completed_runs"], 10)
        self.assertEqual(metrics["required_runs"], 10)
        self.assertEqual(metrics["pass_rate_pct"], 20.0)
        self.assertEqual(metrics["median_files"], 5.0)
        self.assertEqual(metrics["median_messages"], 146.5)
        self.assertEqual(metrics["median_loc"], 420.0)


class EvaluateAgentRunsPhase5Tests(unittest.TestCase):
    def test_olympus_passes_platform_bars(self) -> None:
        data = {
            "available": True,
            "raw_text": OLYMPUS_PANEL,
            "metrics": parse_agent_run_metrics(OLYMPUS_PANEL),
        }
        phase, findings = evaluate_agent_runs_phase5(data, quest="olympus")
        self.assertEqual(phase.status, "PASS")
        self.assertEqual(findings, [])

    def test_mars_passes_with_high_median_loc(self) -> None:
        data = {
            "available": True,
            "raw_text": MARS_PANEL,
            "metrics": parse_agent_run_metrics(MARS_PANEL),
        }
        phase, findings = evaluate_agent_runs_phase5(data, quest="mars")
        self.assertEqual(phase.status, "PASS")
        self.assertEqual(findings, [])

    def test_olympus_fails_low_median_loc(self) -> None:
        data = {
            "available": True,
            "raw_text": LOW_SCOPE_PANEL,
            "metrics": parse_agent_run_metrics(LOW_SCOPE_PANEL),
        }
        phase, findings = evaluate_agent_runs_phase5(data, quest="olympus")
        self.assertEqual(phase.status, "FAIL")
        self.assertTrue(any("LOC" in f.finding for f in findings))


if __name__ == "__main__":
    unittest.main()
