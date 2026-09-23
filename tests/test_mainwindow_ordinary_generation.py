"""Exercise ordinary export capture and cleanup through the real Generate handler."""

from collections.abc import Callable
import csv
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.test_stock_fabrication_recovery import (
    generation_window,
    mainwindow,
    make_window,
)

__all__ = ["generation_window", "mainwindow", "make_window"]


def prepare_output(context: SimpleNamespace) -> Any:
    """Keep real Store, Fabrication and controller, replacing only native plotting."""
    window = context.window
    window.run_drc_before_gerber_export = MagicMock(return_value=True)
    window.count_order_number_placeholders = MagicMock(return_value=0)
    window.run_generate_hook = MagicMock(return_value=True)
    window.fabrication.generate_geber = MagicMock()
    window.fabrication.generate_excellon = MagicMock()
    window.fabrication.zip_gerber_excellon = MagicMock()
    window.library.insert_lcsc_correction_data("C100", 90, (0, 0))
    window.library.insert_lcsc_correction_data("C200", 180, (0, 0))
    return window


def read_rows(path: str) -> list[dict[str, str]]:
    """Read published CSV independently of the exporter snapshot."""
    with Path(path).open(newline="") as stream:
        return list(csv.DictReader(stream))


def test_pre_hook_native_assignment_change_stops_export_then_retry_is_fresh(
    generation_window: Callable[[str], SimpleNamespace],
) -> None:
    """A modal/hook-time board edit cannot mix earlier CPL with later BOM mapping."""
    context = generation_window("healthy")
    window = prepare_output(context)
    footprint = window.pcbnew.GetBoard().FindFootprintByReference("R1")
    previous = dict.fromkeys(
        window.fabrication.get_artifact_paths().values(), b"previous artifact\n"
    )
    for path, content in previous.items():
        Path(path).write_bytes(content)

    def edit_during_hook(stage: str, _env: dict[str, str], **_kwargs: Any) -> bool:
        """Change actual native assignment fields after placement preflight."""
        if stage == "pre":
            footprint.SetField("LCSC", "C200")
        return True

    window.run_generate_hook = edit_during_hook
    window.generate_fabrication_data()

    window.fabrication.generate_geber.assert_not_called()
    assert window.store.get_generation_count() == 0
    assert {path: Path(path).read_bytes() for path in previous} == previous
    context.message_box.assert_called_once()
    assert "changed during generation" in context.message_box.call_args.args[0]
    assert window.fabrication.output_snapshot is None
    assert not window._ordinary_generating

    context.message_box.reset_mock()
    window.run_generate_hook = MagicMock(return_value=True)
    window.generate_fabrication_data()

    context.message_box.assert_not_called()
    assert window.store.get_generation_count() == 1
    assert read_rows(window.fabrication.get_bom_csv_path())[0]["LCSC"] == "C200"
    assert float(read_rows(window.fabrication.get_cpl_csv_path())[0]["Rotation"]) == 180
    assert window.fabrication.output_snapshot is None
    assert not window._ordinary_generating


@pytest.mark.parametrize("stop", ["drc", "hook-cancel", "hook-error"])
def test_generation_stop_releases_snapshot_before_later_native_assignment(
    generation_window: Callable[[str], SimpleNamespace], stop: str
) -> None:
    """Every early exit lets the next Generate operation observe new board data."""
    context = generation_window("healthy")
    window = prepare_output(context)
    if stop == "drc":
        window.run_drc_before_gerber_export.return_value = False
    elif stop == "hook-cancel":
        window.run_generate_hook.return_value = False
    else:
        window.run_generate_hook.side_effect = RuntimeError("hook boundary failed")

    window.generate_fabrication_data()

    assert window.store.get_generation_count() == 0
    assert window.fabrication.output_snapshot is None
    assert not window._ordinary_generating
    window.fabrication.generate_geber.assert_not_called()
    assert not Path(window.fabrication.get_bom_csv_path()).exists()
    assert not Path(window.fabrication.get_cpl_csv_path()).exists()

    context.message_box.reset_mock()
    window.pcbnew.GetBoard().FindFootprintByReference("R1").SetField("LCSC", "C200")
    window.run_drc_before_gerber_export.return_value = True
    window.run_generate_hook = MagicMock(return_value=True)
    window.generate_fabrication_data()

    context.message_box.assert_not_called()
    assert window.store.get_generation_count() == 1
    assert read_rows(window.fabrication.get_bom_csv_path())[0]["LCSC"] == "C200"
    assert float(read_rows(window.fabrication.get_cpl_csv_path())[0]["Rotation"]) == 180
