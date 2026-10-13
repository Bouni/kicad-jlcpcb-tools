"""The real window opens one review pane and generates only when asked."""

from functools import partial
from pathlib import Path
import time
from typing import Any
from zipfile import ZipFile

import pytest

from .native_window_support import (
    _SelectableFootprint,
    _SelectedItem,
    button,
    modal_handler,
    window_ui,
)
from .native_wx_support import wait_until

__all__ = ["window_ui"]
pytestmark = pytest.mark.native_wx

MODES = ["variant", "ordinary"]


def _board(window_ui: Any, mode: str) -> None:
    """Give the board a second part, and no variants for the ordinary table."""
    window_ui.board.parts.append(
        _SelectableFootprint(window_ui.board, "component-2", "R2")
    )
    if mode == "ordinary":
        window_ui.board.names = []
        window_ui.board.current = ""


def _prepare(ui: Any) -> Any:
    """Find an empty zone and a value mismatch; replace only native plotting."""
    dialog = ui.dialog
    ui.board.Drawings = lambda: ()
    if ui.controller is not None:
        ui.board.parts[0].AddVariant("A").SetFieldValue("LCSC", "C999")
        ui.controller.refresh()
    exporter = dialog.fabrication

    def plot(name: str, *_args: Any) -> None:
        (Path(exporter.gerberdir) / name).write_bytes(b"output " + name.encode())

    exporter.generate_geber = partial(plot, "copper.gbr")
    exporter.generate_excellon = partial(plot, "drill.drl")
    exporter.fill_zones = lambda: ["GND on B.Cu"]
    exporter.get_part_consistency_warnings = lambda: {
        "C999": {"10k": ["R1"], "22k": ["R2"]}
    }
    dialog.settings["gerber"]["force_drc"] = False
    dialog.settings["general"]["order_number"] = False
    return exporter


def _selected(ui: Any) -> set[str]:
    """Return the references of the footprints selected on the board."""
    return {part.GetReference() for part in ui.board.parts if part.IsSelected()}


def _opened(ui: Any, pane: Any) -> None:
    """Let the shown pane settle, so focus and first paint have happened."""
    wait_until(ui.wx, pane.IsShownOnScreen, reason="The pane was not shown")
    settled = time.monotonic() + 0.3
    wait_until(ui.wx, lambda: time.monotonic() >= settled)


def _select(ui: Any, pane: Any, row: int) -> None:
    """Select a row and deliver the selection event the user's click sends."""
    control = pane.findings_list
    control.SelectRow(row)
    event = ui.wx.dataview.DataViewEvent(
        ui.wx.dataview.wxEVT_DATAVIEW_SELECTION_CHANGED, control, control.RowToItem(row)
    )
    control.GetEventHandler().ProcessEvent(event)


def _unexpected(pane: Any) -> None:
    """Fail a run that should have generated without opening the pane."""
    raise AssertionError("A board without findings opened the review pane")


def _published(exporter: Any) -> dict[str, Any]:
    """Read the published artifacts: ZIP members by name, CSVs as bytes."""
    paths = exporter.get_artifact_paths()
    with ZipFile(paths["gerber_zip"]) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    return {
        "gerber_zip": members,
        "bom_csv": Path(paths["bom_csv"]).read_bytes(),
        "cpl_csv": Path(paths["cpl_csv"]).read_bytes(),
    }


@pytest.mark.parametrize("mode", MODES)
def test_choosing_a_row_shows_its_detail_and_cancel_writes_nothing(
    window_ui: Any, mode: str
) -> None:
    """The pane shows a finding's detail and leaves the board selection alone."""
    _board(window_ui, mode)

    def check(ui: Any) -> None:
        exporter = _prepare(ui)
        pcbnew = ui.dialog.pcbnew
        track = _SelectedItem()
        track.SetSelected()
        pcbnew.other_items.append(track)
        ui.board.parts[1].SetSelected()
        refreshes = pcbnew.Refresh.call_count
        seen: list[tuple[Any, ...]] = []

        def interact(pane: Any) -> None:
            _opened(ui, pane)
            assert pane.findings_list.GetTextValue(3, 1) == "C999: 10k on R1; 22k on R2"
            _select(ui, pane, 3)
            seen.append(
                (
                    pane.detail.GetValue(),
                    _selected(ui),
                    track.IsSelected(),
                    pcbnew.Refresh.call_count - refreshes,
                )
            )
            button(ui.wx, pane.cancel_button)

        with modal_handler(ui, ui.mainwindow.GenerationReviewDialog, interact) as shown:
            ui.dialog.generate_fabrication_data()

        assert len(shown) == 1
        ((detail, selected, track_selected, redraws),) = seen
        assert detail.endswith("10k: R1\n22k: R2\n\nParts: R1, R2")
        assert (selected, track_selected, redraws) == ({"R2"}, True, 0)
        paths = exporter.get_artifact_paths().values()
        assert not any(Path(path).exists() for path in paths)
        assert exporter.output_snapshot is None
        assert ui.cache.get_generation_count() == 0
        assert ui.dialog.generate_button.IsEnabled()
        if ui.controller is not None:
            assert not ui.controller.session.generating
        assert not ui.messages

    window_ui.run(check)


@pytest.mark.parametrize("mode", MODES)
def test_generate_anyway_publishes_what_a_clean_board_publishes(
    window_ui: Any, mode: str
) -> None:
    """Generate anyway writes the same artifacts as a board with no findings."""
    _board(window_ui, mode)

    def check(ui: Any) -> None:
        exporter = _prepare(ui)
        find_zones, find_values = (
            exporter.fill_zones,
            exporter.get_part_consistency_warnings,
        )
        exporter.fill_zones = list
        exporter.get_part_consistency_warnings = dict
        with modal_handler(
            ui, ui.mainwindow.GenerationReviewDialog, _unexpected
        ) as shown:
            ui.dialog.generate_fabrication_data()
        assert shown == []
        clean = _published(exporter)
        exporter.fill_zones = find_zones
        exporter.get_part_consistency_warnings = find_values

        def generate_anyway(pane: Any) -> None:
            _opened(ui, pane)
            button(ui.wx, pane.generate_button)

        with modal_handler(
            ui, ui.mainwindow.GenerationReviewDialog, generate_anyway
        ) as shown:
            ui.dialog.generate_fabrication_data()

        assert len(shown) == 1
        assert _published(exporter) == clean
        assert ui.cache.get_generation_count() == 2
        assert exporter.output_snapshot is None
        assert ui.dialog.generate_button.IsEnabled()
        assert not ui.messages

    window_ui.run(check)
