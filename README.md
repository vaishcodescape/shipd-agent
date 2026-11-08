<div align="center">

# >- shipd-agent

**From submission to evidence-backed review.**

A code agent for Shipd quests .<br>
Docker checks · rubric-guided reasoning · inspectable feedback · opt-in submission

[![CI](https://github.com/vaishcodescape/shipd-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/vaishcodescape/shipd-agent/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![MIT license](https://img.shields.io/badge/License-MIT-22c55e)](LICENSE)

[Get started](#get-started) · [Terminal preview](#terminal-preview) · [Review approach](#how-it-models-a-human-quality-review) · [Contribute](CONTRIBUTING.md)

</div>

Reviewing a submission means moving between setup scripts, test failures, patches, agent results, and a detailed rubric. **shipd-agent brings those steps into one workflow**, then saves structured feedback you can inspect before posting.

Built for Shipd reviewers with access to the review queue, and developers who want to study or extend a LangGraph + Deep Agents review pipeline. Independent community project; not an official Shipd or Datacurve product.

## Why use it?

| Capability | What it gives you |
| --- | --- |
| **Check before reasoning** | Docker builds, patch checks, and the base/new/solution test contract before the LLM review |
| **Review against a rubric** | Problem, Tests, and Solution ratings with findings, evidence, and suggested fixes |
| **Choose your provider** | Anthropic, OpenAI, Google, DeepSeek, Moonshot, xAI, or an OpenAI-compatible endpoint |
| **Inspect before posting** | A JSON review bundle; feedback submission is off by default |
| **Keep track of batches** | Saved progress, cooldowns, and session statistics |
| **Adapt the workflow** | Separate modules for browser sessions, deterministic checks, reasoning, and submission |

## Terminal preview

![Shipd terminal review preview showing workflow phases and an opt-out submission step](docs/assets/terminal-review.svg)

*Offline preview rendered by the actual CLI UI functions with example statuses. It does not represent a live review or a benchmark.*

<details>
<summary><strong>See the command-line help</strong></summary>

![Shipd CLI help showing setup, provider configuration, and run options](docs/assets/terminal-help.svg)

*Captured from `./run.sh --help`. Regenerate both images with the [preview script](scripts/render_terminal_previews.py).*

</details>

## Get started

You’ll need **Python 3.11+**, Bash, Git, ripgrep (`rg`), a running Docker daemon, a Shipd reviewer account, an IMAP app password for its verification mailbox, and an API key for a supported LLM provider. The shell launcher targets macOS/Linux; Windows users need a Bash environment such as WSL. CI is configured for Python 3.11 and 3.14 on Linux.

**1. Install from source**

```bash
git clone https://github.com/vaishcodescape/shipd-agent.git
cd shipd-agent
./run.sh --setup
cp .env.example .env
```

Edit `.env`: set `AUTH_EMAIL` and `AUTH_PASSWORD`. The password is your mailbox’s **IMAP app password**, used to retrieve the Shipd sign-in code. Keep quoted passwords quoted.

**2. Choose a provider and check your environment**

```bash
./run.sh --configure-llm
./run.sh --check
```

The setup wizard asks for exploration and finalization models, then stores your API key in the OS keychain (with a restricted local file fallback). See [configuration and troubleshooting](docs/configuration.md).

**3. Run one review and keep the evidence**

```bash
./run.sh --once --no-submit --no-cleanup --headed
```

This is a **live workflow**: it signs in, clocks in, reserves or continues a submission, executes Quick Setup, runs Docker checks, and calls your LLM provider. `--no-submit` prevents posting review feedback; it is not an offline mode. `--no-cleanup` keeps the clone and Docker artifacts for inspection.

Quick Setup executes the submission’s shell script on your machine. Use a dedicated development environment for submissions you do not trust; Docker test execution does not isolate the setup script itself.

**4. Inspect the result**

Open `reviews/pending-submit.json` for the latest completed review bundle. Look at the decision, phase results, band ratings, evidence, and contributor feedback. This file is overwritten by the next saved review; copy it if you want to keep a history.

```bash
./run.sh --status
```

When ready to enable posting for a live review:

```bash
./run.sh --once --submit
```

This starts a workflow run; it does not simply upload the JSON you inspected.

## Assessment checks

The code includes the criteria reported in community assessment feedback:

| Check | Implemented behavior |
| --- | --- |
| **Prompt length** | Flags explicit task-prompt files over **1,000 words** |
| **Too Many Requirements** | Reads the platform flag and estimates independent requirements locally |
| **Agent runs** | At least **4 finished unhinted runs** each for Nova, Vega, and Orion. More is allowed. Hinted runs do not count |
| **Passing runs** | **One model only**, above 0% and at most **25%**. The other two stay at 0%. A Nova-only solve is a warning |
| **Long horizon** | Olympus: Nova average **sequence length above 400,000** tokens. There is no solution-LOC floor |
| **Auto Review (prediction)** | Last stage: one model call reads the whole draft review and flags places where the prompt, tests, solution, and runs disagree. A concrete MAJOR blocks approve. Labeled a prediction, not Shipd's verdict |

The requirement-count threshold defaults to 10 and is a **local heuristic**, not a published Shipd threshold. Existing run results are read without clicking evaluation controls. The agent does not launch Nova, Vega, or Orion runs.

Missing data and an aggregate-only run summary have limitations: see the [criteria guide](docs/assessment-checks.md) before treating a PASS as complete verification. Assessment rules can change; this repository’s checks are not a guarantee of acceptance.

## How it models a human quality review

A good reviewer does more than check whether tests are green. They ask whether the problem is clear, the tests are fair, the solution fits the repository, and the evidence supports the verdict. The supplied [review rubric](shipd-rubric.md) makes that process explicit enough for an AI agent to follow.

**The rubric is the review specification.** It defines what to inspect, how to rate it, what counts as evidence, and when to approve, request changes, or reject. shipd-agent combines that specification with repository tools and deterministic checks:

| Human review habit | How the agent follows it |
| --- | --- |
| **Reproduce before judging** | **Phase 0:** checks the base commit, patch application, Docker setup, and test behavior before and after the solution |
| **Understand the actual request** | **Phase 1:** assesses clarity, scope, repository fit, discoverability, and whether the prompt leaks the solution |
| **Inspect the test harness** | **Phase 2:** reviews Dockerfile and `test.sh`, including whether the intended base and new tests actually run |
| **Challenge the tests** | **Phase 3:** looks for missing edge cases, brittle assertions, hidden requirements, and tests that reject valid alternative solutions |
| **Review like a maintainer** | **Phase 4:** judges correctness against the problem, repository conventions, security, maintainability, and unnecessary changes |
| **Investigate agent outcomes** | **Phase 5:** uses available run results and failure evidence to assess solvability and fairness; a pass rate alone is insufficient |
| **Make the overall judgment** | **Phase 6:** considers platform checks, related submissions, eligibility, and Mars/Olympus fit using available evidence |
| **Read it as a whole** | **Auto Review (prediction):** after scoring, rereads the submission together and flags parts that disagree, ahead of Shipd's own Auto Review |

The Deep Agents reviewer can read numbered source pages, search the repository, inspect patches, and delegate focused investigations. Each phase records structured evidence against successful read receipts. A bounded coverage recheck targets missing phase reports. The finalization model turns that evidence into **Problem / Tests / Solution scores (0–3)**, confidence levels, severity-tagged findings, and concrete feedback. Deterministic decision guards then constrain the result so known failures cannot be waved away by a positive LLM summary.

**Example of the intended reasoning:** a new test expects sorted output, but neither the prompt nor existing repository behavior requires sorting. A reviewer should identify a possible hidden requirement, cite the assertion and relevant prompt text, and explain the fix. Under the rubric, a confirmed, fixable fairness defect calls for `request_changes`; the contributor should get a chance to repair it. This is an illustrative example, not a captured review result.

Every finding is expected to point to a file/line, test output, or log evidence. The rubric treats uncertainty as a reason to gather more evidence or request changes, and reserves rejection for extreme cases such as confirmed duplication or no credible fix path.

This reproduces a **review process**, with explicit criteria and inspectable reasoning. It does not establish that the agent matches expert human accuracy: models can miss defects, misread intent, or overstate confidence, and platform evidence can be incomplete. Inspect the saved review and its cited evidence before relying on the verdict. A reproducible evaluation against expert-reviewed examples is a [proposed next step](docs/roadmap.md).

## How it works

```mermaid
flowchart LR
    A[Sign in and reserve] --> B[Quick Setup clone]
    B --> C[Docker and patch checks]
    C --> D[Deep Agents evidence collection]
    E[Existing platform results] --> D
    D --> I[Final scorer]
    I --> J[Auto Review prediction]
    J --> K[Rubric guards]
    K --> F[Structured review bundle]
    F --> G[Inspect locally]
    F --> H[Submit when enabled]
```

The review pipeline covers phases 0–6 of the [bundled rubric](shipd-rubric.md). Deterministic checks feed into the LLM review, which gathers repository evidence and produces a structured verdict. Approval guards account for failed phases, blocking findings, and low band scores.

| Output | Where to find it |
| --- | --- |
| Latest completed review and submission context | `reviews/pending-submit.json` |
| Run log | `logs/run.log` by default |
| Batch resume state | `logs/watch-batch.json` |
| Form failure screenshots and diagnostics | `logs/debug-submit/` |
| Cloned submissions | `submissions/` (keep with `--no-cleanup`) |

## Deep Agents review harness

Deep Agents is the primary review harness. The outer LangGraph workflow keeps ownership of Docker execution, deterministic rubric rules, final validation, and submission. Rerun `./run.sh --setup` after upgrading to install the pinned harness and compatible LangChain packages. Python 3.11 is the minimum supported version (3.14 recommended).

The reviewer can delegate substantial investigations to harness/tests and solution workers. All workers share one model-call budget and a private evidence ledger. `REVIEW_MAX_TOOL_STEPS` retains its old name but now counts model calls across the parent and workers (default 15); a coverage recheck can use up to `REVIEW_COVERAGE_RECHECK_MAX_STEPS` more (default 6), with two additional calls reserved for final scoring and its validation retry. `REVIEW_MAX_TOTAL_TOKENS` (default 200,000) stops new calls once provider-reported usage reaches the limit; already running calls can overshoot, and missing usage is recorded explicitly. These are application-level limits; provider-internal HTTP retries follow the SDK policy.

Submission files are accessed through scoped, read-only tools. `read_submission_file` returns numbered pages, citation receipts, and a continuation line for large files. Deep Agents' built-in filesystem is separate, ephemeral scratch storage: it has no host shell, live browser, persistent memory, or repository write access. Workers receive cached Shipd page data rather than Playwright objects.

Each phase report must cite successful reads of its required artifacts. Citation paths, excerpts, and line ranges come from immutable read receipts. Later investigations may add evidence or escalate a phase to FAIL, while retaining all existing citations and findings; accepted failures cannot be downgraded. This checks provenance and artifact coverage; semantic correctness still depends on review quality. Verified reports and excerpts reach the final scorer without transcript clipping and are saved as `review_evidence` in the review bundle, alongside model-call/token accounting in `review_usage`. If structured evidence exceeds the configured final payload budget, the review is marked incomplete instead of slicing JSON or dropping citations. Missing required reports force `request_changes` with an explicit coverage note, preserving the existing submission policy.

See the [integration validation report](docs/deep-agents-validation.md) for tested behavior, coverage, and sandbox limitations.

## Everyday commands

```bash
./run.sh --help                          # All options
./run.sh --once --quest mars --no-submit # Review a Mars submission
./run.sh --once --no-submit --provider openai
./run.sh --reviews 5 --submit            # Five live reviews with posting enabled
./run.sh --status                        # Session summary and batch progress
./run.sh --configure-llm                 # Change provider, models, or key
```

**Already have a submission cloned?** Run the review entry point directly, without signing in or reserving through the browser:

```bash
.venv/bin/python shipd-agent/review_agent.py /path/to/submission \
  --quest olympus --output reviews/local-review.json
```

This still runs checks and calls the configured LLM. Browser-only platform evidence is unavailable. Its `--dry-run` option skips LLM calls but still runs Phase 0; an incomplete dry run returns a nonzero exit code.

## Make it your own

Start with the part you want to improve:

| You want to… | Start here |
| --- | --- |
| Update assessment rules | [`rubric_defaults.py`](shipd-agent/review/rubric_defaults.py) and the [rubric](shipd-rubric.md) |
| Improve prompt/run checks | [`prompt_checks.py`](shipd-agent/review/prompt_checks.py), [`agent_runs_checks.py`](shipd-agent/review/agent_runs_checks.py) |
| Add a provider | [`llm_providers.py`](shipd-agent/review/llm_providers.py) |
| Change agent behavior | [`deep.py`](shipd-agent/review/deep.py), [`evidence.py`](shipd-agent/review/evidence.py), [`graph.py`](shipd-agent/review/graph.py) |
| Fix browser interactions | [`workflow/`](shipd-agent/workflow), [`scrape.py`](shipd-agent/review/scrape.py) |

Read the [contribution guide](CONTRIBUTING.md) for setup and tests, or browse [proposed improvements](docs/roadmap.md). Small fixes, anonymized UI fixtures, and clearer setup instructions are welcome.

**If shipd-agent helps your review workflow, give it a star.** Fork it to try a provider, improve a check, or build a better reviewer — and send useful changes back as a pull request.

[Report a bug](https://github.com/vaishcodescape/shipd-agent/issues/new?template=bug_report.yml) · [Suggest an improvement](https://github.com/vaishcodescape/shipd-agent/issues/new?template=feature_request.yml) · [MIT license](LICENSE)
