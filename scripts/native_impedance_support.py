"""Own isolated native impedance processes and publish their diagnostic evidence."""

from collections.abc import Iterator
from contextlib import contextmanager, suppress
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any, Optional


class RunnerError(Exception):
    """Reject a native process launch after cancellation has begun."""


def write_json(path: Path, value: Any) -> None:
    """Atomically publish complete evidence, never a partially written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


@contextmanager
def private_environment(parent: Path) -> Iterator[dict[str, str]]:
    """Give a display process and all native descendants disposable private state."""
    with tempfile.TemporaryDirectory(prefix="native-wx-", dir=parent) as temporary:
        root = Path(temporary)
        env = dict(os.environ)
        for key, name in (
            ("XDG_CONFIG_HOME", "config"),
            ("XDG_CACHE_HOME", "cache"),
            ("TMPDIR", "tmp"),
            ("KICAD_CONFIG_HOME", "kicad"),
            ("XDG_RUNTIME_DIR", "runtime"),
        ):
            directory = root / name
            directory.mkdir(mode=0o700)
            env[key] = str(directory)
        # Exact-case native children inherit the private cache, without any
        # shard selector. Their default temporary roots follow private TMPDIR.
        options = shlex.split(env.get("PYTEST_ADDOPTS", ""))
        env["PYTEST_ADDOPTS"] = shlex.join(
            options + ["-o", f"cache_dir={env['XDG_CACHE_HOME']}"]
        )
        yield env


class ProcessSupervisor:
    """Own and reap complete display/pytest process groups, including on failure."""

    def __init__(self) -> None:
        self._processes: set[subprocess.Popen[Any]] = set()
        self._lock = threading.Lock()
        self._stopping = False
        self._stop_locks: dict[Any, Any] = {}
        self._stopped: set[Any] = set()

    def __enter__(self) -> "ProcessSupervisor":
        """Retain ownership until every subprocess has been reaped."""
        return self

    def __exit__(self, *_args: Any) -> None:
        """Clean up descendants even when the caller raises or is interrupted."""
        self.stop_all()

    def _stop(self, process: subprocess.Popen[Any]) -> None:
        """Serialize termination and mark completion only after successful reaping."""
        with self._lock:
            lock = self._stop_locks.setdefault(process, threading.Lock())
        with lock:
            if process not in self._stopped:
                self._terminate_group(process)
                self._stopped.add(process)

    @staticmethod
    def _terminate_group(process: subprocess.Popen[Any]) -> None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        finally:
            # The wrapper may have exited before an uncooperative descendant.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()

    def stop_all(self) -> None:
        """Stop all currently owned groups before disposable state is removed."""
        with self._lock:
            self._stopping = True
            processes = list(self._processes)
        for process in processes:
            self._stop(process)

    def run(
        self,
        command: list[str],
        cwd: Path,
        env: dict[str, str],
        log: Path,
        timeout: float,
        *,
        cancelled: Optional[threading.Event] = None,
    ) -> dict[str, Any]:
        """Run one fresh session, recording wall duration and timeout evidence."""
        started = time.monotonic()
        timed_out = False
        with log.open("x", encoding="utf-8") as output:
            with self._lock:
                if self._stopping or (cancelled is not None and cancelled.is_set()):
                    raise RunnerError("Worker launch cancelled during cleanup")
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=env,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                self._processes.add(process)
            try:
                try:
                    if cancelled is None:
                        process.wait(timeout=timeout)
                    else:
                        while process.poll() is None and not cancelled.is_set():
                            if time.monotonic() - started >= timeout:
                                raise subprocess.TimeoutExpired(command, timeout)
                            cancelled.wait(0.02)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    self._stop(process)
            finally:
                self._stop(process)
                with self._lock:
                    self._processes.discard(process)
        return {
            "returncode": process.returncode,
            "timed_out": timed_out,
            "cancelled": cancelled is not None and cancelled.is_set(),
            "wall_seconds": time.monotonic() - started,
        }
