"""Tests for store pad filtering helpers used by BOM estimator metadata."""

from pathlib import Path
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
    def __init__(self, pads: list[_Pad]) -> None:
        self._pads = pads

    def Pads(self) -> list[_Pad]:
        return self._pads


class _FootprintGetPads:
    def __init__(self, pads: list[_Pad]) -> None:
        self._pads = pads

    def GetPads(self) -> list[_Pad]:
        return self._pads


class _BoardFootprint(_FootprintPads):
    """Stateful native footprint surface for ordinary Store reads."""

    def __init__(
        self,
        *,
        reference: str = "R1",
        lcsc: str = "C1",
        value: str = "10k",
        pads: Optional[list[_Pad]] = None,
        attributes: int = 0,
        is_dnp: bool = False,
    ) -> None:
        super().__init__(pads if pads is not None else [])
        self._reference = reference
        self._lcsc = lcsc
        self._value = value
        self._attributes = attributes
        self._is_dnp = is_dnp

    def GetReference(self) -> str:
        return self._reference

    def GetValue(self) -> str:
        return self._value

    def SetValue(self, value: str) -> None:
        self._value = value

    def GetFPID(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(GetLibItemName=lambda: "R_0603")

    def GetProperties(self) -> dict[str, str]:
        return {"LCSC": self._lcsc}

    def SetProperty(self, name: str, value: str) -> None:
        assert name == "LCSC"
        self._lcsc = value

    def GetAttributes(self) -> int:
        return self._attributes

    def SetAttributes(self, attributes: int) -> None:
        self._attributes = attributes

    def IsDNP(self) -> bool:
        return self._is_dnp

    def SetDNP(self, is_dnp: bool) -> None:
        self._is_dnp = is_dnp


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
) -> tuple[Any, _BoardFootprint]:
    """Create a test store containing one enriched LCSC assignment."""
    footprint = _BoardFootprint(lcsc=lcsc)
    store = _saved_store(tmp_path, (footprint,))
    assert store.set_assembly_metadata(
        "R1", assembly_process, product_type, expected_lcsc=lcsc
    )
    return store, footprint


def _part_state(store: Any) -> tuple[Any, ...]:
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


def test_live_pad_metadata_reflects_board_edits_and_reopening(
    tmp_path: Path,
) -> None:
    """Each read derives electrical metadata from the current native geometry."""
    footprint = _BoardFootprint(pads=[_Pad(0, True), _Pad(0, True)])
    store = _saved_store(tmp_path, (footprint,))
    initial = store.get_part("R1")
    assert (initial["pad_count"], initial["has_tht"]) == (2, 1)

    footprint.Pads()[:] = [_Pad(3, True), _Pad(3, True)]
    updated = store.get_part("R1")
    assert (updated["pad_count"], updated["has_tht"]) == (0, 0)
    assert store.read_all() == [updated]

    reopened = Store(store.parent, str(tmp_path), store.board).get_part("R1")
    assert (reopened["pad_count"], reopened["has_tht"]) == (0, 0)
    assert reopened["lcsc"] == "C1"


def test_assembly_metadata_follows_lcsc_lifecycle(tmp_path: Path) -> None:
    """Session facts follow the current LCSC and reject mismatched responses."""
    store, footprint = _store_with_enriched_part(tmp_path)

    footprint.SetValue("12k")
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

    footprint.SetProperty("LCSC", "C2")
    footprint.SetValue("10k")
    assert _part_state(store) == ("C2", "10k", "", None)

    assert store.set_assembly_metadata(
        "R1", "SMT", ComponentProductType.ECONOMIC_AND_STANDARD, expected_lcsc="C2"
    )
    product_type = store.get_part("R1")["component_product_type"]
    assert product_type == 0 and type(product_type) is int
    footprint.SetProperty("LCSC", "C3")
    assert _part_state(store) == ("C3", "10k", "", None)

    footprint.SetProperty("LCSC", "C1")
    assert _part_state(store) == ("C1", "10k", "SMT updated", 2)

    footprint.SetProperty("LCSC", "")
    assert _part_state(store) == ("", "10k", "", None)
    assert not store.set_assembly_metadata("R1", "SMT", 0, expected_lcsc="C1")

    footprint.SetProperty("LCSC", "C2")
    assert _part_state(store) == ("C2", "10k", "SMT", 0)

    reopened = Store(store.parent, str(tmp_path), store.board)
    assert _part_state(reopened) == ("C2", "10k", "", None)
    assert reopened.get_assembly_enrichment_targets() == {"C2": ["R1"]}


def test_get_assembly_enrichment_targets_uses_or_logic(tmp_path: Path) -> None:
    """Rows missing any required enrichment field should be selected."""
    metadata = [
        ("", None),
        ("SMT", None),
        ("", 0),
        ("SMT", 0),
        ("SMT", 3),
        ("SMT", "bad"),
        ("SMT", 2),
        ("SMT", 1),
    ]
    footprints = tuple(
        _BoardFootprint(reference=f"R{index}", lcsc=f"C{index}")
        for index in range(1, len(metadata) + 1)
    )
    s = _saved_store(tmp_path, footprints)
    for footprint, (process, product_type) in zip(footprints, metadata):
        assert s.set_assembly_metadata(footprint.GetReference(), process, product_type)

    targets = s.get_assembly_enrichment_targets()

    assert targets == {
        "C1": ["R1"],
        "C2": ["R2"],
        "C3": ["R3"],
        "C5": ["R5"],
        "C6": ["R6"],
    }


@pytest.mark.parametrize(
    "attributes,is_dnp,expected_bom,expected_pos",
    [
        (0, False, False, False),
        (1 << 3, False, True, False),
        (1 << 2, False, False, True),
        (0, True, False, False),
        ((1 << 3) | (1 << 2), True, True, True),
    ],
    ids=["included", "no-bom", "no-pos", "dnp", "all-flags"],
)
def test_assembly_flags_follow_native_board_changes(
    tmp_path: Path,
    attributes: int,
    is_dnp: bool,
    expected_bom: bool,
    expected_pos: bool,
) -> None:
    """BOM/POS/DNP flags come from current native state, including reversal."""
    footprint = _BoardFootprint(pads=[_Pad(), _Pad()])
    store = _saved_store(tmp_path, (footprint,))
    baseline = store.get_part("R1")

    footprint.SetAttributes(attributes)
    footprint.SetDNP(is_dnp)
    changed = store.get_part("R1")
    expected_flags = {
        "exclude_from_bom": expected_bom,
        "exclude_from_pos": expected_pos,
        "is_dnp": is_dnp,
    }
    assert parse_assembly_flags(changed) == expected_flags
    assert bool(changed["exclude_from_bom"]) is expected_bom
    assert bool(changed["exclude_from_pos"]) is expected_pos
    assert store.read_all() == [changed]
    reopened = Store(store.parent, str(tmp_path), store.board)
    assert parse_assembly_flags(reopened.get_part("R1")) == expected_flags

    footprint.SetAttributes(0)
    footprint.SetDNP(False)
    assert store.get_part("R1") == baseline


# ---------------------------------------------------------------------------
# assembly_flags JSON round-trip (B5.7)
# ---------------------------------------------------------------------------


def test_assembly_flags_round_trip_writer_keys_match_reader_expectations() -> None:
    """Keys produced by footprint_metadata.get_assembly_flags must parse back via pricing.get_assembly_flags."""
    fp = _BoardFootprint(reference="R1", pads=[_Pad()], is_dnp=True)
    flags_json = footprint_metadata_module.get_assembly_flags(fp)

    parsed = parse_assembly_flags({"assembly_flags": flags_json})

    # Writer should emit all three keys, and reader should parse them back.
    assert set(parsed) == {"exclude_from_bom", "exclude_from_pos", "is_dnp"}
    # is_dnp came from the footprint stub.
    assert parsed["is_dnp"] is True
