"""Assembly status through real window/model constructors and board-backed stores."""

from collections.abc import Callable, Iterator
from functools import partial
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from . import (
    part_preferences_test_support as database_support,
    test_window_layout as layout,
)
from .part_preferences_test_support import Board, Footprint
from .stock_test_support import stock_modules

database_mainwindow = database_support.mainwindow
_clear_callbacks = layout._clear_callbacks


@pytest.fixture
def workflow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, database_mainwindow: Any
) -> Iterator[SimpleNamespace]:
    """Construct the window, real model and native board view with queued workers."""
    with stock_modules() as modules:
        main = layout.mainwindow
        monkeypatch.setattr(
            main, "PartListDataModel", modules.datamodel.PartListDataModel
        )
        monkeypatch.setattr(main, "TypeCellTooltip", MagicMock())
        monkeypatch.setattr(main.wx, "PostEvent", MagicMock(), raising=False)
        monkeypatch.setattr(main.wx, "ToolTip", str, raising=False)
        window = layout._open_main(monkeypatch, {})
        jobs: list[Callable[[], None]] = []
        delivery: list[Callable[[], None]] = []
        metadata = {
            "C100": {"component_product_type": 2, "assembly_process": "SMT"},
            "C200": {"component_product_type": 0, "assembly_process": "SMT"},
            "C300": {"component_product_type": 2, "assembly_process": "SMT"},
        }

        def thread(
            *, target: Callable[..., None], args: tuple[Any, ...], daemon: bool
        ) -> SimpleNamespace:
            return SimpleNamespace(start=lambda: jobs.append(partial(target, *args)))

        def call_after(callback: Callable[..., None], *args: Any) -> None:
            delivery.append(partial(callback, *args))

        worker = import_module(window.assembly_lookup.__class__.__module__)
        provider = MagicMock()
        provider.fetch_iter.side_effect = lambda codes: iter(
            (code, metadata[code]) for code in codes
        )
        monkeypatch.setattr(worker, "Thread", MagicMock(side_effect=thread))
        monkeypatch.setattr(worker.wx, "CallAfter", call_after)
        monkeypatch.setattr(
            worker, "LCSCAssemblyMetadataProvider", MagicMock(return_value=provider)
        )
        board = Board([Footprint("R1", lcsc="C100"), Footprint("R2", lcsc="C200")])
        board.filename = str(tmp_path / "board.kicad_pcb")
        window.pcbnew = SimpleNamespace(GetBoard=lambda: board)
        window._board_identity = main.board_identity(board)
        window.store = database_mainwindow.Store(window, str(tmp_path), board)
        window.library = MagicMock()
        window.library.get_part_details.return_value = {"type": "Basic", "stock": 100}
        window.library.read_correction_data.return_value = SimpleNamespace(
            corrections=(), state=main.CorrectionState.READY, scope="global", db_path=""
        )
        window.hide_bom_parts = window.hide_pos_parts = False
        window.get_correction = MagicMock(return_value="0°")
        window.update_correction_status = MagicMock()
        window.populate_footprint_list()
        yield SimpleNamespace(
            window=window,
            main=main,
            board=board,
            db=database_mainwindow,
            worker=worker,
            provider=provider,
            jobs=jobs,
            delivery=delivery,
            metadata=metadata,
        )


def cell(workflow: SimpleNamespace, reference: str = "R1") -> str:
    """Read the renderer value from the real current row."""
    model = workflow.window.partlist_data_model
    row = model.data[model.find_index(reference)]
    return model.GetValue(model.ObjectToItem(row), model.columns["STANDARD_ONLY_COL"])


def tooltip(workflow: SimpleNamespace, reference: str = "R1") -> str:
    """Read full row help from the real current row."""
    model = workflow.window.partlist_data_model
    return model.get_assembly_tooltip(
        model.ObjectToItem(model.data[model.find_index(reference)])
    )


def run_worker(workflow: SimpleNamespace) -> None:
    """Run one production fetch while retaining its queued UI callbacks."""
    workflow.jobs.pop(0)()


def deliver_next(workflow: SimpleNamespace) -> None:
    """Deliver one production result or completion on the controlled UI queue."""
    workflow.delivery.pop(0)()


def drain_delivery(workflow: SimpleNamespace) -> None:
    """Deliver all remaining callbacks, including the batch completion."""
    while workflow.delivery:
        deliver_next(workflow)


def test_each_result_updates_std_before_batch_completion(
    workflow: SimpleNamespace,
) -> None:
    """A first Standard-only result cannot look Economic while another is pending."""
    window = workflow.window
    assert cell(workflow) == "?"
    window.start_assembly_enrichment()
    assert cell(workflow) == cell(workflow, "R2") == "◷"
    workflow.main.wx.PostEvent.reset_mock()
    run_worker(workflow)
    deliver_next(workflow)
    assert cell(workflow) == "✓"
    assert "Standard Only" in tooltip(workflow)
    assert "SMT" in tooltip(workflow)
    assert cell(workflow, "R2") == "◷"
    workflow.main.wx.PostEvent.assert_not_called()
    deliver_next(workflow)
    assert cell(workflow, "R2") == "—"
    deliver_next(workflow)
    workflow.main.wx.PostEvent.assert_called_once()
    assert workflow.main.wx.PostEvent.call_args.args[1].source == "enrichment_update"
    assert not window.assembly_lookup.pending


@pytest.mark.parametrize(
    "classification,expected", [(None, "?"), (2, "✓"), (0, "—"), (1, "—")]
)
def test_partial_cache_survives_empty_result_but_reopen_fetches_again(
    workflow: SimpleNamespace, classification: Any, expected: str
) -> None:
    """Partial in-memory results survive empty fetches and are disposable on reopen."""
    window = workflow.window
    window.store.set_assembly_metadata("R1", "", classification, expected_lcsc="C100")
    window.populate_footprint_list()
    assert cell(workflow) == expected
    window.start_assembly_enrichment()
    assert cell(workflow) == ("◷" if classification is None else expected)
    workflow.metadata["C100"] = {}
    run_worker(workflow)
    deliver_next(workflow)
    assert cell(workflow) == expected
    assert "unavailable" in tooltip(workflow)
    assert "Retrieving" not in tooltip(workflow)
    assert "C100" in window.store.get_assembly_enrichment_targets()
    window.store = workflow.db.Store(window, window.store.project_path, workflow.board)
    window.populate_footprint_list()
    assert cell(workflow) == "?"
    assert "C100" in window.store.get_assembly_enrichment_targets()


def test_completed_cache_repopulates_without_retrieval(
    workflow: SimpleNamespace,
) -> None:
    """A complete stored classification supplies Std before estimator completion."""
    window = workflow.window
    window.store.set_assembly_metadata("R1", "THT", 1, expected_lcsc="C100")
    window.populate_footprint_list()
    assert cell(workflow) == "—"
    assert "Economic Only" in tooltip(workflow)
    assert "THT" in tooltip(workflow)
    assert "C100" not in window.store.get_assembly_enrichment_targets()
    window._set_standard_only_refs(set())
    assert cell(workflow) == "—"


def test_reassignment_and_stale_callbacks_cannot_restore_old_classification(
    workflow: SimpleNamespace,
) -> None:
    """Persisted assignment guards and model clearing agree when a result is late."""
    window = workflow.window
    window.start_assembly_enrichment(["R1"])
    run_worker(workflow)
    workflow.board.footprints["R1"].SetField("LCSC", "C300")
    window.partlist_data_model.set_lcsc("R1", "C300", "Basic", 100, "")
    assert cell(workflow) == "?"
    drain_delivery(workflow)
    assert cell(workflow) == "?"
    assert "Standard Only" not in tooltip(workflow)
    window.start_assembly_enrichment(["R1"])
    run_worker(workflow)
    window.assembly_lookup.invalidate()
    window.populate_footprint_list()
    drain_delivery(workflow)
    assert cell(workflow) == "?"
    assert window.store.get_part("R1")["component_product_type"] is None
    model = window.partlist_data_model
    model.remove_lcsc_number(model.ObjectToItem(model.data[model.find_index("R1")]))
    assert cell(workflow) == ""
    assert "No assigned LCSC" in tooltip(workflow)


@pytest.mark.parametrize("count", [8, 64])
def test_supplier_result_reads_each_native_recipient_once(
    workflow: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    """A shared supplier result scales with PCB size instead of rescanning per part."""
    window = workflow.window
    workflow.board.footprints = {
        f"R{index}": Footprint(f"R{index}", lcsc="C100")
        for index in range(1, count + 1)
    }
    window.populate_footprint_list()
    field_reads = []
    for footprint in workflow.board.GetFootprints():
        read = MagicMock(wraps=footprint.GetFields)
        monkeypatch.setattr(footprint, "GetFields", read)
        field_reads.append(read)

    window._apply_assembly_metadata(
        "C100", {"assembly_process": "SMT", "component_product_type": 2}
    )

    assert sum(read.call_count for read in field_reads) == count
    assert all(cell(workflow, f"R{index}") == "✓" for index in range(1, count + 1))


def test_late_supplier_result_is_cached_by_code_without_changing_current_assignment(
    workflow: SimpleNamespace,
) -> None:
    """Late facts remain reusable for their code while a different current part stays unknown."""
    window = workflow.window
    window.start_assembly_enrichment(["R1"])
    run_worker(workflow)
    footprint = workflow.board.footprints["R1"]
    footprint.SetField("LCSC", "C300")
    window.partlist_data_model.set_lcsc("R1", "C300", "Basic", 100, "")

    drain_delivery(workflow)

    assert footprint.field.text == "C300"
    assert cell(workflow) == "?"
    assert window.store.get_part("R1")["component_product_type"] is None
    footprint.SetField("LCSC", "C100")
    window.populate_footprint_list()
    assert cell(workflow) == "✓"
    assert window.store.get_part("R1")["component_product_type"] == 2
    assert "C100" not in window.store.get_assembly_enrichment_targets(["R1"])
    workflow.provider.fetch_iter.assert_called_once()


def test_metadata_changes_refresh_stationary_hover(workflow: SimpleNamespace) -> None:
    """Pending and completed events ask the existing hover controller to re-hit-test."""
    window = workflow.window
    controller = window._type_cell_tooltip
    controller.refresh.reset_mock()
    window.start_assembly_enrichment()
    controller.refresh.assert_called_once()
    controller.refresh.reset_mock()
    run_worker(workflow)
    deliver_next(workflow)
    controller.refresh.assert_called_once()


def test_overlapping_batches_finish_each_still_assigned_row(
    workflow: SimpleNamespace,
) -> None:
    """Assigning another part during a lookup cannot strand the first row loading."""
    window = workflow.window
    window.start_assembly_enrichment(["R1"])
    window.start_assembly_enrichment(["R2"])
    run_worker(workflow)
    drain_delivery(workflow)
    assert cell(workflow) == "✓"
    assert cell(workflow, "R2") == "◷"
    run_worker(workflow)
    drain_delivery(workflow)
    assert cell(workflow, "R2") == "—"
    assert not window.assembly_lookup.pending


@pytest.mark.parametrize("repopulate", [False, True])
def test_new_assignment_shares_inflight_result(
    workflow: SimpleNamespace, repopulate: bool
) -> None:
    """A newly assigned sibling joins an existing request and receives its result."""
    window = workflow.window
    window.start_assembly_enrichment(["R1"])
    assert window._apply_lcsc_assignments({"R2": "C100"}) == ["R2"]
    workflow.worker.Thread.assert_called_once()
    if repopulate:
        window.populate_footprint_list()
    assert cell(workflow, "R2") == "◷"
    run_worker(workflow)
    drain_delivery(workflow)
    assert cell(workflow) == cell(workflow, "R2") == "✓"
    assert "Retrieving" not in tooltip(workflow, "R2")
    assert window.store.get_part("R2")["component_product_type"] == 2
    assert not window.assembly_lookup.pending


def test_partial_supplier_failure_clears_pending_metadata_and_hover(
    workflow: SimpleNamespace,
) -> None:
    """A failed batch preserves its first result and releases the unfinished hover."""

    def partial_result(codes: tuple[str, ...]) -> Iterator[tuple[str, dict[str, Any]]]:
        assert set(codes) == {"C100", "C200"}
        yield "C100", workflow.metadata["C100"]
        raise OSError("supplier disconnected")

    window = workflow.window
    controller = window._type_cell_tooltip
    workflow.provider.fetch_iter.side_effect = partial_result
    window.start_assembly_enrichment()
    run_worker(workflow)
    deliver_next(workflow)
    assert cell(workflow) == "✓"
    assert cell(workflow, "R2") == "◷"
    assert "Retrieving" in tooltip(workflow, "R2")
    controller.refresh.reset_mock()
    deliver_next(workflow)
    assert cell(workflow) == "✓"
    assert cell(workflow, "R2") == "?"
    assert "unavailable" in tooltip(workflow, "R2")
    assert "Retrieving" not in tooltip(workflow, "R2")
    controller.refresh.assert_called_once()
    assert window.store.get_part("R1")["component_product_type"] == 2
    assert not window.assembly_lookup.pending
