"""Nominal width results persist per board as soon as each solve settles.

Before this cache, results lived only in the dialog's working copy and reached
project.db on "Save and close" alone; Cancel, closing the window, or quitting
KiCad discarded every completed solve and the next open showed "waiting for
JLCPCB". These checks run against a real temporary sqlite database.
"""

import contextlib
from pathlib import Path
import sqlite3

import pytest

from impedance.database import ImpedanceDatabase
from impedance.repository import ImpedanceRepository
from impedance.stackup_model import MAX_WIDTH_RESULTS, WidthResult


def _board(project: Path, name: str = "main") -> Path:
    path = project / f"{name}.kicad_pcb"
    path.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")
    return path


def _result(
    digest_char: str,
    *,
    status: str = "success",
    layer: str = "F.Cu",
    calculated_at_utc: str = "2026-09-26T00:00:00.000000Z",
) -> WidthResult:
    return WidthResult(
        spec_id="rf",
        layer=layer,
        input_digest=digest_char * 64,
        status=status,
        target_width_nm=181_400 if status == "success" else None,
        calculated_at_utc=calculated_at_utc,
        model="CoatedMicrostrip1B" if status == "success" else "",
        calculation_digest="b" * 64 if status == "success" else "",
        message="" if status == "success" else "Not supported.",
    )


@pytest.fixture
def repository(tmp_path: Path) -> ImpedanceRepository:
    """Register one board in a fresh temporary project database."""
    database = ImpedanceDatabase(tmp_path / "jlcpcb" / "project.db")
    board_id = database.resolve_board(_board(tmp_path))
    return ImpedanceRepository(database, board_id)


def test_width_result_round_trips_without_touching_config_revision(
    repository: ImpedanceRepository,
) -> None:
    """A solved width survives reopen and never bumps board_feature_config."""
    from impedance.model import Config

    revision = repository.save(Config(), 0)
    success = _result("a")
    unsupported = _result("c", status="unsupported")

    repository.save_width_result(success)
    repository.save_width_result(unsupported)

    reopened = ImpedanceRepository(
        ImpedanceDatabase(repository.database.path, initialize=False),
        repository.board_id,
    )
    assert set(reopened.load_width_results()) == {success, unsupported}
    _config, current_revision = reopened.load()
    assert current_revision == revision


@pytest.mark.parametrize("status", ["unavailable", "error", "pending"])
def test_transient_results_are_refused_by_the_cache(
    repository: ImpedanceRepository, status: str
) -> None:
    """Only settled outcomes are cached; transient failures must be retried."""
    from impedance.model import ValidationError

    with pytest.raises(ValidationError):
        repository.save_width_result(_result("d", status=status))
    assert repository.load_width_results() == ()


def test_same_digest_upserts_and_other_stackups_are_retained(
    repository: ImpedanceRepository,
) -> None:
    """Re-solving identical inputs replaces its row; other digests stay cached."""
    first = _result("a", calculated_at_utc="2026-09-26T00:00:00.000000Z")
    other_stackup = _result("e")
    repository.save_width_result(first)
    repository.save_width_result(other_stackup)
    newer = WidthResult(
        **{
            **first.__dict__,
            "target_width_nm": 190_000,
            "calculated_at_utc": "2026-09-26T01:00:00.000000Z",
        }
    )

    repository.save_width_result(newer)

    cached = repository.load_width_results()
    assert len(cached) == 2
    assert newer in cached and other_stackup in cached and first not in cached


def test_cache_is_bounded_per_board_by_newest_calculation_time(
    tmp_path: Path,
) -> None:
    """The per-board row count cannot grow past MAX_WIDTH_RESULTS."""
    database = ImpedanceDatabase(tmp_path / "jlcpcb" / "project.db")
    board_id = database.resolve_board(_board(tmp_path))
    oldest = "2026-01-01T00:00:00.000000Z"
    for index in range(3):
        database.save_width_result(
            board_id,
            f"{index:064x}",
            "rf",
            "F.Cu",
            f"2026-02-0{index + 1}T00:00:00.000000Z" if index else oldest,
            {"index": index},
            max_rows=2,
        )
    remaining = database.load_width_results(board_id)
    assert [item["index"] for item in remaining] == [2, 1]
    assert MAX_WIDTH_RESULTS >= 2


def test_malformed_rows_are_skipped_not_fatal(
    repository: ImpedanceRepository,
) -> None:
    """One damaged row must not stop the dialog from seeding the good ones."""
    good = _result("a")
    repository.save_width_result(good)
    with contextlib.closing(sqlite3.connect(repository.database.path)) as conn, conn:
        conn.execute(
            "INSERT INTO impedance_width_cache VALUES (?, ?, 'rf', 'F.Cu', '', ?)",
            (repository.board_id, "f" * 64, "not json"),
        )
        conn.execute(
            "INSERT INTO impedance_width_cache VALUES (?, ?, 'rf', 'F.Cu', '', ?)",
            (repository.board_id, "e" * 64, '{"spec_id": "rf"}'),
        )

    assert repository.load_width_results() == (good,)


def test_deeply_nested_cache_row_preserves_valid_neighbors_without_writes(
    repository: ImpedanceRepository,
) -> None:
    """A decoding limit skips only that row, retaining neighbors on read/reopen."""
    from impedance.model import Config

    revision = repository.save(Config(enabled=True), 0)
    newer = _result("a")
    older = _result("c", calculated_at_utc="2026-09-24T00:00:00.000000Z")
    repository.save_width_result(newer)
    repository.save_width_result(older)
    nested = '{"bad":' + "[" * 10_000 + "0" + "]" * 10_000 + "}"
    assert len(nested.encode("utf-8")) < 65_536
    database = repository.database
    with database.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO impedance_width_cache VALUES (?, ?, 'rf', 'F.Cu', ?, ?)",
            (
                repository.board_id,
                "d" * 64,
                "2026-09-25T00:00:00.000000Z",
                nested,
            ),
        )
    before_bytes = database.path.read_bytes()
    with database.connect() as connection:
        before_contents = tuple(connection.iterdump())

    assert repository.load_width_results() == (newer, older)
    reopened = ImpedanceRepository(
        ImpedanceDatabase(database.path, initialize=False), repository.board_id
    )
    assert reopened.load_width_results() == (newer, older)
    assert reopened.load() == (Config(enabled=True), revision)
    assert database.path.read_bytes() == before_bytes
    with database.connect() as connection:
        assert tuple(connection.iterdump()) == before_contents


def test_cache_is_scoped_to_its_board(tmp_path: Path) -> None:
    """Two PCBs in one project must not see each other's solved widths."""
    database = ImpedanceDatabase(tmp_path / "jlcpcb" / "project.db")
    first = ImpedanceRepository(database, database.resolve_board(_board(tmp_path)))
    second = ImpedanceRepository(
        database, database.resolve_board(_board(tmp_path, "panel"))
    )
    first.save_width_result(_result("a"))

    assert second.load_width_results() == ()
    assert len(first.load_width_results()) == 1


def test_reading_an_uninitialized_store_returns_no_results(tmp_path: Path) -> None:
    """Looking for cached widths never creates the database."""
    path = tmp_path / "jlcpcb" / "project.db"
    database = ImpedanceDatabase(path, initialize=False)

    assert database.load_width_results("any-board") == []
    assert not path.exists()


def test_older_project_database_without_the_cache_table_upgrades_in_place(
    repository: ImpedanceRepository,
) -> None:
    """A project.db from a release before the cache still opens, reads and writes."""
    with repository.database.connect(write=True) as conn:
        conn.execute("DROP TABLE impedance_width_cache")
    older = ImpedanceRepository(
        ImpedanceDatabase(repository.database.path, initialize=False),
        repository.board_id,
    )

    assert older.load_width_results() == ()
    assert older.load()[1] == 0

    older.save_width_result(_result("a"))
    assert older.load_width_results() == (_result("a"),)


def test_rejections_are_stamped_so_they_are_not_evicted_first(
    repository: ImpedanceRepository,
) -> None:
    """A rejection has no solve time; bounding must not drop it before old successes."""
    old_success = _result("a", calculated_at_utc="2020-01-01T00:00:00.000000Z")
    repository.save_width_result(old_success)
    repository.save_width_result(
        _result("c", status="unsupported", calculated_at_utc="")
    )

    with repository.database.connect() as conn:
        stamps = dict(
            conn.execute(
                "SELECT input_digest, calculated_at_utc FROM impedance_width_cache"
            )
        )
    assert stamps["c" * 64] > stamps["a" * 64]
