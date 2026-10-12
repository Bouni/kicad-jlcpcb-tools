"""Plan the JLC BOM CSV: one line per part group, split to fit JLC's line limit.

JLC rejects a BOM line over 2048. Issue #755 met it as a Designator cell of
over 2048 characters; every line here is held to 2048 UTF-8 bytes, less its
CRLF, the stricter reading. Lines are measured exactly as csv.writer emits
them, so quoting, doubled quotes and multi-byte text all count.

The module is standard library only and imports nothing from the plugin.
"""

from collections.abc import Iterable, Mapping, Sequence
import csv
from dataclasses import dataclass
import io
from typing import Any, NamedTuple, Optional

BOM_LINE_MAX_BYTES = 2048
BOM_HEADER = ("Comment", "Designator", "Footprint", "LCSC", "Quantity")
MANUFACTURER_HEADER = ("Manufacturer", "MPN")
_BLANK = ("", "")


class BomGroup(NamedTuple):
    """One BOM part group: every populated reference that shares its three cells."""

    comment: str
    references: tuple[str, ...]
    footprint: str
    lcsc: str


@dataclass(frozen=True)
class BomPlan:
    """Every line of a BOM file, decided before the file is opened."""

    header: tuple[str, ...]
    lines: tuple[tuple[Any, ...], ...]
    warnings: tuple[str, ...]


def line_bytes(cells: Sequence[Any]) -> int:
    """Measure a line as csv.writer emits it, in UTF-8 bytes less its CRLF.

    The real terminator matters: with an empty one, Python 3.9's csv leaves a
    field holding a line break unquoted, unlike the writer being measured.
    """
    line = io.StringIO()
    csv.writer(line).writerow(cells)
    return len(line.getvalue().encode("utf-8")) - 2


def _line(
    group: BomGroup, chunk: Sequence[str], extra: tuple[str, ...]
) -> tuple[Any, ...]:
    """Build one CSV line holding *chunk* of the group's references."""
    return (
        group.comment,
        ",".join(chunk),
        group.footprint,
        group.lcsc,
        len(chunk),
        *extra,
    )


def _fits(group: BomGroup, chunk: Sequence[str], extra: tuple[str, ...]) -> bool:
    """Report whether the line holding *chunk* is within JLC's limit as written."""
    return line_bytes(_line(group, chunk, extra)) <= BOM_LINE_MAX_BYTES


def pack(group: BomGroup, extra: tuple[str, ...]) -> Optional[list[tuple[Any, ...]]]:  # noqa: UP045
    """Split a group's references, in order, into lines that each fit JLC's limit.

    *extra* is appended to every line. A reference joins the current line
    while the whole line, measured as written, stays within the limit. A line
    never shrinks as references join it, so it closes at the first one that
    does not fit. Returns None when a single reference cannot fit on a line of
    its own.
    """
    lines = []
    chunk: list[str] = []
    for reference in group.references:
        if _fits(group, [*chunk, reference], extra):
            chunk.append(reference)
        elif chunk and _fits(group, [reference], extra):
            lines.append(_line(group, chunk, extra))
            chunk = [reference]
        else:
            return None
    if chunk:
        lines.append(_line(group, chunk, extra))
    return lines


class _ColumnsDoNotFit(Exception):
    """A group fits JLC's limit only without the Manufacturer and MPN cells."""


def plan_bom(
    groups: Iterable[BomGroup],
    columns: Optional[Mapping[str, tuple[str, str]]],  # noqa: UP045
) -> BomPlan:
    """Decide every line of the BOM before any file is opened.

    *columns* is None when the Manufacturer and MPN setting is off. Otherwise
    it maps a group's LCSC cell, as written, to its two cells, and a group
    missing from it gets blank cells. The limit never blocks output, so each
    of these cases returns a warning instead:

    - A group that cannot fit one reference beside its manufacturer and MPN,
      but can beside blank cells, keeps them blank.
    - Even blank, the two cells cost two commas. A group that fits only
      without them makes the whole BOM keep the setting-off columns, rather
      than lose a line JLC accepts. The abandoned plan's warnings are
      dropped: that group's warning comes first, then the five-column
      plan's own.
    - A group that cannot fit one reference beside its other cells is written
      as one line holding the whole group: splitting cannot shorten it.
    """
    groups = tuple(groups)
    if columns is None:
        return BomPlan(BOM_HEADER, *_plan_lines(groups, None))
    try:
        return BomPlan(BOM_HEADER + MANUFACTURER_HEADER, *_plan_lines(groups, columns))
    except _ColumnsDoNotFit as error:
        lines, warnings = _plan_lines(groups, None)
        return BomPlan(BOM_HEADER, lines, (str(error), *warnings))


def _plan_lines(
    groups: tuple[BomGroup, ...],
    columns: Optional[Mapping[str, tuple[str, str]]],  # noqa: UP045
) -> tuple[tuple[tuple[Any, ...], ...], tuple[str, ...]]:
    """Plan every group's lines and warnings, with the columns when given."""
    blank = () if columns is None else _BLANK
    lines: list[tuple[Any, ...]] = []
    warnings = []
    for group in groups:
        references = ",".join(group.references)
        extra = blank if columns is None else columns.get(group.lcsc, _BLANK)
        packed = pack(group, extra)
        if packed is None and extra != blank:
            packed = pack(group, blank)
            if packed is not None:
                warnings.append(
                    f"Manufacturer and MPN left blank for {references}: with them "
                    f"its BOM row exceeds JLC's {BOM_LINE_MAX_BYTES}-byte limit"
                )
        if packed is None and columns is not None and pack(group, ()) is not None:
            raise _ColumnsDoNotFit(
                f"The BOM row for {references} fits JLC's {BOM_LINE_MAX_BYTES}-byte "
                "limit only without Manufacturer and MPN cells; BOM written "
                "without Manufacturer and MPN columns"
            )
        if packed is None:
            warnings.append(
                f"The BOM row for {references} exceeds JLC's "
                f"{BOM_LINE_MAX_BYTES}-byte limit even with one reference per row"
            )
            packed = [_line(group, group.references, blank)]
        lines.extend(packed)
    return tuple(lines), tuple(warnings)
