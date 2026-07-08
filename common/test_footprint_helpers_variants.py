"""Tests for design-variant-aware footprint helpers.

The fakes below mirror the KiCad 10 variant API semantics (verified against
KiCad 10.0.3): ``*ForVariant`` getters fall back to the base state when the
footprint has no override object for the variant (or when the variant name
is empty), field overrides are sparse with per-field fallback, and creating
a footprint variant override snapshots the base flags at creation time.
"""

import importlib.util
from pathlib import Path
import sys
import types

ROOT = Path(__file__).parent.parent
PACKAGE = "kicad_jlcpcb_tools"

if PACKAGE not in sys.modules:
    pkg = types.ModuleType(PACKAGE)
    pkg.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = pkg


def _load_root_module(name):
    """Load a root module as part of a synthetic package for relative imports."""
    module_name = f"{PACKAGE}.{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]

    spec = importlib.util.spec_from_file_location(module_name, str(ROOT / f"{name}.py"))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module spec for {module_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


helpers = _load_root_module("footprint_helpers")

EXCLUDE_FROM_POS_BIT = 1 << helpers.EXCLUDE_FROM_POS
EXCLUDE_FROM_BOM_BIT = 1 << helpers.EXCLUDE_FROM_BOM


class FakeField:
    """A footprint field with a name and base text."""

    def __init__(self, name, text):
        self._name = name
        self._text = text
        self.visible = True

    def GetName(self):
        """Return the field name."""
        return self._name

    def GetText(self):
        """Return the base field text."""
        return self._text

    def SetText(self, text):
        """Set the base field text."""
        self._text = text

    def SetVisible(self, visible):
        """Set field visibility."""
        self.visible = visible


class FakeFootprintVariant:
    """Mirror of FOOTPRINT_VARIANT: explicit flags plus sparse field overrides."""

    def __init__(self, name, dnp=False, bom=False, pos=False):
        self._name = name
        self._dnp = dnp
        self._bom = bom
        self._pos = pos
        self._fields = {}

    def GetDNP(self):
        """Return the DNP flag."""
        return self._dnp

    def SetDNP(self, dnp):
        """Set the DNP flag."""
        self._dnp = dnp

    def GetExcludedFromBOM(self):
        """Return the exclude-from-BOM flag."""
        return self._bom

    def SetExcludedFromBOM(self, exclude):
        """Set the exclude-from-BOM flag."""
        self._bom = exclude

    def GetExcludedFromPosFiles(self):
        """Return the exclude-from-POS flag."""
        return self._pos

    def SetExcludedFromPosFiles(self, exclude):
        """Set the exclude-from-POS flag."""
        self._pos = exclude

    def HasFieldValue(self, name):
        """Return True when the variant overrides the given field."""
        return name in self._fields

    def GetFieldValue(self, name):
        """Return the override value for a field ("" when absent)."""
        return self._fields.get(name, "")

    def SetFieldValue(self, name, value):
        """Set a field override."""
        self._fields[name] = value

    def GetFields(self):
        """Return the {name: value} override map."""
        return dict(self._fields)


class FakeBoard:
    """Board exposing the KiCad 10 current-variant getter."""

    def __init__(self, variant=""):
        self._variant = variant

    def GetCurrentVariant(self):
        """Return the active variant name."""
        return self._variant


class FakeFootprint:
    """Footprint with the KiCad 10 variant API and probe-verified fallbacks."""

    def __init__(self, board=None, value="", fields=None, attributes=0, dnp=False):
        self._board = board
        self._value = value
        self._fields = list(fields or [])
        self._attributes = attributes
        self._dnp = dnp
        self._variants = {}

    def GetBoard(self):
        """Return the owning board."""
        return self._board

    def GetValue(self):
        """Return the base Value field text."""
        return self._value

    def GetFields(self):
        """Return the base fields."""
        return list(self._fields)

    # KiCad 10 has no FOOTPRINT.GetFieldByName; keep this fake accurate so
    # tests exercise the GetFields-loop branch of set_lcsc_value.
    def _field_by_name(self, name):
        """Return the base field with the given name, or None (test helper)."""
        for field in self._fields:
            if field.GetName() == name:
                return field
        return None

    def SetField(self, name, text):
        """Update an existing base field or append a new one."""
        field = self._field_by_name(name)
        if field:
            field.SetText(text)
        else:
            self._fields.append(FakeField(name, text))

    def GetAttributes(self):
        """Return the base attribute bits."""
        return self._attributes

    def SetAttributes(self, attributes):
        """Set the base attribute bits."""
        self._attributes = attributes

    def IsDNP(self):
        """Return the base DNP state."""
        return self._dnp

    def HasVariant(self, name):
        """Return True when the footprint has an override for the variant."""
        return name in self._variants

    def GetVariant(self, name):
        """Return the override object for the variant, or None."""
        return self._variants.get(name)

    def AddVariant(self, name):
        """Create an override snapshotting the current base flags."""
        variant = FakeFootprintVariant(
            name,
            dnp=self._dnp,
            bom=bool(self._attributes & EXCLUDE_FROM_BOM_BIT),
            pos=bool(self._attributes & EXCLUDE_FROM_POS_BIT),
        )
        self._variants[name] = variant
        return variant

    def DeleteVariant(self, name):
        """Remove the override object for a variant."""
        self._variants.pop(name, None)

    def GetDNPForVariant(self, name):
        """Return the DNP state resolved for a variant (base fallback)."""
        if name in self._variants:
            return self._variants[name].GetDNP()
        return self._dnp

    def GetExcludedFromBOMForVariant(self, name):
        """Return the exclude-from-BOM state resolved for a variant."""
        if name in self._variants:
            return self._variants[name].GetExcludedFromBOM()
        return bool(self._attributes & EXCLUDE_FROM_BOM_BIT)

    def GetExcludedFromPosFilesForVariant(self, name):
        """Return the exclude-from-POS state resolved for a variant."""
        if name in self._variants:
            return self._variants[name].GetExcludedFromPosFiles()
        return bool(self._attributes & EXCLUDE_FROM_POS_BIT)

    def GetFieldValueForVariant(self, name, field_name):
        """Return a field's text resolved for a variant (per-field fallback)."""
        variant = self._variants.get(name)
        if variant is not None and variant.HasFieldValue(field_name):
            return variant.GetFieldValue(field_name)
        field = self._field_by_name(field_name)
        if field is None:
            return ""
        return field.GetText()


class LegacyFootprint:
    """KiCad 8/9 style footprint without any variant API."""

    def __init__(self, value="", fields=None, attributes=0, dnp=False):
        self._value = value
        self._fields = list(fields or [])
        self._attributes = attributes
        self._dnp = dnp

    def GetValue(self):
        """Return the Value field text."""
        return self._value

    def GetFields(self):
        """Return the fields."""
        return list(self._fields)

    def GetAttributes(self):
        """Return the attribute bits."""
        return self._attributes

    def SetAttributes(self, attributes):
        """Set the attribute bits."""
        self._attributes = attributes

    def IsDNP(self):
        """Return the DNP state."""
        return self._dnp


class PropertiesFootprint:
    """KiCad <=6 style footprint with GetProperties instead of GetFields."""

    def __init__(self, properties):
        self._properties = properties

    def GetProperties(self):
        """Return the {name: value} property map."""
        return dict(self._properties)


# ---------------------------------------------------------------------------
# get_current_variant / get_footprint_variant
# ---------------------------------------------------------------------------


def test_get_current_variant_returns_active_name():
    """The active variant name is read from the board."""
    assert helpers.get_current_variant(FakeBoard("VarA")) == "VarA"


def test_get_current_variant_defaults_to_empty():
    """Default variant, missing API, and missing board all map to ""."""
    assert helpers.get_current_variant(FakeBoard("")) == ""
    assert helpers.get_current_variant(object()) == ""
    assert helpers.get_current_variant(None) == ""


def test_get_footprint_variant_without_board_api():
    """A footprint without GetBoard resolves to the default variant."""
    assert helpers.get_footprint_variant(LegacyFootprint()) == ""


# ---------------------------------------------------------------------------
# get_resolved_value
# ---------------------------------------------------------------------------


def test_resolved_value_base_when_no_variant_active():
    """With the default variant active the base value is returned."""
    fp = FakeFootprint(FakeBoard(""), value="10k")
    fp.SetField("Value", "10k")
    assert helpers.get_resolved_value(fp) == "10k"


def test_resolved_value_uses_variant_override():
    """A variant Value override wins over the base value."""
    fp = FakeFootprint(FakeBoard("VarA"), value="10k")
    fp.SetField("Value", "10k")
    fp.AddVariant("VarA").SetFieldValue("Value", "22k")
    assert helpers.get_resolved_value(fp) == "22k"


def test_resolved_value_falls_back_without_override():
    """Without an override the variant resolves to the base value."""
    fp = FakeFootprint(FakeBoard("VarA"), value="10k")
    fp.SetField("Value", "10k")
    assert helpers.get_resolved_value(fp) == "10k"


def test_resolved_value_legacy_footprint():
    """Footprints without the variant API return GetValue()."""
    assert helpers.get_resolved_value(LegacyFootprint(value="4k7")) == "4k7"


# ---------------------------------------------------------------------------
# get_lcsc_value
# ---------------------------------------------------------------------------


def test_lcsc_value_from_base_field():
    """The base LCSC field is found on the default variant."""
    fp = FakeFootprint(FakeBoard(""), fields=[FakeField("LCSC", "C123")])
    assert helpers.get_lcsc_value(fp) == "C123"


def test_lcsc_value_resolves_variant_override():
    """A variant override of the LCSC field wins over the base value."""
    fp = FakeFootprint(FakeBoard("VarA"), fields=[FakeField("LCSC", "C123")])
    fp.AddVariant("VarA").SetFieldValue("LCSC", "C999")
    assert helpers.get_lcsc_value(fp) == "C999"


def test_lcsc_value_variant_fallback_to_base():
    """Without an override the base LCSC number is used in a variant."""
    fp = FakeFootprint(FakeBoard("VarA"), fields=[FakeField("LCSC", "C123")])
    assert helpers.get_lcsc_value(fp) == "C123"


def test_lcsc_value_explicit_empty_override_clears_number():
    """An explicit empty override hides the base number in that variant."""
    fp = FakeFootprint(FakeBoard("VarA"), fields=[FakeField("LCSC", "C123")])
    fp.AddVariant("VarA").SetFieldValue("LCSC", "")
    assert helpers.get_lcsc_value(fp) == ""


def test_lcsc_value_from_variant_only_field():
    """An LCSC override without a base field counterpart is still found."""
    fp = FakeFootprint(FakeBoard("VarA"))
    fp.AddVariant("VarA").SetFieldValue("JLC Part", "C777")
    assert helpers.get_lcsc_value(fp) == "C777"


def test_lcsc_value_legacy_properties_fallback():
    """KiCad <=6 footprints resolve through GetProperties."""
    fp = PropertiesFootprint({"lcsc": "C55"})
    assert helpers.get_lcsc_value(fp) == "C55"


def test_lcsc_value_ignores_non_matching_fields():
    """Fields with non-LCSC names or invalid numbers are skipped."""
    fp = FakeFootprint(
        FakeBoard(""),
        fields=[FakeField("MPN", "C123"), FakeField("LCSC", "not-a-number")],
    )
    assert helpers.get_lcsc_value(fp) == ""


# ---------------------------------------------------------------------------
# set_lcsc_value
# ---------------------------------------------------------------------------


def test_set_lcsc_value_updates_base_field():
    """On the default variant the existing base field is updated."""
    fp = FakeFootprint(FakeBoard(""), fields=[FakeField("LCSC", "C123")])
    helpers.set_lcsc_value(fp, "C456")
    assert fp._field_by_name("LCSC").GetText() == "C456"


def test_set_lcsc_value_creates_hidden_field():
    """Without an existing field a hidden LCSC base field is created."""
    fp = FakeFootprint(FakeBoard(""))
    helpers.set_lcsc_value(fp, "C456")
    field = fp._field_by_name("LCSC")
    assert field.GetText() == "C456"
    assert field.visible is False


def test_set_lcsc_value_updates_variant_override_when_present():
    """An explicit variant override is updated instead of the base field."""
    fp = FakeFootprint(FakeBoard("VarA"), fields=[FakeField("LCSC", "C123")])
    fp.AddVariant("VarA").SetFieldValue("LCSC", "C999")
    helpers.set_lcsc_value(fp, "C456")
    assert fp.GetVariant("VarA").GetFieldValue("LCSC") == "C456"
    assert fp._field_by_name("LCSC").GetText() == "C123"


def test_set_lcsc_value_writes_base_when_variant_has_no_override():
    """Without an override the base field is written and inherited."""
    fp = FakeFootprint(FakeBoard("VarA"), fields=[FakeField("LCSC", "C123")])
    helpers.set_lcsc_value(fp, "C456")
    assert fp._field_by_name("LCSC").GetText() == "C456"
    assert not fp.HasVariant("VarA")


def test_set_lcsc_value_updates_variant_only_field():
    """A variant-only LCSC override is updated in place."""
    fp = FakeFootprint(FakeBoard("VarA"))
    fp.AddVariant("VarA").SetFieldValue("JLC Part", "C777")
    helpers.set_lcsc_value(fp, "C456")
    assert fp.GetVariant("VarA").GetFieldValue("JLC Part") == "C456"
    assert fp._field_by_name("LCSC") is None


def test_set_lcsc_value_updates_cleared_override_not_base():
    """An explicitly cleared override is updated, never the shadowed base field."""
    fp = FakeFootprint(FakeBoard("VarA"), fields=[FakeField("LCSC", "C123")])
    fp.AddVariant("VarA").SetFieldValue("LCSC", "")
    helpers.set_lcsc_value(fp, "C456")
    assert fp.GetVariant("VarA").GetFieldValue("LCSC") == "C456"
    assert fp._field_by_name("LCSC").GetText() == "C123"
    assert helpers.get_lcsc_value(fp) == "C456"


def test_set_lcsc_value_remove_then_reassign_round_trip():
    """Removing and re-assigning in a variant keeps the override in charge."""
    fp = FakeFootprint(FakeBoard("VarA"), fields=[FakeField("LCSC", "C123")])
    fp.AddVariant("VarA").SetFieldValue("LCSC", "C999")
    helpers.set_lcsc_value(fp, "")
    assert helpers.get_lcsc_value(fp) == ""
    helpers.set_lcsc_value(fp, "C456")
    assert helpers.get_lcsc_value(fp) == "C456"
    # The base field and thus other variants never changed.
    assert fp._field_by_name("LCSC").GetText() == "C123"


def test_set_lcsc_value_placeholder_override_updated():
    """A non-part placeholder override (e.g. 'TBD') is updated in place."""
    fp = FakeFootprint(FakeBoard("VarA"), fields=[FakeField("LCSC", "C123")])
    fp.AddVariant("VarA").SetFieldValue("LCSC", "TBD")
    helpers.set_lcsc_value(fp, "C456")
    assert fp.GetVariant("VarA").GetFieldValue("LCSC") == "C456"
    assert fp._field_by_name("LCSC").GetText() == "C123"


# ---------------------------------------------------------------------------
# lcsc_override_cleared
# ---------------------------------------------------------------------------


def test_lcsc_override_cleared_detects_explicit_clear():
    """An explicit empty override marks the part as cleared for the variant."""
    fp = FakeFootprint(FakeBoard("VarA"), fields=[FakeField("LCSC", "C123")])
    fp.AddVariant("VarA").SetFieldValue("LCSC", "")
    assert helpers.lcsc_override_cleared(fp) is True


def test_lcsc_override_cleared_false_cases():
    """No variant, no override, or a valid override are not 'cleared'."""
    fp = FakeFootprint(FakeBoard(""), fields=[FakeField("LCSC", "C123")])
    assert helpers.lcsc_override_cleared(fp) is False
    fp = FakeFootprint(FakeBoard("VarA"), fields=[FakeField("LCSC", "C123")])
    assert helpers.lcsc_override_cleared(fp) is False
    fp.AddVariant("VarA").SetFieldValue("LCSC", "C999")
    assert helpers.lcsc_override_cleared(fp) is False


# ---------------------------------------------------------------------------
# flag getters
# ---------------------------------------------------------------------------


def test_flag_getters_base_state_on_default_variant():
    """Attribute bits and IsDNP drive the default variant state."""
    fp = FakeFootprint(
        FakeBoard(""),
        attributes=EXCLUDE_FROM_BOM_BIT | EXCLUDE_FROM_POS_BIT,
        dnp=True,
    )
    assert helpers.get_exclude_from_bom(fp) is True
    assert helpers.get_exclude_from_pos(fp) is True
    assert helpers.get_is_dnp(fp) is True


def test_flag_getters_resolve_variant_override():
    """Explicit variant overrides win over the base flags."""
    fp = FakeFootprint(FakeBoard("VarA"))
    override = fp.AddVariant("VarA")
    override.SetDNP(True)
    override.SetExcludedFromBOM(True)
    override.SetExcludedFromPosFiles(True)
    assert helpers.get_is_dnp(fp) is True
    assert helpers.get_exclude_from_bom(fp) is True
    assert helpers.get_exclude_from_pos(fp) is True
    # The base state is untouched.
    assert fp.IsDNP() is False
    assert fp.GetAttributes() == 0


def test_flag_getters_fall_back_without_override():
    """Without an override object the base flags shine through."""
    fp = FakeFootprint(
        FakeBoard("VarA"),
        attributes=EXCLUDE_FROM_BOM_BIT,
        dnp=True,
    )
    assert helpers.get_is_dnp(fp) is True
    assert helpers.get_exclude_from_bom(fp) is True
    assert helpers.get_exclude_from_pos(fp) is False


def test_flag_getters_legacy_footprint():
    """Footprints without the variant API use base attribute bits."""
    fp = LegacyFootprint(attributes=EXCLUDE_FROM_POS_BIT, dnp=True)
    assert helpers.get_exclude_from_pos(fp) is True
    assert helpers.get_exclude_from_bom(fp) is False
    assert helpers.get_is_dnp(fp) is True


def test_flag_getters_none_footprint():
    """None footprints return the documented defaults."""
    assert helpers.get_exclude_from_bom(None) is None
    assert helpers.get_exclude_from_pos(None) is None
    assert helpers.get_is_dnp(None) is False


# ---------------------------------------------------------------------------
# flag toggles
# ---------------------------------------------------------------------------


def test_toggle_bom_flips_base_bit_on_default_variant():
    """On the default variant the base attribute bit is flipped."""
    fp = FakeFootprint(FakeBoard(""))
    assert helpers.toggle_exclude_from_bom(fp) is True
    assert fp.GetAttributes() == EXCLUDE_FROM_BOM_BIT
    assert helpers.toggle_exclude_from_bom(fp) is False
    assert fp.GetAttributes() == 0


def test_toggle_pos_flips_base_bit_on_default_variant():
    """On the default variant the base attribute bit is flipped."""
    fp = FakeFootprint(FakeBoard(""))
    assert helpers.toggle_exclude_from_pos(fp) is True
    assert fp.GetAttributes() == EXCLUDE_FROM_POS_BIT


def test_toggle_bom_creates_override_and_preserves_base():
    """Toggling in a variant creates an override and leaves the base alone."""
    fp = FakeFootprint(FakeBoard("VarA"), attributes=EXCLUDE_FROM_POS_BIT, dnp=True)
    assert helpers.toggle_exclude_from_bom(fp) is True
    override = fp.GetVariant("VarA")
    assert override.GetExcludedFromBOM() is True
    # The snapshot keeps the effective state of the other flags.
    assert override.GetExcludedFromPosFiles() is True
    assert override.GetDNP() is True
    # Base attributes stay untouched.
    assert fp.GetAttributes() == EXCLUDE_FROM_POS_BIT


def test_toggle_bom_flips_existing_override():
    """Toggling in a variant flips the existing override state."""
    fp = FakeFootprint(FakeBoard("VarA"))
    fp.AddVariant("VarA").SetExcludedFromBOM(True)
    assert helpers.toggle_exclude_from_bom(fp) is False
    assert helpers.get_exclude_from_bom(fp) is False
    # The flipped override mirrored the base exactly and was dropped.
    assert not fp.HasVariant("VarA")
    assert fp.GetAttributes() == 0


def test_toggle_pos_flips_existing_override():
    """Toggling POS in a variant flips the override, not the base bits."""
    fp = FakeFootprint(FakeBoard("VarA"), attributes=EXCLUDE_FROM_POS_BIT)
    assert helpers.toggle_exclude_from_pos(fp) is False
    assert fp.GetVariant("VarA").GetExcludedFromPosFiles() is False
    assert fp.GetAttributes() == EXCLUDE_FROM_POS_BIT


def test_toggle_legacy_footprint_uses_base_bits():
    """Footprints without the variant API keep the old toggle behavior."""
    fp = LegacyFootprint()
    assert helpers.toggle_exclude_from_bom(fp) is True
    assert fp.GetAttributes() == EXCLUDE_FROM_BOM_BIT


def test_toggle_none_footprint():
    """None footprints return None."""
    assert helpers.toggle_exclude_from_bom(None) is None
    assert helpers.toggle_exclude_from_pos(None) is None


def test_toggle_round_trip_drops_redundant_override():
    """Toggling twice removes the override so inheritance is restored."""
    fp = FakeFootprint(FakeBoard("VarA"), attributes=EXCLUDE_FROM_POS_BIT)
    assert helpers.toggle_exclude_from_bom(fp) is True
    assert fp.HasVariant("VarA")
    assert helpers.toggle_exclude_from_bom(fp) is False
    assert not fp.HasVariant("VarA")
    # Base attributes never changed along the way.
    assert fp.GetAttributes() == EXCLUDE_FROM_POS_BIT


def test_toggle_round_trip_keeps_override_with_other_changes():
    """An override that still differs from base survives a toggle round trip."""
    fp = FakeFootprint(FakeBoard("VarA"))
    fp.AddVariant("VarA").SetDNP(True)
    assert helpers.toggle_exclude_from_bom(fp) is True
    assert helpers.toggle_exclude_from_bom(fp) is False
    assert fp.HasVariant("VarA")
    assert fp.GetVariant("VarA").GetDNP() is True


def test_toggle_round_trip_keeps_override_with_field_overrides():
    """An override carrying field overrides survives a toggle round trip."""
    fp = FakeFootprint(FakeBoard("VarA"))
    fp.AddVariant("VarA").SetFieldValue("LCSC", "C999")
    assert helpers.toggle_exclude_from_pos(fp) is True
    assert helpers.toggle_exclude_from_pos(fp) is False
    assert fp.HasVariant("VarA")
    assert fp.GetVariant("VarA").GetFieldValue("LCSC") == "C999"
