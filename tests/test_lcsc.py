"""Tests for the two questions lcsc.py answers about a part number.

normalize_lcsc has its own file, tests/test_lcsc_normalization.py. What is
covered here is the pair that sits on top of it: the strict predicate a field
has to satisfy, and the lenient parser that reads pasted text.
"""

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent

_spec = importlib.util.spec_from_file_location("standalone_lcsc", _ROOT / "lcsc.py")
assert _spec is not None and _spec.loader is not None
_lcsc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_lcsc)

is_lcsc_part = _lcsc.is_lcsc_part
extract_lcsc = _lcsc.extract_lcsc


class TestIsLcscPart:
    """is_lcsc_part answers whether a value names a part, strictly."""

    @pytest.mark.parametrize("value", ["C12345", "c12345", " C12345 ", "C9900101779"])
    def test_a_part_number_is_recognised(self, value):
        """A part number is recognised however it was typed."""
        assert is_lcsc_part(value)

    @pytest.mark.parametrize(
        "value",
        ["", None, "C", "12345", "CC1", "C12a45", "foo C12345 bar", "C12345,C9"],
    )
    def test_anything_else_is_rejected(self, value):
        """A value that is not exactly one part number is not one."""
        assert not is_lcsc_part(value)

    @pytest.mark.parametrize("value", ["C１２３", "C١٢٣"])
    def test_digits_from_other_scripts_are_not_a_part_number(self, value):
        """Only ASCII digits count, because only those are issued.

        The pattern spells its digits out as [0-9] for this reason, rather
        than using the shorthand class, which is not limited to ASCII. The
        part-preference validator folded onto this predicate rejected
        full-width digits before it was folded, and still has to.
        """
        assert not is_lcsc_part(value)


class TestExtractLcsc:
    """extract_lcsc is the lenient counterpart, for text a person pasted."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("C12345", "C12345"),
            ("c12345", "C12345"),
            ("  C12345  ", "C12345"),
            ("LCSC Part C12345 in stock", "C12345"),
            ("https://jlcpcb.com/partdetail/C12345", "C12345"),
            ("C12345, C99999", "C12345"),
        ],
    )
    def test_a_part_number_is_pulled_out_canonically(self, text, expected):
        """A number is found inside surrounding text and returned canonically."""
        assert extract_lcsc(text) == expected

    @pytest.mark.parametrize("text", ["", None, "no part here", "12345"])
    def test_text_without_a_part_number_yields_empty(self, text):
        """Text carrying no part number produces the empty string."""
        assert extract_lcsc(text) == ""

    def test_it_is_more_lenient_than_is_lcsc_part(self):
        """The two answer different questions and are not interchangeable.

        Pasted text should give up its part number; a schematic field claiming
        to *be* a part number should not be accepted when it is something else
        with a number buried in it.
        """
        text = "LCSC Part C12345 in stock"
        assert extract_lcsc(text) == "C12345"
        assert not is_lcsc_part(text)
