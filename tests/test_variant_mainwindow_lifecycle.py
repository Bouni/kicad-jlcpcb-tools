"""Modeless main-frame lifetime through actual constructors and native controls."""

from importlib import import_module
import sqlite3
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pytest

from .native_window_support import (
    _SelectableFootprint,
    activate_popup,
    choose_output as output,
    focus,
    modal_handler,
    window_ui,
)
from .native_wx_support import pump, wait_until

__all__ = ["window_ui"]
pytestmark = pytest.mark.native_wx


@pytest.mark.parametrize("change", ["save_as", "replacement"])
@pytest.mark.parametrize("opened", [False, True])
def test_details_reports_changed_board_without_opening_dialog(
    window_ui: Any, monkeypatch: pytest.MonkeyPatch, change: str, opened: bool
) -> None:
    """Details cannot inspect a stale captured board after its live identity changes."""
    from .variant_native_support import Board

    def check(ui: Any) -> None:
        if opened:
            ui.dialog.show_assembly_mode_details()
            previous = ui.dialog._why_standard_dialog
            assert previous and previous.IsShown()
        if change == "save_as":
            monkeypatch.setattr(
                ui.board, "GetFileName", lambda: str(ui.path / "renamed.kicad_pcb")
            )
        else:
            ui.dialog.pcbnew.board = Board()
        ui.dialog.show_assembly_mode_details()
        assert ui.dialog._why_standard_dialog is None
        assert len(ui.messages) == 1
        assert "Reopen JLCPCB Tools" in ui.messages[0]
        assert not ui.controller.view._mutations_enabled
        assert not ui.dialog.generate_button.IsEnabled()
        if opened:
            wait_until(ui.wx, lambda: not previous)

    window_ui.run(check)


def value(ui: Any, field: str = "stock", variant: str = "A") -> Any:
    """Read the published row independently from supplier caches."""
    model = ui.controller.model
    return model.get_value(
        model.row_for_component("component-1"), model.column_for(variant, field)
    )


def download(ui: Any, *, succeeded: bool = True) -> Any:
    """Capture source identity before dispatching a possibly delayed completion."""
    catalog = ui.catalog
    return SimpleNamespace(
        library=catalog,
        source=(catalog.selected_library, catalog.partsdb_file),
        attempt=catalog.download_attempt,
        succeeded=succeeded,
    )


@pytest.mark.parametrize(
    "transition", ["download", "store_retry", "selected_library", "data_path"]
)
def test_catalog_publication_preserves_session(window_ui: Any, transition: str) -> None:
    """Publish catalog changes without querying on hover or taking ordinary paths."""

    def check(ui: Any) -> None:
        c, d = ui.controller, ui.dialog
        ui.board.parts[0].AddVariant("A").SetDNP(True)
        ui.catalog.parts["C1"] = {"stock": 10, "description": "Original resistor"}
        c.refresh()
        token = c.session.snapshot.source_token
        stable = (c.session, c.view, c.panel, c.timer, c.cache, d.library)
        target = focus(ui, "B")
        assert target.variant == "B" and value(ui) == 10
        description = "Updated resistor ±1%\n" + "Full text. " * 30
        ui.catalog.parts["C1"].update(stock=90, description=description)
        with (
            patch.object(
                ui.mainwindow,
                "stock_concern_references",
                side_effect=AssertionError("Ordinary stock path used"),
            ),
            patch.object(
                d,
                "_fill_empty_lcsc_assignments_from_part_preferences",
                side_effect=AssertionError("Ordinary preference path used"),
            ),
        ):
            d.recompute_stock_concerns()
            if transition == "download":
                ui.catalog.stock = 90
                d.download_completed(download(ui))
            elif transition == "store_retry":
                ui.board.parts[0].GetVariant("A").SetFieldValue("LCSC", "C777")
                d.init_store()
            else:
                d.update_settings(
                    SimpleNamespace(section="library", setting=transition, value="new")
                )
                assert ui.catalog.config_refreshes == 1
                assert d.settings["library"][transition] == "new"
        assert d._variant_controller is c
        assert (c.session, c.view, c.panel, c.timer, c.cache, d.library) == stable
        assert c.view.selected_target == target
        assert c.timer.IsRunning() and c.session.output_variant == "A"
        assert len(ui.created_libraries) == 1
        assert not d._part_preferences_applied_on_open
        assert not c.session.snapshot.get("component-1", "A").pop
        assert c.session.snapshot.get("component-1", "").pop
        if transition == "store_retry":
            assert value(ui, "lcsc") == "C777"
        else:
            assert value(ui) == value(ui, variant="B") == 90
            assert c.session.adapter.snapshot().source_token == token
        with patch.object(
            ui.catalog,
            "get_part_details",
            side_effect=AssertionError("Hover queried catalog"),
        ):
            for name in ("", "B"):
                tooltip = c.model.cell_tooltip(0, c.model.column_for(name, "lcsc"))
                assert description in tooltip and "Original resistor" not in tooltip
        assert not ui.board.modified and not ui.messages

    window_ui.run(check)


@pytest.mark.parametrize("source", ["catalog", "supplier"])
def test_supply_publication_during_generation_is_deferred_until_release(
    window_ui: Any, source: str
) -> None:
    """Both asynchronous supply sources retain the frozen export until release."""

    def check(ui: Any) -> None:
        c = ui.controller
        if source == "supplier":
            ui.supplier.fetch_iter.return_value = iter(
                [
                    (
                        "C1",
                        {
                            "assembly_process": "SMT",
                            "component_product_type": 2,
                        },
                    )
                ]
            )
            ui.run_worker()
        c.begin_generation(())
        captured = (
            c.session._generation_snapshot,
            c.model,
            ui.dialog.bom_estimator_decision,
        )
        if source == "catalog":
            ui.catalog.stock = 90
            ui.dialog.download_completed(download(ui))
        else:
            drained: list[bool] = []
            ui.wx.CallAfter(drained.append, True)
            wait_until(ui.wx, lambda: bool(drained))
            assert not c.assembly_lookup.pending
            assert (
                ui.cache.assembly_rows(c.session.snapshot, "A")[0][
                    "component_product_type"
                ]
                == 2
            )
        assert (
            c.session._generation_snapshot,
            c.model,
            ui.dialog.bom_estimator_decision,
        ) == captured
        assert c.session.generating and not c.view._mutations_enabled
        assert value(ui) == 10 and len(ui.created_libraries) == 1
        c.end_generation()
        assert not c.session.generating and c.view._mutations_enabled
        assert (
            (value(ui) == 90)
            if source == "catalog"
            else (
                value(ui, "standard") is True
                and ui.dialog.bom_estimator_decision.board_standard
            )
        )
        assert not ui.messages

    window_ui.run(check)


@pytest.mark.parametrize(
    "transition", ["missing_source", "failed_download", "invalid_replacement"]
)
def test_unavailable_catalog_clears_variant_supply_then_recovers(
    window_ui: Any, transition: str
) -> None:
    def check(ui: Any) -> None:
        c, d, catalog = ui.controller, ui.dialog, ui.catalog
        native = c.session.snapshot.components
        catalog.reads.clear()
        if transition == "missing_source":
            d.update_settings(
                SimpleNamespace(section="library", setting="data_path", value="missing")
            )
        else:
            catalog.usable = False
            if transition == "failed_download":
                catalog.state = ui.mainwindow.LibraryState.UPDATE_NEEDED
                d.download_finished(download(ui, succeeded=False))
            else:
                d.download_completed(download(ui))
        assert not d.is_catalog_available() and not catalog.reads
        assert d._variant_controller is c and c.session.snapshot.components == native
        for variant in ("", "A", "B"):
            assert value(ui, variant=variant) is None
            assert value(ui, "price", variant) is None
            assert not value(ui, "type", variant)
        assert c.session.reliable and not d._part_preferences_applied_on_open
        catalog.usable, catalog.stock = True, 91
        catalog.state = ui.mainwindow.LibraryState.INITIALIZED
        catalog.download_attempt += 1
        d.download_completed(download(ui))
        assert d.is_catalog_available() and value(ui) == 91
        assert c.session.reliable and not d._project_storage_unavailable
        assert len(ui.created_libraries) == 1 and not ui.messages

    window_ui.run(check)


@pytest.mark.parametrize("identity", ["extraction", "library", "source", "attempt"])
def test_unfinished_or_stale_catalog_cannot_replace_published_rows(
    window_ui: Any, identity: str
) -> None:
    """Only completed work for the current catalog may publish its rows."""

    def check(ui: Any) -> None:
        model = ui.controller.model
        ui.catalog.stock = 90
        if identity == "extraction":
            ui.dialog.gauge.SetValue(41)
            ui.dialog.unzip_extracting_completed()
            assert ui.dialog.gauge.GetValue() == 0
        else:
            event = download(ui, succeeded=False)
            setattr(
                event,
                identity,
                {
                    "library": object(),
                    "source": ("earlier", "earlier/parts.db"),
                    "attempt": event.attempt - 1,
                }[identity],
            )
            ui.dialog.download_completed(event)
            ui.dialog.download_finished(event)
        assert ui.controller.model is model and value(ui) == 10
        assert len(ui.created_libraries) == 1

    window_ui.run(check)


def test_catalog_settings_wait_for_active_worker_before_reusing_provider(
    window_ui: Any,
) -> None:
    def check(ui: Any) -> None:
        d, catalog = ui.dialog, ui.catalog
        catalog.download_running = True
        completion = download(ui)
        d.update_settings(
            SimpleNamespace(section="library", setting="selected_library", value="new")
        )
        assert d._catalog_switch_pending and catalog.config_refreshes == 0
        assert catalog.selected_library == "old" and value(ui) is None
        catalog.download_running = False
        d.download_completed(completion)
        assert catalog.config_refreshes == 0 and value(ui) is None
        d.download_finished(completion)
        assert not d._catalog_switch_pending and catalog.config_refreshes == 1
        assert catalog.selected_library == "new" and value(ui) == 90
        assert d.library is catalog and len(ui.created_libraries) == 1
        assert not ui.messages

    window_ui.run(check)


def test_open_assembly_details_follow_output_population_and_board_count(
    window_ui: Any,
) -> None:
    def check(ui: Any) -> None:
        ui.dialog.show_assembly_mode_details()
        details = ui.dialog._why_standard_dialog
        assert details.GetParent() is ui.dialog and details.IsShown()
        with patch.object(
            details, "update_content", wraps=details.update_content
        ) as update:
            ui.controller.render()
            assert {part["variant_name"] for part in update.call_args.args[1]} == {"A"}
            output(ui, "B")
            target = focus(ui, "B", "pop")
            ui.controller._on_edit(target, False)
            assert update.call_args.args[1][0]["is_dnp"]
            ui.dialog._set_bom_estimator_board_count(17)
            decision, parts = update.call_args.args
            assert decision.board_count == 17
            assert {part["variant_name"] for part in parts} == {"B"}
            assert details._wrapped_labels and not ui.messages

    window_ui.run(check)


def test_removed_output_closes_its_open_assembly_details(window_ui: Any) -> None:
    def check(ui: Any) -> None:
        output(ui, "B")
        ui.dialog.show_assembly_mode_details()
        details = ui.dialog._why_standard_dialog
        ui.board.names.remove("B")
        ui.controller.refresh()
        assert ui.dialog._why_standard_dialog is None
        wait_until(ui.wx, lambda: not details)
        assert ui.controller.session.output_variant == "B"
        assert not ui.dialog.generate_button.IsEnabled()
        assert ui.dialog.bom_estimator_decision is None
        assert (
            ui.controller.session.reliable and ui.controller.view.output_variant is None
        )
        assert ui.controller.output_choice.GetSelection() == ui.wx.NOT_FOUND
        output(ui, "")
        assert (
            ui.controller.session.output_variant
            == ui.controller.view.output_variant
            == ""
        )
        assert ui.controller.output_choice.GetSelection() == 0
        assert ui.dialog.generate_button.IsEnabled() and not ui.messages

    window_ui.run(check)


@pytest.mark.parametrize(
    ("setting", "changed"),
    [("bom_estimator_force_standard", True), ("bom_estimator_board_count", 17)],
)
def test_parent_estimate_change_reports_preparation_failure_and_recovers(
    window_ui: Any, setting: str, changed: Any
) -> None:
    def check(ui: Any) -> None:
        c, d = ui.controller, ui.dialog
        target = focus(ui, "B")
        previous = (c.model, c.session.snapshot)
        setattr(d, setting, changed)
        with patch.object(
            ui.catalog,
            "read_correction_data",
            side_effect=sqlite3.DatabaseError("preparation read failed"),
        ):
            d.recompute_bom_estimate()
        assert len(ui.messages) == 1 and "preparation read failed" in ui.messages[0]
        assert (c.model, c.session.snapshot) == previous
        assert not c.session.reliable and not c.view._mutations_enabled
        assert not c.output_choice.IsEnabled() and not d.generate_button.IsEnabled()
        assert not d.right_toolbar.IsEnabled()
        c.refresh()
        assert c.session.reliable and c.view._mutations_enabled
        assert c.output_choice.IsEnabled() and d.generate_button.IsEnabled()
        assert d.right_toolbar.IsEnabled() and c.session.output_variant == "A"
        assert c.view.selected_target == target and getattr(d, setting) == changed
        assert len(ui.messages) == 1

    window_ui.run(check)


def test_restored_empty_filters_can_be_cleared_without_a_component_target(
    window_ui: Any,
) -> None:
    def save(ui: Any) -> None:
        ui.board.parts[0].attributes = 8 | 4
        ui.dialog.OnBomHide()
        ui.dialog.OnPosHide()
        assert not ui.controller.model.rows

    def reopen(ui: Any) -> None:
        d, c = ui.dialog, ui.controller
        assert (
            not c.model.rows
            and d.right_toolbar.GetToolState(13)
            and d.right_toolbar.GetToolState(14)
        )
        assert d.right_toolbar.GetToolEnabled(13) and d.right_toolbar.GetToolEnabled(14)
        assert not d.right_toolbar.GetToolEnabled(ui.mainwindow.ID_SELECT_PART)
        d.OnBomHide()
        assert not d.hide_bom_parts and not c.model.rows
        d.OnPosHide()
        assert not d.hide_pos_parts and len(c.model.rows) == 1
        assert c.session.output_variant == "A" and not ui.messages

    window_ui.run(save, reopen)


@pytest.mark.parametrize("failure", [None, "capture", "details"])
def test_modeless_teardown_closes_children_even_when_cleanup_fails(
    window_ui: Any, failure: Any
) -> None:
    """Duplicate closes and child cleanup errors still release the actual frame."""

    def check(ui: Any) -> None:
        d, c = ui.dialog, ui.controller
        focus(ui, "B")
        c.select_part()
        assert d._part_selector is not None
        d.show_assembly_mode_details()
        details = d._why_standard_dialog
        owner, method = (
            (c.view, "capture_preferences")
            if failure == "capture"
            else (details, "Close")
        )
        with patch.object(
            owner,
            method,
            side_effect=RuntimeError("cleanup failed") if failure else None,
            wraps=None if failure else getattr(owner, method),
        ) as operation:
            if failure:
                with pytest.raises(RuntimeError, match="cleanup failed"):
                    d.quit_dialog()
            else:
                d.quit_dialog()
            assert c.closed and not c.timer.IsRunning()
            assert d._part_selector is None
            d.quit_dialog()
            operation.assert_called_once_with()
        wait_until(ui.wx, lambda: not d)
        assert not details

    window_ui.run(check)


def test_parent_close_waits_for_modal_child_then_closes_and_reopens(
    window_ui: Any,
) -> None:
    """A programmatic close cannot destroy controls needed by a real modal return."""

    def check(ui: Any) -> None:
        frame, controller, wx = ui.dialog, ui.controller, ui.wx
        focus(ui, "A")
        controller.select_part()
        selector = frame._part_selector

        def close_parent(manager: Any) -> None:
            assert manager.IsModal() and manager.GetParent() is frame
            frame.Close()
            frame.quit_dialog()
            pump(wx)
            assert not frame.IsBeingDeleted() and not controller.closed
            assert controller.timer.IsRunning() and frame._part_selector is selector
            assert manager.IsModal() and not manager.IsBeingDeleted()

        with modal_handler(
            ui, ui.module.CorrectionManagerDialog, close_parent
        ) as managers:
            controller.dispatch_action("correction", focus(ui, None, "correction"))
        assert len(managers) == 1
        wait_until(wx, lambda: not managers[0])
        assert frame.IsEnabled() and selector.IsEnabled()
        frame.Close()
        wait_until(wx, lambda: not frame)
        assert not selector and controller.closed and not controller.timer.IsRunning()
        assert not ui.messages

    window_ui.run(check, check)


@pytest.mark.parametrize("failure", [OSError, RuntimeError])
def test_generation_cleanup_failure_releases_controls_and_allows_retry(
    window_ui: Any, caplog: pytest.LogCaptureFixture, failure: type[Exception]
) -> None:
    def check(ui: Any) -> None:
        c, d = ui.controller, ui.dialog
        target = focus(ui, "B")
        c.begin_generation(())
        assert c.session.generating and not c.view._mutations_enabled
        working = d.fabrication._generation.directory
        with (
            patch.object(
                working,
                "cleanup",
                side_effect=failure("temporary cleanup failed"),
            ) as cleanup,
        ):
            if failure is RuntimeError:
                with pytest.raises(RuntimeError, match="temporary cleanup failed"):
                    c.end_generation()
            else:
                c.end_generation()
            cleanup.assert_called_once_with()
        if failure is OSError:
            assert working.name in caplog.text
        working.cleanup()
        assert not c.session.generating and c.session.reliable
        assert c.view._mutations_enabled and c.output_choice.IsEnabled()
        assert d.generate_button.IsEnabled() and d.right_toolbar.IsEnabled()
        assert c.session.output_variant == "A" and c.view.selected_target == target
        c.begin_generation(())
        c.end_generation()
        assert not c.session.generating and d.generate_button.IsEnabled()
        assert not ui.messages

    window_ui.run(check)


@pytest.mark.parametrize("action", ["copy_part_lcsc", "paste_part_lcsc"])
def test_main_clipboard_actions_report_busy_clipboard_and_recover(
    window_ui: Any, action: str
) -> None:
    def check(ui: Any) -> None:
        focus(ui, "B")
        before = ui.controller.session.adapter.snapshot()
        with patch.object(ui.wx.TheClipboard, "Open", return_value=False):
            getattr(ui.dialog, action)()
        assert len(ui.messages) == 1 and "clipboard is busy" in ui.messages[0]
        assert ui.controller.session.adapter.snapshot() == before
        assert ui.controller.session.output_variant == "A"
        ui.controller.refresh()
        assert ui.wx.TheClipboard.Open()
        try:
            assert ui.wx.TheClipboard.SetData(ui.wx.TextDataObject("C321"))
        finally:
            ui.wx.TheClipboard.Close()
        getattr(ui.dialog, action)()
        if action == "copy_part_lcsc":
            assert ui.wx.TheClipboard.Open()
            try:
                text = ui.wx.TextDataObject()
                assert ui.wx.TheClipboard.GetData(text) and text.GetText() == "C1"
            finally:
                ui.wx.TheClipboard.Close()
        else:
            assert value(ui, "lcsc", "B") == "C321" and value(ui, "lcsc", "A") == "C1"
        assert len(ui.messages) == 1

    window_ui.run(check)


@pytest.mark.parametrize("outcome", ["cancel", "native_failure"])
def test_schematic_file_selection_cancellation_and_native_failure_preserve_output(
    window_ui: Any, outcome: str
) -> None:
    def check(ui: Any) -> None:
        output(ui, "")
        path = ui.path / "custom.kicad_sch"
        path.write_bytes(b"previous schematic")
        exporter = Mock()
        export_module = import_module(
            type(ui.controller).__module__.rsplit(".", 2)[0] + ".schematicexport"
        )
        read = Mock(wraps=ui.controller.session.adapter.snapshot)

        def choose(_dialog: Any) -> int:
            read.side_effect = RuntimeError("PCB became unavailable during selection")
            return ui.wx.ID_OK

        with (
            patch.object(export_module, "SchematicExport", exporter),
            patch.object(ui.wx.FileDialog, "GetPaths", return_value=[str(path)]),
            patch.object(
                ui.wx.FileDialog,
                "ShowModal",
                choose
                if outcome == "native_failure"
                else lambda _dialog: ui.wx.ID_CANCEL,
            ),
        ):
            with patch.object(ui.controller.session.adapter, "snapshot", read):
                ui.dialog.export_to_schematic()
            exporter.assert_not_called()
            assert path.read_bytes() == b"previous schematic"
            if outcome == "cancel":
                read.assert_not_called()
                assert not ui.messages
            else:
                assert ui.messages == ["PCB became unavailable during selection"]
                assert (
                    not ui.dialog.generate_button.IsEnabled()
                    and not ui.controller.view._mutations_enabled
                )
            ui.controller.refresh()
            assert (
                ui.controller.session.reliable and ui.dialog.generate_button.IsEnabled()
            )
            with patch.object(
                ui.wx.FileDialog, "ShowModal", lambda _dialog: ui.wx.ID_OK
            ):
                ui.dialog.export_to_schematic()
            args = exporter.return_value.load_schematic.call_args
            assert args.args == ([str(path)],)
            assert args.kwargs["variant_name"] == ""
            snapshot = args.kwargs["snapshot"]
            assert snapshot.assignments == {"R1": "C1"}
            assert snapshot.bom_parts == (
                {"reference": "R1", "exclude_from_bom": False},
            )
            assert ui.controller.session.output_variant == ""

    window_ui.run(check)


@pytest.mark.os_input
def test_native_popup_copy_paste_work_before_and_after_a_modal(window_ui: Any) -> None:
    """Real popup input copies complete rows, including after a child modal returns."""
    components = ("component-1", "component-2")
    window_ui.board.parts.append(
        _SelectableFootprint(window_ui.board, components[1], "R2")
    )
    expected = (
        {"value": "22k", "lcsc": "C321", "bom": False, "pos": True, "pop": False},
        {"value": "47k", "lcsc": "C654", "bom": True, "pos": False, "pop": True},
    )
    for part, settings in zip(window_ui.board.parts, expected):
        source = part.AddVariant("A")
        source.SetFieldValue("Value", settings["value"])
        source.SetFieldValue("LCSC", settings["lcsc"])
        source.SetExcludedFromBOM(not settings["bom"])
        source.SetExcludedFromPosFiles(not settings["pos"])
        source.SetDNP(not settings["pop"])

    def check(ui: Any) -> None:
        controller = ui.controller
        before = controller.session.snapshot.for_variant("")
        for after_modal in (False, True):
            if after_modal:
                with modal_handler(
                    ui,
                    ui.module.CorrectionManagerDialog,
                    lambda manager: manager.quit_dialog(),
                ):
                    controller.dispatch_action(
                        "correction", focus(ui, None, "correction")
                    )
            controller._on_edit(focus(ui, "B"), "C999")
            previous_nonce = controller._clipboard_nonce
            controller.view.select_components(components, "A")
            activate_popup(ui, "A", "lcsc", "Copy")
            assert controller._clipboard_nonce != previous_nonce, (
                "Native Copy remained disabled: keyboard activation copied nothing"
            )
            assert (
                tuple(dict(row.values) for row in controller._clipboard.rows)
                == expected
            )
            controller.view.select_components(components, "B")
            activate_popup(ui, "B", "price", "Paste")
            snapshot = controller.session.adapter.snapshot()
            for component, settings in zip(components, expected):
                destination = snapshot.get(component, "B")
                assert {
                    field: getattr(destination, field) for field in settings
                } == settings
            assert snapshot.for_variant("") == before
        assert not ui.messages

    window_ui.run(check)
