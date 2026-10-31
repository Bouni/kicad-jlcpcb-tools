"""Exercise dependency settings and lane cleanup without building images."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import textwrap
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
LANE = ROOT / "scripts" / "run_test_lane.sh"
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="Requires Bash")


def workflow_step(name: str) -> str:
    """Read a named workflow step so tests execute its actual shell code."""
    workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text()
    start = workflow.index(f"      - name: {name}\n")
    following = workflow.find("\n      - ", start + 1)
    return workflow[start : following if following != -1 else None]


def step_script(step: str) -> str:
    """Extract the literal run block from a workflow step."""
    return textwrap.dedent(step.split("        run: |\n", 1)[1])


@pytest.fixture
def lane_environment(tmp_path: Path) -> dict[str, str]:
    """Model Docker's exclusive cidfile creation and run-owned containers."""
    executable = tmp_path / "bin" / "docker"
    executable.parent.mkdir()
    executable.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            """\
            import json
            import os
            from pathlib import Path
            import signal
            import sys
            import time

            state = Path(os.environ['DOCKER_STATE'])
            arguments = sys.argv[1:]
            with (state / 'calls').open('a') as log:
                log.write(json.dumps(arguments) + '\\n')
            if arguments[0] == 'rm':
                (state / arguments[-1]).unlink(missing_ok=True)
                sys.exit(0)
            if arguments[0] == 'stop':
                (state / arguments[-1]).write_text('stopped')
                sys.exit(0)
            if arguments[0] != 'run':
                sys.exit('Unexpected Docker command')
            if os.environ.get('DOCKER_FAILURE') == 'start':
                sys.exit(125)
            identifier = arguments[arguments.index('--name') + 1]
            receipt = Path(arguments[arguments.index('--cidfile') + 1])
            try:
                with receipt.open('x') as stream:
                    if os.environ.get('DOCKER_FAILURE') == 'empty':
                        sys.exit(125)
                    stream.write(identifier)
            except FileExistsError:
                sys.exit('Container ID file found, make sure the other container is not running')
            (state / identifier).touch()
            if os.environ.get('DOCKER_FAILURE') == 'overlap':
                (state / (identifier + '.ready')).touch()
                deadline = time.monotonic() + 10
                while not (state / (identifier + '.release')).exists():
                    if time.monotonic() > deadline:
                        sys.exit('Timed out waiting for test release')
                    time.sleep(0.01)
            if os.environ.get('DOCKER_FAILURE') == 'signal':
                os.kill(os.getppid(), signal.SIGTERM)
            sys.exit(int(os.environ.get('DOCKER_STATUS', '0')))
            """
        )
    )
    executable.chmod(0o755)
    native_cat = shutil.which("cat")
    assert native_cat is not None
    uuid_reader = executable.parent / "cat"
    uuid_reader.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "if sys.argv[1:] == ['/proc/sys/kernel/random/uuid']:\n"
        "    print('this-run')\n"
        "else:\n"
        f"    os.execv({native_cat!r}, [{native_cat!r}, *sys.argv[1:]])\n"
    )
    uuid_reader.chmod(0o755)
    state = tmp_path / "docker-state"
    state.mkdir()
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    return dict(
        os.environ,
        PATH=f"{executable.parent}{os.pathsep}{os.environ['PATH']}",
        ACT="",
        DOCKER_STATE=str(state),
        TMPDIR=str(receipts),
        KICAD_VALIDATION_RUN_ID="this-run",
    )


def run_lane(
    directory: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Invoke the outer lane launcher with an image handled only by the fake."""
    return subprocess.run(
        ["bash", str(LANE), "test-image", "ordinary-tests", "amd64"],
        cwd=directory,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def test_direct_lane_can_run_twice(
    tmp_path: Path, lane_environment: dict[str, str]
) -> None:
    """An earlier lane invocation cannot leave a cidfile that prevents reruns."""
    first = run_lane(tmp_path, lane_environment)
    second = run_lane(tmp_path, lane_environment)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert not (tmp_path / ".cache/native-tests/ordinary-tests/container-id").exists()
    assert not list(Path(lane_environment["TMPDIR"]).iterdir())


@pytest.mark.parametrize("failure", ["success", "tests", "start", "empty", "signal"])
@pytest.mark.parametrize("managed", [True, False])
def test_lane_ignores_stale_receipts_without_removing_unrelated_containers(
    tmp_path: Path, lane_environment: dict[str, str], failure: str, managed: bool
) -> None:
    """Only this attempt's container is cleaned or retained for failure artifacts."""
    receipt = tmp_path / ".cache/native-tests/ordinary-tests/container-id"
    receipt.parent.mkdir(parents=True)
    receipt.write_text("unrelated-container")
    state = Path(lane_environment["DOCKER_STATE"])
    unrelated = state / "unrelated-container"
    unrelated.touch()
    lane_environment.update(
        DOCKER_FAILURE=failure,
        DOCKER_STATUS="7" if failure == "tests" else "0",
    )
    if not managed:
        lane_environment.pop("KICAD_VALIDATION_RUN_ID")
    result = run_lane(tmp_path, lane_environment)
    expected = {"success": 0, "tests": 7, "start": 125, "empty": 125, "signal": 143}[
        failure
    ]
    assert result.returncode == expected, result.stderr
    assert unrelated.exists(), "The stale receipt must not authorize container removal"
    current = state / "kicad-tests-this-run-ordinary-tests"
    if managed and failure in ("tests", "signal"):
        assert current.read_text() == "stopped"
    else:
        assert not current.exists()
    assert receipt.read_text() == "unrelated-container"
    assert not list(Path(lane_environment["TMPDIR"]).iterdir())
    calls = [json.loads(line) for line in (state / "calls").read_text().splitlines()]
    assert all(call[-1] != "unrelated-container" for call in calls)
    if failure != "start":
        invocation = calls[0]
        assert invocation[invocation.index("--label") + 1] == (
            "org.kicad-jlcpcb-tools.validation-run=this-run"
        )


def wait_for_file(path: Path) -> None:
    """Wait for a fake container to publish its ready state."""
    deadline = time.monotonic() + 5
    while not path.exists():
        assert time.monotonic() < deadline, f"No ready receipt at {path}"
        time.sleep(0.01)


def test_overlapping_lane_exit_cannot_remove_another_invocations_container(
    tmp_path: Path, lane_environment: dict[str, str]
) -> None:
    """The first exit cleans up its own container while the second stays live."""
    state = Path(lane_environment["DOCKER_STATE"])
    names = [f"kicad-tests-{owner}-ordinary-tests" for owner in ("first", "second")]
    processes = []
    try:
        for owner, name in zip(("first", "second"), names):
            processes.append(
                subprocess.Popen(
                    ["bash", str(LANE), "test-image", "ordinary-tests", "amd64"],
                    cwd=tmp_path,
                    env=dict(
                        lane_environment,
                        KICAD_VALIDATION_RUN_ID=owner,
                        DOCKER_FAILURE="overlap",
                    ),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
            wait_for_file(state / f"{name}.ready")
        calls = [
            json.loads(line) for line in (state / "calls").read_text().splitlines()
        ]
        receipts = [Path(call[call.index("--cidfile") + 1]) for call in calls]
        assert receipts[0] != receipts[1]
        assert all(
            receipt.parent.stat().st_mode & 0o777 == 0o700 for receipt in receipts
        )
        (state / f"{names[0]}.release").touch()
        _, error = processes[0].communicate(timeout=10)
        assert processes[0].returncode == 0, error
        assert (state / names[1]).exists(), "The second container must still be live"
        assert not (state / names[0]).exists(), "The first container must be cleaned up"
        calls = [
            json.loads(line) for line in (state / "calls").read_text().splitlines()
        ]
        removals = [call[-1] for call in calls if call[0] == "rm"]
        assert removals == [names[0]]
    finally:
        for name in names:
            (state / f"{name}.release").touch()
        for process in processes:
            process.communicate(timeout=10)
    assert processes[1].returncode == 0
    assert not (state / names[1]).exists()
    assert not list(Path(lane_environment["TMPDIR"]).iterdir())


@pytest.mark.parametrize(
    ("act", "configured", "major", "expected"),
    [
        ("", "arm64", "", "amd64"),
        ("", "arm64", "10", "amd64"),
        ("false", "arm64", "10", "amd64"),
        ("true", "arm64", "", "arm64"),
        ("true", "arm64", "10", "arm64"),
        ("true", "amd64", "10", "amd64"),
        ("true", "", "10", "amd64"),
        ("true", "arm64", "7", "amd64"),
        ("true", "arm64", "8", "amd64"),
        ("true", "arm64", "9", "amd64"),
    ],
)
def test_image_settings_limit_architecture_override_to_act(
    tmp_path: Path, act: str, configured: str, major: str, expected: str
) -> None:
    """Hosted lanes ignore ARM64 preferences; unsupported local lanes use AMD64."""
    output = tmp_path / "environment"
    result = subprocess.run(
        ["bash", "-c", step_script(workflow_step("Prepare dependency image"))],
        env=dict(
            os.environ,
            ACT=act,
            LOCAL_ARCHITECTURE=configured,
            KICAD_MAJOR=major,
            GITHUB_ENV=str(output),
        ),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values["ARCHITECTURE"] == expected


@pytest.mark.parametrize(
    ("moment", "week"),
    [
        ("2026-12-28T00:00:00+00:00", "2026-W53"),
        ("2027-01-03T23:59:59+00:00", "2026-W53"),
        ("2027-01-04T00:00:00+00:00", "2027-W01"),
    ],
)
def test_dependency_revision_follows_utc_iso_week(
    tmp_path: Path, moment: str, week: str
) -> None:
    """Refresh on the UTC week boundary, retaining ISO week-years at New Year."""
    clock = tmp_path / "date"
    clock.write_text(
        f"#!{sys.executable}\n"
        "from datetime import datetime, timezone\n"
        "import os, sys\n"
        "assert sys.argv[1] == '-u', 'The revision must use UTC'\n"
        "instant = datetime.fromisoformat(os.environ['CLOCK']).astimezone(timezone.utc)\n"
        "print(instant.strftime(sys.argv[2].removeprefix('+')))\n"
    )
    clock.chmod(0o755)
    output = tmp_path / "environment"
    subprocess.run(
        ["bash", "-c", step_script(workflow_step("Prepare dependency image"))],
        env=dict(
            os.environ,
            PATH=f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            TZ="Pacific/Honolulu",
            CLOCK=moment,
            ACT="",
            LOCAL_ARCHITECTURE="",
            KICAD_MAJOR="",
            GITHUB_ENV=str(output),
        ),
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    prefix, _, actual_week = values["DEPENDENCY_REVISION"].partition("-")
    assert prefix.isdecimal(), "Keep a numeric prefix for manual dependency refreshes"
    assert actual_week == week


def test_both_builders_forward_the_dependency_revision() -> None:
    """Both real image paths pass the changing revision to the consumed ARG."""
    hosted = workflow_step("Build hosted dependency image")
    local = step_script(workflow_step("Build local dependency image"))
    assert "DEPENDENCY_REVISION=${{ env.DEPENDENCY_REVISION }}" in hosted
    arguments = shlex.split(local.replace("\\\n", ""))
    revision = arguments.index("DEPENDENCY_REVISION=$DEPENDENCY_REVISION")
    assert arguments[revision - 1] == "--build-arg"
    dockerfile = (ROOT / "docker" / "native-tests.Dockerfile").read_text()
    argument = dockerfile.index("ARG DEPENDENCY_REVISION=")
    consumed = dockerfile.index('RUN test -n "$DEPENDENCY_REVISION"', argument)
    assert consumed < dockerfile.index("apt-get", consumed)
