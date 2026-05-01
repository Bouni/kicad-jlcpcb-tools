"""Tests for store pad filtering helpers used by BOM estimator metadata."""

import contextlib
import importlib.util
import logging
from pathlib import Path
import sqlite3
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


store_module = _load_root_module("store")
Store = store_module.Store


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


def _store_obj():
    """Create a Store object without invoking full constructor side effects."""
    return Store.__new__(Store)


def test_count_pad_filters_npth_non_plated_and_attribute_markers():
    """count_pad excludes clearly non-joint pads using multiple API signals."""
    s = _store_obj()

    assert not s.count_pad(_Pad(npth=True))
    assert not s.count_pad(_Pad(plated=False))
    assert not s.count_pad(_Pad(attribute="PAD_ATTRIB_NPTH"))
    assert not s.count_pad(_Pad(attribute="nonplated_mech"))
    assert s.count_pad(_Pad())


def test_get_footprint_pad_count_counts_only_countable_pads():
    """Pad count includes only pads accepted by count_pad."""
    s = _store_obj()
    fp = _FootprintPads(
        [
            _Pad(),
            _Pad(npth=True),
            _Pad(plated=False),
            _Pad(attribute="NPTH"),
            _Pad(),
        ]
    )

    assert s.get_footprint_pad_count(fp) == 2


def test_get_footprint_pads_supports_getpads_fallback():
    """Footprint pad collection supports the GetPads API variant."""
    s = _store_obj()
    pads = [_Pad(), _Pad()]
    fp = _FootprintGetPads(pads)

    assert list(s.get_footprint_pads(fp)) == pads


def test_footprint_has_tht_ignores_filtered_npth_holes():
    """NPTH pads with holes should not trigger THT detection."""
    s = _store_obj()
    fp = _FootprintPads([_Pad(npth=True, has_hole=True, drill_x=100)])

    assert not s.footprint_has_tht(fp)


def test_footprint_has_tht_detects_plated_drilled_or_holed_pad():
    """Plated pads with holes/drill are treated as THT."""
    s = _store_obj()

    fp_hole = _FootprintPads([_Pad(has_hole=True)])
    assert s.footprint_has_tht(fp_hole)

    fp_drill = _FootprintPads([_Pad(drill_x=100, drill_y=0)])
    assert s.footprint_has_tht(fp_drill)


def test_set_lcsc_resets_cached_assembly_metadata(tmp_path):
    """Changing LCSC clears stale enrichment metadata for re-fetch."""
    s = _store_obj()
    s.logger = logging.getLogger(__name__)
    s.dbfile = str(tmp_path / "project.db")

    s.create_db()

    with contextlib.closing(sqlite3.connect(s.dbfile)) as con, con as cur:
        cur.execute(
            "INSERT INTO part_info ("
            "reference, value, footprint, lcsc, stock, exclude_from_bom, exclude_from_pos"
            ") VALUES (:reference, :value, :footprint, :lcsc, :stock, :exclude_from_bom, :exclude_from_pos)",
            {
                "reference": "R1",
                "value": "10k",
                "footprint": "R_0603",
                "lcsc": "COLD",
                "stock": 1,
                "exclude_from_bom": 0,
                "exclude_from_pos": 0,
            },
        )
        cur.execute(
            "UPDATE part_info SET assembly_process = 'SMT', component_product_type = 0 WHERE reference = 'R1'"
        )
        cur.commit()

    s.set_lcsc("R1", "CNEW")

    with contextlib.closing(sqlite3.connect(s.dbfile)) as con, con as cur:
        row = cur.execute(
            "SELECT lcsc, assembly_process, component_product_type FROM part_info WHERE reference = 'R1'"
        ).fetchone()

    assert row[0] == "CNEW"
    assert row[1] == ""
    assert row[2] is None


def test_get_assembly_enrichment_targets_uses_or_logic(tmp_path):
    """Rows missing any required enrichment field should be selected."""
    s = _store_obj()
    s.logger = logging.getLogger(__name__)
    s.dbfile = str(tmp_path / "project.db")

    s.create_db()

    with contextlib.closing(sqlite3.connect(s.dbfile)) as con, con as cur:
        cur.execute(
            "INSERT INTO part_info (reference, value, footprint, lcsc, stock, exclude_from_bom, exclude_from_pos, assembly_process, component_product_type) "
            "VALUES ('R1', '10k', 'R_0603', 'C1', 1, 0, 0, '', NULL)"
        )
        cur.execute(
            "INSERT INTO part_info (reference, value, footprint, lcsc, stock, exclude_from_bom, exclude_from_pos, assembly_process, component_product_type) "
            "VALUES ('R2', '1u', 'C_0603', 'C2', 1, 0, 0, 'SMT', NULL)"
        )
        cur.execute(
            "INSERT INTO part_info (reference, value, footprint, lcsc, stock, exclude_from_bom, exclude_from_pos, assembly_process, component_product_type) "
            "VALUES ('R3', '100n', 'C_0603', 'C3', 1, 0, 0, '', 0)"
        )
        cur.execute(
            "INSERT INTO part_info (reference, value, footprint, lcsc, stock, exclude_from_bom, exclude_from_pos, assembly_process, component_product_type) "
            "VALUES ('R4', '47k', 'R_0603', 'C4', 1, 0, 0, 'SMT', 0)"
        )
        cur.commit()

    targets = s.get_assembly_enrichment_targets()

    assert targets == {
        "C1": ["R1"],
        "C2": ["R2"],
        "C3": ["R3"],
    }

