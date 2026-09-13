"""Supplier readiness through native frame startup, workers, and reopening."""

from collections.abc import Iterator, Sequence
from typing import Any, Optional
from unittest.mock import patch

import pytest

from .native_window_support import _SelectableFootprint, focus, window_ui
from .native_wx_support import wait_until

__all__ = ["window_ui"]
pytestmark = pytest.mark.native_wx
STANDARD_METADATA = {"assembly_process": "SMT", "component_product_type": 2}


def _standard_cell(
    ui: Any, variant: str = "", component_id: str = "component-1"
) -> tuple[Any, str]:
    model = ui.controller.model
    row = model.row_for_component(component_id)
    column = model.column_for(variant, "standard")
    return model.get_value(row, column), model.cell_style(row, column).status


def _assert_classification(ui: Any, expected: Optional[bool], status: str) -> None:
    """Require every variant's classification and the visible estimate to agree."""
    for variant in ("", "A", "B"):
        assert _standard_cell(ui, variant) == (expected, status)
        model = ui.controller.model
        assert model.get_display(0, model.column_for(variant, "standard")) == (
            "✓" if expected else "—"
        )
    decision = ui.dialog.bom_estimator_decision
    assert decision.standard_only_refs == ({"R1"} if expected else set())
    assert decision.classification_missing_refs == (
        {"R1"} if expected is None else set()
    )
    assert not ui.messages


def _complete(ui: Any, classification: Optional[int] = 2, **extra: Any) -> None:
    """Publish provider data through the production thread and native CallAfter."""
    metadata = {**STANDARD_METADATA, "component_product_type": classification, **extra}
    ui.supplier.fetch_iter.side_effect = None
    ui.supplier.fetch_iter.return_value = iter([("C1", metadata)])
    ui.run_worker()
    wait_until(ui.wx, lambda: _standard_cell(ui)[1] != "pending")
    assert not ui.controller._pending


def test_unassigned_startup_needs_no_lookup_or_ordinary_enrichment_ui(
    window_ui: Any,
) -> None:
    """Unassigned native parts remain unknown without requesting supplier data."""
    window_ui.board.parts[0].SetField("LCSC", "")

    def check(ui: Any) -> None:
        assert _standard_cell(ui) == (None, "missing")
        assert not ui.pending_threads
        assert not ui.dialog.bom_estimator_decision.applicable
        ui.controller.render()
        assert _standard_cell(ui) == (None, "missing")
        assert ui.controller.session.reliable and not ui.messages

    with patch.object(
        window_ui.main.JLCPCBTools,
        "_get_enrichment_status_label",
        side_effect=AssertionError("Variant startup used the ordinary enrichment UI"),
    ) as helper:
        window_ui.run(check)
        helper.assert_not_called()


@pytest.mark.parametrize(
    "classification,expected",
    [(0, False), (2, True), (None, None)],
)
def test_background_classification_updates_standard_and_real_estimate(
    window_ui: Any, classification: Optional[int], expected: Optional[bool]
) -> None:
    """Supplier classification updates all cells and the frame's visible estimate."""

    def check(ui: Any) -> None:
        ui.catalog.parts["C1"] = {"stock": 123}
        ui.dialog.bom_estimator_board_count = 1
        ui.controller.refresh()

        def assert_catalog() -> None:
            model = ui.controller.model
            for variant in ("", "A", "B"):
                column = model.column_for(variant, "stock")
                assert model.get_value(0, column) == 123
                assert model.get_display(0, column) == "✓"
                assert "Available stock: 123" in model.cell_details(0, column)
                for field in ("stock", "lcsc"):
                    style = model.cell_style(0, model.column_for(variant, field))
                    assert (style.status, style.matches) == ("known", ("", "A", "B"))

        _assert_classification(ui, None, "pending")
        assert_catalog()
        assert len(ui.pending_threads) == 1
        _complete(ui, classification, stock=999)
        _assert_classification(ui, expected, "missing" if expected is None else "known")
        assert_catalog()
        if expected is None:
            ui.controller.refresh()
            wait_until(ui.wx, lambda: _standard_cell(ui)[1] == "pending")
            _assert_classification(ui, None, "pending")
            assert_catalog()
            _complete(ui, 0)
            _assert_classification(ui, False, "known")
            assert_catalog()

    window_ui.run(check)


@pytest.mark.parametrize(
    "partial", [False, True], ids=["full-failure", "partial-failure"]
)
def test_failed_lookup_preserves_catalog_until_explicit_refresh(
    window_ui: Any, partial: bool
) -> None:
    """Keep completed facts and descriptions while retrying only unfinished parts."""
    if partial:
        extra = _SelectableFootprint(window_ui.board, "component-2", "R2")
        extra.SetField("LCSC", "C2")
        window_ui.board.parts.append(extra)
    failed_id, failed_code = ("component-2", "C2") if partial else ("component-1", "C1")

    def fetch(lcscs: Sequence[str]) -> Iterator[tuple[str, dict[str, Any]]]:
        assert set(lcscs) == ({"C1", "C2"} if partial else {"C1"})
        if partial:
            yield "C1", STANDARD_METADATA
        raise OSError("Supplier lookup failed")

    def check(ui: Any) -> None:
        ui.catalog.parts = {
            code: {"description": f"{code}: resistor\n10 kΩ ±1%"}
            for code in ("C1", "C2")
        }
        ui.controller.refresh()
        before = ui.controller.session.snapshot.source_token

        def assert_catalog() -> None:
            model = ui.controller.model
            for row, component in enumerate(model.rows):
                for variant in ("", "A", "B"):
                    assert ui.catalog.parts[component.lcsc]["description"] in (
                        model.cell_tooltip(row, model.column_for(variant, "lcsc"))
                    )
            assert ui.controller.session.adapter.snapshot().source_token == before
            assert not ui.board.modified and not ui.messages

        assert _standard_cell(ui, "A", failed_id) == (None, "pending")
        assert_catalog()
        assert len(ui.pending_threads) == 1
        ui.supplier.fetch_iter.side_effect = fetch
        ui.run_worker()
        wait_until(ui.wx, lambda: _standard_cell(ui, "A", failed_id) == (None, "error"))
        assert_catalog()
        if partial:
            assert all(
                _standard_cell(ui, variant) == (True, "known")
                for variant in ("", "A", "B")
            )
            assert ui.dialog.bom_estimator_decision.standard_only_refs == {"R1"}
            assert ui.dialog.bom_estimator_decision.classification_missing_refs == {
                "R2"
            }
        assert not ui.controller._pending
        assert _standard_cell(ui, "A", failed_id) == (None, "error")
        ui.controller.refresh()
        wait_until(
            ui.wx, lambda: _standard_cell(ui, "A", failed_id) == (None, "pending")
        )
        assert not ui.controller._metadata_errors
        assert ui.controller._pending == {failed_code}
        assert_catalog()
        ui.supplier.fetch_iter.side_effect = None
        ui.supplier.fetch_iter.return_value = iter([(failed_code, STANDARD_METADATA)])
        ui.run_worker()
        wait_until(ui.wx, lambda: _standard_cell(ui, "A", failed_id) == (True, "known"))
        assert_catalog()
        assert not ui.controller._pending
        assert not ui.dialog.bom_estimator_decision.classification_missing_refs
        assert ui.dialog.bom_estimator_decision.board_standard
        assert ui.controller.session.reliable

    window_ui.run(check)


def test_queued_classification_cannot_revive_a_closed_controller(
    window_ui: Any,
) -> None:
    """Already queued supplier results cannot publish into a closed controller."""

    def check(ui: Any) -> None:
        controller = ui.controller
        ui.supplier.fetch_iter.return_value = iter([("C1", STANDARD_METADATA)])
        ui.run_worker()
        controller.close()
        model, decision = controller.model, ui.dialog.bom_estimator_decision
        drained: list[bool] = []
        ui.wx.CallAfter(drained.append, True)
        wait_until(ui.wx, lambda: bool(drained))
        assert controller.model is model
        assert ui.dialog.bom_estimator_decision is decision
        assert not controller.view._mutations_enabled
        assert (
            ui.cache.assembly_rows(controller.session.snapshot, "A")[0][
                "component_product_type"
            ]
            is None
        )
        assert not ui.messages

    window_ui.run(check)


@pytest.mark.parametrize("offline", [False, True])
def test_reopened_dialog_refetches_classification_without_the_main_helper(
    window_ui: Any, offline: bool
) -> None:
    """Reopening retains native assignments and fetches fresh session-only facts."""

    def original(ui: Any) -> None:
        _complete(ui)
        _assert_classification(ui, True, "known")

    def reopened(ui: Any) -> None:
        _assert_classification(ui, None, "pending")
        for variant in ("", "A", "B"):
            assert (
                ui.controller.session.snapshot.get("component-1", variant).lcsc == "C1"
            )
        assert len(ui.pending_threads) == 1
        if offline:
            ui.supplier.fetch_iter.side_effect = RuntimeError("supplier unavailable")
            ui.run_worker()
            wait_until(ui.wx, lambda: _standard_cell(ui) == (None, "error"))
        else:
            _complete(ui)
        _assert_classification(
            ui, None if offline else True, "error" if offline else "known"
        )

    with patch.object(
        window_ui.main.JLCPCBTools,
        "_get_enrichment_status_label",
        side_effect=AssertionError("Variant reopening used the ordinary enrichment UI"),
    ) as helper:
        window_ui.run(original, reopened)
        helper.assert_not_called()


def test_late_classification_keeps_reassigned_description_and_focus(
    window_ui: Any,
) -> None:
    """An older result cannot replace B's new assignment or description while A is output."""

    def check(ui: Any) -> None:
        ui.catalog.parts = {
            "C1": {"description": "Base resistor\n10 kΩ ±1%"},
            "C2": {"description": "Precision resistor, 22 kΩ ±0.1%"},
        }
        controller = ui.controller
        controller.refresh()
        assert controller._pending == {"C1"}
        assert len(ui.pending_threads) == 1
        controller._on_edit(focus(ui, "B", "lcsc"), "C2")
        ui.supplier.fetch_iter.return_value = iter([("C1", STANDARD_METADATA)])
        ui.run_worker()
        wait_until(ui.wx, lambda: _standard_cell(ui) == (True, "known"))
        assert controller.session.output_variant == "A"
        assert controller.view.selected_target.variant == "B"
        model = controller.model
        for variant in ("", "A", "B"):
            lcsc = "C2" if variant == "B" else "C1"
            tooltip = model.cell_tooltip(0, model.column_for(variant, "lcsc"))
            assert ui.catalog.parts[lcsc]["description"] in tooltip
            row = ui.cache.assembly_rows(controller.session.snapshot, variant)[0]
            assert (row["lcsc"], row["component_product_type"]) == (
                lcsc,
                None if lcsc == "C2" else 2,
            )
        assert ui.catalog.parts["C1"]["description"] not in tooltip
        assert not ui.messages

    window_ui.run(check)


def test_queued_supplier_results_report_invalidated_board_once_and_release_pending(
    window_ui: Any,
) -> None:
    """Three real queued callbacks after Save As report one error and release work."""
    for index in (2, 3):
        footprint = _SelectableFootprint(
            window_ui.board, f"component-{index}", f"R{index}"
        )
        footprint.SetField("LCSC", f"C{index}")
        window_ui.board.parts.append(footprint)

    def check(ui: Any) -> None:
        controller = ui.controller
        assert controller._pending == {"C1", "C2", "C3"}
        ui.supplier.fetch_iter.return_value = iter(
            (lcsc, STANDARD_METADATA) for lcsc in ("C1", "C2", "C3")
        )
        ui.run_worker()
        ui.board.GetFileName = lambda: str(ui.path / "renamed.kicad_pcb")
        with patch.object(controller, "_on_timer", wraps=controller._on_timer) as timer:
            ui.wx.CallAfter(controller._on_timer, None)
            wait_until(
                ui.wx,
                lambda: timer.called
                and not controller._pending
                and not controller.session.reliable,
            )
        assert len(ui.messages) == 1 and "Reopen JLCPCB Tools" in ui.messages[0]
        assert not controller.view._mutations_enabled
        assert not ui.dialog.generate_button.IsEnabled()
        controller.refresh()
        assert len(ui.messages) == 2
        assert not controller.view._mutations_enabled

    window_ui.run(check)
