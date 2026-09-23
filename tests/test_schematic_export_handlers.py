"""Exercise save-on-close boundaries with real capture and schematic writing."""

from collections.abc import Iterator
from importlib import import_module
import logging
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest

from .native_window_support import choose_output, window_ui
from .test_schematic_autosave_close import CloseEvent
from .variant_native_support import Board
from .wx_harness import load_mainwindow, load_siblings, module, wx_stubs

__all__ = ["window_ui"]


def _schematic() -> str:
    """Provide real base properties whose values reveal unsafe clears or updates."""
    return """(kicad_sch
  (symbol
    (lib_id "Device:R")
    (in_bom yes)
    (property "Reference" "R1"
      (at 0 0 0)
    )
    (property "LCSC" "C777"
      (at 0 1 0)
    )
    (property "JLCPCB PartNr" "C777"
      (at 0 2 0)
    )
    (instances
      (project "board"
        (path "/first"
          (reference "R1")
          (unit 1)
          (variant (name "A")
            (field (name "LCSC") (value "C880"))
          )
          (variant (name "B")
            (field (name "LCSC") (value "C881"))
          )
        )
      )
    )
  )
)
"""


@pytest.fixture
def ordinary(tmp_path: Path) -> Iterator[SimpleNamespace]:
    """Bind real close, backup, and save handlers around stateful PCB objects."""
    board = Board()
    pcbnew = module("pcbnew", GetBuildVersion=lambda: "10.0.6", GetBoard=lambda: board)
    dialog = MagicMock()
    wx = wx_stubs(
        Frame=type("Frame", (), {}),
        NewIdRef=Mock(side_effect=object),
        FileDialog=Mock(),
        MessageBox=Mock(),
        GenericMessageDialog=Mock(return_value=dialog),
    )
    with load_siblings(
        "_export_handler_source",
        ("schematic_safety", "schematicexport"),
        {"pcbnew": pcbnew},
    ) as loaded:
        safety = loaded["schematic_safety"]
        exporter = loaded["schematicexport"]
        main = load_mainwindow(
            "_export_handler_window",
            wx=wx,
            pcbnew=pcbnew,
            schematicexport={"SchematicExport": exporter.SchematicExport},
            schematic_safety={
                "SchematicLockedError": safety.SchematicLockedError,
                "authenticated_project_name": safety.authenticated_project_name,
                "backup_schematics": safety.backup_schematics,
                "resolve_project_schematics": safety.resolve_project_schematics,
            },
        )
        path = tmp_path / "board.kicad_sch"
        path.write_text(_schematic(), encoding="utf-8")
        dialog.ShowModal.return_value = main.wx.ID_NO
        parent = SimpleNamespace(
            _closing=False,
            _saving_on_close=False,
            GetChildren=lambda: [],
            Destroy=Mock(),
            project_path=str(tmp_path),
            board_name="board.kicad_pcb",
            schematic_name=path.name,
            pcbnew=pcbnew,
            logger=logging.getLogger("schematic_export_handler_test"),
            store=SimpleNamespace(
                read_all_parts=Mock(side_effect=AssertionError("Cached parts read"))
            ),
        )
        for name in (
            "quit_dialog",
            "export_to_schematic",
            "confirm_locked_schematic_export",
            "_backup_schematics_before_first_write",
        ):
            setattr(parent, name, MethodType(getattr(main.JLCPCBTools, name), parent))
        yield SimpleNamespace(
            board=board,
            path=path,
            parent=parent,
            dialog=dialog,
            wx=main.wx,
            main=main,
            exporter=exporter,
            backup=tmp_path / "jlcpcb" / main.SCHEMATIC_PRE_WRITE_BACKUP_ZIP,
        )


def test_cancelled_backup_keeps_window_open_without_capture_and_can_retry(
    ordinary: SimpleNamespace,
) -> None:
    """The permanent-backup gate can cancel close before capturing assignments."""
    ordinary.dialog.ShowModal.return_value = ordinary.wx.ID_CANCEL
    event = CloseEvent()
    with (
        patch.object(
            ordinary.board, "GetFootprints", side_effect=AssertionError("Board read")
        ) as read,
        patch.object(
            ordinary.main,
            "backup_schematics",
            side_effect=PermissionError("Permanent backup is unwritable"),
        ),
    ):
        ordinary.parent.quit_dialog(event)
    read.assert_not_called()
    assert event.vetoed and not ordinary.parent._closing
    ordinary.parent.Destroy.assert_not_called()
    ordinary.wx.FileDialog.assert_not_called()
    ordinary.parent.store.read_all_parts.assert_not_called()
    ordinary.wx.MessageBox.assert_not_called()
    assert ordinary.path.read_text(encoding="utf-8") == _schematic()
    assert not Path(str(ordinary.path) + "_old").exists()
    assert not ordinary.backup.exists()
    _parent, text, title, style = ordinary.wx.GenericMessageDialog.call_args.args
    assert "Permanent backup is unwritable" in text
    assert title == "Schematic backup failed" and style & ordinary.wx.ICON_WARNING
    ordinary.dialog.Destroy.assert_called_once()

    ordinary.board.parts[0].SetField("LCSC", "C321")
    retry = CloseEvent()
    ordinary.parent.quit_dialog(retry)
    assert not retry.vetoed and ordinary.parent._closing
    ordinary.parent.Destroy.assert_called_once()
    assert '(property "LCSC" "C321"' in ordinary.path.read_text(encoding="utf-8")
    assert ordinary.backup.exists()
    ordinary.parent.store.read_all_parts.assert_not_called()
    ordinary.wx.GenericMessageDialog.assert_called_once()


@pytest.mark.parametrize("failure", ["capture", "atomic-backup"])
def test_ordinary_export_reports_failure_and_retries_live_board(
    ordinary: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    """A failed close retains a live window; a retry captures new native fields."""
    if failure == "capture":
        failing = patch.object(
            ordinary.board.parts[0],
            "GetFields",
            side_effect=RuntimeError("Native PCB read failed"),
        )
        message = "Native PCB read failed"
    else:
        replace = ordinary.exporter.os.replace

        def replace_unless_backup(source: str, target: str) -> None:
            """Allow the permanent zip but refuse replacing the per-write backup."""
            if str(target).endswith("_old"):
                raise PermissionError("Schematic backup is unwritable")
            replace(source, target)

        failing = patch.object(
            ordinary.exporter.os,
            "replace",
            side_effect=replace_unless_backup,
        )
        message = "Schematic backup is unwritable"
    first = CloseEvent()
    with failing:
        ordinary.parent.quit_dialog(first)
    assert first.vetoed and not ordinary.parent._closing
    assert not ordinary.parent._saving_on_close
    ordinary.parent.Destroy.assert_not_called()
    assert ordinary.path.read_text(encoding="utf-8") == _schematic()
    assert ordinary.backup.exists()
    assert not Path(str(ordinary.path) + "_old").exists()
    ordinary.wx.GenericMessageDialog.assert_called_once()
    _parent, text, title, style = ordinary.wx.GenericMessageDialog.call_args.args
    assert message in text and title == "Schematic save failed"
    assert style & ordinary.wx.ICON_ERROR
    ordinary.dialog.SetYesNoLabels.assert_called_once_with(
        "Close without saving", "Keep open"
    )
    ordinary.dialog.Destroy.assert_called_once()
    assert any(
        record.message == "Automatic schematic save failed"
        and record.exc_info is not None
        for record in caplog.records
    )

    ordinary.board.parts[0].SetField("LCSC", "C321")
    retry = CloseEvent()
    ordinary.parent.quit_dialog(retry)
    assert not retry.vetoed and ordinary.parent._closing
    ordinary.parent.Destroy.assert_called_once()
    result = ordinary.path.read_text(encoding="utf-8")
    assert '(property "LCSC" "C321"' in result
    assert '(property "JLCPCB PartNr" "C321"' in result
    ordinary.parent.store.read_all_parts.assert_not_called()
    ordinary.wx.GenericMessageDialog.assert_called_once()
    ordinary.wx.FileDialog.assert_not_called()
    ordinary.wx.MessageBox.assert_not_called()


@pytest.mark.native_wx
@pytest.mark.parametrize("selected", ["", "A", "B"])
@pytest.mark.parametrize(
    "fields,expected",
    [
        ({"LCSC": "C123", "JLCPCB PartNr": "C123"}, "C123"),
        ({"LCSC": "", "JLCPCB PartNr": ""}, ""),
        ({"LCSC": "C123", "JLCPCB PartNr": "C456"}, "C777"),
        ({}, "C777"),
    ],
    ids=["valid", "explicit-clear", "conflict", "missing"],
)
def test_native_controller_exports_default_provenance_without_cached_rows(
    window_ui: Any,
    selected: str,
    fields: dict[str, str],
    expected: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Native close preserves Default provenance for every selected output."""

    def check(ui: Any) -> None:
        choose_output(ui, selected)
        footprint = ui.board.parts[0]
        footprint.fields = {"Reference": "R1", "Value": "10k", **fields}
        footprint.AddVariant("A").SetFieldValue("LCSC", "C999")
        footprint.AddVariant("B").SetFieldValue("LCSC", "C888")
        ui.controller.refresh()
        before = ui.controller.session.adapter.snapshot()
        remembered = ui.controller.cache.get_output_variant()
        path = ui.path / "board.kicad_sch"
        path.write_text(_schematic(), encoding="utf-8")
        export_module = import_module(
            type(ui.controller).__module__.rsplit(".", 2)[0] + ".schematicexport"
        )
        writer = export_module.SchematicExport
        cached_rows = ui.controller.cache.assembly_rows
        exporting = False

        def begin_export(parent: Any) -> Any:
            nonlocal exporting
            exporting = True
            return writer(parent)

        def rows(snapshot: Any, variant: str) -> Any:
            # Rendering still uses assembly rows for presentation. Once the real
            # writer is constructed, export must retain the native provenance.
            assert not exporting, "Export projected cached assignment rows"
            return cached_rows(snapshot, variant)

        with (
            patch.object(ui.wx.FileDialog, "ShowModal") as picker,
            patch.object(ui.controller.cache, "assembly_rows", rows),
            patch.object(export_module, "SchematicExport", begin_export),
        ):
            assert ui.dialog.Close() is True
        picker.assert_not_called()
        assert exporting
        assert ui.dialog._closing and ui.controller.closed
        result = path.read_text(encoding="utf-8")
        for name in ("LCSC", "JLCPCB PartNr"):
            assert f'(property "{name}" "{expected}"' in result
        assert not ui.messages
        assert ui.controller.session.output_variant == selected
        assert ui.controller.cache.get_output_variant() == remembered
        assert ui.controller.session.adapter.snapshot() == before
        assert footprint.GetVariant("A").GetFieldValue("LCSC") == "C999"
        assert footprint.GetVariant("B").GetFieldValue("LCSC") == "C888"
        assert '(field (name "LCSC") (value "C880"))' in result
        assert '(field (name "LCSC") (value "C881"))' in result
        if expected == "C777":
            assert "R1" in caplog.text and "preserved" in caplog.text.lower()

    window_ui.run(check)
