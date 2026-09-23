"""Apply ordinary assembly edits to native footprints as one recoverable batch."""

from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Optional, Union

from .footprint_helpers import EXCLUDE_FROM_BOM, EXCLUDE_FROM_POS, set_lcsc_value
from .lcsc import is_lcsc_part, normalize_lcsc
from .part_assignments import is_assignment_alias

_FLAG_BITS = {
    "exclude_from_bom": EXCLUDE_FROM_BOM,
    "exclude_from_pos": EXCLUDE_FROM_POS,
}


class BoardEditRecoveryError(RuntimeError):
    """Report an edit failure whose native footprint state could not be restored."""


@dataclass(frozen=True)
class _FieldState:
    text: str
    visible: Optional[bool]


@dataclass(frozen=True)
class _FootprintState:
    fields: dict[str, _FieldState]
    attributes: int
    legacy: bool


def _capture(fp: Any) -> _FootprintState:
    """Read raw values, preserving conflicting aliases and field visibility."""
    try:
        fields = list(fp.GetFields())
    except AttributeError:
        return _FootprintState(
            {
                str(name): _FieldState(str(text), None)
                for name, text in fp.GetProperties().items()
            },
            int(fp.GetAttributes()),
            True,
        )
    captured = {}
    for field in fields:
        name = str(field.GetName())
        if name in captured:
            raise ValueError(f"Duplicate native footprint field: {name}")
        is_visible = getattr(field, "IsVisible", None)
        captured[name] = _FieldState(
            str(field.GetText()), bool(is_visible()) if callable(is_visible) else None
        )
    return _FootprintState(captured, int(fp.GetAttributes()), False)


def _prepare(
    before: _FootprintState, changes: dict[str, Union[str, bool]]
) -> tuple[_FootprintState, dict[str, Union[str, bool]]]:
    """Validate all requested values and calculate the exact expected native state."""
    unknown = set(changes) - {"lcsc", *_FLAG_BITS}
    if unknown:
        raise ValueError(f"Unknown board part edit: {', '.join(sorted(unknown))}")
    fields = dict(before.fields)
    attributes = before.attributes
    normalized = dict(changes)
    for key, value in changes.items():
        if key == "lcsc":
            if not isinstance(value, str):
                raise TypeError("LCSC assignment requires text")
            value = normalize_lcsc(value)
            if value and not is_lcsc_part(value):
                raise ValueError("LCSC must be empty or a C followed by digits")
            normalized[key] = value
            names = [name for name in fields if is_assignment_alias(name)]
            for name in names or ["LCSC"]:
                old = fields.get(name)
                visible = old.visible if old else None if before.legacy else False
                fields[name] = _FieldState(value, visible)
        else:
            if type(value) is not bool:
                raise TypeError(f"{key} requires a boolean exclusion value")
            mask = 1 << _FLAG_BITS[key]
            attributes = attributes | mask if value else attributes & ~mask
    return _FootprintState(fields, attributes, before.legacy), normalized


def _matches(fp: Any, expected: _FootprintState) -> bool:
    """Verify every native value, allowing unavailable visibility accessors."""
    actual = _capture(fp)
    if (
        actual.attributes != expected.attributes
        or actual.fields.keys() != expected.fields.keys()
    ):
        return False
    return all(
        actual.fields[name].text == state.text
        and (
            actual.fields[name].visible is None
            or actual.fields[name].visible == state.visible
        )
        for name, state in expected.fields.items()
    )


def _remove_field(fp: Any, field: Any) -> None:
    """Remove a field introduced by an unsuccessful assignment."""
    remove = getattr(fp, "Remove", None)
    if callable(remove):
        remove(field)
    else:
        fp.RemoveField(field.GetName())


def _restore_fields(fp: Any, before: _FootprintState) -> None:
    """Restore each raw field independently of failures in neighboring fields."""
    if before.legacy:
        fp.SetProperties({name: state.text for name, state in before.fields.items()})
        return
    for name, state in before.fields.items():
        # Avoid requiring a working getter before restoring its captured value.
        # Native setters can mutate and then raise, so readback decides success.
        with suppress(Exception):
            fp.SetField(name, state.text)
    # Known names remain writable even if a collection readback temporarily
    # failed. Enumerate only after raw text has been restored.
    for field in list(fp.GetFields()):
        with suppress(Exception):
            state = before.fields.get(str(field.GetName()))
            if state is None:
                _remove_field(fp, field)
            elif state.visible is not None:
                field.SetVisible(state.visible)


def _restore(fp: Any, before: _FootprintState) -> bool:
    """Attempt every independent restoration, then verify the complete snapshot."""
    with suppress(Exception):
        fp.SetAttributes(before.attributes)
    with suppress(Exception):
        _restore_fields(fp, before)
    try:
        return _matches(fp, before)
    except Exception:
        return False


def apply_board_part_edits(
    edits: Sequence[tuple[Any, dict[str, Union[str, bool]]]],
) -> None:
    """Capture and validate a whole batch before writes; recover on any failure.

    The caller owns the PCB Editor undo boundary. A failed recovery means the
    caller must stop editing until the board is reopened and inspected.
    """
    prepared = []
    seen = set()
    for fp, changes in edits:
        if id(fp) in seen:
            raise ValueError("Combine changes for each footprint into one edit")
        seen.add(id(fp))
        before = _capture(fp)
        after, normalized = _prepare(before, changes)
        if before != after:
            prepared.append((fp, before, after, normalized))
    applied = []
    try:
        for fp, before, after, changes in prepared:
            applied.append((fp, before))
            if "lcsc" in changes:
                set_lcsc_value(fp, str(changes["lcsc"]))
            if after.attributes != before.attributes:
                fp.SetAttributes(after.attributes)
            if not _matches(fp, after):
                raise RuntimeError(
                    "Native board setter did not preserve the requested part state"
                )
        for fp, _before in applied:
            modified = getattr(fp, "SetModified", None)
            if callable(modified):
                modified()
    except Exception as exc:
        restored = [_restore(fp, before) for fp, before in reversed(applied)]
        if not all(restored):
            raise BoardEditRecoveryError(
                "Board edit recovery failed; reopen the plugin and inspect the board before editing"
            ) from exc
        raise
