"""Competing processes cannot publish while failed exports restore old files."""

from contextlib import ExitStack, closing
import importlib
import multiprocessing
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from .variant_native_support import PACKAGE

counter = importlib.import_module(f"{PACKAGE}.variant.generation_counter")
archive = importlib.import_module(f"{PACKAGE}.fabrication_archive")
ARTIFACT_NAMES = ("board.zip", "board-BOM.csv", "board-CPL.csv")


def _prepared_files(root: Path, generation: str) -> tuple[tuple[Path, Path], ...]:
    """Prepare distinct real artifact bytes without changing published outputs."""
    staging = root / generation
    staging.mkdir()
    pairs = []
    for name in ARTIFACT_NAMES:
        source = staging / name
        source.write_bytes(f"{generation}: {name}".encode())
        pairs.append((source, root / name))
    return tuple(pairs)


def _publish(
    database: Path, pairs: tuple[tuple[Path, Path], ...], timeout: float = 5.0
) -> int:
    """Run the production counter/publication nesting around real files."""
    with (
        counter.publication_guard(database, timeout=timeout),
        ExitStack() as recovery,
        counter.generation_count_transaction(database, expected_count=1) as count,
    ):
        recovery.enter_context(archive.artifact_publication(pairs))
    return count


def _competing_export(directory: str, connection: Any) -> None:
    """Attempt publication during compensation, retrying after its lock releases."""
    try:
        root = Path(directory)
        pairs = _prepared_files(root, "generation-B")
        connection.send("ready")
        if not connection.poll(10) or connection.recv() != "publish":
            raise RuntimeError("The first export did not reach compensation")
        try:
            count = _publish(root / "project.db", pairs, timeout=0.15)
        except TimeoutError:
            connection.send(("blocked", None))
            if not connection.poll(10) or connection.recv() != "retry":
                raise RuntimeError(
                    "The first export did not complete compensation"
                ) from None
            count = _publish(root / "project.db", pairs)
        connection.send(("published", count))
    except BaseException as error:
        connection.send(("error", repr(error)))
    finally:
        connection.close()


def test_failed_commit_cannot_restore_over_another_process_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful competing export retains its complete set and committed count."""
    database = tmp_path / "project.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO metadata VALUES ('generation_count', '1')")
    for name in ARTIFACT_NAMES:
        (tmp_path / name).write_bytes(f"old: {name}".encode())
    pairs = _prepared_files(tmp_path, "generation-A")
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_competing_export, args=(str(tmp_path), child))
    reader = sqlite3.connect(database)
    first_attempt: tuple[str, Any] = ("not attempted", None)
    connect, replace = sqlite3.connect, archive.os.replace

    def connect_quickly(path: Any, **kwargs: Any) -> sqlite3.Connection:
        """Make the real blocked COMMIT fail promptly without faking rollback."""
        return connect(path, timeout=0.02, **kwargs)

    def pause_first_restore(source: Any, destination: Any) -> None:
        """Let another process contend exactly after SQLite releases its writer."""
        nonlocal first_attempt
        if (
            Path(source).parent.name.startswith(".jlcpcb-recovery-")
            and first_attempt[0] == "not attempted"
        ):
            reader.close()
            parent.send("publish")
            assert parent.poll(10), "The competing process did not report its attempt"
            first_attempt = parent.recv()
            assert first_attempt[0] in {"blocked", "published"}, first_attempt
        replace(source, destination)

    monkeypatch.setattr(counter.sqlite3, "connect", connect_quickly)
    monkeypatch.setattr(archive.os, "replace", pause_first_restore)
    process.start()
    try:
        assert parent.poll(10)
        assert parent.recv() == "ready"
        reader.execute("BEGIN")
        assert reader.execute("SELECT value FROM metadata").fetchone() == ("1",)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            _publish(database, pairs)
        if first_attempt[0] == "blocked":
            assert all(
                (tmp_path / name).read_bytes() == f"old: {name}".encode()
                for name in ARTIFACT_NAMES
            )
            parent.send("retry")
            assert parent.poll(10)
            assert parent.recv() == ("published", 2)
        process.join(timeout=10)
        assert process.exitcode == 0
        assert {name: (tmp_path / name).read_bytes() for name in ARTIFACT_NAMES} == {
            name: f"generation-B: {name}".encode() for name in ARTIFACT_NAMES
        }
        with closing(connect(database)) as connection:
            assert connection.execute("SELECT value FROM metadata").fetchone() == ("2",)
        assert first_attempt == ("blocked", None)
        assert not list(tmp_path.glob(".jlcpcb-recovery-*"))
    finally:
        reader.close()
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        parent.close()
        child.close()
