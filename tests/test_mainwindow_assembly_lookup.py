"""Ordinary assembly lookups follow live assignments through real wx delivery."""

from collections.abc import Iterator
from importlib import import_module
import sqlite3
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from .native_window_support import _SelectableFootprint, window_ui
from .native_wx_support import pump

__all__ = ["window_ui"]
pytestmark = pytest.mark.native_wx


def assert_assembly_feedback(
    frame: Any, reference: str, glyph: str, *, pending: bool, process: str = ""
) -> None:
    """Read classification and help from the model attached to the native control."""
    model = frame.partlist_data_model
    item = model.ObjectToItem(model.data[model.find_index(reference)])
    assert model.GetValue(item, model.columns["STANDARD_ONLY_COL"]) == glyph
    status = (
        "Pending" if pending else "Done" if glyph in ("✓", "—") else "Class missing"
    )
    assert model.GetValue(item, model.columns["ENRICH_COL"]) == status
    help_text = model.get_assembly_tooltip(item)
    assert ("Retrieving assembly information." in help_text) is pending
    assert f"Assembly process: {process or 'unavailable'}" in help_text


@pytest.mark.parametrize(
    "assignment", ["disjoint", "same_part", "reassigned", "completed"]
)
def test_overlapping_lookups_classify_current_assignments(
    window_ui: Any, assignment: str
) -> None:
    """New requests cannot supersede unrelated work or lose late demand for a part."""
    ui = window_ui
    ui.board.names.clear()
    ui.board.current = ""
    second = _SelectableFootprint(ui.board, "component-2", "R2")
    second.SetField("LCSC", "")
    ui.board.parts.append(second)
    metadata = {
        "C1": {"assembly_process": "THT", "component_product_type": 1},
        "C2": {"assembly_process": "SMT", "component_product_type": 2},
    }
    ui.supplier.fetch_iter.side_effect = lambda codes: iter(
        (code, metadata[code]) for code in codes
    )

    def check(ui: Any) -> None:
        frame = ui.dialog
        assert ui.controller is None
        assert len(ui.pending_threads) == 1
        if assignment == "completed":
            ui.run_worker()
            pump(ui.wx)
        ref = "R1" if assignment == "reassigned" else "R2"
        code = "C1" if assignment in ("same_part", "completed") else "C2"
        frame.assign_parts(
            SimpleNamespace(references=[ref], lcsc=code, type="Basic", stock=100)
        )
        assert len(ui.pending_threads) == (
            0 if assignment == "completed" else 1 if code == "C1" else 2
        )
        if assignment == "completed":
            assert_assembly_feedback(frame, "R2", "—", pending=False, process="THT")
        else:
            ui.run_worker()
            pump(ui.wx)
        if assignment == "reassigned":
            assert frame.store.get_part("R1")["component_product_type"] is None
        elif assignment == "disjoint":
            assert_assembly_feedback(frame, "R1", "—", pending=False, process="THT")
            assert_assembly_feedback(frame, "R2", "◷", pending=True)
        while ui.pending_threads:
            ui.run_worker()
            pump(ui.wx)
        expected = {"R1": "C1", ref: code}
        for reference, lcsc in expected.items():
            part = frame.store.get_part(reference)
            assert part["lcsc"] == lcsc
            assert (
                part["component_product_type"]
                == metadata[lcsc]["component_product_type"]
            )
            assert frame._get_enrichment_status_label(part) == "Done"
            assert_assembly_feedback(
                frame,
                reference,
                "✓" if metadata[lcsc]["component_product_type"] == 2 else "—",
                pending=False,
                process=metadata[lcsc]["assembly_process"],
            )
        assert not frame.assembly_lookup.pending
        queried = [
            code
            for call in ui.supplier.fetch_iter.call_args_list
            for code in call.args[0]
        ]
        assert sorted(queried) == sorted(set(expected.values()) | {"C1"})

    ui.run(check)


@pytest.mark.parametrize(
    "action", ["reselect", "clear_then_reassign", "paste", "preference"]
)
def test_cached_assignment_immediately_restores_assembly_feedback(
    window_ui: Any, action: str
) -> None:
    """All assignment handlers reuse complete session metadata without another lookup."""
    ui = window_ui
    ui.board.names.clear()
    ui.board.current = ""
    second = _SelectableFootprint(ui.board, "component-2", "R2")
    second.SetField("LCSC", "")
    ui.board.parts.append(second)
    ui.supplier.fetch_iter.return_value = iter(
        [("C1", {"assembly_process": "SMT", "component_product_type": 2})]
    )

    def check(ui: Any) -> None:
        frame = ui.dialog
        ui.run_worker()
        pump(ui.wx)
        assert_assembly_feedback(frame, "R1", "✓", pending=False, process="SMT")
        reference = "R2" if action in ("paste", "preference") else "R1"
        model = frame.partlist_data_model
        item = model.ObjectToItem(model.data[model.find_index(reference)])
        frame.footprint_list.Select(item)
        if action == "clear_then_reassign":
            frame.remove_lcsc_number()
            assert frame.store.get_part(reference)["lcsc"] == ""
            assert model.GetValue(item, model.columns["STANDARD_ONLY_COL"]) == ""
            assert model.GetValue(item, model.columns["ENRICH_COL"]) == ""
            assert model.get_assembly_tooltip(item) == "No assigned LCSC part."
        if action in ("reselect", "clear_then_reassign"):
            frame.assign_parts(
                SimpleNamespace(
                    references=[reference], lcsc="C1", type="Basic", stock=100
                )
            )
        elif action == "paste":
            assert ui.wx.TheClipboard.Open()
            try:
                assert ui.wx.TheClipboard.SetData(ui.wx.TextDataObject("C1"))
            finally:
                ui.wx.TheClipboard.Close()
            frame.paste_part_lcsc()
        else:
            part = frame.store.get_part(reference)
            frame.library.create_part_preferences_table()
            frame.library.save_part_preferences(
                [(part["footprint"], part["value"], "C1")]
            )
            frame.apply_selected_part_preferences()
        assert frame.store.get_part(reference)["lcsc"] == "C1"
        assert_assembly_feedback(frame, reference, "✓", pending=False, process="SMT")
        assert not ui.pending_threads
        assert not frame.assembly_lookup.pending
        ui.supplier.fetch_iter.assert_called_once_with(("C1",))

    ui.run(check)


@pytest.mark.parametrize("known", ["assembly_process", "component_product_type"])
def test_assignment_preserves_partial_cached_metadata_while_retrying(
    window_ui: Any, known: str
) -> None:
    """Missing metadata is retried without hiding the supplier facts already known."""
    ui = window_ui
    ui.board.names.clear()
    ui.board.current = ""
    metadata = {"assembly_process": "THT", "component_product_type": 2}
    ui.supplier.fetch_iter.side_effect = [
        iter([("C1", {known: metadata[known]})]),
        iter([("C1", metadata)]),
    ]

    def check(ui: Any) -> None:
        frame = ui.dialog
        ui.run_worker()
        pump(ui.wx)
        process = "THT" if known == "assembly_process" else ""
        assert_assembly_feedback(
            frame, "R1", "?" if process else "✓", pending=False, process=process
        )
        frame.assign_parts(
            SimpleNamespace(references=["R1"], lcsc="C1", type="Basic", stock=100)
        )
        assert len(ui.pending_threads) == 1
        assert_assembly_feedback(
            frame, "R1", "◷" if process else "✓", pending=True, process=process
        )
        ui.run_worker()
        pump(ui.wx)
        assert_assembly_feedback(frame, "R1", "✓", pending=False, process="THT")
        assert ui.supplier.fetch_iter.call_count == 2

    ui.run(check)


def test_completion_storage_failure_disables_stale_rows_and_refreshes_summary(
    window_ui: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Partial supplier success remains durable when the final status read fails."""
    ui = window_ui
    ui.board.names.clear()
    ui.board.current = ""
    second = _SelectableFootprint(ui.board, "component-2", "R2")
    second.SetField("LCSC", "C2")
    ui.board.parts.append(second)

    def partial_result(codes: tuple[str, ...]) -> Iterator[tuple[str, dict[str, Any]]]:
        assert set(codes) == {"C1", "C2"}
        yield "C1", {"assembly_process": "SMT", "component_product_type": 2}
        raise OSError("supplier disconnected")

    ui.supplier.fetch_iter.side_effect = partial_result

    def check(ui: Any) -> None:
        frame = ui.dialog
        store = frame.store
        sources: list[str] = []

        def observe(event: Any) -> None:
            sources.append(event.source)
            event.Skip()

        frame.Bind(ui.mainwindow.EVT_BOM_DATA_CHANGED_EVENT, observe)
        monkeypatch.setattr(
            store, "read_all", Mock(side_effect=sqlite3.OperationalError("read failed"))
        )
        ui.run_worker()
        pump(ui.wx)
        assert frame.store is None and frame._project_storage_unavailable
        assert frame.partlist_data_model.data == []
        assert not frame.footprint_list.IsEnabled()
        assert not frame.generate_button.IsEnabled()
        assert not frame.assembly_lookup.pending
        assert sources == ["enrichment_update"]
        assert not frame._bom_recompute_scheduled
        assert store.get_part("R1")["component_product_type"] == 2
        assert store.get_part("R2")["lcsc"] == "C2"
        assert store.get_part("R2")["component_product_type"] is None

    ui.run(check)


@pytest.mark.parametrize("storage_failed", [False, True])
def test_storage_recovery_restarts_lookup_and_rejects_old_delivery(
    window_ui: Any, storage_failed: bool
) -> None:
    """Each storage lifetime rejects old callbacks and fetches its own supplier facts."""
    ui = window_ui
    ui.board.names.clear()
    ui.board.current = ""
    ui.supplier.fetch_iter.side_effect = [
        iter([("C1", {"assembly_process": "THT", "component_product_type": 1})]),
        iter([("C1", {"assembly_process": "SMT", "component_product_type": 2})]),
        iter([("C1", {"assembly_process": "THT", "component_product_type": 1})]),
    ]

    def recover(ui: Any) -> None:
        frame = ui.dialog
        assert len(ui.pending_threads) == 1
        if storage_failed:
            frame._set_project_storage_error(
                sqlite3.OperationalError("database locked")
            )
            assert frame.store is None
        frame.init_store()
        assert len(ui.pending_threads) == 2
        ui.run_worker()
        pump(ui.wx)
        assert frame.store.get_part("R1")["component_product_type"] is None
        ui.run_worker()
        pump(ui.wx)
        part = frame.store.get_part("R1")
        assert (part["assembly_process"], part["component_product_type"]) == ("SMT", 2)
        assert_assembly_feedback(frame, "R1", "✓", pending=False, process="SMT")

    def reopen(ui: Any) -> None:
        assert ui.dialog.store.get_part("R1")["component_product_type"] is None
        assert len(ui.pending_threads) == 1
        assert_assembly_feedback(ui.dialog, "R1", "◷", pending=True)
        ui.run_worker()
        pump(ui.wx)
        assert ui.dialog.store.get_part("R1")["component_product_type"] == 1
        assert ui.pending_threads == []
        assert_assembly_feedback(ui.dialog, "R1", "—", pending=False, process="THT")
        assert ui.supplier.fetch_iter.call_count == 3

    ui.run(recover, reopen)


def test_thread_start_failure_clears_pending_feedback_and_allows_retry(
    window_ui: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A committed assignment stays valid when optional background work cannot start."""
    ui = window_ui
    ui.board.names.clear()
    ui.board.current = ""
    ui.supplier.fetch_iter.side_effect = lambda codes: iter(
        (code, {"assembly_process": "SMT", "component_product_type": 2})
        for code in codes
    )

    def check(ui: Any) -> None:
        frame = ui.dialog
        worker = import_module(frame.assembly_lookup.__class__.__module__)
        with monkeypatch.context() as patch:
            patch.setattr(
                worker,
                "Thread",
                Mock(
                    return_value=SimpleNamespace(
                        start=Mock(side_effect=RuntimeError("no thread"))
                    )
                ),
            )
            frame.assign_parts(
                SimpleNamespace(references=["R1"], lcsc="C2", type="Basic", stock=100)
            )
        part = frame.store.get_part("R1")
        assert part["lcsc"] == "C2"
        model = frame.partlist_data_model
        assert model.data[model.find_index("R1")][model.columns["ENRICH_COL"]] == (
            "Class missing"
        )
        assert "C2" not in frame.assembly_lookup.pending
        assert_assembly_feedback(frame, "R1", "?", pending=False)
        frame.start_assembly_enrichment(["R1"])
        assert_assembly_feedback(frame, "R1", "◷", pending=True)
        while ui.pending_threads:
            ui.run_worker()
            pump(ui.wx)
        assert frame.store.get_part("R1")["component_product_type"] == 2
        assert not frame.assembly_lookup.pending
        assert_assembly_feedback(frame, "R1", "✓", pending=False, process="SMT")

    ui.run(check)
