"""Tests for store pad filtering helpers used by BOM estimator metadata."""

import contextlib
import logging
from pathlib import Path
import sqlite3
import types
from typing import Any, Optional

import pytest

from bom_estimation.assembly_mode import ComponentProductType
from bom_estimation.pricing import get_assembly_flags as parse_assembly_flags
from tests.wx_harness import load_siblings, wx_stubs

# Keep the real Store and metadata helpers without leaving fake wx installed
# for unrelated tests that need native grid geometry.
with load_siblings(
    "store_pad_tests", ("store", "footprint_metadata"), wx_stubs()
) as _modules:
    store_module = _modules["store"]
    footprint_metadata_module = _modules["footprint_metadata"]

Store = store_module.Store
count_pad = footprint_metadata_module.count_pad
get_footprint_pad_count = footprint_metadata_module.get_footprint_pad_count
get_footprint_pads = footprint_metadata_module.get_footprint_pads
footprint_has_tht = footprint_metadata_module.footprint_has_tht


class _Pad:
    """The numeric attribute/HasHole surface exposed by KiCad's PAD wrapper."""

    def __init__(
        self,
        attribute: int = 1,
        has_hole: bool = False,
    ) -> None:
        self._attribute = attribute
        self._has_hole = has_hole

    def GetAttribute(self) -> int:
        return self._attribute

    def HasHole(self) -> bool:
        return self._has_hole


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


def _saved_store(tmp_path: Path, footprints: tuple[Any, ...] = ()) -> Any:
    """Construct a real ordinary Store using a saved-board fixture."""
    filename = tmp_path / "board.kicad_pcb"
    filename.write_text("(kicad_pcb)\n", encoding="utf-8")
    board = types.SimpleNamespace(
        GetFileName=lambda: str(filename), GetFootprints=lambda: footprints
    )
    return Store(types.SimpleNamespace(settings={}), str(tmp_path), board)


def _store_with_enriched_part(
    tmp_path: Path,
    *,
    lcsc: str = "C1",
    assembly_process: str = "SMT",
    product_type: int = 2,
) -> Any:
    """Create a test store containing one enriched LCSC assignment."""
    store = _saved_store(tmp_path)
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


@pytest.mark.parametrize(
    "attribute,expected", [(0, True), (1, True), (2, True), (3, False)]
)
def test_count_pad_uses_native_numeric_attributes(
    attribute: int, expected: bool
) -> None:
    """PAD_ATTRIB is PTH=0, SMD=1, CONN=2, NPTH=3, not an enum name string."""
    assert count_pad(_Pad(attribute)) is expected


@pytest.mark.parametrize(
    "pads,expected",
    [
        ([], (0, False)),
        ([_Pad(3, True), _Pad(3, True)], (0, False)),
        ([_Pad(1), _Pad(1), _Pad(3, True)], (2, False)),
        ([_Pad(0, True), _Pad(0, True)], (2, True)),
        ([_Pad(0, True), _Pad(1), _Pad(2), _Pad(3, True)], (3, True)),
    ],
    ids=["empty", "npth-only", "smd-with-mounting-hole", "pth", "mixed"],
)
def test_pad_metadata_excludes_mechanical_holes(
    pads: list[_Pad], expected: tuple[int, bool]
) -> None:
    """Physical mounting holes contribute neither solder joints nor THT status."""
    footprint = _FootprintPads(pads)
    assert (
        get_footprint_pad_count(footprint),
        footprint_has_tht(footprint),
    ) == expected
    assert footprint_metadata_module.get_footprint_pad_metadata(footprint) == expected


def test_get_footprint_pads_supports_getpads_fallback() -> None:
    """Footprint pad collection supports the GetPads API variant."""
    pads = [_Pad(), _Pad()]
    fp = _FootprintGetPads(pads)

    assert list(get_footprint_pads(fp)) == pads


def test_refresh_replaces_stale_npth_metadata_and_preserves_it_on_reopen(
    tmp_path: Path,
) -> None:
    """Existing projects shed false joint/THT metadata on the next board sync."""
    footprint = _BackfillFootprint(pads=[_Pad(3, True), _Pad(3, True)])
    store = _saved_store(tmp_path, (footprint,))
    store.set_estimator_metadata(
        "R1", 2, True, footprint_metadata_module.get_assembly_flags(footprint)
    )

    store.update_from_board()
    updated = store.get_part("R1")
    assert (updated["pad_count"], updated["has_tht"]) == (0, 0)

    reopened = Store(store.parent, str(tmp_path), store.board).get_part("R1")
    assert (reopened["pad_count"], reopened["has_tht"]) == (0, 0)
    assert reopened["lcsc"] == "C1"


def test_assembly_metadata_follows_lcsc_lifecycle(tmp_path):
    """Metadata survives refreshes, rejects stale results, and clears on reassignment."""
    store = _store_with_enriched_part(tmp_path)

    _update_part(store, "C1", value="12k")
    assert _part_state(store) == ("C1", "12k", "SMT", 2)

    assert store.set_assembly_metadata("R1", "SMT updated", None, expected_lcsc="C1")
    assert _part_state(store) == ("C1", "12k", "SMT updated", 2)

    for missing_process in ("", None):
        assert store.set_assembly_metadata(
            "R1", missing_process, None, expected_lcsc="C1"
        )
        assert _part_state(store) == ("C1", "12k", "SMT updated", 2)

    assert not store.set_assembly_metadata(
        "R1", "Wave soldering", 0, expected_lcsc="COLD"
    )
    assert _part_state(store) == ("C1", "12k", "SMT updated", 2)

    _update_part(store, "C2")
    assert _part_state(store) == ("C2", "10k", "", None)

    assert store.set_assembly_metadata(
        "R1", "SMT", ComponentProductType.ECONOMIC_AND_STANDARD, expected_lcsc="C2"
    )
    product_type = store.get_part("R1")["component_product_type"]
    assert product_type == 0 and type(product_type) is int
    store.set_lcsc("R1", "CNEW")
    assert _part_state(store) == ("CNEW", "10k", "", None)


def test_get_assembly_enrichment_targets_uses_or_logic(tmp_path: Path) -> None:
    """Rows missing any required enrichment field should be selected."""
    s = _saved_store(tmp_path)

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
                ("R8", "2k2", "C8", "SMT", 1),
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

    def GetValue(self) -> str:
        return "10k"

    def GetFPID(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(GetLibItemName=lambda: "R_0603")

    def GetProperties(self) -> dict[str, str]:
        return {"LCSC": "C1"}

    def Pads(self):
        return self._pads

    def GetAttributes(self):
        return self._attributes

    def IsDNP(self):
        return self._is_dnp


@pytest.mark.parametrize(
    "previous,changed",
    [
        ({}, False),
        ({"pad_count": 1}, True),
        ({"has_tht": None}, True),
        ({"assembly_flags": '{"is_dnp": true}'}, True),
        (None, False),
    ],
    ids=["matching", "stale-pads", "unset-tht", "stale-flags", "missing-row"],
)
def test_backfill_updates_only_changed_existing_metadata(
    previous: Optional[dict[str, Any]], changed: bool
) -> None:
    """A present stale row receives one complete update; other rows remain untouched."""
    store = Store.__new__(Store)
    store.logger = logging.getLogger(__name__)
    store.get_part = lambda _ref: None
    updates: list[tuple[Any, ...]] = []
    store.set_estimator_metadata = lambda *values: updates.append(values)
    footprint = _BackfillFootprint(pads=[_Pad(), _Pad()])
    flags = footprint_metadata_module.get_assembly_flags(footprint)
    current = {"pad_count": 2, "has_tht": 0, "assembly_flags": flags}

    store.backfill_estimator_metadata(
        footprint, {} if previous is None else {**current, **previous}
    )

    assert updates == ([("R1", 2, False, flags)] if changed else [])


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
