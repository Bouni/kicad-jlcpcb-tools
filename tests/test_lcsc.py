"""Tests for the questions lcsc.py answers about a part number.

normalize_lcsc has its own file, tests/test_lcsc_normalization.py. What is
covered here is what sits on top of it: the strict predicate a field has to
satisfy, the lenient parser that reads pasted text, and the Lcsc value type
that both of those define.
"""

import dataclasses
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
Lcsc = _lcsc.Lcsc
format_lcsc = _lcsc.format_lcsc


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


# ---------------------------------------------------------------------------
# The value type
# ---------------------------------------------------------------------------


class TestLcscConstruction:
    """An Lcsc in hand is a real part number in canonical form."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("C12345", "C12345"),
            ("c12345", "C12345"),
            (" C12345 ", "C12345"),
            ("\tc12345\n", "C12345"),
            ("C9900101779", "C9900101779"),
        ],
    )
    def test_it_canonicalises_what_it_is_given(self, value, expected):
        """Every spelling of one number produces the same part."""
        assert str(Lcsc(value)) == expected

    @pytest.mark.parametrize(
        "value", ["", None, "C", "12345", "CC1", "C12a45", "foo C12345 bar"]
    )
    def test_it_refuses_anything_that_is_not_one(self, value):
        """The constructor is the check, so holding one needs no further test."""
        with pytest.raises((ValueError, TypeError)):
            Lcsc(value)

    @pytest.mark.parametrize("value", ["C１２３", "C١٢٣"])
    def test_digits_from_other_scripts_do_not_build_a_part(self, value):
        r"""Only ASCII digits count, and the type inherits that from the pattern.

        Python's ``\d`` matches every Unicode decimal, so full-width and
        Arabic-Indic digits would otherwise build a validated part that no
        catalogue contains and no query can find.
        """
        assert Lcsc.parse(value) is None

    def test_parse_answers_none_instead_of_raising(self):
        """Values merely claimed to be part numbers go through parse."""
        assert Lcsc.parse("C12345") == Lcsc("C12345")
        assert Lcsc.parse("not a part") is None
        assert Lcsc.parse(None) is None

    def test_find_in_reads_pasted_text(self):
        """find_in is lenient about surroundings, strict about the number."""
        assert Lcsc.find_in("LCSC Part C12345 in stock") == Lcsc("C12345")
        assert Lcsc.find_in("no part here") is None
        assert Lcsc.find_in("") is None


class TestLcscBehaviour:
    """It formats and compares as the thing it represents."""

    def test_it_renders_as_the_bare_number(self):
        """Rendering gives the canonical number, not a repr."""
        part = Lcsc.parse(" c12345 ")
        assert str(part) == "C12345"
        assert f"ordering {part}" == "ordering C12345"

    def test_spellings_compare_equal(self):
        """Two spellings of one number are one part."""
        assert Lcsc("c12345 ") == Lcsc("C12345")

    def test_different_parts_are_not_equal(self):
        """Distinct numbers stay distinct."""
        assert Lcsc("C12345") != Lcsc("C12346")

    def test_it_works_as_a_dict_key(self):
        """Being frozen makes it usable as a cache key, which is how it is used.

        The details cache in the parts list is keyed on it, so two spellings
        collapsing to one entry is the property that matters.
        """
        cache = {Lcsc("C12345"): "details"}
        assert cache[Lcsc(" c12345 ")] == "details"
        assert len({Lcsc("C12345"), Lcsc("c12345")}) == 1

    def test_it_is_immutable(self):
        """A part cannot be edited into a different part after validation."""
        part = Lcsc("C12345")
        with pytest.raises(dataclasses.FrozenInstanceError):
            part.value = "C99999"

    def test_parts_do_not_compare_as_greater_or_lesser(self):
        """Ordering is left undefined on purpose, so nobody relies on a wrong one.

        String order puts C10000 before C9999, and numeric order would invent
        a ranking that means nothing, since part numbers are identifiers
        rather than quantities. Refusing the comparison is better than
        answering it misleadingly.
        """
        with pytest.raises(TypeError):
            Lcsc("C10000") < Lcsc("C9999")


class TestHelpersAgreeWithTheType:
    """The string helpers and the type are one definition, not two."""

    @pytest.mark.parametrize(
        "value", ["C12345", "c12345", " C12345 ", "C999", "C", "", None, "junk"]
    )
    def test_is_lcsc_part_matches_parse(self, value):
        """is_lcsc_part is true exactly when parse returns a part."""
        assert is_lcsc_part(value) == (Lcsc.parse(value) is not None)

    @pytest.mark.parametrize(
        "text", ["C12345", "buy C12345 now", "C1,C2", "", None, "no part"]
    )
    def test_extract_lcsc_matches_find_in(self, text):
        """extract_lcsc is find_in rendered as a string."""
        found = Lcsc.find_in(text)
        assert extract_lcsc(text) == (str(found) if found else "")


class TestAbsence:
    """Absence is None inside the plugin and "" only at the edges."""

    def test_there_is_no_empty_part(self):
        """No value of Lcsc represents "no part"; that is what None is for."""
        assert Lcsc.parse("") is None
        assert Lcsc.parse(None) is None
        with pytest.raises(ValueError):
            Lcsc("")

    def test_format_renders_a_part_as_its_number(self):
        """A part renders as the bare canonical number."""
        assert format_lcsc(Lcsc(" c12345 ")) == "C12345"

    def test_format_renders_absence_as_empty(self):
        """None becomes the empty string the storage layers expect."""
        assert format_lcsc(None) == ""

    def test_format_is_what_a_string_boundary_should_call(self):
        """Rendering an absent part never produces "None" in a cell.

        str(None) would put the text "None" into a CSV cell or a list column,
        which is the failure this helper exists to make impossible.
        """
        assert format_lcsc(None) != str(None)
