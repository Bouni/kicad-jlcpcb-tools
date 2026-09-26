"""Opt-in Manufacturer and MPN BOM columns joined from the parts catalog.

https://github.com/Bouni/kicad-jlcpcb-tools/issues/634 asks for the
manufacturer part number in the BOM. The columns come from the local catalog by
LCSC number and never block fabrication when the catalog cannot supply them.
"""

from contextlib import closing
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

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
