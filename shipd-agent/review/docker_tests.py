# Docker-based Phase 0 test.sh execution per Shipd rubric (--network none).

from __future__ import annotations

import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from review.activity import log_activity
from review.context import resolve_repo_path

DEFAULT_BUILD_TIMEOUT_SEC = 600
DEFAULT_RUN_TIMEOUT_SEC = 600

COMPOSE_FILENAMES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)

WORKDIR_RE = re.compile(r"^\s*WORKDIR\s+(.+)$", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class DockerLayout:
    """Resolved Docker harness for a submission repo."""

    kind: str  # "dockerfile" | "compose"
    dockerfile_rel: str | None
    compose_rel: str | None
    context_dir: Path  # relative to repo root
    workdir: str


@dataclass
class DockerBuildResult:
    exit_code: int
    log: str
    tag: str | None


RunCmd = Callable[..., tuple[int, str]]


class TestRunner(Protocol):
    def run_test_sh(
        self,
        test_sh_rel: str,
        mode: str,
        output_path: Path,
        *,
        timeout: int,
    ) -> tuple[int, str]: ...


def _default_run_cmd(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: int,
) -> tuple[int, str]:
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, f"Command timed out after {timeout}s: {' '.join(args)}"
    except FileNotFoundError as exc:
        return 127, str(exc)
    output = (result.stdout + result.stderr).strip()
    header = f"{' '.join(args)} → exit {result.returncode}"
    return result.returncode, f"{header}\n{output}" if output else header


def docker_cli_available(*, run_cmd: RunCmd = _default_run_cmd) -> bool:
    code, _ = run_cmd(["docker", "version"], timeout=30)
    return code == 0


def parse_dockerfile_workdir(
    dockerfile_text: str,
    *,
    default: str = "/workspace",
) -> str:
    match = WORKDIR_RE.search(dockerfile_text)
    if not match:
        return default
    raw = match.group(1).strip()
    if raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1]
    return raw


def _find_compose_file(repo_path: Path) -> Path | None:
    for name in COMPOSE_FILENAMES:
        direct = repo_path / name
        if direct.is_file():
            return direct
        matches = sorted(repo_path.rglob(name))
        if matches:
            return matches[0]
    return None


def discover_docker_layout(
    repo_path: Path,
    *,
    dockerfile_rel: str | None = None,
) -> DockerLayout | None:
    """Locate Dockerfile or docker-compose in the submission repo."""
    repo_path = resolve_repo_path(repo_path)

    if dockerfile_rel:
        df_path = repo_path / dockerfile_rel
        if df_path.is_file():
            try:
                text = df_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            return DockerLayout(
                kind="dockerfile",
                dockerfile_rel=dockerfile_rel,
                compose_rel=None,
                context_dir=df_path.parent.relative_to(repo_path),
                workdir=parse_dockerfile_workdir(text),
            )

    direct = repo_path / "Dockerfile"
    if direct.is_file():
        try:
            text = direct.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        return DockerLayout(
            kind="dockerfile",
            dockerfile_rel="Dockerfile",
            compose_rel=None,
            context_dir=Path("."),
            workdir=parse_dockerfile_workdir(text),
        )

    matches = sorted(repo_path.rglob("Dockerfile"))
    if matches:
        rel = str(matches[0].relative_to(repo_path))
        try:
            text = matches[0].read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        return DockerLayout(
            kind="dockerfile",
            dockerfile_rel=rel,
            compose_rel=None,
            context_dir=matches[0].parent.relative_to(repo_path),
            workdir=parse_dockerfile_workdir(text),
        )

    compose = _find_compose_file(repo_path)
    if compose is not None:
        rel = str(compose.relative_to(repo_path))
        return DockerLayout(
            kind="compose",
            dockerfile_rel=None,
            compose_rel=rel,
            context_dir=compose.parent.relative_to(repo_path),
            workdir="/workspace",
        )

    return None


def plan_docker_build(
    layout: DockerLayout,
    repo_path: Path,
    tag: str,
) -> list[str]:
    """Return the docker build command argv for this layout."""
    repo_path = resolve_repo_path(repo_path)
    context = repo_path / layout.context_dir

    if layout.kind == "compose" and layout.compose_rel:
        compose_path = repo_path / layout.compose_rel
        return [
            "docker",
            "compose",
            "-f",
            str(compose_path),
            "build",
        ]

    dockerfile = repo_path / layout.dockerfile_rel  # type: ignore[operator]
    return [
        "docker",
        "build",
        "-t",
        tag,
        "-f",
        str(dockerfile),
        str(context),
    ]


def plan_docker_run_test_sh(
    layout: DockerLayout,
    repo_path: Path,
    tag: str,
    *,
    test_sh_rel: str,
    mode: str,
    container_output: str,
    host_junit_dir: Path,
) -> list[str]:
    """
    Return docker run argv for ./test.sh inside the container.

    Rubric: containers run with ``--network none`` at test time.
    Mounts the live repo (patch state) and a host dir for JUnit XML.
    """
    repo_path = resolve_repo_path(repo_path)
    host_junit_dir.mkdir(parents=True, exist_ok=True)

    test_cmd = f"./{test_sh_rel} --output_path {container_output} {mode}"

    if layout.kind == "compose" and layout.compose_rel:
        compose_path = repo_path / layout.compose_rel
        service = "test"
        return [
            "docker",
            "compose",
            "-f",
            str(compose_path),
            "run",
            "--rm",
            "--no-deps",
            "--network",
            "none",
            "-v",
            f"{repo_path}:{layout.workdir}",
            "-w",
            layout.workdir,
            service,
            "/bin/bash",
            "-lc",
            test_cmd,
        ]

    return [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "-v",
        f"{repo_path}:{layout.workdir}",
        "-v",
        f"{host_junit_dir}:{host_junit_dir}",
        "-w",
        layout.workdir,
        tag,
        "/bin/bash",
        "-lc",
        test_cmd,
    ]


def make_image_tag(repo_path: Path) -> str:
    slug = resolve_repo_path(repo_path).name.lower()
    slug = re.sub(r"[^a-z0-9._-]", "-", slug)[:32] or "submission"
    return f"shipd-review-{slug}-{uuid.uuid4().hex[:8]}"


def run_docker_build(
    layout: DockerLayout,
    repo_path: Path,
    *,
    tag: str | None = None,
    build_timeout: int = DEFAULT_BUILD_TIMEOUT_SEC,
    run_cmd: RunCmd = _default_run_cmd,
) -> DockerBuildResult:
    repo_path = resolve_repo_path(repo_path)
    image_tag = tag or make_image_tag(repo_path)
    build_argv = plan_docker_build(layout, repo_path, image_tag)
    cmd_str = " ".join(build_argv)
    log_lines = ["=== Docker build (Phase 0) ===", f"  command: {cmd_str}"]
    log_activity(
        f"docker build starting (timeout {build_timeout}s): {cmd_str}",
        category="phase0",
    )
    started = time.monotonic()
    code, output = run_cmd(build_argv, cwd=repo_path, timeout=build_timeout)
    elapsed = time.monotonic() - started
    log_lines.append(output)
    if code != 0:
        log_lines.append(f"  Docker build FAILED (exit {code})")
        log_activity(
            f"docker build FAILED after {elapsed:.1f}s (exit {code})",
            category="phase0",
        )
        return DockerBuildResult(
            exit_code=code, log="\n".join(log_lines), tag=None
        )
    log_lines.append(f"  Docker build OK → tag {image_tag}")
    log_activity(
        f"docker build OK in {elapsed:.1f}s → {image_tag}", category="phase0"
    )
    return DockerBuildResult(
        exit_code=0, log="\n".join(log_lines), tag=image_tag
    )


def run_test_sh_in_docker(
    layout: DockerLayout,
    repo_path: Path,
    tag: str,
    *,
    test_sh_rel: str,
    mode: str,
    host_output_path: Path,
    run_timeout: int = DEFAULT_RUN_TIMEOUT_SEC,
    run_cmd: RunCmd = _default_run_cmd,
) -> tuple[int, str]:
    """Run test.sh in container; JUnit XML written to host_output_path."""
    repo_path = resolve_repo_path(repo_path)
    host_junit_dir = host_output_path.parent
    container_output = str(host_output_path)
    run_argv = plan_docker_run_test_sh(
        layout,
        repo_path,
        tag,
        test_sh_rel=test_sh_rel,
        mode=mode,
        container_output=container_output,
        host_junit_dir=host_junit_dir,
    )
    log_activity(
        f"running ./test.sh {mode} in Docker (--network none, "
        f"timeout {run_timeout}s)",
        category="phase0",
    )
    started = time.monotonic()
    code, output = run_cmd(run_argv, cwd=repo_path, timeout=run_timeout)
    elapsed = time.monotonic() - started
    log_activity(
        f"./test.sh {mode} finished in {elapsed:.1f}s → exit {code}",
        category="phase0",
    )
    header = (
        f"docker test.sh {mode} (--network none) "
        f"→ exit {code}\n  command: {' '.join(run_argv)}"
    )
    return code, f"{header}\n{output}" if output else header


class HostTestRunner:
    """Run test.sh on the host — for unit tests only; not rubric-compliant."""

    def __init__(self, repo_path: Path) -> None:
        self.repo_path = resolve_repo_path(repo_path)

    def run_test_sh(
        self,
        test_sh_rel: str,
        mode: str,
        output_path: Path,
        *,
        timeout: int,
    ) -> tuple[int, str]:
        from review.review_phases import _run_test_sh  # noqa: PLC0415

        return _run_test_sh(
            self.repo_path,
            test_sh_rel,
            mode,
            output_path,
            timeout=timeout,
        )


class DockerTestRunner:
    """Build once, run test.sh contract inside Docker with --network none."""

    def __init__(
        self,
        repo_path: Path,
        *,
        layout: DockerLayout,
        build_timeout: int = DEFAULT_BUILD_TIMEOUT_SEC,
        run_timeout: int = DEFAULT_RUN_TIMEOUT_SEC,
        run_cmd: RunCmd = _default_run_cmd,
        tag: str | None = None,
    ) -> None:
        self.repo_path = resolve_repo_path(repo_path)
        self.layout = layout
        self.build_timeout = build_timeout
        self.run_timeout = run_timeout
        self.run_cmd = run_cmd
        self.tag = tag
        self.build_log = ""
        self._built = False

    def ensure_built(self) -> tuple[bool, str]:
        if self._built and self.tag:
            return True, self.build_log
        result = run_docker_build(
            self.layout,
            self.repo_path,
            tag=self.tag,
            build_timeout=self.build_timeout,
            run_cmd=self.run_cmd,
        )
        self.build_log = result.log
        if result.exit_code != 0 or not result.tag:
            return False, self.build_log
        self.tag = result.tag
        self._built = True
        return True, self.build_log

    def run_test_sh(
        self,
        test_sh_rel: str,
        mode: str,
        output_path: Path,
        *,
        timeout: int,
    ) -> tuple[int, str]:
        ok, build_log = self.ensure_built()
        if not ok:
            return 127, build_log
        run_timeout = min(timeout, self.run_timeout)
        code, out = run_test_sh_in_docker(
            self.layout,
            self.repo_path,
            self.tag,  # type: ignore[arg-type]
            test_sh_rel=test_sh_rel,
            mode=mode,
            host_output_path=output_path,
            run_timeout=run_timeout,
            run_cmd=self.run_cmd,
        )
        return code, out


def create_test_runner(
    repo_path: Path,
    *,
    dockerfile_rel: str | None = None,
    build_timeout: int = DEFAULT_BUILD_TIMEOUT_SEC,
    run_timeout: int = DEFAULT_RUN_TIMEOUT_SEC,
    run_cmd: RunCmd = _default_run_cmd,
    force_host: bool = False,
) -> tuple[TestRunner | None, str]:
    """
    Create a Docker test runner or return None with a reason.

    Returns (runner, discovery_log).
    """
    repo_path = resolve_repo_path(repo_path)
    log_lines = ["=== Docker harness discovery ==="]

    if force_host:
        log_lines.append(
            "  Using host test.sh (force_host=True — not rubric-compliant)"
        )
        return HostTestRunner(repo_path), "\n".join(log_lines)

    if not docker_cli_available(run_cmd=run_cmd):
        log_lines.append("  Docker CLI not available")
        return None, "\n".join(log_lines)

    layout = discover_docker_layout(repo_path, dockerfile_rel=dockerfile_rel)
    if layout is None:
        log_lines.append("  No Dockerfile or docker-compose found")
        return None, "\n".join(log_lines)

    log_lines.append(f"  kind: {layout.kind}")
    if layout.dockerfile_rel:
        log_lines.append(f"  Dockerfile: {layout.dockerfile_rel}")
    if layout.compose_rel:
        log_lines.append(f"  compose: {layout.compose_rel}")
    log_lines.append(f"  workdir: {layout.workdir}")

    runner = DockerTestRunner(
        repo_path,
        layout=layout,
        build_timeout=build_timeout,
        run_timeout=run_timeout,
        run_cmd=run_cmd,
    )
    return runner, "\n".join(log_lines)
