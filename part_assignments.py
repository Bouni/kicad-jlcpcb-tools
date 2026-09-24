"""Resolve native part assignment aliases consistently for every assembly mode."""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class AssignmentAlias:
    """One raw native assignment field and whether its value is inherited."""

    name: str
    text: str
    inherited: bool


@dataclass(frozen=True)
class ResolvedAssignment:
    """Assignment provenance without collapsing absence, blanks, or conflicts."""

    status: str
    inherited: bool
    aliases: tuple[AssignmentAlias, ...]


def is_assignment_alias(name: str) -> bool:
    """Recognize part-number field names independently of their current text."""
    normalized = re.sub(r"[^a-z0-9]", "", name.casefold())
    return bool(
        re.fullmatch(
            r"(?:lcsc|jlcpcb|jlc)(?:part(?:number|num|no|nr)?|pn|number|code|id)?",
            normalized,
        )
    )


def assignment_names(fields: dict[str, str], overrides: dict[str, str]) -> set[str]:
    """Include every recognized alias, retaining blank and invalid assignments."""
    # Prefixes alone also match unrelated metadata such as "JLCPCB Rotation".
    # An assignment remains an assignment after base and override are cleared.
    return {name for name in (*fields, *overrides) if is_assignment_alias(name)}


def _alias_order(name: str) -> tuple[bool, str, str]:
    return name.casefold() != "lcsc", name.casefold(), name


def resolve_assignment(
    fields: dict[str, str], overrides: dict[str, str], variant_name: str
) -> tuple[ResolvedAssignment, str]:
    """Resolve aliases once for reading and editing, retaining explicit blanks.

    A named variant's explicit assignment aliases shadow its inherited aliases.
    Multiple occupied aliases with different values are a conflict, never a
    first-match catalog lookup. A present canonical empty field is intentional.
    Supported names are LCSC, JLC, or JLCPCB with an optional Part, Part Number,
    Part Num, Part No, Part Nr, PN, Number, Code, or ID suffix, ignoring punctuation,
    whitespace, and case. Other prefixed metadata is never an assignment.
    """
    names = sorted(assignment_names(fields, overrides), key=_alias_order)
    aliases = tuple(
        AssignmentAlias(
            n,
            overrides.get(n, fields.get(n, "")),
            bool(variant_name and n not in overrides),
        )
        for n in names
    )
    if not aliases:
        return ResolvedAssignment("missing", bool(variant_name), ()), ""
    explicit = tuple(a for a in aliases if a.name in overrides) if variant_name else ()
    active = explicit or aliases
    first = active[0]
    normalized = tuple(a.text.strip().upper() for a in active)
    occupied = {text for text in normalized if text}
    if len(occupied) > 1 or (first.text == "" and occupied):
        status, lcsc = "conflict", ""
    elif first.text == "":
        status, lcsc = "empty", ""
    elif re.fullmatch(r"C[0-9]+", normalized[0]):
        status, lcsc = "valid", normalized[0]
    else:
        status, lcsc = "invalid", ""
    return ResolvedAssignment(status, first.inherited, aliases), lcsc
