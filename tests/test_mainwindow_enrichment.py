"""Assembly status through real window/model constructors and database events."""

from collections.abc import Iterator
from pathlib import Path
import sys
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
    """Construct real window/model/cache objects with queued worker boundaries."""
    with stock_modules() as modules:
        main = layout.mainwindow
        monkeypatch.setattr(
            main, "PartListDataModel", modules.datamodel.PartListDataModel
        )
        monkeypatch.setattr(main, "TypeCellTooltip", MagicMock())
        monkeypatch.setattr(main, "set_lcsc_value", database_mainwindow.set_lcsc_value)
        monkeypatch.setattr(
            main,
            "classify_component_product_type",
            database_mainwindow.classify_component_product_type,
        )
        monkeypatch.setattr(main.wx, "PostEvent", MagicMock(), raising=False)
        monkeypatch.setattr(main.wx, "ToolTip", str, raising=False)
        monkeypatch.setattr(main, "Thread", MagicMock())
        monkeypatch.setattr(
            sys.modules[database_mainwindow.Library.__module__], "Thread", MagicMock()
        )
        window = layout._open_main(monkeypatch, {})
        board = Board([Footprint("R1", lcsc="C100"), Footprint("R2", lcsc="C200")])
        window.pcbnew = SimpleNamespace(
            GetBoard=lambda: board,
            GetCurrentSelection=lambda: [
                fp for fp in board.GetFootprints() if getattr(fp, "selected", False)
            ],
            Refresh=MagicMock(),
        )
        window._board_uuid = main.board_identity(board)
        window._board_filename = None
        window.project_path = str(tmp_path)
        window.settings["library"] = {"data_path": str(tmp_path / "global")}
        window.library = database_mainwindow.Library(window)
        window.store = database_mainwindow.Store(window, str(tmp_path), board)
        monkeypatch.setattr(
            window.library,
            "get_part_details",
            MagicMock(return_value={"type": "Basic", "stock": 100}),
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
    lcsc: str,
    metadata: dict[str, Any],
    *,
    generation: Any = None,
) -> None:
    """Deliver the same payload and generation carried by a worker event."""
    window = workflow.window
    window.on_assembly_enrichment_progress(
        SimpleNamespace(
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
    result(workflow, "C100", {"component_product_type": 2, "assembly_process": "SMT"})
    assert cell(workflow) == "✓"
    assert "Standard Only" in tooltip(workflow)
    assert "SMT" in tooltip(workflow)
    assert cell(workflow, "R2") == "◷"
    workflow.main.wx.PostEvent.assert_not_called()
    result(workflow, "C200", {"component_product_type": 0, "assembly_process": "SMT"})
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
    result(workflow, "C100", {})
    assert cell(workflow) == expected
    assert "unavailable" in tooltip(workflow)
    assert "Retrieving" not in tooltip(workflow)
    assert "C100" in window.store.get_assembly_enrichment_targets()
    window.library = workflow.db.Library(window)
    window.library.get_part_details = MagicMock(
        return_value={"type": "Basic", "stock": 100}
    )
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


@pytest.mark.parametrize("process", ["THT", ""])
def test_assignment_uses_known_global_classification_immediately(
    workflow: SimpleNamespace, process: str
) -> None:
    """Choosing a cached code displays its known class while only missing fields fetch."""
    window = workflow.window
    window.library.merge_lcsc_metadata(
        "C100", {"assembly_process": process, "component_product_type": 2}
    )
    window.populate_footprint_list()
    assert cell(workflow) == "✓"
    assert cell(workflow, "R2") == "?"
    workflow.main.Thread.reset_mock()

    window.assign_parts(
        SimpleNamespace(references=["R2"], lcsc="C100", type="Basic", stock=100)
    )

    assert workflow.board.footprints["R2"].field.text == "C100"
    assert cell(workflow, "R2") == "✓"
    assert "Standard Only" in tooltip(workflow, "R2")
    if process:
        assert process in tooltip(workflow, "R2")
        workflow.main.Thread.assert_not_called()
    else:
        workflow.main.Thread.assert_called_once()


def test_reassignment_and_stale_callbacks_cannot_restore_old_classification(
    workflow: SimpleNamespace,
) -> None:
    """Current board assignments guard rendered classification when a result is late."""
    window = workflow.window
    window.start_assembly_enrichment()
    old_generation = window.assembly_enrichment_generation
    workflow.board.footprints["R1"].SetField("LCSC", "C300")
    workflow.board.footprints["R2"].SetField("LCSC", "C100")
    window.populate_footprint_list()
    assert cell(workflow) == "?"
    result(workflow, "C100", {"component_product_type": 2, "assembly_process": "SMT"})
    assert cell(workflow) == "?"
    assert "Standard Only" not in tooltip(workflow)
    assert cell(workflow, "R2") == "✓"
    assert "C100" not in window.pending_assembly_enrichment
    assert window.library.get_lcsc_metadata(["C100"])["C100"] == {
        "component_product_type": 2,
        "assembly_process": "SMT",
    }
    window.assembly_enrichment_generation += 1
    result(workflow, "C300", {"component_product_type": 2}, generation=old_generation)
    assert cell(workflow) == "?"
    assert window.library.get_lcsc_metadata(["C300"]) == {}
    model = window.partlist_data_model
    window.footprint_list.GetSelections.return_value = [
        model.ObjectToItem(model.data[model.find_index("R1")])
    ]
    window.remove_lcsc_number()
    assert cell(workflow) == ""
    assert workflow.board.footprints["R1"].field.text == ""
    assert "No assigned LCSC" in tooltip(workflow)


def test_metadata_changes_refresh_stationary_hover(workflow: SimpleNamespace) -> None:
    """Pending and completed events ask the existing hover controller to re-hit-test."""
    window = workflow.window
    controller = window._type_cell_tooltip
    controller.refresh.reset_mock()
    window.start_assembly_enrichment()
    controller.refresh.assert_called_once()
    controller.refresh.reset_mock()
    result(workflow, "C100", {"component_product_type": 2, "assembly_process": "SMT"})
    controller.refresh.assert_called_once()


@pytest.mark.parametrize("overlap", [False, True])
def test_batches_finish_each_still_assigned_row(
    workflow: SimpleNamespace,
    overlap: bool,
) -> None:
    """Assigning another part during a lookup cannot strand the first row loading."""
    window = workflow.window
    window.start_assembly_enrichment(["R1"])
    first_generation = window.assembly_enrichment_generation
    if overlap:
        window.start_assembly_enrichment(["R2"])
    result(
        workflow,
        "C100",
        {"component_product_type": 2, "assembly_process": "SMT"},
        generation=first_generation,
    )
    assert cell(workflow) == "✓"
    if not overlap:
        window.start_assembly_enrichment(["R2"])
        assert window.assembly_enrichment_generation == first_generation
        workflow.main.wx.PostEvent.reset_mock()
        window.on_assembly_enrichment_completed(
            SimpleNamespace(generation=first_generation)
        )
        workflow.main.wx.PostEvent.assert_called_once()
    result(workflow, "C200", {"component_product_type": 0, "assembly_process": "SMT"})
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
    result(workflow, "C100", {"component_product_type": 2, "assembly_process": "SMT"})
    assert cell(workflow) == cell(workflow, "R2") == "✓"
    assert "Retrieving" not in tooltip(workflow, "R2")
    assert window.store.read_all()[1]["component_product_type"] == 2
    assert window.pending_assembly_enrichment == set()
