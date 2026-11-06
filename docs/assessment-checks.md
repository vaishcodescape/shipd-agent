# Assessment checks

[← Back to README](../README.md)

This guide describes the rules implemented in this checkout, based on reported assessment feedback and the bundled rubric. It is not independently verified platform policy. Confirm changes against the assessment instructions you receive.

## Prompt quality

The local word limit is **1,000 words**. The checker measures explicit prompt files (`problem.md`, `PROBLEM.md`, `description.md`, or `prompt.md`), not the target repository’s README. If it cannot find a prompt, it cannot verify its length.

“Too Many Requirements” has two inputs:

- The platform’s visible Holistic Check flag, when available.
- A local estimate based on list items, modal verbs, and imperative sentences, ignoring code fences and headings.

The estimate defaults to flagging more than **10** requirements. This number is a project heuristic, not an official published threshold. The current implementation makes a MAJOR finding and fails this part of Phase 1 when it exceeds the threshold. Review the evidence: lists of background facts can overcount, and complex prose can undercount.

Configure `REVIEW_MAX_PROMPT_WORDS` and `REVIEW_MAX_PROMPT_REQUIREMENTS` in `.env`. A focused prompt can still need work on clarity, testability, or repository fit even when these checks pass.

## Agent run matrix

Each of Nova, Vega, and Orion must have four runs. Exactly one of the 12 runs passes. The winning run can belong to any one of the three agents.

| Example | Nova | Vega | Orion | Run-count/pass-count check |
| --- | --- | --- | --- | --- |
| One passing run | 1/4 | 0/4 | 0/4 | Pass |
| Different winning agent | 0/4 | 0/4 | 1/4 | Pass |
| Two passes in one type | 2/4 | 0/4 | 0/4 | Fail |
| Passes across types | 1/4 | 1/4 | 0/4 | Fail |
| Zero passes | 0/4 | 0/4 | 0/4 | Fail |
| Incomplete evaluation | 1/4 | 0/3 | 0/4 | Fail |

Fractions mean **passing runs / runs**, not completed / scheduled. Other Phase 5 checks, including available scope medians, still apply.

The reviewer reads existing results. It does not schedule these evaluations or click run/re-run controls. Hidden panel content may need manual inspection because scraping does not expand controls.

## Evidence limits

An unavailable or unreadable Agent Runs panel returns **SKIP**. An aggregate-only result such as `1/12 pass` can currently return **PASS with a note that the per-agent split is unverified**. That does not prove four runs per type. Inspect the split manually before relying on it.

## Updating the rules

Keep these files aligned when rules change:

- [Rubric](../shipd-rubric.md) and [shared defaults](../shipd-agent/review/rubric_defaults.py)
- [Prompt checks](../shipd-agent/review/prompt_checks.py) and [agent run checks](../shipd-agent/review/agent_runs_checks.py)
- [Configuration](../.env.example) and this guide
- Regression tests for boundary cases and propagation into the final review

```bash
python -m pytest shipd-agent/tests/test_prompt_checks.py \
  shipd-agent/tests/test_agent_runs_checks.py \
  shipd-agent/tests/test_assessment_criteria_flow.py
```
