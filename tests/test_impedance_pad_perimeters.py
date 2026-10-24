"""Use actual native pad boundaries, including concavities, to exclude stubs."""

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from impedance.matching import analyze, is_pad_via_stub, validate_review
from impedance.model import (
    EXCLUSION_COVERED_STUB,
    BoardSnapshot,
    Config,
    CopperFeature,
    LayerSettings,
    Specification,
    Trace,
    ValidationError,
)
from impedance.pcbnew_adapter import _copper_features, snapshot_board
from impedance.service import prepare

Point = tuple[int, int]
Contour = tuple[Point, ...]


class NativePolygon:
    """Preserve native outline/hole ownership and API semantics without loading wx."""

    def __init__(self) -> None:
        self.polygons: tuple[tuple[Contour, ...], ...] = ()

    def OutlineCount(self) -> int:
        """Return the number of distinct native copper islands."""
        return len(self.polygons)

    def Outline(self, index: int) -> Any:
        """Return one native outer boundary."""
        return self._chain(self.polygons[index][0])

    def HoleCount(self, index: int) -> int:
        """Return the number of holes in one island."""
        return len(self.polygons[index]) - 1

    def Hole(self, index: int, hole: int) -> Any:
        """Return one native hole boundary."""
        return self._chain(self.polygons[index][hole + 1])

    @staticmethod
    def _chain(points: Contour) -> Any:
        return SimpleNamespace(
            PointCount=lambda: len(points),
            CPoint=lambda index: SimpleNamespace(
                x=points[index][0], y=points[index][1]
            ),
        )


class NativePad:
    """Expose exact layer geometry separately from the misleading bounding size."""

    def __init__(
        self, shape: int, polygons: tuple[tuple[Contour, ...], ...] = ()
    ) -> None:
        self.shape = shape
        self.polygons = polygons
        self.reads: list[tuple[int, int, int, str]] = []

    def GetNetname(self) -> str:
        """Return independently known routed-net membership."""
        return "RF"

    def FlashLayer(self, layer: int) -> bool:
        """Expose physical front copper only."""
        return layer == 0

    def GetSize(self, layer: int) -> Any:
        """Expose the anchor size, which cannot prove complex containment."""
        return SimpleNamespace(x=1_000_000, y=1_000_000)

    def GetShape(self, layer: int) -> int:
        """Identify native geometry without coercing unknown values."""
        return self.shape

    def ShapePos(self, layer: int) -> Any:
        """Return native board coordinates of the copper center."""
        return SimpleNamespace(x=0, y=0)

    def GetOrientationDegrees(self) -> float:
        """Return the native analytic-shape rotation."""
        return 0.0

    def GetRoundRectCornerRadius(self, layer: int) -> int:
        """Return the physical-layer corner radius."""
        assert layer == 0
        return 250_000

    def TransformShapeToPolygon(
        self,
        buffer: NativePolygon,
        layer: int,
        clearance: int,
        error: int,
        location: str,
    ) -> None:
        """Populate the buffer in board coordinates, as KiCad does."""
        self.reads.append((layer, clearance, error, location))
        buffer.polygons = self.polygons


def _scenario(
    pad: NativePad, start: Point, end: Point, width: int
) -> tuple[Config, BoardSnapshot]:
    """Copy the pad through the real adapter with independently supplied intent."""
    board = SimpleNamespace(
        GetTracks=lambda: (),
        GetFootprints=lambda: (SimpleNamespace(Pads=lambda: (pad,)),),
    )
    module = SimpleNamespace(
        PAD_SHAPE_CIRCLE=0,
        PAD_SHAPE_OVAL=1,
        PAD_SHAPE_RECT=2,
        PAD_SHAPE_ROUNDRECT=3,
        SHAPE_POLY_SET=NativePolygon,
        ERROR_INSIDE="inside",
    )
    features = _copper_features(board, module, {"F.Cu": 0, "B.Cu": 31})
    trace = Trace("route", "F.Cu", "RF", width, (start, end))
    snapshot = BoardSnapshot(
        ("F.Cu", "B.Cu"),
        (trace,),
        net_classes=("RF",),
        net_class_memberships=(("RF", ("RF",)),),
        net_class_context_digest="classes",
        copper_features=features,
    )
    config = Config(
        True,
        (
            Specification(
                "rf",
                "RF",
                "50",
                "single_ended",
                "RF",
                (LayerSettings("F.Cu", ("B.Cu",)),),
            ),
        ),
    )
    return config, snapshot


def _exclusion(pad: NativePad, start: Point, end: Point, width: int = 100_000) -> str:
    """Exercise the real native adapter, validation, matching, and section policy."""
    config, snapshot = _scenario(pad, start, end, width)
    return analyze(config, snapshot).sections[0].exclusion_reason


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        ((400_000, 400_000), (425_000, 400_000), ""),
        ((0, 0), (100_000, 0), EXCLUSION_COVERED_STUB),
        ((0, 0), (450_000, 0), EXCLUSION_COVERED_STUB),
        ((0, 0), (450_001, 0), ""),
        ((0, 0), (370_000, 410_000), EXCLUSION_COVERED_STUB),
        ((0, 0), (400_000, 400_000), ""),
    ],
)
def test_roundrect_requires_whole_capsule_inside_actual_corners(
    start: Point, end: Point, expected: str
) -> None:
    """Rounded corners expose copper which an enclosing rectangle would hide."""
    assert _exclusion(NativePad(3), start, end) == expected


@pytest.mark.parametrize("shape", [4, 5, 99])
def test_native_chamfer_trapezoid_or_custom_perimeter_is_used(shape: int) -> None:
    """Every nonprimitive shape uses its native perimeter, never its bounding box."""
    outer = (
        (-500_000, -500_000),
        (500_000, -500_000),
        (500_000, 0),
        (0, 0),
        (0, 500_000),
        (-500_000, 500_000),
    )
    pad = NativePad(shape, ((outer,),))
    assert (
        _exclusion(pad, (-300_000, 200_000), (-300_000, 400_000))
        == EXCLUSION_COVERED_STUB
    )
    assert _exclusion(pad, (300_000, 200_000), (300_000, 400_000)) == ""
    # Both endpoint discs fit; the center of the connecting capsule leaves the pad.
    assert _exclusion(pad, (-200_000, 300_000), (300_000, -200_000)) == ""
    assert set(pad.reads) == {(0, 0, 1, "inside")}


def test_native_perimeter_holes_and_boundary_are_respected() -> None:
    """A capsule cannot cross a hole even when its endpoints lie within copper."""
    outer = (
        (-500_000, -500_000),
        (500_000, -500_000),
        (500_000, 500_000),
        (-500_000, 500_000),
    )
    hole = (
        (-100_000, -100_000),
        (100_000, -100_000),
        (100_000, 100_000),
        (-100_000, 100_000),
    )
    pad = NativePad(99, ((outer, hole),))
    assert _exclusion(pad, (-300_000, 0), (300_000, 0)) == ""
    assert (
        _exclusion(pad, (-300_000, 300_000), (450_000, 300_000))
        == EXCLUSION_COVERED_STUB
    )
    assert _exclusion(pad, (-300_000, 300_000), (450_001, 300_000)) == ""


def test_native_perimeter_unavailable_retains_trace_for_review() -> None:
    """Unreadable complex geometry cannot become a guessed rectangular flash."""
    pad = NativePad(99)
    pad.TransformShapeToPolygon = None  # type: ignore[method-assign]
    assert _exclusion(pad, (0, 0), (100_000, 0)) == ""


def test_oval_uses_native_capsule_perimeter_instead_of_an_ellipse() -> None:
    """A horizontal oval has straight sides between its semicircular ends."""
    pad = NativePad(1)
    pad.GetSize = lambda layer: SimpleNamespace(x=1_000_000, y=200_000)  # type: ignore[method-assign]
    assert (
        _exclusion(pad, (250_000, 45_000), (350_000, 45_000)) == EXCLUSION_COVERED_STUB
    )
    assert _exclusion(pad, (400_000, 45_000), (450_000, 45_000)) == ""


def test_native_custom_coordinates_are_not_rotated_or_offset_twice() -> None:
    """The native polygon already includes shape position and orientation."""
    outer = (
        (500_000, 500_000),
        (1_000_000, 500_000),
        (1_000_000, 1_000_000),
        (500_000, 1_000_000),
    )
    pad = NativePad(99, ((outer,),))
    pad.GetOrientationDegrees = lambda: 90.0  # type: ignore[method-assign]
    pad.ShapePos = lambda layer: SimpleNamespace(x=750_000, y=750_000)  # type: ignore[method-assign]
    assert (
        _exclusion(pad, (600_000, 750_000), (900_000, 750_000))
        == EXCLUSION_COVERED_STUB
    )
    assert _exclusion(pad, (0, 0), (100_000, 0)) == ""


def test_separate_native_copper_islands_cannot_cover_the_gap_between_them() -> None:
    """Different contained endpoints cannot authorize the intervening empty space."""
    left = (
        (-500_000, -500_000),
        (-100_000, -500_000),
        (-100_000, 500_000),
        (-500_000, 500_000),
    )
    right = (
        (100_000, -500_000),
        (500_000, -500_000),
        (500_000, 500_000),
        (100_000, 500_000),
    )
    pad = NativePad(99, ((left,), (right,)))
    assert _exclusion(pad, (-300_000, 0), (300_000, 0)) == ""
    assert _exclusion(pad, (200_000, 0), (400_000, 0)) == EXCLUSION_COVERED_STUB


@pytest.mark.parametrize("shape", [3, 99])
def test_contained_pad_copper_stays_out_of_required_export_after_reopening(
    shape: int,
) -> None:
    """Keep the full native route inventory while exporting only reviewable rows."""
    outer = (
        (-500_000, -500_000),
        (500_000, -500_000),
        (500_000, 500_000),
        (-500_000, 500_000),
    )
    pad = NativePad(shape, ((outer,),))
    config, snapshot = _scenario(pad, (0, 0), (100_000, 0), 100_000)
    route = Trace(
        "external", "F.Cu", "RF", 100_000, ((0, 2_000_000), (1_000_000, 2_000_000))
    )
    snapshot = replace(snapshot, traces=snapshot.traces + (route,))
    analysis = analyze(config, snapshot)
    assert {section.exclusion_reason for section in analysis.sections} == {
        "",
        EXCLUSION_COVERED_STUB,
    }
    approved = replace(
        config,
        reviewed_digest=analysis.digest,
        included_section_ids=tuple(
            section.section_id
            for section in analysis.sections
            if not section.exclusion_reason
        ),
    )
    reopened = Config.from_dict(approved.to_dict())
    plan = prepare(reopened, snapshot)
    assert plan is not None
    assert tuple(
        trace.trace_id for section in plan.sections for trace in section.traces
    ) == ("external",)
    assert {
        trace.trace_id for section in plan.analysis.sections for trace in section.traces
    } == {"route", "external"}


def _native_arc(
    pad: NativePad,
    stroke: tuple[tuple[Contour, ...], ...],
    *,
    available: bool = True,
    net: str = "RF",
) -> Trace:
    """Copy a native arc's outward copper envelope independently of its samples."""
    reads: list[tuple[int, int, int, str]] = []

    def transform(
        buffer: NativePolygon, layer: int, clearance: int, error: int, location: str
    ) -> None:
        """Supply the native complete stroke, including its end caps."""
        reads.append((layer, clearance, error, location))
        buffer.polygons = stroke

    item = SimpleNamespace(
        GetClass=lambda: "PCB_ARC",
        GetLayer=lambda: 0,
        GetWidth=lambda: 100_000,
        GetStart=lambda: SimpleNamespace(x=100_000, y=0),
        GetMid=lambda: SimpleNamespace(x=70_711, y=70_711),
        GetEnd=lambda: SimpleNamespace(x=0, y=100_000),
        GetNetname=lambda: net,
        TransformShapeToPolygon=transform if available else None,
        m_Uuid=SimpleNamespace(AsString=lambda: "arc"),
    )
    board = SimpleNamespace(
        GetTracks=lambda: (item,),
        GetEnabledLayers=lambda: SimpleNamespace(CuStack=lambda: (0, 31)),
        GetFootprints=lambda: (SimpleNamespace(Pads=lambda: (pad,)),),
        GetDrawings=lambda: (),
    )
    module = SimpleNamespace(
        F_Cu=0,
        B_Cu=31,
        PAD_SHAPE_CIRCLE=0,
        PAD_SHAPE_OVAL=1,
        PAD_SHAPE_RECT=2,
        PAD_SHAPE_ROUNDRECT=3,
        SHAPE_POLY_SET=NativePolygon,
        ERROR_INSIDE="inside",
        ERROR_OUTSIDE="outside",
        PCB_IO_KICAD_SEXPR=lambda: SimpleNamespace(
            Format=lambda item: None,
            GetStringOutput=lambda reset: '(footprint "sample")',
        ),
    )
    trace = snapshot_board(board, module).traces[0]
    assert reads == ([(0, 0, 1, "outside")] if available and net == "RF" else [])
    return trace


@pytest.mark.parametrize("shape", ["circle", "oval", "rect", "roundrect"])
def test_complete_native_arc_stroke_inside_convex_pad_is_a_stub(shape: str) -> None:
    """A trustworthy native outside envelope proves copper containment of arcs."""
    # This independent outer envelope includes the complete quarter-circle
    # stroke and both 50 um-radius caps, rather than just a centerline polygon.
    outer = (
        (-50_000, -50_000),
        (150_000, -50_000),
        (150_000, 150_000),
        (-50_000, 150_000),
    )
    trace = _native_arc(NativePad(2), ((outer,),))
    feature = CopperFeature(
        "pad",
        "RF",
        "F.Cu",
        "pad",
        shape,
        (0, 0),
        600_000,
        600_000,
        corner_radius_nm=100_000 if shape == "roundrect" else 0,
    )
    assert is_pad_via_stub(trace, (feature,))


def test_native_arc_stroke_proof_contains_width_and_round_endcaps() -> None:
    """A centerline that fits does not authorize excluding an exposed end cap."""
    outer = (
        (-50_000, -50_000),
        (150_000, -50_000),
        (150_000, 150_000),
        (-50_000, 150_000),
    )
    trace = _native_arc(NativePad(2), ((outer,),))
    feature = CopperFeature(
        "pad", "RF", "F.Cu", "pad", "rect", (0, 0), 280_000, 280_000
    )
    assert all(abs(x) <= 140_000 and abs(y) <= 140_000 for x, y in trace.points)
    assert not is_pad_via_stub(trace, (feature,))


def test_native_arc_cannot_use_a_sampled_chord_as_copper_proof() -> None:
    """The native stroke can expose copper between apparently safe samples."""
    outer = (
        (-50_000, -50_000),
        (150_000, -50_000),
        (180_000, 70_000),
        (150_000, 150_000),
        (-50_000, 150_000),
    )
    trace = _native_arc(NativePad(2), ((outer,),))
    feature = CopperFeature(
        "pad", "RF", "F.Cu", "pad", "rect", (0, 0), 310_000, 310_000
    )
    assert all(
        abs(x) + 50_000 <= 155_000 and abs(y) + 50_000 <= 155_000
        for x, y in trace.points
    )
    assert not is_pad_via_stub(trace, (feature,))


def test_native_arc_inside_custom_pad_avoids_holes_and_concavities() -> None:
    """Whole native stroke boundaries, not endpoint discs, determine containment."""
    outer = (
        (-50_000, -50_000),
        (150_000, -50_000),
        (150_000, 150_000),
        (-50_000, 150_000),
    )
    trace = _native_arc(NativePad(2), ((outer,),))
    pad_outer = (
        (-500_000, -500_000),
        (500_000, -500_000),
        (500_000, 500_000),
        (-500_000, 500_000),
    )
    pad_hole = ((70_000, 70_000), (80_000, 70_000), (80_000, 80_000), (70_000, 80_000))
    feature = CopperFeature(
        "pad",
        "RF",
        "F.Cu",
        "pad",
        "polygon",
        (0, 0),
        1_000_000,
        1_000_000,
        polygons=((pad_outer,),),
    )
    assert is_pad_via_stub(trace, (feature,))
    assert not is_pad_via_stub(
        trace, (replace(feature, polygons=((pad_outer, pad_hole),)),)
    )
    notch = (
        (-500_000, -500_000),
        (500_000, -500_000),
        (500_000, 30_000),
        (20_000, 30_000),
        (20_000, 50_000),
        (500_000, 50_000),
        (500_000, 500_000),
        (-500_000, 500_000),
    )
    assert not is_pad_via_stub(trace, (replace(feature, polygons=((notch,),)),))


def test_custom_pad_hole_inside_native_arc_void_does_not_expose_copper() -> None:
    """Native stroke holes remain empty; they are not fabricated copper."""
    outer = (
        (-200_000, -200_000),
        (200_000, -200_000),
        (200_000, 200_000),
        (-200_000, 200_000),
    )
    inner = ((-60_000, -60_000), (60_000, -60_000), (60_000, 60_000), (-60_000, 60_000))
    trace = _native_arc(NativePad(2), ((outer, inner),))
    pad_outer = (
        (-500_000, -500_000),
        (500_000, -500_000),
        (500_000, 500_000),
        (-500_000, 500_000),
    )
    pad_hole = (
        (-40_000, -40_000),
        (40_000, -40_000),
        (40_000, 40_000),
        (-40_000, 40_000),
    )
    feature = CopperFeature(
        "pad",
        "RF",
        "F.Cu",
        "pad",
        "polygon",
        (0, 0),
        1_000_000,
        1_000_000,
        polygons=((pad_outer, pad_hole),),
    )
    assert is_pad_via_stub(trace, (feature,))


@pytest.mark.parametrize(
    "failure",
    ["missing_api", "incomplete_native_contour", "degenerate_contour", "other_net"],
)
def test_native_arc_without_trustworthy_native_stroke_stays_reviewable(
    failure: str,
) -> None:
    """Unavailable proof never falls back to inferred centerline containment."""
    outer = (
        (-50_000, -50_000),
        (150_000, -50_000),
        (150_000, 150_000),
        (-50_000, 150_000),
    )
    contour = (
        outer[:2]
        if failure == "incomplete_native_contour"
        else ((0, 0), (1, 1), (2, 2))
        if failure == "degenerate_contour"
        else outer
    )
    trace = _native_arc(
        NativePad(2),
        ((contour,),),
        available=failure != "missing_api",
        net="OTHER" if failure == "other_net" else "RF",
    )
    feature = CopperFeature(
        "pad", "RF", "F.Cu", "pad", "rect", (0, 0), 1_000_000, 1_000_000
    )
    assert not trace.copper_polygons
    assert not is_pad_via_stub(trace, (feature,))


def test_native_arc_proof_change_invalidates_approval_and_keeps_section_identity() -> (
    None
):
    """Changed native proof cannot reuse an old approval of identical samples."""
    outer = (
        (-50_000, -50_000),
        (150_000, -50_000),
        (150_000, 150_000),
        (-50_000, 150_000),
    )
    pad = NativePad(2)
    arc = _native_arc(pad, ((outer,),))
    config, snapshot = _scenario(pad, (0, 0), (100_000, 0), 100_000)
    before = replace(snapshot, traces=(replace(arc, copper_polygons=()),))
    current = replace(snapshot, traces=(arc,))
    historical = analyze(config, before)
    corrected = analyze(config, current)
    assert historical.sections[0].exclusion_reason == ""
    assert corrected.sections[0].exclusion_reason == EXCLUSION_COVERED_STUB
    assert historical.sections[0].section_id == corrected.sections[0].section_id
    approved = replace(
        config,
        reviewed_digest=historical.digest,
        included_section_ids=(historical.sections[0].section_id,),
    )
    reopened = Config.from_dict(approved.to_dict())
    with pytest.raises(ValidationError, match="changed|review"):
        validate_review(reopened, corrected)


def test_native_arc_copper_proof_is_validated_before_matching() -> None:
    """Malformed supposedly immutable native geometry cannot authorize exclusion."""
    config, snapshot = _scenario(NativePad(2), (0, 0), (100_000, 0), 100_000)
    malformed = replace(
        snapshot.traces[0],
        is_native_arc=True,
        copper_polygons=([((0, 0), (1, 1), (2, 0))],),
    )
    with pytest.raises(ValidationError, match="copper perimeter"):
        analyze(config, replace(snapshot, traces=(malformed,)))


def test_contained_native_arc_is_not_required_for_export_after_reopening() -> None:
    """Native arc stubs follow the same selected-report policy as straight stubs."""
    outer = (
        (-50_000, -50_000),
        (150_000, -50_000),
        (150_000, 150_000),
        (-50_000, 150_000),
    )
    pad = NativePad(2)
    arc = _native_arc(pad, ((outer,),))
    config, snapshot = _scenario(pad, (0, 0), (100_000, 0), 100_000)
    route = Trace(
        "external", "F.Cu", "RF", 100_000, ((0, 2_000_000), (1_000_000, 2_000_000))
    )
    snapshot = replace(snapshot, traces=(arc, route))
    analysis = analyze(config, snapshot)
    assert {section.exclusion_reason for section in analysis.sections} == {
        "",
        EXCLUSION_COVERED_STUB,
    }
    approved = replace(
        config,
        reviewed_digest=analysis.digest,
        included_section_ids=tuple(
            section.section_id
            for section in analysis.sections
            if not section.exclusion_reason
        ),
    )
    plan = prepare(Config.from_dict(approved.to_dict()), snapshot)
    assert plan is not None
    assert tuple(
        trace.trace_id for section in plan.sections for trace in section.traces
    ) == ("external",)
