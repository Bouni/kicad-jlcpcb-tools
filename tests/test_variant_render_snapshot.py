"""Publish captured variant data through real windows and native wx events."""

from decimal import Decimal
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

import pytest

from .native_window_support import (
    _SelectableFootprint,
    choose_output as _output,
    focus,
    window_ui,
)
from .native_wx_support import pump, wait_until
from .variant_matrix_native_test_support import MatrixHarness

__all__ = ["window_ui"]
pytestmark = pytest.mark.native_wx


def _checkbox(ui: Any, control: Any, value: bool) -> None:
    control.SetValue(value)
    event = ui.wx.CommandEvent(ui.wx.wxEVT_CHECKBOX, control.GetId())
    event.SetEventObject(control)
    control.GetEventHandler().ProcessEvent(event)


def _complete(ui: Any) -> None:
    ui.run_worker()
    wait_until(
        ui.wx,
        lambda: not ui.controller.assembly_lookup.pending
        and not ui.controller._render_queued,
    )


def test_initial_preparation_failure_keeps_correction_unknown_until_refresh(
    window_ui: Any,
) -> None:
    """An unprepared bottom-side component must never advertise a valid zero angle."""
    window_ui.board.parts[0].GetOrientationDegrees = lambda: 31.0
    window_ui.board.parts[0].IsFlipped = lambda: True

    def check(ui: Any) -> None:
        c = ui.controller
        c.timer.Stop()
        col = c.model.column_for(None, "correction")
        assert not c.session.reliable and not c.view._mutations_enabled
        assert c.model.cell_style(0, col).status != "known"
        assert c.model.get_display(0, col) == "Unavailable"
        assert "final CPL angle:" not in c.model.cell_details(0, col)
        assert any("preparation unavailable" in message for message in ui.messages)
        resolve.side_effect = None
        c.refresh()
        assert c.session.reliable and c.view._mutations_enabled
        assert c.model.cell_style(0, col).status == "known"
        assert c.model.get_value(0, col).final_angle == 149.0
        assert "final CPL angle: 149.0°" in c.model.cell_details(0, col)

    with patch.object(
        window_ui.module,
        "resolve_shared_corrections",
        wraps=window_ui.module.resolve_shared_corrections,
        side_effect=RuntimeError("preparation unavailable"),
    ) as resolve:
        window_ui.run(check)


@pytest.mark.parametrize("database_state", ["absent", "legacy", "invalid"])
def test_native_workflow_never_opens_project_database(
    window_ui: Any, database_state: str
) -> None:
    """With global corrections, native edits and supplier delivery preserve old project storage."""
    path = window_ui.path / "jlcpcb" / "project.db"
    if database_state != "absent":
        path.parent.mkdir()
        if database_state == "legacy":
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TABLE part_info(reference TEXT PRIMARY KEY, lcsc TEXT)"
                )
                connection.execute("INSERT INTO part_info VALUES('R1','C999')")
            connection.close()
        else:
            path.write_bytes(b"unavailable development database")
    before = path.read_bytes() if path.exists() else None
    path.parent.mkdir(exist_ok=True)
    csv = path.with_name("part_assignments.csv")
    csv.write_bytes(b"R1,C888\n")
    original_connect = sqlite3.connect

    def connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        name = str(database)
        actual = unquote(urlsplit(name).path) if name.startswith("file:") else name
        assert Path(actual).resolve() != path.resolve(), (
            "Variant workflow opened project SQLite"
        )
        return original_connect(database, *args, **kwargs)

    footprint = window_ui.board.parts[0]
    footprint.SetField("LCSC", "C100")
    footprint.AddVariant("A").SetFieldValue("LCSC", "C200")
    window_ui.supplier.fetch_iter.side_effect = lambda codes: iter(
        (code, {"assembly_process": "SMT", "component_product_type": 0})
        for code in codes
    )

    def check(ui: Any) -> None:
        c = ui.controller
        corrections = Path(ui.catalog.correctionsdb_file)
        correction_bytes = corrections.read_bytes()
        assert Path(ui.cache.dbfile) == path
        assert c.session.snapshot.get("component-1", "").lcsc == "C100"
        assert c.session.snapshot.get("component-1", "A").lcsc == "C200"
        assert not any(
            "Import legacy assignments" in child.GetLabel()
            for child in c.panel.GetChildren()
            if isinstance(child, ui.wx.Button)
        )
        focus(ui, "A")
        ui.dialog.remove_lcsc_number()
        h = MatrixHarness(ui.dialog, ui.wx, ui.view, ui.module, view=c.view)
        for field in ("bom", "pop"):
            h.click(0, c.model.column_for("A", field))
        _complete(ui)
        assert corrections.read_bytes() == correction_bytes
        c.refresh()
        part = c.session.snapshot.get("component-1", "A")
        assert part.lcsc == "" and not part.bom and not part.pop
        row = ui.cache.assembly_rows(c.session.snapshot, "A")[0]
        assert row["component_product_type"] is None
        assert c.session.snapshot.get("component-1", "").lcsc == "C100"
        c.begin_generation(())
        assert c.session.generating
        c.end_generation()
        assert ui.dialog.generate_button.IsEnabled() and not ui.messages

    def reopened(ui: Any) -> None:
        c = ui.controller
        assert c.session.snapshot.get("component-1", "").lcsc == "C100"
        part = c.session.snapshot.get("component-1", "A")
        assert part.lcsc == "" and not part.bom and not part.pop
        assert ui.cache.get_missing_metadata(c.session.snapshot) == {"C100"}
        _complete(ui)
        assert not ui.messages

    with patch.object(sqlite3, "connect", connect):
        window_ui.run(check, reopened)
    assert (path.read_bytes() if path.exists() else None) == before
    assert csv.read_bytes() == b"R1,C888\n"


def test_display_gestures_reuse_prepared_snapshot_without_native_or_catalog_reads(
    window_ui: Any,
) -> None:
    """Output and display choices repaint captured data regardless of table width."""
    window_ui.board.names = ["A", "B", *[f"V{i}" for i in range(10)]]

    def check(ui: Any) -> None:
        c = ui.controller
        c.timer.Stop()  # Measure display actions independently of periodic native refresh.
        snapshot = c.session.snapshot
        with (
            patch.object(
                c.session.adapter,
                "snapshot",
                side_effect=AssertionError("display read native data"),
            ),
            patch.object(
                ui.catalog,
                "read_correction_data",
                side_effect=AssertionError("display prepared corrections"),
            ),
            patch.object(
                ui.catalog,
                "get_part_details",
                side_effect=AssertionError("display queried catalog"),
            ),
        ):
            c.render()
            _output(ui, "")
            for control in (c.differences, c.show_footprint_library):
                _checkbox(ui, control, True)
            pump(ui.wx)
        assert c.session.reliable and c.session.snapshot is snapshot
        assert c.model.snapshot is snapshot and c.session.output_variant == ""
        assert not ui.messages

    window_ui.run(check)


@pytest.mark.parametrize("boundary", ["timer", "refresh"])
@pytest.mark.parametrize("failure", ["external_edit", "renamed_board"])
def test_native_boundaries_observe_changes_without_display_validation_loops(
    window_ui: Any, boundary: str, failure: str
) -> None:
    """Explicit refresh events update native edits or disable a replaced board once."""

    def check(ui: Any) -> None:
        c = ui.controller
        c.timer.Stop()
        if failure == "external_edit":
            ui.board.parts[0].AddVariant("A").SetDNP(True)
        else:
            ui.board.GetFileName = lambda: str(ui.path / "renamed.kicad_pcb")

        def show_error(message: str, *_args: Any) -> None:
            assert not c.view._mutations_enabled
            assert not ui.dialog.generate_button.IsEnabled()
            ui.messages.append(message)

        with (
            patch.object(ui.wx, "MessageBox", show_error),
            patch.object(
                c.session.adapter, "snapshot", wraps=c.session.adapter.snapshot
            ) as read,
        ):
            c._update_enabled()
            assert c.view._mutations_enabled and read.call_count == 0
            if boundary == "timer":
                c.timer.StartOnce(1)
                wait_until(
                    ui.wx,
                    lambda: not c.session.reliable
                    or not c.session.snapshot.get("component-1", "A").pop,
                )
            else:
                c.refresh()
        if failure == "external_edit":
            assert read.call_count == 1
            assert not c.session.snapshot.get("component-1", "A").pop
            assert c.model.snapshot is c.session.snapshot and c.session.reliable
            assert not ui.messages
        else:
            assert read.call_count == 0 and not c.session.reliable
            assert len(ui.messages) == 1 and "reopen" in ui.messages[0].lower()

    window_ui.run(check)


def test_supplier_completion_joins_captured_lcsc_without_native_snapshot_fanout(
    window_ui: Any,
) -> None:
    """Shared facts survive empty updates, never follow reassignment, and refetch on reopen."""
    window_ui.board.names = ["A", "B", *[f"V{i}" for i in range(10)]]
    window_ui.board.parts.extend(
        _SelectableFootprint(window_ui.board, f"component-{index}", f"R{index}")
        for index in range(2, 101)
    )

    def check(ui: Any) -> None:
        c = ui.controller
        snapshot = c.session.snapshot
        c.timer.Stop()
        assert c.assembly_lookup.pending == {"C1"} and len(ui.pending_threads) == 1
        ui.board.parts[0].SetField("Reference", "R1000")
        ui.board.parts[0].AddVariant("A").SetFieldValue("LCSC", "C900")
        ui.supplier.fetch_iter.return_value = iter(
            [
                ("C1", {"assembly_process": "SMT", "component_product_type": 0}),
                ("C1", {"assembly_process": "", "component_product_type": None}),
            ]
        )
        with patch.object(
            c.session.adapter,
            "snapshot",
            side_effect=AssertionError("supplier reread native data"),
        ):
            _complete(ui)
            assert set(ui.supplier.fetch_iter.call_args.args[0]) == {"C1"}
            assert c.model.get_value(0, c.model.column_for("A", "standard")) is False
            c.render()
            assert c.session.snapshot is snapshot
            for variant in snapshot.variants:
                assert all(
                    (row["assembly_process"], row["component_product_type"])
                    == ("SMT", 0)
                    for row in ui.cache.assembly_rows(snapshot, variant.name)
                )
        assert ui.cache.get_missing_metadata(snapshot) == set()
        with patch.object(
            c.session.adapter, "snapshot", wraps=c.session.adapter.snapshot
        ) as read:
            c.refresh()
        read.assert_called_once()
        row = next(
            row
            for row in ui.cache.assembly_rows(c.session.snapshot, "A")
            if row["reference"] == "R1000"
        )
        assert row["lcsc"] == "C900" and row["component_product_type"] is None
        assert all(
            row["component_product_type"] == 0
            for row in ui.cache.assembly_rows(c.session.snapshot, "B")
        )
        ui.supplier.fetch_iter.return_value = iter(())
        _complete(ui)
        assert not ui.messages

    def reopened(ui: Any) -> None:
        c = ui.controller
        assert ui.cache.get_missing_metadata(c.session.snapshot) == {"C1", "C900"}
        ui.supplier.fetch_iter.return_value = iter(
            [
                ("C1", {"assembly_process": "THT", "component_product_type": 2}),
                ("C900", {}),
            ]
        )
        c.timer.Stop()
        with patch.object(
            c.session.adapter,
            "snapshot",
            side_effect=AssertionError("supplier reread native data"),
        ):
            _complete(ui)
            assert c.model.get_value(0, c.model.column_for("B", "standard")) is True
            for variant in c.session.snapshot.variants:
                for row in ui.cache.assembly_rows(c.session.snapshot, variant.name):
                    if row["lcsc"] == "C900":
                        assert row["component_product_type"] is None
                    else:
                        assert (
                            row["assembly_process"],
                            row["component_product_type"],
                        ) == ("THT", 2)
        assert not Path(ui.cache.dbfile).exists() and not ui.messages

    window_ui.run(check, reopened)


@pytest.mark.parametrize("ending", ["drop", "unavailable_board", "close"])
def test_enrichment_completion_during_native_header_drag(
    window_ui: Any, ending: str
) -> None:
    def check(ui: Any) -> None:
        c, view = ui.controller, ui.controller.view
        h = MatrixHarness(ui.dialog, ui.wx, ui.view, ui.module, view=view)
        source, output, original = c.session.snapshot, c.session.output_variant, c.model
        current = ui.board.current
        destination = h.start_drag("B", "A")
        drag, table = view._header_drag, view.GetTable()
        widths = tuple(view.GetColSize(col) for col in range(view.GetNumberCols()))
        ui.supplier.fetch_iter.return_value = iter(
            [("C1", {"assembly_process": "SMT", "component_product_type": 2})]
        )
        _complete(ui)
        updated = c.model
        assert not ui.messages and updated is view.model and updated is not original
        assert updated.get_value(0, updated.column_for("A", "standard")) is True
        assert view._header_drag is drag and view.GetGridColLabelWindow().HasCapture()
        assert view.GetTable() is table
        assert (
            tuple(view.GetColSize(col) for col in range(view.GetNumberCols())) == widths
        )
        if ending == "unavailable_board":
            ui.board.GetFileName = lambda: str(ui.path / "another.kicad_pcb")
            c.refresh()
            assert not c.session.reliable and len(ui.messages) == 1
        elif ending == "close":
            c.close()
        h.header_event(ui.wx.wxEVT_LEFT_UP, destination)
        assert c.model.variant_order == (
            ("", "B", "A") if ending == "drop" else ("", "A", "B")
        )
        assert c.model is updated
        assert (
            view._header_drag is None and not view.GetGridColLabelWindow().HasCapture()
        )
        assert not view._header_drag_timer.IsRunning()
        assert c.session.snapshot.components == source.components
        assert c.session.output_variant == output and ui.board.current == current

    window_ui.run(check)


def test_real_matrix_display_order_edit_and_reopen(window_ui: Any) -> None:
    """Real constructor/control bindings preserve targets and restore saved display choices."""
    order = ("B", "", "A")

    def check(ui: Any) -> None:
        c, wx = ui.controller, ui.wx
        ui.board.parts.append(_SelectableFootprint(ui.board, "component-2", "R2"))
        c.refresh()
        h = MatrixHarness(ui.dialog, wx, ui.view, ui.module, view=c.view)
        checkbox = c.show_footprint_library
        assert checkbox.IsEnabled() and not checkbox.GetValue()
        assert checkbox.GetLabel() == "Show footprint library"
        h.drag("B", "")
        assert c.model.variant_order == order
        c.view.select_components(("component-1", "component-2"), "B")
        c.view.Scroll(30, 0)
        pump(wx)
        selection, scroll = h.selection(), c.view.GetViewStart()
        c.select_part()
        assignment = ui.dialog._part_selector.assignment_context
        _checkbox(ui, checkbox, True)
        assert (
            c.model.get_display(0, c.model.column_for(None, "footprint")) == "R:R0603"
        )
        assert h.selection() == selection and c.view.GetViewStart() == scroll
        assert ui.dialog._part_selector.assignment_context == assignment
        _output(ui, "")
        c.assign_parts(SimpleNamespace(assignment_context=assignment, lcsc="C2"))
        assert all(
            c.session.snapshot.get(component, "B").lcsc == "C2"
            for component in ("component-1", "component-2")
        )
        assert c.session.snapshot.get("component-1", "A").lcsc == "C1"
        assert c.model.variant_order == order

    def reopened(ui: Any) -> None:
        c = ui.controller
        assert c.model.show_footprint_library
        assert c.model.variant_order == order
        assert c.view.selected_target is None and not c.view.selected_component_ids()
        assert c.session.output_variant == "" and ui.board.current == "A"
        preferences = ui.cache.get_display_preferences()
        assert preferences["show_footprint_library"] is True
        assert preferences["variant_order"] == list(order)
        assert not {"selection", "target", "scroll"}.intersection(preferences)
        # Reset is a deliberate saved preference, not a native variant change.
        c.view.restore_preferences({"variant_order": c.model.variants})
        assert c.model.variant_order == ("", "A", "B")

    def reset(ui: Any) -> None:
        assert ui.controller.model.variant_order == ("", "A", "B")
        assert ui.controller.session.output_variant == "" and ui.board.current == "A"
        assert not ui.messages

    window_ui.run(check, reopened, reset)


@pytest.mark.parametrize(
    "preferences",
    [
        {"show_footprint_library": "false", "variant_order": "B,A"},
        {"show_footprint_library": 1},
    ],
)
def test_invalid_display_preferences_use_defaults(
    window_ui: Any, preferences: dict[str, Any]
) -> None:
    window_ui.settings["variants"] = {
        "boards": {
            str(Path(window_ui.board.GetFileName()).resolve()): {
                "display_preferences": preferences
            }
        }
    }

    def check(ui: Any) -> None:
        c = ui.controller
        assert (
            not c.show_footprint_library.GetValue()
            and not c.model.show_footprint_library
        )
        assert c.model.variant_order == ("", "A", "B")
        assert c.session.output_variant == "A" and ui.board.current == "A"
        assert not ui.board.modified and not ui.messages

    window_ui.run(check)


def test_controller_publishes_numeric_prices_and_recomputes_after_variant_edits(
    window_ui: Any,
) -> None:
    """Real render, assignment and flag handlers project current per-variant prices."""

    def check(ui: Any) -> None:
        ui.catalog.parts = {
            "C1": {"price": "1-9:0.0012341,10-:0.001"},
            "C2": {"price": "1-:0.0012342"},
            "C3": {"price": "1-:0"},
            "C404": {},
        }
        ui.dialog.bom_estimator_board_count = 5
        ui.controller.refresh()

        def price(variant: str) -> Any:
            amount = ui.controller.model._metadata_for("component-1", variant).price
            # The shared BOM evaluator multiplies floats before this projection.
            return (
                None if amount is None else pytest.approx(amount, rel=Decimal("1e-12"))
            )

        assert price("") == price("A") == price("B") == Decimal("0.0061705")
        target = focus(ui, "A", "lcsc")
        ui.controller._on_edit(target, "C2")
        assert price("A") == Decimal("0.0061710")
        assert price("B") == Decimal("0.0061705")

        ui.dialog.bom_estimator_board_count = 10
        ui.controller.render()
        assert price("A") == Decimal("0.0123420")
        assert price("B") == Decimal("0.010")

        focus(ui, "A", "pop")
        ui.controller.toggle(("pop",))
        assert price("A") == Decimal("0.0123420")
        focus(ui, "A", "bom")
        ui.controller.toggle(("bom",))
        assert price("A") is None
        ui.controller.toggle(("bom",))

        target = focus(ui, "A", "lcsc")
        ui.controller._on_edit(target, "C3")
        assert price("A") == 0.0
        ui.controller._on_edit(target, "C404")
        assert price("A") is None
        assert ui.messages == []

    window_ui.run(check)
