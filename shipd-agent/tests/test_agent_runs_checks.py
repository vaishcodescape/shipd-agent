# Unit tests for agent run platform checks.
#
# Phase 5 bar: at least 4 unhinted runs per model, one model only at <= 25%,
# and (Olympus) Nova average sequence length above 400K tokens.

from __future__ import annotations

import unittest

from review.agent_runs_checks import (
    evaluate_agent_runs_phase5,
    parse_agent_run_metrics,
    parse_agent_type_runs,
)


def _data(panel: str) -> dict:
    return {
        "available": True,
        "raw_text": panel,
        "metrics": parse_agent_run_metrics(panel),
    }


ONE_PASS_PANEL = """
Agent Runs
12/12 agent runs complete.
Nova: 1/4 pass
Vega: 0/4 pass
Orion: 0/4 pass
Median files: 5, messages: 146.5, LOC: 420
Nova average sequence length: 450000
""".strip()

TWO_PASSES_ONE_AGENT_PANEL = """
Agent Runs
Nova: 2/4 pass
Vega: 0/4 pass
Orion: 0/4 pass
Median files: 5, messages: 146.5, LOC: 420
""".strip()

PASSES_ACROSS_AGENTS_PANEL = """
Agent Runs
Nova: 1/4 pass
Vega: 1/4 pass
Orion: 0/4 pass
Median files: 5, messages: 146.5, LOC: 420
""".strip()

ZERO_PASS_PANEL = """
Agent Runs
Nova: 0/4 pass
Vega: 0/4 pass
Orion: 0/4 pass
""".strip()

MISSING_ORION_PANEL = """
Agent Runs
Nova: 1/4 pass
Vega: 0/4 pass
Median files: 5, messages: 146.5, LOC: 420
""".strip()

SHORT_VEGA_PANEL = """
Agent Runs
Nova: 1/4 pass
Vega: 0/3 pass
Orion: 0/4 pass
Median files: 5, messages: 146.5, LOC: 420
""".strip()

# The pre-2026-10 bar (10 runs, ≤ 20% pass rate) no longer qualifies.
LEGACY_TEN_RUN_PANEL = """
Agent Runs
10/10 agent runs complete.
2/10 pass
Median files: 5, messages: 146.5, LOC: 420
""".strip()

AGGREGATE_ONLY_PANEL = """
Agent Runs
12/12 agent runs complete.
1/12 pass
Median files: 5, messages: 146.5, LOC: 420
""".strip()

LOW_SCOPE_PANEL = """
Agent Runs
Nova: 1/4 pass
Vega: 0/4 pass
Orion: 0/4 pass
Median files: 2, messages: 40, LOC: 80
Nova average sequence length: 300000
""".strip()

VEGA_SOLVES_PANEL = """
Agent Runs
Vega: 1/4 pass
Nova: 0/4 pass
Orion: 0/4 pass
Nova average sequence length: 450000
""".strip()

VEGA_TWO_OF_EIGHT_PANEL = """
Agent Runs
Vega: 2/8 pass
Nova: 0/4 pass
Orion: 0/4 pass
Nova average sequence length: 450000
""".strip()

VEGA_ONE_OF_FIVE_PANEL = """
Agent Runs
Vega: 1/5 pass
Nova: 0/4 pass
Orion: 0/4 pass
Nova average sequence length: 410000
""".strip()

HINTED_PASS_EXCLUDED_PANEL = """
Agent Runs
Vega run 1: PASS hinted
Vega run 2: FAIL
Vega run 3: FAIL
Vega run 4: FAIL
Vega run 5: FAIL
Nova: 0/4 pass
Orion: 0/4 pass
Nova average sequence length: 450000
""".strip()


class ParseAgentRunMetricsTests(unittest.TestCase):
    def test_parses_runs_pass_rate_and_medians(self) -> None:
        metrics = parse_agent_run_metrics(LEGACY_TEN_RUN_PANEL)
        self.assertEqual(metrics["completed_runs"], 10)
        self.assertEqual(metrics["required_runs"], 10)
        self.assertEqual(metrics["pass_rate_pct"], 20.0)
        self.assertEqual(metrics["median_files"], 5.0)
        self.assertEqual(metrics["median_messages"], 146.5)
        self.assertEqual(metrics["median_loc"], 420.0)
        self.assertEqual(metrics["per_agent"], {})

    def test_per_agent_lines_are_not_read_as_the_aggregate(self) -> None:
        metrics = parse_agent_run_metrics(ONE_PASS_PANEL)
        # "Nova: 1/4 pass" must not become a 1/4 = 25% aggregate.
        self.assertEqual(metrics["passes"], 1)
        self.assertEqual(metrics["total_runs"], 12)
        self.assertEqual(metrics["pass_rate_pct"], 8.3)
        self.assertEqual(metrics["completed_runs"], 12)

    def test_explicit_total_line_wins_over_per_agent_sum(self) -> None:
        panel = AGGREGATE_ONLY_PANEL + "\nNova: 1/4 pass\nVega: 0/4 pass\nOrion: 0/4 pass"
        metrics = parse_agent_run_metrics(panel)
        self.assertEqual((metrics["passes"], metrics["total_runs"]), (1, 12))
        self.assertEqual(set(metrics["per_agent"]), {"Nova", "Vega", "Orion"})


class ParseAgentTypeRunsTests(unittest.TestCase):
    EXPECTED = {
        "Nova": {"runs": 4, "passes": 1},
        "Vega": {"runs": 4, "passes": 0},
        "Orion": {"runs": 4, "passes": 0},
    }

    def test_fraction_summaries(self) -> None:
        self.assertEqual(parse_agent_type_runs(ONE_PASS_PANEL), self.EXPECTED)

    def test_count_and_of_summaries(self) -> None:
        panel = "Nova — 4 runs, 1 passed\nVega — 4 runs, 0 passed\nOrion 0 of 4 passed"
        self.assertEqual(parse_agent_type_runs(panel), self.EXPECTED)

    def test_status_badges_on_agent_line(self) -> None:
        panel = "Nova PASS FAIL FAIL FAIL\nVega FAIL FAIL FAIL FAIL\nOrion ❌ ❌ ❌ ❌"
        self.assertEqual(parse_agent_type_runs(panel), self.EXPECTED)

    def test_one_row_per_run_counts_each_row_once(self) -> None:
        rows = []
        for agent in ("Nova", "Vega", "Orion"):
            for run in range(1, 5):
                status = "PASS" if (agent, run) == ("Nova", 3) else "FAIL — 2 tests failed"
                rows.append(f"{agent} run {run}: {status}")
        self.assertEqual(parse_agent_type_runs("\n".join(rows)), self.EXPECTED)

    def test_agent_headings_with_run_rows(self) -> None:
        panel = """
Nova
Run 1: FAIL
Run 2: PASS
Run 3: FAIL
Run 4: FAIL
Vega
Run 1: FAIL
Run 2: FAIL
Run 3: FAIL
Run 4: FAIL
Orion
Run 1: FAIL
Run 2: FAIL
Run 3: FAIL
Run 4: FAIL
Failure patterns
Agents failed because the edge case was unstated.
""".strip()
        self.assertEqual(parse_agent_type_runs(panel), self.EXPECTED)

    def test_several_agents_on_one_line(self) -> None:
        panel = "Nova 1/4 pass · Vega 0/4 pass · Orion 0/4 pass"
        self.assertEqual(parse_agent_type_runs(panel), self.EXPECTED)

    def test_case_insensitive_and_pass_rate_is_not_a_run(self) -> None:
        panel = "NOVA: 1/4 pass\nvega: 0/4 pass\nOrion: 0/4 pass\nOverall pass rate 8%"
        self.assertEqual(parse_agent_type_runs(panel), self.EXPECTED)

    def test_absent_agents_are_absent(self) -> None:
        self.assertEqual(
            parse_agent_type_runs(MISSING_ORION_PANEL),
            {"Nova": {"runs": 4, "passes": 1}, "Vega": {"runs": 4, "passes": 0}},
        )
        self.assertEqual(parse_agent_type_runs("Agent Runs\n2/10 pass"), {})


class EvaluateAgentRunsPhase5Tests(unittest.TestCase):
    def test_vega_within_25_percent_passes_when_nova_bar_is_met(self) -> None:
        phase, findings = evaluate_agent_runs_phase5(_data(VEGA_SOLVES_PANEL), quest="olympus")
        self.assertEqual(phase.status, "PASS")
        self.assertEqual(findings, [])
        self.assertIn("Vega 1/4", phase.summary)

    def test_two_of_eight_is_25_percent_and_passes(self) -> None:
        phase, findings = evaluate_agent_runs_phase5(
            _data(VEGA_TWO_OF_EIGHT_PANEL), quest="olympus"
        )
        self.assertEqual(phase.status, "PASS", phase.summary)
        self.assertEqual(findings, [])

    def test_one_of_five_is_allowed(self) -> None:
        phase, findings = evaluate_agent_runs_phase5(
            _data(VEGA_ONE_OF_FIVE_PANEL), quest="olympus"
        )
        self.assertEqual(phase.status, "PASS", phase.summary)
        self.assertEqual(findings, [])

    def test_nova_only_solve_passes_with_a_warning(self) -> None:
        phase, findings = evaluate_agent_runs_phase5(_data(ONE_PASS_PANEL), quest="olympus")
        self.assertEqual(phase.status, "PASS", phase.summary)
        self.assertEqual([f.severity for f in findings], ["MINOR"])
        self.assertIn("Nova-only", findings[0].finding)

    def test_two_passes_from_one_agent_fails(self) -> None:
        phase, findings = evaluate_agent_runs_phase5(
            _data(TWO_PASSES_ONE_AGENT_PANEL), quest="olympus"
        )
        self.assertEqual(phase.status, "FAIL")
        self.assertEqual([f.finding for f in findings], ["Solve rate above 25%"])
        self.assertEqual(findings[0].severity, "MAJOR")
        self.assertIn("50%", findings[0].evidence)

    def test_passes_across_agent_types_fail_and_name_them(self) -> None:
        phase, findings = evaluate_agent_runs_phase5(
            _data(PASSES_ACROSS_AGENTS_PANEL), quest="mars"
        )
        self.assertEqual(phase.status, "FAIL")
        self.assertIn("Nova and Vega", findings[0].evidence)
        self.assertEqual(findings[0].finding, "More than one model solved the task")

    def test_zero_passes_fails(self) -> None:
        phase, findings = evaluate_agent_runs_phase5(_data(ZERO_PASS_PANEL), quest="olympus")
        self.assertEqual(phase.status, "FAIL")
        self.assertIn("No model solved the task", [f.finding for f in findings])

    def test_hinted_pass_does_not_count(self) -> None:
        phase, findings = evaluate_agent_runs_phase5(
            _data(HINTED_PASS_EXCLUDED_PANEL), quest="olympus"
        )
        self.assertEqual(phase.status, "FAIL")
        self.assertIn("No model solved the task", [f.finding for f in findings])

    def test_missing_agent_type_fails(self) -> None:
        phase, findings = evaluate_agent_runs_phase5(_data(MISSING_ORION_PANEL), quest="olympus")
        self.assertEqual(phase.status, "FAIL")
        incomplete = [f for f in findings if f.finding.startswith("Agent runs incomplete")]
        self.assertEqual(len(incomplete), 1)
        self.assertIn("Orion 0/4", incomplete[0].evidence)

    def test_short_agent_type_fails(self) -> None:
        phase, findings = evaluate_agent_runs_phase5(_data(SHORT_VEGA_PANEL), quest="olympus")
        self.assertEqual(phase.status, "FAIL")
        self.assertIn("Vega 3/4", phase.summary)

    def test_legacy_ten_run_panel_is_not_a_pass(self) -> None:
        phase, findings = evaluate_agent_runs_phase5(
            _data(LEGACY_TEN_RUN_PANEL), quest="olympus"
        )
        self.assertEqual(phase.status, "SKIP")
        self.assertEqual(findings, [])
        self.assertIn("not a pass", phase.summary)

    def test_aggregate_only_panel_does_not_pass(self) -> None:
        phase, findings = evaluate_agent_runs_phase5(
            _data(AGGREGATE_ONLY_PANEL), quest="olympus"
        )
        self.assertEqual(phase.status, "SKIP")
        self.assertEqual(findings, [])
        self.assertIn("not a pass", phase.summary)

    def test_sequence_length_replaces_olympus_medians(self) -> None:
        phase, findings = evaluate_agent_runs_phase5(_data(LOW_SCOPE_PANEL), quest="olympus")
        self.assertEqual(phase.status, "FAIL")
        self.assertTrue(any("sequence length" in f.finding for f in findings))
        self.assertFalse(any("file count" in f.finding for f in findings))
        mars, mars_findings = evaluate_agent_runs_phase5(_data(LOW_SCOPE_PANEL), quest="mars")
        self.assertEqual(mars.status, "FAIL")
        self.assertTrue(any("Median agent LOC" in f.finding for f in mars_findings))

    def test_unavailable_panel_skips(self) -> None:
        phase, findings = evaluate_agent_runs_phase5({"available": False}, quest="olympus")
        self.assertEqual(phase.status, "SKIP")
        self.assertEqual(findings, [])

    def test_unlabeled_sequence_length_does_not_pass(self) -> None:
        panel = VEGA_SOLVES_PANEL.replace(
            "Nova average sequence length: 450000",
            "Nova tokens: 500000",
        )
        phase, findings = evaluate_agent_runs_phase5(_data(panel), quest="olympus")
        self.assertEqual(phase.status, "SKIP")
        self.assertIn("Long horizon unverified", phase.summary)
        self.assertEqual(findings, [])

    def test_sequence_length_at_the_floor_fails(self) -> None:
        panel = VEGA_SOLVES_PANEL.replace("450000", "400000")
        phase, findings = evaluate_agent_runs_phase5(_data(panel), quest="olympus")
        self.assertEqual(phase.status, "FAIL")
        self.assertIn("Nova sequence length below the long-horizon bar", [f.finding for f in findings])

    def test_cheat_rate_at_20_percent_fails(self) -> None:
        phase, findings = evaluate_agent_runs_phase5(
            _data(VEGA_SOLVES_PANEL + "\nCheat rate: 20%"), quest="olympus"
        )
        self.assertEqual(phase.status, "FAIL")
        self.assertIn("Cheat rate is not below 20%", [f.finding for f in findings])

    def test_unfair_and_blocker_flags_fail_and_clear_flags_do_not(self) -> None:
        unfair, findings = evaluate_agent_runs_phase5(
            _data(VEGA_SOLVES_PANEL + "\nVega run 1 flagged the task unfair"),
            quest="olympus",
        )
        self.assertEqual(unfair.status, "FAIL")
        self.assertIn("Task flagged unfair or broken", [f.finding for f in findings])

        blocked, findings = evaluate_agent_runs_phase5(
            _data(VEGA_SOLVES_PANEL + "\nNova run 2 blocked by the environment"),
            quest="olympus",
        )
        self.assertEqual(blocked.status, "FAIL")
        self.assertIn("Run blocked by environment or verifier", [f.finding for f in findings])

        clear, findings = evaluate_agent_runs_phase5(
            _data(VEGA_SOLVES_PANEL + "\nCheat rate: 0%\nno unfair runs\nno environment blocker"),
            quest="olympus",
        )
        self.assertEqual(clear.status, "PASS", clear.summary)
        self.assertEqual(findings, [])
        self.assertNotIn("Not on the page", clear.summary)

    def test_missing_integrity_flags_stay_a_pass_and_are_named(self) -> None:
        phase, findings = evaluate_agent_runs_phase5(_data(VEGA_SOLVES_PANEL), quest="olympus")
        self.assertEqual(phase.status, "PASS")
        self.assertEqual(findings, [])
        self.assertIn("Not on the page: cheat rate, fair-task flags, environment or verifier blockers.", phase.summary)
        self.assertIn("treated as unhinted", phase.summary)

    def test_unreadable_panel_skips_instead_of_passing(self) -> None:
        phase, findings = evaluate_agent_runs_phase5(
            _data("Agent Runs\nEvaluation in progress…"), quest="olympus"
        )
        self.assertEqual(phase.status, "SKIP")
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
