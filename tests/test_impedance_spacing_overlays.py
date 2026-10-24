"""Independent physical-coordinate oracles for spacing presentation."""

from dataclasses import replace
import math
from pathlib import Path

import pytest

from impedance.model import ValidationError
from impedance.service import CapturedImage, load_rendered_capture
from impedance.spacing_overlays import spacing_guide_paths

from .test_impedance_report_pipeline import _png


def test_viewport_metadata_preserves_png_bytes_hash_and_equality() -> None:
    """Presentation metadata must not alter pixel evidence or capture identity."""
    data = _png()
    plain = CapturedImage.from_bytes(data)
    located = CapturedImage.from_bytes(
        data, viewport_nm=(-1_000_000, 0, 2_000_000, 1_000_000)
    )
    assert located == plain
    assert (located.data, located.sha256) == (plain.data, plain.sha256)


@pytest.mark.parametrize(
    "viewport", [(0, 0, 0, 1), (0, 0, 1, float("nan")), (0, 0, True, 1), [0, 0, 1, 1]]
)
def test_invalid_viewport_metadata_is_rejected(viewport: object) -> None:
    """Refuse nonfinite or degenerate transforms at the immutable boundary."""
    with pytest.raises(ValidationError, match="viewport"):
        CapturedImage(_png(), viewport_nm=viewport)


def test_rendered_capture_uses_renderer_frame_without_replotting(
    tmp_path: Path,
) -> None:
    """Ask the owning renderer for its frame without requesting another image."""
    path = tmp_path / "capture.png"
    path.write_bytes(_png())
    calls: list[tuple[object, int, int]] = []

    class Renderer:
        def viewport(
            self, section: object, width_px: int, height_px: int
        ) -> tuple[float, float, float, float]:
            calls.append((section, width_px, height_px))
            return (-10_000_000, 5_000_000, 20_000_000, 10_000_000)

    section = object()
    capture = load_rendered_capture(path, Renderer(), section)
    assert capture.viewport_nm == (-10_000_000, 5_000_000, 20_000_000, 10_000_000)
    assert calls == [(section, 800, 420)]


def test_target_edges_follow_rotated_facing_copper_not_board_axes() -> None:
    """A 3-4-5 direction places guides at 0.225/0.325mm normal offsets."""
    from impedance.matching import analyze
    from impedance.spacing_review import spacing_review_entries

    from .test_impedance_spacing_checks import _line, _snapshot
    from .test_impedance_spacing_review_entries import _config

    snapshot = _snapshot(
        _line("p", "P", ((0, 0), (3_000_000, 4_000_000))),
        _line("n", "N", ((-440_000, 330_000), (2_560_000, 4_330_000))),
    )
    config = _config()
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    guides = [
        guide for guide in spacing_guide_paths(entry) if guide.kind == "target-edge"
    ]
    assert len(guides) == 2
    # The measured centers are 0.55mm apart, so the inward target from the mate
    # is 0.55 - 0.075 - 0.15 = 0.325mm from the source centerline.
    offsets = sorted(-0.8 * guide.start[0] + 0.6 * guide.start[1] for guide in guides)
    assert offsets == pytest.approx((225_000, 325_000), abs=1)
    for guide in guides:
        dx, dy = guide.end[0] - guide.start[0], guide.end[1] - guide.start[1]
        assert dx * 4 - dy * 3 == pytest.approx(0, abs=1)


def test_concentric_target_guides_preserve_exact_native_curvature() -> None:
    """Guides follow offset circles, with a true local radial measured ruler."""
    from impedance.matching import analyze
    from impedance.spacing_review import spacing_review_entries

    from .test_impedance_spacing_checks import _arc, _snapshot
    from .test_impedance_spacing_review_entries import _config

    snapshot = _snapshot(_arc("p", "P", 5_000_000), _arc("n", "N", 5_400_000))
    config = _config()
    entries = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )
    for entry in entries:
        guides = spacing_guide_paths(entry)
        curves = [guide for guide in guides if guide.kind == "target-edge"]
        assert len(curves) == 2
        assert all(guide.center == (0, 0) for guide in curves)
        assert sorted(
            math.dist(guide.start, guide.center) for guide in curves
        ) == pytest.approx((5_175_000, 5_225_000), abs=1)
        assert any(guide.kind == "measured-gap" for guide in guides)


def test_paired_full_circles_keep_target_guides_on_both_semicircles() -> None:
    """Shared endpoints cannot deduplicate distinct upper and lower circular paths."""
    from impedance.matching import analyze
    from impedance.spacing_review import spacing_review_entries

    from .test_impedance_spacing_checks import _arc, _snapshot
    from .test_impedance_spacing_review_entries import _config

    source, mate = _arc("pu", "P", 1_000_000), _arc("nu", "N", 1_400_000)
    snapshot = _snapshot(
        source,
        replace(source, trace_id="pl", native_arc_midpoint=(0, -1_000_000)),
        mate,
        replace(mate, trace_id="nl", native_arc_midpoint=(0, -1_400_000)),
    )
    config = _config()
    entries = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )
    assert len(entries) == 1
    guides = [
        guide
        for guide in spacing_guide_paths(entries[0])
        if guide.kind == "target-edge"
    ]
    assert len(guides) == 4
    assert sorted(round(math.dist(guide.start, guide.center)) for guide in guides) == [
        1_175_000,
        1_175_000,
        1_225_000,
        1_225_000,
    ]
    assert sum(guide.sweep > 0 for guide in guides) == 2
    assert sum(guide.sweep < 0 for guide in guides) == 2
    reversed_snapshot = replace(
        snapshot,
        traces=tuple(
            replace(trace, points=tuple(reversed(trace.points)))
            for trace in snapshot.traces
        ),
    )
    reversed_entry = spacing_review_entries(
        config, reversed_snapshot, analyze(config, reversed_snapshot).sections
    )[0]
    assert (
        len(
            [
                guide
                for guide in spacing_guide_paths(reversed_entry)
                if guide.kind == "target-edge"
            ]
        )
        == 4
    )


def test_same_center_arcs_with_endcap_correspondence_use_local_rulers() -> None:
    """A shared circle center cannot invent mate copper outside its angular span."""
    from impedance.matching import analyze
    from impedance.spacing_checks import _location, _primitives
    from impedance.spacing_review import spacing_review_entries

    from .test_impedance_spacing_checks import _arc, _snapshot
    from .test_impedance_spacing_review_entries import _config

    source = _arc("p", "P", 5_000_000)
    complete_mate = _arc("n", "N", 5_400_000)
    mate = replace(
        complete_mate,
        points=((0, 5_400_000), (0, -5_400_000)),
        native_arc_midpoint=(-5_400_000, 0),
    )
    location = _location(_primitives(source)[0], _primitives(mate), 0, 1)
    first = location.cross_sections[0]
    assert first.source_point == (5_000_000, 0)
    assert first.mate_point == (0, 5_400_000)
    config, snapshot = _config(), _snapshot(source, complete_mate)
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    entry = replace(
        entry,
        source_traces=(source,),
        mate_traces=(mate,),
        members=(location,),
        viewport_nm=(-9_000_000, -6_000_000, 18_000_000, 12_000_000),
    )
    guides = spacing_guide_paths(entry)
    assert not any(guide.kind == "target-edge" for guide in guides)
    assert not any(guide.facing_edge is not None for guide in guides)
    assert {guide.kind for guide in guides} == {"target-gap", "measured-gap"}


def test_radial_arc_witnesses_cannot_bridge_unsampled_angular_gap() -> None:
    """Radial samples at 0/90/135/180/270 degrees cannot prove mate coverage 0..90."""
    from impedance.matching import analyze
    from impedance.spacing_checks import _location, _primitives
    from impedance.spacing_review import spacing_review_entries

    from .test_impedance_spacing_checks import _arc, _snapshot
    from .test_impedance_spacing_review_entries import _config

    source = replace(
        _arc("p", "P", 5_000_000),
        points=((5_000_000, 0), (0, -5_000_000)),
        native_arc_midpoint=(-5_000_000, 0),
    )
    mate = replace(
        _arc("n", "N", 5_400_000),
        points=((0, 5_400_000), (5_400_000, 0)),
        native_arc_midpoint=(-5_400_000, 0),
    )
    location = _location(_primitives(source)[0], _primitives(mate), 0, 1)
    # Every sampled pair is radial; the missing shared angular interval still
    # prohibits continuous target edges. Require an exact sweep proof as well.
    for section in location.cross_sections:
        projected = tuple(
            value * 5_400_000 / 5_000_000 for value in section.source_point
        )
        assert math.dist(section.mate_point, projected) <= 2
    config, snapshot = (
        _config(),
        _snapshot(_arc("p", "P", 5_000_000), _arc("n", "N", 5_400_000)),
    )
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    entry = replace(
        entry,
        source_traces=(source,),
        mate_traces=(mate,),
        members=(location,),
        viewport_nm=(-9_000_000, -6_000_000, 18_000_000, 12_000_000),
    )
    assert not any(guide.kind == "target-edge" for guide in spacing_guide_paths(entry))


def test_guide_generation_never_invents_missing_correspondence() -> None:
    """Whole-route geometry alone cannot authorize a claimed target guide."""
    from impedance.matching import analyze
    from impedance.spacing_review import spacing_review_entries

    from .test_impedance_spacing_checks import _parallel
    from .test_impedance_spacing_review_entries import _config

    snapshot, config = _parallel(250_000), _config()
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    entry = replace(
        entry,
        members=tuple(replace(member, cross_sections=()) for member in entry.members),
        representative=None,
    )
    assert spacing_guide_paths(entry) == ()


def test_each_target_guide_uses_its_own_copper_width() -> None:
    """Asymmetric widths offset the two expected facing edges independently."""
    from impedance.matching import analyze
    from impedance.spacing_review import spacing_review_entries

    from .test_impedance_spacing_checks import _line, _parallel, _snapshot
    from .test_impedance_spacing_review_entries import _config

    snapshot = _snapshot(
        _line("p", "P", ((0, 0), (3_000_000, 0)), 100_000),
        _line("n", "N", ((0, 550_000), (3_000_000, 550_000)), 300_000),
    )
    config = _config()
    # The review scope is pair/layer; guide widths come from current native
    # geometry independently of manufacturing's matching-width row grouping.
    scope = analyze(config, _parallel(400_000, length=3_000_000)).sections
    entry = spacing_review_entries(config, snapshot, scope)[0]
    guides = spacing_guide_paths(entry)
    assert {guide.start[1] for guide in guides if guide.kind == "target-edge"} == {
        200_000,
        250_000,
    }
    assert all(
        math.dist(guide.start, guide.end) == pytest.approx(350_000)
        for guide in guides
        if guide.kind == "measured-gap"
    )


def test_single_witness_uses_local_target_ruler_without_invented_continuation() -> None:
    """An isolated exact witness supports one ruler, not a guessed long edge."""
    from impedance.matching import analyze
    from impedance.spacing_review import spacing_review_entries

    from .test_impedance_spacing_checks import _parallel
    from .test_impedance_spacing_review_entries import _config

    snapshot, config = _parallel(250_000), _config()
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    member = entry.members[0]
    section = member.cross_sections[1]
    entry = replace(
        entry,
        members=(replace(member, cross_sections=(section,)),),
        representative=section,
    )
    guides = spacing_guide_paths(entry)
    assert {guide.kind for guide in guides} == {"target-gap", "measured-gap"}
    target = next(guide for guide in guides if guide.kind == "target-gap")
    measured = next(guide for guide in guides if guide.kind == "measured-gap")
    assert math.dist(target.start, target.end) == pytest.approx(150_000)
    assert math.dist(measured.start, measured.end) == pytest.approx(250_000)


def test_long_uniform_concern_has_one_readable_measured_ruler() -> None:
    """One worst-gap detail retains the complete 80mm native concern geometry."""
    from impedance.matching import analyze
    from impedance.spacing_review import spacing_review_entries

    from .test_impedance_spacing_checks import _parallel
    from .test_impedance_spacing_review_entries import _config

    snapshot, config = _parallel(50_000, length=80_000_000), _config()
    entries = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )
    assert len(entries) == 1
    for entry in entries:
        assert min(point[0] for trace in entry.traces for point in trace.points) == 0
        assert (
            max(point[0] for trace in entry.traces for point in trace.points)
            == 80_000_000
        )
        assert entry.representative is not None
        ticks = [
            guide
            for guide in spacing_guide_paths(entry)
            if guide.kind == "measured-gap"
        ]
        assert 1 <= len(ticks) <= 3
        for tick in ticks:
            assert math.dist(tick.start, tick.end) == pytest.approx(50_000)
