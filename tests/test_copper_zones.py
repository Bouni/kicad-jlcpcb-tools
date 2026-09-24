"""Locate empty pours using copied intended zone boundaries, never filled copper."""

from dataclasses import FrozenInstanceError
import importlib.util
import logging
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from .native_kicad_support import (
    native_bindings,  # noqa: F401 -- pytest discovers this imported fixture.
)
from .wx_harness import load_siblings


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


@pytest.mark.parametrize("teardrop", [True, False, 1, 0])
def test_diagnostics_identify_native_zone_type_and_copy_optional_metadata(
    copper_zones: ModuleType, teardrop: Any
) -> None:
    """Generated teardrops remain findings and have distinct, detached details."""
    shape = _shape(
        [
            [
                (10_000_000, 20_000_000),
                (10_995_000, 20_000_000),
                (10_995_000, 20_300_000),
            ]
        ],
        {},
    )
    zone = _zone("vext-full-native-id", shape, areas={0: 0}, net="VEXT")
    zone.IsTeardropArea = lambda: teardrop
    zone.GetZoneName = lambda: "U31 pad 1"
    zone.IsFilled = lambda: 1
    zone.NeedRefill = lambda: 0

    (region,) = copper_zones.collect_empty_copper_zones(
        _board([zone]), lambda _layer: True, refill_performed=True
    )

    assert region.is_teardrop is bool(teardrop)
    assert region.is_filled is True
    assert region.needs_refill is False
    assert region.refill_performed is True
    assert region.label == "VEXT on F.Cu"
    assert region.diagnostic_label == (
        "VEXT on F.Cu — generated teardrop"
        if teardrop
        else "VEXT on F.Cu — copper zone"
    )
    assert region.identity_text == "Zone name: U31 pad 1; Zone ID: vext-full-native-id"
    assert region.dimensions_text == "Outline dimensions: 0.995 × 0.300 mm"
    assert region.fill_status_text == (
        "No filled copper remains on F.Cu after this operation's successful refill. "
        "Native zone state: filled; refill not required (zone-level flags)."
    )
    zone.GetZoneName = lambda: "changed name"
    zone.IsTeardropArea = lambda: not teardrop
    assert region.zone_name == "U31 pad 1"
    assert region.is_teardrop is bool(teardrop)


@pytest.mark.parametrize("metadata", ["missing", "throws", "unusable"])
def test_unavailable_metadata_never_guesses_type_or_hides_empty_copper(
    copper_zones: ModuleType, metadata: str
) -> None:
    """Older or faulty bindings retain the finding without inventing metadata."""
    zone = _zone("unknown", _shape([[(0, 0), (1, 0), (1, 1)]], {}), areas={0: 0})

    def fail_metadata() -> Any:
        """Represent an optional native getter failing on the current board."""
        raise RuntimeError("metadata unavailable")

    for getter in ("IsTeardropArea", "IsFilled", "NeedRefill", "GetZoneName"):
        if metadata != "missing":
            setattr(
                zone,
                getter,
                fail_metadata if metadata == "throws" else MagicMock(),
            )

    (region,) = copper_zones.collect_empty_copper_zones(
        _board([zone]), lambda _layer: True
    )

    assert region.is_teardrop is None
    assert region.is_filled is None
    assert region.needs_refill is None
    assert region.refill_performed is None
    assert region.zone_name == ""
    assert region.diagnostic_label == region.label
    assert region.identity_text == "Zone ID: unknown"
    assert region.fill_status_text == (
        "No filled copper is present on F.Cu in the inspected fill data. "
        "Refill status for this operation is unknown. "
        "Native zone state: fill status unknown; refill requirement unknown "
        "(zone-level flags)."
    )


@pytest.mark.parametrize("value", [2, -1, "true", "", [], object()])
def test_non_boolean_metadata_is_unknown(copper_zones: ModuleType, value: Any) -> None:
    """Only native booleans and integer zero/one are reliable flag values."""
    zone = _zone("unknown", _shape([], {}), areas={0: 0})
    zone.IsTeardropArea = lambda: value
    zone.IsFilled = lambda: value
    zone.NeedRefill = lambda: value

    (region,) = copper_zones.collect_empty_copper_zones(
        _board([zone]), lambda _layer: True
    )

    assert region.is_teardrop is None
    assert region.is_filled is None
    assert region.needs_refill is None


def test_no_refill_explains_existing_per_layer_fill_and_zone_level_flags(
    copper_zones: ModuleType,
) -> None:
    """A filled multilayer zone can still lack copper on the reported layer."""
    zone = _zone(
        "multilayer",
        _shape([[(0, 0), (1, 0), (1, 1)]], {}),
        areas={0: 0, 2: 100},
    )
    zone.IsFilled = lambda: True
    zone.NeedRefill = lambda: True

    (region,) = copper_zones.collect_empty_copper_zones(
        _board([zone]), lambda _layer: True, refill_performed=False
    )

    assert region.layer_name == "F.Cu"
    assert region.refill_performed is False
    assert region.fill_status_text == (
        "No filled copper is present on F.Cu in the inspected fill data. "
        "Refilling is disabled for this operation. "
        "Native zone state: filled; refill required (zone-level flags)."
    )


def test_geometry_failure_keeps_identity_and_explicit_unavailable_dimensions(
    copper_zones: ModuleType,
) -> None:
    """A metadata-rich item is still actionable when its outline cannot be read."""
    zone = _zone("", _shape([], {}), areas={0: 0}, position=(123, 456))
    zone.GetZoneName = lambda: "Failed outline"
    zone.IsFilled = lambda: False
    zone.NeedRefill = lambda: True

    (region,) = copper_zones.collect_empty_copper_zones(
        _board([zone]), lambda _layer: True, refill_performed=True
    )

    assert region.identity_text == "Zone name: Failed outline; Zone ID: zone-1"
    assert region.dimensions_text == "Outline dimensions: unavailable"
    assert region.geometry_error
    assert "refill required" in region.fill_status_text
    assert "not filled" in region.fill_status_text


def test_diagnostic_defaults_preserve_existing_record_construction(
    copper_zones: ModuleType,
) -> None:
    """Previously constructed detached records remain usable by callers."""
    region = copper_zones.EmptyCopperZone("legacy", "VIN", 0, "F.Cu", ())

    assert region.is_teardrop is None
    assert region.zone_name == ""
    assert region.refill_performed is None
    assert region.diagnostic_label == "VIN on F.Cu"
    assert region.identity_text == "Zone ID: legacy"
    assert region.dimensions_text == "Outline dimensions: unavailable"


def test_unusable_native_identity_uses_transient_snapshot_id(
    copper_zones: ModuleType,
) -> None:
    """Unbound UUID APIs cannot accidentally surface opaque object strings."""
    zone = _zone("unused", _shape([], {}), areas={0: 0})
    zone.m_Uuid.AsString = MagicMock()

    (region,) = copper_zones.collect_empty_copper_zones(
        _board([zone]), lambda _layer: True
    )

    assert region.identity_text == "Zone ID: zone-1"


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


@pytest.mark.native_kicad
@pytest.mark.kicad10_impedance
@pytest.mark.parametrize("teardrop", [False, True])
def test_native_metadata_survives_saved_board_reopening(
    copper_zones: ModuleType, tmp_path: Path, teardrop: bool
) -> None:
    """Read real native flags and serialized teardrop identity after reopening."""
    pcbnew = pytest.importorskip("pcbnew", reason="native KiCad bindings unavailable")
    board = pcbnew.BOARD()
    zone = pcbnew.ZONE(board)
    if not hasattr(zone, "IsTeardropArea"):
        pytest.skip("native KiCad does not expose generated-teardrop identification")
    zone.SetLayer(pcbnew.F_Cu)
    zone.SetZoneName("U31 pad 1")
    outline = zone.Outline()
    outline.NewOutline()
    for x, y in (
        (10_000_000, 20_000_000),
        (10_995_000, 20_000_000),
        (10_995_000, 20_300_000),
    ):
        outline.Append(x, y)
    board.Add(zone)
    zone.SetIsFilled(True)
    zone.SetNeedRefill(True)
    (before,) = copper_zones.collect_empty_copper_zones(board, pcbnew.IsCopperLayer)
    zone.SetIsFilled(False)
    zone.SetNeedRefill(False)
    (after,) = copper_zones.collect_empty_copper_zones(board, pcbnew.IsCopperLayer)
    assert before.is_filled is True and before.needs_refill is True
    assert after.is_filled is False and after.needs_refill is False

    path = tmp_path / "empty-copper.kicad_pcb"
    pcbnew.SaveBoard(str(path), board)
    if teardrop:
        # TEARDROP_TYPE is not exposed by all SWIG versions. Use KiCad's native
        # persisted attribute rather than assuming a Python enum or integer API.
        saved, replacements = re.subn(
            r"\(zone(?=\s)",
            "(zone (attr (teardrop (type padvia)))",
            path.read_text(),
            count=1,
        )
        assert replacements == 1
        path.write_text(saved)
    reopened = pcbnew.LoadBoard(str(path))
    native = next(iter(reopened.Zones()))

    (region,) = copper_zones.collect_empty_copper_zones(
        reopened, pcbnew.IsCopperLayer, refill_performed=False
    )

    assert native.IsTeardropArea() is teardrop
    assert region.is_teardrop is teardrop
    assert region.zone_name == "U31 pad 1"
    assert region.zone_id == zone.m_Uuid.AsString()
    assert region.is_filled == native.IsFilled()
    assert region.needs_refill == native.NeedRefill()
    assert region.dimensions_text == "Outline dimensions: 0.995 × 0.300 mm"
    assert region.refill_performed is False


def _native_empty_zones(pcbnew: ModuleType, directory: Path) -> Any:
    """Build synthetic clearance-clipped pours, persisting one native teardrop tag."""
    path = directory / "isolated-empty-zones.kicad_pcb"
    board = pcbnew.NewBoard(str(path))
    assert board is not None
    board.SetFileName(str(path))
    edge = pcbnew.PCB_SHAPE(board)
    edge.SetShape(pcbnew.SHAPE_T_RECT)
    edge.SetStart(pcbnew.VECTOR2I(0, 0))
    edge.SetEnd(pcbnew.VECTOR2I(50_000_000, 30_000_000))
    edge.SetWidth(100_000)
    edge.SetLayer(pcbnew.Edge_Cuts)
    board.Add(edge)
    for name, net_name, left, width, height in (
        ("Synthetic generated teardrop", "VEXT", 10_000_000, 995_000, 300_000),
        ("Synthetic ordinary pour", "GND", 20_000_000, 2_000_000, 2_000_000),
    ):
        net = pcbnew.NETINFO_ITEM(board, net_name)
        board.Add(net)
        zone = pcbnew.ZONE(board)
        zone.SetLayer(pcbnew.F_Cu)
        zone.SetZoneName(name)
        zone.SetNetCode(net.GetNetCode())
        zone.SetMinThickness(25_400)
        outline = zone.Outline()
        outline.NewOutline()
        for x, y in (
            (left, 10_000_000),
            (left + width, 10_000_000),
            (left + width, 10_000_000 + height),
            (left, 10_000_000 + height),
        ):
            outline.Append(x, y)
        board.Add(zone)
    # A foreign-net track covers both outlines, forcing native clearance removal
    # for ordinary and generated copper alike. This exercises reporting, not
    # KiCad's teardrop generation algorithm or electrical correctness.
    obstacle_net = pcbnew.NETINFO_ITEM(board, "OBSTACLE")
    board.Add(obstacle_net)
    obstacle = pcbnew.PCB_TRACK(board)
    obstacle.SetLayer(pcbnew.F_Cu)
    obstacle.SetNetCode(obstacle_net.GetNetCode())
    obstacle.SetStart(pcbnew.VECTOR2I(8_000_000, 11_000_000))
    obstacle.SetEnd(pcbnew.VECTOR2I(24_000_000, 11_000_000))
    obstacle.SetWidth(6_000_000)
    board.Add(obstacle)
    assert pcbnew.SaveBoard(str(path), board)
    saved, replacements = re.subn(
        re.escape('(name "Synthetic generated teardrop")'),
        '(attr (teardrop (type padvia))) (name "Synthetic generated teardrop")',
        path.read_text(encoding="utf-8"),
        count=1,
    )
    assert replacements == 1
    path.write_text(saved, encoding="utf-8")
    reopened = pcbnew.LoadBoard(str(path))
    assert reopened is not None
    zones = {str(zone.GetZoneName()): zone for zone in reopened.Zones()}
    assert zones["Synthetic generated teardrop"].IsTeardropArea() is True
    assert zones["Synthetic ordinary pour"].IsTeardropArea() is False
    return reopened


def _native_zone_reporting_state(board: Any) -> tuple[Any, ...]:
    """Read actual native geometry and flags independently of diagnostic records."""
    states = []
    for zone in board.Zones():
        outline = zone.Outline()
        contours = []
        for index in range(outline.OutlineCount()):
            chains = [outline.COutline(index)] + [
                outline.CHole(index, hole) for hole in range(outline.HoleCount(index))
            ]
            for chain in chains:
                contours.append(
                    tuple(
                        (int(chain.CPoint(point).x), int(chain.CPoint(point).y))
                        for point in range(chain.PointCount())
                    )
                )
        states.append(
            (
                str(zone.m_Uuid.AsString()),
                tuple(contours),
                bool(zone.IsFilled()),
                bool(zone.NeedRefill()),
                tuple(
                    (
                        int(layer),
                        zone.GetFilledPolysList(layer).Area(),
                        zone.GetFilledPolysList(layer).FullPointCount(),
                    )
                    for layer in zone.GetLayerSet().Seq()
                ),
            )
        )
    return tuple(states)


@pytest.mark.native_kicad
@pytest.mark.kicad10_impedance
def test_native_completed_empty_teardrop_is_logged_after_real_fabrication_fill(
    native_bindings: SimpleNamespace,  # noqa: F811 -- imported pytest fixture.
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Synthetic native clearance removal yields info for generated copper only."""
    pcbnew = native_bindings.pcbnew
    board = _native_empty_zones(pcbnew, tmp_path)
    with load_siblings(
        "_empty_zone_fabrication_native", ("fabrication", "copper_zones"), {}
    ) as modules:
        parent = SimpleNamespace(settings={"gerber": {"fill_zones": True}})
        fabrication = modules["fabrication"].Fabrication(parent, board)
        caplog.set_level(logging.INFO, logger=fabrication.logger.name)

        actionable = fabrication.fill_zone_regions()

        for zone in board.Zones():
            assert zone.GetFilledPolysList(pcbnew.F_Cu).Area() == 0
            assert zone.IsFilled() is True
            assert zone.NeedRefill() is False
        assert [region.zone_name for region in actionable] == [
            "Synthetic ordinary pour"
        ]
        before = _native_zone_reporting_state(board)
        raw = modules["copper_zones"].collect_empty_copper_zones(
            board, pcbnew.IsCopperLayer, refill_performed=True
        )
        assert len(raw) == 2
        generated = next(region for region in raw if region.is_teardrop is True)
        ordinary = next(region for region in raw if region.is_teardrop is False)
        assert generated.is_informational_teardrop is True
        assert ordinary.is_informational_teardrop is False
        assert _native_zone_reporting_state(board) == before
        messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == fabrication.logger.name and record.levelno == logging.INFO
        ]
        assert len(messages) == 1
        assert "Empty generated teardrop" in messages[0]
        for detail in (
            generated.label,
            generated.zone_id,
            generated.location_text,
            generated.dimensions_text,
            generated.fill_status_text,
        ):
            assert detail in messages[0]
        # A read-only inspection of the completed native result stays info-only.
        parent.settings["gerber"]["fill_zones"] = False
        assert fabrication.fill_zones() == ["GND on F.Cu"]
        assert _native_zone_reporting_state(board) == before


@pytest.mark.native_kicad
@pytest.mark.kicad10_impedance
def test_native_empty_teardrop_reporting_reads_changed_flags_without_refilling(
    native_bindings: SimpleNamespace,  # noqa: F811 -- imported pytest fixture.
    tmp_path: Path,
) -> None:
    """Fresh native stale/unfilled states remain actionable and are not changed by reporting."""
    pcbnew = native_bindings.pcbnew
    board = _native_empty_zones(pcbnew, tmp_path)
    with load_siblings(
        "_empty_zone_state_native", ("fabrication", "copper_zones"), {}
    ) as modules:
        parent = SimpleNamespace(settings={"gerber": {"fill_zones": True}})
        fabrication = modules["fabrication"].Fabrication(parent, board)
        fabrication.fill_zone_regions()
        generated = next(zone for zone in board.Zones() if zone.IsTeardropArea())
        assert generated.GetFilledPolysList(pcbnew.F_Cu).Area() == 0
        assert generated.IsFilled() is True and generated.NeedRefill() is False
        expected_net = str(generated.GetNetname())
        outline = generated.Outline().COutline(0)
        expected_boundary = tuple(
            (int(outline.CPoint(index).x), int(outline.CPoint(index).y))
            for index in range(outline.PointCount())
        )
        completed_path = tmp_path / "completed-empty-zones.kicad_pcb"
        assert pcbnew.SaveBoard(str(completed_path), board)
        reopened = pcbnew.LoadBoard(str(completed_path))
        assert reopened is not None
        reopened_generated = next(
            zone for zone in reopened.Zones() if zone.IsTeardropArea()
        )
        assert str(reopened_generated.GetNetname()) == expected_net == "VEXT"
        reopened_outline = reopened_generated.Outline()
        assert reopened_outline.OutlineCount() == 1
        assert reopened_outline.HoleCount(0) == 0
        reopened_boundary = reopened_outline.COutline(0)
        assert (
            tuple(
                (
                    int(reopened_boundary.CPoint(index).x),
                    int(reopened_boundary.CPoint(index).y),
                )
                for index in range(reopened_boundary.PointCount())
            )
            == expected_boundary
        )
        reopened_fabrication = modules["fabrication"].Fabrication(parent, reopened)
        # Loading may reset native readiness flags. Exercise an enabled native
        # refill before classifying the persisted zero-copper result again.
        assert [
            region.zone_name for region in reopened_fabrication.fill_zone_regions()
        ] == ["Synthetic ordinary pour"]
        assert reopened_generated.GetFilledPolysList(pcbnew.F_Cu).Area() == 0
        assert reopened_generated.IsFilled() is True
        assert reopened_generated.NeedRefill() is False
        parent.settings["gerber"]["fill_zones"] = False
        for filled, needs_refill, expected_generated in (
            (True, False, False),
            (True, True, True),
            (False, False, True),
            (True, False, False),
        ):
            reopened_generated.SetIsFilled(filled)
            reopened_generated.SetNeedRefill(needs_refill)
            before = _native_zone_reporting_state(reopened)

            actionable = reopened_fabrication.fill_zone_regions()

            assert reopened_generated.IsFilled() is filled
            assert reopened_generated.NeedRefill() is needs_refill
            assert _native_zone_reporting_state(reopened) == before
            assert any(region.is_teardrop is True for region in actionable) is (
                expected_generated
            )
            assert any(region.is_teardrop is False for region in actionable)
            if expected_generated:
                (region,) = (item for item in actionable if item.is_teardrop is True)
                assert region.is_filled is filled
                assert region.needs_refill is needs_refill
                assert region.refill_performed is False
