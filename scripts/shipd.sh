# Run the Shipd agent

set -euo pipefail

# macOS libmalloc otherwise prints "MallocStackLogging: can't turn off malloc
# stack logging because it was not enabled" from every child process (python,
# git, docker, bash, chromium) that inherits these debug vars. Drop them so the
# run log stays clean; harmless no-op when they are already unset or on Linux.
unset MallocStackLogging MallocStackLoggingNoCompact MallocStackLoggingDirectory

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
AGENT_DIR="${ROOT_DIR}/shipd-agent"
VENV_DIR="${ROOT_DIR}/.venv"
ENV_FILE="${ROOT_DIR}/.env"
LOG_DIR="${ROOT_DIR}/logs"
DEFAULT_LOG_FILE="${LOG_DIR}/run.log"
DEFAULT_INTERVAL=1200

QUEST="olympus"
SEPARATE_STEPS=0
ONCE=0
SKIP_PROMPT=0
MAX_RUNS_EXPLICIT=0
INTERVAL=""
MAX_RUNS=""
HEADED=0
NO_CLONE=0
NO_CLEANUP=0
REVIEW=1
SUBMIT=1
SUBMIT_EXPLICIT=0
FOREGROUND=0
CLONE_DIR=""
LOG_FILE=""
EXTRA_ARGS=()
SHUTDOWN=0
FRESH=0
RESUME_BATCH=0

# Terminal UX state (colors/spinners when stdout is a TTY; plain otherwise).
USE_TTY_UI=0
C_RESET="" C_BOLD="" C_DIM="" C_RED="" C_GREEN="" C_YELLOW="" C_BLUE="" C_CYAN="" C_MAGENTA=""
CURRENT_REVIEW=0
CURRENT_REVIEW_TOTAL=0

usage() {
    cat <<'EOF'
EOF
}

init_ui() {
    if [[ -t 1 ]] && [[ "${SHIPD_NO_COLOR:-0}" != "1" ]] && [[ "${SHIPD_PLAIN:-0}" != "1" ]]; then
        USE_TTY_UI=1
        C_RESET=$'\033[0m'
        C_BOLD=$'\033[1m'
        C_DIM=$'\033[2m'
        C_RED=$'\033[31m'
        C_GREEN=$'\033[32m'
        C_YELLOW=$'\033[33m'
        C_BLUE=$'\033[34m'
        C_CYAN=$'\033[36m'
        C_MAGENTA=$'\033[35m'
    fi
}

load_env_file() {
    local env_path="$1"
    [[ -f "${env_path}" ]] || return 0

    local py=python3
    if [[ -x "${VENV_DIR}/bin/python3" ]]; then
        py="${VENV_DIR}/bin/python3"
    fi

    local exports
    exports="$(
        SHIPD_ENV_FILE="${env_path}" "${py}" - <<'PY' 2>/dev/null || true
import os
import shlex

try:
    from dotenv import dotenv_values
except ImportError:
    raise SystemExit(0)

path = os.environ["SHIPD_ENV_FILE"]
for key, value in dotenv_values(path).items():
    if value is not None:
        print(f"export {key}={shlex.quote(value)}")
PY
    )"
    if [[ -n "${exports}" ]]; then
        # shellcheck disable=SC1090
        set -a
        eval "${exports}"
        set +a
    fi
}

_log_to_file() {
    if [[ -n "${LOG_FILE}" ]]; then
        printf '%s\n' "$1" >> "${LOG_FILE}"
    fi
}

_emit_terminal() {
    printf '%b\n' "$1"
}

log() {
    local line="[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*"
    _log_to_file "$line"
    if [[ "${USE_TTY_UI}" -eq 1 ]]; then
        _emit_terminal "${C_DIM}${line}${C_RESET}"
    else
        printf '%s\n' "$line"
    fi
}

log_msg() {
    local line="$1"
    _log_to_file "$line"
    ui_format_log_line "$line"
}

die() {
    if [[ "${USE_TTY_UI}" -eq 1 ]]; then
        _emit_terminal "${C_RED}${C_BOLD}ERROR:${C_RESET} ${C_RED}$*${C_RESET}"
    else
        printf 'ERROR: %s\n' "$*"
    fi
    log "ERROR: $*"
    exit 1
}

ui_header() {
    local title="$1"
    local width=52
    local line
    line="$(printf '─%.0s' $(seq 1 "$width"))"
    if [[ "${USE_TTY_UI}" -eq 0 ]]; then
        log "========================================"
        log "${title}"
        log "========================================"
        return
    fi
    _emit_terminal ""
    _emit_terminal "${C_CYAN}${C_BOLD}┌${line}┐${C_RESET}"
    _emit_terminal "${C_CYAN}${C_BOLD}│${C_RESET} ${C_BOLD}${title}${C_RESET}"
    _emit_terminal "${C_CYAN}${C_BOLD}└${line}┘${C_RESET}"
}

ui_review_banner() {
    local current="$1"
    local total="$2"
    CURRENT_REVIEW="${current}"
    CURRENT_REVIEW_TOTAL="${total}"
    ui_reset_phases

    if [[ "${USE_TTY_UI}" -eq 0 ]]; then
        log "----------------------------------------"
        log "Review ${current}/${total} starting"
        log "----------------------------------------"
        return
    fi

    local bar
    bar="$(ui_progress_bar "${current}" "${total}" 24)"
    _emit_terminal ""
    _emit_terminal "${C_MAGENTA}${C_BOLD}╭─ Review ${current} of ${total} ${bar}${C_RESET}"
    _emit_terminal "${C_DIM}  browser → auth → clock-in → reserve → clone → review → submit → cleanup → stats → clock-out${C_RESET}"
}

ui_progress_bar() {
    local current="$1"
    local total="$2"
    local width="${3:-20}"
    if [[ "${total}" -le 0 ]]; then
        printf ''
        return
    fi
    local filled=$((current * width / total))
    local empty=$((width - filled))
    local i
    local bar=""
    for ((i = 0; i < filled; i++)); do bar+="█"; done
    for ((i = 0; i < empty; i++)); do bar+="░"; done
    local pct=$((current * 100 / total))
    printf '[%s] %d%%' "$bar" "$pct"
}

ui_phase_label() {
    case "$1" in
        browser) printf 'Browser session' ;;
        auth) printf 'Sign in' ;;
        clock_in) printf 'Clock in' ;;
        clock_out) printf 'Clock out' ;;
        reserve) printf 'Reserve submission' ;;
        clone) printf 'Clone repo' ;;
        review) printf 'Review agent' ;;
        submit) printf 'Submit on Shipd' ;;
        cleanup) printf 'Cleanup artifacts' ;;
        stats) printf 'Session stats' ;;
        *) printf '%s' "$1" ;;
    esac
}

ui_reset_phases() {
    :
}

ui_phase_update() {
    local phase="$1"
    local status="$2"

    local base="${status%%:*}"
    local detail=""
    if [[ "${status}" == *:* ]]; then
        detail="${status#*:}"
    fi

    local label
    label="$(ui_phase_label "${phase}")"
    local suffix=""

    if [[ "${USE_TTY_UI}" -eq 0 ]]; then
        log "PHASE ${phase}: ${status}"
        return
    fi

    case "${base}" in
        start)
            ui_spinner_start "${label}"
            ;;
        done)
            ui_spinner_kill
            [[ -n "${detail}" ]] && suffix=" ${C_DIM}(${detail})${C_RESET}"
            _emit_terminal "  ${C_GREEN}✓${C_RESET} ${label}${suffix}"
            ;;
        skip)
            ui_spinner_kill
            [[ -n "${detail}" ]] && suffix=" ${C_DIM}(${detail})${C_RESET}"
            _emit_terminal "  ${C_DIM}−${C_RESET} ${label}${suffix} ${C_DIM}(skipped)${C_RESET}"
            ;;
        fail)
            ui_spinner_kill
            [[ -n "${detail}" ]] && suffix=" ${C_RED}— ${detail}${C_RESET}"
            _emit_terminal "  ${C_RED}✗${C_RESET} ${label}${suffix}"
            ;;
        *)
            _emit_terminal "  ${C_DIM}○${C_RESET} ${label}"
            ;;
    esac
}

ui_format_log_line() {
    local line="$1"
    local body="${line#\[*\] }"
    if [[ "${line}" == "${body}" ]]; then
        body="${line}"
    fi

    if [[ "${USE_TTY_UI}" -eq 0 ]]; then
        printf '%s\n' "$line"
        return
    fi

    ui_clear_spinner_line
    if [[ "${body}" == ERROR:* ]] || [[ "${body}" == *" failed:"* ]] || [[ "${body}" == *"Failed"* ]]; then
        _emit_terminal "${C_DIM}${line%${body}}${C_RESET}${C_RED}${body}${C_RESET}"
    elif [[ "${body}" == WARNING:* ]] || [[ "${body}" == Warning:* ]]; then
        _emit_terminal "${C_DIM}${line%${body}}${C_RESET}${C_YELLOW}${body}${C_RESET}"
    elif [[ "${body}" == submit:* ]]; then
        _emit_terminal "${C_DIM}${line%${body}}${C_RESET}${C_CYAN}${body}${C_RESET}"
    elif [[ "${body}" == *"Session stats:"* ]]; then
        _emit_terminal "${C_DIM}${line%${body}}${C_RESET}${C_BLUE}${body}${C_RESET}"
    elif [[ "${body}" == *"Review decision:"* ]]; then
        _emit_terminal "${C_DIM}${line%${body}}${C_RESET}${C_GREEN}${body}${C_RESET}"
    elif [[ "${body}" == *"Cloned submission"* ]] || [[ "${body}" == *"finished."* ]]; then
        _emit_terminal "${C_DIM}${line%${body}}${C_RESET}${C_GREEN}${body}${C_RESET}"
    else
        _emit_terminal "${C_DIM}${line}${C_RESET}"
    fi
}

ui_clear_spinner_line() {
    # Keep streamed lines from colliding with an active phase spinner.
    if [[ "${USE_TTY_UI}" -eq 1 ]] && [[ -n "${SPINNER_PID:-}" ]]; then
        printf '\r\033[K'
    fi
}

ui_activity_line() {
    # Agent activity: "[HH:MM:SS] [review|phase0|agent] message"
    local timestamp="$1"
    local category="$2"
    local body="$3"

    if [[ "${USE_TTY_UI}" -eq 0 ]]; then
        printf '%s\n' "[${timestamp}] [${category}] ${body}"
        return
    fi

    ui_clear_spinner_line
    local color="${C_CYAN}"
    case "${body}" in
        *FAILED*|*"CRITICAL FAIL"*) color="${C_RED}" ;;
        WARNING*|*"did not register"*) color="${C_YELLOW}" ;;
        "→ "*|"← "*) color="${C_DIM}" ;;
    esac
    _emit_terminal "    ${C_DIM}${timestamp} ${category}${C_RESET} ${color}${body}${C_RESET}"
}

ui_handle_structured_line() {
    local body="$1"
    local marker="${body#SHIPD:}"

    if [[ "${marker}" == REVIEW:* ]]; then
        local rest="${marker#REVIEW:}"
        local current="${rest%%:*}"
        rest="${rest#*:}"
        local total="${rest%%:*}"
        rest="${rest#*:}"
        local status="${rest%%:*}"
        if [[ "${total}" -eq 0 ]] && [[ -n "${MAX_RUNS}" ]]; then
            total="${MAX_RUNS}"
        fi
        case "${status}" in
            start) ui_review_banner "${current}" "${total}" ;;
            done)
                if [[ "${USE_TTY_UI}" -eq 1 ]]; then
                    _emit_terminal "${C_GREEN}${C_BOLD}  ✓ Review ${current}/${total} completed${C_RESET}"
                fi
                ;;
            fail)
                if [[ "${USE_TTY_UI}" -eq 1 ]]; then
                    _emit_terminal "${C_RED}${C_BOLD}  ✗ Review ${current}/${total} failed${C_RESET}"
                fi
                ;;
        esac
        return 0
    fi

    if [[ "${marker}" == PHASE:* ]]; then
        local rest="${marker#PHASE:}"
        local phase="${rest%%:*}"
        local status="${rest#*:}"
        ui_phase_update "${phase}" "${status}"
        return 0
    fi

    return 1
}

ui_process_stream_line() {
    local line="$1"
    _log_to_file "$line"

    # Agent activity lines: "[HH:MM:SS] [review|phase0|agent] message"
    if [[ "${line}" =~ ^\[([0-9]{2}:[0-9]{2}:[0-9]{2})\]\ \[([a-z0-9_-]+)\]\ (.*)$ ]]; then
        ui_activity_line "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}"
        return
    fi

    local body="${line}"
    if [[ "${line}" =~ ^\[([0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9]{2}:[0-9]{2}:[0-9]{2}\ UTC)\]\ (.*)$ ]]; then
        body="${BASH_REMATCH[2]}"
    fi

    if ui_handle_structured_line "${body}"; then
        return
    fi

    ui_format_log_line "$line"
}

ui_session_panel() {
    local panel
    panel="$(cd "${AGENT_DIR}" && PYTHONPATH="${AGENT_DIR}" "${PYTHON}" -c "
from session_stats import get_summary
s = get_summary()
lines = [
    ('Approved', s['approved']),
    ('Request changes', s['request_changes']),
    ('Rejected', s['rejected']),
    ('Failed', s['failed']),
    ('Completed', s['total_completed']),
]
w = max(len(k) for k, _ in lines)
for label, val in lines:
    print(f'  {label + \":\":<{w}}  {val}')
")"

    if [[ "${USE_TTY_UI}" -eq 0 ]]; then
        log_session_summary_plain
        return
    fi

    _emit_terminal ""
    _emit_terminal "${C_BLUE}${C_BOLD}┌─ Session summary ─────────────────────┐${C_RESET}"
    while IFS= read -r row; do
        _emit_terminal "${C_BLUE}│${C_RESET}${row}${C_BLUE}│${C_RESET}"
    done <<< "${panel}"
    _emit_terminal "${C_BLUE}${C_BOLD}└───────────────────────────────────────┘${C_RESET}"
}

log_session_summary_plain() {
    local summary
    summary="$(cd "${AGENT_DIR}" && PYTHONPATH="${AGENT_DIR}" "${PYTHON}" -c "from session_stats import format_summary_log; print(format_summary_log())")"
    log "${summary}"
}

ui_spinner_start() {
    local msg="$1"
    SPINNER_MSG="${msg}"
    SPINNER_PID=""
    if [[ "${USE_TTY_UI}" -eq 0 ]]; then
        log "${msg}..."
        return
    fi
    (
        local frames='|/-\'
        local i=0
        while true; do
            printf '\r  %s %s' "${frames:i:1}" "${SPINNER_MSG}"
            i=$(((i + 1) % 4))
            sleep 0.12
        done
    ) &
    SPINNER_PID=$!
}

ui_spinner_kill() {
    if [[ -n "${SPINNER_PID:-}" ]]; then
        kill "${SPINNER_PID}" 2>/dev/null || true
        wait "${SPINNER_PID}" 2>/dev/null || true
        SPINNER_PID=""
    fi
    if [[ "${USE_TTY_UI}" -eq 1 ]]; then
        printf '\r\033[K'
    fi
}

ui_spinner_stop() {
    local status="${1:-0}"
    ui_spinner_kill
    if [[ "${USE_TTY_UI}" -eq 1 ]]; then
        if [[ "${status}" -eq 0 ]]; then
            _emit_terminal "  ${C_GREEN}✓${C_RESET} ${SPINNER_MSG}"
        else
            _emit_terminal "  ${C_RED}✗${C_RESET} ${SPINNER_MSG}"
        fi
    fi
}

ui_sleep_tick() {
    local elapsed="$1"
    local total="$2"
    local remaining=$((total - elapsed))
    if [[ "${USE_TTY_UI}" -eq 0 ]]; then
        return
    fi
    local mins=$((remaining / 60))
    local secs=$((remaining % 60))
    local bar
    bar="$(ui_progress_bar "${elapsed}" "${total}" 16)"
    printf '\r  %s Next review in %dm %02ds ' "${bar}" "${mins}" "${secs}"
}

prompt_review_count() {
    if [[ -n "${MAX_RUNS}" ]]; then
        log "Review count: ${MAX_RUNS}"
        return
    fi

    if [[ "${SKIP_PROMPT}" -eq 1 ]]; then
        die "Review count required. Pass --reviews N or --max-runs N with --no-prompt."
    fi

    if [[ ! -t 0 ]]; then
        die "Interactive prompt unavailable. Pass --reviews N (e.g. ./run.sh --reviews 3)."
    fi

    local count=""
    while true; do
        if [[ "${USE_TTY_UI}" -eq 1 ]]; then
            printf '%b' "${C_BOLD}How many reviews would you like to do today? ${C_RESET}" >/dev/tty
        else
            printf 'How many reviews would you like to do today? ' >/dev/tty
        fi
        read -r count </dev/tty
        if [[ "${count}" =~ ^[1-9][0-9]*$ ]]; then
            MAX_RUNS="${count}"
            log "Plan for today: ${MAX_RUNS} review(s)"
            break
        fi
        printf 'Please enter a whole number greater than 0.\n' >/dev/tty
    done
}

on_signal() {
    SHUTDOWN=1
    log "Shutdown requested; finishing current cycle then exiting"
}

sleep_interruptible() {
    local seconds="$1"
    local elapsed=0
    if [[ "${USE_TTY_UI}" -eq 0 ]]; then
        log "Sleeping ${seconds}s before next review (browser closed, low memory)"
    fi
    while [[ "${elapsed}" -lt "${seconds}" ]] && [[ "${SHUTDOWN}" -eq 0 ]]; do
        ui_sleep_tick "${elapsed}" "${seconds}"
        sleep 1
        elapsed=$((elapsed + 1))
    done
    if [[ "${USE_TTY_UI}" -eq 1 ]]; then
        printf '\r\033[K'
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --once)
            ONCE=1
            MAX_RUNS=1
            MAX_RUNS_EXPLICIT=1
            SKIP_PROMPT=1
            shift
            ;;
        --reviews)
            MAX_RUNS="${2:?--reviews requires a value}"
            MAX_RUNS_EXPLICIT=1
            SKIP_PROMPT=1
            shift 2
            ;;
        --no-prompt)
            SKIP_PROMPT=1
            shift
            ;;
        --quest)
            QUEST="${2:?--quest requires a value}"
            shift 2
            ;;
        --review)
            REVIEW=1
            shift
            ;;
        --no-review)
            REVIEW=0
            shift
            ;;
        --submit)
            SUBMIT=1
            SUBMIT_EXPLICIT=1
            REVIEW=1
            shift
            ;;
        --no-submit)
            SUBMIT=0
            shift
            ;;
        --no-clone)
            NO_CLONE=1
            shift
            ;;
        --no-cleanup)
            NO_CLEANUP=1
            shift
            ;;
        --interval)
            INTERVAL="${2:?--interval requires a value}"
            shift 2
            ;;
        --max-runs)
            MAX_RUNS="${2:?--max-runs requires a value}"
            MAX_RUNS_EXPLICIT=1
            SKIP_PROMPT=1
            shift 2
            ;;
        --headed)
            HEADED=1
            shift
            ;;
        --foreground-priority)
            FOREGROUND=1
            shift
            ;;
        --clone-dir)
            CLONE_DIR="${2:?--clone-dir requires a value}"
            shift 2
            ;;
        --log-file)
            LOG_FILE="${2:?--log-file requires a value}"
            shift 2
            ;;
        --separate-steps)
            SEPARATE_STEPS=1
            shift
            ;;
        --fresh)
            FRESH=1
            shift
            ;;
        --setup)
            EXTRA_ARGS=(setup)
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "Unknown option: $1 (try --help)"
            ;;
    esac
done

init_ui

if [[ "${SHIPD_FRESH:-0}" == "1" ]]; then
    FRESH=1
fi

if [[ -z "${LOG_FILE}" ]]; then
    load_env_file "${ROOT_DIR}/.env"
    if [[ -n "${LOG_FILE:-}" ]]; then
        LOG_FILE="${ROOT_DIR}/${LOG_FILE#./}"
    else
        LOG_FILE="${DEFAULT_LOG_FILE}"
    fi
elif [[ "${LOG_FILE}" != /* ]]; then
    LOG_FILE="${ROOT_DIR}/${LOG_FILE#./}"
fi

# shipd.sh owns the run log (it tees every streamed line); keep LOG_FILE out
# of the Python child environment so orchestrator/activity don't double-write.
export -n LOG_FILE 2>/dev/null || true

if [[ -z "${INTERVAL}" ]]; then
    INTERVAL="${WATCH_INTERVAL_SEC:-${DEFAULT_INTERVAL}}"
fi

mkdir -p "$(dirname "${LOG_FILE}")" "${LOG_DIR}" "${ROOT_DIR}/reviews" "${ROOT_DIR}/submissions"

activate_venv() {
    if [[ -f "${VENV_DIR}/bin/activate" ]]; then
        # shellcheck disable=SC1091
        source "${VENV_DIR}/bin/activate"
        log "Using virtualenv: ${VENV_DIR}"
    else
        log "No virtualenv at ${VENV_DIR}; using system Python"
    fi
}

resolve_python() {
    if command -v python3 >/dev/null 2>&1; then
        PYTHON=python3
    elif command -v python >/dev/null 2>&1; then
        PYTHON=python
    else
        die "Python not found. Install Python 3 or create .venv."
    fi
}

run_setup() {
    ui_header "Shipd setup"
    log "Starting setup"

    ui_spinner_start "Create virtualenv"
    if [[ ! -d "${VENV_DIR}" ]]; then
        "${PYTHON}" -m venv "${VENV_DIR}"
        ui_spinner_stop 0
        log "Created virtualenv at ${VENV_DIR}"
    else
        ui_spinner_stop 0
        log "Virtualenv already exists"
    fi

    # shellcheck disable=SC1091
    source "${VENV_DIR}/bin/activate"

    ui_spinner_start "Install Python dependencies"
    if pip install -q -r "${ROOT_DIR}/requirements.txt"; then
        ui_spinner_stop 0
        log "Dependencies installed"
    else
        ui_spinner_stop 1
        die "pip install failed"
    fi

    ui_spinner_start "Install Playwright Chromium"
    if playwright install chromium; then
        ui_spinner_stop 0
        log "Playwright Chromium ready"
    else
        ui_spinner_stop 1
        die "Playwright install failed"
    fi

    log "Setup complete"
}

preflight() {
    log "Preflight checks"
    resolve_python
    activate_venv

    if [[ ! -d "${AGENT_DIR}" ]]; then
        die "Missing agent directory: ${AGENT_DIR}"
    fi

    if [[ ! -f "${ENV_FILE}" ]]; then
        log "WARNING: ${ENV_FILE} not found — copy .env.example and fill in credentials"
    elif [[ -f "${ENV_FILE}" ]]; then
        load_env_file "${ENV_FILE}"
    fi

    if [[ "${REVIEW}" -eq 1 ]] && [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
        die "ANTHROPIC_API_KEY is required for review (set in .env or use --no-review)"
    fi

    if ! "${PYTHON}" -c "import playwright" 2>/dev/null; then
        die "Playwright not installed. Run: ./run.sh --setup"
    fi

    if [[ "${SUBMIT}" -eq 1 ]] && [[ "${REVIEW}" -eq 0 ]]; then
        if [[ "${SUBMIT_EXPLICIT}" -eq 1 ]]; then
            die "--submit requires --review (or a saved reviews/pending-submit.json for manual submit)"
        fi
        # Submit defaults on; --no-review implies nothing to submit.
        SUBMIT=0
    fi

    if [[ "${REVIEW}" -eq 1 ]] && [[ "${NO_CLONE}" -eq 1 ]]; then
        die "--no-review is required when using --no-clone (review needs a cloned repo)"
    fi

    log "Preflight passed (reviews=${MAX_RUNS:-prompt}, quest=${QUEST}, review=${REVIEW}, submit=${SUBMIT}, interval=${INTERVAL}s)"
}

build_orchestrator_args() {
    ORCH_ARGS=(--quest "${QUEST}")
    [[ "${HEADED}" -eq 1 ]] && ORCH_ARGS+=(--headed)
    [[ "${NO_CLONE}" -eq 1 ]] && ORCH_ARGS+=(--no-clone)
    [[ "${NO_CLEANUP}" -eq 1 ]] && ORCH_ARGS+=(--no-cleanup)
    [[ "${REVIEW}" -eq 1 ]] && ORCH_ARGS+=(--review)
    [[ "${SUBMIT}" -eq 1 ]] && ORCH_ARGS+=(--submit)
    [[ "${FOREGROUND}" -eq 1 ]] && ORCH_ARGS+=(--foreground-priority)
    [[ -n "${CLONE_DIR}" ]] && ORCH_ARGS+=(--clone-dir "${CLONE_DIR}")
    [[ "${FRESH}" -eq 1 ]] && ORCH_ARGS+=(--fresh)
    # No --log-file: shipd.sh tees every streamed line into LOG_FILE itself;
    # passing it would write each orchestrator/activity line twice.

    if [[ -n "${MAX_RUNS}" ]]; then
        ORCH_ARGS+=(--watch --max-runs "${MAX_RUNS}" --interval "${INTERVAL}")
    fi
}

run_python_stream() {
    local script="$1"
    shift
    log "Running: ${PYTHON} ${script} $*"

    local rc=0
    set +e
    (
        cd "${AGENT_DIR}" && PYTHONUNBUFFERED=1 PYTHONPATH="${AGENT_DIR}" "${PYTHON}" -u "${script}" "$@"
    ) 2>&1 | while IFS= read -r line || [[ -n "${line}" ]]; do
        ui_process_stream_line "$line"
    done
    rc=${PIPESTATUS[0]}
    set -e
    return "${rc}"
}

run_python() {
    run_python_stream "$@"
}

run_python_or_log() {
    local script="$1"
    shift
    if run_python_stream "${script}" "$@"; then
        log "Finished: ${script}"
        return 0
    fi
    local code=$?
    log "WARNING: ${script} exited with status ${code}"
    return "${code}"
}

reset_session_stats() {
    (cd "${AGENT_DIR}" && PYTHONPATH="${AGENT_DIR}" "${PYTHON}" -c "from session_stats import reset_session; reset_session()")
    log "Session stats reset"
}

batch_python() {
    (cd "${AGENT_DIR}" && PYTHONPATH="${AGENT_DIR}" "${PYTHON}" -c "$1")
}

clear_watch_batch() {
    batch_python "from stats.watch_batch import clear_batch; clear_batch()"
    log "Cleared saved batch resume state"
}

apply_watch_resume() {
    local info max completed message
    info="$(batch_python "
from stats import watch_batch
batch = watch_batch.get_active_batch()
if batch is None:
    raise SystemExit(1)
print(batch['max_runs'])
print(batch['completed_runs'])
print(watch_batch.format_resume_message(batch))
")" || return 1

    max="$(printf '%s\n' "${info}" | sed -n '1p')"
    completed="$(printf '%s\n' "${info}" | sed -n '2p')"
    message="$(printf '%s\n' "${info}" | sed -n '3p')"

    if [[ "${MAX_RUNS_EXPLICIT}" -eq 1 ]] && [[ -n "${MAX_RUNS}" ]] && [[ "${MAX_RUNS}" != "${max}" ]]; then
        die "Saved batch targets ${max} reviews (${completed} done). Use --fresh to start ${MAX_RUNS} new reviews."
    fi

    MAX_RUNS="${max}"
    RESUME_BATCH=1
    SKIP_PROMPT=1
    log "${message}"
}

check_watch_batch_compatible() {
    local cleanup_arg="None"
    if [[ "${NO_CLEANUP}" -eq 1 ]]; then
        cleanup_arg="False"
    fi
    batch_python "
from stats import watch_batch
batch = watch_batch.get_active_batch()
if batch is None:
    raise SystemExit(0)
options = {
    'review': ${REVIEW} == 1,
    'submit': ${SUBMIT} == 1,
    'clone': $((1 - NO_CLONE)) == 1,
    'cleanup': ${cleanup_arg},
    'separate_steps': ${SEPARATE_STEPS} == 1,
}
if not watch_batch.options_compatible(
    batch,
    quest='${QUEST}',
    interval_sec=int('${INTERVAL}'),
    options=options,
):
    raise SystemExit(1)
" || die "Saved batch options differ from current flags — rerun with --fresh or SHIPD_FRESH=1"
}

prepare_watch_batch_start() {
    local cleanup_arg="None"
    if [[ "${NO_CLEANUP}" -eq 1 ]]; then
        cleanup_arg="False"
    fi
    batch_python "
from stats import watch_batch
watch_batch.start_batch(
    max_runs=int('${MAX_RUNS}'),
    quest='${QUEST}',
    interval_sec=int('${INTERVAL}'),
    options={
        'review': ${REVIEW} == 1,
        'submit': ${SUBMIT} == 1,
        'clone': $((1 - NO_CLONE)) == 1,
        'cleanup': ${cleanup_arg},
        'separate_steps': ${SEPARATE_STEPS} == 1,
    },
)
"
}

record_watch_run() {
    batch_python "from stats.watch_batch import record_run_complete; record_run_complete('${1}')"
}

next_watch_run_number() {
    batch_python "
from stats import watch_batch
batch = watch_batch.get_active_batch()
if batch is None:
    print(0)
else:
    print(watch_batch.next_run_number(batch))
"
}

watch_batch_is_active() {
    batch_python "
from stats import watch_batch
raise SystemExit(0 if watch_batch.get_active_batch() else 1)
"
}

init_batch_session() {
    if [[ "${FRESH}" -eq 1 ]]; then
        clear_watch_batch
        reset_session_stats
        return
    fi

    if [[ "${RESUME_BATCH}" -eq 1 ]]; then
        check_watch_batch_compatible
        log "Continuing saved batch (session stats preserved)"
        return
    fi

    reset_session_stats
}

record_workflow_failure() {
    (cd "${AGENT_DIR}" && PYTHONPATH="${AGENT_DIR}" "${PYTHON}" -c "from session_stats import record_failure; record_failure()")
}

log_session_summary() {
    local clock_message
    clock_message="$(cd "${AGENT_DIR}" && PYTHONPATH="${AGENT_DIR}" "${PYTHON}" -c "
from session_stats import format_clock_out_message
print(format_clock_out_message())
" 2>/dev/null || true)"
    if [[ -n "${clock_message}" ]]; then
        log "Clock-out message:"
        while IFS= read -r line || [[ -n "${line}" ]]; do
            log "  ${line}"
        done <<< "${clock_message}"
    fi
    ui_session_panel
}

find_latest_submission() {
    local dir
    dir="$(ls -td "${ROOT_DIR}"/submissions/*/ 2>/dev/null | head -1 || true)"
    dir="${dir%/}"
    if [[ -z "${dir}" ]] || [[ ! -d "${dir}" ]]; then
        log "WARNING: No cloned submission found under ${ROOT_DIR}/submissions"
        return 1
    fi
    printf '%s' "${dir}"
}

load_session_review_url() {
    (cd "${AGENT_DIR}" && PYTHONPATH=. "${PYTHON}" -c "from review.review_bundles import load_session_meta; print(load_session_meta()['review_url'])")
}

run_separate_phase() {
    local phase="$1"
    local label="$2"
    shift 2
    ui_phase_update "${phase}" "start"
    if run_python_or_log "$@"; then
        ui_phase_update "${phase}" "done"
        return 0
    fi
    ui_phase_update "${phase}" "fail"
    return 1
}

run_separate_steps_cycle() {
    log "Cycle mode: separate steps"
    local cycle_ok=0

    run_separate_phase auth "Sign in (auth/auth.py)" auth/auth.py || return 1
    run_separate_phase clock_in "Clock in for quest ${QUEST} (workflow/time_logs.py)" workflow/time_logs.py --quest "${QUEST}" || return 1

    local review_args=()
    [[ "${HEADED}" -eq 1 ]] && review_args+=(--headed)
    [[ "${NO_CLONE}" -eq 1 ]] && review_args+=(--no-clone)
    [[ -n "${CLONE_DIR}" ]] && review_args+=(--clone-dir "${CLONE_DIR}")
    review_args+=(--quest "${QUEST}")
    run_separate_phase reserve "Reserve submission and clone (workflow/review.py)" workflow/review.py "${review_args[@]}" || return 1

    if [[ "${REVIEW}" -eq 1 ]]; then
        local repo_path review_url
        repo_path="$(find_latest_submission)" || return 1
        review_url="$(load_session_review_url)" || return 1
        log "Review target: ${repo_path}"
        log "Review URL: ${review_url}"
        if run_separate_phase review "Autonomous review (review/agent.py)" review/agent.py "${repo_path}" --quest "${QUEST}" --review-url "${review_url}"; then
            :
        else
            cycle_ok=1
            record_workflow_failure
            log "WARNING: Review agent failed; continuing without submit"
        fi
    else
        ui_phase_update review "skip"
    fi

    if [[ "${SUBMIT}" -eq 1 ]]; then
        local submit_args=()
        [[ "${HEADED}" -eq 1 ]] && submit_args+=(--headed)
        submit_args+=(--quest "${QUEST}")
        if run_separate_phase submit "Submit on Shipd (workflow/submit_from_json.py)" workflow/submit_from_json.py "${submit_args[@]}"; then
            :
        else
            cycle_ok=1
            record_workflow_failure
        fi
    else
        ui_phase_update submit "skip"
    fi

    if [[ "${REVIEW}" -eq 1 ]] && [[ "${NO_CLEANUP}" -eq 0 ]]; then
        if run_separate_phase cleanup "Cleanup cloned repo and Docker (workflow/cleanup.py)" workflow/cleanup.py --from-session-meta; then
            :
        else
            log "WARNING: Post-review cleanup failed; artifacts may remain"
        fi
    elif [[ "${NO_CLEANUP}" -eq 1 ]]; then
        ui_phase_update cleanup "skip:no-cleanup"
    else
        ui_phase_update cleanup "skip"
    fi

    ui_phase_update stats "start"
    ui_phase_update stats "done"
    return "${cycle_ok}"
}

run_separate_steps_batch() {
    local cycle=0
    trap on_signal INT TERM
    trap '' TSTP

    if [[ "${RESUME_BATCH}" -eq 0 ]]; then
        prepare_watch_batch_start
    fi

    log "Batch started: ${MAX_RUNS} review(s), interval=${INTERVAL}s between cycles"

    while [[ "${SHUTDOWN}" -eq 0 ]]; do
        if ! watch_batch_is_active; then
            break
        fi

        cycle="$(next_watch_run_number)"
        if [[ "${cycle}" -le 0 ]] || [[ "${cycle}" -gt "${MAX_RUNS}" ]]; then
            break
        fi

        ui_review_banner "${cycle}" "${MAX_RUNS}"

        if run_separate_steps_cycle; then
            record_watch_run "done"
            if [[ "${USE_TTY_UI}" -eq 1 ]]; then
                _emit_terminal "${C_GREEN}${C_BOLD}  ✓ Review ${cycle}/${MAX_RUNS} completed${C_RESET}"
            else
                log "Review ${cycle}/${MAX_RUNS} completed successfully"
            fi
        else
            record_workflow_failure
            record_watch_run "fail"
            if [[ "${USE_TTY_UI}" -eq 1 ]]; then
                _emit_terminal "${C_RED}${C_BOLD}  ✗ Review ${cycle}/${MAX_RUNS} failed; continuing${C_RESET}"
            else
                log "Review ${cycle}/${MAX_RUNS} failed; continuing with remaining reviews"
            fi
        fi
        log_session_summary

        if ! watch_batch_is_active || [[ "${SHUTDOWN}" -eq 1 ]]; then
            break
        fi

        sleep_interruptible "${INTERVAL}"
    done

    trap - INT TERM
    if watch_batch_is_active; then
        apply_watch_resume >/dev/null 2>&1 || true
        log "Batch paused; rerun ./run.sh to continue"
    else
        log "Batch finished: ${MAX_RUNS} review(s) completed"
    fi
}

run_orchestrator_batch() {
    trap on_signal INT TERM
    trap '' TSTP
    build_orchestrator_args

    log "Batch started: ${MAX_RUNS} review(s) via orchestrator"
    run_python_stream orchestrator.py "${ORCH_ARGS[@]}" || true
    trap - INT TERM
    log "Batch finished: scheduled ${MAX_RUNS} review(s)"
}

run_once() {
    if [[ "${SEPARATE_STEPS}" -eq 1 ]]; then
        ui_review_banner 1 1
        if run_separate_steps_cycle; then
            if [[ "${USE_TTY_UI}" -eq 1 ]]; then
                _emit_terminal "${C_GREEN}${C_BOLD}  ✓ Review 1/1 completed${C_RESET}"
            else
                log "Review 1/1 completed"
            fi
        else
            record_workflow_failure
            die "Pipeline cycle failed"
        fi
        log_session_summary
    else
        build_orchestrator_args
        ORCH_ARGS=(--quest "${QUEST}")
        [[ "${HEADED}" -eq 1 ]] && ORCH_ARGS+=(--headed)
        [[ "${NO_CLONE}" -eq 1 ]] && ORCH_ARGS+=(--no-clone)
        [[ "${NO_CLEANUP}" -eq 1 ]] && ORCH_ARGS+=(--no-cleanup)
        [[ "${REVIEW}" -eq 1 ]] && ORCH_ARGS+=(--review)
        [[ "${SUBMIT}" -eq 1 ]] && ORCH_ARGS+=(--submit)
        [[ "${FOREGROUND}" -eq 1 ]] && ORCH_ARGS+=(--foreground-priority)
        [[ -n "${CLONE_DIR}" ]] && ORCH_ARGS+=(--clone-dir "${CLONE_DIR}")
        run_python_stream orchestrator.py "${ORCH_ARGS[@]}" || die "Pipeline cycle failed"
        log_session_summary
    fi
}

main() {
    if [[ "${#EXTRA_ARGS[@]}" -gt 0 ]] && [[ "${EXTRA_ARGS[0]}" == "setup" ]]; then
        resolve_python
        run_setup
        exit 0
    fi

    resolve_python
    activate_venv

    ui_header "> - Shipd agent"
    log "Log file: ${LOG_FILE}"

    if [[ "${FRESH}" -eq 0 ]] && [[ "${ONCE}" -eq 0 ]]; then
        apply_watch_resume || true
    fi

    prompt_review_count
    preflight
    init_batch_session

    if [[ "${ONCE}" -eq 1 ]] || [[ "${MAX_RUNS}" -eq 1 ]]; then
        log "Running 1 review"
        run_once
        ui_header "Done — 1/1 review completed"
        log_session_summary
        exit 0
    fi

    if [[ "${SEPARATE_STEPS}" -eq 1 ]]; then
        run_separate_steps_batch
    else
        run_orchestrator_batch
    fi

    ui_header "Done — ${MAX_RUNS} review(s) scheduled"
    log_session_summary
}

main "$@"
