"""Read saved Default assignments without native loaders or editor board caches.

This deliberately reads only identity and assignment evidence from KiCad 7–10
PCB S-expressions. Unsupported or ambiguous evidence raises ValueError, retaining
legacy recovery data. Geometry is not interpreted, and this module never writes.
"""

import os
from pathlib import Path
import re
from typing import Union
from uuid import UUID

from .legacy_board_coverage import SavedBoardPart
from .part_assignments import resolve_assignment
from .schematic_fields import _TOKEN, _arguments, _Atom, _Form, _forms

# Stable KiCad 7 through KiCad 10.0; future formats need an explicit review.
_FIRST_VERSION = 20221018
_LAST_VERSION = 20260206
# Native parseBOARD/parseFOOTPRINT section names, with bodies intentionally opaque
# except for the identity and Default assignment evidence read below. Unknown
# names must not silently turn a footprint owner or exclusion flag into absence.
_BOARD_SECTIONS = frozenset(
    """version host generator generator_version general paper title_block layers
    setup property variants net net_class gr_arc gr_curve gr_line gr_poly gr_circle
    gr_rect image barcode gr_text gr_text_box table dimension footprint segment arc
    group generated via zone target point embedded_fonts embedded_files""".split()
)
_FOOTPRINT_SECTIONS = frozenset(
    """version generator generator_version locked placed layer stackup tedit tstamp
    uuid at descr tags property path sheetname sheetfile units autoplace_cost90
    autoplace_cost180 private_layers net_tie_pad_groups duplicate_pad_numbers_are_jumpers
    jumper_pad_groups solder_mask_margin solder_paste_margin solder_paste_ratio
    solder_paste_margin_ratio clearance zone_connect thermal_width thermal_gap attr
    fp_text fp_text_box table fp_arc fp_circle fp_curve fp_rect fp_line fp_poly image
    barcode dimension pad model zone group point embedded_fonts embedded_files
    component_classes variant""".split()
)
_ATTRIBUTES = frozenset(
    {
        "through_hole",
        "smd",
        "board_only",
        "exclude_from_pos_files",
        "exclude_from_bom",
        "allow_missing_courtyard",
        "allow_soldermask_bridges",
        "dnp",
        "virtual",
    }
)
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")


def _gap(text: str, start: int, end: int, flags: frozenset[str] = frozenset()) -> None:
    """Reject atoms discarded by the balanced-form parser, except known flags."""
    for token in _TOKEN.finditer(text, start, end):
        if token.group("comment") is None and token.group() not in flags:
            raise ValueError("Unexpected token in saved PCB assignment evidence")


def _parts(
    text: str, form: _Form, flags: frozenset[str] = frozenset()
) -> tuple[list[_Atom], tuple[_Form, ...]]:
    """Return leading arguments and direct children without ignoring stray atoms."""
    arguments = _arguments(text, form)
    if any("\0" in argument.text for argument in arguments):
        raise ValueError("NUL escapes are unsupported in saved PCB evidence")
    if any(
        "|" in argument.text and text[argument.start] != '"' for argument in arguments
    ):
        raise ValueError("Unquoted bar tokens are unsupported in saved PCB evidence")
    children = tuple(_forms(text, form.start + 1, form.end - 1))
    head = next(
        token
        for token in _TOKEN.finditer(text, form.start + 1, form.end - 1)
        if token.group("comment") is None
    )
    position = arguments[-1].end if arguments else head.end()
    for child in children:
        _gap(text, position, child.start, flags)
        position = child.end
    _gap(text, position, form.end - 1, flags)
    return arguments, children


def _scalar(text: str, form: _Form) -> str:
    """Require exactly one scalar argument for UUID and version evidence."""
    arguments, children = _parts(text, form)
    if len(arguments) != 1 or children:
        raise ValueError(f"Expected one saved PCB {form.name} value")
    return arguments[0].text


def _check_version(text: str, form: _Form) -> None:
    """Reject unsupported version authority in either board or footprint headers."""
    version = _scalar(text, form)
    if (
        not re.fullmatch(r"[0-9]{8}", version)
        or not _FIRST_VERSION <= int(version) <= _LAST_VERSION
    ):
        raise ValueError("Unsupported saved PCB version; expected KiCad 7–10")


def _identity(text: str, forms: tuple[_Form, ...]) -> str:
    """Accept full native UUIDs, rejecting random-repair and legacy-short cases."""
    identity = [form for form in forms if form.name in {"uuid", "tstamp"}]
    if len(identity) != 1:
        raise ValueError("Saved footprint requires exactly one UUID")
    raw = _scalar(text, identity[0])
    if not _UUID.fullmatch(raw) or not UUID(raw).int:
        raise ValueError("Saved footprint requires a nonempty full UUID")
    return str(UUID(raw))


def _item_name(identifier: str) -> str:
    """Match LIB_ID's first-colon split without emulating version-specific repair."""
    item = identifier.split(":", 1)[-1]
    if any(character in ':\\<>"' or ord(character) < 32 for character in item):
        raise ValueError(
            "Saved footprint library item requires unsupported native repair"
        )
    return item


def _text_field(text: str, form: _Form) -> tuple[str, str]:
    """Read legacy mandatory fp_text fields with KiCad's text-variable upgrade."""
    arguments, _children = _parts(text, form, frozenset({"hide"}))
    if not arguments or arguments[0].text not in {"reference", "value", "user"}:
        raise ValueError("Unsupported saved footprint text kind")
    value_index = 1
    if len(arguments) > 1 and text[arguments[1].start : arguments[1].end] == "locked":
        value_index += 1
    if len(arguments) <= value_index or any(
        text[arg.start : arg.end] != "hide" for arg in arguments[value_index + 1 :]
    ):
        raise ValueError("Missing or ambiguous saved footprint text")
    value = (
        arguments[value_index]
        .text.replace("%V", "${VALUE}")
        .replace("%R", "${REFERENCE}")
    )
    return arguments[0].text, value


def _footprint(text: str, form: _Form) -> SavedBoardPart:
    """Capture direct Default fields and flags, never nested variant overrides."""
    arguments, children = _parts(text, form, frozenset({"locked", "placed"}))
    if not arguments or any(
        text[arg.start : arg.end] not in {"locked", "placed"} for arg in arguments[1:]
    ):
        raise ValueError("Saved footprint requires one library identifier")
    item = _item_name(arguments[0].text)
    component_id = _identity(text, children)
    versions = [child for child in children if child.name == "version"]
    if len(versions) > 1:
        raise ValueError("Duplicate saved footprint version")
    if versions:
        _check_version(text, versions[0])
    properties: dict[str, str] = {}
    mandatory: dict[str, str] = {}
    attrs: set[str] = set()
    seen_attrs = False
    for child in children:
        if child.name not in _FOOTPRINT_SECTIONS:
            raise ValueError(f"Unsupported saved footprint section: {child.name!r}")
        if child.name == "property":
            args, _layout = _parts(text, child)
            if len(args) != 2 or args[0].text in properties:
                raise ValueError("Missing or duplicate saved footprint property")
            name, value = args[0].text, args[1].text
            properties[name] = value
            if name in {"Reference", "Value"}:
                if name in mandatory:
                    raise ValueError("Duplicate saved footprint reference or value")
                mandatory[name] = value
        elif child.name == "fp_text":
            kind, value = _text_field(text, child)
            if kind in {"reference", "value"}:
                name = kind.title()
                if name in mandatory:
                    raise ValueError("Duplicate saved footprint reference or value")
                mandatory[name] = value
        elif child.name == "attr":
            args, nested = _parts(text, child)
            values = [arg.text for arg in args]
            if (
                seen_attrs
                or nested
                or len(set(values)) != len(values)
                or not set(values) <= _ATTRIBUTES
            ):
                raise ValueError("Unsupported or ambiguous saved footprint attributes")
            seen_attrs = True
            attrs = set(values)
    if set(mandatory) != {"Reference", "Value"}:
        raise ValueError("Saved footprint requires an explicit reference and value")
    assignment, lcsc = resolve_assignment(properties, {}, "")
    return SavedBoardPart(
        component_id,
        mandatory["Reference"],
        mandatory["Value"],
        item,
        not bool(attrs & {"exclude_from_bom", "virtual"}),
        not bool(attrs & {"exclude_from_pos_files", "virtual"}),
        assignment,
        lcsc,
    )


def read_saved_pcb_assignments(
    path: Union[str, os.PathLike[str]],
) -> tuple[SavedBoardPart, ...]:
    """Return detached persisted Default values, or raise without partial results.

    The caller fingerprints this file before/after reading and again under the
    archival lock. This reader neither imports pcbnew nor consults the live board.
    """
    text = Path(path).read_text(encoding="utf-8")
    if "\0" in text:
        raise ValueError("NUL bytes are unsupported in saved PCB evidence")
    roots = tuple(_forms(text, 0, len(text)))
    if len(roots) != 1 or roots[0].name != "kicad_pcb":
        raise ValueError("Expected one complete kicad_pcb document")
    root = roots[0]
    _gap(text, 0, root.start)
    _gap(text, root.end, len(text))
    arguments, children = _parts(text, root)
    if arguments:
        raise ValueError("Unexpected saved PCB root arguments")
    versions = [form for form in children if form.name == "version"]
    if len(versions) != 1:
        raise ValueError("Expected one saved PCB version")
    _check_version(text, versions[0])
    result = []
    identities: set[str] = set()
    for child in children:
        if child.name not in _BOARD_SECTIONS:
            raise ValueError(f"Unsupported saved PCB section: {child.name!r}")
        if child.name != "footprint":
            continue
        part = _footprint(text, child)
        if part.component_id in identities:
            raise ValueError("Duplicate saved footprint UUID")
        identities.add(part.component_id)
        result.append(part)
    return tuple(result)
