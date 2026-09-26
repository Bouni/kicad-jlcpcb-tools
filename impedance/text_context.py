"""Preserve native shown text without attaching a live PROJECT to a render copy.

Resolve source text first, copy only plain values, and mutate independently owned
counterparts only when their native shown text differs. Native read-back checks
guard against a second expansion/unescape pass or different textbox wrapping.
This is logically read-only for the source model; native font/text caches may be
consulted by const shown-text methods.
"""

from dataclasses import dataclass
import json
import re
from typing import Any

_TEXT_KINDS = {
    "PCB_TEXT",
    "PCB_TEXTBOX",
    "PCB_FIELD",
    "PCB_TABLECELL",
    "PCB_DIM_ALIGNED",
    "PCB_DIM_ORTHOGONAL",
    "PCB_DIM_RADIAL",
    "PCB_DIM_LEADER",
    "PCB_DIM_CENTER",
    "BARCODE",
    "PCB_BARCODE",
}
_EXPANSION = re.compile(
    r"\$\{|@\{|\{(?:dollar|brace|backslash)\}|<<<ESC_(?:DOLLAR|AT):|\x1b"
)


class TextContextError(RuntimeError):
    """Report unsupported or unpreservable live rendered text."""


@dataclass(frozen=True)
class ResolvedText:
    """Carry immutable text values across native board ownership boundaries."""

    item_id: str
    kind: str
    raw_text: str
    shown_text: str


def _kind(item: Any) -> str:
    """Use native class names without interpreting a SWIG repr or pointer address."""
    getter = getattr(item, "GetClass", None)
    return str(getter()) if callable(getter) else type(item).__name__


def _typed(item: Any, method: str) -> Any:
    """Cast only an untyped container result; typed fields lack generic Cast support."""
    if callable(getattr(item, method, None)):
        return item
    cast = getattr(item, "Cast", None)
    if callable(cast):
        item = cast()
    if not callable(getattr(item, method, None)):
        raise TextContextError(
            f"KiCad cannot read {_kind(item)} rendered text with these bindings."
        )
    return item


def _same_native_item(first: Any, second: Any) -> bool:
    """Reject shared native allocations even when SWIG creates separate wrappers."""
    if first is second:
        return True
    first_pointer, second_pointer = (
        getattr(first, "this", None),
        getattr(second, "this", None),
    )
    return (
        first_pointer is not None
        and second_pointer is not None
        and int(first_pointer) == int(second_pointer)
    )


def _literal_table(table: Any, pcbnew_module: Any) -> None:
    """Allow literal opaque tables, never assume a declared C++ vector is wrapped."""
    try:
        formatter = pcbnew_module.PCB_IO_KICAD_SEXPR()
        formatter.Format(table)
        serialized = str(formatter.GetStringOutput(True))
        if not serialized.strip() or _EXPANSION.search(serialized):
            raise ValueError("variable/expression-bearing cells cannot be resolved")
    except Exception as error:
        raise TextContextError(
            "Cannot preserve PCB table text with these KiCad bindings: "
            f"{error}. Use literal table cells or a runtime exposing typed table cells."
        ) from error


def _text_items(board: Any, pcbnew_module: Any) -> dict[str, Any]:
    """Enumerate typed text, footprint children and supported table cells once."""
    result: dict[str, Any] = {}

    def visit(item: Any) -> None:
        """Descend only through audited native container APIs."""
        kind = _kind(item)
        if kind == "FOOTPRINT":
            item = _typed(item, "GetFields")
            for child in (*item.GetFields(), *item.GraphicalItems()):
                visit(child)
            return
        if kind == "PCB_TABLE":
            if not isinstance(getattr(pcbnew_module, "PCB_TABLECELL", None), type):
                _literal_table(item, pcbnew_module)
                return
            item = _typed(item, "GetCells")
            for child in item.GetCells():
                visit(child)
            return
        if kind not in _TEXT_KINDS:
            getter = getattr(item, "GetText", None)
            if callable(getter) and _EXPANSION.search(str(getter())):
                raise TextContextError(
                    f"Cannot preserve unsupported variable-bearing {kind} text."
                )
            return
        item = _typed(item, "GetShownText")
        uuid = getattr(item, "m_Uuid", None)
        identifier = str(uuid.AsString()) if uuid is not None else ""
        if not identifier.strip():
            raise TextContextError(
                f"KiCad returned {kind} text without a stable item UUID."
            )
        if identifier in result:
            if not _same_native_item(result[identifier], item):
                raise TextContextError(f"Duplicate rendered text UUID: {identifier}.")
            return
        result[identifier] = item

    for method in ("GetDrawings", "GetFootprints"):
        getter = getattr(board, method, None)
        if not callable(getter):
            raise TextContextError(
                f"KiCad cannot read the {method} collection for rendered text."
            )
        for item in getter():
            visit(item)
    return result


def _shown_text(item: Any, kind: str) -> str:
    """Match native plotting's True argument; barcodes have a no-argument method."""
    result = (
        item.GetShownText()
        if kind in {"BARCODE", "PCB_BARCODE"}
        else item.GetShownText(True)
    )
    if not isinstance(result, str):
        raise TextContextError(f"KiCad returned non-text shown content for {kind}.")
    return result


def resolve_text_context(board: Any, pcbnew_module: Any) -> tuple[ResolvedText, ...]:
    """Read all current live project/variant values before any detached text writes."""
    try:
        result = []
        for identifier, item in sorted(_text_items(board, pcbnew_module).items()):
            kind = _kind(item)
            raw = item.GetText()
            if not isinstance(raw, str):
                raise TextContextError(
                    f"KiCad returned non-text raw content for {kind}."
                )
            result.append(ResolvedText(identifier, kind, raw, _shown_text(item, kind)))
        return tuple(result)
    except TextContextError:
        raise
    except Exception as error:
        raise TextContextError(f"Cannot resolve native PCB text: {error}") from error


def text_context_records(board: Any, pcbnew_module: Any) -> tuple[str, ...]:
    """Fingerprint live resolved strings, including unsaved project-variable edits."""
    return ("resolved-text:version:1",) + tuple(
        "resolved-text:"
        + json.dumps(
            (value.item_id, value.kind, value.raw_text, value.shown_text),
            ensure_ascii=True,
            separators=(",", ":"),
        )
        for value in resolve_text_context(board, pcbnew_module)
    )


def apply_text_context(
    source: Any,
    detached: Any,
    pcbnew_module: Any,
    values: tuple[ResolvedText, ...],
) -> None:
    """Materialize values only on verified independent counterparts, then read back."""
    try:
        if _same_native_item(source, detached):
            raise TextContextError(
                "PCB text capture requires an independent detached board."
            )
        originals = _text_items(source, pcbnew_module)
        targets = _text_items(detached, pcbnew_module)
        identifiers = {value.item_id for value in values}
        if (
            len(identifiers) != len(values)
            or identifiers != set(originals)
            or identifiers != set(targets)
        ):
            raise TextContextError(
                "Rendered text counterparts changed or disappeared during snapshotting."
            )
        changes = []
        for value in values:
            original, target = originals[value.item_id], targets[value.item_id]
            if _same_native_item(original, target):
                raise TextContextError(
                    "PCB text capture requires independent detached text items."
                )
            if _kind(original) != value.kind or _kind(target) != value.kind:
                raise TextContextError(
                    f"Rendered text counterpart type changed: {value.item_id}."
                )
            if (
                original.GetText() != value.raw_text
                or _shown_text(original, value.kind) != value.shown_text
            ):
                raise TextContextError(
                    "Source PCB text changed while creating its snapshot."
                )
            if _shown_text(target, value.kind) == value.shown_text:
                continue
            if value.kind.startswith("PCB_DIM_"):
                # SetText invokes virtual ClearRenderCache, which calls Update
                # for native dimensions and can regenerate text and geometry.
                raise TextContextError(
                    "Cannot preserve changed dimension text with these KiCad bindings. "
                    "Use a literal dimension label before impedance capture."
                )
            if _EXPANSION.search(value.shown_text):
                raise TextContextError(
                    f"Cannot safely preserve resolved {value.kind} text containing expansion/escape tokens."
                )
            if not callable(getattr(target, "SetText", None)):
                raise TextContextError(
                    f"KiCad cannot materialize detached {value.kind} text."
                )
            changes.append((target, value.shown_text))
        for target, shown in changes:
            target.SetText(shown)
        # Changing a footprint field can also affect an item skipped above.
        # Verify the complete set after all writes, not just each changed item.
        for value in values:
            if _shown_text(targets[value.item_id], value.kind) != value.shown_text:
                raise TextContextError(
                    f"Detached {value.kind} shown text does not match the live board "
                    "after materialization (wrapping, escaping or field dependency)."
                )
    except TextContextError:
        raise
    except Exception as error:
        raise TextContextError(f"Cannot preserve native PCB text: {error}") from error
