# Proposed improvements

[← Back to README](../README.md)

These are contribution ideas, not shipped features or promised release dates. Prioritize changes that make the first successful review easier and failures easier to understand.

| Priority | Improvement | User benefit | Completion criterion |
| --- | --- | --- | --- |
| 1 | Offline sample submission and report | Try the workflow before configuring accounts | One documented command generates a labeled sample without network access |
| 1 | Strict handling of incomplete platform evidence | Know exactly what a PASS verifies | Missing per-agent splits cannot be mistaken for a verified 4/4/4 matrix |
| 1 | Versioned assessment profiles | Understand which rules a report used | Every saved report identifies its rubric version and effective thresholds |
| 2 | Immutable review history | Compare runs without copying the latest JSON | Each completed review has a unique artifact path |
| 2 | Usage accounting and budget stops | Control provider spend during batches | Actual token usage is recorded and new calls stop at configured limits |
| 2 | Installable CLI entry point | Launch reliably after package installation | A documented console command works outside the source checkout |
| 3 | Fixture-based provider evaluation | Compare review quality with reproducible evidence | Public, synthetic fixtures and repeatable quality/cost measurements |

The current repository already includes deterministic checks, provider selection, saved batch progress, and opt-in feedback submission. Start with the [contribution guide](../CONTRIBUTING.md) to build on them.
