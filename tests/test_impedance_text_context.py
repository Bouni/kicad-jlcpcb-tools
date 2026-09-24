"""Preserve live project/variant text in independently owned render snapshots."""

from copy import deepcopy
import json
from types import SimpleNamespace
from typing import Any

import pytest

from impedance.text_context import (
    TextContextError,
    apply_text_context,
    resolve_text_context,
    text_context_records,
)


class NativeText:
    """Resolve a real mutable raw string against source-only project variables."""

    def __init__(self, uid: str, raw: str, kind: str = "PCB_TEXT") -> None:
        self.uid, self.raw, self.kind = uid, raw, kind
        self.context = {"VERSION": "revision B", "PROJECTNAME": "RF board"}
        self.position = (1200, 3400)
        self.style = ("F.SilkS", 90, 1000)
        self.writes: list[str] = []
        self.visible = True

    @property
    def m_Uuid(self) -> Any:
        """Return stable native identity rather than a wrapper object address."""
        return SimpleNamespace(AsString=lambda: self.uid)

    def GetClass(self) -> str:
        """Use native GetClass spellings, including BARCODE's special spelling."""
        return self.kind

    def GetText(self) -> str:
        """Read the actual stored text, retaining variable syntax."""
        return self.raw

    def _shown(self) -> str:
        """Expand known variables and model one native brace-token decoding pass."""
        value = self.raw
        for key, replacement in self.context.items():
            value = value.replace("${" + key + "}", replacement)
        return value.replace("{brace}", "{")

    def GetShownText(self, allow_extra: bool) -> str:
        """Match the actual plotter's True parameter, not a guessed no-argument API."""
        assert allow_extra is True
        return self._shown()

    def SetText(self, value: str) -> None:
        """Change state so later native shown-text verification is meaningful."""
        self.writes.append(value)
        self.raw = value

    def IsVisible(self) -> bool:
        """Retain native field visibility independently of the stored text."""
        return self.visible

    def GetLayer(self) -> int:
        """Read the same mutable layer subsequently serialized by the fixture."""
        return {
            "F.Cu": 0,
            "B.Cu": 2,
            "In1.Cu": 4,
            "B.Adhes": 32,
            "F.Adhes": 33,
            "F.SilkS": 37,
            "B.SilkS": 36,
            "Edge.Cuts": 44,
            "F.Fab": 49,
            "F.CrtYd": 47,
            "B.CrtYd": 46,
            "Dwgs.User": 40,
            "Cmts.User": 41,
            "Eco1.User": 42,
            "Eco2.User": 43,
            "Rescue": 59,
        }[self.style[0]]

    def Cast(self) -> Any:
        """Already typed fields must never use the incomplete generic Cast switch."""
        raise AssertionError("Do not cast a typed text item")


class Barcode(NativeText):
    """Preserve PCB_BARCODE's different native shown-text signature."""

    def __init__(self, uid: str, raw: str) -> None:
        super().__init__(uid, raw, "BARCODE")

    def GetShownText(self) -> str:
        """Require no positional boolean, as in the installed native binding."""
        return self._shown()


class Footprint:
    """Expose all fields once and independent graphical children."""

    def __init__(self, fields: tuple[Any, ...], graphics: tuple[Any, ...]) -> None:
        self.fields, self.graphics = fields, graphics

    def GetClass(self) -> str:
        """Identify the native footprint container."""
        return "FOOTPRINT"

    def GetFields(self) -> tuple[Any, ...]:
        """Include reference, value and user fields exactly once."""
        return self.fields

    def GraphicalItems(self) -> tuple[Any, ...]:
        """Read footprint-local drawings."""
        return self.graphics


class Table:
    """Allow typed future bindings but also reproduce KiCad10's opaque cells."""

    def __init__(self, cells: tuple[Any, ...], serialized: str = "") -> None:
        self.cells, self.serialized = cells, serialized

    def GetClass(self) -> str:
        """Identify a table independently of text item types."""
        return "PCB_TABLE"

    def GetCells(self) -> tuple[Any, ...]:
        """Model a runtime with typed, iterable cells when explicitly enabled."""
        return self.cells


class Board:
    """Hold independent PCB containers without any project-attachment API."""

    def __init__(
        self, drawings: tuple[Any, ...] = (), footprints: tuple[Footprint, ...] = ()
    ) -> None:
        self.drawings, self.footprints = drawings, footprints

    def GetDrawings(self) -> tuple[Any, ...]:
        """Read the top-level native drawing collection."""
        return self.drawings

    def GetFootprints(self) -> tuple[Footprint, ...]:
        """Read footprint containers without mutating their contents."""
        return self.footprints


class NativePointer:
    """Expose only SWIG's terminal address conversion, without Python identity."""

    def __init__(self, address: int) -> None:
        self.address = address

    def __int__(self) -> int:
        """Different terminal wrappers may denote one native allocation."""
        return self.address


def native_pointer(address: int, depth: int) -> Any:
    """Model BOARD().this retaining another BOARD before the terminal pointer."""
    pointer: Any = NativePointer(address)
    for _ in range(depth):
        pointer = SimpleNamespace(this=pointer)
    return pointer


def module(*, typed_cells: bool = False) -> Any:
    """Offer only audited native APIs, with explicit table-cell availability."""

    class IO:
        text = ""

        def Format(self, item: Any) -> None:
            self.text = item.serialized

        def GetStringOutput(self, clear: bool) -> str:
            assert clear
            return self.text

    names = {
        0: "F.Cu",
        2: "B.Cu",
        4: "In1.Cu",
        32: "B.Adhes",
        33: "F.Adhes",
        37: "F.SilkS",
        36: "B.SilkS",
        44: "Edge.Cuts",
        49: "F.Fab",
        47: "F.CrtYd",
        46: "B.CrtYd",
        40: "Dwgs.User",
        41: "Cmts.User",
        42: "Eco1.User",
        43: "Eco2.User",
        59: "Rescue",
    }
    display_names = {
        "F.Adhes": "F.Adhesive",
        "B.Adhes": "B.Adhesive",
        "F.SilkS": "F.Silkscreen",
        "B.SilkS": "B.Silkscreen",
        "F.CrtYd": "F.Courtyard",
        "B.CrtYd": "B.Courtyard",
        "Dwgs.User": "User.Drawings",
        "Cmts.User": "User.Comments",
        "Eco1.User": "User.Eco1",
        "Eco2.User": "User.Eco2",
    }

    def display_name(layer_id: int) -> str:
        """Model LayerName's display strings independently from file-format names."""
        name = names[layer_id]
        return display_names.get(name, name)

    result = SimpleNamespace(
        PCB_IO_KICAD_SEXPR=IO,
        LayerName=display_name,
        LSET=SimpleNamespace(Name=names.__getitem__),
    )
    if typed_cells:
        result.PCB_TABLECELL = NativeText
    return result


@pytest.mark.parametrize(
    "kind",
    ["PCB_TEXT", "PCB_TEXTBOX", "PCB_FIELD"],
)
def test_materialization_matches_live_project_and_preserves_item_geometry(
    kind: str,
) -> None:
    """Resolve live values while retaining positions, styles, identity and source text."""
    source_item = NativeText("label", "${PROJECTNAME}: ${VERSION}", kind)
    source = Board((source_item,))
    detached = deepcopy(source)
    detached.drawings[0].context = {}
    values = resolve_text_context(source, module())
    assert source_item.writes == []
    apply_text_context(source, detached, module(), values)
    target = detached.drawings[0]
    assert target.GetText() == "RF board: revision B"
    assert target.GetShownText(True) == source_item.GetShownText(True)
    assert target.position == source_item.position
    assert target.style == source_item.style
    assert target.uid == source_item.uid
    assert source_item.GetText() == "${PROJECTNAME}: ${VERSION}"
    assert source_item.writes == []


def test_footprint_fields_graphics_tables_and_barcode_are_enumerated() -> None:
    """Exercise all audited containers and the special barcode call signature."""
    field = NativeText("field", "${VERSION}", "PCB_FIELD")
    graphic = NativeText("graphic", "${VERSION}", "PCB_TEXTBOX")
    cell = NativeText("cell", "${VERSION}", "PCB_TABLECELL")
    barcode = Barcode("barcode", "${VERSION}")
    source = Board((barcode,), (Footprint((field,), (graphic, Table((cell,)))),))
    detached = deepcopy(source)
    target_items = (
        detached.drawings[0],
        detached.footprints[0].fields[0],
        detached.footprints[0].graphics[0],
        detached.footprints[0].graphics[1].cells[0],
    )
    for item in target_items:
        item.context = {}
    api = module(typed_cells=True)
    values = resolve_text_context(source, api)
    assert len(values) == 4
    apply_text_context(source, detached, api, values)
    assert all(item.GetText() == "revision B" for item in target_items)
    assert all(item.writes == [] for item in (field, graphic, cell, barcode))


def test_text_records_detect_unsaved_project_changes_but_ignore_item_order() -> None:
    """Invalidate review on live project changes even when raw text is identical."""
    first = NativeText("one", "${VERSION}")
    second = Barcode("two", "literal")
    board = Board((first, second))
    original = text_context_records(board, module())
    board.drawings = (second, first)
    assert text_context_records(board, module()) == original
    first.context["VERSION"] = "unsaved revision C"
    assert first.GetText() == "${VERSION}"
    assert text_context_records(board, module()) != original
    assert all(item.writes == [] for item in board.drawings)


def test_hidden_field_edits_do_not_change_review_text_records() -> None:
    """Adding, editing, clearing and deleting unshown metadata preserves review."""
    footprint = Footprint((), ())
    board = Board(footprints=(footprint,))
    original = text_context_records(board, module())
    field = NativeText("new-lcsc-field", "C123", "PCB_FIELD")
    field.visible = False
    footprint.fields = (field,)
    for value in ("C123", "C456", ""):
        field.SetText(value)
        assert text_context_records(board, module()) == original
    footprint.fields = ()
    assert text_context_records(board, module()) == original


@pytest.mark.parametrize("kind", ["PCB_TEXT", "PCB_FIELD", "PCB_TEXTBOX"])
@pytest.mark.parametrize("layer", ["F.Fab", "F.CrtYd", "Dwgs.User"])
def test_nonplotted_text_is_not_an_approval_dependency(kind: str, layer: str) -> None:
    """Layer exclusion applies to fields and both board and footprint text."""
    item = NativeText("nonplotted", "${VERSION}", kind)
    item.style = (layer, 90, 1000)
    board = Board((item,))
    original = text_context_records(board, module())
    item.context["VERSION"] = "unplotted revision"
    item.SetText("${VERSION} changed")
    assert text_context_records(board, module()) == original
    assert (
        resolve_text_context(board, module())[0].shown_text
        == "unplotted revision changed"
    )
    item.style = ("F.SilkS", 90, 1000)
    assert text_context_records(board, module()) != original
    item.style = (layer, 90, 1000)
    assert text_context_records(board, module()) == original


@pytest.mark.parametrize("kind", ["PCB_TEXT", "PCB_FIELD", "PCB_TEXTBOX"])
@pytest.mark.parametrize(
    "layer",
    [
        "F.CrtYd",
        "B.CrtYd",
        "Dwgs.User",
        "Cmts.User",
        "Eco1.User",
        "Eco2.User",
        "F.Adhes",
        "B.Adhes",
    ],
)
def test_canonical_nonplotted_layers_override_native_display_names(
    kind: str, layer: str
) -> None:
    """Match KiCad's distinct LayerName display and LSET.Name serialized strings."""
    api = module()
    item = NativeText("native-layer", "${VERSION}", kind)
    item.style = (layer, 90, 1000)
    assert api.LSET.Name(item.GetLayer()) == layer
    assert api.LayerName(item.GetLayer()) != layer
    board = Board((item,))
    empty = text_context_records(Board(), api)
    assert text_context_records(board, api) == empty
    item.SetText("changed unplotted text")
    assert text_context_records(board, api) == empty
    assert resolve_text_context(board, api)[0].shown_text == "changed unplotted text"


@pytest.mark.parametrize(
    "layer", ["F.Cu", "B.Cu", "In1.Cu", "F.SilkS", "B.SilkS", "Edge.Cuts"]
)
def test_canonical_capture_layers_remain_text_dependencies(layer: str) -> None:
    """Canonical lookup must retain copper, both silkscreens and the outline."""
    item = NativeText("capture-layer", "${VERSION}")
    item.style = (layer, 90, 1000)
    api = module()
    board = Board((item,))
    before = text_context_records(board, api)
    assert before != text_context_records(Board(), api)
    item.SetText("changed plotted text")
    assert text_context_records(board, api) != before


def test_canonical_layer_lookup_does_not_use_translated_display_names() -> None:
    """Rescue's translated display string cannot change canonical classification."""
    api = module()
    api.LayerName = lambda layer_id: "Rettung"
    item = NativeText("rescue", "unplotted")
    item.style = ("Rescue", 90, 1000)
    assert text_context_records(Board((item,)), api) == text_context_records(
        Board(), api
    )


@pytest.mark.parametrize("canonical_api", [None, SimpleNamespace(Name=None)])
def test_missing_canonical_layer_api_retains_unknown_display_names(
    canonical_api: Any,
) -> None:
    """A missing binding cannot turn an unfamiliar display label into an omission."""
    api = module()
    api.LSET = canonical_api
    item = NativeText("unknown-layer", "original")
    item.style = ("F.CrtYd", 90, 1000)
    board = Board((item,))
    before = text_context_records(board, api)
    assert before != text_context_records(Board(), api)
    item.SetText("changed")
    assert text_context_records(board, api) != before


def test_unknown_canonical_layer_is_retained_despite_known_display_label() -> None:
    """An unfamiliar canonical layer is never reclassified using a display alias."""
    api = module()
    api.LSET.Name = lambda layer_id: "Future.Native.Layer"
    api.LayerName = lambda layer_id: "F.Fab"
    item = NativeText("unknown-canonical-layer", "original")
    board = Board((item,))
    before = text_context_records(board, api)
    assert before != text_context_records(Board(), api)
    item.SetText("changed")
    assert text_context_records(board, api) != before


def test_missing_layer_name_apis_retain_text_dependencies() -> None:
    """Unavailable naming APIs provide no evidence that text is unplotted."""
    api = module()
    api.LSET, api.LayerName = None, None
    item = NativeText("missing-layer-api", "original")
    item.style = ("F.Fab", 90, 1000)
    assert text_context_records(Board((item,)), api) != text_context_records(
        Board(), api
    )


def test_nonplotted_field_consumed_by_plotted_text_remains_a_dependency() -> None:
    """The consumer's native shown text keeps its omitted field dependency."""
    field = NativeText("value", "revision B", "PCB_FIELD")
    field.style = ("F.Fab", 90, 1000)

    class Consumer(NativeText):
        """Resolve the actual current field instead of recording setter calls."""

        def _shown(self) -> str:
            return field.GetText()

    consumer = Consumer("consumer", "${VALUE}")
    board = Board(footprints=(Footprint((field,), (consumer,)),))
    original = text_context_records(board, module())
    field.SetText("revision C")
    assert text_context_records(board, module()) != original
    assert field.GetText() == "revision C"
    assert consumer.writes == []


def test_hidden_field_values_remain_available_for_render_materialization() -> None:
    """Fingerprint exclusions must not discard hidden live text from the copy."""
    field = NativeText("lcsc", "${VERSION}", "PCB_FIELD")
    field.visible = False
    source = Board(footprints=(Footprint((field,), ()),))
    detached = deepcopy(source)
    detached.footprints[0].fields[0].context = {}
    values = resolve_text_context(source, module())
    assert len(values) == 1
    apply_text_context(source, detached, module(), values)
    assert detached.footprints[0].fields[0].GetText() == "revision B"
    assert field.GetText() == "${VERSION}"
    assert field.writes == []


def test_field_visibility_and_visible_text_edits_change_review_records() -> None:
    """Only an unshown field is excluded; both visibility transitions matter."""
    field = NativeText("field", "C123", "PCB_FIELD")
    board = Board(footprints=(Footprint((field,), ()),))
    visible = text_context_records(board, module())
    field.SetText("C456")
    changed = text_context_records(board, module())
    assert changed != visible
    field.visible = False
    hidden = text_context_records(board, module())
    assert hidden != changed
    field.visible = True
    assert text_context_records(board, module()) == changed


@pytest.mark.parametrize("visibility", [None, 0, "false"])
def test_unavailable_field_visibility_cannot_exclude_review_text(
    visibility: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown native visibility must fail rather than omit potentially shown text."""
    field = NativeText("field", "C123", "PCB_FIELD")
    monkeypatch.setattr(field, "IsVisible", lambda: visibility)
    with pytest.raises(TextContextError, match="visibility"):
        text_context_records(Board(footprints=(Footprint((field,), ()),)), module())


@pytest.mark.parametrize("raw", ["literal", "{brace}brace}", r"\${UNKNOWN}"])
def test_already_identical_native_text_is_not_rewritten(raw: str) -> None:
    """Avoid double unescaping or rewriting an already faithful literal."""
    source = Board((NativeText("text", raw),))
    detached = deepcopy(source)
    apply_text_context(
        source, detached, module(), resolve_text_context(source, module())
    )
    assert detached.drawings[0].writes == []
    assert detached.drawings[0].GetText() == raw


@pytest.mark.parametrize(
    "replacement",
    [
        "${SECOND}",
        "@{1+2}",
        "<<<ESC_DOLLAR:NAME>>>",
        "<<<ESC_AT:1+2>>>",
        "{brace}brace}",
    ],
)
def test_unsafe_resolved_expansion_tokens_fail_before_any_detached_write(
    replacement: str,
) -> None:
    """Do not feed still-active expansions into a second native resolution pass."""
    item = NativeText("label", "${VERSION}")
    item.context["VERSION"] = replacement
    source = Board((item,))
    detached = deepcopy(source)
    detached.drawings[0].context = {}
    with pytest.raises(TextContextError, match="safely preserve"):
        apply_text_context(
            source, detached, module(), resolve_text_context(source, module())
        )
    assert item.writes == []
    assert detached.drawings[0].writes == []


def test_different_textbox_wrapping_is_rejected_by_native_readback() -> None:
    """Reject a snapshot if native SetText changes the final displayed line breaks."""
    item = NativeText("label", "${VERSION}", "PCB_TEXTBOX")
    source = Board((item,))
    detached = deepcopy(source)
    target = detached.drawings[0]
    target.context = {}

    def rewrap(value: str) -> None:
        """Model a native text box producing different line breaks after SetText."""
        target.writes.append(value)
        target.raw = value.replace(" ", "\n")

    target.SetText = rewrap
    with pytest.raises(TextContextError, match="does not match"):
        apply_text_context(
            source, detached, module(), resolve_text_context(source, module())
        )
    assert item.writes == []


@pytest.mark.parametrize("failure", ["missing", "duplicate", "wrong-type", "alias"])
def test_invalid_detached_counterparts_fail_before_writing(failure: str) -> None:
    """Check the complete identity/type map before changing any detached item."""
    source = Board((NativeText("a", "${VERSION}"), NativeText("b", "${VERSION}")))
    detached = deepcopy(source)
    for item in detached.drawings:
        item.context = {}
    if failure == "missing":
        detached.drawings = detached.drawings[:1]
    elif failure == "duplicate":
        detached.drawings[1].uid = "a"
    elif failure == "wrong-type":
        detached.drawings[1].kind = "PCB_TEXTBOX"
    else:
        detached.drawings = (detached.drawings[0], source.drawings[1])
    with pytest.raises(TextContextError):
        apply_text_context(
            source, detached, module(), resolve_text_context(source, module())
        )
    assert all(item.writes == [] for item in (*source.drawings, *detached.drawings))


def test_source_board_cannot_be_its_own_detached_target() -> None:
    """An accidental live-board target must fail without a single text write."""
    source = Board((NativeText("label", "${VERSION}"),))
    with pytest.raises(TextContextError, match="independent"):
        apply_text_context(
            source, source, module(), resolve_text_context(source, module())
        )
    assert source.drawings[0].writes == []


@pytest.mark.parametrize("raw", ["${VERSION}", "@{1+2}", "{dollar}{brace}VERSION}"])
def test_opaque_native_tables_with_expansions_fail_closed(raw: str) -> None:
    """Never mistake an opaque cell-vector signature for a usable native binding."""
    table = Table((), f"(table (cell {json.dumps(raw)}))")
    with pytest.raises(TextContextError, match="table.*bindings"):
        resolve_text_context(Board((table,)), module())


def test_opaque_literal_table_is_preserved_without_guessing_cell_api() -> None:
    """Keep ordinary literal table geometry without touching opaque cell pointers."""
    table = Table((), '(table (cell "Literal RF notes"))')

    def forbidden() -> Any:
        raise AssertionError("Opaque cell vector must not be dereferenced")

    table.GetCells = forbidden
    assert resolve_text_context(Board((table,)), module()) == ()


def test_unknown_variable_bearing_text_type_is_an_actionable_error() -> None:
    """Require an explicit API contract before materializing a future text type."""
    item = NativeText("future", "${VERSION}", "PCB_FUTURE_TEXT")
    with pytest.raises(TextContextError, match="unsupported.*PCB_FUTURE_TEXT"):
        resolve_text_context(Board((item,)), module())


def test_native_resolution_failure_is_not_silently_dropped() -> None:
    """Propagate native project-resolution errors instead of showing stale labels."""
    item = NativeText("label", "${VERSION}")

    def broken(_allow_extra: bool) -> str:
        raise RuntimeError("project resolver failed")

    item.GetShownText = broken
    with pytest.raises(TextContextError, match="project resolver failed"):
        resolve_text_context(Board((item,)), module())


@pytest.mark.parametrize(
    "kind",
    [
        "PCB_DIM_ALIGNED",
        "PCB_DIM_ORTHOGONAL",
        "PCB_DIM_RADIAL",
        "PCB_DIM_LEADER",
        "PCB_DIM_CENTER",
    ],
)
def test_changed_dimension_text_fails_before_native_setter_can_regenerate_geometry(
    kind: str,
) -> None:
    """Native dimension SetText can call Update through its virtual cache clearer."""
    item = NativeText("dimension", "${VERSION}", kind)
    source = Board((item,))
    detached = deepcopy(source)
    target = detached.drawings[0]
    target.context = {}

    def regenerate(value: str) -> None:
        """Represent native Update restoring computed dimension text and geometry."""
        target.writes.append(value)
        target.raw = "100 mm"
        target.position = (999, 999)

    target.SetText = regenerate
    values = resolve_text_context(source, module())
    with pytest.raises(TextContextError, match="dimension.*bindings"):
        apply_text_context(source, detached, module(), values)
    assert target.writes == []
    assert target.position == item.position
    assert item.writes == []


def test_matching_dimension_text_is_kept_without_rebuilding_it() -> None:
    """Ordinary dimension labels remain unchanged when both native views agree."""
    source = Board((NativeText("dimension", "100 mm", "PCB_DIM_ALIGNED"),))
    detached = deepcopy(source)
    apply_text_context(
        source, detached, module(), resolve_text_context(source, module())
    )
    assert detached.drawings[0].writes == []


def test_untyped_native_container_results_are_cast_but_typed_fields_are_not() -> None:
    """The installed generic Cast switch supports PCB_TEXT, but not PCB_FIELD."""
    source_item = NativeText("label", "${VERSION}")
    target_item = deepcopy(source_item)
    target_item.context = {}

    def wrapper(item: NativeText) -> Any:
        """Return only the BOARD_ITEM API until the specific native cast is made."""
        return SimpleNamespace(GetClass=item.GetClass, Cast=lambda: item)

    source, detached = Board((wrapper(source_item),)), Board((wrapper(target_item),))
    apply_text_context(
        source, detached, module(), resolve_text_context(source, module())
    )
    assert target_item.GetText() == "revision B"
    assert source_item.writes == []


@pytest.mark.parametrize("alias", ["board", "item"])
@pytest.mark.parametrize("depths", [(0, 0), (1, 0), (0, 1), (2, 3)])
def test_different_python_wrappers_for_same_native_pointer_cannot_be_targets(
    alias: str, depths: tuple[int, int]
) -> None:
    """Python object identity alone does not prove SWIG allocation independence."""
    source = Board((NativeText("label", "${VERSION}"),))
    detached = deepcopy(source)
    first, second = (
        (source, detached)
        if alias == "board"
        else (source.drawings[0], detached.drawings[0])
    )
    first.this = native_pointer(451, depths[0])
    second.this = native_pointer(451, depths[1])
    with pytest.raises(TextContextError, match="independent"):
        apply_text_context(
            source, detached, module(), resolve_text_context(source, module())
        )
    assert all(item.writes == [] for item in (*source.drawings, *detached.drawings))


@pytest.mark.parametrize("depths", [(0, 0), (1, 0), (0, 1), (2, 3)])
def test_independent_nested_native_wrappers_preserve_live_text(
    depths: tuple[int, int],
) -> None:
    """A nested source board and text proxy can safely target separate allocations."""
    source = Board((NativeText("label", "${VERSION}"),))
    detached = deepcopy(source)
    target = detached.drawings[0]
    target.context = {}
    source.this = native_pointer(451, depths[0])
    detached.this = native_pointer(452, depths[1])
    source.drawings[0].this = native_pointer(987, depths[0])
    target.this = native_pointer(988, depths[1])

    apply_text_context(
        source, detached, module(), resolve_text_context(source, module())
    )

    assert target.GetText() == "revision B"
    assert target.writes == ["revision B"]
    assert target.position == source.drawings[0].position
    assert target.style == source.drawings[0].style
    assert source.drawings[0].GetText() == "${VERSION}"
    assert source.drawings[0].writes == []


@pytest.mark.parametrize("item_kind", ["board", "item"])
@pytest.mark.parametrize(
    "failure",
    ["null", "zero", "opaque", "cycle", "deep", "missing", "text", "float", "bool"],
)
def test_unprovable_native_independence_fails_before_text_writes(
    item_kind: str, failure: str
) -> None:
    """Unsupported native pointers must never turn wrapper identity into permission."""
    source = Board((NativeText("label", "${VERSION}"),))
    detached = deepcopy(source)
    detached.drawings[0].context = {}
    first, second = (
        (source, detached)
        if item_kind == "board"
        else (source.drawings[0], detached.drawings[0])
    )
    first.this = native_pointer(451, 0)
    second.this = native_pointer(452, 0)
    if failure == "cycle":
        pointer = SimpleNamespace()
        pointer.this = pointer
    else:
        pointer = {
            "null": None,
            "zero": NativePointer(0),
            "opaque": object(),
            "deep": native_pointer(451, 40),
            "missing": NativePointer(451),
            "text": "451",
            "float": 451.5,
            "bool": True,
        }[failure]
    first.this = pointer
    if failure == "missing":
        del second.this

    with pytest.raises(TextContextError, match="native pointer"):
        apply_text_context(
            source, detached, module(), resolve_text_context(source, module())
        )

    assert source.drawings[0].GetText() == "${VERSION}"
    assert detached.drawings[0].GetText() == "${VERSION}"
    assert source.drawings[0].writes == detached.drawings[0].writes == []


def test_dynamic_python_adapters_do_not_acquire_native_pointer_attributes() -> None:
    """Do not query synthesized .this attributes on ordinary non-SWIG adapters."""

    class DynamicBoard(Board):
        """Model an adapter whose dynamic fallback is unrelated to native pointers."""

        def __getattr__(self, name: str) -> Any:
            """Reject probing a native-only attribute that the adapter never declared."""
            if name == "this":
                raise AssertionError("Dynamic native pointer lookup")
            raise AttributeError(name)

    source = DynamicBoard((NativeText("label", "${VERSION}"),))
    detached = deepcopy(source)
    detached.drawings[0].context = {}
    apply_text_context(
        source, detached, module(), resolve_text_context(source, module())
    )
    assert detached.drawings[0].GetText() == "revision B"
    assert source.drawings[0].writes == []


def test_duplicate_text_uuid_wrappers_use_terminal_native_identity() -> None:
    """Repeated enumeration of one allocation remains one resolved text record."""
    first = NativeText("label", "${VERSION}")
    second = deepcopy(first)
    first.this, second.this = native_pointer(987, 2), native_pointer(987, 0)
    values = resolve_text_context(Board((first, second)), module())
    assert len(values) == 1
    assert values[0].shown_text == "revision B"
    assert first.writes == second.writes == []


def test_source_edit_after_resolution_invalidates_application_before_writes() -> None:
    """A stale text snapshot must not overwrite a newer source intent in the image."""
    source = Board((NativeText("label", "${VERSION}"),))
    detached = deepcopy(source)
    values = resolve_text_context(source, module())
    source.drawings[0].raw = "Changed label"
    with pytest.raises(TextContextError, match="Source PCB text changed"):
        apply_text_context(source, detached, module(), values)
    assert detached.drawings[0].writes == []


def test_final_readback_checks_initially_matching_dependent_text_too() -> None:
    """A field setter may affect another item's native expansion after it was skipped."""
    field = NativeText("a-field", "${VERSION}", "PCB_FIELD")
    note = NativeText("b-note", "Existing note")
    source = Board((field, note))
    detached = deepcopy(source)
    target_field, target_note = detached.drawings
    target_field.context = {}

    def dependent_shown(allow_extra: bool) -> str:
        """Model a dependent rendered label that changes when the field materializes."""
        assert allow_extra is True
        return "Changed dependency" if target_field.writes else "Existing note"

    target_note.GetShownText = dependent_shown
    with pytest.raises(TextContextError, match="does not match"):
        apply_text_context(
            source, detached, module(), resolve_text_context(source, module())
        )
    assert target_note.writes == []
    assert field.writes == []
    assert note.writes == []


@pytest.mark.parametrize("method", ["GetDrawings", "GetFootprints"])
def test_missing_native_collections_cannot_look_like_an_empty_board(
    method: str,
) -> None:
    """An unsupported/broken native handle must not produce a successful fingerprint."""
    source = Board()
    setattr(source, method, None)
    with pytest.raises(TextContextError, match="collection"):
        resolve_text_context(source, module())


def test_unsaved_project_edit_after_resolution_blocks_application() -> None:
    """Recheck source shown text because project-variable edits do not alter raw text."""
    source = Board((NativeText("label", "${VERSION}"),))
    detached = deepcopy(source)
    values = resolve_text_context(source, module())
    source.drawings[0].context["VERSION"] = "new revision"
    assert source.drawings[0].GetText() == values[0].raw_text
    with pytest.raises(TextContextError, match="Source PCB text changed"):
        apply_text_context(source, detached, module(), values)
    assert detached.drawings[0].writes == []
