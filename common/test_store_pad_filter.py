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
footprint_metadata_module = _load_root_module("footprint_metadata")
Store = store_module.Store
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


def _store_obj():
    """Create a Store object without invoking full constructor side effects."""
    return Store.__new__(Store)


def _store_with_enriched_part(
    tmp_path, *, lcsc="C1", assembly_process="SMT", product_type=2
):
    """Create a test store containing one enriched LCSC assignment."""
    store = _store_obj()
    store.logger = logging.getLogger(__name__)
    store.dbfile = str(tmp_path / "project.db")
    store.create_db()
    with contextlib.closing(sqlite3.connect(store.dbfile)) as con, con as cur:
        cur.execute(
            "INSERT INTO part_info ("
            "reference, value, footprint, lcsc, stock, exclude_from_bom, "
            "exclude_from_pos, assembly_process, component_product_type"
            ") VALUES ('R1', '10k', 'R_0603', ?, 1, 0, 0, ?, ?)",
            (lcsc, assembly_process, product_type),
        )
        cur.commit()
    return store


def _update_part(store, lcsc, value="10k"):
    store.update_part(
        {
            "reference": "R1",
            "value": value,
            "footprint": "R_0603",
            "lcsc": lcsc,
            "exclude_from_bom": 0,
            "exclude_from_pos": 0,
        }
    )


def _part_state(store):
    part = store.get_part("R1")
    return (
        part["lcsc"],
        part["value"],
        part["assembly_process"],
        part["component_product_type"],
    )


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


def test_assembly_metadata_follows_lcsc_lifecycle(tmp_path):
    """Metadata survives refreshes, rejects stale results, and clears on reassignment."""
    store = _store_with_enriched_part(tmp_path)

    _update_part(store, "C1", value="12k")
    assert _part_state(store) == ("C1", "12k", "SMT", 2)

    assert store.set_assembly_metadata("R1", "SMT updated", None, expected_lcsc="C1")
    assert _part_state(store) == ("C1", "12k", "SMT updated", 2)

    assert not store.set_assembly_metadata(
        "R1", "Wave soldering", 0, expected_lcsc="COLD"
    )
    assert _part_state(store) == ("C1", "12k", "SMT updated", 2)

    _update_part(store, "C2")
    assert _part_state(store) == ("C2", "10k", "", None)

    assert store.set_assembly_metadata("R1", "SMT", 0, expected_lcsc="C2")
    store.set_lcsc("R1", "CNEW")
    assert _part_state(store) == ("CNEW", "10k", "", None)


def test_get_assembly_enrichment_targets_uses_or_logic(tmp_path):
    """Rows missing any required enrichment field should be selected."""
    s = _store_obj()
    s.logger = logging.getLogger(__name__)
    s.dbfile = str(tmp_path / "project.db")

    s.create_db()

    with contextlib.closing(sqlite3.connect(s.dbfile)) as con, con as cur:
        cur.executemany(
            "INSERT INTO part_info (reference, value, footprint, lcsc, stock, exclude_from_bom, exclude_from_pos, assembly_process, component_product_type) "
            "VALUES (?, ?, 'R_0603', ?, 1, 0, 0, ?, ?)",
            [
                ("R1", "10k", "C1", "", None),
                ("R2", "1u", "C2", "SMT", None),
                ("R3", "100n", "C3", "", 0),
                ("R4", "47k", "C4", "SMT", 0),
                ("R5", "22k", "C5", "SMT", 3),
                ("R6", "4k7", "C6", "SMT", "bad"),
                ("R7", "1k", "C7", "SMT", 2),
            ],
        )
        cur.commit()

    targets = s.get_assembly_enrichment_targets()

    assert targets == {
        "C1": ["R1"],
        "C2": ["R2"],
        "C3": ["R3"],
        "C5": ["R5"],
        "C6": ["R6"],
    }


# ---------------------------------------------------------------------------
# backfill_estimator_metadata tests (B5.5)
# ---------------------------------------------------------------------------


class _BackfillFootprint:
    """Minimal footprint stub for backfill_estimator_metadata tests."""

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


def _backfill_store():
    """Build a Store with a recording stub for set_estimator_metadata."""
    s = _store_obj()
    s.logger = logging.getLogger(__name__)
    s.update_calls = []

    def _record(ref, pad_count, has_tht, assembly_flags):
        s.update_calls.append((ref, pad_count, has_tht, assembly_flags))

    s.set_estimator_metadata = _record
    return s


def test_backfill_skips_update_when_all_metadata_matches():
    """Backfill must not call set_estimator_metadata when nothing changed."""
    s = _backfill_store()
    fp = _BackfillFootprint(reference="R1", pads=[_Pad(), _Pad()])
    expected_flags = footprint_metadata_module.get_assembly_flags(fp)

    db_part = {
        "pad_count": 2,
        "has_tht": 0,
        "assembly_flags": expected_flags,
    }

    s.backfill_estimator_metadata(fp, db_part)

    assert s.update_calls == []


def test_backfill_writes_when_pad_count_differs():
    """A stale pad_count triggers a single update."""
    s = _backfill_store()
    fp = _BackfillFootprint(reference="R1", pads=[_Pad(), _Pad(), _Pad()])
    expected_flags = footprint_metadata_module.get_assembly_flags(fp)

    db_part = {
        "pad_count": 1,  # outdated
        "has_tht": 0,
        "assembly_flags": expected_flags,
    }

    s.backfill_estimator_metadata(fp, db_part)

    assert len(s.update_calls) == 1
    ref, pad_count, _has_tht, _flags = s.update_calls[0]
    assert ref == "R1"
    assert pad_count == 3


def test_backfill_writes_when_has_tht_was_none():
    """Existing rows with NULL has_tht always backfill."""
    s = _backfill_store()
    fp = _BackfillFootprint(reference="R1", pads=[_Pad()])
    expected_flags = footprint_metadata_module.get_assembly_flags(fp)

    db_part = {
        "pad_count": 1,
        "has_tht": None,  # never set
        "assembly_flags": expected_flags,
    }

    s.backfill_estimator_metadata(fp, db_part)

    assert len(s.update_calls) == 1


def test_backfill_writes_when_assembly_flags_differ():
    """Stale assembly_flags JSON triggers an update."""
    s = _backfill_store()
    fp = _BackfillFootprint(reference="R1", pads=[_Pad()])

    db_part = {
        "pad_count": 1,
        "has_tht": 0,
        "assembly_flags": '{"is_dnp": true}',  # outdated content
    }

    s.backfill_estimator_metadata(fp, db_part)

    assert len(s.update_calls) == 1
    _ref, _pad_count, _has_tht, flags = s.update_calls[0]
    # Fresh flags should match what footprint_metadata builds for this fp.
    assert flags == footprint_metadata_module.get_assembly_flags(fp)


def test_backfill_returns_silently_when_db_part_empty():
    """Missing db_part with no fallback row available is a no-op."""
    s = _backfill_store()
    s.get_part = lambda _ref: None
    fp = _BackfillFootprint(reference="R-MISSING", pads=[_Pad()])

    s.backfill_estimator_metadata(fp, {})

    assert s.update_calls == []


# ---------------------------------------------------------------------------
# assembly_flags JSON round-trip (B5.7)
# ---------------------------------------------------------------------------


def test_assembly_flags_round_trip_writer_keys_match_reader_expectations():
    """Keys produced by footprint_metadata.get_assembly_flags must parse back via pricing.get_assembly_flags."""
    fp = _BackfillFootprint(reference="R1", pads=[_Pad()], is_dnp=True)
    flags_json = footprint_metadata_module.get_assembly_flags(fp)

    parsed = parse_assembly_flags({"assembly_flags": flags_json})

    # Writer should emit all three keys, and reader should parse them back.
    assert set(parsed) == {"exclude_from_bom", "exclude_from_pos", "is_dnp"}
    # is_dnp came from the footprint stub.
    assert parsed["is_dnp"] is True
