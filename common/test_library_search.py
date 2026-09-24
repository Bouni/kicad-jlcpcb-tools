"""Regression coverage for issue #578: short ohm searches returning no parts.

Exercise the real Library.search against the production FTS5 trigram schema.
Only wx imports and Library's filesystem/download initialization are bypassed.
These tests use the running interpreter's SQLite; the original report used
KiCad's SQLite 3.37.2 on macOS. On that runtime, the reported LIKE queries
without an ESCAPE clause return no rows for this catalog; the current escaped
queries return the expected parts.
"""

from contextlib import closing
import importlib
import logging
from pathlib import Path
import sqlite3
import sys
import types

import pytest

from common.partsdb import _CREATE_STATEMENTS
from value_normalize import canonicalize, quantity_for_reference

_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE = "_issue_578_library_search_tests"


def _add_parts(library, rows):
    """Add ``(LCSC Part, Package, Description, First Category)`` catalog rows."""
    with closing(sqlite3.connect(library.partsdb_file)) as con, con:
        con.executemany(
            'INSERT INTO parts ("LCSC Part", "Package", "Description", '
            '"First Category", "Library Type", "Stock") '
            "VALUES (?, ?, ?, ?, 'Basic', '1000')",
            rows,
        )


@pytest.fixture
def search_library(tmp_path, monkeypatch):
    """Load real search code with isolated GUI imports and a small catalog."""
    package = types.ModuleType(_PACKAGE)
    package.__path__ = [str(_ROOT)]
    monkeypatch.setitem(sys.modules, _PACKAGE, package)

    wx = types.ModuleType("wx")
    wx.__path__ = []
    wx.dataview = types.ModuleType("wx.dataview")
    monkeypatch.setitem(sys.modules, "wx", wx)
    monkeypatch.setitem(sys.modules, "wx.dataview", wx.dataview)

    try:
        module = importlib.import_module(f"{_PACKAGE}.library")
        library = module.Library.__new__(module.Library)
        library.logger = logging.getLogger(__name__)
        library.order_by = "LCSC Part"
        library.order_dir = "ASC"
        library.partsdb_file = str(tmp_path / "parts-fts5.db")

        # Synthetic catalog; the two reported part IDs anchor the regression.
        # Packages deliberately appear only in Package, as in generated catalogs.
        rows = [
            ("C17477", "0402", "0Ω ±1% Chip Resistor", "Resistors"),
            ("C578805", "0805", "0Ω ±1% Chip Resistor", "Resistors"),
            ("C578603", "0603", "0Ω ±1% Chip Resistor", "Resistors"),
            ("C25077", "0402", "10Ω ±1% Chip Resistor", "Resistors"),
            ("C578100", "0402", "100nF Ceramic Capacitor", "Capacitors"),
            *[
                (f"C57800{value}", "0402", f"{value}Ω ±1% Chip Resistor", "Resistors")
                for value in range(1, 10)
            ],
        ]
        with closing(sqlite3.connect(library.partsdb_file)) as con, con:
            for statement in _CREATE_STATEMENTS:
                con.execute(statement)
        _add_parts(library, rows)

        yield library
    finally:
        # Imports of sibling modules must not leak this private package into
        # other tests. monkeypatch restores any pre-existing wx modules.
        for name in tuple(sys.modules):
            if name.startswith(f"{_PACKAGE}."):
                sys.modules.pop(name)


def _search_ids(library, keyword, **filters):
    """Return catalog IDs using the same default switches as the part selector."""
    parameters = {
        "keyword": keyword,
        "basic": True,
        "extended": True,
        "preferred": True,
        "stock": False,
        **filters,
    }
    return {row[0] for row in library.search(parameters)}


@pytest.mark.parametrize(
    ("keyword", "expected"),
    [
        pytest.param("0Ω", {"C17477", "C578805", "C578603"}, id="zero-ohm-alone"),
        pytest.param("0Ω 0805", {"C578805"}, id="zero-ohm-0805"),
        pytest.param("0Ω 0402", {"C17477"}, id="zero-ohm-0402"),
    ],
)
def test_zero_ohm_search_returns_parts(search_library, keyword, expected):
    """Both short-only and mixed-length queries must find zero-ohm parts."""
    # 0Ω is a value written with its unit, so it no longer finds the 10Ω part.
    assert _search_ids(search_library, keyword) == expected, (
        f"Issue #578 regression with SQLite {sqlite3.sqlite_version}"
    )


@pytest.mark.parametrize("value", range(1, 10))
@pytest.mark.parametrize("suffix", ["", " 0402"], ids=["alone", "with-package"])
def test_single_digit_ohm_search_returns_parts(search_library, value, suffix):
    """All single-digit ohm values must work alone and with a package keyword."""
    assert _search_ids(search_library, f"{value}Ω{suffix}") == {f"C57800{value}"}, (
        f"Issue #578 regression with SQLite {sqlite3.sqlite_version}"
    )


@pytest.mark.parametrize("keyword", ["10Ω", "10Ω 0402"])
def test_two_digit_ohm_search_control(search_library, keyword):
    """Retain the successful longer query reported alongside the regression."""
    assert _search_ids(search_library, keyword) == {"C25077"}


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        pytest.param({"package": "0402"}, {"C17477"}, id="package-0402"),
        pytest.param({"package": "0805"}, {"C578805"}, id="package-0805"),
        pytest.param(
            {"package": "0402", "category": "Resistors"},
            {"C17477"},
            id="resistor-category",
        ),
        pytest.param(
            {"package": "0402", "category": "Capacitors"},
            set(),
            id="exclude-other-category",
        ),
    ],
)
def test_short_ohm_search_respects_filters(search_library, filters, expected):
    """Short keywords must still combine correctly with scoped MATCH filters."""
    assert _search_ids(search_library, "0Ω", **filters) == expected


@pytest.mark.parametrize(
    ("reference", "board_value", "expected"),
    [
        # Issue #786: the board says 0.1uF, the catalog says 100nF.
        pytest.param("C1", "0.1uF", {"C578100"}, id="issue-786-fractional"),
        pytest.param("C1", "0.1uf", {"C578100"}, id="issue-786-lowercase"),
        pytest.param("C1", "0.1µF", {"C578100"}, id="micro-sign"),
        pytest.param("C1", "100n", {"C578100"}, id="bare-prefix"),
        pytest.param("C1", "0.1u", {"C578100"}, id="bare-fractional"),
        pytest.param("R1", "10", {"C25077"}, id="bare-resistance"),
        pytest.param("R1", "10R", {"C25077"}, id="r-spelling"),
        pytest.param("R1", "0.01k", {"C25077"}, id="fractional-resistance"),
        pytest.param("R1", "10ohm", {"C25077"}, id="ohm-spelling"),
    ],
)
def test_canonicalized_board_value_finds_the_part(
    search_library, reference, board_value, expected
):
    """The spelling the part selector prefills must match the real catalog.

    This is the whole path issue #786 reported: a value field the search cannot
    match. canonicalize() rewrites it, and the rewritten term is what the user
    sees in the box and what Library.search actually runs.
    """
    quantity = quantity_for_reference(reference)
    keyword = canonicalize(board_value, quantity)
    assert keyword is not None, f"{board_value} on {reference} was not canonicalized"
    assert _search_ids(search_library, keyword) == expected


@pytest.mark.parametrize(
    ("reference", "board_value"),
    [
        pytest.param("D1", "1N4148", id="diode-part-number"),
        pytest.param("LD1", "1N34", id="laser-diode-not-inductor"),
        pytest.param("CB1", "10A", id="breaker-not-capacitor"),
        pytest.param("RL1", "NO/NC", id="relay-not-resistor"),
        pytest.param("U1", "LM358", id="ic"),
        pytest.param("C1", "X7R", id="dielectric-code"),
        pytest.param("R1", "0603", id="package-in-value-field"),
    ],
)
def test_values_the_prefill_must_not_rewrite(reference, board_value):
    """Anything that is not a passive value reaches the search box untouched."""
    assert canonicalize(board_value, quantity_for_reference(reference)) is None


# Issue #849: one milliohm and one megohm part at each of two values, as the
# catalog writes them.  62 resistor values appear both ways in a 717,025-part
# snapshot; at 10MΩ only 274 of the 1000 rows the search showed were 10MΩ parts.
_OHM_CASE_PARTS = [
    ("C849001", "2512", "10mΩ ±1% 1W Current Sense Resistor", "Resistors"),
    ("C849002", "0603", "10MΩ ±1% 100mW Chip Resistor", "Resistors"),
    ("C849003", "0603", "1.5mΩ ±1% 2W Current Sense Resistor", "Resistors"),
    ("C849004", "0603", "1.5MΩ ±1% 100mW Chip Resistor", "Resistors"),
]


@pytest.mark.parametrize(
    ("keyword", "expected"),
    [
        pytest.param("10mΩ", {"C849001"}, id="milliohm"),
        pytest.param("10MΩ", {"C849002"}, id="megohm"),
        pytest.param("1.5mΩ", {"C849003"}, id="fractional-milliohm"),
        pytest.param("1.5MΩ 0603", {"C849004"}, id="with-package"),
        pytest.param("10m\u2126", {"C849001"}, id="ohm-sign"),
    ],
)
def test_a_resistance_with_its_unit_matches_its_prefix_case(
    search_library, keyword, expected
):
    """The trigram index folds case, so 10mΩ used to find 10MΩ as well."""
    _add_parts(search_library, _OHM_CASE_PARTS)
    assert _search_ids(search_library, keyword) == expected


@pytest.mark.parametrize("keyword", ["10m", "10M"])
def test_a_bare_prefix_still_finds_both(search_library, keyword):
    """Without the Ω the term names no unit, so its case is not trusted."""
    _add_parts(search_library, _OHM_CASE_PARTS)
    assert _search_ids(search_library, keyword) == {"C849001", "C849002"}


@pytest.mark.parametrize(
    ("board_value", "expected"),
    [
        pytest.param("10m", {"C849001"}, id="lower-case-is-milli"),
        pytest.param("10M", {"C849002"}, id="upper-case-is-mega"),
        pytest.param("0R01", {"C849001"}, id="rkm-milliohm"),
    ],
)
def test_the_prefill_settles_a_bare_prefix(search_library, board_value, expected):
    """The part selector opens on the canonical spelling, which writes the Ω.

    So a resistor valued 10m opens on 10mΩ and finds only milliohm parts.  A 1m
    that meant a megohm shows a list of shunts, each labelled with its value in
    ohms in the Params column, rather than a mix the eye can misread.
    """
    _add_parts(search_library, _OHM_CASE_PARTS)
    keyword = canonicalize(board_value, quantity_for_reference("R1"))
    assert _search_ids(search_library, keyword) == expected


def test_the_value_pattern_is_bound_not_inlined(search_library, caplog):
    """The GLOB pattern reaches SQLite as a parameter, never as SQL text."""
    caplog.set_level(logging.DEBUG, logger=__name__)
    _search_ids(search_library, "10mΩ")
    (query,) = [r.args[0] for r in caplog.records if r.msg == "query '%s'"]
    assert "GLOB ?" in query
    assert "[^0-9.]" not in query


# A value and the longer values that contain it, written where the catalog
# writes them: at the start of the description, after a space, and after the
# ideographic comma that separates a ferrite's impedances.  In a 717,025-part
# snapshot 1kΩ found 7,129 parts, and only 2,412 of them were 1kΩ.
_WHOLE_VALUE_PARTS = [
    ("C200001", "0603", "1kΩ ±1% 100mW Chip Resistor", "Resistors"),
    ("C200002", "0603", "5.1kΩ ±1% 100mW Chip Resistor", "Resistors"),
    ("C200003", "0603", "51kΩ ±1% 100mW Chip Resistor", "Resistors"),
    ("C200004", "0603", "0.1kΩ ±1% 100mW Chip Resistor", "Resistors"),
    ("C200005", "0603", "±1% 1kΩ 100mW Thick Film Resistor", "Resistors"),
    ("C200006", "0402", "60Ω@10MHz、1kΩ@100MHz Ferrite Bead", "Filters"),
    ("C200007", "0805", "10uF 25V X5R Ceramic Capacitor", "Capacitors"),
    ("C200008", "0805", "110uF 25V Ceramic Capacitor", "Capacitors"),
    ("C200009", "0603", "15Ω ±1% 100mW Chip Resistor", "Resistors"),
    ("C200010", "0603", "0.5Ω ±1% 100mW Chip Resistor", "Resistors"),
]

_ONE_KILOHM = {"C200001", "C200005", "C200006"}


@pytest.mark.parametrize(
    ("keyword", "expected"),
    [
        pytest.param("1kΩ", _ONE_KILOHM, id="kilohm"),
        pytest.param("1KΩ", _ONE_KILOHM, id="upper-case-k"),
        pytest.param("1k\u2126", _ONE_KILOHM, id="ohm-sign"),
        pytest.param("1kΩ 0603", {"C200001", "C200005"}, id="with-package"),
        pytest.param("5.1kΩ", {"C200002"}, id="fractional"),
        pytest.param("51kΩ", {"C200003"}, id="two-digit"),
        pytest.param(".1kΩ", {"C200004"}, id="leading-point"),
        pytest.param("10uF", {"C200007"}, id="capacitance"),
        pytest.param("10UF", {"C200007"}, id="upper-case-u"),
        pytest.param("5Ω", {"C578005"}, id="short-term"),
    ],
)
def test_a_value_with_its_unit_finds_only_that_value(search_library, keyword, expected):
    """1kΩ finds 1kΩ parts, not the 5.1kΩ, 51kΩ and 0.1kΩ ones around it.

    Short terms go through LIKE rather than the full-text index and are held to
    the same rule: 5Ω finds neither 15Ω nor 0.5Ω.
    """
    _add_parts(search_library, _WHOLE_VALUE_PARTS)
    assert _search_ids(search_library, keyword) == expected


@pytest.mark.parametrize(
    ("keyword", "expected"),
    [
        pytest.param(
            "1k",
            {"C200001", "C200002", "C200003", "C200004", "C200005", "C200006"},
            id="no-unit",
        ),
        pytest.param("4k7", set(), id="rkm"),
    ],
)
def test_a_value_without_its_unit_is_still_a_substring(
    search_library, keyword, expected
):
    """A bare 1k names no unit, so it may be part of anything: 5.1kΩ, 1kHz."""
    _add_parts(search_library, _WHOLE_VALUE_PARTS)
    assert _search_ids(search_library, keyword) == expected


def test_a_value_only_in_the_part_number_is_found(search_library):
    """Some parts carry their value only in the part number, under no description.

    Without the part number in the whole-value text, 4.7uH would no longer find
    them, though the substring search always did.  Part numbers often write the
    value in upper case, and a k, u, n, p, F or H means the same either way.
    """
    with closing(sqlite3.connect(search_library.partsdb_file)) as con, con:
        con.executemany(
            'INSERT INTO parts ("LCSC Part", "MFR.Part", "Package", "Description", '
            '"First Category", "Library Type", "Stock") '
            "VALUES (?, ?, 'SMD', '', 'Inductors, Coils, Chokes', 'Extended', '2400')",
            [
                ("C200011", "XRNR4020-4.7uH/M"),
                ("C200012", "XRNR4020-14.7uH/M"),
                ("C200013", "CYA1265-4.7UH"),
                ("C200014", "CYA1265-14.7UH"),
            ],
        )
    assert _search_ids(search_library, "4.7uH") == {"C200011", "C200013"}


@pytest.mark.parametrize(
    ("keyword", "expected"),
    [
        pytest.param("10\u00b5F", {"C200007"}, id="micro-sign"),
        pytest.param("10\u03bcF", {"C200007"}, id="greek-mu"),
        pytest.param("10\u00b5", {"C200007", "C200008"}, id="no-unit"),
        pytest.param("0\u00b5", {"C200007", "C200008"}, id="short-term"),
        pytest.param("5\u2126", {"C578005"}, id="short-ohm-sign"),
    ],
)
def test_micro_and_ohm_signs_search_as_the_catalog_writes_them(
    search_library, keyword, expected
):
    """The catalog writes 10uF and U+03A9, so 10µF and 5Ω in U+2126 found nothing.

    The part selector's µ button types U+00B5.  Without a unit the term is
    still a substring, so 10µ finds 110uF too.  Short terms go through LIKE,
    which folds neither sign, so they need the fold as much as long ones.
    """
    _add_parts(search_library, _WHOLE_VALUE_PARTS)
    assert _search_ids(search_library, keyword) == expected
