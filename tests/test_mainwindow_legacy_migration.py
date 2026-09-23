"""Recover historical Default assignments through the real startup controller."""

from collections.abc import Callable
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock
from uuid import NAMESPACE_URL, uuid5

import pytest

from . import part_preferences_test_support as support
from .native_kicad_support import native_bindings

__all__ = ["native_bindings"]

mainwindow = support.mainwindow
make_window = support.make_window


@pytest.fixture
def native_migration_window(
    native_bindings: SimpleNamespace, request: pytest.FixtureRequest
) -> SimpleNamespace:
    """Capture real KiCad bindings before the window harness installs import stubs."""
    # Request these dynamically: declaring them as sibling dependencies would
    # allow the harness to replace sys.modules['pcbnew'] before native loading.
    factory = request.getfixturevalue("make_window")
    return SimpleNamespace(
        pcbnew=native_bindings.pcbnew,
        make_window=factory,
        mainwindow=request.getfixturevalue("mainwindow"),
    )


class LinkedFootprint(support.Footprint):
    """Retain editable native fields and an explicit footprint-to-symbol link."""

    def __init__(self, reference: str = "R1", **kwargs: Any) -> None:
        super().__init__(reference, **kwargs)
        self.m_Uuid = SimpleNamespace(
            AsString=lambda: str(uuid5(NAMESPACE_URL, "migration-test:" + reference))
        )
        self.schematic_path = "/root/symbol-" + reference

    def GetPath(self) -> Any:
        """Return the current native link, including changes during a transaction."""
        return SimpleNamespace(AsString=lambda: self.schematic_path)


def save_fixture_board(board: Any) -> None:
    """Write actual saved metadata; later native mutations cannot alter these bytes."""

    def quote(text: str) -> str:
        return json.dumps(str(text), ensure_ascii=False)

    footprints = []
    for footprint in board.GetFootprints():
        properties = {
            "Reference": footprint.GetReference(),
            "Value": footprint.GetValue(),
        }
        properties.update(
            {name: field.GetText() for name, field in footprint.fields.items()}
        )
        fields = " ".join(
            f"(property {quote(name)} {quote(value)})"
            for name, value in properties.items()
        )
        attributes = []
        if footprint.GetAttributes() & 8:
            attributes.append("exclude_from_bom")
        if footprint.GetAttributes() & 4:
            attributes.append("exclude_from_pos_files")
        footprints.append(
            f"(footprint {quote(footprint.GetFPID().GetLibItemName())} "
            f"(uuid {quote(footprint.m_Uuid.AsString())}) {fields} "
            f"(attr {' '.join(attributes)}))"
        )
    Path(board.GetFileName()).write_text(
        "(kicad_pcb (version 20240108) (generator migration_test) "
        + " ".join(footprints)
        + ")\n",
        encoding="utf-8",
    )


def seed(window: Any, schematic: Optional[str] = None, *, second: bool = False) -> Path:
    """Create complete historical schema and genuinely linked symbols."""
    window.board_name = "board.kicad_pcb"
    window._maybe_show_schematic_storage_notice = MagicMock()
    database = Path(window.store.dbfile)
    database.parent.mkdir(exist_ok=True)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "CREATE TABLE part_info (reference TEXT, value TEXT, footprint TEXT, "
            "lcsc TEXT, stock INTEGER, exclude_from_bom INTEGER, exclude_from_pos INTEGER)"
        )
        for reference in ["R1", *(["R2"] if second else [])]:
            connection.execute(
                "INSERT INTO part_info VALUES (?, '10k', 'R_0603', 'C123', 8, 0, 0)",
                (reference,),
            )
    fields = "" if schematic is None else f'(property "LCSC" "{schematic}")'
    symbols = "\n".join(
        f'(symbol (lib_id "Device:R") (uuid "symbol-{ref}") '
        f'(property "Reference" "{ref}") (in_bom yes) {fields})'
        for ref in ["R1", *(["R2"] if second else [])]
    )
    (Path(window.project_path) / "board.kicad_sch").write_text(
        f'(kicad_sch (uuid "root") {symbols})', encoding="utf-8"
    )
    return database


@pytest.mark.parametrize("schematic", [None, "C123"])
def test_init_store_recovers_legacy_before_preferences_and_initial_rows(
    make_window: Callable[..., Any], schematic: Optional[str]
) -> None:
    """Actual initialization recovers absent native fields without catalog lookup."""
    footprint = LinkedFootprint(fields={})
    window = make_window(
        footprints=[footprint], part_preferences={("R_0603", "10k"): "C999"}
    )
    seed(window, schematic)
    window.library.get_part_details.side_effect = AssertionError("catalog read")
    batches = []

    def transaction(change: Callable[[], None]) -> None:
        batches.append(dict(footprint.fields))
        change()

    window._board_action = transaction
    window.init_store()

    assert "LCSC" in footprint.fields, window.logger.warning.call_args_list
    assert footprint.field.text == "C123"
    assert window.test_rows["R1"]["lcsc"] == "C123"
    assert len(batches) == 1 and batches[0] == {}
    window.library.get_part_preference.assert_not_called()
    window.library.get_part_details.assert_not_called()
    assert not window._project_storage_unavailable


@pytest.mark.parametrize("schematic", ["C456", "", "invalid"])
def test_protected_schematic_disagreement_also_blocks_automatic_preferences(
    make_window: Callable[..., Any], schematic: str
) -> None:
    """A stale recovery candidate cannot be replaced indirectly by opening preferences."""
    footprint = LinkedFootprint(fields={})
    window = make_window(
        footprints=[footprint], part_preferences={("R_0603", "10k"): "C999"}
    )
    seed(window, schematic)

    window.init_store()

    assert footprint.fields == {}
    assert window.test_rows["R1"]["lcsc"] == ""
    assert "R1" in str(window.logger.warning.call_args_list)
    assert not window._project_storage_unavailable


def test_migration_batch_failure_rolls_back_and_allows_retry(
    make_window: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later failed setter restores earlier absent fields before the next startup try."""
    first, second = LinkedFootprint(fields={}), LinkedFootprint("R2", fields={})
    window = make_window(footprints=[first, second])
    seed(window, second=True)
    setter = second.SetField

    def reject(name: str, value: str) -> None:
        setter(name, value)
        raise RuntimeError("later recovery setter failed")

    monkeypatch.setattr(second, "SetField", reject)
    window.init_store()
    assert first.fields == second.fields == {}
    assert window.store is not None and not window._project_storage_unavailable
    monkeypatch.setattr(second, "SetField", setter)
    window.init_store()
    assert first.field.text == second.field.text == "C123"


def test_successful_migration_is_not_reapplied_after_undo_in_same_window(
    make_window: Callable[..., Any],
) -> None:
    """Reinitializing adapters respects a user undo of the completed native transaction."""
    footprint = LinkedFootprint(fields={})
    window = make_window(footprints=[footprint])
    seed(window)
    window.init_store()
    assert "LCSC" in footprint.fields, window.logger.warning.call_args_list
    assert footprint.field.text == "C123"
    footprint.RemoveField("LCSC")

    window.init_store()

    assert footprint.fields == {}
    assert window.test_rows["R1"]["lcsc"] == ""


def test_legacy_read_failure_preserves_rows_and_blocks_preference_fallback(
    make_window: Callable[..., Any],
) -> None:
    """Recovery failure retains native availability while leaving recovery data active."""
    footprint = LinkedFootprint(fields={})
    window = make_window(
        footprints=[footprint], part_preferences={("R_0603", "10k"): "C999"}
    )
    database = seed(window)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("ALTER TABLE part_info RENAME COLUMN value TO old_value")

    window.init_store()

    assert footprint.fields == {}
    assert window.store is not None and "R1" in window.test_rows
    assert not window._project_storage_unavailable
    assert "legacy" in str(window.logger.warning.call_args_list).lower()


@pytest.mark.parametrize("change", ["path", "native", "legacy"])
def test_migration_rechecks_sources_inside_native_transaction(
    make_window: Callable[..., Any], change: str
) -> None:
    """Source changes while entering the host action cannot apply a stale recovery plan."""
    footprint = LinkedFootprint(fields={})
    window = make_window(footprints=[footprint])
    database = seed(window)

    def transaction(apply: Callable[[], None]) -> None:
        if change == "path":
            footprint.schematic_path = "/root/other-symbol"
        elif change == "native":
            footprint.SetField("LCSC", "C456")
        else:
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("UPDATE part_info SET lcsc = 'C456'")
        apply()

    window._board_action = transaction
    window.init_store()

    assert window.store.get_part("R1")["lcsc"] == ("C456" if change == "native" else "")
    assert window._legacy_migration_failed
    assert not window._project_storage_unavailable


@pytest.mark.parametrize("eligible", [False, True])
def test_close_rechecks_current_recovery_rows_without_importing_new_rows(
    make_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
    eligible: bool,
) -> None:
    """Successful safe writes never retire a new row that belongs to another board."""
    from copy import deepcopy

    footprint = LinkedFootprint(fields={})
    window = make_window(footprints=[footprint])
    database = seed(window)
    window.init_store()
    assert footprint.field.text == "C123"
    saved_current = deepcopy(window.pcbnew.GetBoard())
    sibling = support.Board([LinkedFootprint("R_other", fields={})])
    sibling.filename = str(Path(window.project_path) / "sibling.kicad_pcb")
    save_fixture_board(saved_current)
    save_fixture_board(sibling)
    window._backup_schematics_before_first_write = lambda **_kwargs: True
    exporter = MagicMock()

    def save(*_args: Any, **_kwargs: Any) -> Any:
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                "INSERT INTO part_info VALUES ('R_other', '10k', 'R_0603', 'C456', 2, 0, 0)"
            )
        return SimpleNamespace(
            retirement_eligible=eligible,
            diagnostics=(),
            saved=(footprint.m_Uuid.AsString(),),
        )

    exporter.load_schematic.side_effect = save
    monkeypatch.setattr(mainwindow, "SchematicExport", MagicMock(return_value=exporter))
    archive = MagicMock()
    monkeypatch.setattr(mainwindow, "retire_legacy_part_info", archive)
    # The saved current board is enough before the sibling's recovery row is
    # introduced. A later blocked archive must therefore be caused by that row.
    outcome = SimpleNamespace(retirement_eligible=True, diagnostics=(), saved=())
    assert window._finalize_legacy_assignments(outcome, schematic_saved=True) == []
    archive.assert_called_once()
    archive.reset_mock()

    assert window.export_to_schematic(interactive=False) is True

    archive.assert_not_called()
    assert footprint.field.text == "C123"
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM part_info").fetchone() == (2,)


def test_no_active_table_never_calls_unguarded_archive_after_close(
    make_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absence at the coverage check cannot authorize archiving subsequently added rows."""
    footprint = LinkedFootprint(lcsc="C123")
    window = make_window(footprints=[footprint])
    database = seed(window)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("ALTER TABLE part_info RENAME TO part_info_retired")
    window._backup_schematics_before_first_write = lambda **_kwargs: True
    exporter = MagicMock()
    exporter.load_schematic.return_value = SimpleNamespace(
        retirement_eligible=True, diagnostics=()
    )
    monkeypatch.setattr(mainwindow, "SchematicExport", MagicMock(return_value=exporter))
    archive = MagicMock()
    monkeypatch.setattr(mainwindow, "retire_legacy_part_info", archive)

    assert window.export_to_schematic(interactive=False) is True

    archive.assert_not_called()


def test_same_assignment_and_repeated_clear_do_not_enter_native_host_action(
    make_window: Callable[..., Any],
) -> None:
    """No-op Default actions update views without creating host undo or dirty state."""
    footprint = LinkedFootprint(lcsc="C123")
    window = make_window(footprints=[footprint])
    host = MagicMock(side_effect=lambda change: change())
    window._board_action = host

    assert window._apply_lcsc_assignments({"R1": "C123"}) == ["R1"]
    host.assert_not_called()
    window.remove_lcsc_number()
    assert host.call_count == 1 and footprint.field.text == ""
    window.remove_lcsc_number()
    assert host.call_count == 1 and footprint.field.text == ""


def test_migration_rechecks_resolved_file_when_sheet_file_changes_in_transaction(
    make_window: Callable[..., Any],
) -> None:
    """Unchanged native UUID paths cannot hide a changed physical schematic target."""
    footprint = LinkedFootprint(fields={})
    footprint.schematic_path = "/root/sheet/symbol-R1"
    window = make_window(footprints=[footprint])
    seed(window)
    root = Path(window.project_path) / "board.kicad_sch"
    child = '(kicad_sch (uuid "child") (symbol (lib_id "Device:R") (uuid "symbol-R1") (property "Reference" "R1") (in_bom yes)))'
    for name in ("first.kicad_sch", "second.kicad_sch"):
        (root.parent / name).write_text(child, encoding="utf-8")
    root.write_text(
        '(kicad_sch (uuid "root") (sheet (uuid "sheet") (property "Sheetfile" "first.kicad_sch")))',
        encoding="utf-8",
    )

    def transaction(apply: Callable[[], None]) -> None:
        root.write_text(
            root.read_text(encoding="utf-8").replace(
                "first.kicad_sch", "second.kicad_sch"
            ),
            encoding="utf-8",
        )
        apply()

    window._board_action = transaction
    window.init_store()

    assert footprint.fields == {}
    assert window._legacy_migration_failed
    assert not window._project_storage_unavailable


def test_variant_retry_restores_cache_before_retrying_failed_migration(
    make_window: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Storage recovery retries the legacy batch before refreshing an existing matrix."""
    footprint = LinkedFootprint(fields={})
    window = make_window(footprints=[footprint])
    seed(window)
    setter = footprint.SetField
    monkeypatch.setattr(
        footprint, "SetField", MagicMock(side_effect=RuntimeError("retry native write"))
    )
    window.init_store()
    assert window._legacy_migration_failed and footprint.fields == {}
    cache = window.store
    controller = SimpleNamespace(
        cache=cache,
        session=SimpleNamespace(_check_board=lambda: None, reliable=True),
        refresh=MagicMock(side_effect=lambda: footprint.field.text),
        _update_enabled=MagicMock(),
    )
    window._variant_mode = True
    window._variant_controller = controller
    window._set_project_storage_error(OSError("temporary catalog/storage problem"))
    assert window.store is None
    monkeypatch.setattr(footprint, "SetField", setter)

    window.init_store()

    assert footprint.field.text == "C123"
    assert window.store is cache and not window._project_storage_unavailable
    assert window._legacy_migration_completed and not window._legacy_migration_failed
    controller.refresh.assert_called_once()


@pytest.mark.parametrize("change", ["schematic", "saved_pcb", "sibling_added"])
def test_archive_guard_rechecks_sources_inside_database_transaction(
    make_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """A source changed after coverage cannot authorize the actual archive rename."""
    from copy import deepcopy

    window = make_window(footprints=[LinkedFootprint(lcsc="C123")])
    database = seed(window, "C123")
    window.init_store()
    saved = deepcopy(window.pcbnew.GetBoard())
    save_fixture_board(saved)
    retire = mainwindow.retire_legacy_part_info

    def changed_before_rename(*args: Any, **kwargs: Any) -> None:
        if change == "schematic":
            path = Path(window.project_path) / "board.kicad_sch"
            path.write_text(
                path.read_text(encoding="utf-8").replace("C123", "C456"),
                encoding="utf-8",
            )
        elif change == "saved_pcb":
            Path(saved.GetFileName()).write_text("changed PCB", encoding="utf-8")
        else:
            (Path(window.project_path) / "new.kicad_pcb").write_text(
                "new sibling", encoding="utf-8"
            )
        retire(*args, **kwargs)

    monkeypatch.setattr(mainwindow, "retire_legacy_part_info", changed_before_rename)
    outcome = SimpleNamespace(
        retirement_eligible=True,
        diagnostics=(),
        saved=(saved.GetFootprints()[0].m_Uuid.AsString(),),
    )

    with pytest.raises(sqlite3.OperationalError, match="changed"):
        window._finalize_legacy_assignments(outcome, schematic_saved=True)

    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT lcsc FROM part_info").fetchall() == [
            ("C123",)
        ]


def test_failed_migration_recovery_disables_native_edits_and_retains_source(
    make_window: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unrestorable native fields cannot be presented as reliable after startup."""
    footprint = LinkedFootprint(fields={})
    window = make_window(footprints=[footprint])
    database = seed(window)
    setter = footprint.SetField

    def partial_setter(name: str, value: str) -> None:
        setter(name, value)
        raise RuntimeError("native write failed after changing its field")

    monkeypatch.setattr(footprint, "SetField", partial_setter)
    monkeypatch.setattr(
        footprint, "RemoveField", MagicMock(side_effect=RuntimeError("cannot restore"))
    )

    window.init_store()

    assert window._board_unreliable and window._project_storage_unavailable
    assert window.store is None and window.test_rows == {}
    assert window._legacy_migration_failed
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT lcsc FROM part_info").fetchall() == [
            ("C123",)
        ]


@pytest.mark.parametrize("source", ["pcb_only", "pcb_only_root", "no_schematic"])
def test_recover_native_only_legacy_assignments_before_initial_display(
    make_window: Callable[..., Any],
    source: str,
) -> None:
    """A proven absent schematic target provides no conflicting assignment to preserve."""
    footprint = LinkedFootprint(fields={})
    window = make_window(footprints=[footprint])
    seed(window, "C456")
    if source.startswith("pcb_only"):
        footprint.schematic_path = "/" if source == "pcb_only_root" else ""
    else:
        (Path(window.project_path) / "board.kicad_sch").unlink()
    window.library.get_part_details.side_effect = AssertionError("catalog read")

    window.init_store()

    assert window.store.get_part("R1")["lcsc"] == "C123"
    assert window.test_rows["R1"]["lcsc"] == "C123"
    window.library.get_part_details.assert_not_called()


@pytest.mark.parametrize(
    "failure", ["invalid_project", "missing_declared_root", "missing_child"]
)
def test_unreadable_or_incomplete_schematic_context_cannot_be_treated_as_absent(
    make_window: Callable[..., Any],
    failure: str,
) -> None:
    """Only genuine absence permits recovery without a schematic provenance check."""
    footprint = LinkedFootprint(fields={})
    window = make_window(
        footprints=[footprint], part_preferences={("R_0603", "10k"): "C999"}
    )
    seed(window)
    root = Path(window.project_path) / "board.kicad_sch"
    project = root.with_suffix(".kicad_pro")
    if failure == "invalid_project":
        project.write_text("{broken", encoding="utf-8")
    elif failure == "missing_declared_root":
        root.unlink()
        project.write_text(
            '{"schematic":{"top_level_sheets":[{"filename":"missing.kicad_sch"}]}}',
            encoding="utf-8",
        )
    else:
        root.write_text(
            '(kicad_sch (uuid "root") (sheet (uuid "child") (property "Sheetfile" "missing.kicad_sch")))',
            encoding="utf-8",
        )

    window.init_store()

    assert footprint.fields == {}
    assert window.store is not None and not window._project_storage_unavailable
    window.library.get_part_preference.assert_not_called()


def test_archive_durability_reads_saved_bytes_without_cached_or_unsafe_native_loaders(
    make_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Saved proof cannot use a cached board or a C++ reader with untranslated failures."""
    footprint = LinkedFootprint(fields={})
    window = make_window(footprints=[footprint])
    database = seed(window)
    live = window.pcbnew.GetBoard()
    save_fixture_board(live)
    window.init_store()
    assert footprint.field.text == "C123"
    window.pcbnew.LoadBoard = MagicMock(return_value=live)
    unsafe = MagicMock(side_effect=AssertionError("unsafe native reader entered"))
    manager = SimpleNamespace(KICAD_SEXP=17, Load=unsafe)
    monkeypatch.setattr(mainwindow.kicad_pcbnew, "PCB_IO_MGR", manager, raising=False)

    assert window._finalize_legacy_assignments(None, schematic_saved=False) == []

    window.pcbnew.LoadBoard.assert_not_called()
    unsafe.assert_not_called()
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT lcsc FROM part_info").fetchall() == [
            ("C123",)
        ]

    save_fixture_board(live)
    assert window._finalize_legacy_assignments(None, schematic_saved=False) == []
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT lcsc FROM part_info_retired").fetchall() == [
            ("C123",)
        ]


@pytest.mark.native_kicad
def test_native_disk_parser_keeps_unsaved_override_out_of_archive_proof(
    native_migration_window: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production durability boundary rereads saved native bytes, not editor state."""
    pcbnew = native_migration_window.pcbnew
    mainwindow = native_migration_window.mainwindow
    window = native_migration_window.make_window(
        footprints=[LinkedFootprint(lcsc="C123")]
    )
    database = seed(window)
    filename = str(Path(window.project_path) / "board.kicad_pcb")
    board = pcbnew.BOARD()
    board.SetFileName(filename)
    footprint = pcbnew.FOOTPRINT(board)
    footprint.SetReference("R1")
    footprint.SetValue("10k")
    footprint.SetFPID(pcbnew.LIB_ID("R", "R_0603"))
    footprint.SetField("LCSC", "C123")
    board.Add(footprint)
    assert pcbnew.SaveBoard(filename, board)
    disk_before = Path(filename).read_bytes()
    footprint.SetField("LCSC", "C456")
    window.pcbnew.GetBoard = lambda: board
    window._board_identity = mainwindow.board_identity(board)
    cached_loader = MagicMock(return_value=board)
    window.pcbnew.LoadBoard = cached_loader
    monkeypatch.setattr(mainwindow, "kicad_pcbnew", pcbnew)
    monkeypatch.setattr(pcbnew, "LoadBoard", cached_loader)
    save = MagicMock(side_effect=AssertionError("implicit PCB save"))
    monkeypatch.setattr(pcbnew, "SaveBoard", save)

    assert window._finalize_legacy_assignments(None, schematic_saved=False) == []

    cached_loader.assert_not_called()
    save.assert_not_called()
    assert footprint.GetField("LCSC").GetText() == "C456"
    assert Path(filename).read_bytes() == disk_before
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT lcsc FROM part_info").fetchall() == [
            ("C123",)
        ]


def test_schematic_changed_after_export_cannot_prove_import_is_durable(
    make_window: Callable[..., Any], mainwindow: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exporter UUID receipt alone is not proof after another editor replaces its write."""
    from copy import deepcopy

    footprint = LinkedFootprint(fields={})
    window = make_window(footprints=[footprint])
    database = seed(window)
    saved = deepcopy(window.pcbnew.GetBoard())
    window.init_store()
    save_fixture_board(saved)
    window._backup_schematics_before_first_write = lambda **_kwargs: True
    exporter = MagicMock()

    def save_then_external_overwrite(*_args: Any, **_kwargs: Any) -> Any:
        schematic = Path(window.project_path) / "board.kicad_sch"
        original = schematic.read_text(encoding="utf-8")
        schematic.write_text(
            original.replace("(in_bom yes)", '(in_bom yes) (property "LCSC" "C123")'),
            encoding="utf-8",
        )
        schematic.write_text(original, encoding="utf-8")
        return SimpleNamespace(
            retirement_eligible=True,
            diagnostics=(),
            saved=(footprint.m_Uuid.AsString(),),
        )

    exporter.load_schematic.side_effect = save_then_external_overwrite
    monkeypatch.setattr(mainwindow, "SchematicExport", MagicMock(return_value=exporter))

    assert window.export_to_schematic(interactive=False) is True

    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT lcsc FROM part_info").fetchall() == [
            ("C123",)
        ]
    assert footprint.field.text == "C123"


@pytest.mark.parametrize("rows", ["empty", "ignored"])
def test_empty_recovery_finalization_needs_no_unrelated_project_or_saved_board_read(
    make_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
    rows: str,
) -> None:
    """No recoverable values require source proof, while the table digest remains guarded."""
    window = make_window(footprints=[LinkedFootprint(fields={})])
    database = seed(window)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "DELETE FROM part_info"
            if rows == "empty"
            else "UPDATE part_info SET lcsc = 'invalid'"
        )
    (Path(window.project_path) / "board.kicad_pro").write_text(
        "{broken", encoding="utf-8"
    )
    Path(window.pcbnew.GetBoard().GetFileName()).unlink()
    loader = MagicMock(side_effect=AssertionError("irrelevant saved board read"))
    monkeypatch.setattr(mainwindow, "read_saved_pcb_assignments", loader)
    window.init_store()

    assert window._finalize_legacy_assignments(None, schematic_saved=False) == []

    loader.assert_not_called()
    with closing(sqlite3.connect(database)) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='part_info'"
            ).fetchall()
            == []
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM part_info_retired"
        ).fetchone() == ((0,) if rows == "empty" else (1,))


@pytest.mark.parametrize("failure", ["malformed", "newer_version", "duplicate_uuid"])
def test_unsafe_saved_pcb_retains_recovery_after_safe_schematic_save(
    make_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Unreadable or ambiguous saved inventory cannot authorize global archival."""
    from importlib import reload
    import sys

    monkeypatch.setattr(
        mainwindow.kicad_pcbnew, "GetBuildVersion", lambda: "10.0-test", raising=False
    )
    reload(sys.modules[mainwindow.__package__ + ".schematic_safety"])
    exporter = reload(sys.modules[mainwindow.__package__ + ".schematicexport"])
    monkeypatch.setattr(mainwindow, "SchematicExport", exporter.SchematicExport)
    window = make_window(footprints=[LinkedFootprint(lcsc="C123")])
    database = seed(window, "C123")
    board = window.pcbnew.GetBoard()
    save_fixture_board(board)
    saved = Path(board.GetFileName())
    text = saved.read_text(encoding="utf-8")
    if failure == "malformed":
        text = text[:-3]
    elif failure == "newer_version":
        text = text.replace("20240108", "20991231")
    else:
        part = text[text.index("(footprint") : text.rindex(")")]
        text = text[: text.rindex(")")] + part + ")\n"
    saved.write_text(text, encoding="utf-8")
    window._backup_schematics_before_first_write = lambda **_kwargs: True
    window.init_store()

    assert window.export_to_schematic(interactive=False) is True

    report = Path(window.project_path) / "jlcpcb" / "schematic-save-report.txt"
    assert "Cannot read saved PCB" in report.read_text(encoding="utf-8")
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT lcsc FROM part_info").fetchall() == [
            ("C123",)
        ]
