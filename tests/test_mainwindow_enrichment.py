"""Assembly status through real window/model constructors and database events."""

from collections.abc import Iterator
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


@pytest.fixture
def workflow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, database_mainwindow: Any
) -> Iterator[SimpleNamespace]:
    """Construct the window, real model and persistent store with queued workers."""
    with stock_modules() as modules:
        main = layout.mainwindow
        monkeypatch.setattr(
            main, "PartListDataModel", modules.datamodel.PartListDataModel
        )
        monkeypatch.setattr(main, "TypeCellTooltip", MagicMock())
        monkeypatch.setattr(main.wx, "PostEvent", MagicMock(), raising=False)
        monkeypatch.setattr(main.wx, "ToolTip", str, raising=False)
        monkeypatch.setattr(main, "Thread", MagicMock())
        window = layout._open_main(monkeypatch, {})
        board = Board([Footprint("R1", lcsc="C100"), Footprint("R2", lcsc="C200")])
        board.filename = str(tmp_path / "test.kicad_pcb")
        window.pcbnew = SimpleNamespace(GetBoard=lambda: board)
        window._board_uuid = main.board_identity(board)
        window._board_filename = board.filename
        window.library = object.__new__(database_mainwindow.Library)
        window.library.logger = MagicMock()
        window.library.part_preferences_db_file = str(tmp_path / "mappings.db")
        window.library.create_lcsc_metadata_table()
        window.library.get_part_details = MagicMock()
        window.library.read_correction_data = MagicMock()
        window.store = database_mainwindow.Store(window, str(tmp_path), board)
        window.library.get_part_details.return_value = {"type": "Basic", "stock": 100}
        window.library.read_correction_data.return_value = SimpleNamespace(
            corrections=(), state=main.CorrectionState.READY, scope="global", db_path=""
        )
        window.hide_bom_parts = window.hide_pos_parts = False
        window.get_correction = MagicMock(return_value="0°")
        window.update_correction_status = MagicMock()
        window.populate_footprint_list()
        yield SimpleNamespace(
            window=window, main=main, board=board, db=database_mainwindow
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


def result(
    workflow: SimpleNamespace,
    reference: str,
    lcsc: str,
    metadata: dict[str, Any],
    *,
    generation: Any = None,
) -> None:
    """Deliver the same payload and generation carried by a worker event."""
    window = workflow.window
    window.on_assembly_enrichment_progress(
        SimpleNamespace(
            refs=[reference],
            lcsc=lcsc,
            metadata=metadata,
            generation=window.assembly_enrichment_generation
            if generation is None
            else generation,
        )
    )


def test_each_result_updates_std_before_batch_completion(
    workflow: SimpleNamespace,
) -> None:
    """A first Standard-only result cannot look Economic while another is pending."""
    window = workflow.window
    assert cell(workflow) == "?"
    window.start_assembly_enrichment()
    assert cell(workflow) == cell(workflow, "R2") == "◷"
    workflow.main.wx.PostEvent.reset_mock()
    window.store.read_all = MagicMock(wraps=window.store.read_all)
    window.library.merge_lcsc_metadata = MagicMock(
        wraps=window.library.merge_lcsc_metadata
    )
    result(
        workflow, "R1", "C100", {"component_product_type": 2, "assembly_process": "SMT"}
    )
    window.store.read_all.assert_called_once_with()
    window.library.merge_lcsc_metadata.assert_called_once()
    assert cell(workflow) == "✓"
    assert "Standard Only" in tooltip(workflow)
    assert "SMT" in tooltip(workflow)
    assert cell(workflow, "R2") == "◷"
    workflow.main.wx.PostEvent.assert_not_called()
    result(
        workflow, "R2", "C200", {"component_product_type": 0, "assembly_process": "SMT"}
    )
    assert cell(workflow, "R2") == "—"
    window.on_assembly_enrichment_completed(
        SimpleNamespace(generation=window.assembly_enrichment_generation)
    )
    workflow.main.wx.PostEvent.assert_called_once()
    assert workflow.main.wx.PostEvent.call_args.args[1].source == "enrichment_update"
    assert window.pending_assembly_enrichment == set()


@pytest.mark.parametrize(
    "classification,expected", [(None, "?"), (2, "✓"), (0, "—"), (1, "—")]
)
def test_partial_cache_survives_empty_result_and_reopen(
    workflow: SimpleNamespace, classification: Any, expected: str
) -> None:
    """A process-only fetch preserves classification through SQLite and reopening."""
    window = workflow.window
    window.library.merge_lcsc_metadata(
        "C100", {"component_product_type": classification}
    )
    window.populate_footprint_list()
    assert cell(workflow) == expected
    window.start_assembly_enrichment()
    assert cell(workflow) == ("◷" if classification is None else expected)
    result(workflow, "R1", "C100", {})
    assert cell(workflow) == expected
    assert "unavailable" in tooltip(workflow)
    assert "Retrieving" not in tooltip(workflow)
    assert "C100" in window.store.get_assembly_enrichment_targets()
    window.store = workflow.db.Store(window, window.store.project_path, workflow.board)
    window.populate_footprint_list()
    assert cell(workflow) == expected


def test_completed_cache_repopulates_without_retrieval(
    workflow: SimpleNamespace,
) -> None:
    """A complete stored classification supplies Std before estimator completion."""
    window = workflow.window
    window.library.merge_lcsc_metadata(
        "C100", {"assembly_process": "THT", "component_product_type": 1}
    )
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
    window.start_assembly_enrichment()
    old_generation = window.assembly_enrichment_generation
    workflow.board.footprints["R1"].SetField("LCSC", "C300")
    window.store.set_lcsc_assignments([("R1", "C300", None)])
    window.partlist_data_model.set_lcsc("R1", "C300", "Basic", 100, "")
    assert cell(workflow) == "?"
    result(
        workflow, "R1", "C100", {"component_product_type": 2, "assembly_process": "SMT"}
    )
    assert cell(workflow) == "?"
    assert "Standard Only" not in tooltip(workflow)
    window.assembly_enrichment_generation += 1
    result(
        workflow, "R1", "C300", {"component_product_type": 2}, generation=old_generation
    )
    assert cell(workflow) == "?"
    model = window.partlist_data_model
    model.remove_lcsc_number(model.ObjectToItem(model.data[model.find_index("R1")]))
    assert cell(workflow) == ""
    assert "No assigned LCSC" in tooltip(workflow)


def test_metadata_changes_refresh_stationary_hover(workflow: SimpleNamespace) -> None:
    """Pending and completed events ask the existing hover controller to re-hit-test."""
    window = workflow.window
    controller = window._type_cell_tooltip
    controller.refresh.reset_mock()
    window.start_assembly_enrichment()
    controller.refresh.assert_called_once()
    controller.refresh.reset_mock()
    result(
        workflow, "R1", "C100", {"component_product_type": 2, "assembly_process": "SMT"}
    )
    controller.refresh.assert_called_once()


def test_overlapping_batches_finish_each_still_assigned_row(
    workflow: SimpleNamespace,
) -> None:
    """Assigning another part during a lookup cannot strand the first row loading."""
    window = workflow.window
    window.start_assembly_enrichment(["R1"])
    first_generation = window.assembly_enrichment_generation
    window.start_assembly_enrichment(["R2"])
    result(
        workflow,
        "R1",
        "C100",
        {"component_product_type": 2, "assembly_process": "SMT"},
        generation=first_generation,
    )
    assert cell(workflow) == "✓"
    result(
        workflow, "R2", "C200", {"component_product_type": 0, "assembly_process": "SMT"}
    )
    assert cell(workflow, "R2") == "—"
    assert window.pending_assembly_enrichment == set()


@pytest.mark.parametrize("repopulate", [False, True])
def test_new_assignment_shares_inflight_result(
    workflow: SimpleNamespace, repopulate: bool
) -> None:
    """A newly assigned sibling joins an existing request and receives its result."""
    window = workflow.window
    window.start_assembly_enrichment(["R1"])
    assert window._apply_lcsc_assignments({"R2": "C100"}) == ["R2"]
    workflow.main.Thread.assert_called_once()
    if repopulate:
        window.populate_footprint_list()
    assert cell(workflow, "R2") == "◷"
    result(
        workflow, "R1", "C100", {"component_product_type": 2, "assembly_process": "SMT"}
    )
    assert cell(workflow) == cell(workflow, "R2") == "✓"
    assert "Retrieving" not in tooltip(workflow, "R2")
    assert window.store.get_part("R2")["component_product_type"] == 2
    assert window.pending_assembly_enrichment == set()
