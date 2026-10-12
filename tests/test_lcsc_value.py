"""Tests for the Lcsc value type.

The string helpers it is built on have their own file,
tests/test_lcsc_normalization.py; what is covered here is the type they define.
"""

from collections.abc import Mapping
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
LcscDict = _lcsc.LcscDict


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


class TestLcscDict:
    """A part-keyed dict refuses every key that is not a part.

    An Lcsc never equals a str, so a string key in a part-keyed dict fails
    silently: written and never read, or asked for and never found.
    """

    def test_it_finds_a_value_under_any_spelling_of_its_part(self):
        """Keys are parts, so equivalent spellings reach one entry."""
        parts = LcscDict()
        parts[Lcsc("C12345")] = "resistor"

        assert parts[Lcsc(" c12345 ")] == "resistor"
        assert Lcsc("c12345") in parts
        assert Lcsc("C1") not in parts
        assert list(parts) == [Lcsc("C12345")]
        assert len(parts) == 1

    @pytest.mark.parametrize(
        "use",
        [
            pytest.param(lambda d: d.__setitem__("C12345", 1), id="set"),
            pytest.param(lambda d: d["C12345"], id="get"),
            pytest.param(lambda d: "C12345" in d, id="contains"),
            pytest.param(lambda d: d.get("C12345"), id="get-default"),
            pytest.param(lambda d: d.pop("C12345", None), id="pop"),
            pytest.param(lambda d: d.setdefault("C12345", 1), id="setdefault"),
            pytest.param(lambda d: d.__delitem__("C12345"), id="delete"),
            pytest.param(lambda d: d.update({"C12345": 1}), id="update"),
            pytest.param(lambda d: LcscDict({"C12345": 1}), id="construct"),
        ],
    )
    def test_it_refuses_a_string_key_however_it_is_used(self, use):
        """Every read and write path raises rather than silently missing."""
        parts = LcscDict({Lcsc("C12345"): 0})

        with pytest.raises(TypeError, match="Lcsc"):
            use(parts)
        assert dict(parts) == {Lcsc("C12345"): 0}

    def test_it_equals_a_dict_holding_the_same_entries(self):
        """Comparison is by content, so an empty cache still equals {}."""
        assert LcscDict() == {}
        assert LcscDict({Lcsc("C1"): 1}) == {Lcsc("C1"): 1}
        assert LcscDict({Lcsc("C1"): 1}) != {Lcsc("C1"): 2}

    @pytest.mark.parametrize(
        "key",
        [
            pytest.param(None, id="none"),
            pytest.param("", id="blank"),
            pytest.param(12345, id="number"),
            pytest.param(b"C12345", id="bytes"),
            pytest.param(["C12345"], id="list"),
            pytest.param({"C12345": 1}, id="dict"),
            pytest.param({"C12345"}, id="set"),
        ],
    )
    def test_it_refuses_any_other_kind_of_key_before_hashing_it(self, key):
        """None, numbers and unhashable values get the same error as a string.

        The key is checked before the underlying dict sees it, so a list is
        reported as the wrong kind of key rather than as unhashable.
        """
        parts = LcscDict({Lcsc("C12345"): 0})

        with pytest.raises(TypeError, match="keys are Lcsc parts"):
            parts[key] = 1
        with pytest.raises(TypeError, match="keys are Lcsc parts"):
            _ = key in parts
        assert dict(parts) == {Lcsc("C12345"): 0}

    def test_a_part_it_does_not_hold_is_absent_as_in_a_dict(self):
        """A valid part that is missing gets defaults and KeyError, not TypeError."""
        parts = LcscDict({Lcsc("C1"): 1})
        absent = Lcsc("C2")

        assert absent not in parts
        assert parts.get(absent) is None
        assert parts.get(absent, "default") == "default"
        assert parts.pop(absent, "default") == "default"
        with pytest.raises(KeyError):
            _ = parts[absent]
        with pytest.raises(KeyError):
            parts.pop(absent)
        with pytest.raises(KeyError):
            del parts[absent]
        assert parts.setdefault(absent, 2) == 2
        assert dict(parts) == {Lcsc("C1"): 1, Lcsc("C2"): 2}

    @pytest.mark.parametrize("shape", ["mapping", "keys", "pairs"])
    def test_an_update_keeps_the_entries_before_a_bad_key_and_stops_there(self, shape):
        """update() is not atomic, as with dict.update.

        The entries before the bad key are stored, the bad key raises, and
        nothing after it is read, whether the update is a mapping, an object
        with keys(), or an iterable of pairs.
        """
        entries = {Lcsc("C1"): 1, "C2": 2, Lcsc("C3"): 3}
        read = []

        def keys():
            for key in entries:
                read.append(key)
                yield key

        class Tracked(Mapping):
            def __iter__(self):
                return keys()

            def __len__(self):
                return len(entries)

            def __getitem__(self, key):
                return entries[key]

        class KeysOnly:
            def keys(self):
                return keys()

            def __getitem__(self, key):
                return entries[key]

        update = {
            "mapping": Tracked(),
            "keys": KeysOnly(),
            "pairs": ((key, entries[key]) for key in keys()),
        }[shape]
        parts = LcscDict()

        with pytest.raises(TypeError, match="keys are Lcsc parts"):
            parts.update(update)
        assert dict(parts) == {Lcsc("C1"): 1}
        assert read == [Lcsc("C1"), "C2"]
