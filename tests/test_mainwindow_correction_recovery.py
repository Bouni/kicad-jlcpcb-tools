"""Exercise correction recovery and generation ordering with real stored data."""

from collections.abc import Iterator
from contextlib import closing
import logging
from pathlib import Path
import sqlite3
from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call

import pytest

from tests.correction_test_support import (
    fresh_library,
    make_library,
    raw_rows,
    seed_raw,
)
from tests.test_corrections_import_export import install_manager_controls
from tests.test_fabrication_correction_recovery import Point, make_fabrication, read_cpl
from tests.test_mainwindow_empty_zone_warning import _make_window
from tests.wx_harness import load_correction_modules, mainwindow_stubs, wx_stubs


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[SimpleNamespace]:
    """Keep real window, placement and persistence siblings in one scoped package."""
    levels = {name: logging.getLogger(name).level for name in ("requests", "urllib3")}
    package = "mainwindow_correction_recovery_tests"
    wx = wx_stubs(
        Dialog=type("Dialog", (), {}),
        NewIdRef=MagicMock(side_effect=object),
        BeginBusyCursor=MagicMock(),
        EndBusyCursor=MagicMock(),
        IsBusy=MagicMock(return_value=True),
        MessageBox=MagicMock(),
        MessageDialog=MagicMock(),
        PostEvent=MagicMock(),
    )
    pcbnew = MagicMock()
    pcbnew.FromMM = lambda value: value
    pcbnew.ToMM = lambda value: value
    pcbnew.wxPoint = Point
    pcbnew.VECTOR2I = Point
    replacements = mainwindow_stubs(package, wx=wx, pcbnew=pcbnew)
    for sibling in ("library", "corrections", "fabrication", "helpers", "events"):
        replacements.pop(f"{package}.{sibling}", None)
    try:
        with load_correction_modules(
            package=package,
            wx=wx,
            pcbnew=pcbnew,
            names=("mainwindow", "fabrication"),
            replacements=replacements,
        ) as modules:
            yield SimpleNamespace(
                modules=modules,
                mainwindow=modules.mainwindow,
                wx=modules.wx,
                library=make_library(modules.library, tmp_path),
            )
    finally:
        for name, level in levels.items():
            logging.getLogger(name).setLevel(level)


class StatusLabel:
    """Retain the visibility and text used by the real readiness handlers."""

    def __init__(self) -> None:
        self.label = ""
        self.tooltip = ""
        self.shown = False

    def SetLabel(self, label: str) -> None:
        """Store the visible text."""
        self.label = label

    def GetLabel(self) -> str:
        """Read the currently visible text."""
        return self.label

    def SetToolTip(self, tooltip: str) -> None:
        """Store the tooltip text."""
        self.tooltip = tooltip

    def Show(self, shown: bool = True) -> None:
        """Update the visibility state."""
        self.shown = shown

    def IsShown(self) -> bool:
        """Read the current visibility state."""
        return self.shown


def _population_window(runtime: SimpleNamespace, library: Any = None) -> Any:
    """Supply controls and board records while preserving real window methods."""
    window = object.__new__(runtime.mainwindow.JLCPCBTools)
    window._project_storage_unavailable = False
    window._part_preferences_applied_on_open = False
    window.project_storage_status = MagicMock()
    window.footprint_list = MagicMock()
    window.right_toolbar = MagicMock()
    window.upper_toolbar = MagicMock()
    window.library = library or fresh_library(runtime.library)
    window.scale_factor = 1
    window.window = object()
    window.correction_status = StatusLabel()
    window.Layout = MagicMock()
    window.partlist_data_model = MagicMock()
    window.store = MagicMock()
    window.store.read_all.return_value = [
        {
            "reference": reference,
            "value": "47u",
            "footprint": "Capacitor_SMD:C_0603",
            "lcsc": "",
            "exclude_from_bom": 0,
            "exclude_from_pos": 0,
        }
        for reference in ("C1", "C2")
    ]
    footprint = SimpleNamespace(GetLayer=lambda: 0)
    board = SimpleNamespace(FindFootprintByReference=lambda _reference: footprint)
    window.pcbnew = SimpleNamespace(GetBoard=lambda: board)
    window.hide_bom_parts = False
    window.hide_pos_parts = False
    window._get_enrichment_status_label = lambda _part: ""
    return window


@pytest.mark.parametrize("entrypoint", ["toolbar", "reference", "package", "name"])
def test_manager_close_refreshes_recovered_corrections_through_real_constructor(
    runtime: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    """Each manager entry point replaces unresolved cells after retrying a transfer."""
    library = runtime.library
    _write_sql(
        library.rotationsdb_file,
        "CREATE TABLE rotation (regex, rotation); "
        "INSERT INTO rotation VALUES ('C1', 90)",
    )
    _write_sql(
        library.correctionsdb_file,
        "CREATE TRIGGER reject_insert BEFORE INSERT ON correction "
        "BEGIN SELECT RAISE(ABORT, 'failed copy'); END",
    )
    assert library.migrate_corrections()
    window = _population_window(runtime)
    window.populate_footprint_list()
    assert _displayed_corrections(window) == ["Unresolved", "Unresolved"]
    _write_sql(library.correctionsdb_file, "DROP TRIGGER reject_insert")
    install_manager_controls(runtime.modules, library, monkeypatch)
    monkeypatch.setattr(
        runtime.wx.Dialog, "ShowModal", lambda _dialog: runtime.wx.ID_OK, raising=False
    )
    window.partlist_data_model.AddEntry.reset_mock()

    if entrypoint == "toolbar":
        window.manage_corrections()
    else:
        window.footprint_list = SimpleNamespace(GetSelections=lambda: ["selected"])
        window.partlist_data_model.get_reference.return_value = "C1"
        window.partlist_data_model.get_footprint.return_value = "Capacitor_SMD:C_0603"
        window.partlist_data_model.get_value.return_value = "47u"
        constant = {
            "reference": "ID_CONTEXT_MENU_ADD_ROT_BY_REFERENCE",
            "package": "ID_CONTEXT_MENU_ADD_ROT_BY_PACKAGE",
            "name": "ID_CONTEXT_MENU_ADD_ROT_BY_NAME",
        }[entrypoint]
        event_id = getattr(runtime.mainwindow, constant)
        window.add_correction(SimpleNamespace(GetId=lambda: event_id))

    assert _displayed_corrections(window) == ["90°, 0.0/0.0 (ref)", "0°, 0.0/0.0"]
    assert not window.correction_status.IsShown()
    runtime.wx.MessageBox.assert_not_called()


def _generation_window(runtime: SimpleNamespace) -> tuple[SimpleNamespace, list[str]]:
    """Bind real preflight methods to the established generation test harness."""
    window, steps = _make_window([])
    window.library = fresh_library(runtime.library)
    window.correction_status = StatusLabel()
    window.Layout = MagicMock()
    for name in (
        "read_valid_corrections_for_generation",
        "update_correction_status",
    ):
        setattr(
            window,
            name,
            MethodType(getattr(runtime.mainwindow.JLCPCBTools, name), window),
        )
    return window, steps


def _displayed_corrections(window: Any) -> list[str]:
    """Return the correction cells most recently added to the model."""
    return [
        invocation.args[0][9]
        for invocation in window.partlist_data_model.AddEntry.call_args_list
    ]


def _write_sql(path: str, sql: str) -> None:
    """Commit a historical database state through an independent connection."""
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(sql)


def _record_failed_transfer(library: Any) -> None:
    """Establish known incomplete migration before source-free consumer reads."""
    _write_sql(
        library.correctionsdb_file,
        "CREATE TRIGGER reject_insert BEFORE INSERT ON correction "
        "BEGIN SELECT RAISE(ABORT, 'failed copy'); END",
    )
    assert library.migrate_corrections()


@pytest.mark.parametrize(
    "rows",
    [
        [("C1", "47u", 0, 0), ("C2", 90, 0, 0)],
        [("C1", "47u", 0, 0), ("C2", "bad", 0, 0)],
        [(None, 90, 0, 0)],
        [("C1", None, 0, 0)],
        [("C1", 90, None, 0)],
        [("C1", 90, 0, "inf")],
        [("[", 90, 0, 0)],
        [("C1", 90, 0, 0), ("C1", 180, 0, 0)],
    ],
)
def test_invalid_saved_data_keeps_population_accessible_and_unresolved(
    runtime: SimpleNamespace, rows: list[tuple[object, ...]]
) -> None:
    """Bad rows never crash matching or appear as a valid partial correction set."""
    seed_raw(runtime.library, rows)
    before = raw_rows(runtime.library)
    window = _population_window(runtime)

    window.populate_footprint_list()

    assert _displayed_corrections(window) == ["Unresolved", "Unresolved"]
    assert "active global database" in window.correction_status.GetLabel()
    assert "Open Corrections Manager" in window.correction_status.GetLabel()
    assert window.correction_status.IsShown()
    runtime.wx.MessageBox.assert_not_called()
    assert raw_rows(runtime.library) == before
    runtime.wx.PostEvent.assert_called_once()


@pytest.mark.parametrize(
    "damage", ["unreadable", "missing_schema", "pending_migration"]
)
def test_storage_and_pending_migration_failures_remain_visible(
    runtime: SimpleNamespace, damage: str
) -> None:
    """Unavailable or incomplete storage is unresolved even without invalid rows."""
    if damage == "unreadable":
        Path(runtime.library.correctionsdb_file).write_bytes(b"not a SQLite database")
    elif damage == "missing_schema":
        _write_sql(runtime.library.correctionsdb_file, "DROP TABLE correction")
    else:
        _write_sql(
            runtime.library.rotationsdb_file,
            "CREATE TABLE rotation (regex, rotation); INSERT INTO rotation VALUES ('C1', '47u')",
        )
        _record_failed_transfer(runtime.library)
    window = _population_window(runtime)

    window.populate_footprint_list()

    assert _displayed_corrections(window) == ["Unresolved", "Unresolved"]
    assert window.correction_status.IsShown()
    details = window.correction_status.tooltip
    assert str(runtime.library.correctionsdb_file) in details
    runtime.wx.MessageBox.assert_not_called()


def test_refresh_remains_nonmodal_and_only_complete_repair_clears_status(
    runtime: SimpleNamespace,
) -> None:
    """Independent rereads retain warnings until every original bad row is repaired."""
    seed_raw(runtime.library, [("C1", "47u", 0, 0), ("C2", 90, "bad", 0)])
    window = _population_window(runtime)
    for _ in range(2):
        window.populate_footprint_list()
        assert window.correction_status.IsShown()
    assert "47u" not in window.correction_status.tooltip
    rows = runtime.library.read_correction_data().rows
    runtime.library.save_correction_data("C1", 180, (0.5, -0.25), rowid=rows[0].rowid)
    window.library = fresh_library(runtime.library)
    window.partlist_data_model.AddEntry.reset_mock()

    window.populate_footprint_list()

    assert window.correction_status.IsShown()
    assert _displayed_corrections(window) == ["Unresolved", "Unresolved"]
    runtime.library.save_correction_data("C2", -90, (1.25, 2.5), rowid=rows[1].rowid)
    window.library = fresh_library(runtime.library)
    window.partlist_data_model.AddEntry.reset_mock()

    window.populate_footprint_list()

    assert _displayed_corrections(window) == [
        "180°, 0.5/-0.25 (ref)",
        "-90°, 1.25/2.5 (ref)",
    ]
    assert window.correction_status.GetLabel() == ""
    assert window.correction_status.tooltip == ""
    assert not window.correction_status.IsShown()
    runtime.wx.MessageBox.assert_not_called()


def test_valid_empty_database_displays_zero_and_clears_prior_issue(
    runtime: SimpleNamespace,
) -> None:
    """An actually empty valid set retains the established zero display."""
    window = _population_window(runtime)
    window.correction_status.Show(True)

    window.populate_footprint_list()

    assert _displayed_corrections(window) == ["0°, 0.0/0.0", "0°, 0.0/0.0"]
    assert not window.correction_status.IsShown()


def test_startup_store_population_recovers_invalid_saved_corrections(
    runtime: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The startup init_store path reaches recovery without losing the part list."""
    seed_raw(runtime.library, [("C1", "47u", 0, 0)])
    window = _population_window(runtime)
    store = window.store
    monkeypatch.setattr(runtime.mainwindow, "Store", lambda *_args: store)
    window.project_path = runtime.library.parent.project_path
    window.settings = {
        "part_preferences": {"fill_empty_lcsc_assignments_on_open": False}
    }
    window.start_assembly_enrichment = MagicMock()
    window.recompute_bom_estimate = MagicMock()
    window.store = None

    window.init_store()

    assert window.store is store
    assert _displayed_corrections(window) == ["Unresolved", "Unresolved"]
    window.start_assembly_enrichment.assert_called_once_with()
    window.recompute_bom_estimate.assert_called_once_with()


def test_inactive_bad_global_database_does_not_affect_active_local(
    runtime: SimpleNamespace, tmp_path: Path
) -> None:
    """Recovery status and generation preflight inspect the selected scope only."""
    local = make_library(runtime.modules.library, tmp_path / "other", local=True)
    local.create_correction_table(local.globalcorrectionsdb_file)
    _write_sql(
        local.globalcorrectionsdb_file,
        "INSERT INTO correction VALUES ('C1', '47u', 0, 0)",
    )
    local.insert_correction_data("C1", 90, (0.25, -0.5))
    window = _population_window(runtime, library=local)

    window.populate_footprint_list()
    corrections = window.read_valid_corrections_for_generation()

    assert _displayed_corrections(window) == ["90°, 0.25/-0.5 (ref)", "0°, 0.0/0.0"]
    assert [(item.pattern, item.rotation) for item in corrections] == [("C1", 90)]
    assert local.read_correction_data(local.globalcorrectionsdb_file).issues


@pytest.mark.parametrize(
    "rows",
    [
        [("C1", "47u", 0, 0)],
        [("C1", "47u", 0, 0), ("C2", 90, 0, 0)],
        [("[", 90, 0, 0)],
        [("C1", 90, 0, None)],
    ],
)
def test_generation_rejects_bad_data_before_all_side_effects(
    runtime: SimpleNamespace, tmp_path: Path, rows: list[tuple[object, ...]]
) -> None:
    """No prompts, fills, checks, hooks, output writes, or counters precede validation."""
    seed_raw(runtime.library, rows)
    window, steps = _generation_window(runtime)
    window.settings["general"]["order_number"] = True
    artifacts = []
    for name in (
        "generate_geber",
        "generate_excellon",
        "zip_gerber_excellon",
        "write_cpl",
        "generate_bom",
    ):
        path = tmp_path / f"{name}.existing"
        path.write_bytes(b"previous valid output\x00\xff")
        artifacts.append(path)
        getattr(window.fabrication, name).side_effect = (
            lambda *_args, path=path: path.write_bytes(b"overwritten")
        )

    runtime.mainwindow.JLCPCBTools.generate_fabrication_data(window)

    assert steps == ["Validating corrections"]
    for method in vars(window.fabrication).values():
        method.assert_not_called()
    window.run_drc_before_gerber_export.assert_not_called()
    window.count_order_number_placeholders.assert_not_called()
    window.build_generate_hook_env.assert_not_called()
    window.run_generate_hook.assert_not_called()
    window.store.get_generation_count.assert_not_called()
    window.store.increment_generation_count.assert_not_called()
    runtime.wx.MessageDialog.assert_not_called()
    runtime.wx.MessageBox.assert_called_once()
    assert "Validating corrections" in runtime.wx.MessageBox.call_args.args[0]
    assert all(
        path.read_bytes() == b"previous valid output\x00\xff" for path in artifacts
    )
    assert window.generate_button.Enable.call_args_list == [call(False), call(True)]
    runtime.wx.EndBusyCursor.assert_called_once_with()
    assert window._current_generation_step == "initialization"
    assert window.correction_status.IsShown()


@pytest.mark.parametrize(
    "damage", ["unreadable", "missing_schema", "pending_migration"]
)
def test_generation_blocks_unavailable_or_unmigrated_storage(
    runtime: SimpleNamespace, damage: str
) -> None:
    """An empty-looking destination cannot permit incomplete fabrication output."""
    if damage == "unreadable":
        Path(runtime.library.correctionsdb_file).write_bytes(b"invalid SQLite")
    elif damage == "missing_schema":
        _write_sql(runtime.library.correctionsdb_file, "DROP TABLE correction")
    else:
        _write_sql(
            runtime.library.rotationsdb_file,
            "CREATE TABLE rotation (regex, rotation); INSERT INTO rotation VALUES ('C1', 90)",
        )
        _record_failed_transfer(runtime.library)
    window, steps = _generation_window(runtime)

    runtime.mainwindow.JLCPCBTools.generate_fabrication_data(window)

    assert steps == ["Validating corrections"]
    window.fabrication.write_cpl.assert_not_called()
    window.fabrication.fill_zones.assert_not_called()
    window.store.increment_generation_count.assert_not_called()
    runtime.wx.EndBusyCursor.assert_called_once_with()
    window.generate_button.Enable.assert_called_with(True)


def test_unknown_archive_warnings_keep_healthy_display_and_generation_ready(
    runtime: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordinary consumers use the ready snapshot without inspecting unknown archives."""
    runtime.library.insert_correction_data("C1", 90, (0.25, -0.5))
    Path(runtime.library.rotationsdb_file).write_bytes(b"unreadable archive")
    assert runtime.library.migrate_corrections() == ()
    snapshot = fresh_library(runtime.library).read_correction_data()
    assert snapshot.corrections is not None
    assert snapshot.warnings
    inspect_archive = MagicMock(side_effect=AssertionError("unexpected archive read"))
    monkeypatch.setattr(
        runtime.modules.library.Library, "_legacy_rotation_rows", inspect_archive
    )
    window = _population_window(runtime)

    window.populate_footprint_list()

    assert _displayed_corrections(window) == ["90°, 0.25/-0.5 (ref)", "0°, 0.0/0.0"]
    assert not window.correction_status.IsShown()
    generation, steps = _generation_window(runtime)
    runtime.mainwindow.JLCPCBTools.generate_fabrication_data(generation)
    assert steps[0] == "Validating corrections"
    generation.fabrication.prepare_cpl.assert_called_once_with(snapshot.corrections)
    generation.fabrication.write_cpl.assert_called_once_with(
        generation.fabrication.prepare_cpl.return_value
    )
    generation.store.increment_generation_count.assert_called_once()
    inspect_archive.assert_not_called()
    runtime.wx.MessageBox.assert_not_called()


def test_repair_allows_subsequent_generation_and_clears_warning(
    runtime: SimpleNamespace,
) -> None:
    """A failed run leaves controls usable and a repaired reread can generate."""
    seed_raw(runtime.library, [("C1", "47u", 0, 0)])
    window, _steps = _generation_window(runtime)
    runtime.mainwindow.JLCPCBTools.generate_fabrication_data(window)
    assert window.correction_status.IsShown()
    row = runtime.library.read_correction_data().rows[0]
    runtime.library.save_correction_data("C1", -90, (0.5, -0.25), rowid=row.rowid)
    window.library = fresh_library(runtime.library)

    runtime.mainwindow.JLCPCBTools.generate_fabrication_data(window)

    assert not window.correction_status.IsShown()
    window.fabrication.write_cpl.assert_called_once()
    window.store.increment_generation_count.assert_called_once()
    assert runtime.wx.EndBusyCursor.call_count == 2


@pytest.mark.parametrize("state_name", ["READY", "NEEDS_REPAIR", "UNAVAILABLE"])
def test_ordinary_consumers_need_only_aggregate_snapshot_state(
    runtime: SimpleNamespace, state_name: str
) -> None:
    """Display and generation do not require row-level repair diagnostics."""
    state = getattr(runtime.modules.library.CorrectionState, state_name)
    ready = state_name == "READY"
    snapshot = SimpleNamespace(
        state=state,
        corrections=() if ready else None,
        scope="global",
        db_path=runtime.library.correctionsdb_file,
    )
    window = _population_window(runtime)
    window.library = SimpleNamespace(read_correction_data=lambda: snapshot)

    window.populate_footprint_list()

    assert window.correction_status.IsShown() is not ready
    if ready:
        assert window.read_valid_corrections_for_generation() == ()
        assert _displayed_corrections(window) == ["0°, 0.0/0.0"] * 2
    else:
        assert _displayed_corrections(window) == ["Unresolved"] * 2
        assert ("unavailable" if state_name == "UNAVAILABLE" else "unresolved") in (
            window.correction_status.GetLabel()
        )
        with pytest.raises(ValueError, match="Open Corrections Manager"):
            window.read_valid_corrections_for_generation()


def test_generation_event_keeps_snapshot_for_real_cpl_then_blocks_and_recovers(
    runtime: SimpleNamespace, tmp_path: Path
) -> None:
    """The real generation handler and CPL writer share one snapshot across a run."""
    runtime.library.save_correction_data("Device", 90, (1, 2))
    window, _steps = _generation_window(runtime)
    fabrication = make_fabrication(runtime.modules, window.library, tmp_path)
    window.fabrication = fabrication
    matcher = MagicMock(wraps=fabrication._correction_for_footprint)
    fabrication._correction_for_footprint = matcher
    for method in (
        "fill_zones",
        "generate_geber",
        "generate_excellon",
        "zip_gerber_excellon",
        "generate_bom",
    ):
        setattr(fabrication, method, MagicMock(return_value=[]))

    def damage_storage_after_preflight() -> str:
        """Simulate an external legacy write after the complete snapshot was read."""
        _write_sql(
            runtime.library.correctionsdb_file,
            "UPDATE correction SET rotation='47u'",
        )
        for footprint in fabrication.board.Footprints():
            footprint.GetPosition = lambda: Point(100, 200)
        return ""

    fabrication.get_part_consistency_warnings = MagicMock(
        side_effect=damage_storage_after_preflight
    )

    runtime.mainwindow.JLCPCBTools.generate_fabrication_data(window)

    assert [
        tuple(float(row[field]) for field in ("Mid X", "Mid Y", "Rotation"))
        for row in read_cpl(fabrication)
    ] == [(10, -20, 90), (27, -37, 180)]
    assert matcher.call_count == 2
    destination = Path(fabrication.get_cpl_csv_path())
    prior_output = destination.read_bytes()
    window.store.increment_generation_count.assert_called_once_with()
    runtime.wx.MessageBox.assert_not_called()

    runtime.mainwindow.JLCPCBTools.generate_fabrication_data(window)

    assert destination.read_bytes() == prior_output
    window.store.increment_generation_count.assert_called_once_with()
    runtime.wx.MessageBox.assert_called_once()
    assert window.correction_status.IsShown()

    row = runtime.library.read_correction_data().rows[0]
    runtime.library.save_correction_data("Device", -90, (0, 0), rowid=row.rowid)
    fabrication.get_part_consistency_warnings.side_effect = None
    fabrication.get_part_consistency_warnings.return_value = ""

    runtime.mainwindow.JLCPCBTools.generate_fabrication_data(window)

    assert [float(row["Rotation"]) for row in read_cpl(fabrication)] == [270, 0]
    assert window.store.increment_generation_count.call_count == 2
    assert not window.correction_status.IsShown()
    assert runtime.wx.MessageBox.call_count == 1


@pytest.mark.parametrize("offset", [1e308, 2147483647.0])
def test_generation_prepares_placements_before_any_output_or_board_changes(
    runtime: SimpleNamespace, tmp_path: Path, offset: float
) -> None:
    """Unrepresentable correction transforms leave every previous artifact intact."""
    runtime.library.save_correction_data("Device", 0, (offset, 0))
    window, steps = _generation_window(runtime)
    fabrication = make_fabrication(runtime.modules, window.library, tmp_path)
    window.fabrication = fabrication
    destination = Path(fabrication.get_cpl_csv_path())
    destination.write_bytes(b"previous complete CPL\x00\xff")
    later_steps = (
        "fill_zones",
        "generate_geber",
        "generate_excellon",
        "zip_gerber_excellon",
        "generate_bom",
        "get_part_consistency_warnings",
    )
    for method in later_steps:
        setattr(fabrication, method, MagicMock(return_value=[]))

    runtime.mainwindow.JLCPCBTools.generate_fabrication_data(window)

    assert steps == ["Validating corrections", "Preparing placement data"]
    for method in later_steps:
        getattr(fabrication, method).assert_not_called()
    window.run_drc_before_gerber_export.assert_not_called()
    window.run_generate_hook.assert_not_called()
    window.store.increment_generation_count.assert_not_called()
    assert destination.read_bytes() == b"previous complete CPL\x00\xff"
    assert "placement" in runtime.wx.MessageBox.call_args.args[0].lower()
    assert window.generate_button.Enable.call_args_list == [call(False), call(True)]
