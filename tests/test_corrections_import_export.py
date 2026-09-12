"""Regression tests for correction CSV import, persistence, and reopening."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.correction_test_support import (
    fresh_library,
    make_library,
    raw_rows,
    seed_raw,
)
from tests.wx_harness import load_correction_modules


@pytest.fixture
def modules() -> Iterator[SimpleNamespace]:
    """Keep production siblings under one package for the full test lifecycle."""
    with load_correction_modules() as loaded:
        yield loaded


def reopened_rows(library: Any) -> list[tuple[str, int, tuple[float, float]]]:
    """Read the complete reopened set and expose its values for legacy fixtures."""
    corrections = fresh_library(library).get_all_correction_data()
    assert corrections is not None
    return [(item.pattern, item.rotation, item.offset) for item in corrections]


@pytest.mark.parametrize("overwrite", [False, True], ids=["insert", "overwrite"])
def test_issue531_rejected_import_preserves_database(
    modules: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overwrite: bool,
) -> None:
    """The reported 47u value cannot persist or replace an existing rotation."""
    existing = [("R1", 90, 0.0, 0.0)] if overwrite else []
    library = make_library(modules.library, tmp_path, existing)
    original = raw_rows(library)
    path = tmp_path / "issue531.csv"
    path.write_text("Pattern,Rotation,Offset X,Offset Y\nR1,47u,0,0\n")

    dialog = manager(modules, library, monkeypatch)
    dialog.populate_corrections_list = MagicMock(
        side_effect=library.get_all_correction_data
    )

    assert dialog._import_corrections(str(path)) is False
    assert raw_rows(library) == original
    assert reopened_rows(library) == ([("R1", 90, (0.0, 0.0))] if overwrite else [])
    dialog.populate_corrections_list.assert_not_called()
    modules.wx.PostEvent.assert_not_called()
    message = str(modules.wx.MessageBox.call_args)
    assert "47u" in message
    assert "rotation" in message.lower()
    assert "2" in message


class TextControl:
    """Capture original text and button/label state without native wx."""

    def __init__(self, value: Any = "") -> None:
        self.value = value
        self.enabled = True
        self.tooltip = ""
        self.bindings = {}

    def Bind(self, event: object, callback: Any) -> None:
        """Retain bound handlers so tests can exercise the constructor's wiring."""
        self.bindings[event] = callback

    def SetBitmap(self, _bitmap: object) -> None:
        """Accept a button bitmap without simulating rendering."""

    def SetBitmapMargins(self, _margins: tuple[int, int]) -> None:
        """Accept bitmap spacing without simulating rendering."""

    def GetValue(self) -> Any:
        """Return the exact control value."""
        return self.value

    def SetValue(self, value: Any) -> None:
        """Retain the exact supplied control value."""
        self.value = value

    def SetLabel(self, value: Any) -> None:
        """Capture a visible diagnostic."""
        self.value = value

    def SetToolTip(self, value: Any) -> None:
        """Capture the full diagnostic details."""
        self.tooltip = value

    def Wrap(self, _width: int) -> None:
        """Accept the native label wrapping request."""

    def Enable(self, enabled: bool) -> None:
        """Capture toolbar availability."""
        self.enabled = enabled


class CorrectionList:
    """Exercise list rebuilding and selection using wx-compatible row methods."""

    def __init__(self) -> None:
        self.rows = []
        self.selected = -1
        self.on_change = None
        self.columns = []

    def Bind(self, _event: object, callback: Any) -> None:
        """Exercise the selection handler registered by the real constructor."""
        self.on_change = lambda: callback(
            SimpleNamespace(GetItem=lambda: self.selected)
        )

    def AppendTextColumn(self, label: str, **_kwargs: object) -> None:
        """Keep the visible column order for constructor assertions."""
        self.columns.append(label)

    def SetMinSize(self, _size: tuple[int, int]) -> None:
        """Accept sizing without claiming native layout verification."""

    def DeleteAllItems(self) -> None:
        """Clear rows and optionally simulate synchronous wx selection events."""
        self.rows = []
        self.selected = -1
        if self.on_change:
            self.on_change()

    def AppendItem(self, values: list[str]) -> None:
        """Capture a displayed original correction row."""
        self.rows.append(values)

    def SelectRow(self, index: int) -> None:
        """Select a row, optionally dispatching synchronous wx events."""
        if self.selected != index:
            self.selected = index
            if self.on_change:
                self.on_change()

    def GetSelectedRow(self) -> int:
        """Return the selected row index or wx.NOT_FOUND."""
        return self.selected


def install_manager_controls(
    modules: SimpleNamespace, library: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provide stateful controls without constructing or replacing the real dialog."""
    wx = modules.wx
    for name in ("Bind", "SetAcceleratorTable", "SetSizer", "Layout", "Centre"):
        monkeypatch.setattr(wx.Dialog, name, MagicMock(), raising=False)

    def init_dialog(_self: Any, *_args: object, **_kwargs: object) -> None:
        """Allow the real subclass constructor to initialize its own state."""

    def control(*args: object, **kwargs: Any) -> TextControl:
        """Retain the value supplied to native text, button, and checkbox constructors."""
        return TextControl(args[2] if len(args) > 2 else kwargs.get("label", ""))

    monkeypatch.setattr(wx.Dialog, "__init__", init_dialog)
    for name in ("DefaultPosition", "DefaultSize"):
        monkeypatch.setattr(wx, name, (-1, -1), raising=False)
    for name in ("StaticText", "TextCtrl", "Button", "CheckBox"):
        monkeypatch.setattr(wx, name, control, raising=False)
    for name in (
        "NewId",
        "AcceleratorEntry",
        "AcceleratorTable",
        "BoxSizer",
        "StaticBoxSizer",
    ):
        monkeypatch.setattr(wx, name, MagicMock(), raising=False)
    monkeypatch.setattr(wx, "ToolTip", lambda text: text, raising=False)
    monkeypatch.setattr(wx, "Size", lambda *args: args, raising=False)
    monkeypatch.setattr(
        wx.dataview,
        "DataViewListCtrl",
        MagicMock(side_effect=lambda *_args, **_kwargs: CorrectionList()),
        raising=False,
    )
    monkeypatch.setattr(
        modules.corrections, "HighResWxSize", lambda _window, size: size
    )
    monkeypatch.setattr(modules.corrections, "loadBitmapScaled", MagicMock())
    # Prevent automatic CSV detection from reading any real user's plugin data.
    monkeypatch.setattr(modules.corrections, "PLUGIN_PATH", library.datadir)


def manager(
    modules: SimpleNamespace, library: Any, monkeypatch: pytest.MonkeyPatch
) -> Any:
    """Run the real constructor with stateful controls and real event bindings."""
    install_manager_controls(modules, library, monkeypatch)
    parent = SimpleNamespace(library=library, scale_factor=1, window=object())
    return modules.corrections.CorrectionManagerDialog(parent, "")


@pytest.fixture
def setup_manager(
    modules: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[SimpleNamespace, Any, Any]:
    """Provide isolated real production imports, SQLite, and captured controls."""
    library = make_library(modules.library, tmp_path)
    return modules, library, manager(modules, library, monkeypatch)


def set_inputs(
    dialog: Any,
    pattern: str = "R1",
    rotation: str = "90",
    offset_x: str = "0",
    offset_y: str = "0",
) -> None:
    """Fill fields while preserving the exact caller-provided strings."""
    for control, value in zip(
        (dialog.regex, dialog.rotation, dialog.offset_x, dialog.offset_y),
        (pattern, rotation, offset_x, offset_y),
    ):
        control.SetValue(value)


def select(dialog: Any, index: int) -> None:
    """Select through the production handler rather than fabricating identity."""
    dialog.corrections_list.SelectRow(index)


def field_values(dialog: Any) -> tuple[str, str, str, str]:
    """Read exact editable values for preservation assertions."""
    return tuple(
        control.GetValue()
        for control in (dialog.regex, dialog.rotation, dialog.offset_x, dialog.offset_y)
    )


@pytest.mark.parametrize("position", [0, 1, 2])
def test_invalid_batch_never_applies_inserts_or_replacements(
    setup_manager: tuple[SimpleNamespace, Any, Any], tmp_path: Path, position: int
) -> None:
    """Every invalid-row position leaves the complete database and UI intact."""
    modules, library, dialog = setup_manager
    library.insert_correction_data("R1", 90, (0.25, -0.5))
    dialog.populate_corrections_list()
    select(dialog, 0)
    set_inputs(dialog, rotation="unsaved input")
    original = raw_rows(library)
    selection = (dialog.selection_db_path, dialog.selected_record.rowid)
    contents = ["R1,180,1,2", "R2,-90,3,4"]
    contents.insert(position, "C1,47u,0,0")
    path = tmp_path / "mixed.csv"
    path.write_text("Pattern,Rotation,Offset X,Offset Y\n" + "\n".join(contents))
    assert dialog._import_corrections(path) is False
    assert raw_rows(library) == original
    assert (dialog.selection_db_path, dialog.selected_record.rowid) == selection
    assert field_values(dialog) == ("R1", "unsaved input", "0", "0")
    assert reopened_rows(library) == [("R1", 90, (0.25, -0.5))]
    modules.wx.PostEvent.assert_not_called()


@pytest.mark.parametrize(
    "contents, expected",
    [
        (
            "Pattern,Rotation,Offset X,Offset Y\nR1,-90,0.125,-0.75\n",
            [("R1", -90, (0.125, -0.75))],
        ),
        ("Footprint pattern,Correction\nR1,90.0\n", [("R1", 90, (0.0, 0.0))]),
        (
            '"Footprint pattern", "Correction", "Offset X", "Offset Y"\nR1,90\nR2,180,1.5\n',
            [("R1", 90, (0.0, 0.0)), ("R2", 180, (1.5, 0.0))],
        ),
        (
            "Offset Y,Rotation,Pattern,Offset X\n-0.4,90,R1,0.12345678901234568\n",
            [("R1", 90, (0.12345678901234568, -0.4))],
        ),
        ('\ufeffPattern,Rotation\r\n"Ω,\\d+",90\r\n', [("Ω,\\d+", 90, (0.0, 0.0))]),
        ("Pattern,Rotation\nR1,90\nR1,180\n", [("R1", 180, (0.0, 0.0))]),
    ],
)
def test_valid_formats_commit_and_reopen(
    setup_manager: tuple[SimpleNamespace, Any, Any],
    tmp_path: Path,
    contents: str,
    expected: list[tuple[str, int, tuple[float, float]]],
) -> None:
    """Compatible formats and duplicate policy survive the real import boundary."""
    modules, library, dialog = setup_manager
    path = tmp_path / "valid.csv"
    path.write_text(contents, encoding="utf-8")
    assert dialog._import_corrections(path) is True
    assert reopened_rows(library) == expected
    assert len(dialog.corrections_list.rows) == len(expected)
    modules.wx.PostEvent.assert_called_once()
    modules.wx.MessageBox.assert_not_called()


@pytest.mark.parametrize(
    "contents",
    [
        b"",
        b"Pattern,Rotation\nR1\n",
        b"Pattern,Rotation\nR1,47u\nR1,90\n",
        b"Pattern,Rotation\nR1,90,0\n",
        b"Value,Rotation\nR1,90\n",
        b"Pattern,Rotation,Correction\nR1,90,90\n",
        b"Pattern,Rotation,Offset X,Offset Y\nR1,90,,0\n",
        b"Pattern,Rotation,Offset X,Offset Y\nR1,90,0,NaN\n",
        b'Pattern,Rotation\n"unterminated,90\n',
        b"Pattern,Rotation\n\xff,90\n",
    ],
)
def test_malformed_import_is_handled_without_side_effects(
    setup_manager: tuple[SimpleNamespace, Any, Any], tmp_path: Path, contents: bytes
) -> None:
    """Formatting, decoding, and field failures preserve storage and UI."""
    modules, library, dialog = setup_manager
    path = tmp_path / "invalid.csv"
    path.write_bytes(contents)
    assert dialog._import_corrections(path) is False
    assert raw_rows(library) == []
    modules.wx.PostEvent.assert_not_called()
    assert str(path) in str(modules.wx.MessageBox.call_args)


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_import_read_errors_are_handled(
    setup_manager: tuple[SimpleNamespace, Any, Any], tmp_path: Path, kind: str
) -> None:
    """Missing and inaccessible input types receive a useful file error."""
    modules, library, dialog = setup_manager
    path = tmp_path / "missing.csv" if kind == "missing" else tmp_path
    assert dialog._import_corrections(path) is False
    assert raw_rows(library) == []
    assert str(path) in str(modules.wx.MessageBox.call_args)
    modules.wx.PostEvent.assert_not_called()


def test_header_only_import_preserves_selection(
    setup_manager: tuple[SimpleNamespace, Any, Any], tmp_path: Path
) -> None:
    """A valid empty batch is a successful no-op, preserving active edits."""
    modules, library, dialog = setup_manager
    library.insert_correction_data("R1", 90, (0, 0))
    dialog.populate_corrections_list()
    select(dialog, 0)
    before = (raw_rows(library), dialog.selected_record.rowid)
    path = tmp_path / "header.csv"
    path.write_text("Pattern,Rotation\n")
    assert dialog._import_corrections(path) is True
    assert (raw_rows(library), dialog.selected_record.rowid) == before
    modules.wx.PostEvent.assert_not_called()


def abort_writes(library: Any, trigger: str) -> None:
    """Install a real SQLite trigger to exercise rollback after preceding writes."""
    with closing(sqlite3.connect(library.correctionsdb_file)) as connection, connection:
        connection.execute(trigger)


def test_import_database_failure_rolls_back_previous_write(
    setup_manager: tuple[SimpleNamespace, Any, Any], tmp_path: Path
) -> None:
    """A storage failure after a replacement cannot leave a partially imported file."""
    modules, library, dialog = setup_manager
    library.insert_correction_data("R1", 90, (0, 0))
    original = raw_rows(library)
    abort_writes(
        library,
        "CREATE TRIGGER reject_insert BEFORE INSERT ON correction BEGIN SELECT RAISE(ABORT, 'test failure'); END",
    )
    path = tmp_path / "failure.csv"
    path.write_text("Pattern,Rotation\nR1,180\nR2,90\n")
    assert dialog._import_corrections(path) is False
    assert raw_rows(library) == original
    modules.wx.PostEvent.assert_not_called()
    assert "test failure" in str(modules.wx.MessageBox.call_args)


@pytest.mark.parametrize(
    "method", ["import_corrections_dialog", "export_corrections_dialog"]
)
def test_file_dialog_cancellation_changes_nothing(
    setup_manager: tuple[SimpleNamespace, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    """Canceling either file picker cannot touch storage or emit completion."""
    modules, library, dialog = setup_manager
    picker = MagicMock()
    picker.__enter__.return_value = picker
    picker.ShowModal.return_value = modules.wx.ID_CANCEL
    monkeypatch.setattr(
        modules.wx, "FileDialog", MagicMock(return_value=picker), raising=False
    )
    assert getattr(dialog, method)() is False
    picker.GetPath.assert_not_called()
    assert raw_rows(library) == []
    modules.wx.PostEvent.assert_not_called()


@pytest.mark.parametrize(
    "rows",
    [
        [("R1", "47u", 0, 0)],
        [("R1", 90, "NaN", 0)],
        [("[", 90, 0, 0)],
        [(None, None, None, None)],
        [("R1", 90, 0, 0), ("R1", 180, 0, 0)],
    ],
)
def test_recovery_displays_every_original_row(
    modules: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rows: list[tuple[Any, ...]],
) -> None:
    """Invalid and conflicting rows remain visible, selectable, and diagnosed."""
    library = make_library(modules.library, tmp_path, rows)
    dialog = manager(modules, library, monkeypatch)
    for index, record in enumerate(library.read_correction_data().rows):
        expected = tuple(map(str, (record.pattern, record.rotation, *record.offset)))
        if rows == [("R1", 90, 0, 0), ("R1", 180, 0, 0)]:
            expected = ("R1", "90" if index == 0 else "180", "0.0", "0.0")
        assert dialog.corrections_list.rows[index][:4] == list(expected)
        assert dialog.corrections_list.rows[index][4]
        select(dialog, index)
        assert field_values(dialog) == expected
        assert dialog.selected_record.rowid == record.rowid
        assert dialog.selection_db_path == library.correctionsdb_file
    assert "need repair" in dialog.correction_status.value
    assert "blocked" in dialog.correction_status.value
    assert dialog.correction_status.tooltip
    modules.wx.MessageBox.assert_not_called()


def test_missing_schema_is_visible_and_accessible(
    setup_manager: tuple[SimpleNamespace, Any, Any],
) -> None:
    """Storage errors appear in the manager even without any readable rows."""
    modules, library, dialog = setup_manager
    abort_writes(library, "DROP TABLE correction")
    dialog.populate_corrections_list()
    assert dialog.corrections_list.rows == []
    assert "database" in dialog.correction_status.value
    assert library.correctionsdb_file in dialog.correction_status.value
    assert "Select a row" not in dialog.correction_status.value
    assert dialog.selected_record is None
    assert dialog.delete_button.enabled is False
    modules.wx.MessageBox.assert_not_called()


def write_rotation_archive(path: str, pattern: str = "ARCHIVED") -> None:
    """Create a legacy archive independently of the migration implementation."""
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE rotation (regex, rotation)")
        connection.execute("INSERT INTO rotation VALUES (?, 180)", (pattern,))


def test_manager_open_retries_global_archive_once_and_refresh_only_reads(
    modules: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opening the real manager imports a waiting archive without refresh retries."""
    library = make_library(modules.library, tmp_path, [("EXISTING", 90, 0, 0)])
    write_rotation_archive(library.rotationsdb_file)
    inspect_archive = MagicMock(wraps=library._legacy_rotation_rows)
    retry_migrations = MagicMock(wraps=library.retry_correction_migrations)
    monkeypatch.setattr(library, "_legacy_rotation_rows", inspect_archive)
    monkeypatch.setattr(library, "retry_correction_migrations", retry_migrations)

    dialog = manager(modules, library, monkeypatch)

    assert {row[1] for row in raw_rows(library)} == {"EXISTING", "ARCHIVED"}
    assert dialog.correction_snapshot.corrections is not None
    retry_migrations.assert_called_once_with()
    archive_reads = inspect_archive.call_count
    assert archive_reads > 0
    for _ in range(3):
        dialog.populate_corrections_list()
    assert inspect_archive.call_count == archive_reads
    retry_migrations.assert_called_once_with()
    assert reopened_rows(library) == [
        ("ARCHIVED", 180, (0.0, 0.0)),
        ("EXISTING", 90, (0.0, 0.0)),
    ]
    modules.wx.MessageBox.assert_not_called()


def test_manager_open_keeps_local_scope_isolated_from_archives(
    modules: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An active local editor never attempts migration of inactive global archives."""
    library = make_library(modules.library, tmp_path, [("LOCAL", 90, 0, 0)], local=True)
    Path(library.rotationsdb_file).write_bytes(b"unreadable archive")
    inspect_archive = MagicMock(wraps=library._legacy_rotation_rows)
    retry_migrations = MagicMock(wraps=library.retry_correction_migrations)
    monkeypatch.setattr(library, "_legacy_rotation_rows", inspect_archive)
    monkeypatch.setattr(library, "retry_correction_migrations", retry_migrations)

    dialog = manager(modules, library, monkeypatch)
    dialog.populate_corrections_list()

    inspect_archive.assert_not_called()
    retry_migrations.assert_not_called()
    assert dialog.global_corrections.GetValue() is False
    assert dialog.correction_snapshot.scope == "local"
    assert dialog.correction_snapshot.corrections is not None
    assert "archive" not in dialog.correction_status.value
    assert raw_rows(library)[0][1:] == ("LOCAL", 90, 0.0, 0.0)


def test_unknown_archive_warnings_allow_export_and_reopening_retries(
    modules: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Warnings identify every unknown file and the advertised reopen action works."""
    library = make_library(modules.library, tmp_path, [("EXISTING", 90, 0, 0)])
    for path in (library.rotationsdb_file, library.partsdb_file):
        Path(path).write_bytes(b"not an SQLite archive")

    dialog = manager(modules, library, monkeypatch)

    assert dialog.correction_snapshot.corrections is not None
    assert dialog.correction_snapshot.issues == ()
    assert len(dialog.correction_snapshot.warnings) == 2
    for path in (library.rotationsdb_file, library.partsdb_file):
        assert path in dialog.correction_status.value
        assert path in dialog.correction_status.tooltip
    assert "reopen Corrections Manager to retry" in dialog.correction_status.value
    assert "Select a row" not in dialog.correction_status.value
    assert "blocked" not in dialog.correction_status.value
    export_path = tmp_path / "healthy.csv"
    assert dialog._export_corrections(export_path) is True
    assert "EXISTING" in export_path.read_text()

    for path, pattern in (
        (library.rotationsdb_file, "ROTATIONS"),
        (library.partsdb_file, "PARTS"),
    ):
        Path(path).unlink()
        write_rotation_archive(path, pattern)
    dialog.populate_corrections_list()
    assert len(dialog.correction_snapshot.warnings) == 2
    assert [row[1] for row in raw_rows(library)] == ["EXISTING"]

    reopened = manager(modules, fresh_library(library), monkeypatch)

    assert reopened.correction_snapshot.warnings == ()
    assert reopened.correction_snapshot.corrections is not None
    assert {row.pattern for row in reopened.correction_snapshot.rows} == {
        "EXISTING",
        "ROTATIONS",
        "PARTS",
    }
    modules.wx.MessageBox.assert_not_called()


def test_known_failed_transfer_has_file_retry_help_without_row_repair(
    modules: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed copy remains blocking until the stated reopen action succeeds."""
    library = make_library(modules.library, tmp_path, [("EXISTING", 90, 0, 0)])
    write_rotation_archive(library.rotationsdb_file)
    abort_writes(
        library,
        "CREATE TRIGGER reject_insert BEFORE INSERT ON correction "
        "BEGIN SELECT RAISE(ABORT, 'storage failure'); END",
    )

    dialog = manager(modules, library, monkeypatch)

    assert dialog.correction_snapshot.corrections is None
    assert "blocked" in dialog.correction_status.value
    assert library.rotationsdb_file in dialog.correction_status.value
    for details in (dialog.correction_status.value, dialog.correction_status.tooltip):
        assert library.globalcorrectionsdb_file in details
        assert "storage failure" in details
    assert "reopen Corrections Manager to retry" in dialog.correction_status.value
    assert "Select a row" not in dialog.correction_status.value
    assert [row[1] for row in raw_rows(library)] == ["EXISTING"]
    export_path = tmp_path / "previous.csv"
    export_path.write_text("previous valid export")
    assert dialog._export_corrections(export_path) is False
    assert export_path.read_text() == "previous valid export"

    abort_writes(library, "DROP TRIGGER reject_insert")
    dialog.populate_corrections_list()
    assert dialog.correction_snapshot.corrections is None
    reopened = manager(modules, fresh_library(library), monkeypatch)
    assert reopened.correction_snapshot.corrections is not None
    assert reopened.correction_snapshot.issues == ()
    assert {row[1] for row in raw_rows(library)} == {"EXISTING", "ARCHIVED"}


def test_manager_stays_accessible_after_initial_download_preparation_read_fails(
    modules: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Storage failing after the migration step cannot prevent opening recovery."""
    library = make_library(modules.library, tmp_path)
    monkeypatch.setattr(
        library,
        "_initial_seed_is_eligible",
        MagicMock(side_effect=sqlite3.OperationalError("seed-state unavailable")),
    )

    dialog = manager(modules, library, monkeypatch)

    assert library.globalcorrectionsdb_file in dialog.correction_status.value
    assert "seed-state unavailable" in dialog.correction_status.value
    assert "reopen Corrections Manager to retry" in dialog.correction_status.value
    assert "Select a row" not in dialog.correction_status.value
    assert dialog.global_corrections.GetValue() is True
    assert dialog.correction_snapshot.corrections == ()
    assert "blocked" not in dialog.correction_status.value
    modules.wx.MessageBox.assert_not_called()


@pytest.mark.parametrize(
    "field, value",
    [
        ("rotation", "47u"),
        ("rotation", "90.5"),
        ("offset_x", "NaN"),
        ("offset_y", "wrong"),
        ("regex", "["),
    ],
)
def test_invalid_manual_save_preserves_inputs_and_record(
    setup_manager: tuple[SimpleNamespace, Any, Any], field: str, value: Any
) -> None:
    """Manual entry shares strict validation and never defaults or truncates input."""
    modules, library, dialog = setup_manager
    library.insert_correction_data("R1", 90, (0, 0))
    dialog.populate_corrections_list()
    select(dialog, 0)
    getattr(dialog, field).SetValue(value)
    original = raw_rows(library)
    inputs = field_values(dialog)
    assert dialog.save_correction() is False
    assert raw_rows(library) == original
    assert field_values(dialog) == inputs
    assert value in str(modules.wx.MessageBox.call_args)
    modules.wx.PostEvent.assert_not_called()


def test_repair_invalid_row_clears_warning_and_survives_reopening(
    modules: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful precise repair restores a completely valid correction set."""
    library = make_library(modules.library, tmp_path, [("R1", "47u", 0, 0)])
    dialog = manager(modules, library, monkeypatch)
    select(dialog, 0)
    rowid = dialog.selected_record.rowid
    dialog.rotation.SetValue("-90.0")
    assert dialog.save_correction() is True
    assert raw_rows(library) == [(rowid, "R1", -90, 0.0, 0.0)]
    assert reopened_rows(library) == [("R1", -90, (0, 0))]
    assert "need repair" not in dialog.correction_status.value
    assert dialog.selected_record.rowid == rowid
    reopened = manager(modules, fresh_library(library), monkeypatch)
    select(reopened, 0)
    assert field_values(reopened) == ("R1", "-90", "0.0", "0.0")
    assert "need repair" not in reopened.correction_status.value
    modules.wx.PostEvent.assert_called_once()


def test_repair_one_of_multiple_invalid_rows_retains_warning(
    modules: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repairing one bad row does not hide another unresolved row."""
    library = make_library(
        modules.library, tmp_path, [("R1", "47u", 0, 0), ("R2", "bad", 0, 0)]
    )
    dialog = manager(modules, library, monkeypatch)
    select(dialog, 0)
    dialog.rotation.SetValue("90")
    assert dialog.save_correction() is True
    assert "need repair" in dialog.correction_status.value
    assert len(fresh_library(library).read_correction_data().issues) == 1


@pytest.mark.parametrize(
    "rows",
    [
        [(None, "47u", 0, 0), (None, "bad", 0, 0)],
        [("R1", "47u", 0, 0), ("R1", 90, 0, 0)],
    ],
)
def test_deletion_targets_exact_null_or_duplicate_row(
    modules: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rows: list[tuple[Any, ...]],
) -> None:
    """Deletion uses stored identity rather than a null or duplicated pattern."""
    library = make_library(modules.library, tmp_path, rows)
    dialog = manager(modules, library, monkeypatch)
    select(dialog, 0)
    rowid = dialog.selected_record.rowid
    expected = [row for row in raw_rows(library) if row[0] != rowid]
    assert dialog.delete_correction() is True
    assert raw_rows(library) == expected
    assert dialog.selected_record is None
    modules.wx.PostEvent.assert_called_once()


def test_list_refresh_keeps_selection_despite_native_events(
    setup_manager: tuple[SimpleNamespace, Any, Any],
) -> None:
    """A dirty refresh retains the record, leaving a fresh row selectable by click."""
    _modules, library, dialog = setup_manager
    library.insert_correction_data("R1", 90, (0, 0))
    dialog.populate_corrections_list()
    select(dialog, 0)
    rowid = dialog.selected_record.rowid
    set_inputs(dialog, rotation="unsaved")
    dialog.populate_corrections_list()
    assert dialog.selected_record.rowid == rowid
    assert dialog.rotation.GetValue() == "unsaved"
    assert dialog.corrections_list.GetSelectedRow() == -1
    assert dialog.delete_button.enabled is False


@pytest.mark.parametrize("operation", ["save_correction", "delete_correction"])
def test_stale_selection_cannot_modify_different_active_database(
    setup_manager: tuple[SimpleNamespace, Any, Any], operation: str
) -> None:
    """A rowid must never be reused after the active database path changes."""
    modules, library, dialog = setup_manager
    library.insert_correction_data("R1", 90, (0, 0))
    dialog.populate_corrections_list()
    select(dialog, 0)
    original = raw_rows(library)
    library.create_correction_table(library.localcorrectionsdb_file)
    library.insert_correction_data(
        "UNRELATED", 180, (0, 0), db_path=library.localcorrectionsdb_file
    )
    library.correctionsdb_file = library.localcorrectionsdb_file
    other = raw_rows(library)
    assert getattr(dialog, operation)() is False
    assert raw_rows(library) == other
    modules.wx.PostEvent.assert_not_called()
    assert "database changed" in str(modules.wx.MessageBox.call_args)
    select(dialog, 0)
    assert field_values(dialog) == ("UNRELATED", "180", "0.0", "0.0")
    assert getattr(dialog, operation)() is True
    library.correctionsdb_file = library.globalcorrectionsdb_file
    assert raw_rows(library) == original


@pytest.mark.parametrize("operation", ["save_correction", "delete_correction"])
@pytest.mark.parametrize("change", ["deleted", "reused", "edited"])
@pytest.mark.parametrize(
    "dirty, refresh", [(False, False), (True, False), (True, True)]
)
def test_stale_selection_preserves_concurrent_data_and_unsaved_inputs(
    setup_manager: tuple[SimpleNamespace, Any, Any],
    operation: str,
    change: str,
    dirty: bool,
    refresh: bool,
) -> None:
    """A stale editor cannot modify another writer's row, even after list refresh."""
    modules, library, dialog = setup_manager
    library.insert_correction_data("R1", 90, (0, 0))
    dialog.populate_corrections_list()
    select(dialog, 0)
    if dirty:
        set_inputs(dialog, rotation="270", offset_x="1.25")
    if change == "edited":
        library.update_correction_data("R1", 180, (3, 4))
    else:
        library.delete_correction_row(dialog.selected_record.rowid)
        if change == "reused":
            library.insert_correction_data("UNRELATED", 180, (3, 4))
    before = raw_rows(library)
    entered = field_values(dialog)
    if refresh:
        dialog.populate_corrections_list()

    assert getattr(dialog, operation)() is False
    assert raw_rows(library) == before
    assert field_values(dialog) == entered
    assert modules.wx.MessageBox.call_args.args[0]
    modules.wx.PostEvent.assert_not_called()
    if before:
        assert dialog.corrections_list.GetSelectedRow() == -1
        select(dialog, 0)
        assert field_values(dialog) == (before[0][1], "180", "3.0", "4.0")
        assert getattr(dialog, operation)() is True


@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.parametrize("change", ["edited", "deleted", "reused", "added"])
def test_replacement_confirmation_cannot_approve_concurrent_changes(
    setup_manager: tuple[SimpleNamespace, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    selected: bool,
    change: str,
) -> None:
    """Confirmation authorizes exactly the raw records displayed in the prompt."""
    modules, library, dialog = setup_manager
    library.insert_correction_data("A", 90, (0, 0))
    library.insert_correction_data("B", 180, (1, 2))
    dialog.populate_corrections_list()
    if selected:
        select(dialog, 0)
    set_inputs(dialog, "B", "270", "3", "4")
    before = []

    def confirm(_correction: Any, conflicts: Any) -> bool:
        assert [(row.pattern, row.rotation) for row in conflicts] == [("B", 180)]
        if change == "edited":
            library.update_correction_data("B", -90, (1, 2))
        elif change == "added":
            seed_raw(library, [("B", 0, 0, 0)])
        else:
            library.delete_correction_row(conflicts[0].rowid)
            if change == "reused":
                library.insert_correction_data("B", -90, (1, 2))
        before.extend(raw_rows(library))
        return True

    monkeypatch.setattr(dialog, "_confirm_replacement", confirm)
    assert dialog.save_correction() is False
    assert raw_rows(library) == before
    assert field_values(dialog) == ("B", "270", "3", "4")
    modules.wx.PostEvent.assert_not_called()
    assert modules.wx.MessageBox.call_args.args[0]


def test_duplicate_replacement_cancel_preserves_every_value(
    setup_manager: tuple[SimpleNamespace, Any, Any],
) -> None:
    """Declining replacement leaves the selected row and target untouched."""
    modules, library, dialog = setup_manager
    library.insert_correction_data("A", 90, (0, 0))
    library.insert_correction_data("B", 180, (1, 2))
    dialog.populate_corrections_list()
    select(dialog, 0)
    set_inputs(dialog, "B", "270.0", "1.25", "-3")
    original = raw_rows(library)
    inputs = field_values(dialog)
    modules.wx.MessageDialog.return_value.ShowModal.return_value = modules.wx.ID_NO
    assert dialog.save_correction() is False
    assert raw_rows(library) == original
    assert field_values(dialog) == inputs
    assert "180°" in modules.wx.MessageDialog.return_value.ExtendedMessage
    modules.wx.PostEvent.assert_not_called()
    modules.wx.MessageDialog.return_value.Destroy.assert_called_once()


def test_confirmed_replacement_commits_exact_selected_row(
    setup_manager: tuple[SimpleNamespace, Any, Any],
) -> None:
    """Confirmed rename/replacement preserves row identity and replaces its collision."""
    modules, library, dialog = setup_manager
    for pattern, rotation in (("A", 90), ("B", 180), ("Z", 270)):
        library.insert_correction_data(pattern, rotation, (0, 0))
    dialog.populate_corrections_list()
    select(dialog, 0)
    rowid = dialog.selected_record.rowid
    set_inputs(dialog, "B", "-90", "0.125", "-0.75")
    modules.wx.MessageDialog.return_value.ShowModal.return_value = modules.wx.ID_YES
    assert dialog.save_correction() is True
    assert raw_rows(library) == [
        (rowid, "B", -90, 0.125, -0.75),
        (3, "Z", 270, 0.0, 0.0),
    ]
    assert "180°" in modules.wx.MessageDialog.return_value.ExtendedMessage
    assert "270°" not in modules.wx.MessageDialog.return_value.ExtendedMessage
    assert dialog.selected_record.rowid == rowid
    modules.wx.PostEvent.assert_called_once()


def test_replacement_failure_restores_both_records(
    setup_manager: tuple[SimpleNamespace, Any, Any],
) -> None:
    """A failed update rolls back the confirmed deletion of a conflicting row."""
    modules, library, dialog = setup_manager
    library.insert_correction_data("A", 90, (0, 0))
    library.insert_correction_data("B", 180, (0, 0))
    dialog.populate_corrections_list()
    select(dialog, 0)
    set_inputs(dialog, "B", "-90")
    original = raw_rows(library)
    abort_writes(
        library,
        "CREATE TRIGGER reject_update BEFORE UPDATE ON correction BEGIN SELECT RAISE(ABORT, 'test failure'); END",
    )
    modules.wx.MessageDialog.return_value.ShowModal.return_value = modules.wx.ID_YES
    assert dialog.save_correction() is False
    assert raw_rows(library) == original
    assert field_values(dialog) == ("B", "-90", "0", "0")
    modules.wx.PostEvent.assert_not_called()


def test_identical_existing_manual_rule_selects_without_writing(
    setup_manager: tuple[SimpleNamespace, Any, Any],
) -> None:
    """Entering an identical existing correction keeps the historical selection behavior."""
    modules, library, dialog = setup_manager
    library.insert_correction_data("R1", 90, (0, 0))
    set_inputs(dialog)
    before = raw_rows(library)
    assert dialog.save_correction() is True
    assert raw_rows(library) == before
    assert dialog.selected_record.rowid == before[0][0]
    modules.wx.MessageDialog.assert_not_called()
    modules.wx.PostEvent.assert_not_called()


def legacy_csv(
    modules: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contents: str = "Pattern,Rotation\nR1,90\n",
) -> Path:
    """Place a legacy source only in this test's plugin directory."""
    plugin = tmp_path / "plugin"
    directory = plugin / "corrections"
    directory.mkdir(parents=True)
    path = directory / "cpl_rotations_db.csv"
    path.write_text(contents)
    monkeypatch.setattr(modules.corrections, "PLUGIN_PATH", str(plugin))
    return path


def test_legacy_success_marks_and_archives_after_commit(
    setup_manager: tuple[SimpleNamespace, Any, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Archival follows successful persistence and refresh waits for constructed controls."""
    modules, library, dialog = setup_manager
    path = legacy_csv(modules, tmp_path, monkeypatch)
    original = path.read_bytes()
    key = library.correction_csv_migration_key(path, original)
    assert dialog.import_legacy_corrections() is True
    assert fresh_library(library).has_correction_migration(key) is True
    assert reopened_rows(library) == [("R1", 90, (0, 0))]
    assert not path.exists()
    assert path.with_suffix(".csv.backup").read_bytes() == original
    assert dialog.corrections_list.rows == []
    modules.wx.PostEvent.assert_called_once()
    modules.wx.MessageBox.assert_not_called()


@pytest.mark.parametrize("archive_problem", ["collision", "link_error", "unlink_error"])
def test_archive_failure_never_replays_over_repairs(
    setup_manager: tuple[SimpleNamespace, Any, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    archive_problem: Any,
) -> None:
    """Committed content provenance suppresses replay when the source remains in place."""
    modules, library, dialog = setup_manager
    path = legacy_csv(modules, tmp_path, monkeypatch)
    backup = path.with_suffix(".csv.backup")
    if archive_problem == "collision":
        backup.write_bytes(b"existing archive")
    else:
        operation = "link" if archive_problem == "link_error" else "unlink"
        monkeypatch.setattr(
            modules.corrections.os,
            operation,
            MagicMock(side_effect=PermissionError("archive denied")),
        )
    assert dialog.import_legacy_corrections() is True
    assert path.exists()
    if archive_problem == "collision":
        assert backup.read_bytes() == b"existing archive"
    library.update_correction_data("R1", 180, (1, 2))
    repaired = raw_rows(library)
    modules.wx.PostEvent.reset_mock()
    dialog.parent.library = fresh_library(library)
    assert dialog.import_legacy_corrections() is True
    assert raw_rows(library) == repaired
    modules.wx.PostEvent.assert_not_called()
    assert "imported successfully" in str(modules.wx.MessageBox.call_args)
    assert "will not be imported again" in str(modules.wx.MessageBox.call_args)


def test_legacy_completion_survives_scope_switch(
    setup_manager: tuple[SimpleNamespace, Any, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remaining global legacy CSV cannot replay over subsequent local repairs."""
    modules, library, dialog = setup_manager
    path = legacy_csv(modules, tmp_path, monkeypatch)
    path.with_suffix(".csv.backup").write_bytes(b"old archive")
    assert dialog.import_legacy_corrections() is True
    library.switch_to_global_correction_database(False)
    library.update_correction_data("R1", 180, (1, 2))
    repaired = raw_rows(library)
    dialog.parent.library = fresh_library(library)
    assert dialog.import_legacy_corrections() is True
    assert raw_rows(library) == repaired


def test_changed_legacy_contents_can_be_imported_once(
    setup_manager: tuple[SimpleNamespace, Any, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Content changes distinguish corrected/new automatic inputs from completed ones."""
    modules, library, dialog = setup_manager
    path = legacy_csv(modules, tmp_path, monkeypatch)
    path.with_suffix(".csv.backup").write_bytes(b"old archive")
    assert dialog.import_legacy_corrections() is True
    path.write_text("Pattern,Rotation\nR1,180\n")
    assert dialog.import_legacy_corrections() is True
    assert reopened_rows(library) == [("R1", 180, (0, 0))]


@pytest.mark.parametrize("failure", ["invalid", "decoding", "read", "storage"])
def test_legacy_failure_preserves_source_for_retry(
    setup_manager: tuple[SimpleNamespace, Any, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Failed legacy imports retain exact unmarked input for repair and retry."""
    modules, library, dialog = setup_manager
    path = legacy_csv(modules, tmp_path, monkeypatch)
    if failure == "invalid":
        path.write_text("Pattern,Rotation\nR1,47u\n")
    elif failure == "decoding":
        path.write_bytes(b"\xff")
    elif failure == "read":
        monkeypatch.setattr(
            modules.corrections,
            "open",
            MagicMock(side_effect=PermissionError("denied")),
            raising=False,
        )
    else:
        abort_writes(
            library,
            "CREATE TRIGGER reject_insert BEFORE INSERT ON correction BEGIN SELECT RAISE(ABORT, 'storage failure'); END",
        )
    before = path.read_bytes()
    assert dialog.import_legacy_corrections() is False
    assert path.read_bytes() == before
    assert not path.with_suffix(".csv.backup").exists()
    assert raw_rows(library) == []
    assert not library.has_correction_migration(
        library.correction_csv_migration_key(path, path.read_bytes())
    )
    modules.wx.PostEvent.assert_not_called()
    if failure == "invalid":
        path.write_text("Pattern,Rotation\nR1,90\n")
        assert dialog.import_legacy_corrections() is True
        assert reopened_rows(library) == [("R1", 90, (0, 0))]


def test_manager_constructor_imports_only_after_controls_exist(
    setup_manager: tuple[SimpleNamespace, Any, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real constructor can import legacy input without touching uncreated widgets."""
    modules, library, _dialog = setup_manager
    legacy_csv(modules, tmp_path, monkeypatch)
    parent = SimpleNamespace(library=library, scale_factor=1, window=object())
    dialog = modules.corrections.CorrectionManagerDialog(parent, "R1")
    assert len(dialog.corrections_list.rows) == 1
    assert dialog.corrections_list.rows == [["R1", "90", "0.0", "0.0", "", "Footprint"]]
    assert (
        modules.wx.dataview.DataViewListCtrl.call_args.kwargs["style"]
        == modules.wx.dataview.DV_SINGLE
    )
    assert dialog.corrections_list.columns == [
        "Regex",
        "Rotation",
        "Offset X",
        "Offset Y",
        "Status",
        "Kind",
    ]
    assert dialog.global_corrections.GetValue() is True
    select(dialog, 0)
    assert field_values(dialog) == ("R1", "90", "0.0", "0.0")
    assert dialog.delete_button.enabled is True
    modules.wx.PostEvent.assert_called_once()
    assert raw_rows(library)[0][1:3] == ("R1", 90)


@pytest.mark.parametrize("kind", ["invalid", "conflicting", "schema"])
def test_export_errors_leave_existing_bytes_unchanged(
    setup_manager: tuple[SimpleNamespace, Any, Any], tmp_path: Path, kind: str
) -> None:
    """No correction export opens its destination until the snapshot fully validates."""
    modules, library, dialog = setup_manager
    if kind == "invalid":
        seed_raw(library, [("R1", "47u", 0, 0)])
    elif kind == "conflicting":
        seed_raw(library, [("R1", 90, 0, 0), ("R1", 180, 0, 0)])
    else:
        abort_writes(library, "DROP TABLE correction")
    path = tmp_path / "existing.csv"
    path.write_bytes(b"previous export\x00\xff")
    assert dialog._export_corrections(path) is False
    assert path.read_bytes() == b"previous export\x00\xff"
    assert str(path) in str(modules.wx.MessageBox.call_args)


def test_export_and_import_preserve_quoted_unicode_and_precision(
    setup_manager: tuple[SimpleNamespace, Any, Any], tmp_path: Path
) -> None:
    """CSV round trips preserve literal patterns, numeric precision, and whole rotations."""
    modules, library, dialog = setup_manager
    pattern = 'Ω,"O\'Brien"\\d+'
    library.insert_correction_data(pattern, -90, (0.12345678901234568, -1.25))
    path = tmp_path / "roundtrip.csv"
    assert dialog._export_corrections(path) is True
    assert path.read_text().startswith('"Pattern","Rotation","Offset X","Offset Y"')
    destination = make_library(modules.library, tmp_path / "reimport")
    dialog.parent.library = destination
    assert dialog._import_corrections(path) is True
    assert reopened_rows(destination) == reopened_rows(library)


def test_export_open_failure_is_actionable(
    setup_manager: tuple[SimpleNamespace, Any, Any], tmp_path: Path
) -> None:
    """An unwritable destination becomes a handled file-specific error."""
    modules, _library, dialog = setup_manager
    assert dialog._export_corrections(tmp_path) is False
    assert str(tmp_path) in str(modules.wx.MessageBox.call_args)


@pytest.mark.parametrize("confirmation", ["ID_NO", "ID_CANCEL"])
def test_scope_cancellation_restores_checkbox(
    setup_manager: tuple[SimpleNamespace, Any, Any], confirmation: str
) -> None:
    """Any unconfirmed scope change restores the active scope without refresh."""
    modules, library, dialog = setup_manager
    modules.wx.MessageDialog.return_value.ShowModal.return_value = getattr(
        modules.wx, confirmation
    )
    dialog.global_corrections.SetValue(False)
    assert dialog.on_global_corrections_changed() is False
    assert dialog.global_corrections.GetValue() is True
    assert library.correctionsdb_file == library.globalcorrectionsdb_file
    modules.wx.PostEvent.assert_not_called()


def test_invalid_scope_switch_keeps_active_data_and_selection(
    setup_manager: tuple[SimpleNamespace, Any, Any],
) -> None:
    """Rejected scope transfers preserve the source and native checkbox state."""
    modules, library, dialog = setup_manager
    seed_raw(library, [("R1", "47u", 0, 0)])
    dialog.populate_corrections_list()
    select(dialog, 0)
    before = (raw_rows(library), dialog.selected_record.rowid)
    modules.wx.MessageDialog.return_value.ShowModal.return_value = modules.wx.ID_YES
    dialog.global_corrections.SetValue(False)
    assert dialog.on_global_corrections_changed() is False
    assert dialog.global_corrections.GetValue() is True
    assert (raw_rows(library), dialog.selected_record.rowid) == before
    assert library.correctionsdb_file == library.globalcorrectionsdb_file
    modules.wx.PostEvent.assert_not_called()


def test_scope_checkbox_uses_active_path_even_if_other_database_appears(
    setup_manager: tuple[SimpleNamespace, Any, Any],
) -> None:
    """Existing inactive tables cannot change the checkbox's interpretation of scope."""
    modules, library, dialog = setup_manager
    library.create_correction_table(library.localcorrectionsdb_file)
    library.insert_correction_data(
        "UNRELATED", 180, (0, 0), db_path=library.localcorrectionsdb_file
    )
    assert library.uses_global_correction_database() is False
    modules.wx.MessageDialog.return_value.ShowModal.return_value = modules.wx.ID_NO
    assert dialog.on_global_corrections_changed() is False
    assert dialog.global_corrections.GetValue() is True
    assert library.correctionsdb_file == library.globalcorrectionsdb_file


def test_successful_scope_switch_clears_selection_and_refreshes(
    setup_manager: tuple[SimpleNamespace, Any, Any],
) -> None:
    """A successful copy clears global row identity before showing local data."""
    modules, library, dialog = setup_manager
    library.insert_correction_data("R1", 90, (0, 0))
    dialog.populate_corrections_list()
    select(dialog, 0)
    modules.wx.MessageDialog.return_value.ShowModal.return_value = modules.wx.ID_YES
    assert dialog.on_global_corrections_changed() is True
    assert dialog.selected_record is None
    assert dialog.selection_db_path is None
    assert dialog.global_corrections.GetValue() is False
    assert dialog.correction_snapshot.scope == "local"
    assert reopened_rows(library) == [("R1", 90, (0, 0))]
    modules.wx.PostEvent.assert_called_once()


@pytest.mark.parametrize("bad", [False, True])
def test_remote_update_refreshes_only_after_success(
    setup_manager: tuple[SimpleNamespace, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    bad: bool,
) -> None:
    """The real HTTP ingestion path controls whether the manager posts a refresh."""
    modules, library, dialog = setup_manager
    response = MagicMock()
    response.text = "Pattern,Rotation\nR1," + ("47u" if bad else "90") + "\n"
    monkeypatch.setattr(
        modules.library.requests, "get", MagicMock(return_value=response)
    )
    assert dialog.download_correction_data() is not bad
    events = [call.args[1] for call in modules.wx.PostEvent.call_args_list]
    refreshes = [
        event
        for event in events
        if isinstance(event, modules.corrections.PopulateFootprintListEvent)
    ]
    assert len(refreshes) == (0 if bad else 1)
    assert len(raw_rows(library)) == (0 if bad else 1)
    assert len(dialog.corrections_list.rows) == (0 if bad else 1)


@pytest.mark.parametrize("dirty", [False, True])
def test_selected_import_refreshes_clean_inputs_and_preserves_unsaved_edits(
    setup_manager: tuple[SimpleNamespace, Any, Any], tmp_path: Path, dirty: bool
) -> None:
    """Import refresh respects edits and never authorizes a stale save of its replacement."""
    _modules, library, dialog = setup_manager
    library.insert_correction_data("R1", 90, (0, 0))
    dialog.populate_corrections_list()
    select(dialog, 0)
    rowid = dialog.selected_record.rowid
    if dirty:
        dialog.rotation.SetValue("270")
    entered = field_values(dialog)
    path = tmp_path / "overwrite.csv"
    path.write_text("Pattern,Rotation,Offset X,Offset Y\nR1,180,1,2\n")
    assert dialog._import_corrections(path) is True
    assert dialog.selected_record.rowid == rowid
    assert reopened_rows(library) == [("R1", 180, (1, 2))]
    for _ in range(2):
        assert field_values(dialog) == (
            entered if dirty else ("R1", "180", "1.0", "2.0")
        )
        dialog.populate_corrections_list()
    assert dialog.save_correction() is not dirty
    select(dialog, 0)
    assert field_values(dialog) == ("R1", "180", "1.0", "2.0")
    assert dialog.save_correction() is True
    assert reopened_rows(library) == [("R1", 180, (1, 2))]


@pytest.mark.parametrize("dirty", [False, True])
def test_refresh_updates_only_untouched_selected_inputs(
    setup_manager: tuple[SimpleNamespace, Any, Any], dirty: bool
) -> None:
    """Repeated refreshes keep clean fields current and preserve actual unsaved edits."""
    _modules, library, dialog = setup_manager
    library.insert_correction_data("R1", 90, (0, 0))
    dialog.populate_corrections_list()
    select(dialog, 0)
    original = field_values(dialog)
    if dirty:
        dialog.rotation.SetValue("270")
        original = field_values(dialog)
    for rotation in (180, -90):
        library.update_correction_data("R1", rotation, (1, 2))
        dialog.populate_corrections_list()
        assert field_values(dialog) == (
            original if dirty else ("R1", str(rotation), "1.0", "2.0")
        )
    assert dialog.save_correction() is not dirty
    assert reopened_rows(library) == [("R1", -90, (1, 2))]
    if dirty:
        select(dialog, 0)
    library.update_correction_data("R1", 180, (3, 4))
    dialog.populate_corrections_list()
    assert field_values(dialog) == ("R1", "180", "3.0", "4.0")


def test_remote_refresh_synchronizes_clean_selection_after_external_update(
    setup_manager: tuple[SimpleNamespace, Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Successful remote refresh also shows committed same-path updates in untouched fields."""
    modules, library, dialog = setup_manager
    library.insert_correction_data("R1", 90, (0, 0))
    dialog.populate_corrections_list()
    select(dialog, 0)

    # The remote policy keeps existing values. Simulate another writer changing
    # the selected rule while the HTTP request is in progress.
    def remote_response(*_args: Any, **_kwargs: Any) -> MagicMock:
        library.update_correction_data("R1", 180, (1, 2))
        response = MagicMock()
        response.text = "Pattern,Rotation\nR2,90\n"
        return response

    monkeypatch.setattr(modules.library.requests, "get", remote_response)
    assert dialog.download_correction_data() is True
    assert field_values(dialog) == ("R1", "180", "1.0", "2.0")
    assert dialog.save_correction() is True
    assert reopened_rows(library) == [
        ("R1", 180, (1, 2)),
        ("R2", 90, (0, 0)),
    ]


def test_switch_to_global_warns_that_project_corrections_are_discarded(
    setup_manager: tuple[SimpleNamespace, Any, Any],
) -> None:
    """A scope change must disclose the loss of project-specific corrections."""
    modules, library, dialog = setup_manager
    library.switch_to_global_correction_database(False)
    dialog.populate_corrections_list()
    dialog.global_corrections.SetValue(True)
    modules.wx.MessageDialog.return_value.ShowModal.return_value = modules.wx.ID_NO

    assert dialog.on_global_corrections_changed() is False

    warning = modules.wx.MessageDialog.return_value.ExtendedMessage
    assert isinstance(warning, str)
    assert "project-specific corrections" in warning
    assert "discarded" in warning
    assert modules.wx.MessageDialog.call_args.args[3] & modules.wx.NO_DEFAULT
    assert dialog.global_corrections.GetValue() is False


@pytest.mark.parametrize("confirmation", ["ID_YES", "ID_NO", "ID_CANCEL"])
def test_invalid_local_corrections_can_be_discarded_for_healthy_global(
    setup_manager: tuple[SimpleNamespace, Any, Any], confirmation: str
) -> None:
    """Repair remains possible by leaving an invalid local copy after confirmation."""
    modules, library, dialog = setup_manager
    library.insert_correction_data("GLOBAL", 90, (1, 2))
    library.switch_to_global_correction_database(False)
    seed_raw(library, [("BROKEN", "47u", 0, 0)])
    dialog.populate_corrections_list()
    select(dialog, 1)
    before = (raw_rows(library), dialog.selected_record.rowid, field_values(dialog))
    local_path = library.correctionsdb_file
    modules.wx.MessageDialog.return_value.ShowModal.return_value = getattr(
        modules.wx, confirmation
    )
    dialog.global_corrections.SetValue(True)

    confirmed = confirmation == "ID_YES"
    handler = dialog.global_corrections.bindings[modules.wx.EVT_CHECKBOX]
    assert handler() is confirmed

    if confirmed:
        assert library.correctionsdb_file == library.globalcorrectionsdb_file
        assert dialog.global_corrections.GetValue() is True
        assert dialog.selected_record is None
        assert fresh_library(library).read_correction_data().issues == ()
        assert [row[1:] for row in raw_rows(library)] == [("GLOBAL", 90, 1.0, 2.0)]
        with closing(sqlite3.connect(local_path)) as connection:
            assert (
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='correction'"
                ).fetchall()
                == []
            )
        modules.wx.PostEvent.assert_called_once()
    else:
        assert library.correctionsdb_file == local_path
        assert dialog.global_corrections.GetValue() is False
        assert (
            raw_rows(library),
            dialog.selected_record.rowid,
            field_values(dialog),
        ) == before
        modules.wx.PostEvent.assert_not_called()


@pytest.mark.parametrize("global_problem", ["invalid", "invalid_schema"])
def test_invalid_local_scope_switch_failure_preserves_editor_and_both_databases(
    setup_manager: tuple[SimpleNamespace, Any, Any], global_problem: str
) -> None:
    """A broken global destination must not discard the local recovery path."""
    modules, library, dialog = setup_manager
    library.insert_correction_data("GLOBAL", 90, (1, 2))
    library.switch_to_global_correction_database(False)
    seed_raw(library, [("BROKEN", "47u", 0, 0)])
    dialog.populate_corrections_list()
    select(dialog, 1)
    dialog.rotation.SetValue("unsaved repair")
    original_local = raw_rows(library)
    local_path = library.correctionsdb_file
    selected = dialog.selected_record.rowid
    editor = field_values(dialog)
    with (
        closing(sqlite3.connect(library.globalcorrectionsdb_file)) as connection,
        connection,
    ):
        if global_problem == "invalid":
            connection.execute("UPDATE correction SET rotation='bad'")
        else:
            connection.execute(
                "ALTER TABLE correction RENAME COLUMN rotation TO broken"
            )
    global_bytes = Path(library.globalcorrectionsdb_file).read_bytes()
    modules.wx.MessageDialog.return_value.ShowModal.return_value = modules.wx.ID_YES
    dialog.global_corrections.SetValue(True)

    handler = dialog.global_corrections.bindings[modules.wx.EVT_CHECKBOX]
    assert handler() is False

    assert library.correctionsdb_file == local_path
    assert dialog.global_corrections.GetValue() is False
    assert dialog.selected_record.rowid == selected
    assert field_values(dialog) == editor
    assert raw_rows(library) == original_local
    assert Path(library.globalcorrectionsdb_file).read_bytes() == global_bytes
    assert [row[1:] for row in raw_rows(fresh_library(library))] == [
        ("GLOBAL", 90, 1.0, 2.0),
        ("BROKEN", "47u", 0, 0),
    ]
    modules.wx.PostEvent.assert_not_called()
    modules.wx.MessageBox.assert_called_once()


@pytest.mark.parametrize("condition", ["unknown_archive", "known_transfer", "drop"])
def test_scope_switch_distinguishes_archive_warnings_from_blocking_failures(
    modules: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    condition: str,
) -> None:
    """Confirmed local discard ignores unknown archives but preserves failed transfers."""
    library = make_library(
        modules.library, tmp_path, [("LOCAL", "47u", 0, 0)], local=True
    )
    library.create_correction_table(library.globalcorrectionsdb_file)
    with (
        closing(sqlite3.connect(library.globalcorrectionsdb_file)) as connection,
        connection,
    ):
        connection.execute("INSERT INTO correction VALUES ('GLOBAL', 90, 1, 2)")
        if condition == "known_transfer":
            connection.execute(
                "CREATE TRIGGER reject_insert BEFORE INSERT ON correction "
                "BEGIN SELECT RAISE(ABORT, 'transfer denied'); END"
            )
    if condition == "unknown_archive":
        Path(library.rotationsdb_file).write_bytes(b"unreadable archive")
    elif condition == "known_transfer":
        write_rotation_archive(library.rotationsdb_file)
    dialog = manager(modules, library, monkeypatch)
    select(dialog, 0)
    dialog.rotation.SetValue("unsaved local repair")
    before = (raw_rows(library), dialog.selected_record.rowid, field_values(dialog))
    local_path = library.correctionsdb_file
    if condition == "drop":
        connect = sqlite3.connect

        def deny_drop(action: int, *_args: object) -> int:
            """Reject the native SQLite drop while permitting rollback and reads."""
            return (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_DROP_TABLE
                else sqlite3.SQLITE_OK
            )

        def connect_with_drop_failure(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            """Apply the failure only to actual connections to the local database."""
            connection = connect(*args, **kwargs)
            if connection.execute("PRAGMA database_list").fetchone()[2] == local_path:
                connection.set_authorizer(deny_drop)
            return connection

        monkeypatch.setattr(
            modules.library.sqlite3, "connect", connect_with_drop_failure
        )
    modules.wx.MessageDialog.return_value.ShowModal.return_value = modules.wx.ID_YES
    dialog.global_corrections.SetValue(True)

    assert dialog.global_corrections.bindings[modules.wx.EVT_CHECKBOX]() is (
        condition == "unknown_archive"
    )

    if condition == "unknown_archive":
        assert library.correctionsdb_file == library.globalcorrectionsdb_file
        assert dialog.global_corrections.GetValue() is True
        assert dialog.selected_record is None
        assert dialog.correction_snapshot.corrections is not None
        assert dialog.correction_snapshot.issues == ()
        assert library.rotationsdb_file in dialog.correction_status.value
        assert "reopen Corrections Manager to retry" in dialog.correction_status.value
        assert raw_rows(library)[0][1:] == ("GLOBAL", 90, 1, 2)
        with closing(sqlite3.connect(local_path)) as connection:
            assert (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='correction'"
                ).fetchone()
                is None
            )
        modules.wx.PostEvent.assert_called_once()
        modules.wx.MessageBox.assert_not_called()
    else:
        assert library.correctionsdb_file == local_path
        assert dialog.global_corrections.GetValue() is False
        assert (
            raw_rows(library),
            dialog.selected_record.rowid,
            field_values(dialog),
        ) == before
        assert fresh_library(library).correctionsdb_file == local_path
        assert dialog.correction_snapshot.scope == "local"
        modules.wx.PostEvent.assert_not_called()
        modules.wx.MessageBox.assert_called_once()
        relevant_path = (
            library.globalcorrectionsdb_file
            if condition == "known_transfer"
            else local_path
        )
        assert relevant_path in modules.wx.MessageBox.call_args.args[0]
        assert (
            "try switching databases again" in modules.wx.MessageBox.call_args.args[0]
        )


def test_empty_corrections_export_header_and_remain_ready(
    setup_manager: tuple[SimpleNamespace, Any, Any], tmp_path: Path
) -> None:
    """A healthy empty database is exportable, unlike an unavailable collection."""
    modules, library, dialog = setup_manager
    path = tmp_path / "empty.csv"

    assert dialog._export_corrections(path) is True

    assert path.read_text() == '"Pattern","Rotation","Offset X","Offset Y"\n'
    assert fresh_library(library).get_all_correction_data() == ()
    modules.wx.MessageBox.assert_not_called()
