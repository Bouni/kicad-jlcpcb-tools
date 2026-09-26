"""Opt-in Manufacturer and MPN BOM columns joined from the parts catalog.

https://github.com/Bouni/kicad-jlcpcb-tools/issues/634 asks for the
manufacturer part number in the BOM. The columns come from the local catalog by
LCSC number and never block fabrication when the catalog cannot supply them.
"""

from collections.abc import Callable
from contextlib import closing
import csv
import logging
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call

import pytest

from tests.correction_test_support import make_library
from tests.fabrication_test_support import modules as fabrication_modules

modules = fabrication_modules

# The downloaded catalog's FTS5 layout. It is trigram-tokenised like the real
# one, so a lookup for C25804 also meets its prefix neighbour C2580400.
_CATALOG_COLUMNS = (
    "LCSC Part",
    "First Category",
    "Second Category",
    "MFR.Part",
    "Package",
    "Solder Joint",
    "Manufacturer",
    "Library Type",
    "Description",
    "Datasheet",
    "Price",
    "Stock",
)

# Real catalog rows (2026), except C9999999, which holds the catalog's longest
# manufacturer (100 characters) and MPN (97 characters).
_CATALOG = {
    "C25804": ("UNI-ROYAL(Uniroyal Elec)", "0603WAF1002T5E"),
    "C2580400": ("KOA Speer Elec", "RK73B3ATTE113J"),
    "C2500": ("Nexperia", "BAV99,215"),
    "C5498641": ("3M", '3M 1120 1/2" X 4"-100'),
    "C13887": ("TI(德州仪器)", "LM1117IDT-ADJ"),
    "C9900030474": ("无", "3mm黑色塑料灯座带灯\r\n颜色：三孔黄红绿\r\n极性：左正右负"),
    "C9999999": ("W" * 100, "M" * 97),
}


def _write_catalog(path: str) -> None:
    """Create a parts table holding ``_CATALOG`` in the catalog's own layout."""
    columns = ", ".join(f'"{name}"' for name in _CATALOG_COLUMNS)
    placeholders = ", ".join("?" for _ in _CATALOG_COLUMNS)
    with closing(sqlite3.connect(path)) as con, con:
        con.execute(
            f"CREATE VIRTUAL TABLE parts USING fts5({columns}, tokenize='trigram')"
        )
        for lcsc, (manufacturer, mpn) in _CATALOG.items():
            row = dict.fromkeys(_CATALOG_COLUMNS, "")
            row.update(
                {"LCSC Part": lcsc, "Manufacturer": manufacturer, "MFR.Part": mpn}
            )
            con.execute(
                f"INSERT INTO parts VALUES ({placeholders})", tuple(row.values())
            )


@pytest.fixture
def library(modules: SimpleNamespace, tmp_path: Path) -> Any:
    """Read a test-written catalog through the real Library, counting lookups."""
    library = make_library(modules.library, tmp_path)
    _write_catalog(library.partsdb_file)
    library.get_part_details = MagicMock(wraps=library.get_part_details)
    return library


def test_part_details_report_the_manufacturer(library: Any) -> None:
    """The reader returns the manufacturer beside the MPN, for the exact code only."""
    details = library.get_part_details("C25804")

    assert (details["lcsc"], details["manufacturer"], details["part_no"]) == (
        "C25804",
        "UNI-ROYAL(Uniroyal Elec)",
        "0603WAF1002T5E",
    )


BomFactory = Callable[..., Any]

_HEADER = [
    "Comment",
    "Designator",
    "Footprint",
    "LCSC",
    "Quantity",
    "Manufacturer",
    "MPN",
]


@pytest.fixture
def bom_factory(modules: SimpleNamespace, library: Any, tmp_path: Path) -> BomFactory:
    """Construct the real exporter over explicit groups, setting and catalog state."""

    def catalog_details(code: str, *, strict: bool = False) -> dict[str, Any]:
        """Stand in for the main window's cache, reading the real test catalog."""
        assert strict, "catalog errors must reach the exporter"
        return library.get_part_details(code)

    def make(
        groups: list[dict[str, str]], *, enabled: bool = True, available: bool = True
    ) -> Any:
        footprints = [
            SimpleNamespace(GetReference=lambda ref=ref: ref)
            for group in groups
            for ref in group["refs"].split(",")
        ]
        board = SimpleNamespace(
            Footprints=lambda: footprints,
            GetFileName=lambda: str(tmp_path / "board.kicad_pcb"),
        )
        parent = SimpleNamespace(
            settings={
                "gerber": {"lcsc_bom_cpl": True, "bom_manufacturer_columns": enabled}
            },
            store=SimpleNamespace(read_bom_parts=lambda: groups),
            is_catalog_available=lambda: available,
            _catalog_get_part_details=catalog_details,
        )
        return modules.fabrication.Fabrication(parent, board)

    return make


def _group(
    value: str, refs: str, lcsc: str, footprint: str = "R_0603"
) -> dict[str, str]:
    """Describe one BOM group as the project store returns it."""
    return {"value": value, "refs": refs, "footprint": footprint, "lcsc": lcsc}


def _written(fab: Any) -> list[list[str]]:
    """Generate the BOM and read back every CSV record, header first."""
    fab.generate_bom()
    with Path(fab.get_bom_csv_path()).open(newline="", encoding="utf-8") as stream:
        return list(csv.reader(stream))


def test_setting_on_appends_catalog_manufacturer_and_mpn(
    bom_factory: BomFactory,
) -> None:
    """Catalog hits fill both cells; a missing or unknown LCSC number stays blank."""
    fab = bom_factory(
        [
            _group("10k", "R1,R2", "C25804"),
            _group("BAV99", "D1", "C2500", "SOT-23"),
            _group("TP", "TP1", ""),
            _group("NE5532", "U1", "C404404", "SOIC-8"),
        ]
    )

    assert _written(fab) == [
        _HEADER,
        [
            "10k",
            "R1,R2",
            "R_0603",
            "C25804",
            "2",
            "UNI-ROYAL(Uniroyal Elec)",
            "0603WAF1002T5E",
        ],
        ["BAV99", "D1", "SOT-23", "C2500", "1", "Nexperia", "BAV99,215"],
        ["TP", "TP1", "R_0603", "", "1", "", ""],
        ["NE5532", "U1", "SOIC-8", "C404404", "1", "", ""],
    ]


def test_each_part_is_looked_up_once_in_canonical_form(
    bom_factory: BomFactory, library: Any
) -> None:
    """Groups sharing a part cost one lookup; text that is no LCSC number is skipped."""
    fab = bom_factory(
        [
            _group("10k", "R1", "C25804"),
            _group("10K", "R2", " c25804 "),
            _group("?", "R3", "foo-bar"),
        ]
    )

    rows = _written(fab)

    assert [row[5:] for row in rows[1:]] == [
        ["UNI-ROYAL(Uniroyal Elec)", "0603WAF1002T5E"],
        ["UNI-ROYAL(Uniroyal Elec)", "0603WAF1002T5E"],
        ["", ""],
    ]
    assert library.get_part_details.call_args_list == [call("C25804")]


def test_setting_off_keeps_the_original_columns_without_lookups(
    bom_factory: BomFactory, library: Any
) -> None:
    """A BOM is unchanged unless the user opts in."""
    fab = bom_factory([_group("10k", "R1,R2", "C25804")], enabled=False)

    assert _written(fab) == [
        ["Comment", "Designator", "Footprint", "LCSC", "Quantity"],
        ["10k", "R1,R2", "R_0603", "C25804", "2"],
    ]
    library.get_part_details.assert_not_called()


def test_unavailable_catalog_leaves_blank_columns_and_warns(
    bom_factory: BomFactory, library: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A catalog that is missing or still downloading never blocks the BOM."""
    fab = bom_factory([_group("10k", "R1", "C25804")], available=False)

    with caplog.at_level(logging.WARNING):
        rows = _written(fab)

    assert rows[1] == ["10k", "R1", "R_0603", "C25804", "1", "", ""]
    library.get_part_details.assert_not_called()
    assert "Parts catalog is unavailable" in caplog.text


def test_catalog_error_keeps_earlier_hits_and_still_writes(
    bom_factory: BomFactory, library: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing lookup blanks that code and every later one; the file is written."""

    def lookup(code: str) -> dict[str, str]:
        if code == "C25804":
            raise sqlite3.OperationalError("database disk image is malformed")
        return {"manufacturer": "Nexperia", "part_no": "BAV99,215"}

    library.get_part_details.side_effect = lookup
    fab = bom_factory(
        [
            _group("BAV99", "D1", "C2500", "SOT-23"),
            _group("10k", "R1", "C25804"),
            _group("tape", "X1", "C5498641"),
        ]
    )

    with caplog.at_level(logging.WARNING):
        rows = _written(fab)

    assert [row[5:] for row in rows[1:]] == [
        ["Nexperia", "BAV99,215"],
        ["", ""],
        ["", ""],
    ]
    assert library.get_part_details.call_args_list == [call("C2500"), call("C25804")]
    assert "lookup failed at C25804" in caplog.text


def test_unexpected_lookup_error_preserves_the_previous_bom(
    bom_factory: BomFactory, library: Any
) -> None:
    """Every lookup runs before the output opens, so a crash cannot truncate it."""
    fab = bom_factory([_group("10k", "R1", "C25804")])
    fab.generate_bom()
    destination = Path(fab.get_bom_csv_path())
    previous = destination.read_bytes()
    library.get_part_details.side_effect = RuntimeError("catalog reader crashed")

    with pytest.raises(RuntimeError, match="catalog reader crashed"):
        fab.generate_bom()

    assert destination.read_bytes() == previous


def test_catalog_text_survives_csv_quoting_one_line_per_row(
    bom_factory: BomFactory,
) -> None:
    """Commas, quotes and Chinese text round-trip; catalog line breaks become spaces."""
    fab = bom_factory(
        [
            _group("BAV99", "D1", "C2500", "SOT-23"),
            _group("tape", "X1", "C5498641"),
            _group("LM1117", "U1", "C13887", "SOT-223"),
            _group("holder", "X2", "C9900030474"),
        ]
    )

    rows = _written(fab)

    assert [row[5:] for row in rows[1:]] == [
        ["Nexperia", "BAV99,215"],
        ["3M", '3M 1120 1/2" X 4"-100'],
        ["TI(德州仪器)", "LM1117IDT-ADJ"],
        ["无", "3mm黑色塑料灯座带灯 颜色：三孔黄红绿 极性：左正右负"],
    ]
    text = Path(fab.get_bom_csv_path()).read_text(encoding="utf-8")
    assert len(text.splitlines()) == len(rows)


@pytest.mark.parametrize("pad", range(7))
def test_long_rows_are_resplit_to_fit_the_jlc_row_limit(
    bom_factory: BomFactory, pad: int
) -> None:
    """The 500-LED export (issue #755) stays within 2048 bytes a row with the columns.

    Each reference costs seven bytes, so stepping the comment through every
    remainder lands one re-split row exactly on the limit.
    """
    refs = [f"LED{i:03d}" for i in range(1, 501)]
    comment = "WS2812B" + "x" * pad
    fab = bom_factory([_group(comment, ",".join(refs), "C9999999", "LED_0805")])

    rows = _written(fab)

    lines = Path(fab.get_bom_csv_path()).read_text(encoding="utf-8").splitlines()
    assert max(len(line.encode("utf-8")) for line in lines) <= 2048
    assert [ref for row in rows[1:] for ref in row[1].split(",")] == refs
    assert sum(int(row[4]) for row in rows[1:]) == 500
    for row in rows[1:]:
        assert int(row[4]) == len(row[1].split(","))
        assert row[5:] == ["W" * 100, "M" * 97]


def test_resplit_counts_non_ascii_designators_in_bytes(
    bom_factory: BomFactory,
) -> None:
    """A reference outside ASCII costs more bytes than characters; the limit is bytes."""
    refs = [f"Ж{i:03d}" for i in range(1, 341)]
    fab = bom_factory([_group("WS2812B", ",".join(refs), "C9999999", "LED_0805")])

    rows = _written(fab)

    lines = Path(fab.get_bom_csv_path()).read_text(encoding="utf-8").splitlines()
    assert max(len(line.encode("utf-8")) for line in lines) <= 2048
    assert [ref for row in rows[1:] for ref in row[1].split(",")] == refs
