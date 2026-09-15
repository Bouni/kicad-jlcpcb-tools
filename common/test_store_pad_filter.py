"""Tests for store pad filtering helpers used by BOM estimator metadata."""

import importlib.util
from pathlib import Path
import sys
import types

# Provide minimal wx stubs so root-level helpers/store imports succeed in tests.
if "wx" not in sys.modules:
    sys.modules["wx"] = types.ModuleType("wx")
if "wx.dataview" not in sys.modules:
    sys.modules["wx.dataview"] = types.ModuleType("wx.dataview")

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


footprint_metadata_module = _load_root_module("footprint_metadata")
count_pad = footprint_metadata_module.count_pad
get_footprint_pad_count = footprint_metadata_module.get_footprint_pad_count
get_footprint_pads = footprint_metadata_module.get_footprint_pads
footprint_has_tht = footprint_metadata_module.footprint_has_tht

# Imported here for the round-trip test below; the import sits below the
# package bootstrap so bom_estimation resolves correctly.
from bom_estimation.pricing import (  # noqa: E402  pylint: disable=wrong-import-position,import-error
    get_assembly_flags as parse_assembly_flags,
)


class _Drill:
    def __init__(self, x=0, y=0):
        self.x = x
        self.y = y


class _Pad:
    def __init__(
        self,
        *,
        npth=False,
        plated=True,
        attribute="",
        has_hole=False,
        drill_x=0,
        drill_y=0,
    ):
        self._npth = npth
        self._plated = plated
        self._attribute = attribute
        self._has_hole = has_hole
        self._drill = _Drill(drill_x, drill_y)

    def IsNPTH(self):
        return self._npth

    def IsPlated(self):
        return self._plated

    def GetAttribute(self):
        return self._attribute

    def HasHole(self):
        return self._has_hole

    def GetDrillSize(self):
        return self._drill


class _FootprintPads:
    def __init__(self, pads):
        self._pads = pads

    def Pads(self):
        return self._pads


class _FootprintGetPads:
    def __init__(self, pads):
        self._pads = pads

    def GetPads(self):
        return self._pads


def test_count_pad_filters_npth_non_plated_and_attribute_markers():
    """count_pad excludes clearly non-joint pads using multiple API signals."""
    assert not count_pad(_Pad(npth=True))
    assert not count_pad(_Pad(plated=False))
    assert not count_pad(_Pad(attribute="PAD_ATTRIB_NPTH"))
    assert not count_pad(_Pad(attribute="nonplated_mech"))
    assert count_pad(_Pad())


def test_get_footprint_pad_count_counts_only_countable_pads():
    """Pad count includes only pads accepted by count_pad."""
    fp = _FootprintPads(
        [
            _Pad(),
            _Pad(npth=True),
            _Pad(plated=False),
            _Pad(attribute="NPTH"),
            _Pad(),
        ]
    )

    assert get_footprint_pad_count(fp) == 2


def test_get_footprint_pads_supports_getpads_fallback():
    """Footprint pad collection supports the GetPads API variant."""
    pads = [_Pad(), _Pad()]
    fp = _FootprintGetPads(pads)

    assert list(get_footprint_pads(fp)) == pads


def test_footprint_has_tht_ignores_filtered_npth_holes():
    """NPTH pads with holes should not trigger THT detection."""
    fp = _FootprintPads([_Pad(npth=True, has_hole=True, drill_x=100)])

    assert not footprint_has_tht(fp)


def test_footprint_has_tht_detects_plated_drilled_or_holed_pad():
    """Plated pads with holes/drill are treated as THT."""
    fp_hole = _FootprintPads([_Pad(has_hole=True)])
    assert footprint_has_tht(fp_hole)

    fp_drill = _FootprintPads([_Pad(drill_x=100, drill_y=0)])
    assert footprint_has_tht(fp_drill)


class _AssemblyFootprint:
    """Footprint state needed by assembly-flag serialization tests."""

    def __init__(
        self,
        *,
        reference="R1",
        pads=None,
        attributes=0,
        is_dnp=False,
    ):
        self._reference = reference
        self._pads = pads or []
        self._attributes = attributes
        self._is_dnp = is_dnp

    def GetReference(self):
        return self._reference

    def Pads(self):
        return self._pads

    def GetAttributes(self):
        return self._attributes

    def IsDNP(self):
        return self._is_dnp


def test_assembly_flags_round_trip_writer_keys_match_reader_expectations():
    """Keys produced by footprint_metadata.get_assembly_flags must parse back via pricing.get_assembly_flags."""
    fp = _AssemblyFootprint(reference="R1", pads=[_Pad()], is_dnp=True)
    flags_json = footprint_metadata_module.get_assembly_flags(fp)

    parsed = parse_assembly_flags({"assembly_flags": flags_json})

    # Writer should emit all three keys, and reader should parse them back.
    assert set(parsed) == {"exclude_from_bom", "exclude_from_pos", "is_dnp"}
    # is_dnp came from the footprint stub.
    assert parsed["is_dnp"] is True
