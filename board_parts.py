"""Read the parts list's facts about each footprint from board records.

Plain functions over ``kicad_backend`` records, with no wx and no KiCad
objects, so the windows and the store share one reading of the board.
"""

from collections.abc import Iterable
import json
import re
from typing import Any, Optional

from .kicad_backend.records import BoardSnapshot, ComponentRecord, ComponentState
from .part_assignments import ResolvedAssignment, resolve_assignment

# The parts list shows footprints whose reference starts with a letter, digit,
# underscore or hyphen.
_LISTED_REFERENCE = re.compile(r"[\w\d-]+")


def is_listed(reference: str) -> bool:
    """Return whether the parts list shows a footprint with this reference."""
    return _LISTED_REFERENCE.match(reference) is not None


def find_component(
    snapshot: BoardSnapshot, reference: str
) -> Optional[ComponentRecord]:  # noqa: UP045
    """Return the first footprint with this reference, as KiCad's lookup does."""
    found = snapshot.by_reference(reference)
    return found[0] if found else None


def placement_sides(
    snapshot: BoardSnapshot, references: Iterable[str]
) -> dict[str, str]:
    """Return "top" or "bottom" for each reference the board has a footprint for."""
    sides = {}
    for reference in references:
        component = find_component(snapshot, reference)
        if component is not None:
            sides[reference] = component.side
    return sides


def repeated_field_names(snapshot: BoardSnapshot) -> list[tuple[str, str]]:
    """Return (reference, field name) for each name a footprint holds twice.

    KiCad's footprint dialog accepts a second field with a name the
    footprint already has. Footprints come in board order.
    """
    return [
        (component.reference, name)
        for component in snapshot.components
        for name in component.default.duplicate_names
    ]


def first_field_text(state: ComponentState, name: str) -> str:
    """Return the text of the first field with this name, or "" if none.

    KiCad's own Value field comes before any field a user adds with that
    name, and is the one KiCad shows as the footprint's value.
    """
    for record in state.replaced_fields:
        if record.name == name:
            return record.text
    field = state.fields.get(name)
    return field.text if field is not None else ""


def library_item_name(lib_id: str) -> str:
    """Return the footprint name of a "Lib:Name" identifier."""
    return lib_id.split(":", 1)[1] if ":" in lib_id else lib_id


def default_assignment(component: ComponentRecord) -> tuple[ResolvedAssignment, str]:
    """Resolve the footprint's Default assignment fields and its LCSC number."""
    fields = {name: field.text for name, field in component.default.fields.items()}
    return resolve_assignment(fields, {}, "")


def part_facts(component: ComponentRecord) -> dict[str, Any]:
    """Return the parts-list columns one footprint's Default state gives.

    Pads that are bare holes are not solder joints; a counted pad with a
    hole makes the part through-hole.
    """
    state = component.default
    assignment, lcsc = default_assignment(component)
    joints = [pad for pad in component.pads if pad.kind != "npth"]
    flags = {
        "exclude_from_bom": state.exclude_from_bom,
        "exclude_from_pos": state.exclude_from_pos,
        "is_dnp": state.dnp,
    }
    return {
        "reference": component.reference,
        "value": first_field_text(state, "Value"),
        "footprint": library_item_name(component.footprint_lib_id),
        "lcsc": lcsc,
        "assignment_status": assignment.status,
        **flags,
        "pad_count": len(joints),
        "has_tht": any(pad.has_hole for pad in joints),
        "assembly_flags": json.dumps(flags, sort_keys=True),
    }
