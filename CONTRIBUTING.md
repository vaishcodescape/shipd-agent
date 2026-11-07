# Contributing to shipd-agent

Thanks for helping make review automation easier to inspect and more reliable. Useful contributions include reproducible bug reports, anonymized browser fixtures, provider fixes, rubric updates, and onboarding improvements.

## Set up your fork

Fork the repository on GitHub, clone your fork, and run from its root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m playwright install chromium
```

Use Python 3.11 or newer (3.14 recommended). Bash, Git, and ripgrep are needed for the launcher/repository tools. Docker and Shipd credentials are needed for a live review, but not for the mocked test suite or synthetic browser fixtures.

## Check your changes

```bash
python -m pytest -ra
python -m pip install ruff==0.17.0 shellcheck-py==0.11.0.1
ruff check .
shellcheck --severity=warning run.sh scripts/shipd.sh
./run.sh --help
```

Browser tests may skip when Chromium is unavailable. To require them:

```bash
SHIPD_REQUIRE_BROWSER=1 python -m pytest -ra
```

CI also builds and imports the wheel. For packaging changes, run:

```bash
python -m pip install build twine
python -m build
python -m twine check --strict dist/*
```

## Pick a starting point

| Area | Useful contribution |
| --- | --- |
| Onboarding | Reproduce installation on a fresh supported environment |
| Browser automation | Add a synthetic HTML fixture for a changed Shipd layout |
| Assessment checks | Add a focused boundary case when the rubric changes |
| Providers | Test tool calling and structured output with an accessible model |
| Review artifacts | Improve evidence readability and explain missing data |

See the [proposed roadmap](docs/roadmap.md) for larger ideas. Discuss large changes in an issue before investing in an implementation.

## Browser interaction rules

Reading a platform panel must not click buttons or launch evaluations. Scope form actions to the verified review form, fail when the scope is missing, and test with unrelated controls elsewhere on the page. Avoid replaying clicks after navigation timeouts: the first action may already have succeeded.

Use synthetic pages for regressions. A test for an unwanted paid action should assert that the action was **never** triggered, including after repeated reads.

## Before opening a pull request

Explain the user-visible problem, the new behavior, and how you tested it. Link the related issue and include a reproduction or fixture for bug fixes. Keep `.env`, session cookies, private submissions, and debug captures with personal information out of commits.

Update the README or guides when commands, defaults, or assessment rules change. Provider/model IDs in this repository are defaults, not claims about account availability.

## Regenerate terminal previews

```bash
python -m pip install rich
python scripts/render_terminal_previews.py
```

The preview script uses the actual CLI renderer with clearly labeled example statuses and captures the real help text. It never signs in, reserves a submission, starts a platform evaluation, or calls an LLM.
