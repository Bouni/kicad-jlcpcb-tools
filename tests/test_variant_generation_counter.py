"""Real SQLite publication reservations and failures preserve the project counter."""
# ruff: noqa: D103

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import errno
import importlib
from pathlib import Path
import sqlite3
from threading import Barrier
from typing import Any

import pytest

from .variant_native_support import PACKAGE

counter = importlib.import_module(f"{PACKAGE}.variant.generation_counter")
lock_module = importlib.import_module(f"{PACKAGE}.core.file_lock")


def _seed(path: Path, count: str = "4") -> None:
    """Create the ordinary project's schema with data the helper must preserve."""
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute(
            "CREATE TABLE metadata (key TEXT NOT NULL PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO metadata VALUES (?, ?)",
            [("generation_count", count), ("unrelated", "keep")],
        )
        connection.execute("CREATE TABLE part_info (reference TEXT, lcsc TEXT)")
        connection.execute("INSERT INTO part_info VALUES ('R1', 'C100')")


def _read(path: Path) -> str:
    """Observe the committed count through a separate connection."""
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT value FROM metadata WHERE key = 'generation_count'"
        ).fetchone()[0]


def _short_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep real SQLite busy failures quick without faking transaction behavior."""
    connect = sqlite3.connect

    def connect_quickly(path: Any, **kwargs: Any) -> sqlite3.Connection:
        return connect(path, timeout=0.02, **kwargs)

    monkeypatch.setattr(counter.sqlite3, "connect", connect_quickly)


def test_count_is_uncommitted_until_publication_succeeds(tmp_path: Path) -> None:
    path = tmp_path / "project.db"
    _seed(path)
    with counter.generation_count_transaction(path, expected_count=4) as count:
        assert count == 5
        assert _read(path) == "4"
    assert _read(path) == "5"
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT * FROM metadata ORDER BY key").fetchall() == [
            ("generation_count", "5"),
            ("unrelated", "keep"),
        ]
        assert connection.execute("SELECT * FROM part_info").fetchall() == [
            ("R1", "C100")
        ]
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall() == [("metadata",), ("part_info",)]


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_failed_publication_rolls_back_and_releases_writer(
    tmp_path: Path, failure: type[BaseException]
) -> None:
    path = tmp_path / "project.db"
    _seed(path)
    with (
        pytest.raises(failure, match="publication failed"),
        counter.generation_count_transaction(path, expected_count=4),
    ):
        raise failure("publication failed")
    assert _read(path) == "4"
    with counter.generation_count_transaction(path, expected_count=4) as count:
        assert count == 5
    assert _read(path) == "5"


def test_failed_first_publication_rolls_back_new_metadata(tmp_path: Path) -> None:
    path = tmp_path / "jlcpcb" / "project.db"
    with (
        pytest.raises(OSError, match="publication failed"),
        counter.generation_count_transaction(str(path), expected_count=0),
    ):
        raise OSError("publication failed")
    with closing(sqlite3.connect(path)) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            == []
        )
    with counter.generation_count_transaction(path, expected_count=0) as count:
        assert count == 1
    assert _read(path) == "1"


@pytest.mark.parametrize("raw_count", ["not a number", "-3", "0"])
def test_existing_invalid_counts_keep_the_normalized_zero_contract(
    tmp_path: Path, raw_count: str
) -> None:
    path = tmp_path / "project.db"
    _seed(path, raw_count)
    with counter.generation_count_transaction(path, expected_count=0) as count:
        assert count == 1
    assert _read(path) == "1"


def test_readonly_database_fails_before_publication(tmp_path: Path) -> None:
    path = tmp_path / "project.db"
    _seed(path)
    path.chmod(0o444)
    entered = False
    try:
        with (
            pytest.raises(sqlite3.OperationalError, match="readonly"),
            counter.generation_count_transaction(path, expected_count=4),
        ):
            entered = True
    finally:
        path.chmod(0o600)
    assert not entered
    assert _read(path) == "4"


def test_competing_writer_fails_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "project.db"
    _seed(path)
    _short_timeout(monkeypatch)
    entered = False
    with closing(sqlite3.connect(path)) as other:
        other.execute("BEGIN IMMEDIATE")
        with (
            pytest.raises(sqlite3.OperationalError, match="locked"),
            counter.generation_count_transaction(path, expected_count=4),
        ):
            entered = True
    assert not entered
    assert _read(path) == "4"
    with counter.generation_count_transaction(path, expected_count=4) as count:
        assert count == 5


def test_busy_commit_rolls_back_after_publication_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real read transaction permits reservation but prevents the final commit."""
    path = tmp_path / "project.db"
    _seed(path)
    _short_timeout(monkeypatch)
    entered = False
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as reader:
        reader.execute("BEGIN")
        assert reader.execute("SELECT value FROM metadata").fetchall()
        with (
            pytest.raises(sqlite3.OperationalError, match="locked"),
            counter.generation_count_transaction(path, expected_count=4) as count,
        ):
            entered = True
            assert count == 5
    assert entered
    assert _read(path) == "4"
    with counter.generation_count_transaction(path, expected_count=4) as count:
        assert count == 5
    assert _read(path) == "5"


def test_expected_count_conflict_prevents_stale_output_publication(
    tmp_path: Path,
) -> None:
    path = tmp_path / "project.db"
    _seed(path)
    with counter.generation_count_transaction(path, expected_count=4):
        pass
    entered = False
    with (
        pytest.raises(counter.GenerationCountConflict, match="changed"),
        counter.generation_count_transaction(path, expected_count=4),
    ):
        entered = True
    assert not entered
    assert _read(path) == "5"


def test_concurrent_reservations_cannot_lose_increments(tmp_path: Path) -> None:
    path = tmp_path / "project.db"
    _seed(path, "0")
    ready = Barrier(4)

    def reserve() -> int:
        ready.wait(timeout=5)
        with counter.generation_count_transaction(path) as count:
            return count

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(reserve) for _ in range(4)]
        assert sorted(future.result(timeout=5) for future in futures) == [1, 2, 3, 4]
    assert _read(path) == "4"


def test_only_one_competing_export_can_publish_a_prepared_count(tmp_path: Path) -> None:
    path = tmp_path / "project.db"
    _seed(path, "0")
    ready = Barrier(2)
    published = []

    def reserve() -> str:
        ready.wait(timeout=5)
        try:
            with counter.generation_count_transaction(path, expected_count=0) as count:
                published.append(count)
        except counter.GenerationCountConflict:
            return "retry"
        return "published"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(reserve) for _ in range(2)]
        assert sorted(future.result(timeout=5) for future in futures) == [
            "published",
            "retry",
        ]
    assert published == [1]
    assert _read(path) == "1"


def test_publication_guard_has_a_stable_sidecar_and_releases_after_failure(
    tmp_path: Path,
) -> None:
    path = tmp_path / "jlcpcb" / "project.db"
    lock_path = Path(str(path) + ".generation.lock")
    with (
        pytest.raises(RuntimeError, match="publication failed"),
        counter.publication_guard(path),
    ):
        assert lock_path.is_file()
        inode = lock_path.stat().st_ino
        assert not path.exists()
        raise RuntimeError("publication failed")
    with counter.publication_guard(path):
        assert lock_path.stat().st_ino == inode
        assert not path.exists()
    assert lock_path.is_file()


def test_publication_guard_blocks_threads_only_for_the_same_project(
    tmp_path: Path,
) -> None:
    first, second = tmp_path / "first.db", tmp_path / "second.db"

    def attempt(path: Path) -> None:
        with counter.publication_guard(path, timeout=0.02):
            pass

    with (
        counter.publication_guard(first),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        blocked = executor.submit(attempt, first)
        independent = executor.submit(attempt, second)
        with pytest.raises(TimeoutError, match="publishing fabrication files"):
            blocked.result(timeout=5)
        assert independent.result(timeout=5) is None
    with counter.publication_guard(first, timeout=0):
        pass


def test_nested_independent_file_locks_do_not_block_each_other(tmp_path: Path) -> None:
    with (
        lock_module.file_lock(tmp_path / "settings.lock", timeout=0),
        lock_module.file_lock(tmp_path / "publication.lock", timeout=0),
    ):
        assert sorted(path.name for path in tmp_path.iterdir()) == [
            "publication.lock",
            "settings.lock",
        ]


def test_database_symlink_alias_uses_the_same_publication_guard(tmp_path: Path) -> None:
    path = tmp_path / "project.db"
    _seed(path)
    alias = tmp_path / "alias.db"
    alias.symlink_to(path)
    with (
        counter.publication_guard(path),
        pytest.raises(TimeoutError, match="publishing fabrication files"),
        counter.publication_guard(alias, timeout=0),
    ):
        pytest.fail("An alias bypassed the publication guard")


def test_os_lock_error_prevents_publication_and_releases_thread_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "project.db"
    entered = False

    def fail(_handle: Any) -> None:
        raise OSError(errno.EIO, "lock unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(lock_module, "_try_file_lock", fail)
        with (
            pytest.raises(OSError, match="lock unavailable"),
            counter.publication_guard(path),
        ):
            entered = True
    assert not entered
    with counter.publication_guard(path, timeout=0):
        pass


def test_unlock_error_cannot_report_committed_generation_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "project.db"

    def fail(_handle: Any) -> None:
        raise OSError(errno.EIO, "unlock unavailable")

    monkeypatch.setattr(lock_module, "_unlock_file", fail)
    with counter.publication_guard(path), counter.generation_count_transaction(path):
        pass
    assert _read(path) == "1"
    # Handle closure still frees the OS lock after an explicit unlock error.
    with counter.publication_guard(path, timeout=0):
        pass
