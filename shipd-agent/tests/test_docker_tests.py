# Unit tests for Docker harness discovery and command planning (mocked subprocess).

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from review.docker_tests import (
    DockerLayout,
    discover_docker_layout,
    docker_cli_available,
    parse_dockerfile_workdir,
    plan_docker_build,
    plan_docker_run_test_sh,
    run_docker_build,
    run_test_sh_in_docker,
)


class ParseDockerfileWorkdirTests(unittest.TestCase):
    def test_parses_quoted_workdir(self) -> None:
        text = 'FROM ubuntu\nWORKDIR "/app"\n'
        self.assertEqual(parse_dockerfile_workdir(text), "/app")

    def test_default_when_missing(self) -> None:
        self.assertEqual(parse_dockerfile_workdir("FROM alpine\n"), "/workspace")


class DiscoverDockerLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_finds_root_dockerfile(self) -> None:
        (self.repo / "Dockerfile").write_text(
            "FROM ubuntu\nWORKDIR /testbed\nENTRYPOINT [\"/bin/bash\"]\n",
            encoding="utf-8",
        )
        layout = discover_docker_layout(self.repo)
        self.assertIsNotNone(layout)
        assert layout is not None
        self.assertEqual(layout.kind, "dockerfile")
        self.assertEqual(layout.dockerfile_rel, "Dockerfile")
        self.assertEqual(layout.workdir, "/testbed")

    def test_finds_nested_dockerfile(self) -> None:
        nested = self.repo / "harness"
        nested.mkdir()
        (nested / "Dockerfile").write_text("FROM ubuntu\n", encoding="utf-8")
        layout = discover_docker_layout(self.repo)
        self.assertIsNotNone(layout)
        assert layout is not None
        self.assertEqual(layout.context_dir, Path("harness"))

    def test_falls_back_to_compose(self) -> None:
        (self.repo / "docker-compose.yml").write_text(
            "services:\n  test:\n    build: .\n",
            encoding="utf-8",
        )
        layout = discover_docker_layout(self.repo)
        self.assertIsNotNone(layout)
        assert layout is not None
        self.assertEqual(layout.kind, "compose")
        self.assertEqual(layout.compose_rel, "docker-compose.yml")

    def test_returns_none_when_missing(self) -> None:
        self.assertIsNone(discover_docker_layout(self.repo))


class PlanDockerCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        (self.repo / "Dockerfile").write_text("FROM ubuntu\nWORKDIR /ws\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_build_plan_uses_dockerfile_context(self) -> None:
        layout = discover_docker_layout(self.repo)
        assert layout is not None
        argv = plan_docker_build(layout, self.repo, "shipd-review-test")
        self.assertEqual(argv[0:3], ["docker", "build", "-t"])
        self.assertIn("shipd-review-test", argv)
        self.assertIn("-f", argv)
        self.assertTrue(str(self.repo / "Dockerfile") in argv[-2] or argv[-2].endswith("Dockerfile"))

    def test_run_plan_uses_network_none_and_mounts(self) -> None:
        layout = discover_docker_layout(self.repo)
        assert layout is not None
        junit_dir = self.repo / "junit-out"
        argv = plan_docker_run_test_sh(
            layout,
            self.repo,
            "shipd-review-test",
            test_sh_rel="test.sh",
            mode="base",
            container_output=str(junit_dir / "base.xml"),
            host_junit_dir=junit_dir,
        )
        self.assertIn("--network", argv)
        self.assertIn("none", argv)
        self.assertIn("-v", argv)
        mount = next(v for v in argv if v.endswith(":/ws"))
        self.assertTrue(mount.startswith(str(self.repo.resolve())) or mount.startswith(str(self.repo)))
        joined = " ".join(argv)
        self.assertIn("./test.sh", joined)
        self.assertIn("base", joined)


class MockSubprocessDockerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        (self.repo / "Dockerfile").write_text("FROM ubuntu\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_run_docker_build_success(self) -> None:
        layout = discover_docker_layout(self.repo)
        assert layout is not None
        calls: list[list[str]] = []

        def fake_run(args: list[str], *, cwd: Path | None = None, timeout: int) -> tuple[int, str]:
            calls.append(args)
            return 0, "Successfully built abc123"

        result = run_docker_build(
            layout,
            self.repo,
            tag="shipd-review-mock",
            run_cmd=fake_run,
        )
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.tag, "shipd-review-mock")
        self.assertEqual(calls[0][0:2], ["docker", "build"])

    def test_run_test_sh_in_docker_invokes_run(self) -> None:
        layout = DockerLayout(
            kind="dockerfile",
            dockerfile_rel="Dockerfile",
            compose_rel=None,
            context_dir=Path("."),
            workdir="/workspace",
        )
        calls: list[list[str]] = []

        def fake_run(args: list[str], *, cwd: Path | None = None, timeout: int) -> tuple[int, str]:
            calls.append(args)
            return 0, "ok"

        out_path = self.repo / "out" / "base.xml"
        code, log = run_test_sh_in_docker(
            layout,
            self.repo,
            "shipd-review-mock",
            test_sh_rel="test.sh",
            mode="base",
            host_output_path=out_path,
            run_cmd=fake_run,
        )
        self.assertEqual(code, 0)
        self.assertIn("--network none", log.replace("\n", " "))
        self.assertEqual(calls[0][0:2], ["docker", "run"])

    def test_docker_cli_available_checks_version(self) -> None:
        def ok_run(args: list[str], *, cwd: Path | None = None, timeout: int) -> tuple[int, str]:
            self.assertEqual(args[:2], ["docker", "version"])
            return 0, ""

        self.assertTrue(docker_cli_available(run_cmd=ok_run))

        def fail_run(args: list[str], *, cwd: Path | None = None, timeout: int) -> tuple[int, str]:
            return 127, "not found"

        self.assertFalse(docker_cli_available(run_cmd=fail_run))


class Phase0DockerIntegrationTests(unittest.TestCase):
    def test_missing_dockerfile_fails_phase0_tests(self) -> None:
        from review.review_phases import run_phase0_tests

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            artifacts = {
                "test.sh": "test.sh",
                "test.patch": "test.patch",
                "solution.patch": None,
                "Dockerfile": None,
            }
            (repo / "test.sh").write_text("#!/bin/bash\n", encoding="utf-8")
            (repo / "test.patch").write_text("--- /dev/null\n+++ b/x\n@@ -0,0 +1 @@\n+x\n", encoding="utf-8")

            with patch("review.docker_tests.docker_cli_available", return_value=True):
                log, findings, critical = run_phase0_tests(
                    repo,
                    artifacts=artifacts,
                    timeout=10,
                )
            self.assertTrue(critical)
            self.assertTrue(
                any("Missing Dockerfile" in f.finding for f in findings),
                msg=log,
            )


class DockerfileRuleTests(unittest.TestCase):
    def test_pinned_accepted_image_passes(self) -> None:
        from review.harness_checks import evaluate_dockerfile

        text = """
FROM python:3.12
WORKDIR /app
RUN useradd -m -u 1000 model
USER model
ENTRYPOINT ["bash"]
""".strip()
        self.assertEqual(evaluate_dockerfile(text), [])

    def test_unpinned_base_tests_and_user_fail(self) -> None:
        from review.harness_checks import evaluate_dockerfile, evaluate_python_install, evaluate_test_harness

        text = """
FROM ubuntu
WORKDIR /tmp
RUN pytest
RUN pip install requests
ENTRYPOINT ["python", "app.py"]
""".strip()
        names = [item.finding for item in evaluate_dockerfile(text)]
        for title in (
            "Dockerfile base image is not a pinned accepted image",
            "Dockerfile workdir is not /app",
            "Dockerfile runs tests during the build",
            "Dockerfile installs an unpinned package",
            "Dockerfile does not run as model:1000",
            "Dockerfile entrypoint is not an interactive shell",
        ):
            self.assertIn(title, names)

        install = evaluate_python_install("RUN pip install /app\n", "")
        self.assertEqual(
            install[0].finding,
            "Python install is not editable and the prompt does not say how tests run",
        )
        self.assertEqual(evaluate_python_install("RUN pip install /app\n", "Run the tests with pytest."), [])

        harness = evaluate_test_harness(
            "#!/bin/sh\npip install pytest\npytest -x --tb=no\n",
            executable=False,
            test_patch=(
                "+++ b/src/app.py\n+print('solve')\n"
                "+++ b/check.py\n+assert result == '" + ("x" * 130) + "'\n"
                "+++ b/tests/odd_case.py\n+# datacurve\n+assert True\n"
            ),
            solution_patch="+++ b/src/app.py\n+print('solve')\n",
        )
        harness_names = [item.finding for item in harness]
        for title in (
            "test.sh has no base mode",
            "test.sh has no new mode",
            "test.sh is not executable",
            "test.sh installs packages",
            "Test patch contains a banned marker",
            "Tests use a fail-fast flag",
            "Tests hide failure diagnostics",
            "Tests pin an oversized expected output",
            "Test file name is not predictable",
            "Test patch contains solution code",
        ):
            self.assertIn(title, harness_names)


if __name__ == "__main__":
    unittest.main()
