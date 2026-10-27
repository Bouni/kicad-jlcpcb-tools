"""Run the seven-lane test workflow locally using Docker and act."""

import argparse
from contextlib import suppress
import ipaddress
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Optional
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
LABEL = "org.kicad-jlcpcb-tools.validation-run"
LANE_LABEL = "org.kicad-jlcpcb-tools.validation-lane"


def docker(*arguments: str) -> str:
    """Query the selected Docker daemon."""
    return subprocess.check_output(
        ["docker", *arguments], text=True, timeout=30
    ).strip()


def daemon_architecture(selection: str) -> str:
    """Prefer the daemon's architecture unless AMD64 was explicitly requested."""
    if selection == "amd64":
        return selection
    found = docker("info", "--format", "{{.Architecture}}")
    aliases = {
        "aarch64": "arm64",
        "arm64": "arm64",
        "x86_64": "amd64",
        "amd64": "amd64",
    }
    if found not in aliases:
        raise ValueError(f"Unsupported Docker architecture: {found}")
    return aliases[found]


def private_endpoints() -> tuple[str, str]:
    """Bind local services privately while keeping them reachable by containers."""
    if "docker desktop" in docker("info", "--format", "{{.OperatingSystem}}").lower():
        return "127.0.0.1", "host.docker.internal"
    address = ipaddress.ip_address(
        docker(
            "network",
            "inspect",
            "bridge",
            "--format",
            "{{(index .IPAM.Config 0).Gateway}}",
        )
    )
    if not address.is_private or address.is_loopback or address.is_unspecified:
        raise ValueError(
            "Docker bridge must have a private, container-reachable gateway"
        )
    return str(address), f"[{address}]" if address.version == 6 else str(address)


def run_process(command: list[str], log: Path, environment: dict[str, str]) -> int:
    """Keep logs and terminate the owned process group when interrupted."""
    with log.open("w", encoding="utf-8") as stream:
        process = subprocess.Popen(
            command,
            stdout=stream,
            stderr=subprocess.STDOUT,
            env=environment,
            cwd=ROOT,
            start_new_session=True,
        )
        try:
            return process.wait(timeout=1800)
        finally:
            if process.poll() is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()


def cleanup(
    run_id: str,
    partial_output: Optional[Path] = None,
    existing_volumes: Optional[set[str]] = None,
) -> None:
    """Let owned containers retain diagnostics before removing any survivors."""
    selector = ("--filter", f"label={LABEL}={run_id}")
    identifiers = docker("ps", "--all", "--quiet", *selector).split()
    workspaces: set[str] = set()
    try:
        if identifiers:
            # Native supervisors publish partial reports when they receive TERM.
            with suppress(subprocess.SubprocessError):
                docker("stop", "--time", "15", *identifiers)
        if partial_output is None:
            return
        native = []
        for identifier in docker(
            "ps", "--all", "--quiet", *selector, "--filter", f"label={LANE_LABEL}"
        ).split():
            container = json.loads(
                docker("inspect", "--format", "{{json .}}", identifier)
            )
            # Recover only ACT's ephemeral checkouts, never shared tool caches.
            workspaces.update(
                mount["Name"]
                for mount in container["Mounts"]
                if mount["Type"] == "volume"
                and mount["Destination"] == container["Config"]["WorkingDir"]
                and mount["Name"].startswith("act-")
                and existing_volumes is not None
                and mount["Name"] not in existing_volumes
            )
            native.append((identifier, container))
        for identifier, container in native:
            config = container["Config"]
            lane = config["Labels"][LANE_LABEL]
            if lane not in {
                "ordinary-tests",
                "native-wx-0",
                "native-wx-1",
                "native-kicad-7",
                "native-kicad-8",
                "native-kicad-9",
                "native-kicad-10",
            }:
                raise RuntimeError(f"Unknown retained test lane: {lane}")
            destination = partial_output / lane
            destination.mkdir(parents=True, exist_ok=True)
            source = Path(config["WorkingDir"]) / ".cache/native-tests" / lane
            docker("cp", f"{identifier}:{source}/.", str(destination))
    finally:
        if identifiers:
            # --rm containers may already have disappeared after graceful shutdown.
            with suppress(subprocess.SubprocessError):
                docker("rm", "--force", *identifiers)
        if docker("ps", "--all", "--quiet", *selector):
            raise RuntimeError("Run-owned containers remain after cleanup")
        if workspaces:
            docker("volume", "rm", *sorted(workspaces))


def copy_tracked(directory: Path) -> None:
    """Copy tracked working files, excluding untracked tests and local state."""
    subprocess.run(
        [
            "git",
            "clone",
            "--shared",
            "--no-checkout",
            "--quiet",
            str(ROOT),
            str(directory),
        ],
        check=True,
    )
    remote = subprocess.check_output(
        ["git", "remote", "get-url", "origin"], cwd=ROOT, text=True
    ).strip()
    subprocess.run(
        ["git", "remote", "set-url", "origin", remote], cwd=directory, check=True
    )
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).split(b"\0")
    for name in filter(None, names):
        source = ROOT / os.fsdecode(name)
        if source.is_file() or source.is_symlink():
            destination = directory / os.fsdecode(name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination, follow_symlinks=False)
    subprocess.run(["git", "add", "--force", "."], cwd=directory, check=True)


def run_workflow(output: Path, cache: Path, selection: str, source: Path) -> int:
    """Execute the shared workflow with one scheduler and private services."""
    architecture = daemon_architecture(selection)
    address, hostname = private_endpoints()
    if any(path == ROOT or ROOT in path.parents for path in (output, cache)):
        raise ValueError("Results and cache must be outside the checkout")
    output.mkdir(parents=True, exist_ok=False)
    cache.mkdir(parents=True, exist_ok=True)
    run_id = uuid4().hex
    common = [
        "act",
        "push",
        f"--directory={source}",
        f"--workflows={source / '.github/workflows/tests.yml'}",
        f"--container-architecture=linux/{architecture}",
        f"--action-cache-path={cache / 'actions'}",
        f"--container-options=--user=0 --label={LABEL}={run_id}",
    ]
    command = [
        *common,
        f"--var=KICAD_VALIDATION_ARCHITECTURE={architecture}",
        f"--env=KICAD_VALIDATION_RUN_ID={run_id}",
        f"--artifact-server-path={output / 'artifacts'}",
        f"--artifact-server-addr={address}",
        "--artifact-server-port=34567",
        f"--cache-server-addr={address}",
        "--cache-server-port=34568",
        f"--cache-server-path={cache / 'downloads'}",
        f"--cache-server-external-url=http://{hostname}:34568",
        f"--env=ACTIONS_RUNTIME_URL=http://{hostname}:34567/",
    ]
    environment = dict(os.environ, ACTIONS_RUNTIME_URL=f"http://{hostname}:34567/")
    existing_volumes = set(docker("volume", "ls", "--quiet").split())
    sys.stdout.write(f"Logs and artifacts: {output}\n")
    sys.stdout.flush()
    status = 1
    try:
        # Initialize shared action repositories serially before concurrent jobs.
        result = run_process(
            [
                *common,
                "--dryrun",
                "--concurrent-jobs=1",
                "--matrix=lane:ordinary-tests",
                "--artifact-server-path=",
                "--no-cache-server",
            ],
            output / "setup.log",
            environment,
        )
        status = result or run_process(command, output / "act.log", environment)
        return status
    finally:
        cleanup(run_id, output / "partial-native" if status else None, existing_volumes)


def run(output: Path, cache: Path, selection: str) -> int:
    """Run current tracked sources without collecting unrelated local files."""
    with tempfile.TemporaryDirectory(prefix="kicad-test-source-") as directory:
        source = Path(directory)
        copy_tracked(source)
        return run_workflow(output, cache, selection, source)


def main(argv: Optional[list[str]] = None) -> int:
    """Choose the daemon architecture and print complete elapsed wall time."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--architecture", choices=("auto", "amd64"), default="auto")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--cache", type=Path, default=Path.home() / ".cache/kicad-jlcpcb-tools/act"
    )
    args = parser.parse_args(argv)
    output = args.output or Path(tempfile.gettempdir()) / f"kicad-tests-{uuid4().hex}"
    started = time.monotonic()

    cancelled = False

    def interrupted(_signum: int, _frame: object) -> None:
        nonlocal cancelled
        if not cancelled:
            cancelled = True
            raise KeyboardInterrupt

    previous = {
        signum: signal.signal(signum, interrupted)
        for signum in (signal.SIGTERM, signal.SIGINT)
    }
    status = 1
    try:
        status = run(output.resolve(), args.cache.resolve(), args.architecture)
    except KeyboardInterrupt:
        status = 130
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        sys.stdout.write(f"Validation failed: {error}\n")
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        sys.stdout.write(
            f"{'PASS' if status == 0 else 'FAIL'}: {time.monotonic() - started:.1f}s\n"
        )
    return status


if __name__ == "__main__":
    raise SystemExit(main())
