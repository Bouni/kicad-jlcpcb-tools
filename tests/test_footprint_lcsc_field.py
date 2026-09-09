"""Tests for reading and writing the LCSC field on a footprint (issue #773)."""

import importlib.util
from pathlib import Path
import sys
import types

import pytest

_ROOT = Path(__file__).parent.parent

# footprint_helpers imports .lcsc relatively, so give it a package to resolve
# against. Neither module touches wx or pcbnew, so both load for real.
_pkg = sys.modules.get("kicadplugin")
if _pkg is None:
    _pkg = types.ModuleType("kicadplugin")
    _pkg.__path__ = [str(_ROOT)]
    sys.modules["kicadplugin"] = _pkg

_spec = importlib.util.spec_from_file_location(
    "kicadplugin.footprint_helpers", _ROOT / "footprint_helpers.py"
)
assert _spec is not None and _spec.loader is not None
_fh = importlib.util.module_from_spec(_spec)
_fh.__package__ = "kicadplugin"
sys.modules["kicadplugin.footprint_helpers"] = _fh
_spec.loader.exec_module(_fh)

get_lcsc_value = _fh.get_lcsc_value
set_lcsc_value = _fh.set_lcsc_value


class FakeField:
    """A KiCad footprint field."""

    def __init__(self, name, text):
        self.name = name
        self.text = text
        self.visible = True

    def GetName(self):
        """Return the field name."""
        return self.name

    def GetText(self):
        """Return the field value."""
        return self.text

    def SetVisible(self, visible):
        """Record the field's visibility."""
        self.visible = visible


class FakeFootprint:
    """A KiCad 7.99+ footprint, which exposes fields rather than properties."""

    def __init__(self, *fields):
        self.fields = list(fields)

    def GetFields(self):
        """Return every field on the footprint."""
        return self.fields

    def SetField(self, name, value):
        """Set a field by name, adding it if it does not exist yet."""
        for field in self.fields:
            if field.GetName() == name:
                field.text = value
                return
        self.fields.append(FakeField(name, value))


class LegacyFootprint:
    """A KiCad <= 7 footprint, which exposes a properties dict."""

    def __init__(self, properties):
        self.properties = properties

    def GetFields(self):
        """Raise the way the older API does, so callers fall back."""
        raise AttributeError("GetFields")

    def GetProperties(self):
        """Return the footprint's properties."""
        return self.properties


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


class TestReadingTheField:
    """A part number is recognised whatever spacing or case it was typed in."""

    @pytest.mark.parametrize(
        "text",
        [
            "C12345",
            "C12345 ",
            " C12345",
            "  C12345  ",
            "C12345\n",
            "\tC12345",
            "c12345",
        ],
    )
    def test_a_part_number_is_found(self, text):
        r"""Stray whitespace or lower case no longer hides the part.

        Reported as issue #773: a schematic field of "C12345 " left the part
        blank in the parts list, because the value went straight into
        re.match(r"^C\\d+$", ...) which a trailing space fails. Lower case
        failed the same test, and a trailing newline passed it, since $ also
        matches before a final newline.
        """
        footprint = FakeFootprint(FakeField("LCSC Part #", text))
        assert get_lcsc_value(footprint) == "C12345"

    @pytest.mark.parametrize("text", ["", "  ", "not a part", "C", "12345", "CC12345"])
    def test_a_non_part_number_is_still_rejected(self, text):
        """Values that do not name a part are still ignored."""
        footprint = FakeFootprint(FakeField("LCSC Part #", text))
        assert get_lcsc_value(footprint) == ""

    def test_the_users_own_field_wins_over_a_plugin_written_one(self):
        """An untrimmed field is preferred over a later duplicate.

        Before normalisation the untrimmed field failed the match, so a second
        field written by the plugin was returned instead -- the BOM, CPL and
        corrections all silently used a different part than the schematic said.
        """
        footprint = FakeFootprint(
            FakeField("LCSC Part #", "C12345 "),
            FakeField("LCSC", "C99999"),
        )
        assert get_lcsc_value(footprint) == "C12345"

    def test_legacy_properties_are_normalised_too(self):
        """The KiCad <= 7 properties path gets the same treatment."""
        footprint = LegacyFootprint({"LCSC Part #": " c12345 "})
        assert get_lcsc_value(footprint) == "C12345"


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


class TestWritingTheField:
    """Assignments write the canonical form, and reuse the field already there."""

    def test_an_untrimmed_field_is_reused_not_duplicated(self):
        """Assigning a part overwrites the existing field and repairs it.

        The original set_lcsc_value used the reader's raw regex to find the
        field to overwrite, so an untrimmed one was invisible to it and a
        second field named LCSC was added alongside. #809 already widened the
        write-side test; this guards that the shared predicate keeps it wide.
        """
        field = FakeField("LCSC Part #", "C12345 ")
        footprint = FakeFootprint(field)
        set_lcsc_value(footprint, "C99999")
        assert [f.GetName() for f in footprint.GetFields()] == ["LCSC Part #"]
        assert field.GetText() == "C99999"

    def test_it_writes_the_field_that_reading_will_use(self):
        """Assignment lands in the field get_lcsc_value reads back.

        A footprint can carry more than one matching field -- the duplicate
        pair older releases created is exactly that. Reading takes the first
        match and writing (since #809) fills every claiming field, so the two
        agree by construction; this guards that the read side's new tolerance
        does not reopen a gap between them.
        """
        footprint = FakeFootprint(
            FakeField("LCSC Part #", "C12345 "),
            FakeField("LCSC", "C99999"),
        )
        set_lcsc_value(footprint, "C77777")
        assert get_lcsc_value(footprint) == "C77777"

    def test_every_field_claiming_the_part_is_rewritten(self):
        """A duplicate is repaired rather than left holding a stale number.

        Writing into every field that claims the part is #809's behaviour,
        kept here: leaving the second field on its old value would only move
        the disagreement, since a later read that picks the other field still
        returns a part the user never assigned.
        """
        user_field = FakeField("LCSC Part #", "C12345 ")
        stale = FakeField("LCSC", "C99999")
        set_lcsc_value(FakeFootprint(user_field, stale), "C77777")
        assert user_field.GetText() == "C77777"
        assert stale.GetText() == "C77777"

    def test_clearing_removes_every_claim_on_the_part(self):
        """Removal clears all matching fields, not just the first.

        remove_lcsc_number exists to say "this part has no LCSC number". A
        duplicate field left over from an older version still claiming one
        would put the part straight back on the next reload, since the store
        is rebuilt from the board. #809's write-all loop covers this; the
        guard is that an untrimmed duplicate now counts as claiming too.
        """
        footprint = FakeFootprint(
            FakeField("LCSC Part #", "C12345 "),
            FakeField("LCSC", "C99999"),
        )
        set_lcsc_value(footprint, "")
        assert get_lcsc_value(footprint) == ""

    def test_the_written_value_is_canonical(self):
        """A part number is stored trimmed and upper case."""
        footprint = FakeFootprint(FakeField("LCSC Part #", "C12345"))
        set_lcsc_value(footprint, " c99999 ")
        assert footprint.GetFields()[0].GetText() == "C99999"

    def test_a_footprint_without_a_field_gains_a_hidden_one(self):
        """The existing behaviour of adding an LCSC field is unchanged."""
        footprint = FakeFootprint()
        set_lcsc_value(footprint, "C12345")
        assert [f.GetName() for f in footprint.GetFields()] == ["LCSC"]
        assert footprint.GetFields()[0].GetText() == "C12345"

    def test_clearing_the_value_still_works(self):
        """Removing a part number empties the field rather than writing junk."""
        field = FakeField("LCSC Part #", "C12345")
        footprint = FakeFootprint(field)
        set_lcsc_value(footprint, "")
        assert field.GetText() == ""
