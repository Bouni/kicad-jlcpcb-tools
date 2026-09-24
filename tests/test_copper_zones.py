"""Locate empty pours using copied intended zone boundaries, never filled copper."""

from dataclasses import FrozenInstanceError
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def copper_zones(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the geometry collector without importing the KiCad plugin package."""
    name = "_empty_copper_zone_tests"
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).parent.parent / "copper_zones.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _chain(points: list[tuple[int, int]]) -> SimpleNamespace:
    """Model native line-chain reads with mutable point storage."""
    native_points = [SimpleNamespace(x=x, y=y) for x, y in points]
    return SimpleNamespace(
        points=native_points,
        PointCount=lambda: len(native_points),
        CPoint=native_points.__getitem__,
    )


def _shape(
    outlines: list[list[tuple[int, int]]],
    holes: dict[int, list[list[tuple[int, int]]]],
) -> SimpleNamespace:
    """Model every outer contour and its indexed native holes."""
    chains = [_chain(points) for points in outlines]
    hole_chains = {
        index: [_chain(points) for points in contours]
        for index, contours in holes.items()
    }
    return SimpleNamespace(
        chains=chains,
        OutlineCount=lambda: len(chains),
        TotalVertices=lambda: sum(len(chain.points) for chain in chains),
        COutline=chains.__getitem__,
        HoleCount=lambda index: len(hole_chains.get(index, [])),
        CHole=lambda index, hole: hole_chains[index][hole],
    )


def _zone(
    identity: str,
    shape: Any,
    *,
    areas: dict[int, int],
    net: str = "VIN",
    position: Any = None,
    rule_area: bool = False,
) -> SimpleNamespace:
    """Represent native zone metadata independently of its fill results."""

    def get_position() -> SimpleNamespace:
        """Native zones use their first corner, or synthetic zero for empty outlines."""
        if position is not None:
            return SimpleNamespace(x=position[0], y=position[1])
        if shape.chains and shape.chains[0].points:
            return shape.chains[0].points[0]
        return SimpleNamespace(x=0, y=0)

    return SimpleNamespace(
        m_Uuid=SimpleNamespace(AsString=lambda: identity),
        GetNetname=lambda: net,
        GetIsRuleArea=lambda: rule_area,
        GetLayerSet=lambda: SimpleNamespace(Seq=lambda: list(areas)),
        GetFilledPolysList=lambda layer: SimpleNamespace(Area=lambda: areas[layer]),
        Outline=lambda: shape,
        GetPosition=get_position,
    )


def _board(zones: list[Any]) -> SimpleNamespace:
    """Retain source order and custom display layer names."""
    return SimpleNamespace(
        Zones=lambda: zones,
        GetLayerName=lambda layer: {0: "F.Cu", 2: "B.Cu", 4: "Power plane"}[layer],
    )


def test_regions_keep_duplicate_nets_and_only_empty_copper_layers(
    copper_zones: ModuleType,
) -> None:
    """Two VIN pours remain individually locatable, including a multilayer zone."""
    shape = _shape([[(1, 2), (3, 2), (3, 4), (1, 4)]], {})
    zones = [
        _zone("vin-a", shape, areas={2: 0, 4: 23, 0: 0}),
        _zone("vin-b", shape, areas={0: 0}),
        _zone("filled", shape, areas={0: 20}),
        _zone("keepout", shape, areas={0: 0}, rule_area=True),
        _zone("graphic", shape, areas={5: 0}),
    ]

    regions = copper_zones.collect_empty_copper_zones(
        _board(zones), lambda layer: layer in {0, 2, 4}
    )

    assert [(region.zone_id, region.layer_id) for region in regions] == [
        ("vin-a", 2),
        ("vin-a", 0),
        ("vin-b", 0),
    ]
    assert [region.label for region in regions] == [
        "VIN on B.Cu",
        "VIN on F.Cu",
        "VIN on F.Cu",
    ]


def test_regions_copy_all_outer_and_hole_boundaries(copper_zones: ModuleType) -> None:
    """Empty fill polygons cannot locate the absent copper; intended outlines can."""
    outer = [
        (10_000_000, 20_000_000),
        (30_000_000, 20_000_000),
        (30_000_000, 40_000_000),
        (10_000_000, 40_000_000),
    ]
    hole = [
        (15_000_000, 25_000_000),
        (20_000_000, 25_000_000),
        (20_000_000, 30_000_000),
        (15_000_000, 30_000_000),
    ]
    island = [
        (50_000_000, 20_000_000),
        (60_000_000, 20_000_000),
        (60_000_000, 30_000_000),
        (50_000_000, 30_000_000),
    ]
    shape = _shape([outer, island], {0: [hole]})
    (region,) = copper_zones.collect_empty_copper_zones(
        _board([_zone("vin", shape, areas={0: 0})]), lambda _layer: True
    )

    assert region.contours == (tuple(outer), tuple(hole), tuple(island))
    assert region.bounds == (10_000_000, 20_000_000, 60_000_000, 40_000_000)
    assert region.location_text == (
        "Board X 10.000 to 60.000 mm, Y 20.000 to 40.000 mm"
    )
    assert region.geometry_error == ""
    shape.chains[0].points[0].x = 999
    assert region.contours[0][0] == (10_000_000, 20_000_000)
    with pytest.raises(FrozenInstanceError):
        region.net_name = "changed"


@pytest.mark.parametrize("failure", ["empty", "short", "throws"])
def test_geometry_failures_preserve_warning_and_real_position(
    copper_zones: ModuleType, failure: str
) -> None:
    """A broken outline still names missing copper and its accurate fallback XY."""
    shape = _shape(
        []
        if failure == "empty"
        else [[(12_345_000, -6_789_000), (13_000_000, -6_000_000)]],
        {},
    )
    zone = _zone("broken", shape, areas={0: 0}, net="")
    if failure == "throws":

        def fail_outline() -> Any:
            raise RuntimeError("outline unavailable")

        zone.Outline = fail_outline

    (region,) = copper_zones.collect_empty_copper_zones(
        _board([zone]), lambda _layer: True
    )

    assert region.label == "no net on F.Cu"
    assert region.contours == ()
    assert region.bounds is None
    assert "outline" in region.geometry_error.lower()
    assert region.location_text == (
        "Board location unavailable"
        if failure == "empty"
        else "Board X 12.345 mm, Y -6.789 mm"
    )


def test_empty_native_outline_never_labels_synthetic_origin(
    copper_zones: ModuleType,
) -> None:
    """GetPosition's native (0,0) for zero vertices is not a real board location."""
    zone = _zone("empty", _shape([[]], {}), areas={0: 0})
    (region,) = copper_zones.collect_empty_copper_zones(
        _board([zone]), lambda _layer: True
    )

    assert region.position is None
    assert region.location_text == "Board location unavailable"


def test_unknown_geometry_never_invents_zero_position(copper_zones: ModuleType) -> None:
    """Missing native location data is explicit instead of pointing at the origin."""
    zone = _zone("unknown", _shape([], {}), areas={0: 0})
    del zone.GetPosition
    (region,) = copper_zones.collect_empty_copper_zones(
        _board([zone]), lambda _layer: True
    )

    assert region.location_text == "Board location unavailable"
    assert region.geometry_error


def test_missing_uuid_preserves_distinct_pours(copper_zones: ModuleType) -> None:
    """Compatibility fallback identities distinguish same-net, same-layer pours."""
    shape = _shape([[(1, 2), (3, 2), (3, 4)]], {})
    zones = [_zone("", shape, areas={0: 0}), _zone("", shape, areas={0: 0})]
    for zone in zones:
        del zone.m_Uuid
    regions = copper_zones.collect_empty_copper_zones(
        _board(zones), lambda _layer: True
    )

    assert len({region.zone_id for region in regions}) == 2
    assert all(region.zone_id for region in regions)


@pytest.mark.native_kicad
def test_native_outline_and_hole_are_copied(copper_zones: ModuleType) -> None:
    """Exercise the documented SWIG polygon getters when bindings are available."""
    pcbnew = pytest.importorskip("pcbnew", reason="native KiCad bindings unavailable")
    board = pcbnew.BOARD()
    zone = pcbnew.ZONE(board)
    zone.SetLayer(pcbnew.F_Cu)
    outline = zone.Outline()
    outline.NewOutline()
    outer = [
        (1_000_000, 2_000_000),
        (3_000_000, 2_000_000),
        (3_000_000, 4_000_000),
        (1_000_000, 4_000_000),
    ]
    for x, y in outer:
        outline.Append(x, y)
    outline.NewHole(0)
    hole = [
        (1_500_000, 2_500_000),
        (2_000_000, 2_500_000),
        (2_000_000, 3_000_000),
        (1_500_000, 3_000_000),
    ]
    for x, y in hole:
        outline.Append(x, y, 0, 0)
    board.Add(zone)

    (region,) = copper_zones.collect_empty_copper_zones(board, pcbnew.IsCopperLayer)

    assert region.contours == (tuple(outer), tuple(hole))
    assert region.zone_id == zone.m_Uuid.AsString()
