"""Exercise the real Deep Agents harness offline, including delegated work."""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult, LLMResult
from langgraph.errors import GraphRecursionError

from review.config import get_review_config
from review.context import discover_artifacts
from review.deep import ReviewBudget, ReviewBudgetExceeded, create_review_harness, run_deep_review
from review.evidence import EvidenceLedger, EvidenceReference, PhaseEvidenceInput, fit_finalize_payload, panel_text_available
from review import graph as review_graph
from review.graph import _apply_evidence_reports, finalize_node, validate_node, unified_review_node
from review.review_phases import Phase0Result
from review.schemas import BandRating, BandRatings, PhaseResult, ReviewResult
from review.tools import make_review_tools
from review.review_bundles import save_review_bundle, load_review_bundle


@pytest.fixture
def submission(tmp_path):
    for name in ("problem.md", "Dockerfile", "test.sh", "test.patch", "solution.patch"):
        (tmp_path / name).write_text(f"{name} evidence\nsecond line\n", encoding="utf-8")
    return tmp_path


def setup_review(path, **overrides):
    cfg = replace(get_review_config(), **overrides)
    ledger = EvidenceLedger(path, discover_artifacts(path), {}, "Phase 0 PASS")
    phase0 = Phase0Result("PASS", "ok", "Phase 0 PASS", artifacts=discover_artifacts(path))
    tools = make_review_tools(path, quest="olympus", cached_phase0=phase0,
                              config=cfg, cached_scrape={}, evidence=ledger)
    return cfg, ledger, {t.name: t for t in tools}


def read(tools, path, **kwargs):
    output = tools["read_submission_file"].invoke({"path": path, **kwargs})
    source_id = re.search(r"source_id: (src_\w+)", output).group(1)
    return {"source_id": source_id, "start_line": 1, "end_line": 1}


def approved_review():
    band = BandRating(score=3, confidence="high")
    return ReviewResult(decision="approve", band_ratings=BandRatings(problem=band, tests=band, solution=band),
                        phase_results={str(i): PhaseResult(status="PASS", summary="ok") for i in range(7)},
                        recommendation_summary="Looks good", contributor_feedback="Looks good")


class ScriptedReviewer(BaseChatModel):
    delegated: bool = False
    prose_only: bool = False
    scratch: bool = False
    seen_tools: list[str] = []

    @property
    def _llm_type(self):
        return "offline-reviewer"

    def bind_tools(self, tools, **kwargs):
        self.seen_tools = [t.name if hasattr(t, "name") else t["name"] for t in tools]
        assert len(self.seen_tools) == len(set(self.seen_tools))
        assert "execute" not in self.seen_tools
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        def call(name, args):
            return {"name": name, "args": args, "id": uuid4().hex}

        calls = []
        outputs = [m for m in messages if isinstance(m, ToolMessage)]
        worker = "Record only assigned phase 4" in str(messages[0].content)
        if self.prose_only:
            pass
        elif self.scratch:
            if not outputs:
                calls = [call("write_file", {"file_path": "/private-note.md", "content": "scratch only"})]
            elif outputs[-1].name == "write_file":
                calls = [call("read_file", {"file_path": "/private-note.md"})]
        elif self.delegated and not worker:
            if not outputs:
                calls = [call("task", {"subagent_type": "solution", "description": "Read solution.patch; record phase 4."})]
        else:
            files = ["solution.patch"] if worker else ["problem.md", "Dockerfile", "test.sh", "test.patch", "solution.patch"]
            if not outputs:
                calls = [call("read_submission_file", {"path": path}) for path in files]
            elif not any(m.name == "record_phase_review" for m in outputs):
                receipts = [re.search(r"source_id: (src_\w+)", str(m.content)).group(1)
                            for m in outputs if m.name == "read_submission_file"]
                refs = {path: {"source_id": source, "start_line": 1, "end_line": 1}
                        for path, source in zip(files, receipts)}
                assignments = {"4": ["solution.patch"]} if worker else {
                    "1": ["problem.md"], "2": ["Dockerfile", "test.sh"],
                    "3": ["test.patch"], "4": ["solution.patch"], "5": [], "6": [],
                }
                calls = [call("record_phase_review", {
                    "phase": phase, "status": "PASS" if paths else "SKIP",
                    "summary": "Reviewed evidence" if paths else "No platform data",
                    "citations": [refs[p] for p in paths],
                }) for phase, paths in assignments.items()]
        msg = AIMessage(content="" if calls else "Phase 1: PASS\nPhase 2: PASS\nPhase 3: PASS\nPhase 4: PASS",
                        tool_calls=calls, usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
        return ChatResult(generations=[ChatGeneration(message=msg)])


def test_real_harness_records_evidence_and_counts_usage(submission):
    cfg, ledger, tools = setup_review(submission)
    budget = ReviewBudget(15, 6, 10000)
    output = run_deep_review(model=ScriptedReviewer(), tools=list(tools.values()), prompt="Review all phases",
                             ledger=ledger, config=cfg, budget=budget)
    assert output["evidence_gaps"] == []
    assert set(output["review_evidence"]["phases"]) == set("123456")
    assert output["review_evidence"]["phases"]["4"]["citations"][0]["excerpt"] == "solution.patch evidence"
    assert budget.snapshot()["model_calls"] == 3
    assert budget.snapshot()["reported_tokens"] == 45


def test_delegated_worker_records_shared_evidence_and_uses_shared_budget(submission):
    cfg, ledger, tools = setup_review(submission, review_coverage_recheck=False)
    budget = ReviewBudget(8, 0, 10000)
    result = run_deep_review(model=ScriptedReviewer(delegated=True), tools=list(tools.values()),
                             prompt="Delegate phase 4", ledger=ledger, config=cfg, budget=budget)
    assert "4" in result["review_evidence"]["phases"]
    assert budget.snapshot()["model_calls"] == 5  # parent twice, worker three times
    assert budget.snapshot()["reported_tokens"] == 75


def test_harness_filesystem_is_private_scratch(submission):
    _, _, tools = setup_review(submission)
    model = ScriptedReviewer(scratch=True)
    harness = create_review_harness(model, list(tools.values()), "Review")
    result = harness.invoke({"messages": [{"role": "user", "content": "Make scratch note"}]})
    assert not (submission / "private-note.md").exists()
    assert not Path("/private-note.md").exists()
    assert any(isinstance(m, ToolMessage) and m.name == "read_file" and "scratch only" in str(m.content)
               for m in result["messages"])


def test_prose_cannot_satisfy_phase_coverage(submission):
    cfg, ledger, tools = setup_review(submission)
    output = run_deep_review(model=ScriptedReviewer(prose_only=True), tools=list(tools.values()),
                             prompt="Review", ledger=ledger, config=cfg, budget=ReviewBudget(3, 2, 10000))
    assert output["evidence_gaps"] == ["1", "2", "3", "4"]
    result = validate_node({"config": cfg, "review_result": approved_review().to_submit_dict(), **output})["review_result"]
    assert result["decision"] == "request_changes"
    assert "Coverage gap" in result["internal_notes"]
    assert result["review_complete"] is True  # existing flag-and-submit policy


def test_paginated_reads_recover_middle_and_reject_escaping_symlinks(submission, tmp_path):
    cfg, ledger, tools = setup_review(submission)
    text = "\n".join(f"line {i}" for i in range(1, 5001))
    (submission / "large.py").write_text(text)
    output = tools["read_submission_file"].invoke({"path": "large.py", "start_line": 2500, "line_count": 2})
    assert "2500: line 2500\n2501: line 2501" in output
    source_id = re.search(r"source_id: (src_\w+)", output).group(1)
    assert ledger._resolve(EvidenceReference(source_id=source_id, start_line=2500, end_line=2500))["excerpt"] == "line 2500"
    outside = tmp_path.parent / f"outside-{uuid4().hex}.txt"
    outside.write_text("outside")
    try:
        (submission / "escape").symlink_to(outside)
        before = ledger.source_listing()
        assert "Error" in tools["read_submission_file"].invoke({"path": "escape"})
        assert "Error" in tools["read_submission_file"].invoke({"path": "missing"})
        assert ledger.source_listing() == before
    finally:
        outside.unlink()


def test_rejects_invented_citations_wrong_phase_sources_and_failed_reads(submission):
    _, ledger, tools = setup_review(submission)
    cite = read(tools, "test.patch")
    for citations in ([{"source_id": "invented"}], [cite], [{**cite, "end_line": 999}]):
        result = tools["record_phase_review"].invoke({"phase": "1", "status": "PASS", "summary": "ok", "citations": citations})
        assert result.startswith("Error")
        assert "1" in ledger.gaps()
    assert tools["record_phase_review"].invoke({"phase": "3", "status": "PASS", "summary": "ok", "citations": [cite]}).startswith("Recorded")


def test_two_harness_artifacts_required_and_platform_panels_are_distinct(submission):
    cfg, ledger, tools = setup_review(submission)
    docker = read(tools, "Dockerfile")
    result = tools["record_phase_review"].invoke({"phase": "2", "status": "PASS", "summary": "ok", "citations": [docker]})
    assert "test.sh" in result and "2" in ledger.gaps()
    panels = {"agent_runs": "Nova passes 1/4", "related_submissions": "Similar submission 0.8"}
    ledger = EvidenceLedger(submission, discover_artifacts(submission), panels, "PASS")
    tools = {t.name: t for t in make_review_tools(submission, quest="olympus", cached_scrape=panels, config=cfg, evidence=ledger)}
    text = tools["read_shipd_review_panel"].invoke({"panel_name": "Agent Runs"})
    source_id = re.search(r"source_id: (src_\w+)", text).group(1)
    cite = {"source_id": source_id}
    assert tools["record_phase_review"].invoke({"phase": "5", "status": "PASS", "summary": "ok", "citations": [cite]}).startswith("Recorded")
    assert tools["record_phase_review"].invoke({"phase": "6", "status": "PASS", "summary": "ok", "citations": [cite]}).startswith("Error")
    assert tools["record_phase_review"].invoke({"phase": "6", "status": "SKIP", "summary": "none"}).startswith("Error")


def test_recorded_failure_survives_scorer_approval_and_audit_is_saved(submission):
    cfg, ledger, tools = setup_review(submission)
    cite = read(tools, "solution.patch")
    ledger.record(PhaseEvidenceInput(phase="4", status="FAIL", summary="Missing case", citations=[cite], findings=[{
        "severity": "MAJOR", "finding": "Missing case", "citations": [cite], "suggested_fix": "Handle it",
    }]))
    state = {"config": cfg, "review_evidence": ledger.snapshot(), "evidence_gaps": ledger.gaps(),
             "phase_results": {"4": {"status": "PASS", "summary": "LOC OK"}},
             "review_result": approved_review().to_submit_dict(), "review_budget": ReviewBudget(15, 6, 10000)}
    result = validate_node(state)["review_result"]
    assert result["decision"] == "request_changes"
    assert result["phase_results"]["4"]["status"] == "FAIL"
    assert result["band_ratings"]["solution"]["score"] <= 1
    assert "solution.patch:1-1" in result["findings"][0]["evidence"]
    assert "review_evidence" in result and "review_usage" in result
    assert _apply_evidence_reports(approved_review(), state).findings


def test_json_budget_preserves_evidence_and_never_slices_json():
    evidence = {"phases": {"4": {"summary": "Critical", "citations": [{"excerpt": "assert result"}]}}}
    encoded = fit_finalize_payload({"review_evidence": evidence, "explore_notes": "x" * 40000, "phase0_log": "y" * 20000}, 1000)
    assert len(encoded) <= 1000
    assert json.loads(encoded)["review_evidence"] == evidence
    with pytest.raises(ValueError, match="Structured review evidence"):
        fit_finalize_payload({"review_evidence": evidence}, 10)


def test_shared_budget_is_atomic_and_reserves_finalization_calls():
    budget = ReviewBudget(2, 1, 1000)
    callback = budget.callback()
    def start(_):
        try:
            callback.on_chat_model_start({}, [], run_id=uuid4())
            return True
        except ReviewBudgetExceeded:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(start, range(20))) == 2
    budget.callback(recheck=True).on_chat_model_start({}, [], run_id=uuid4())
    for _ in range(2):
        budget.callback(finalize=True).on_chat_model_start({}, [], run_id=uuid4())
    with pytest.raises(ReviewBudgetExceeded):
        budget.callback(finalize=True).on_chat_model_start({}, [], run_id=uuid4())


def test_reported_token_limit_stops_next_call():
    budget = ReviewBudget(15, 6, 20)
    callback = budget.callback()
    rid = uuid4()
    callback.on_chat_model_start({}, [], run_id=rid)
    callback.on_llm_end(LLMResult(generations=[[ChatGeneration(message=AIMessage(content="done", usage_metadata={
        "input_tokens": 20, "output_tokens": 5, "total_tokens": 25,
    }))]]), run_id=rid)
    with pytest.raises(ReviewBudgetExceeded, match="token"):
        callback.on_chat_model_start({}, [], run_id=uuid4())


def test_primary_node_uses_deep_harness_and_never_hands_workers_a_browser(submission):
    cfg, _, _ = setup_review(submission)
    state = {"config": cfg, "repo_path": str(submission), "quest": "mars",
             "submission_summary": {"artifacts": discover_artifacts(submission)}, "page": object()}
    mechanical = {"phase0_result": Phase0Result("PASS", "ok", "PASS"), "phase0_log": "PASS"}
    with patch("review.graph._run_phase0_into_state", return_value=mechanical), \
         patch("review.graph.llm_config_error", return_value=None), \
         patch("review.graph.make_chat_model", return_value=ScriptedReviewer()), \
         patch("review.graph.make_review_tools", wraps=make_review_tools) as factory:
        result = unified_review_node(state)
    assert "error" not in result
    assert result["evidence_gaps"] == []
    assert factory.call_args.kwargs["page"] is None
    assert result["review_budget"].snapshot()["model_calls"] == 3


@pytest.mark.parametrize("start,line_count,max_chars,expected", [
    (1, 2, 100, "1: first\n2: "),
    (2, 1, 100, "2: "),
    (3, 2, 100, "3: last"),
    (4, 1, 100, "Error: start_line exceeds"),
    (1, 3, 10, "More lines available: call start_line=2"),
    (3, 1, 1, "Error: source line exceeds"),
])
def test_read_boundaries_and_blank_line_citations(submission, start, line_count, max_chars, expected):
    _, ledger, tools = setup_review(submission)
    (submission / "lines.txt").write_text("first\n\nlast\n")
    output = tools["read_submission_file"].invoke({"path": "lines.txt", "start_line": start,
                                                    "line_count": line_count, "max_chars": max_chars})
    assert expected in output
    if start == 2:
        source_id = re.search(r"source_id: (src_\w+)", output).group(1)
        assert ledger._resolve(EvidenceReference(source_id=source_id, start_line=2, end_line=2))["excerpt"] == ""


def test_empty_files_have_citable_receipts_without_invented_lines(submission):
    _, ledger, tools = setup_review(submission)
    (submission / "test.patch").write_text("")
    result = tools["read_submission_file"].invoke({"path": "test.patch"})
    assert "(empty file)" in result
    source_id = re.search(r"source_id: (src_\w+)", result).group(1)
    ledger.record(PhaseEvidenceInput(phase="3", status="FAIL", summary="Empty patch", citations=[{"source_id": source_id}]))
    assert ledger.snapshot()["phases"]["3"]["citations"][0]["start_line"] is None
    with pytest.raises(ValueError, match="Empty sources"):
        ledger._resolve(EvidenceReference(source_id=source_id, start_line=1))


def test_read_io_error_never_creates_a_receipt(submission):
    _, ledger, tools = setup_review(submission)
    before = ledger.source_listing()
    with patch.object(Path, "read_text", side_effect=OSError("denied")):
        assert "Error reading" in tools["read_submission_file"].invoke({"path": "problem.md"})
    assert ledger.source_listing() == before
    assert "Error: not a file" in tools["read_submission_file"].invoke({"path": "."})


def test_search_fallback_and_listing_cannot_follow_escaping_symlinks(submission, tmp_path):
    _, _, tools = setup_review(submission)
    outside = tmp_path.parent / f"secret-{uuid4().hex}.txt"
    outside.write_text("private needle")
    try:
        (submission / "escape.txt").symlink_to(outside)
        listing = tools["list_directory"].invoke({"path": "."})
        assert "escape.txt" not in listing
        with patch("review.tools.subprocess.run", side_effect=FileNotFoundError):
            result = tools["search_repo"].invoke({"pattern": "needle", "glob": "*.txt"})
        assert result == "(no matches)"
        # Option-looking patterns must remain patterns, not rg's --follow flag.
        with patch("review.tools.subprocess.run") as run:
            run.return_value.returncode = 1
            run.return_value.stdout = ""
            tools["search_repo"].invoke({"pattern": "--follow"})
            argv = run.call_args.args[0]
            assert argv.index("--") < argv.index("--follow")
    finally:
        outside.unlink()


def test_reports_are_immutable_detached_and_isolated_per_submission(submission):
    _, ledger, tools = setup_review(submission)
    cite = read(tools, "test.patch")
    report = PhaseEvidenceInput(phase="3", status="FAIL", summary="Problem", citations=[cite])
    ledger.record(report)
    assert ledger.record(report).startswith("Recorded")  # safe idempotent retry
    with pytest.raises(ValueError, match="overwritten"):
        ledger.record(report.model_copy(update={"status": "PASS"}))
    snapshot = ledger.snapshot()
    snapshot["phases"]["3"]["summary"] = "changed"
    assert ledger.snapshot()["phases"]["3"]["summary"] == "Problem"
    isolated = EvidenceLedger(submission, discover_artifacts(submission), {}, "PASS")
    with pytest.raises(ValueError, match="Unknown source_id"):
        isolated.record(report)


def test_competing_workers_cannot_erase_an_accepted_report(submission):
    _, ledger, tools = setup_review(submission)
    cite = read(tools, "solution.patch")
    def record(i):
        try:
            ledger.record(PhaseEvidenceInput(phase="4", status="FAIL", summary=f"Issue {i}", citations=[cite]))
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(record, range(30))) == 1
    assert len(ledger.snapshot()["phases"]) == 1


@pytest.mark.parametrize("ref", [
    {"start_line": 0}, {"start_line": -1}, {"end_line": 0},
])
def test_invalid_citation_schema_is_rejected(submission, ref):
    _, ledger, tools = setup_review(submission)
    cite = read(tools, "test.patch")
    with pytest.raises(ValueError):
        PhaseEvidenceInput(phase="3", status="PASS", summary="ok", citations=[{**cite, **ref}])
    assert "3" in ledger.gaps()


@pytest.mark.parametrize("start,end", [(2, 1), (1, 100), (3, 3)])
def test_citations_must_stay_inside_the_returned_page(submission, start, end):
    _, ledger, tools = setup_review(submission)
    cite = read(tools, "test.patch")
    with pytest.raises(ValueError, match="outside"):
        ledger.record(PhaseEvidenceInput(phase="3", status="PASS", summary="ok",
                                         citations=[{**cite, "start_line": start, "end_line": end}]))


def test_missing_artifacts_require_manifest_and_cannot_pass(submission):
    (submission / "solution.patch").unlink()
    _, ledger, tools = setup_review(submission)
    ref = {"source_id": ledger.manifest_id}
    with pytest.raises(ValueError, match="missing required artifact"):
        ledger.record(PhaseEvidenceInput(phase="4", status="PASS", summary="ok", citations=[ref]))
    ledger.record(PhaseEvidenceInput(phase="4", status="FAIL", summary="Missing solution", citations=[ref]))
    assert "4" not in ledger.gaps()


def test_oversized_citations_and_reports_fail_without_mutating_coverage(submission):
    _, ledger, tools = setup_review(submission)
    (submission / "test.patch").write_text("a" * 1700)
    cite = read(tools, "test.patch")
    with pytest.raises(ValueError, match="1600"):
        ledger.record(PhaseEvidenceInput(phase="3", status="PASS", summary="ok", citations=[cite]))
    assert "3" in ledger.gaps()
    (submission / "test.patch").write_text("short")
    cite = read(tools, "test.patch")
    ledger.max_phase_chars = 100
    with pytest.raises(ValueError, match="Phase report exceeds"):
        ledger.record(PhaseEvidenceInput(phase="3", status="FAIL", summary="A long issue" * 20, citations=[cite]))
    assert "3" in ledger.gaps()


def test_skip_requires_absent_data_and_cannot_smuggle_findings(submission):
    _, ledger, tools = setup_review(submission)
    cite = read(tools, "problem.md")
    with pytest.raises(ValueError, match="requires successfully"):
        ledger.record(PhaseEvidenceInput(phase="1", status="PASS", summary="ok"))
    with pytest.raises(ValueError, match="cannot be SKIP"):
        ledger.record(PhaseEvidenceInput(phase="1", status="SKIP", summary="none"))
    with pytest.raises(ValueError, match="SKIP cannot"):
        ledger.record(PhaseEvidenceInput(phase="5", status="SKIP", summary="none", findings=[{
            "severity": "MAJOR", "finding": "unverified", "citations": [cite],
        }]))
    assert tools["read_shipd_review_panel"].invoke({"panel_name": "made-up"}).startswith("Error")


class RecheckReviewer(ScriptedReviewer):
    first: bool = True

    def _generate(self, messages, **kwargs):
        if self.first:
            self.first = False
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="Done without evidence"))])
        return super()._generate(messages, **kwargs)


def test_recheck_has_a_bounded_extra_allowance_and_preserves_existing_receipts(submission):
    cfg, ledger, tools = setup_review(submission, review_max_tool_steps=1, review_coverage_recheck_max_steps=3)
    budget = ReviewBudget(1, 3, 10000)
    result = run_deep_review(model=RecheckReviewer(), tools=list(tools.values()), prompt="Review",
                             ledger=ledger, config=cfg, budget=budget)
    assert result["evidence_gaps"] == []
    assert budget.snapshot()["model_calls"] == 4


@pytest.mark.parametrize("calls,recorded", [(1, False), (3, True)])
def test_exhaustion_during_real_delegation_salvages_only_recorded_evidence(submission, calls, recorded):
    cfg, ledger, tools = setup_review(submission, review_coverage_recheck=False)
    budget = ReviewBudget(calls, 0, 10000)
    result = run_deep_review(model=ScriptedReviewer(delegated=True), tools=list(tools.values()), prompt="Delegate",
                             ledger=ledger, config=cfg, budget=budget)
    assert ("4" in result["review_evidence"]["phases"]) is recorded
    assert budget.snapshot()["model_calls"] == calls
    # Two scoring starts remain available after a worker spends the last call.
    budget.callback(finalize=True).on_chat_model_start({}, [], run_id=uuid4())
    budget.callback(finalize=True).on_chat_model_start({}, [], run_id=uuid4())


def test_usage_accounting_handles_missing_provider_usage_and_duplicate_events():
    budget = ReviewBudget(3, 0, 0)
    callback = budget.callback()
    rid = uuid4()
    callback.on_chat_model_start({}, [], run_id=rid)
    callback.on_chat_model_start({}, [], run_id=rid)
    reply = LLMResult(generations=[[ChatGeneration(message=AIMessage(content="ok"))]])
    callback.on_llm_end(reply, run_id=rid)
    callback.on_llm_end(reply, run_id=rid)
    assert budget.snapshot()["model_calls"] == 1
    assert budget.snapshot()["calls_without_token_usage"] == 1
    rid = uuid4()
    callback.on_chat_model_start({}, [], run_id=rid)
    callback.on_llm_end(LLMResult(generations=[], llm_output={"token_usage": {"total_tokens": 12}}), run_id=rid)
    assert budget.snapshot()["reported_tokens"] == 12


class ScriptedScorer(BaseChatModel):
    retry_once: bool = False
    attempts: int = 0
    payloads: list[dict] = []

    @property
    def _llm_type(self):
        return "offline-scorer"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, **kwargs):
        content = str(messages[1].content).split("\n\nPrevious validation error", 1)[0]
        self.payloads.append(json.loads(content))
        self.attempts += 1
        args = {} if self.retry_once and self.attempts == 1 else approved_review().model_dump()
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[{
            "name": "ReviewResult", "args": args, "id": uuid4().hex,
        }], usage_metadata={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}))])


@pytest.mark.parametrize("retry", [False, True])
def test_full_graph_real_harness_to_scorer_to_saved_bundle(submission, retry):
    # An excerpt beyond the old 600-character clip must survive to the scorer.
    (submission / "solution.patch").write_text("important " * 100)
    cfg, _, _ = setup_review(submission, llm_api_key="offline-key")
    explorer = ScriptedReviewer()
    scorer = ScriptedScorer(retry_once=retry)
    def factory(config, role, structured_output=None):
        return explorer if role == "explore" else scorer.with_structured_output(structured_output)
    mechanical = {"phase0_result": Phase0Result("PASS", "ok", "PASS"), "phase0_log": "PASS",
                  "phase_results": {"0": {"status": "PASS", "summary": "Mechanical checks passed"}}}
    with patch("review.graph._run_phase0_into_state", return_value=mechanical), \
         patch("review.graph.make_chat_model", side_effect=factory):
        result = review_graph.run_review_graph(repo_path=str(submission), quest="olympus", review_url="",
                                               config=cfg)
    assert result["review_complete"] is True
    assert result["decision"] == "approve"
    # Finalize uses one call (two when the first payload is empty). Auto Review
    # prediction then tries twice because this scorer does not emit that schema.
    assert result["review_usage"]["model_calls"] == (7 if retry else 6)
    assert scorer.attempts == (2 if retry else 1)
    excerpt = scorer.payloads[0]["review_evidence"]["phases"]["4"]["citations"][0]["excerpt"]
    assert excerpt == "important " * 100
    bundle_path = submission / "review-bundle.json"
    save_review_bundle(result, quest="olympus", review_url="https://shipd.ai/review/offline", path=bundle_path)
    loaded, _, _, _ = load_review_bundle(bundle_path)
    assert loaded["review_evidence"] == result["review_evidence"]
    assert loaded["review_usage"] == result["review_usage"]


def test_provider_failure_marks_review_incomplete_and_preserves_audit(submission):
    cfg, _, _ = setup_review(submission, llm_api_key="offline-key")
    mechanical = {"phase0_result": Phase0Result("PASS", "ok", "PASS"), "phase0_log": "PASS"}
    with patch("review.graph._run_phase0_into_state", return_value=mechanical), \
         patch("review.graph.make_chat_model", side_effect=RuntimeError("provider unavailable")):
        result = review_graph.run_review_graph(repo_path=str(submission), quest="olympus", review_url="", config=cfg)
    assert result["review_complete"] is False
    assert "provider unavailable" in result["review_error"]
    assert result["review_evidence"]["coverage_gaps"] == list("1234")
    assert result["review_usage"]["model_calls"] == 0


def test_scorer_overflow_is_incomplete_and_never_invokes_a_model(submission):
    cfg, _, _ = setup_review(submission, llm_api_key="offline-key", review_finalize_payload_max_chars=100)
    state = {"config": cfg, "review_evidence": {"phases": {"1": {"summary": "x" * 200}}}}
    with patch("review.graph.make_chat_model") as model:
        result = finalize_node(state)["review_result"]
    model.assert_not_called()
    assert result["review_complete"] is False
    assert result["review_evidence"] == state["review_evidence"]


def test_dry_run_and_phase0_short_circuit_never_construct_a_harness(submission):
    cfg, _, _ = setup_review(submission)
    state = {"config": replace(cfg, review_dry_run=True), "repo_path": str(submission), "quest": "olympus"}
    with patch("review.graph._run_phase0_into_state", return_value={"force_request_changes": True}), \
         patch("review.graph.run_deep_review") as harness:
        assert "dry run" in unified_review_node(state)["explore_notes"]
        state["config"] = replace(cfg, review_skip_explore_on_phase0_fail=True)
        assert "critical FAIL" in unified_review_node(state)["explore_notes"]
    harness.assert_not_called()


@pytest.mark.parametrize("placeholder", ["", "none", "N/A", "not available", "  not available — run via orchestrator with browser session  ",
                                        "Holistic AI Check: not available — run via orchestrator"])
def test_unavailable_platform_placeholders_never_require_or_create_evidence(submission, placeholder):
    cfg, _, _ = setup_review(submission)
    panels = {"agent_runs": placeholder, "related_submissions": placeholder}
    ledger = EvidenceLedger(submission, discover_artifacts(submission), panels, "PASS")
    assert ledger.gaps() == list("1234")
    assert review_graph._agent_runs_available(panels) is False
    assert review_graph._platform_data_available(panels) is False
    tools = {t.name: t for t in make_review_tools(submission, quest="olympus", cached_scrape=panels, config=cfg, evidence=ledger)}
    before = ledger.source_listing()
    tools["read_shipd_review_panel"].invoke({"panel_name": "Agent Runs"})
    assert ledger.source_listing() == before


def test_holistic_panel_is_available_and_negative_related_result_is_real_data(submission):
    cfg, _, _ = setup_review(submission)
    assert panel_text_available("No related submissions.")
    panels = {"holistic_check": "Fairness PASS", "related_submissions": "No related submissions."}
    ledger = EvidenceLedger(submission, discover_artifacts(submission), panels, "PASS")
    assert "6" in ledger.gaps()
    assert "platform:holistic_check" in ledger.panel_paths["5"]
    holistic = {"available": True, "status": "PASS", "raw_text": "Fairness PASS"}
    tools = {t.name: t for t in make_review_tools(submission, quest="olympus", cached_scrape={},
                                                cached_holistic=holistic, config=cfg, evidence=ledger)}
    result = tools["read_holistic_check"].invoke({})
    source_id = re.search(r"source_id: (src_\w+)", result).group(1)
    ledger.record(PhaseEvidenceInput(phase="6", status="PASS", summary="Fair", citations=[{
        "source_id": source_id, "start_line": 2, "end_line": 2,
    }]))
    assert ledger.snapshot()["phases"]["6"]["citations"][0]["excerpt"] == "Status: PASS"


def test_provider_errors_are_counted_once_and_never_retried_by_another_harness(submission):
    class BrokenReviewer(ScriptedReviewer):
        def _generate(self, messages, **kwargs):
            raise RuntimeError("HTTP failure")
    cfg, ledger, tools = setup_review(submission)
    budget = ReviewBudget(15, 6, 10000)
    with pytest.raises(RuntimeError, match="HTTP failure"):
        run_deep_review(model=BrokenReviewer(), tools=list(tools.values()), prompt="Review",
                         ledger=ledger, config=cfg, budget=budget)
    assert budget.snapshot()["model_calls"] == 1
    assert budget.snapshot()["failed_model_calls"] == 1
    assert budget.snapshot()["calls_without_token_usage"] == 1


def test_recursion_error_retains_previously_accepted_reports(submission):
    cfg, ledger, tools = setup_review(submission, review_coverage_recheck=False)
    ledger.record(PhaseEvidenceInput(phase="3", status="FAIL", summary="Test issue",
                                     citations=[read(tools, "test.patch")]))
    with patch("review.deep.create_review_harness") as factory:
        factory.return_value.invoke.side_effect = GraphRecursionError("recursion limit")
        result = run_deep_review(model=ScriptedReviewer(), tools=list(tools.values()), prompt="Review",
                                 ledger=ledger, config=cfg, budget=ReviewBudget(15, 0, 10000))
    assert result["review_evidence"]["phases"]["3"]["status"] == "FAIL"
    assert "3" not in result["evidence_gaps"]


def test_json_budget_trims_last_optional_field_and_handles_escaped_unicode():
    payload = {"review_evidence": {"phases": {}}, "prompt_analysis": '\\"\né' * 1000}
    encoded = fit_finalize_payload(payload, 500)
    assert len(encoded) <= 500
    assert json.loads(encoded)["review_evidence"] == {"phases": {}}
    assert len(payload["prompt_analysis"]) > 1000  # caller's evidence remains untouched


@pytest.mark.parametrize("raw", [None, {"decision": "bogus"}])
def test_missing_or_invalid_scorer_output_retains_audit_and_is_incomplete(submission, raw):
    cfg, ledger, _ = setup_review(submission)
    result = validate_node({"config": cfg, "review_result": raw, "review_evidence": ledger.snapshot()})["review_result"]
    assert result["review_complete"] is False
    assert result["review_evidence"] == ledger.snapshot()


def test_recheck_retains_harness_scratch_state(submission):
    class ScratchAcrossTurns(ScriptedReviewer):
        def _generate(self, messages, **kwargs):
            last_user = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
            after_user = messages[last_user + 1:]
            recheck = str(messages[last_user].content).startswith("Record missing")
            calls = []
            if not after_user:
                calls = [{"name": "read_file" if recheck else "write_file", "id": uuid4().hex,
                          "args": {"file_path": "/retained.md"} if recheck else {
                              "file_path": "/retained.md", "content": "retained across recheck",
                          }}]
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="" if calls else "Done", tool_calls=calls))])
    cfg, ledger, tools = setup_review(submission)
    result = run_deep_review(model=ScratchAcrossTurns(), tools=list(tools.values()), prompt="Review",
                             ledger=ledger, config=cfg, budget=ReviewBudget(5, 5, 10000))
    read_results = [m for m in result["explore_messages"] if isinstance(m, ToolMessage) and m.name == "read_file"]
    assert len(read_results) == 1
    assert "retained across recheck" in read_results[0].content
    assert read_results[0].status == "success"


def test_scorer_initialization_failure_is_incomplete_with_collected_evidence(submission):
    cfg, ledger, _ = setup_review(submission, llm_api_key="offline-key")
    state = {"config": cfg, "review_evidence": ledger.snapshot(), "review_budget": ReviewBudget(15, 6, 10000)}
    with patch("review.graph.make_chat_model", side_effect=ValueError("invalid scorer setting")):
        result = finalize_node(state)["review_result"]
    assert result["review_complete"] is False
    assert "Cannot initialize final scorer" in result["review_error"]
    assert result["review_evidence"] == ledger.snapshot()


def test_two_invalid_scorer_outputs_stop_without_losing_evidence(submission):
    cfg, ledger, _ = setup_review(submission, llm_api_key="offline-key")
    state = {"config": cfg, "review_evidence": ledger.snapshot(), "review_budget": ReviewBudget(15, 6, 10000)}
    with patch("review.graph.make_chat_model") as factory:
        factory.return_value.invoke.side_effect = ValueError("invalid structured output")
        result = finalize_node(state)["review_result"]
    assert factory.return_value.invoke.call_count == 2
    assert result["review_complete"] is False
    assert "review_evidence" in result


def test_recorded_finding_cannot_downgrade_a_deterministic_blocker(submission):
    cfg, ledger, tools = setup_review(submission)
    ledger.record(PhaseEvidenceInput(phase="3", status="FAIL", summary="Issue", citations=[read(tools, "test.patch")], findings=[{
        "severity": "MINOR", "finding": "Known blocker", "citations": [read(tools, "test.patch")],
    }]))
    result = _apply_evidence_reports(approved_review(), {
        "config": cfg, "review_evidence": ledger.snapshot(), "findings": [{
            "phase": "3", "severity": "BLOCKER", "finding": "Known blocker", "evidence": "Mechanical check",
        }],
    })
    assert len(result.findings) == 1
    assert result.findings[0].severity == "BLOCKER"
    assert result.findings[0].evidence == "Mechanical check"


def test_failed_model_callback_is_idempotent():
    budget = ReviewBudget(2, 0, 0)
    callback = budget.callback()
    rid = uuid4()
    callback.on_chat_model_start({}, [], run_id=rid)
    callback.on_llm_error(RuntimeError("failure"), run_id=rid)
    callback.on_llm_error(RuntimeError("duplicate"), run_id=rid)
    assert budget.snapshot()["failed_model_calls"] == 1
    assert budget.snapshot()["calls_without_token_usage"] == 1


def test_late_findings_can_escalate_pass_without_erasing_prior_evidence(submission):
    _, ledger, tools = setup_review(submission)
    cite = read(tools, "test.patch")
    ledger.record(PhaseEvidenceInput(phase="3", status="PASS", summary="Initial review", citations=[cite]))
    finding = {"severity": "MAJOR", "finding": "Late edge case", "citations": [cite]}
    ledger.record(PhaseEvidenceInput(phase="3", status="FAIL", summary="Edge case discovered", citations=[cite], findings=[finding]))
    assert ledger.snapshot()["phases"]["3"]["findings"][0]["finding"] == "Late edge case"
    with pytest.raises(ValueError, match="overwritten"):
        ledger.record(PhaseEvidenceInput(phase="3", status="FAIL", summary="Erase finding", citations=[cite]))
    assert ledger.snapshot()["phases"]["3"]["findings"]


def test_followup_can_add_supporting_sources_but_cannot_remove_any(submission):
    _, ledger, tools = setup_review(submission)
    first = read(tools, "test.patch")
    second = read(tools, "solution.patch")
    ledger.record(PhaseEvidenceInput(phase="3", status="PASS", summary="Initial", citations=[first]))
    ledger.record(PhaseEvidenceInput(phase="3", status="PASS", summary="Compared solution", citations=[first, second]))
    with pytest.raises(ValueError, match="overwritten"):
        ledger.record(PhaseEvidenceInput(phase="3", status="FAIL", summary="Remove source", citations=[first]))
    assert len(ledger.snapshot()["phases"]["3"]["citations"]) == 2
