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

Each of Nova, Vega, and Orion needs at least four finished unhinted runs. More than four is allowed. Hinted runs never count. Exactly one model solves the task, at a rate above 0% and at most 25%. The other two stay at 0%.

Olympus long horizon is Nova's average sequence length above 400,000 tokens. The page must label that figure as sequence length. A generic token count does not count. Averages above 400,000 raise the scope share of the reward through 500,000; that cap is reported, not a second failure. Olympus has no solution-LOC floor and no file or message median. Mars still requires a median of at least 100 LOC when that figure is shown.

A Nova-only solve that otherwise meets the bar is a **warning**, not a failure, until Shipd settles a disagreement between the criteria panel and The Process.

| Example | Vega | Nova | Orion | Verdict |
| --- | --- | --- | --- | --- |
| One model at 25%, Nova above 400K | 1/4 | 0/4 | 0/4 | Pass |
| 2 of 8 is still 25% | 2/8 | 0/4 | 0/4 | Pass |
| 1 of 5 is 20% | 1/5 | 0/4 | 0/4 | Pass |
| 2 of 4 is 50% | 2/4 | 0/4 | 0/4 | Fail |
| Two models solve it | 1/4 | 1/4 | 0/4 | Fail |
| Nova average 300K | 1/4 | 0/4 | 0/4 | Fail |
| Nova is the only solver | 0/4 | 1/4 | 0/4 | Pass, with a warning |
| Fewer than 4 unhinted runs | 1/4 | 0/3 | 0/4 | Fail |

Fractions mean **passing runs / unhinted runs**. The reviewer reads existing results. It does not schedule these evaluations or click run/re-run controls.

## Auto Review prediction

Shipd's Auto Review is the last and hardest stage, and its criteria are not published. The agent predicts it in a final stage that runs after the scored review and before the decision guards. One structured model call reads the draft decision and scores, all earlier findings, the prompt, `test.patch`, `solution.patch`, the agent runs, and the expected agent line-up. It looks for parts that disagree.

- A concrete, cited contradiction is **MAJOR**. It makes the `auto_review` stage FAIL, which turns an approve into request_changes.
- Speculative notes are **QUESTION** and do not fail the stage.
- A test patch with an empty or missing solution is a local MAJOR, even when the model is unavailable.
- When the model errors or `REVIEW_AUTO_REVIEW=0`, the stage is skipped. A skip is not a coverage gap.

Every finding is labeled "Auto Review prediction" and is not Shipd's verdict. `REVIEW_AUTO_REVIEW_MODEL` picks the model and defaults to `REVIEW_MODEL`.

The agent line-up (Vega, Orion, Nova) is a guess, because Shipd does not publish it. Override it with `REVIEW_VEGA_MODEL`, `REVIEW_ORION_MODEL`, and `REVIEW_NOVA_MODEL`, or with the `REVIEW_AGENT_MODELS` JSON. These names are recorded with the review for staleness and never decide a pass or a fail.

## Evidence limits

An unavailable or unreadable Agent Runs panel returns **SKIP**. An aggregate-only line such as `1/12 pass` also returns **SKIP**. It cannot show per-agent unhinted counts or the Nova average, so it is not a pass. When the solve bar is met but sequence length is not labeled, Olympus returns **SKIP** for that missing bar. Cheat rate, fair-task flags, and environment blockers are each skipped when the page does not show them; the summary says so instead of calling them passed.

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
