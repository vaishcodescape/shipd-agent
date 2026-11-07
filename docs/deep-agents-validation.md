# Deep Agents integration validation

Validated on 2026-11-07 using Python 3.14.2 and Deep Agents 0.7.34.

## Results

| Check | Result |
| --- | --- |
| Sandbox regression suite | 352 passed; 15 browser tests skipped; 2 terminal tests explicitly deselected |
| New Deep Agents feature cases | 60 passed |
| Existing model-factory/provider cases, migrated to the production harness | 23 passed |
| Statement coverage across `review/deep.py` and `review/evidence.py` | 237/237, 100% |
| Branch coverage across those two modules | 75/76, 98.68% |
| Ruff and `git diff --check` | Passed |
| `./run.sh --help` | Passed |
| Distribution build | Source distribution and wheel built offline using installed setuptools |
| Installed-wheel import smoke test | All 48 Python modules imported from the wheel installation; tests excluded; Python floor and harness pin verified |

Coverage percentages above apply to the two new runtime modules, not the whole repository.
The wheel smoke test used the same dependency environment as the tests, not a fresh dependency installation.

## Feature cases

`shipd-agent/tests/test_deep_review.py` runs the real Deep Agents graph with scripted
models. Provider transport cases in `test_llm_factory.py` use mocked HTTP or SDK
responses through the production harness. They do not call paid model endpoints.

The cases cover:

- Direct and delegated evidence collection, shared usage accounting, parallel call limits, and reserved scoring calls.
- Exhaustion during a worker investigation, reported-token limits, provider errors, duplicate callback events, and absent token usage.
- Recheck allowances and preservation of the harness scratch filesystem between invocations.
- Private scratch writes, repository path escapes, external symlinks in reads/listings/search fallback, and option-looking ripgrep patterns.
- Large-file pagination, page boundaries, blank and empty files, missing files, read errors, and oversized source lines.
- Forged receipt IDs, wrong artifact citations, negative/reversed/out-of-page line ranges, absent mandatory artifacts, and oversized citations/reports.
- Different platform panel requirements, absent-data placeholders, genuine negative platform results, and legitimate SKIP verdicts.
- Concurrent report writes, detached snapshots, per-review isolation, and preservation of accepted findings.
- Late evidence additions and PASS-to-FAIL escalation without deleting prior citations or findings.
- Preservation of deterministic blockers against weaker duplicate findings and an approving scorer.
- JSON payload limits, Unicode/escaping, exact excerpts beyond the former 600-character clipping limit, and explicit overflow failures.
- Full graph execution through real structured-output parsing, scorer retry, validation, and saved-bundle round trips.
- Scorer initialization/validation failures, retained audit information, dry runs, and the configured Phase 0 short circuit.

## Reproduce

Use a Python environment with the project, pytest, and pytest-cov installed.
From the repository root:

```bash
python -m pytest -q shipd-agent/tests/test_deep_review.py shipd-agent/tests/test_llm_factory.py \
  --cov=review.deep --cov=review.evidence --cov-branch --cov-report=term-missing
```

For the sandbox regression run, Chromium launch fails with macOS Mach-port
permission denial, and two pseudo-terminal cases fail because `/dev/tty` is
inaccessible. The exact restricted run was:

```bash
SHIPD_REQUIRE_BROWSER=0 python -m pytest -q --tb=short \
  --deselect=shipd-agent/tests/test_llm_setup.py::ShellFirstRunTests::test_configured_user_goes_straight_to_the_run \
  --deselect=shipd-agent/tests/test_llm_setup.py::ShellFirstRunTests::test_new_user_sets_up_llm_first_then_the_run_continues \
  --cov=review.deep --cov=review.evidence --cov-branch --cov-report=term-missing
```

CI still requires Chromium on its Python 3.14 job and runs the terminal tests.
The minimum-version CI job now targets Python 3.11. Neither that interpreter nor
the 17 sandbox-blocked cases was verified locally in this session.

No live Shipd submission or paid model benchmark was run. These checks establish
integration behavior and failure handling; they do not establish a speedup,
lower cost, or human-level semantic review accuracy.
