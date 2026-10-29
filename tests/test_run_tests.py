"""Exercise architecture selection, failure propagation and owned cleanup."""

from pathlib import Path
import signal
import subprocess
from typing import Optional

import pytest

from scripts import run_tests as runner


@pytest.mark.parametrize(
    ("found", "expected"),
    [("aarch64", "arm64"), ("arm64", "arm64"), ("x86_64", "amd64"), ("amd64", "amd64")],
)
def test_architecture_follows_daemon_not_client(
    monkeypatch: pytest.MonkeyPatch, found: str, expected: str
) -> None:
    """Daemon aliases and an explicit AMD64 override work on either client."""
    monkeypatch.setattr(runner, "docker", lambda *_args: found)
    assert runner.daemon_architecture("auto") == expected
    monkeypatch.setattr(
        runner, "docker", lambda *_args: pytest.fail("Unexpected query")
    )
    assert runner.daemon_architecture("amd64") == "amd64"


def test_unknown_architecture_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unsupported daemon cannot silently run the wrong architecture."""
    monkeypatch.setattr(runner, "docker", lambda *_args: "unknown")
    with pytest.raises(ValueError, match="Unsupported Docker architecture"):
        runner.daemon_architecture("auto")


@pytest.mark.parametrize("gateway", ["172.17.0.1", "8.8.8.8", "127.0.0.1"])
def test_linux_services_require_private_reachable_address(
    monkeypatch: pytest.MonkeyPatch, gateway: str
) -> None:
    """Service listeners must not bind a public or container-local address."""
    monkeypatch.setattr(
        runner, "docker", lambda *args: "Linux" if args[0] == "info" else gateway
    )
    if gateway == "172.17.0.1":
        assert runner.private_endpoints() == (gateway, gateway)
    else:
        with pytest.raises(ValueError, match="private"):
            runner.private_endpoints()


def test_cleanup_is_limited_to_owned_containers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup selects this run's containers and verifies their removal."""
    calls = []
    results = iter(["owned-one\nowned-two", "", ""])

    def docker(*args: str) -> str:
        calls.append(args)
        return next(results)

    monkeypatch.setattr(runner, "docker", docker)
    runner.cleanup("this-run")
    assert calls == [
        ("ps", "--all", "--quiet", "--filter", f"label={runner.LABEL}=this-run"),
        ("rm", "--force", "owned-one", "owned-two"),
        ("ps", "--all", "--quiet", "--filter", f"label={runner.LABEL}=this-run"),
    ]


@pytest.mark.parametrize("failure", ["setup", "tests", "interrupt"])
def test_failed_or_interrupted_workflow_preserves_status_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Failure and interruption cannot skip cleanup or become a success."""
    calls, cleaned = [], []
    monkeypatch.setattr(runner, "daemon_architecture", lambda _choice: "arm64")
    monkeypatch.setattr(
        runner, "private_endpoints", lambda: ("127.0.0.1", "host.docker.internal")
    )
    monkeypatch.setattr(runner, "cleanup", cleaned.append)
    monkeypatch.setattr(runner, "copy_tracked", lambda _directory: None)

    def execute(command: list[str], _log: Path, _environment: dict[str, str]) -> int:
        calls.append(command)
        if failure == "interrupt":
            raise KeyboardInterrupt
        return 7 if failure == "setup" or len(calls) == 2 else 0

    monkeypatch.setattr(runner, "run_process", execute)
    if failure == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            runner.run(tmp_path / "out", tmp_path / "cache", "auto")
    else:
        assert runner.run(tmp_path / "out", tmp_path / "cache", "auto") == 7
    assert len(cleaned) == 1
    assert len(calls) == (2 if failure == "tests" else 1)
    assert "--matrix=lane:ordinary-tests" in calls[0]
    if len(calls) == 2:
        assert not any(argument.startswith("--matrix") for argument in calls[1])


def test_interrupted_process_group_is_terminated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interruption escalates termination for an unresponsive owned process."""
    actions = []

    class Process:
        pid = 1234
        returncode = None

        def wait(self, timeout: int = 0) -> int:
            actions.append(("wait", timeout))
            if timeout == 1800:
                raise KeyboardInterrupt
            if timeout == 5:
                raise subprocess.TimeoutExpired("act", timeout)
            self.returncode = -9
            return self.returncode

        def poll(self) -> Optional[int]:
            return self.returncode

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(
        runner.os, "killpg", lambda *args: actions.append(("signal", *args))
    )
    with pytest.raises(KeyboardInterrupt):
        runner.run_process(["act"], tmp_path / "act.log", {})
    assert actions == [
        ("wait", 1800),
        ("signal", 1234, signal.SIGTERM),
        ("wait", 5),
        ("signal", 1234, signal.SIGKILL),
        ("wait", 0),
    ]


def test_snapshot_preserves_working_changes_without_collecting_untracked_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tracked ignored files and edits survive while unrelated tests stay out."""
    repository, snapshot = tmp_path / "repository", tmp_path / "snapshot"
    repository.mkdir()

    def git(*arguments: str) -> None:
        subprocess.run(
            ["git", *arguments], cwd=repository, check=True, capture_output=True
        )

    git("init", "--quiet")
    git("remote", "add", "origin", "https://github.com/example/project.git")
    (repository / ".gitignore").write_text("tracked.py\n")
    (repository / "tracked.py").write_text("original")
    git("add", "--force", ".")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    (repository / "tracked.py").write_text("working edit")
    (repository / "test_untracked.py").write_text(
        "raise AssertionError('must not collect')"
    )
    original_index = (repository / ".git/index").read_bytes()
    monkeypatch.setattr(runner, "ROOT", repository)
    runner.copy_tracked(snapshot)
    assert (snapshot / "tracked.py").read_text() == "working edit"
    assert not (snapshot / "test_untracked.py").exists()
    assert (repository / ".git/index").read_bytes() == original_index
    assert (repository / "tracked.py").read_text() == "working edit"
    assert (repository / "test_untracked.py").is_file()
    tracked = subprocess.check_output(["git", "ls-files"], cwd=snapshot, text=True)
    assert "tracked.py" in tracked.splitlines()
