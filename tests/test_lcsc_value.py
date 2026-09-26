"""Tests for the Lcsc value type.

The string helpers it is built on have their own file,
tests/test_lcsc_normalization.py; what is covered here is the type they define.
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
Lcsc = _lcsc.Lcsc


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
        """Only ASCII digits count, and the type inherits that from is_lcsc_part.

        Full-width and Arabic-Indic digits would otherwise build a validated
        part that no catalogue contains and no query can find.
        """
        assert Lcsc.parse(value) is None

    def test_parse_answers_none_instead_of_raising(self):
        """Values merely claimed to be part numbers go through parse."""
        assert Lcsc.parse("C12345") == Lcsc("C12345")
        assert Lcsc.parse("not a part") is None
        assert Lcsc.parse(None) is None


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


class TestAbsence:
    """Absence is None, never a part that stands for no part."""

    def test_there_is_no_empty_part(self):
        """No value of Lcsc represents "no part"; that is what None is for."""
        assert Lcsc.parse("") is None
        assert Lcsc.parse(None) is None
        with pytest.raises(ValueError):
            Lcsc("")
