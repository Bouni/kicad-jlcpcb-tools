"""Exercise assignment and fabrication after native-window catalog recovery."""

from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from zipfile import ZipFile

import pytest

from .native_window_support import (
    button,
    choose_output,
    focus,
    select_result,
    window_ui,
)
from .native_wx_support import wait_until
from .test_variant_mainwindow_lifecycle import download

__all__ = ["window_ui"]
pytestmark = pytest.mark.native_wx


def _fail_catalog(ui: Any, failure: str) -> None:
    """Deliver an invalid replacement or a real settings-change event failure."""
    if failure == "replacement":
        ui.catalog.usable = False
        ui.dialog.download_completed(download(ui))
    else:
        with patch.object(
            ui.catalog, "refresh_library_config", side_effect=OSError("catalog read")
        ):
            ui.dialog.update_settings(
                SimpleNamespace(section="library", setting="data_path", value="new")
            )


def _recover_catalog(ui: Any) -> None:
    """Publish a healthy source through its actual completion handler."""
    ui.catalog.usable = True
    ui.catalog.state = ui.mainwindow.LibraryState.INITIALIZED
    ui.catalog.download_attempt += 1
    ui.dialog.download_completed(download(ui))


@pytest.mark.parametrize("failure", ["replacement", "settings"])
def test_catalog_retry_restores_assignment_and_generation(
    window_ui: Any, failure: str
) -> None:
    """Repeated recovery keeps the same store and permits complete user operations."""

    def check(ui: Any) -> None:
        dialog, controller = ui.dialog, ui.controller
        ui.board.Drawings = lambda: ()
        exporter = dialog.fabrication

        def plot(name: str, *_args: Any) -> None:
            (Path(exporter.gerberdir) / name).write_bytes(b"recovered output\n")

        exporter.generate_geber = partial(plot, "copper.gbr")
        exporter.generate_excellon = partial(plot, "drill.drl")
        exporter.fill_zones = lambda: []
        dialog.settings["gerber"]["force_drc"] = False
        dialog.settings["general"]["order_number"] = False
        choose_output(ui, "A")
        for count in (1, 2):
            _fail_catalog(ui, failure)
            assert dialog._project_storage_unavailable
            assert dialog.store is None
            _recover_catalog(ui)
            assert dialog.is_catalog_available()
            assert dialog.store is controller.cache
            assert not dialog._project_storage_unavailable

            focus(ui, "A")
            dialog.select_part()
            selector = dialog._part_selector
            select_result(ui, selector)
            button(ui.wx, selector.select_part_button)
            wait_until(
                ui.wx,
                lambda: controller.session.snapshot.get("component-1", "A").lcsc
                == "C321",
            )
            assert dialog._part_selector is None
            assert (
                controller.session.adapter.snapshot().get("component-1", "A").lcsc
                == "C321"
            )

            dialog.generate_fabrication_data()
            paths = exporter.get_artifact_paths()
            assert "C321" in Path(paths["bom_csv"]).read_text()
            assert "R1" in Path(paths["cpl_csv"]).read_text()
            with ZipFile(paths["gerber_zip"]) as archive:
                assert archive.read("copper.gbr") == b"recovered output\n"
            assert controller.cache.get_generation_count() == count
            assert dialog.generate_button.IsEnabled()
            assert not controller.session.generating
            assert not ui.messages

    window_ui.run(check)


@pytest.mark.parametrize("failure", ["replacement", "settings"])
def test_catalog_retry_does_not_restore_a_replaced_board(
    window_ui: Any, failure: str
) -> None:
    """Catalog readiness cannot re-enable an expired native board session."""
    from .variant_native_support import Board

    def check(ui: Any) -> None:
        _fail_catalog(ui, failure)
        ui.dialog.pcbnew.board = Board()
        _recover_catalog(ui)
        assert ui.dialog.store is None
        assert ui.dialog._project_storage_unavailable
        assert not ui.controller.session.reliable
        assert not ui.dialog.generate_button.IsEnabled()
        assert not ui.dialog._can_apply_user_assignments()

    window_ui.run(check)
