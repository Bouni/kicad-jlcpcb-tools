"""Store, validate and carry per-part corrections beside the pattern rules."""

from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest

from tests.correction_test_support import fresh_library, make_library, raw_rows
from tests.wx_harness import load_correction_modules


@pytest.fixture
def modules() -> Iterator[SimpleNamespace]:
    """Keep the real storage and value modules under one scoped package."""
    with load_correction_modules() as loaded:
        yield loaded


@pytest.fixture
def library(modules: SimpleNamespace, tmp_path: Path) -> Any:
    """Create real SQLite correction storage away from user databases."""
    return make_library(modules.library, tmp_path)


def execute(path: str, sql: str, parameters: tuple = ()) -> list[tuple[Any, ...]]:
    """Run one statement through an independent connection."""
    with closing(sqlite3.connect(path)) as con, con:
        return con.execute(sql, parameters).fetchall()


def lcsc_rows(library: Any, path: Any = None) -> list[tuple[Any, ...]]:
    """Read the exact persisted part-number rows."""
    return execute(
        path or library.correctionsdb_file,
        "SELECT rowid, * FROM lcsc_correction ORDER BY rowid",
    )


def table_names(path: str) -> set[str]:
    """Return the ordinary tables in the database at path."""
    return {
        row[0]
        for row in execute(path, "SELECT name FROM sqlite_master WHERE type='table'")
    }


def part_rules(modules: SimpleNamespace, library: Any, path: Any = None) -> list:
    """Expose the validated part-number rules of a fresh snapshot."""
    snapshot = fresh_library(library).read_correction_data(path)
    assert snapshot.corrections is not None, snapshot.issues
    return [
        (rule.lcsc, rule.rotation, rule.offset)
        for rule in snapshot.corrections
        if isinstance(rule, modules.data.LcscCorrection)
    ]


def test_part_rule_round_trips_through_its_own_table(
    modules: SimpleNamespace, library: Any
) -> None:
    """A part-number rule is stored beside, not among, the pattern rules."""
    rowid = library.insert_lcsc_correction_data("C12345", 90, (0.5, -0.25))

    assert lcsc_rows(library) == [(rowid, "C12345", 90, 0.5, -0.25)]
    assert raw_rows(library) == []
    snapshot = fresh_library(library).read_correction_data()
    assert snapshot.state is modules.library.CorrectionState.READY
    (row,) = snapshot.rows
    assert (row.kind, row.rowid, row.pattern) == ("lcsc", rowid, "C12345")
    assert row.correction == modules.data.LcscCorrection("C12345", 90, (0.5, -0.25))
    assert snapshot.corrections == (row.correction,)


@pytest.mark.parametrize("spelling", ["c12345", "  C12345 ", "\tc12345\n"])
def test_key_is_stored_in_canonical_form(library: Any, spelling: str) -> None:
    """Whatever spelling arrives, the table holds the form lookups use."""
    library.insert_lcsc_correction_data(spelling, 90, (0, 0))
    assert [row[1] for row in lcsc_rows(library)] == ["C12345"]


@pytest.mark.parametrize(
    "key", ["^C12345$", "C12345|C999", "SOT-23", "", "C", " ", None, 12345]
)
def test_invalid_key_is_rejected_before_any_write(
    modules: SimpleNamespace, library: Any, key: Any
) -> None:
    """An exact-match key that is not a bare part number would never fire."""
    with pytest.raises(modules.data.CorrectionDataError) as error:
        library.insert_lcsc_correction_data(key, 90, (0, 0))
    assert [issue.field for issue in error.value.issues] == ["lcsc"]
    assert "C12345" in str(error.value)
    assert lcsc_rows(library) == []


@pytest.mark.parametrize(
    ("rotation", "offset", "field"),
    [("47u", (0, 0), "rotation"), (90, ("x", 0), "offset_x"), (90, (0,), "offset")],
)
def test_numbers_share_the_pattern_rules_validation(
    modules: SimpleNamespace, library: Any, rotation: Any, offset: Any, field: str
) -> None:
    """Rotation and offsets are checked the same way for both kinds of rule."""
    with pytest.raises(modules.data.CorrectionDataError) as error:
        library.insert_lcsc_correction_data("C1", rotation, offset)
    assert [issue.field for issue in error.value.issues] == [field]
    assert lcsc_rows(library) == []


def test_exact_keys_do_not_collide_by_prefix(
    modules: SimpleNamespace, library: Any
) -> None:
    """C1234 and C12345 are two rules, not one rule and a duplicate."""
    library.insert_lcsc_correction_data("C12345", 90, (0, 0))
    library.insert_lcsc_correction_data("C1234", 180, (0, 0))
    assert part_rules(modules, library) == [
        ("C1234", 180, (0.0, 0.0)),
        ("C12345", 90, (0.0, 0.0)),
    ]


@pytest.mark.parametrize("spelling", ["C12345", "c12345", " C12345"])
def test_duplicate_key_is_refused_without_replace(
    modules: SimpleNamespace, library: Any, spelling: str
) -> None:
    """A second rule for the same part, however spelled, needs explicit replacement."""
    library.insert_lcsc_correction_data("C12345", 90, (0, 0))
    before = lcsc_rows(library)
    with pytest.raises(modules.data.CorrectionDataError) as error:
        library.insert_lcsc_correction_data(spelling, 180, (0, 0))
    assert "already uses this part number" in str(error.value)
    assert lcsc_rows(library) == before


def test_replace_removes_every_row_for_that_part(
    modules: SimpleNamespace, library: Any
) -> None:
    """Replacing collapses case variants of one part number into the saved row."""
    execute(
        library.correctionsdb_file,
        "INSERT INTO lcsc_correction VALUES ('C12345', 90, 0, 0), ('c12345', 45, 1, 1)",
    )
    rowid = library.save_correction_data(
        "C12345", 180, (0, 0), kind=modules.data.KIND_LCSC, replace=True
    )
    assert lcsc_rows(library) == [(rowid, "C12345", 180, 0.0, 0.0)]


def test_invalid_stored_row_needs_repair_and_repairs_in_place(
    modules: SimpleNamespace, library: Any
) -> None:
    """A malformed part rule blocks the set until it is fixed, like a pattern rule."""
    execute(
        library.correctionsdb_file,
        "INSERT INTO lcsc_correction VALUES ('C1', '47u', 0, 0)",
    )
    snapshot = library.read_correction_data()
    assert snapshot.state is modules.library.CorrectionState.NEEDS_REPAIR
    assert snapshot.corrections is None
    (row,) = snapshot.rows
    assert row.kind == "lcsc"
    assert [issue.field for issue in row.issues] == ["rotation"]
    assert row.issues[0].rowid == row.rowid

    library.save_correction_data(
        "C1", 90, (0, 0), rowid=row.rowid, expected_record=row, kind=row.kind
    )
    assert lcsc_rows(library) == [(row.rowid, "C1", 90, 0.0, 0.0)]
    assert part_rules(modules, library) == [("C1", 90, (0.0, 0.0))]


def test_case_variants_with_different_values_conflict(
    modules: SimpleNamespace, library: Any
) -> None:
    """Two spellings of one part number cannot both claim to be its rule."""
    execute(
        library.correctionsdb_file,
        "INSERT INTO lcsc_correction VALUES ('C1', 90, 0, 0), ('c1', 180, 0, 0)",
    )
    snapshot = library.read_correction_data()
    assert snapshot.corrections is None
    assert all("part number" in str(row.issues[0]) for row in snapshot.rows)
    assert {issue.rowid for issue in snapshot.issues} == {1, 2}


def test_case_variants_with_identical_values_resolve_to_one_rule(
    modules: SimpleNamespace, library: Any
) -> None:
    """Agreeing duplicates are redundant, not contradictory."""
    execute(
        library.correctionsdb_file,
        "INSERT INTO lcsc_correction VALUES ('C1', 90, 0, 0), ('c1', 90, 0, 0)",
    )
    assert part_rules(modules, library) == [("C1", 90, (0.0, 0.0))]


def test_rowids_are_independent_across_the_two_tables(
    modules: SimpleNamespace, library: Any
) -> None:
    """Deleting row 1 of one kind must not touch row 1 of the other."""
    assert library.insert_correction_data("R1", 90, (0, 0)) == 1
    assert library.insert_lcsc_correction_data("C1", 180, (0, 0)) == 1
    rows = {row.kind: row for row in library.read_correction_data().rows}
    assert {kind: row.rowid for kind, row in rows.items()} == {
        "footprint": 1,
        "lcsc": 1,
    }

    library.delete_correction_row(1, expected_record=rows["lcsc"])
    assert lcsc_rows(library) == []
    assert raw_rows(library) == [(1, "R1", 90, 0.0, 0.0)]

    library.delete_correction_row(1, kind=modules.data.KIND_FOOTPRINT)
    assert raw_rows(library) == []


def test_delete_and_repair_reject_a_changed_part_rule(
    modules: SimpleNamespace, library: Any
) -> None:
    """A stale selection cannot act on a row another window has since edited."""
    library.insert_lcsc_correction_data("C1", 90, (0, 0))
    (row,) = library.read_correction_data().rows
    execute(library.correctionsdb_file, "UPDATE lcsc_correction SET rotation=180")
    for operation in (
        lambda: library.delete_correction_row(row.rowid, expected_record=row),
        lambda: library.save_correction_data(
            "C1", 0, (0, 0), rowid=row.rowid, expected_record=row, kind=row.kind
        ),
    ):
        with pytest.raises(modules.data.CorrectionDataError) as error:
            operation()
        assert "stored correction changed" in str(error.value)
    assert lcsc_rows(library) == [(row.rowid, "C1", 180, 0.0, 0.0)]


def test_database_from_an_earlier_release_reads_and_gains_the_table(
    modules: SimpleNamespace, library: Any
) -> None:
    """A corrections database that predates part rules is valid without them."""
    library.insert_correction_data("R1", 90, (0, 0))
    execute(library.correctionsdb_file, "DROP TABLE lcsc_correction")
    assert "lcsc_correction" not in table_names(library.correctionsdb_file)

    snapshot = fresh_library(library).read_correction_data()
    assert snapshot.state is modules.library.CorrectionState.READY
    assert [row.kind for row in snapshot.rows] == ["footprint"]
    assert "lcsc_correction" not in table_names(library.correctionsdb_file)

    fresh_library(library).insert_lcsc_correction_data("C1", 180, (0, 0))
    assert lcsc_rows(library) == [(1, "C1", 180, 0.0, 0.0)]


def test_unsupported_part_table_schema_is_reported_not_repaired(
    modules: SimpleNamespace, library: Any
) -> None:
    """A part table with the wrong columns is an error, like the pattern table."""
    execute(library.correctionsdb_file, "DROP TABLE lcsc_correction")
    execute(library.correctionsdb_file, "CREATE TABLE lcsc_correction (lcsc, angle)")
    snapshot = library.read_correction_data()
    assert snapshot.state is modules.library.CorrectionState.UNAVAILABLE
    assert "lcsc_correction table must contain exactly lcsc" in str(snapshot.issues[0])


def test_get_all_correction_data_lists_both_kinds(
    modules: SimpleNamespace, library: Any
) -> None:
    """One typed set carries pattern rules first and part rules after them."""
    library.insert_lcsc_correction_data("C1", 180, (0, 0))
    library.insert_correction_data("R1", 90, (0, 0))
    assert library.get_all_correction_data() == (
        modules.data.Correction("R1", 90, (0, 0)),
        modules.data.LcscCorrection("C1", 180, (0, 0)),
    )


def test_csv_batches_leave_part_rules_alone(
    modules: SimpleNamespace, library: Any
) -> None:
    """Imports address the pattern table only; part rules are not a CSV concern."""
    library.insert_lcsc_correction_data("C1", 180, (0, 0))
    result = library.apply_corrections([("R1", 90, (0, 0)), ("C1", 45, (1, 1))])
    assert (result.inserted, result.updated) == (2, 0)
    assert lcsc_rows(library) == [(1, "C1", 180, 0.0, 0.0)]
    assert [row[1] for row in raw_rows(library)] == ["R1", "C1"]


def test_switch_to_local_copies_both_kinds_and_keeps_global(
    modules: SimpleNamespace, library: Any
) -> None:
    """Part rules follow the pattern rules into a board-local database."""
    library.insert_correction_data("^SOT-23", 180, (0, 0))
    library.insert_lcsc_correction_data("C12345", 90, (0.5, -0.5))

    library.switch_to_global_correction_database(False)

    assert library.correctionsdb_file == library.localcorrectionsdb_file
    assert part_rules(modules, library) == [("C12345", 90, (0.5, -0.5))]
    assert [row[1] for row in raw_rows(library)] == ["^SOT-23"]
    assert part_rules(modules, library, library.globalcorrectionsdb_file) == [
        ("C12345", 90, (0.5, -0.5))
    ]


def test_switch_to_global_drops_both_local_tables(
    modules: SimpleNamespace, library: Any
) -> None:
    """Returning to global leaves no part rules stranded in the project database."""
    library.insert_correction_data("^SOT-23", 180, (0, 0))
    library.insert_lcsc_correction_data("C12345", 90, (0, 0))
    library.switch_to_global_correction_database(False)
    fresh_library(library).insert_lcsc_correction_data("C99999", 45, (0, 0))

    library.switch_to_global_correction_database(True)

    assert library.correctionsdb_file == library.globalcorrectionsdb_file
    assert not {"correction", "lcsc_correction"} & table_names(
        library.localcorrectionsdb_file
    )
    assert library.uses_global_correction_database()
    assert part_rules(modules, library) == [("C12345", 90, (0.0, 0.0))]


def test_switch_to_a_global_database_predating_the_table(
    modules: SimpleNamespace, library: Any
) -> None:
    """An older global database gains the table on the first write after a switch."""
    library.switch_to_global_correction_database(False)
    execute(library.globalcorrectionsdb_file, "DROP TABLE lcsc_correction")

    library.switch_to_global_correction_database(True)

    assert library.correctionsdb_file == library.globalcorrectionsdb_file
    assert part_rules(modules, library) == []
    library.insert_lcsc_correction_data("C12345", 90, (0, 0))
    assert part_rules(modules, library) == [("C12345", 90, (0.0, 0.0))]


def test_invalid_local_part_rule_does_not_block_return_to_global(
    modules: SimpleNamespace, library: Any
) -> None:
    """Discarding the local database needs a healthy global one, not a healthy local one."""
    library.switch_to_global_correction_database(False)
    execute(
        library.correctionsdb_file,
        "INSERT INTO lcsc_correction VALUES ('C1', '47u', 0, 0)",
    )
    library.switch_to_global_correction_database(True)
    assert library.correctionsdb_file == library.globalcorrectionsdb_file
    assert library.read_correction_data().state is (
        modules.library.CorrectionState.READY
    )


def test_save_refuses_an_expected_record_of_the_other_kind(
    modules: SimpleNamespace, library: Any
) -> None:
    """A part rule cannot be saved over a selected pattern row, nor the reverse."""
    library.insert_correction_data("R1", 90, (0, 0))
    (row,) = library.read_correction_data().rows
    with pytest.raises(modules.data.CorrectionDataError) as error:
        library.save_correction_data(
            "C1",
            90,
            (0, 0),
            rowid=row.rowid,
            expected_record=row,
            kind=modules.data.KIND_LCSC,
        )
    assert "different kind" in str(error.value)
    assert lcsc_rows(library) == []
    assert raw_rows(library) == [(1, "R1", 90, 0.0, 0.0)]
