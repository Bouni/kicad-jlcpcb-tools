"""Adapter coverage for pad and via copper flashes used by stub detection."""

from math import cos, radians, sin
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from impedance.matching import is_pad_via_stub
from impedance.model import CopperFeature, Trace
from impedance.pcbnew_adapter import _copper_features


class _Point:
    def __init__(self, x: int, y: int) -> None:
        self.x = x
        self.y = y


def test_copper_features_emit_via_and_pad_flashes_per_layer() -> None:
    """Through vias and pads contribute one same-net disc per copper layer."""
    pcbnew = SimpleNamespace(
        PCB_VIA=type("PCB_VIA", (), {}),
        PAD_SHAPE_CIRCLE=0,
        PAD_SHAPE_OVAL=1,
    )

    class Via(pcbnew.PCB_VIA):
        def GetWidth(self, layer_id: int) -> int:
            return 600_000

        def GetPosition(self) -> _Point:
            return _Point(10, 20)

        def GetNetname(self) -> str:
            return "CLK"

        def FlashLayer(self, layer_id: int) -> bool:
            return layer_id in (0, 31)

        @property
        def m_Uuid(self) -> SimpleNamespace:
            return SimpleNamespace(AsString=lambda: "via-uuid")

    class Pad:
        def GetNetname(self) -> str:
            return "CLK"

        def GetSize(self, layer_id: int) -> _Point:
            return _Point(800_000, 800_000)

        def ShapePos(self, layer_id: int) -> _Point:
            return _Point(0, 0)

        def GetShape(self, layer_id: int) -> int:
            return 0

        def GetOrientationDegrees(self) -> float:
            return 0.0

        def FlashLayer(self, layer_id: int) -> bool:
            return layer_id == 0

        @property
        def m_Uuid(self) -> SimpleNamespace:
            return SimpleNamespace(AsString=lambda: "pad-uuid")

    board = SimpleNamespace(
        GetTracks=lambda: (Via(),),
        GetFootprints=lambda: (SimpleNamespace(Pads=lambda: (Pad(),)),),
    )
    layers = {"F.Cu": 0, "B.Cu": 31}

    features = _copper_features(board, pcbnew, layers)

    assert {feature.kind for feature in features} == {"via", "pad"}
    vias = [feature for feature in features if feature.kind == "via"]
    pads = [feature for feature in features if feature.kind == "pad"]
    assert {feature.layer for feature in vias} == {"F.Cu", "B.Cu"}
    assert len(pads) == 1 and pads[0].layer == "F.Cu"
    assert pads[0].shape == "circle"
    assert pads[0].net == "CLK"
    assert all(feature.feature_id.count(":") == 1 for feature in features)


class LayerPad:
    """Model KiCad's front compatibility getters and exact physical-layer getters."""

    def __init__(self, rotation: float = 0.0) -> None:
        self.rotation = rotation
        self.sizes = {0: (1_000_000, 1_000_000), 31: (200_000, 200_000)}
        self.shapes = {0: 2, 31: 0}
        self.positions = {0: (500_000, 0), 31: (0, 0)}
        self.reads: list[tuple[str, int]] = []

    def GetNetname(self) -> str:
        """Return the shared routed signal net."""
        return "CLK"

    def GetSize(self, layer_id: int = 0) -> _Point:
        """Preserve the native no-argument F.Cu compatibility behavior."""
        self.reads.append(("size", layer_id))
        return _Point(*self.sizes[layer_id])

    def GetShape(self, layer_id: int = 0) -> int:
        """Expose independent circular and rectangular layer flashes."""
        self.reads.append(("shape", layer_id))
        return self.shapes[layer_id]

    def GetPosition(self) -> _Point:
        """Return the anchor, which differs from offset copper geometry."""
        return _Point(0, 0)

    def ShapePos(self, layer_id: int) -> _Point:
        """Return native already-rotated board coordinates of the copper flash."""
        self.reads.append(("position", layer_id))
        return _Point(*self.positions[layer_id])

    def GetOrientationDegrees(self) -> float:
        """Return the orientation used by the native shape position."""
        return self.rotation

    def FlashLayer(self, layer_id: int) -> bool:
        """Expose only actual flashed copper layers."""
        return layer_id in self.sizes


class LayerVia:
    """Represent a via whose physical-layer diameters differ."""

    def GetClass(self) -> str:
        """Let the adapter identify a via without requiring pcbnew imports."""
        return "PCB_VIA"

    def GetWidth(self, layer_id: Optional[int] = None) -> int:
        """Expose a misleading inherited default alongside exact diameters."""
        return 200_000 if layer_id == 31 else 1_000_000

    def GetPosition(self) -> _Point:
        """Keep each flash at the native via center."""
        return _Point(0, 0)

    def GetNetname(self) -> str:
        """Return the routed signal net."""
        return "CLK"

    def FlashLayer(self, layer_id: int) -> bool:
        """Restrict copper to the layers occupied by the via."""
        return layer_id in (0, 31)


def _features(pad: Any = None, via: Any = None) -> tuple[CopperFeature, ...]:
    """Use the actual adapter against independent native-style geometry."""
    board = SimpleNamespace(
        GetTracks=lambda: () if via is None else (via,),
        GetFootprints=lambda: (
            () if pad is None else (SimpleNamespace(Pads=lambda: (pad,)),)
        ),
    )
    module = SimpleNamespace(PAD_SHAPE_CIRCLE=0, PAD_SHAPE_OVAL=1)
    return _copper_features(board, module, {"F.Cu": 0, "B.Cu": 31})


@pytest.mark.parametrize("kind", ["pad", "via"])
def test_smaller_back_flash_does_not_exclude_uncovered_route(kind: str) -> None:
    """Front padstack geometry cannot hide a route beyond back-layer copper."""
    features = _features(pad=LayerPad()) if kind == "pad" else _features(via=LayerVia())
    route = Trace("back", "B.Cu", "CLK", 100_000, ((0, 0), (350_000, 0)))
    assert not is_pad_via_stub(route, features)
    assert (
        next(feature for feature in features if feature.layer == "B.Cu").size_x_nm
        == 200_000
    )


def test_pad_offset_uses_native_shape_position() -> None:
    """A pad's anchor cannot exclude a route outside its offset copper rectangle."""
    pad = LayerPad()
    route = Trace("offset", "F.Cu", "CLK", 100_000, ((0, 0), (-400_000, 0)))
    features = _features(pad=pad)
    assert not is_pad_via_stub(route, features)
    front = next(feature for feature in features if feature.layer == "F.Cu")
    assert front.center == (500_000, 0)
    assert pad.positions == {0: (500_000, 0), 31: (0, 0)}
    assert set(pad.reads) == {
        (method, layer) for method in ("size", "shape", "position") for layer in (0, 31)
    }


def test_rotated_offset_is_not_applied_a_second_time() -> None:
    """Native ShapePos already includes the pad rotation and board translation."""
    pad = LayerPad(rotation=90.0)
    pad.positions[0] = (100_000, 700_000)
    features = _features(pad=pad)
    front = next(feature for feature in features if feature.layer == "F.Cu")
    assert front.center == (100_000, 700_000)
    route = Trace(
        "covered", "F.Cu", "CLK", 100_000, ((100_000, 700_000), (100_000, 750_000))
    )
    assert is_pad_via_stub(route, features)


@pytest.mark.parametrize(
    "missing",
    ["ShapePos", "GetSize", "GetShape", "FlashLayer", "GetOrientationDegrees"],
)
def test_unavailable_precise_pad_api_cannot_exclude_copper(missing: str) -> None:
    """Incomplete native geometry leaves routing exportable instead of guessing."""
    pad = LayerPad()
    setattr(pad, missing, None)
    assert not _features(pad=pad)


def test_legacy_front_only_size_is_not_reused_on_other_layers() -> None:
    """Bindings exposing only compatibility getters do not prove any layer flash."""
    pad = LayerPad()
    pad.GetSize = lambda: _Point(1_000_000, 1_000_000)  # type: ignore[method-assign]
    assert not _features(pad=pad)


def test_legacy_via_diameter_is_not_reused_on_other_layers() -> None:
    """An inherited no-argument via width cannot establish physical-layer copper."""
    via = LayerVia()
    via.GetWidth = lambda: 1_000_000  # type: ignore[method-assign]
    assert not _features(via=via)


def test_existing_unsupported_pad_shape_classification_is_preserved() -> None:
    """Leave the explicitly deferred unsupported-shape behavior unchanged."""
    pad = LayerPad()
    pad.shapes = {0: 99, 31: 99}
    assert {feature.shape for feature in _features(pad=pad)} == {"rect"}


@pytest.mark.parametrize(
    ("shape", "rotation"), [(2, 30.0), (2, 45.0), (1, 30.0), (1, 45.0)]
)
@pytest.mark.parametrize("covered", [False, True])
def test_oblique_pad_coverage_uses_inverse_native_rotation(
    shape: int, rotation: float, covered: bool
) -> None:
    """KiCad's positive rotation maps local +x toward board -y, never toward +y."""
    pad = LayerPad(rotation=rotation)
    pad.sizes[0] = (1_000_000, 200_000)
    pad.shapes[0] = shape
    pad.positions[0] = (500_000, 100_000)
    angle = radians(rotation)
    distance = 300_000
    end = (
        500_000 + round(distance * cos(angle)),
        100_000 + round(distance * sin(angle)) * (-1 if covered else 1),
    )
    route = Trace("rotated", "F.Cu", "CLK", 100_000, ((500_000, 100_000), end))
    assert is_pad_via_stub(route, _features(pad=pad)) is covered


def test_unreadable_back_padstack_preserves_other_layer_flashes() -> None:
    """A failed precise lookup cannot reuse front data or erase trustworthy front copper."""
    pad = LayerPad()

    def size(layer_id: int) -> _Point:
        """Model a native runtime that cannot read one padstack layer."""
        if layer_id == 31:
            raise RuntimeError("Layer geometry is unavailable")
        return _Point(1_000_000, 1_000_000)

    pad.GetSize = size  # type: ignore[method-assign]
    assert {feature.layer for feature in _features(pad=pad)} == {"F.Cu"}


def test_removed_copper_and_membership_only_api_do_not_prove_flashes() -> None:
    """Copper layer membership cannot replace native FlashLayer evidence."""
    pad = LayerPad()
    pad.FlashLayer = None  # type: ignore[method-assign]
    pad.IsOnLayer = lambda layer_id: True
    assert not _features(pad=pad)


def test_native_angle_wrapper_fallback_retains_precise_orientation() -> None:
    """Bindings exposing EDA_ANGLE instead of direct degrees still support containment."""
    pad = LayerPad()
    pad.GetOrientationDegrees = None  # type: ignore[method-assign]
    pad.GetOrientation = lambda: SimpleNamespace(AsDegrees=lambda: 90.0)
    assert {feature.rotation_deg for feature in _features(pad=pad)} == {90.0}


def test_unitless_native_angle_cannot_be_assumed_to_be_degrees() -> None:
    """Legacy integer decidegrees cannot establish the orientation of a copper flash."""
    pad = LayerPad()
    pad.GetOrientationDegrees = None  # type: ignore[method-assign]
    pad.GetOrientation = lambda: 450
    assert not _features(pad=pad)
