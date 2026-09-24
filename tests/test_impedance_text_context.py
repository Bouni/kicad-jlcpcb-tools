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


def module(*, typed_cells: bool = False) -> Any:
    """Offer only audited native APIs, with explicit table-cell availability."""

    class IO:
        text = ""

        def Format(self, item: Any) -> None:
            self.text = item.serialized

        def GetStringOutput(self, clear: bool) -> str:
            assert clear
            return self.text

    result = SimpleNamespace(PCB_IO_KICAD_SEXPR=IO)
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
def test_different_python_wrappers_for_same_native_pointer_cannot_be_targets(
    alias: str,
) -> None:
    """Python object identity alone does not prove SWIG allocation independence."""
    source = Board((NativeText("label", "${VERSION}"),))
    detached = deepcopy(source)
    if alias == "board":
        source.this, detached.this = 451, 451
    else:
        source.drawings[0].this, detached.drawings[0].this = 987, 987
    with pytest.raises(TextContextError, match="independent"):
        apply_text_context(
            source, detached, module(), resolve_text_context(source, module())
        )
    assert all(item.writes == [] for item in (*source.drawings, *detached.drawings))


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
