"""Tests for looking a part up in the parts database by an LCSC number."""

import importlib.util
from pathlib import Path
import sqlite3
import sys
import types
from unittest.mock import MagicMock

import pytest

_ROOT = Path(__file__).parent.parent

for _mod in ["pcbnew", "wx", "wx.dataview", "requests"]:
    sys.modules.setdefault(_mod, MagicMock())

_pkg = sys.modules.get("kicadplugin")
if _pkg is None:
    _pkg = types.ModuleType("kicadplugin")
    _pkg.__path__ = [str(_ROOT)]
    sys.modules["kicadplugin"] = _pkg
for _name in [
    "dblib",
    "events",
    "partselector_columns",
    "search_escape",
    "unzip_parts",
]:
    sys.modules.setdefault(f"kicadplugin.{_name}", MagicMock())

# helpers supplies dict_factory, which get_part_details depends on for real
_helpers_spec = importlib.util.spec_from_file_location(
    "kicadplugin.helpers", _ROOT / "helpers.py"
)
assert _helpers_spec is not None and _helpers_spec.loader is not None
_helpers = importlib.util.module_from_spec(_helpers_spec)
_helpers.__package__ = "kicadplugin"
sys.modules["kicadplugin.helpers"] = _helpers
_helpers_spec.loader.exec_module(_helpers)

_spec = importlib.util.spec_from_file_location(
    "kicadplugin.library", _ROOT / "library.py"
)
assert _spec is not None and _spec.loader is not None
_library = importlib.util.module_from_spec(_spec)
_library.__package__ = "kicadplugin"
sys.modules["kicadplugin.library"] = _library
_spec.loader.exec_module(_library)

Library = _library.Library

# Mirrors common/partsdb.py, including the trigram tokenizer -- the tokenizer
# is case-insensitive, so MATCH was never the half that failed.
_CREATE_PARTS = """
    CREATE virtual TABLE IF NOT EXISTS parts using fts5 (
        'LCSC Part', 'First Category', 'Second Category', 'MFR.Part', 'Package',
        'Solder Joint' unindexed, 'Manufacturer', 'Library Type', 'Description',
        'Datasheet' unindexed, 'Price' unindexed, 'Stock' unindexed
    , tokenize="trigram")
"""


@pytest.fixture
def library(tmp_path):
    """Build a bare Library over a parts database holding one known part."""
    partsdb = tmp_path / "parts.db"
    with sqlite3.connect(partsdb) as con:
        con.execute(_CREATE_PARTS)
        con.execute(
            "INSERT INTO parts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "C12345",
                "Capacitors",
                "MLCC",
                "CL10B104KB8NNNC",
                "0603",
                "",
                "Samsung",
                "Basic",
                "100nF 50V X7R",
                "",
                "0.0035",
                "5000",
            ),
        )
    lib = object.__new__(Library)
    lib.logger = MagicMock()
    lib.partsdb_file = str(partsdb)
    return lib


class TestGetPartDetails:
    """A part is found however its number was spelled."""

    @pytest.mark.parametrize(
        "number", ["C12345", "c12345", "C12345 ", " C12345", " c12345\n", "\tC12345 "]
    )
    def test_every_spelling_finds_the_part(self, library, number):
        """Case and padding no longer decide whether stock and type appear.

        The FTS5 tokenizer is trigram, which folds case, so MATCH always
        returned the row. The confirming comparison did not: it was
        n["lcsc"] == number against the raw argument, so a lower-case or
        padded number returned {} and the parts list showed blank Stock and
        Library Type with no error anywhere.
        """
        details = library.get_part_details(number)
        assert details.get("stock") == "5000"
        assert details.get("type") == "Basic"

    def test_a_different_part_is_still_not_found(self, library):
        """Normalizing does not make unrelated numbers match."""
        assert library.get_part_details("C99999") == {}

    def test_a_prefix_is_not_a_match(self, library):
        """C1234 does not resolve to C12345."""
        assert library.get_part_details("C1234") == {}

    @pytest.mark.parametrize("number", ["", "   "])
    def test_an_empty_number_is_not_a_query(self, library, number):
        """A blank number returns nothing rather than reaching FTS5.

        'parts MATCH " "' raises OperationalError: fts5: syntax error, so a
        whitespace-only store value used to take down the lookup.
        """
        assert library.get_part_details(number) == {}
