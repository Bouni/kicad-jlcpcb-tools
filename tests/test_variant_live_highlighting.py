"""Controller edits publish current difference styles before any timer refresh."""

from types import SimpleNamespace
from typing import Any

import pytest

from .native_window_support import focus, window_ui
from .native_wx_support import wait_until

__all__ = ["window_ui"]
pytestmark = pytest.mark.native_wx


def _assert_differences(ui: Any, expected: dict[str, set[str]]) -> None:
    """Check both styles used for actual rendering, with no refresh call."""
    model = ui.controller.model
    row = model.row_for_component("component-1")
    assert row is not None
    assert ui.controller.view.model is model
    assert model.snapshot.source_token == ui.controller.session.snapshot.source_token
    for column, spec in enumerate(model.columns):
        style = model.cell_style(row, column)
        fields = expected.get(spec.variant, set())
        assert style.different is (spec.key in fields), (spec.variant, spec.key)
        assert style.variant_different is bool(fields), (spec.variant, spec.key)
    assert not ui.messages


@pytest.mark.parametrize(
    "route,field,original,different",
    [
        ("cell", "bom", True, False),
        ("cell", "pop", True, False),
        ("default", "bom", True, False),
        ("default", "pop", True, False),
        ("toolbar", "pos", True, False),
        ("paste", "value", "10k", "22k"),
        ("paste", "lcsc", "C1", "C2"),
    ],
)
def test_edit_routes_publish_and_clear_both_highlights_synchronously(
    window_ui: Any, route: str, field: str, original: Any, different: Any
) -> None:
    def check(ui: Any) -> None:
        """Cell, toolbar, Default inheritance, and paste all update before returning."""
        if route == "default":
            record = ui.board.parts[0].AddVariant("A")
            if field in ("value", "lcsc"):
                record.SetFieldValue("Value" if field == "value" else "LCSC", original)
            ui.controller.refresh()
        target = focus(ui, "" if route == "default" else "A", field)
        _assert_differences(ui, {})
        for changed, value in ((True, different), (False, original)):
            previous = ui.controller.model
            if route == "toolbar":
                ui.controller.toggle((field,))
            elif route == "paste":
                assert ui.wx.TheClipboard.Open()
                try:
                    assert ui.wx.TheClipboard.SetData(ui.wx.TextDataObject(value))
                finally:
                    ui.wx.TheClipboard.Close()
                ui.controller.dispatch_action("paste", target)
            else:
                ui.controller._on_edit(target, value)
            assert ui.controller.model is not previous
            _assert_differences(ui, {"A": {field}} if changed else {})

    window_ui.run(check)


def test_assignment_and_use_base_publish_difference_styles_before_returning(
    window_ui: Any,
) -> None:
    def check(ui: Any) -> None:
        """Selector assignment and inherited restoration both rebuild the view now."""
        descriptions = {"C1": "Base resistor\n10 kΩ ±1%", "C2": "Precision 22 kΩ"}
        ui.catalog.parts = {
            code: {"description": text} for code, text in descriptions.items()
        }
        ui.controller.refresh()

        def tooltip(variant: str) -> str:
            model = ui.controller.model
            return model.cell_tooltip(0, model.column_for(variant, "lcsc"))

        target = focus(ui, "B", "lcsc")
        ui.controller.select_part()
        context = ui.dialog._part_selector.assignment_context
        ui.controller.assign_parts(
            SimpleNamespace(assignment_context=context, lcsc="C2")
        )
        _assert_differences(ui, {"B": {"lcsc"}})
        assert descriptions["C2"] in tooltip("B")
        assert "LCSC: explicit" in tooltip("B")
        assert all(descriptions["C1"] in tooltip(name) for name in ("", "A"))
        ui.controller.remove()
        _assert_differences(ui, {"B": {"lcsc"}})
        model = ui.controller.model
        assert model.get_value(0, model.column_for("B", "lcsc")) == ""
        assert all(text not in tooltip("B") for text in descriptions.values())
        ui.controller._on_edit(target, "C404")
        assert "Description unavailable" in tooltip("B")
        assert all(text not in tooltip("B") for text in descriptions.values())
        ui.controller.action_use_base(target)
        _assert_differences(ui, {})
        assert descriptions["C1"] in tooltip("B")
        assert "Inherited from Default" in tooltip("B")

    window_ui.run(check)


def test_restoring_last_difference_removes_row_from_differences_filter(
    window_ui: Any,
) -> None:
    def check(ui: Any) -> None:
        """Update filtering in the final edit's model publication."""
        target = focus(ui, "A", "pop")
        ui.controller._on_edit(target, False)
        ui.controller.differences.SetValue(True)
        ui.controller.render()
        assert len(ui.controller.model.rows) == 1
        ui.controller._on_edit(target, True)
        assert ui.controller.model.rows == ()
        assert ui.controller.view.model is ui.controller.model
        assert not ui.messages

    window_ui.run(check)


@pytest.mark.parametrize(
    ("attributes", "populated"),
    [(64, False), (32, True)],
    ids=["native-dnp", "just-added-is-populated"],
)
def test_native_attribute_flags_preserve_population_when_creating_a_variant(
    window_ui: Any, attributes: int, populated: bool
) -> None:
    def check(ui: Any) -> None:
        """Preserve native DNP and ignore JUST_ADDED when adding a BOM override."""
        footprint = ui.board.parts[0]
        footprint.attributes = attributes
        ui.controller.refresh()
        assert footprint.IsDNP() is (not populated)
        _assert_differences(ui, {})

        ui.controller._on_edit(focus(ui, "A", "bom"), False)

        for variant in ("", "A", "B"):
            assert (
                ui.controller.session.snapshot.get("component-1", variant).pop
                is populated
            )
        assert footprint.GetVariant("A").GetDNP() is (not populated)
        assert footprint.GetAttributes() == attributes
        _assert_differences(ui, {"A": {"bom"}})

        target = focus(ui, "A", "pop")
        ui.controller._on_edit(target, not populated)
        assert ui.controller.session.snapshot.get("component-1", "A").pop is (
            not populated
        )
        _assert_differences(ui, {"A": {"bom", "pop"}})

        ui.controller._on_edit(target, populated)
        assert ui.controller.session.snapshot.get("component-1", "A").pop is populated
        assert footprint.GetVariant("A").GetDNP() is (not populated)
        assert footprint.GetAttributes() == attributes
        _assert_differences(ui, {"A": {"bom"}})

    window_ui.run(check)


def _publish_standard(
    ui: Any, lcsc: str, classification: int, variant: str = ""
) -> None:
    """Wait for provider facts to reach the native grid through production callbacks."""
    ui.supplier.fetch_iter.return_value = iter(
        [(lcsc, {"assembly_process": "SMT", "component_product_type": classification})]
    )
    ui.run_worker()
    wait_until(
        ui.wx,
        lambda: ui.controller.model.get_value(
            0, ui.controller.model.column_for(variant, "standard")
        )
        is (classification == 2),
    )


def _assert_standard(ui: Any, variant: str, *, value: bool, different: bool) -> None:
    """Read actual Std values and every cue derived from its comparison."""
    model = ui.controller.model
    row = model.row_for_component("component-1")
    column = model.column_for(variant, "standard")
    assert model.get_value(row, column) is value
    assert model.get_display(row, column) == ("✓" if value else "—")
    style = model.cell_style(row, column)
    assert style.status == "known"
    assert style.different is different
    assert style.symbol == ("≠" if different else "")
    assert ("Different from Default" in model.cell_tooltip(row, column)) is different


@pytest.mark.parametrize(
    "toggled_variant,different", [("A", False), ("A", True), ("", True)]
)
def test_population_restores_only_eligible_standard_difference_cues(
    window_ui: Any, toggled_variant: str, different: bool
) -> None:
    """Unpopulating retains classification; repopulating restores relevant cues."""

    def check(ui: Any) -> None:
        facts = {"price": "1-:0.01", "stock": 1000, "type": "Basic"}
        ui.catalog.parts.update(C1=dict(facts), C2=dict(facts))
        ui.controller.refresh()
        _publish_standard(ui, "C1", 2)
        ui.board.parts[0].AddVariant("A").SetFieldValue("LCSC", "C1")
        ui.controller.refresh()
        if different:
            ui.controller._on_edit(focus(ui, toggled_variant, "lcsc"), "C2")
            _publish_standard(ui, "C2", 0, toggled_variant)
        _assert_differences(ui, {"A": {"lcsc", "standard"}} if different else {})
        target = focus(ui, toggled_variant, "pop")
        for populated in (False, True):
            ui.controller._on_edit(target, populated)
            for name in ("", "A", "B"):
                follows_toggled = name == toggled_variant or (
                    toggled_variant == "" and name == "B"
                )
                _assert_standard(
                    ui,
                    name,
                    value=not (different and follows_toggled),
                    different=different and populated and name == "A",
                )
            fields = {"lcsc"} if different else set()
            if not populated:
                fields.add("pop")
            elif different:
                fields.add("standard")
            _assert_differences(ui, {"A": fields} if fields else {})

    window_ui.run(check)
