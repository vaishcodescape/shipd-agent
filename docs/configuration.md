# Configuration

[← Back to README](../README.md)


The review agent works with any of these providers. Anthropic stays the default, so an existing `.env` with `ANTHROPIC_API_KEY` keeps working unchanged. If you installed before multi-provider support, rerun `./run.sh --setup` once to add `langchain-openai`, `langchain-google-genai` and `keyring`.

| Provider | `REVIEW_PROVIDER` | API key environment variable |
| --- | --- | --- |
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` |
| OpenAI | `openai` | `OPENAI_API_KEY` |
| Google | `google` | `GEMINI_API_KEY` or `GOOGLE_API_KEY` |
| DeepSeek | `deepseek` | `DEEPSEEK_API_KEY` |
| Moonshot | `moonshot` | `MOONSHOT_API_KEY` |
| xAI | `xai` | `XAI_API_KEY` |
| OpenAI-compatible endpoint | `openai-compatible` | `REVIEW_LLM_API_KEY` |

The **explore** model drives the Deep Agents reviewer and its focused workers. The **finalize** model (`REVIEW_MODEL`) writes the structured verdict from verified phase reports. Choose a model your account can access that supports the tools and structured output this pipeline uses. Repository defaults live in [`llm_providers.py`](../shipd-agent/review/llm_providers.py); they are not a guarantee of provider availability. After upgrading, rerun `./run.sh --setup` to install the pinned Deep Agents and compatible LangChain packages.

**Interactive setup (recommended)**

```bash
./run.sh --configure-llm
```

This asks for the provider, an explore model and a finalize model (pick from a list or type any ID), then the API key. Key input is hidden.

- The key is saved to the OS keychain: macOS Keychain on a Mac, Windows Credential Locker, or Secret Service on Linux.
- With no keychain available, it goes to `~/.config/shipd-agent/credentials`, with the file at `0600` and the directory at `0700`.
- The key is never written to `.env`, never printed or logged (`--check` shows only the last 4 characters), and never passed as a command-line argument.
- The provider and model choices are saved to `.env` as `REVIEW_PROVIDER`, `REVIEW_EXPLORE_MODEL` and `REVIEW_MODEL`, so later runs reuse them.

**First run:** when a review run finds no API key for the selected provider, it runs this same setup before anything else, saves the choices without asking, and then continues the run. If you passed `--provider`, `--model` or `--explore-model`, it only asks for the key. Once a key is found (env var or stored), runs start straight away. In cron, CI or with piped stdin there is no terminal to ask on, so the run stops right away with an error that says what to set.

**Key lookup order:** the provider's env var (shell or `.env`) first, then the stored key.

**Per-run overrides**

```bash
./run.sh --once --no-submit --provider openai                    # provider defaults
./run.sh --once --no-submit --provider gemini --model gemini-3.8-flash
./run.sh --once --no-submit --explore-model claude-haiku-5-5     # keep provider, change explore model
./run.sh --check --provider deepseek                             # is that provider ready?
./run.sh --forget-llm-key                                        # remove the stored key (current provider)
./run.sh --forget-llm-key --provider xai                         # ...or a specific one
```

If you switch provider with `--provider` and leave out `--model` and `--explore-model`, that provider's default models are used, not the models saved in `.env`.

**Provider notes**

- **OpenAI:** calls go through the Responses API, as configured by this project’s provider adapter.
- **Gemini:** uses the native Gemini API (`langchain-google-genai`), which keeps Gemini 3+ thought signatures intact across tool calls.
- **DeepSeek:** runs in non-thinking mode, because thinking mode rejects the forced tool choice that structured output needs.
- **Kimi K3:** gets `tool_choice="required"` and has its `reasoning_content` passed back on each turn.
- **Reasoning effort:** OpenAI, Gemini and Kimi run at `REVIEW_REASONING_EFFORT=low` by default. Thinking tokens count toward `REVIEW_*_MAX_OUTPUT_TOKENS`.
- **Escape hatches:** `REVIEW_STRUCTURED_OUTPUT_METHOD` (`function_calling`, `json_schema` or `json_mode`) and `REVIEW_LLM_BASE_URL` (proxies and regional endpoints).

**Adding a provider:** if it has an OpenAI-compatible API, append one `ProviderSpec` entry with its base URL, key env var and default models to [`llm_providers.py`](../shipd-agent/review/llm_providers.py).


## Budgets and artifacts

See [`.env.example`](../.env.example) for all settings. Start with one review and inspect the result before increasing batch size.

| Setting | Purpose |
| --- | --- |
| `REVIEW_MAX_TOOL_STEPS` | Shared model calls across the reviewer and workers (legacy variable name; default 15) |
| `REVIEW_COVERAGE_RECHECK_MAX_STEPS` | Additional shared model calls for missing phase reports (default 6); two more calls are reserved for final scoring and retry |
| `REVIEW_MAX_TOTAL_TOKENS` | Stop new calls once provider-reported review usage reaches the limit (default 200,000); in-flight calls can overshoot |
| `REVIEW_EXPLORE_MAX_OUTPUT_TOKENS` | Cap exploration output per call |
| `REVIEW_FINALIZE_MAX_OUTPUT_TOKENS` | Cap finalization output per call |
| `REVIEW_PHASE0_TEST_TIMEOUT` | Bound each test invocation |
| `REVIEW_PHASE0_DOCKER_BUILD_TIMEOUT` | Bound the Docker build |
| `CLEANUP_AFTER_REVIEW=0` | Keep cloned repositories and Docker artifacts |

These limits bound individual operations; they are not a monetary spending cap. LLM API usage is billed by your provider. The reviewer reads existing Nova/Vega/Orion results; it has no supported command for launching those evaluations.

Review bundles include `review_evidence` with verified citation excerpts and `review_usage` with shared call counts, reported tokens, and missing usage. Provider-internal HTTP retries follow the client SDK policy. See the [review harness](../README.md#deep-agents-review-harness) and [validation report](deep-agents-validation.md) for details.

## Troubleshooting

| Symptom | Next step |
| --- | --- |
| Setup or imports fail | Use Python 3.11+ (3.14 recommended); rerun `./run.sh --setup` in the checkout |
| Chromium cannot launch | Run `.venv/bin/python -m playwright install chromium`; on Linux, install required system libraries with `playwright install --with-deps chromium` |
| Docker check fails | Start your Docker daemon and confirm `docker info` succeeds |
| Sign-in cannot retrieve OTP | Check mailbox IMAP settings and the app password in `.env` |
| Provider rejects a model | Run `./run.sh --configure-llm` and select an accessible model |
| Review stops on a completed status | Open an active reservation in Shipd; the guard deliberately stops on visible approval/rejection labels |
| Platform data is missing | Inspect the page with `--headed`; unavailable evidence needs manual review |
| Form selection cannot find its scope | Inspect `logs/debug-submit/`; do not broaden selectors to unrelated page controls |

Before sharing diagnostics, remove credentials, session cookies, private submission content, and personal information.
