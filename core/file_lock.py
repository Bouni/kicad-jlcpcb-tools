"""Coordinate short file update sections across threads and KiCad processes."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress
import errno
import os
from pathlib import Path
from threading import Lock
import time
from typing import BinaryIO, Optional, Union

if os.name == "nt":
    import msvcrt
else:
    import fcntl

_LOCKS: dict[str, Lock] = {}
_REGISTRY_LOCK = Lock()


def _try_file_lock(handle: BinaryIO) -> None:
    """Acquire an OS lock without blocking beyond the caller's deadline."""
    if os.name == "nt":
        # locking() permits a region beyond EOF, including an empty lock file:
        # https://docs.python.org/3.9/library/msvcrt.html#msvcrt.locking
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: BinaryIO) -> None:
    """Release exactly the region acquired by _try_file_lock()."""
    if os.name == "nt":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def file_lock(
    path: Union[str, Path],
    timeout: float = 5.0,
    timeout_message: Optional[str] = None,
) -> Iterator[None]:
    """Hold a stable sidecar lock, with one deadline for thread and process waits.

    The caller creates the containing directory. Never remove or replace the
    lock file: its stable inode keeps all processes on the same OS lock.
    """
    if timeout < 0:
        raise ValueError("File lock timeout must be nonnegative.")
    path = Path(path).resolve()
    key = os.path.normcase(str(path))
    with _REGISTRY_LOCK:
        process_lock = _LOCKS.setdefault(key, Lock())
    message = timeout_message or f"Timed out waiting for file lock: {path}"
    deadline = time.monotonic() + timeout
    if not process_lock.acquire(timeout=timeout):
        raise TimeoutError(message)
    try:
        with path.open("a+b") as handle:
            while True:
                try:
                    _try_file_lock(handle)
                    break
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(message) from error
                    time.sleep(min(0.05, remaining))
            try:
                yield
            finally:
                # Closing also releases the lock. An unlock failure must not
                # misreport a committed operation as a failed operation.
                with suppress(OSError):
                    _unlock_file(handle)
    finally:
        process_lock.release()
