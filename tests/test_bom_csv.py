"""Plan JLC BOM lines: exact byte packing and the optional-column policies."""

import csv
import io
from typing import Any

import pytest

from bom_csv import (
    BOM_HEADER,
    BOM_LINE_MAX_BYTES,
    MANUFACTURER_HEADER,
    BomGroup,
    BomPlan,
    line_bytes,
    pack,
    plan_bom,
)

UNIROYAL = ("UNI-ROYAL(Uniroyal Elec)", "0603WAF1002T5E")


def _group(
    references: list[str], comment: str = "10k", lcsc: str = "C25804"
) -> BomGroup:
    """Describe one part group as the producers hand it to the writer."""
    return BomGroup(comment, tuple(references), "R_0603", lcsc)


def _written(line: tuple[Any, ...]) -> str:
    """Write one line with csv's real CRLF terminator."""
    stream = io.StringIO()
    csv.writer(stream).writerow(line)
    return stream.getvalue()


def _padded(references: list[str], extra: tuple[str, ...], spare: int) -> BomGroup:
    """Pad the comment so one line holding every reference has *spare* bytes left."""
    used = line_bytes(("", ",".join(references), "R_0603", "C25804", len(references)))
    used += line_bytes(extra) + 1 if extra else 0
    return _group(references, "Z" * (BOM_LINE_MAX_BYTES - spare - used))


def _assert_tight(group: BomGroup, extra: tuple[str, ...], lines: list[Any]) -> None:
    """Every line fits, keeps order and counts, and the next reference would not fit."""
    chunks = [line[1].split(",") for line in lines]
    assert [ref for chunk in chunks for ref in chunk] == list(group.references)
    for line, chunk in zip(lines, chunks):
        assert line == (
            group.comment,
            ",".join(chunk),
            "R_0603",
            group.lcsc,
            len(chunk),
            *extra,
        )
        assert len(_written(line).encode("utf-8")) - 2 <= BOM_LINE_MAX_BYTES
    for chunk, after in zip(chunks, chunks[1:]):
        grown = (
            group.comment,
            ",".join([*chunk, after[0]]),
            "R_0603",
            group.lcsc,
            len(chunk) + 1,
            *extra,
        )
        assert len(_written(grown).encode("utf-8")) - 2 > BOM_LINE_MAX_BYTES


def test_line_bytes_measures_what_csv_writes_less_its_crlf() -> None:
    """Quotes, doubled quotes and multi-byte text count as written."""
    line = ('10 "k" Ω', 'R"1,R2', "R_0603", "C25804", 2)
    assert _written(line) == '"10 ""k"" Ω","R""1,R2",R_0603,C25804,2\r\n'
    assert line_bytes(line) == len('"10 ""k"" Ω","R""1,R2",R_0603,C25804,2'.encode())


@pytest.mark.parametrize(
    "spare,expected", [(0, [2]), (-1, [1, 1])], ids=("exact", "over")
)
def test_a_line_of_exactly_the_limit_is_kept_and_one_byte_more_splits(
    spare: int, expected: list[int]
) -> None:
    """2048 bytes is within the limit; 2049 is not."""
    group = _padded(["R1", "R2"], (), spare)

    lines = pack(group, ())

    assert [line[4] for line in lines] == expected
    assert line_bytes(lines[0]) == (
        BOM_LINE_MAX_BYTES if spare == 0 else BOM_LINE_MAX_BYTES - 4
    )


def test_the_quotes_a_second_reference_brings_are_counted() -> None:
    """Without its quotes "R1,R2" would fit in 2047 bytes; quoted it needs 2049."""
    group = _padded(["R1"], (), 4)
    assert line_bytes((group.comment, "R1,R2", "R_0603", "C25804", 2)) == 2049

    lines = pack(group._replace(references=("R1", "R2")), ())

    assert [line[1] for line in lines] == ["R1", "R2"]


def test_the_quantity_cell_growing_a_digit_is_counted() -> None:
    """Ten references fit only if Quantity stayed one digit; the tenth starts a line."""
    references = [f"R{index}" for index in range(1, 11)]
    group = _padded(references, (), 0)
    group = group._replace(comment=group.comment + "Z")
    assert (
        line_bytes((group.comment, ",".join(references), "R_0603", "C25804", 9)) == 2048
    )

    lines = pack(group, ())

    assert [line[4] for line in lines] == [9, 1]


@pytest.mark.parametrize(
    "references,comment,extra",
    [
        ([f"LED{index}" for index in range(1, 2001)], "WS2812B", ()),
        ([f'R"{index:03d}' for index in range(1, 600)], "10k", ()),
        ([f"Ж{index:03d}" for index in range(1, 700)], "Ω" * 300, ()),
        ([f"R{index}" for index in range(1, 900)], '10 "k"', UNIROYAL),
        ([f"C{index}" for index in range(1, 600)], "100n", ("", "")),
        (
            [f"R{index}" for index in range(1, 700)],
            "1k",
            ("Suntsu Electronics, Inc.", 'MF1/4W-1KΩ±1%T52 "x"'),
        ),
    ],
    ids=(
        "leds",
        "doubled-quotes",
        "non-ascii",
        "with-cells",
        "blank-cells",
        "quoted-cells",
    ),
)
def test_lines_are_packed_tight_in_order(
    references: list[str], comment: str, extra: tuple[str, ...]
) -> None:
    """Each line holds every reference that fits and no more."""
    group = _group(references, comment)

    lines = pack(group, extra)

    assert len(lines) > 1
    _assert_tight(group, extra, lines)


def test_five_hundred_leds_split_where_the_line_is_full() -> None:
    """Issue #755's 500 LEDs: 303 + 197 references, where 1920 characters gave 289 + 211."""
    group = BomGroup(
        "WS2812B", tuple(f"LED{i}" for i in range(1, 501)), "LED_0805", "C25741"
    )

    assert [line[4] for line in pack(group, ())] == [303, 197]


@pytest.mark.parametrize(
    "group",
    [
        _group(["R1", "X" * 3000, "R2"]),
        _group(["R1"], "Z" * 2048),
    ],
    ids=("oversize-reference", "oversize-comment"),
)
def test_a_reference_that_cannot_fit_alone_gives_none(group: BomGroup) -> None:
    """Splitting cannot shorten a line whose single reference already overflows."""
    assert pack(group, ()) is None


def test_an_empty_group_gives_no_lines() -> None:
    """Every reference was filtered out: nothing to write."""
    assert pack(_group([]), ()) == []


def test_setting_off_writes_a_fitting_group_as_today() -> None:
    """One line, five columns, byte-identical to the export before this change."""
    plan = plan_bom([_group(["R1", "R2"])], None)

    assert plan.header == BOM_HEADER
    assert [_written(line) for line in plan.lines] == [
        '10k,"R1,R2",R_0603,C25804,2\r\n'
    ]
    assert plan.warnings == ()


def test_setting_on_appends_each_groups_cells_or_blanks() -> None:
    """A part missing from *columns* gets blank cells; no warning for either."""
    groups = [_group(["R1"]), _group(["D1"], "BAV99", "C2500")]

    plan = plan_bom(groups, {"C25804": UNIROYAL})

    assert plan.header == BOM_HEADER + MANUFACTURER_HEADER
    assert plan.lines == (
        ("10k", "R1", "R_0603", "C25804", 1, *UNIROYAL),
        ("BAV99", "D1", "R_0603", "C2500", 1, "", ""),
    )
    assert plan.warnings == ()


@pytest.mark.parametrize("lcsc", ["", None], ids=("blank", "none"))
def test_setting_on_gives_an_unassigned_group_blank_cells(lcsc: Any) -> None:
    """A group with no LCSC number has nothing to look up; its cells are blank."""
    plan = plan_bom([BomGroup("10k", ("R9",), "R_0603", lcsc)], {"C25804": UNIROYAL})

    assert [_written(line) for line in plan.lines] == ["10k,R9,R_0603,,1,,\r\n"]
    assert plan.warnings == ()


def test_setting_on_with_no_groups_still_writes_the_seven_column_header() -> None:
    """An empty BOM keeps the header the setting asks for."""
    plan = plan_bom([], {})

    assert plan == BomPlan(BOM_HEADER + MANUFACTURER_HEADER, (), ())


def test_a_group_with_no_room_for_its_cells_keeps_them_blank() -> None:
    """One reference fits beside blank cells but not beside the catalog's text."""
    group = _padded(["R1"], ("", ""), 0)

    plan = plan_bom([group], {"C25804": UNIROYAL})

    assert plan.header == BOM_HEADER + MANUFACTURER_HEADER
    assert plan.lines == ((group.comment, "R1", "R_0603", "C25804", 1, "", ""),)
    assert plan.warnings == (
        "Manufacturer and MPN left blank for R1: with them its BOM row exceeds "
        "JLC's 2048-byte limit",
    )


@pytest.mark.parametrize("cells", [UNIROYAL, None], ids=("hit", "missing"))
def test_a_group_that_fits_only_without_blank_cells_keeps_five_columns(
    cells: Any,
) -> None:
    """Two commas tip a 2047-byte line over, so the whole BOM drops the columns.

    The earlier group's blank-cells warning belongs to the abandoned plan and
    is dropped; only the fallback's own warning is kept.
    """
    blank_first = _padded(["R1"], ("", ""), 0)
    tight = _padded(["D1"], (), 1)
    groups = [blank_first, tight]
    columns = {} if cells is None else {"C25804": cells}

    plan = plan_bom(groups, columns)

    assert plan.header == BOM_HEADER
    assert plan.lines == plan_bom(groups, None).lines
    assert plan.warnings == (
        "The BOM row for D1 fits JLC's 2048-byte limit only without Manufacturer "
        "and MPN cells; BOM written without Manufacturer and MPN columns",
    )


@pytest.mark.parametrize("columns", [None, {"C25804": UNIROYAL}], ids=("off", "on"))
def test_an_unsplittable_group_is_one_line_holding_every_reference(
    columns: Any,
) -> None:
    """A comment over the limit cannot be split around: the group is written whole."""
    references = [f"R{index}" for index in range(1, 701)]
    group = _group(references, "Z" * 2100)

    plan = plan_bom([group], columns)

    blank = () if columns is None else ("", "")
    assert plan.lines == (
        ("Z" * 2100, ",".join(references), "R_0603", "C25804", 700, *blank),
    )
    assert plan.warnings == (
        f"The BOM row for {','.join(references)} exceeds JLC's 2048-byte limit "
        "even with one reference per row",
    )


def test_an_oversize_reference_writes_its_group_whole() -> None:
    """One reference too long for any line keeps its neighbours on the same line."""
    group = _group(["R1", "X" * 3000, "R2"])

    plan = plan_bom([group], None)

    assert plan.lines == (("10k", "R1," + "X" * 3000 + ",R2", "R_0603", "C25804", 3),)
    assert len(plan.warnings) == 1
