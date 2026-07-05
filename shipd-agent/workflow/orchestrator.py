# Clock in, reserve a submission, and clone it in one browser session.

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from auth import (
    AUTH_STATE_PATH,
    REPO_ROOT,
    AuthConfig,
    ensure_signed_in,
    goto_page,
    load_auth_config,
    managed_browser,
)
from browser.session import (
    DEFAULT_WATCH_INTERVAL_SEC,
    ShutdownWatcher,
    background_priority,
)
from stats import session_stats
from stats import watch_batch

try:
    from review.agent import run_review_agent
except (ImportError, AttributeError):
    run_review_agent = None  # type: ignore[assignment, misc]
from review.activity import set_activity_log_file
from review.review_bundles import save_review_bundle
from review.result import is_review_complete, review_failure_reason
from workflow.review import (
    clone_submission_locally,
    extract_setup_script,
    reserve_and_open_review,
)
from workflow.cleanup import (
    cleanup_after_review_enabled,
    cleanup_submission_artifacts,
    snapshot_docker_state,
)
from workflow.time_logs import (
    clock_in,
    clock_out,
    return_to_reviews,
    time_logs_url,
    wait_for_time_logs,
)


PHASE_PREFIX = "SHIPD:PHASE:"
COOLDOWN_PREFIX = "SHIPD:COOLDOWN:"


class ReviewAgentError(RuntimeError):
    """Review agent failed; the failure is already recorded in session stats."""


def log_message(message: str, *, log_file: Path | None = None) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{timestamp}] {message}"
    print(line, flush=True)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def log_phase(phase: str, status: str, *, log_file: Path | None = None) -> None:
    """Emit a machine-readable phase marker for shipd.sh terminal UX."""
    log_message(f"{PHASE_PREFIX}{phase}:{status}", log_file=log_file)


class PhaseTracker:
    """Track workflow phases and emit start/done/fail/skip markers."""

    def __init__(self, log_file: Path | None) -> None:
        self.log_file = log_file
        self.current: str | None = None
        self._started: dict[str, float] = {}

    def start(self, phase: str) -> None:
        self.current = phase
        self._started[phase] = time.monotonic()
        log_phase(phase, "start", log_file=self.log_file)

    def done(self, phase: str | None = None) -> None:
        name = phase or self.current
        if not name:
            return
        elapsed = time.monotonic() - self._started.get(name, time.monotonic())
        log_phase(name, f"done:{elapsed:.1f}s", log_file=self.log_file)
        if phase is None:
            self.current = None

    def skip(self, phase: str, reason: str = "") -> None:
        status = f"skip:{reason}" if reason else "skip"
        log_phase(phase, status, log_file=self.log_file)

    def fail(self, phase: str | None, reason: str) -> None:
        name = phase or self.current
        if not name:
            return
        safe = reason.replace(":", " -").replace("\n", " ")
        log_phase(name, f"fail:{safe}", log_file=self.log_file)
        self.current = None

    @contextmanager
    def step(self, phase: str):
        self.start(phase)
        try:
            yield
            self.done(phase)
        except Exception as exc:
            self.fail(phase, str(exc))
            raise


def _clock_out_in_session(
    page,
    *,
    quest: str,
    phases: PhaseTracker,
    log_file: Path | None,
    context,
    auth_state_path: Path,
) -> None:
    """Clock out on Shipd using the active browser session."""
    message = session_stats.format_clock_out_message()
    if not message.strip():
        phases.skip("clock_out", "no-reviews")
        return

    log_message(f"Clock-out message:\n{message}", log_file=log_file)
    with phases.step("clock_out"):
        logs_url = time_logs_url(quest)
        if f"/quests/{quest}/time-logs" not in page.url:
            goto_page(page, logs_url)
        wait_for_time_logs(page)
        stopped = clock_out(page, message, quest=quest)
        context.storage_state(path=str(auth_state_path))
        if stopped:
            log_message("Clock stopped on Shipd time logs.", log_file=log_file)
        else:
            log_message(
                "WARNING: Clock was not running; message logged to run log only.",
                log_file=log_file,
            )


def run_workflow(
    *,
    quest: str = "olympus",
    config: AuthConfig | None = None,
    headless: bool = True,
    auth_state_path: Path = AUTH_STATE_PATH,
    clone_dir: Path | None = None,
    clone: bool = True,
    review: bool = False,
    submit: bool = False,
    cleanup: bool | None = None,
    clock_out_on_exit: bool = False,
    log_file: Path | None = None,
) -> Path | None:
    """Sign in if needed, clock in, reserve/continue, clone, and optionally review."""
    if submit and not review:
        raise RuntimeError("--submit requires --review.")
    if review and not clone:
        raise RuntimeError("--review requires clone (omit --no-clone).")

    config = config or AuthConfig()
    auth_state_path.parent.mkdir(parents=True, exist_ok=True)

    set_activity_log_file(log_file)
    log_message("Starting workflow run.", log_file=log_file)
    phases = PhaseTracker(log_file)
    do_cleanup = cleanup if cleanup is not None else cleanup_after_review_enabled()

    cloned_path: Path | None = None
    docker_state_before = None
    review_attempted = False
    phases.start("browser")
    browser_ready = 0
    try:
        with managed_browser(
            headless=headless,
            auth_state_path=auth_state_path,
            lightweight=headless,
        ) as session:
            phases.done("browser")
            browser_ready = 1
            page = session.page
            context = session.context

            with phases.step("auth"):
                ensure_signed_in(
                    page,
                    time_logs_url(quest),
                    config,
                    headed=not headless,
                )
                context.storage_state(path=str(auth_state_path))

            with phases.step("clock_in"):
                wait_for_time_logs(page)
                clock_in(page, quest)
                return_to_reviews(page)

            with phases.step("reserve"):
                reserve_and_open_review(page)

            review_url = page.url
            if clone:
                if do_cleanup:
                    docker_state_before = snapshot_docker_state()
                with phases.step("clone"):
                    setup_script = extract_setup_script(page)
                    cloned_path = clone_submission_locally(
                        setup_script,
                        clone_dir=clone_dir or (REPO_ROOT / "submissions"),
                    )
                    log_message(
                        f"Cloned submission to {cloned_path}.",
                        log_file=log_file,
                    )
            else:
                phases.skip("clone", "no-clone")
                log_message(f"Review ready at {review_url}.", log_file=log_file)

            if review:
                review_attempted = True
                try:
                    if cloned_path is None:
                        phases.skip("review", "no-clone")
                        phases.skip("submit", "no-clone" if submit else "not-requested")
                    elif run_review_agent is None:
                        raise RuntimeError(
                            "Autonomous review requires review.agent with "
                            "run_review_agent(). Implement shipd-agent/review/agent.py "
                            "or run without --review."
                        )
                    else:
                        review_result: dict | None = None
                        review_error: str | None = None
                        try:
                            log_message(
                                "Running autonomous review agent.",
                                log_file=log_file,
                            )
                            phases.start("review")
                            review_result = run_review_agent(
                                repo_path=cloned_path,
                                quest=quest,
                                review_url=review_url,
                                page=page,
                            )
                        except Exception as exc:
                            review_error = str(exc)
                            phases.fail("review", review_error)
                            session_stats.record_failure()
                            log_message(
                                f"WARNING: Review agent failed: {review_error}",
                                log_file=log_file,
                            )
                        else:
                            if not review_result or not review_result.get("decision"):
                                review_error = (
                                    "run_review_agent() returned no decision "
                                    "(check review/agent.py or use --no-review)."
                                )
                                phases.fail("review", review_error)
                                session_stats.record_failure()
                                log_message(
                                    f"WARNING: {review_error}",
                                    log_file=log_file,
                                )
                            elif not is_review_complete(review_result):
                                review_error = review_failure_reason(review_result)
                                phases.fail("review", review_error)
                                session_stats.record_failure()
                                log_message(
                                    f"WARNING: Review agent did not finish rubric "
                                    f"phases: {review_error}",
                                    log_file=log_file,
                                )
                            else:
                                phases.done("review")
                                log_message(
                                    f"Review decision: {review_result.get('decision')} — "
                                    f"{review_result.get('recommendation_summary', '')}",
                                    log_file=log_file,
                                )
                                session_stats.record_decision(
                                    review_result["decision"],
                                    repo_path=cloned_path,
                                    review_url=review_url,
                                    quest=quest,
                                )
                                bundle_path = save_review_bundle(
                                    review_result,
                                    review_url=review_url,
                                    quest=quest,
                                    repo_path=cloned_path,
                                )
                                log_message(
                                    f"Saved review bundle to {bundle_path}.",
                                    log_file=log_file,
                                )

                        if (
                            submit
                            and review_result
                            and is_review_complete(review_result)
                        ):
                            from workflow.submit import submit_review

                            with phases.step("submit"):
                                log_message(
                                    "Submitting review on Shipd.",
                                    log_file=log_file,
                                )
                                if review_url not in page.url:
                                    goto_page(page, review_url)
                                confirmed = submit_review(
                                    page,
                                    review_result,
                                    quest=quest,
                                    log=lambda m: log_message(m, log_file=log_file),
                                    review_url=review_url,
                                )
                                if not confirmed:
                                    log_message(
                                        "WARNING: Submission not confirmed — "
                                        "verify manually on Shipd.",
                                        log_file=log_file,
                                    )
                        elif submit:
                            phases.skip("submit", "review-failed")
                        else:
                            phases.skip("submit", "not-requested")

                        if review_error and review_attempted:
                            raise ReviewAgentError(review_error)
                finally:
                    if do_cleanup:
                        with phases.step("cleanup"):
                            cleanup_submission_artifacts(
                                cloned_path,
                                docker_state_before=docker_state_before,
                                log=lambda message: log_message(
                                    message, log_file=log_file
                                ),
                            )
                        cloned_path = None
            else:
                phases.skip("review", "no-review")
                phases.skip("submit", "no-review")

            if clock_out_on_exit:
                _clock_out_in_session(
                    page,
                    quest=quest,
                    phases=phases,
                    log_file=log_file,
                    context=context,
                    auth_state_path=auth_state_path,
                )
            else:
                context.storage_state(path=str(auth_state_path))

            if not headless:
                print("Press Enter to close the browser...")
                input()

    except Exception as exc:
        if not browser_ready:
            phases.fail("browser", str(exc))
        raise

    return cloned_path


def _batch_options(
    *,
    clone: bool,
    review: bool,
    submit: bool,
    cleanup: bool | None,
) -> dict[str, bool | None]:
    return {
        "review": review,
        "submit": submit,
        "clone": clone,
        "cleanup": cleanup,
        "separate_steps": False,
    }


def _load_failed_review_url() -> str:
    try:
        from review.review_bundles import load_session_meta

        return str(load_session_meta().get("review_url", "")).strip()
    except (ImportError, OSError, ValueError, FileNotFoundError):
        return ""


def _prepare_watch_batch(
    *,
    quest: str,
    interval_sec: int,
    max_runs: int | None,
    clone: bool,
    review: bool,
    submit: bool,
    cleanup: bool | None,
    fresh: bool,
    log_file: Path | None,
) -> tuple[int | None, dict[str, Any] | None]:
    """Load or create batch resume state for watch mode."""
    if max_runs is None:
        return None, None

    options = _batch_options(
        clone=clone,
        review=review,
        submit=submit,
        cleanup=cleanup,
    )
    if fresh:
        watch_batch.clear_batch()
        return max_runs, watch_batch.start_batch(
            max_runs=max_runs,
            quest=quest,
            interval_sec=interval_sec,
            options=options,
        )

    active = watch_batch.get_active_batch()
    if active is not None:
        if not watch_batch.options_compatible(
            active,
            quest=quest,
            interval_sec=interval_sec,
            options=options,
        ):
            log_message(
                "WARNING: Saved batch options differ from current flags; "
                "pass --fresh or set SHIPD_FRESH=1 to restart.",
                log_file=log_file,
            )
            raise RuntimeError(
                "Incompatible resume state — use --fresh to start a new batch."
            )
        log_message(watch_batch.format_resume_message(active), log_file=log_file)
        return int(active["max_runs"]), active

    return max_runs, watch_batch.start_batch(
        max_runs=max_runs,
        quest=quest,
        interval_sec=interval_sec,
        options=options,
    )


def run_clock_out(
    *,
    quest: str = "olympus",
    config: AuthConfig,
    headless: bool,
    auth_state_path: Path = AUTH_STATE_PATH,
    log_file: Path | None = None,
) -> None:
    """Open a browser session, stop the Shipd clock, and post session notes."""
    message = session_stats.format_clock_out_message()
    if not message.strip():
        log_message("No completed reviews; skipping clock out.", log_file=log_file)
        log_phase("clock_out", "skip:no-reviews", log_file=log_file)
        return

    log_phase("clock_out", "start", log_file=log_file)
    log_message(f"Clock-out message:\n{message}", log_file=log_file)

    try:
        with managed_browser(
            headless=headless,
            auth_state_path=auth_state_path,
            lightweight=headless,
        ) as session:
            page = session.page
            context = session.context

            ensure_signed_in(
                page,
                time_logs_url(quest),
                config,
                headed=not headless,
            )
            wait_for_time_logs(page)
            stopped = clock_out(page, message, quest=quest)
            context.storage_state(path=str(auth_state_path))

            if stopped:
                log_message("Clock stopped on Shipd time logs.", log_file=log_file)
                log_phase("clock_out", "done", log_file=log_file)
            else:
                log_message(
                    "WARNING: Clock was not running; message logged to run log only.",
                    log_file=log_file,
                )
                log_phase("clock_out", "skip:not-clocked-in", log_file=log_file)
    except (PlaywrightTimeoutError, RuntimeError, ValueError) as exc:
        log_message(f"WARNING: Clock out failed: {exc}", log_file=log_file)
        log_phase("clock_out", f"fail:{exc}", log_file=log_file)


def run_watch_loop(
    *,
    quest: str,
    config: AuthConfig,
    headless: bool,
    auth_state_path: Path,
    clone_dir: Path,
    clone: bool,
    review: bool,
    submit: bool,
    cleanup: bool | None,
    interval_sec: int,
    cooldown_every: int,
    cooldown_sec: int,
    max_runs: int | None,
    log_file: Path | None,
    fresh: bool = False,
) -> int:
    """Repeat the workflow with the browser closed between runs."""
    max_runs, batch = _prepare_watch_batch(
        quest=quest,
        interval_sec=interval_sec,
        max_runs=max_runs,
        clone=clone,
        review=review,
        submit=submit,
        cleanup=cleanup,
        fresh=fresh,
        log_file=log_file,
    )

    watcher = ShutdownWatcher()
    watcher.install()
    exit_code = 0
    batch_complete = False

    log_message(
        f"Watch mode started (interval={interval_sec}s, max_runs={max_runs or '∞'}).",
        log_file=log_file,
    )
    log_message("Press Ctrl+C to stop gracefully.", log_file=log_file)

    try:
        while not watcher.requested:
            batch = watch_batch.get_active_batch()
            if batch is None:
                batch_complete = True
                break

            runs = watch_batch.next_run_number(batch)
            max_display = int(batch["max_runs"])
            log_message(
                f"SHIPD:REVIEW:{runs}:{max_display}:start",
                log_file=log_file,
            )
            log_message(f"Run {runs} starting.", log_file=log_file)
            run_status = "done"
            try:
                run_workflow(
                    quest=quest,
                    config=config,
                    headless=headless,
                    auth_state_path=auth_state_path,
                    clone_dir=clone_dir,
                    clone=clone,
                    review=review,
                    submit=submit,
                    cleanup=cleanup,
                    clock_out_on_exit=False,
                    log_file=log_file,
                )
                log_message(f"Run {runs} finished.", log_file=log_file)
                log_message(
                    f"SHIPD:REVIEW:{runs}:{max_display}:done",
                    log_file=log_file,
                )
            except (
                PlaywrightTimeoutError,
                RuntimeError,
                ValueError,
                subprocess.CalledProcessError,
            ) as exc:
                exit_code = 1
                run_status = "fail"
                if not isinstance(exc, ReviewAgentError):
                    session_stats.record_failure()
                log_message(f"Run {runs} failed: {exc}", log_file=log_file)
                log_message(
                    f"SHIPD:REVIEW:{runs}:{max_display}:fail",
                    log_file=log_file,
                )

            failed_url = _load_failed_review_url() if run_status == "fail" else ""
            finished = watch_batch.record_run_complete(
                run_status,
                review_url=failed_url,
            )
            if finished is not None and watch_batch.is_batch_complete(finished):
                batch_complete = True

            log_phase("stats", "start", log_file=log_file)
            log_message(session_stats.format_summary_log(), log_file=log_file)
            log_phase("stats", "done", log_file=log_file)

            if batch_complete:
                log_message("Reached --max-runs limit.", log_file=log_file)
                break
            if watcher.requested:
                break

            completed_reviews = int(
                session_stats.get_summary().get("total_completed", 0)
            )
            should_cooldown = (
                cooldown_every > 0
                and cooldown_sec > 0
                and completed_reviews > 0
                and completed_reviews % cooldown_every == 0
            )
            if should_cooldown:
                log_message(
                    f"Cooldown triggered after {completed_reviews} completed "
                    f"reviews. Waiting {cooldown_sec}s.",
                    log_file=log_file,
                )
                for elapsed in range(cooldown_sec):
                    if watcher.requested:
                        break
                    log_message(
                        f"{COOLDOWN_PREFIX}{elapsed}:{cooldown_sec}",
                        log_file=log_file,
                    )
                    watcher.sleep(1)
                if watcher.requested:
                    break
                log_message(
                    f"{COOLDOWN_PREFIX}{cooldown_sec}:{cooldown_sec}",
                    log_file=log_file,
                )
                log_message("Cooldown complete; starting next run.", log_file=log_file)
            elif interval_sec > 0:
                log_message(
                    f"Sleeping {interval_sec}s before next run "
                    "(browser closed, minimal memory use).",
                    log_file=log_file,
                )
                watcher.sleep(interval_sec)
    finally:
        watcher.restore()
        log_message("Watch mode stopped.", log_file=log_file)
        log_message(session_stats.format_summary_log(), log_file=log_file)
        if batch_complete:
            run_clock_out(
                quest=quest,
                config=config,
                headless=headless,
                auth_state_path=auth_state_path,
                log_file=log_file,
            )
        else:
            active = watch_batch.get_active_batch()
            if active is not None:
                log_message(
                    f"{watch_batch.format_resume_message(active)} "
                    "(rerun ./run.sh to continue).",
                    log_file=log_file,
                )

    return exit_code


def parse_args() -> argparse.Namespace:
    default_interval = int(
        os.getenv("WATCH_INTERVAL_SEC", str(DEFAULT_WATCH_INTERVAL_SEC))
    )
    parser = argparse.ArgumentParser(
        description=(
            "Run the full Shipd review prep flow in one browser session: "
            "sign in if needed, clock in, reserve or continue a submission, "
            "and clone it locally via Quick Setup."
        ),
    )
    parser.add_argument(
        "--quest",
        choices=("olympus", "mars"),
        default="olympus",
        help="Quest to clock hours for (default: olympus).",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help=(
            "Open a visible browser window "
            "(default is headless background mode)."
        ),
    )
    parser.add_argument(
        "--auth-state",
        type=Path,
        default=AUTH_STATE_PATH,
        help="Path to save/load Playwright auth state.",
    )
    parser.add_argument(
        "--clone-dir",
        type=Path,
        default=None,
        help=(
            "Directory where Quick Setup clones the submission "
            "(default: ./submissions or SUBMISSIONS_DIR from .env)."
        ),
    )
    parser.add_argument(
        "--no-clone",
        action="store_true",
        help="Reserve and open the review without running Quick Setup.",
    )
    parser.add_argument(
        "--review",
        action="store_true",
        help=(
            "After cloning, run the autonomous review agent "
            "(requires ANTHROPIC_API_KEY)."
        ),
    )
    parser.add_argument(
        "--submit",
        action="store_true",
        help="Submit the generated review on Shipd (implies --review).",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help=(
            "Keep running: repeat the workflow on an interval with the browser "
            "fully closed between runs (safe for overnight use)."
        ),
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=default_interval,
        help=(
            "Seconds to wait between watch-mode runs "
            f"(default: {default_interval}, or WATCH_INTERVAL_SEC from .env)."
        ),
    )
    parser.add_argument(
        "--cooldown-every",
        type=int,
        default=int(os.getenv("WATCH_COOLDOWN_EVERY_COMPLETED", "10")),
        help=(
            "After this many completed reviews, pause for cooldown "
            "(default: 10, or WATCH_COOLDOWN_EVERY_COMPLETED from .env)."
        ),
    )
    parser.add_argument(
        "--cooldown-sec",
        type=int,
        default=int(os.getenv("WATCH_COOLDOWN_SEC", "3600")),
        help=(
            "Cooldown duration in seconds after each cooldown-every milestone "
            "(default: 3600, or WATCH_COOLDOWN_SEC from .env)."
        ),
    )
    parser.add_argument(
        "--max-runs",
        type=int,
        default=None,
        help="Stop watch mode after this many runs (default: run until Ctrl+C).",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help=(
            "Keep the cloned submission and Docker artifacts after review "
            "(default removes them when CLEANUP_AFTER_REVIEW=1)."
        ),
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="Append timestamped run logs to this file (useful overnight).",
    )
    parser.add_argument(
        "--foreground-priority",
        action="store_true",
        help="Do not lower CPU priority (default lowers nice for background use).",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help=(
            "Start a new review batch (clear resume state and session stats). "
            "Same as SHIPD_FRESH=1."
        ),
    )
    return parser.parse_args()


def _should_reset_session(*, watch: bool, fresh: bool) -> bool:
    if fresh:
        return True
    if watch and watch_batch.get_active_batch() is not None:
        return False
    return True


def main() -> int:
    args = parse_args()
    fresh = args.fresh or os.getenv("SHIPD_FRESH", "").strip() == "1"
    if fresh:
        watch_batch.clear_batch()
    if _should_reset_session(watch=args.watch, fresh=fresh):
        session_stats.reset_session()

    config = load_auth_config()

    submissions_dir = os.getenv("SUBMISSIONS_DIR", "").strip()
    default_clone_dir = (
        Path(submissions_dir) if submissions_dir else REPO_ROOT / "submissions"
    )

    log_file = args.log_file
    if log_file is None:
        log_path = os.getenv("LOG_FILE", "").strip()
        if log_path:
            log_file = Path(log_path)

    if not args.foreground_priority and not args.headed:
        background_priority()

    clone_dir = args.clone_dir or default_clone_dir
    clone = not args.no_clone
    review = args.review or args.submit
    submit = args.submit
    cleanup = False if args.no_cleanup else None
    headless = not args.headed

    if args.watch:
        if args.interval < 0:
            print("Warning: negative --interval is invalid; using 0.", file=sys.stderr)
            args.interval = 0
        if args.interval != 0 and args.interval < 60:
            print(
                "Warning: intervals under 60s may keep Chromium busy too often.",
                file=sys.stderr,
            )
        return run_watch_loop(
            quest=args.quest,
            config=config,
            headless=headless,
            auth_state_path=args.auth_state,
            clone_dir=clone_dir,
            clone=clone,
            review=review,
            submit=submit,
            cleanup=cleanup,
            interval_sec=args.interval,
            cooldown_every=args.cooldown_every,
            cooldown_sec=args.cooldown_sec,
            max_runs=args.max_runs,
            log_file=log_file,
            fresh=fresh,
        )

    try:
        log_message("SHIPD:REVIEW:1:1:start", log_file=log_file)
        run_workflow(
            quest=args.quest,
            config=config,
            headless=headless,
            auth_state_path=args.auth_state,
            clone_dir=clone_dir,
            clone=clone,
            review=review,
            submit=submit,
            cleanup=cleanup,
            clock_out_on_exit=True,
            log_file=log_file,
        )
        log_message("SHIPD:REVIEW:1:1:done", log_file=log_file)
    except (
        PlaywrightTimeoutError,
        RuntimeError,
        ValueError,
        subprocess.CalledProcessError,
    ) as exc:
        if not isinstance(exc, ReviewAgentError):
            session_stats.record_failure()
        log_message(f"Workflow failed: {exc}", log_file=log_file)
        log_message("SHIPD:REVIEW:1:1:fail", log_file=log_file)
        log_phase("stats", "start", log_file=log_file)
        log_message(session_stats.format_summary_log(), log_file=log_file)
        log_phase("stats", "done", log_file=log_file)
        return 1

    log_phase("stats", "start", log_file=log_file)
    log_message(session_stats.format_summary_log(), log_file=log_file)
    log_phase("stats", "done", log_file=log_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
