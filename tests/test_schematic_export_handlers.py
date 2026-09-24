"""Exercise export event boundaries with real capture and schematic writing."""

from collections.abc import Iterator
from importlib import import_module
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest

from .native_window_support import choose_output, window_ui
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
  )
)
"""


@pytest.fixture
def ordinary(tmp_path: Path) -> Iterator[SimpleNamespace]:
    """Use the real event handler and writer with explicit file-dialog boundaries."""
    board = Board()
    pcbnew = module("pcbnew", GetBuildVersion=lambda: "10.0.6", GetBoard=lambda: board)
    picker = MagicMock()
    picker.__enter__.return_value = picker
    wx = wx_stubs(
        Frame=type("Frame", (), {}),
        NewIdRef=Mock(side_effect=object),
        FileDialog=Mock(return_value=picker),
        MessageBox=Mock(),
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
                "resolve_project_schematics": safety.resolve_project_schematics,
            },
        )
        path = tmp_path / "custom.kicad_sch"
        path.write_text(_schematic(), encoding="utf-8")
        picker.ShowModal.return_value = main.wx.ID_OK
        picker.GetPaths.return_value = [str(path)]
        parent = SimpleNamespace(
            project_path=str(tmp_path),
            board_name="board.kicad_pcb",
            schematic_name=path.name,
            pcbnew=pcbnew,
            logger=logging.getLogger("schematic_export_handler_test"),
            store=SimpleNamespace(
                read_all_parts=Mock(side_effect=AssertionError("Cached parts read"))
            ),
        )
        yield SimpleNamespace(
            export=lambda: main.JLCPCBTools.export_to_schematic(parent),
            board=board,
            path=path,
            parent=parent,
            picker=picker,
            wx=main.wx,
            exporter=exporter,
        )


def test_cancelled_selection_does_not_capture_or_export(
    ordinary: SimpleNamespace,
) -> None:
    """Cancellation leaves existing files untouched without reading board or cache."""
    ordinary.picker.ShowModal.return_value = ordinary.wx.ID_CANCEL
    with patch.object(
        ordinary.board, "GetFootprints", side_effect=AssertionError("Board read")
    ) as read:
        ordinary.export()
    read.assert_not_called()
    ordinary.picker.GetPaths.assert_not_called()
    ordinary.parent.store.read_all_parts.assert_not_called()
    ordinary.wx.MessageBox.assert_not_called()
    assert ordinary.path.read_text(encoding="utf-8") == _schematic()
    assert not Path(str(ordinary.path) + "_old").exists()


@pytest.mark.parametrize("failure", ["capture", "write"])
def test_ordinary_export_reports_failure_and_retries_live_board(
    ordinary: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    """The event boundary reports failures and a retry observes new native fields."""
    if failure == "capture":
        failing = patch.object(
            ordinary.board.parts[0],
            "GetFields",
            side_effect=RuntimeError("Native PCB read failed"),
        )
        message = "Native PCB read failed"
    else:
        failing = patch.object(
            ordinary.exporter.os,
            "replace",
            side_effect=PermissionError("Schematic backup is unwritable"),
        )
        message = "Schematic backup is unwritable"
    with failing:
        ordinary.export()
    assert ordinary.path.read_text(encoding="utf-8") == _schematic()
    ordinary.wx.MessageBox.assert_called_once()
    text, title = ordinary.wx.MessageBox.call_args.args
    style = ordinary.wx.MessageBox.call_args.kwargs["style"]
    assert message in text and title == "Schematic Export Error"
    assert style & ordinary.wx.ICON_ERROR
    assert any(
        record.message == "Schematic export failed" and record.exc_info is not None
        for record in caplog.records
    )

    ordinary.board.parts[0].SetField("LCSC", "C321")
    ordinary.export()
    result = ordinary.path.read_text(encoding="utf-8")
    assert '(property "LCSC" "C321"' in result
    assert '(property "JLCPCB PartNr" "C321"' in result
    ordinary.parent.store.read_all_parts.assert_not_called()
    assert ordinary.wx.MessageBox.call_count == 1


@pytest.mark.native_wx
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
    fields: dict[str, str],
    expected: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Real constructors and controls retain clear/conflict provenance at export."""

    def check(ui: Any) -> None:
        choose_output(ui, "")
        footprint = ui.board.parts[0]
        footprint.fields = {"Reference": "R1", "Value": "10k", **fields}
        footprint.AddVariant("A").SetFieldValue("LCSC", "C999")
        ui.controller.refresh()
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
            patch.object(ui.wx.FileDialog, "GetPaths", return_value=[str(path)]),
            patch.object(ui.wx.FileDialog, "ShowModal", lambda _dialog: ui.wx.ID_OK),
            patch.object(ui.controller.cache, "assembly_rows", rows),
            patch.object(export_module, "SchematicExport", begin_export),
        ):
            ui.dialog.export_to_schematic()
        assert exporting
        result = path.read_text(encoding="utf-8")
        for name in ("LCSC", "JLCPCB PartNr"):
            assert f'(property "{name}" "{expected}"' in result
        assert not ui.messages
        assert ui.controller.session.output_variant == ""
        assert footprint.GetVariant("A").GetFieldValue("LCSC") == "C999"
        if expected == "C777":
            assert "R1" in caplog.text and "preserved" in caplog.text.lower()

    window_ui.run(check)
