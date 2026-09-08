"""Require complete project assignment batches to commit or roll back together."""

from contextlib import closing
from pathlib import Path
import sqlite3
import types
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from .wx_harness import load, package_stubs, wx_stubs

_PACKAGE = "_store_assignment_batch_tests"
Assignment = tuple[str, str, Optional[int]]


class _EmptyBoard:
    """Allow the real Store constructor to finish board reconciliation."""

    def GetFootprints(self) -> list[Any]:
        """Return no footprints before the test seeds persisted project rows."""
        return []


@pytest.fixture
def store_module() -> types.ModuleType:
    """Load real project persistence with only the unused GUI imports stubbed."""
    stubs = {**package_stubs(_PACKAGE), **wx_stubs()}
    return load(_PACKAGE, "store", stubs)


@pytest.fixture
def project_store(store_module: types.ModuleType, tmp_path: Path) -> Any:
    """Create the real database and retain distinct assignments with enrichment."""
    store = store_module.Store(
        types.SimpleNamespace(settings={}), str(tmp_path), _EmptyBoard()
    )
    with closing(sqlite3.connect(store.dbfile)) as connection, connection:
        connection.executemany(
            "INSERT INTO part_info ("
            "reference, value, footprint, lcsc, stock, assembly_process, "
            "component_product_type, pad_count, has_tht, assembly_flags"
            ") VALUES (?, '10k', 'R_0603', ?, ?, ?, ?, 2, 0, 'test flags')",
            [
                ("R1", "C100", 11, "SMT", 1),
                ("R2", "C200", 22, "THT", 2),
                ("R3", "C300", 33, "SMT", 0),
            ],
        )
    return store


def _persisted_rows(store: Any) -> list[tuple[Any, ...]]:
    """Read all durable assignment and metadata state on a fresh connection."""
    with closing(sqlite3.connect(store.dbfile)) as connection:
        return connection.execute(
            "SELECT reference, lcsc, stock, assembly_process, "
            "component_product_type, pad_count, has_tht, assembly_flags "
            "FROM part_info ORDER BY reference"
        ).fetchall()


def _reject_second_update(store: Any, column: str) -> None:
    """Raise after the first reference was updated within a requested batch."""
    assert column in {"lcsc", "stock"}
    with closing(sqlite3.connect(store.dbfile)) as connection, connection:
        connection.execute(
            f"CREATE TRIGGER reject_second_assignment BEFORE UPDATE OF {column} "
            "ON part_info WHEN NEW.reference = 'R2' "
            "BEGIN SELECT RAISE(ABORT, 'second assignment failed'); END"
        )


def test_assignment_batch_persists_ids_stock_and_metadata_together(
    project_store: Any,
    store_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commit one mixed-ID action, preserving unrelated rows and board metadata."""
    connect = MagicMock(wraps=sqlite3.connect)
    monkeypatch.setattr(store_module.sqlite3, "connect", connect)
    assignments = (("R1", "C456", 100), ("R2", "C789", None))

    project_store.set_lcsc_assignments(assignment for assignment in assignments)

    connect.assert_called_once_with(project_store.dbfile)
    assert _persisted_rows(project_store) == [
        ("R1", "C456", 100, "", None, 2, 0, "test flags"),
        ("R2", "C789", None, "", None, 2, 0, "test flags"),
        ("R3", "C300", 33, "SMT", 0, 2, 0, "test flags"),
    ]


@pytest.mark.parametrize("column", ["lcsc", "stock"])
@pytest.mark.parametrize(
    "assignments",
    [
        [("R1", "C999", 99), ("R2", "C888", 88)],
        [("R1", "", None), ("R2", "", None)],
    ],
    ids=["assign", "clear"],
)
def test_assignment_batch_failure_preserves_every_previous_row(
    project_store: Any, column: str, assignments: list[Assignment]
) -> None:
    """A later write error must undo IDs, stock, and enrichment on every row."""
    before = _persisted_rows(project_store)
    _reject_second_update(project_store, column)

    with pytest.raises(sqlite3.IntegrityError, match="second assignment failed"):
        project_store.set_lcsc_assignments(assignments)

    assert _persisted_rows(project_store) == before


def test_assignment_batch_clears_stock_and_enrichment_with_ids(
    project_store: Any,
) -> None:
    """Clearing a selection invalidates all part-specific state in one action."""
    untouched = _persisted_rows(project_store)[-1]

    project_store.set_lcsc_assignments([("R1", "", None), ("R2", "", None)])

    assert _persisted_rows(project_store) == [
        ("R1", "", None, "", None, 2, 0, "test flags"),
        ("R2", "", None, "", None, 2, 0, "test flags"),
        untouched,
    ]


def test_empty_assignment_batch_does_not_open_database(
    project_store: Any,
    store_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty action needs no persistence and remains harmless if DB is locked."""
    connect = MagicMock(side_effect=sqlite3.OperationalError("database is locked"))
    monkeypatch.setattr(store_module.sqlite3, "connect", connect)

    project_store.set_lcsc_assignments(iter(()))

    connect.assert_not_called()
