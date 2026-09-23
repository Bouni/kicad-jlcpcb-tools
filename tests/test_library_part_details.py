"""Tests for looking a part up in the parts database by an LCSC number."""

import sqlite3
from unittest.mock import MagicMock

import pytest

from tests.wx_harness import load_correction_modules

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
    with load_correction_modules() as loaded:
        lib = object.__new__(loaded.library.Library)
        lib.logger = MagicMock()
        lib.partsdb_file = str(partsdb)
        yield lib


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
        padded number returned {}.
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

    @pytest.mark.parametrize("number", ["", "   ", "C123-4", "C12.5", "N/A", 'C1"2'])
    def test_text_that_is_not_a_number_is_not_a_query(self, library, number):
        """Whatever the store holds is looked up, never parsed as FTS5 syntax.

        Passed to MATCH bare, each of these raised OperationalError: a blank
        is an FTS5 syntax error, and "C123-4" reads as a column filter.
        """
        assert library.get_part_details(number) == {}
