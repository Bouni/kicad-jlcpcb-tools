"""Tests for jlcfootprint.geometry: units, frames, hashing and the CCW conversion."""

import pytest

from jlcfootprint.geometry import (
    FLIP_EASYEDA_Y,
    Pad,
    ccw_correction,
    centroid,
    easyeda_pads_to_mm,
    mirror_box,
    mirror_y,
    named_pads,
    naming_rule_to_cpl,
    normalize_angle,
    pad_box_centre,
    pad_geom,
    pad_hash,
    pad_pitch,
    snap_to_90,
)


def test_easyeda_pads_to_mm_scales_and_flips_only_when_asked():
    """One canvas unit is 0.254 mm; Y follows the flip argument, defaulting to the module constant."""
    raw = [
        {
            "number": "1",
            "x": 10,
            "y": -20,
            "w": 4,
            "h": 2,
            "rotation": 90,
            "shape": "RECT",
        }
    ]
    (unflipped,) = easyeda_pads_to_mm(raw, flip_y=False)
    assert unflipped.number == "1"
    assert unflipped.x == pytest.approx(2.54)
    assert unflipped.y == pytest.approx(-5.08)
    assert unflipped.width == pytest.approx(1.016)
    assert unflipped.height == pytest.approx(0.508)
    assert unflipped.rotation == 90.0
    assert unflipped.shape == "RECT"
    (flipped,) = easyeda_pads_to_mm(raw, flip_y=True)
    assert flipped.y == pytest.approx(5.08)
    (default,) = easyeda_pads_to_mm(raw)
    assert default == easyeda_pads_to_mm(raw, flip_y=FLIP_EASYEDA_Y)[0]


def test_easyeda_pads_to_mm_defaults_rotation_and_shape():
    """Raw pads without a rotation or shape field get 0 degrees and an empty shape."""
    (pad,) = easyeda_pads_to_mm(
        [{"number": "2", "x": 0, "y": 0, "w": 1, "h": 1, "rotation": None}]
    )
    assert (pad.rotation, pad.shape) == (0.0, "")


def test_pad_hash_is_golden_and_order_independent():
    """The hash is a persistent key: a fixed input must always give this value."""
    pads = [Pad("1", 0.0, 0.0, 1.0, 1.0), Pad("2", 1.0, 0.0, 1.0, 1.0, 0.0, "K")]
    assert pad_hash(pads) == "8198e4768b124619"
    assert pad_hash(list(reversed(pads))) == pad_hash(pads)
    assert len(pad_hash(pads)) == 16


def test_pad_hash_ignores_pin_function_and_sign_of_zero_but_not_size_or_turn():
    """Wiring and -0.0 do not change the hash; moving, resizing or turning a pad does."""
    base = [Pad("1", 0.0, 0.0, 2.0, 1.0), Pad("2", 1.0, 0.0, 1.0, 1.0)]
    assert pad_hash(
        [Pad("1", -0.0, 0.0, 2, 1, 0, "K"), Pad("2", 1.0, -0.0, 1.0, 1.0)]
    ) == pad_hash(base)
    assert pad_hash(
        [Pad("1", 0.0, 0.0, 2.0, 1.0), Pad("2", 1.5, 0.0, 1.0, 1.0)]
    ) != pad_hash(base)
    assert pad_hash(
        [Pad("1", 0.0, 0.0, 2.0, 1.0, 90.0), Pad("2", 1.0, 0.0, 1.0, 1.0)]
    ) != pad_hash(base)
    assert pad_hash(
        [Pad("1", 0.0004, 0.0, 2.0, 1.0), Pad("2", 1.0, 0.0, 1.0, 1.0)]
    ) == pad_hash(base)
    assert pad_hash(
        [Pad("1", 0.0006, 0.0, 2.0, 1.0), Pad("2", 1.0, 0.0, 1.0, 1.0)]
    ) != pad_hash(base)


def test_named_pads_keeps_every_named_pad():
    """Unnamed pads (NPTH holes, paste-only copper) are dropped; lettered names and EP stay."""
    pads = [
        Pad("1", 0, 0, 1, 1),
        Pad("", 0, 0, 2, 2),
        Pad("A1", 0, 0, 1, 1),
        Pad("EP", 0, 0, 1, 1),
    ]
    assert [p.number for p in named_pads(pads)] == ["1", "A1", "EP"]


def test_mirror_y_negates_y_only():
    """Bottom-side un-mirroring flips Y and leaves X, size, function and shape alone."""
    (pad,) = mirror_y([Pad("1", 1.0, 2.0, 3.0, 4.0, 90.0, "K", "custom")])
    assert pad == Pad("1", 1.0, -2.0, 3.0, 4.0, 90.0, "K", "custom")


def test_pad_geom_swaps_width_and_height_for_turned_pads():
    """A pad turned 90 or 270 degrees presents its height along X; other angles give the bounding box."""
    assert pad_geom(Pad("1", 0, 0, 2.0, 1.0, 90.0)) == (0, 0, 1.0, 2.0)
    assert pad_geom(Pad("1", 0, 0, 2.0, 1.0, -90.0)) == (0, 0, 1.0, 2.0)
    assert pad_geom(Pad("2", 1, 0, 2.0, 1.0, 180.0)) == (1, 0, 2.0, 1.0)
    _, _, width, height = pad_geom(Pad("3", 0, 0, 2.0, 1.0, 45.0))
    assert width == pytest.approx(height) and width == pytest.approx(3 / 2**0.5)


def test_centroid_is_the_mean_and_rejects_no_pads():
    """The centroid is the plain mean; an empty list is a caller error."""
    pads = [Pad("1", -1.0, 0.0, 1, 1), Pad("2", 1.0, 2.0, 1, 1)]
    assert centroid(pads) == pytest.approx((0.0, 1.0))
    with pytest.raises(ValueError):
        centroid([])


def test_the_pad_box_centre_is_the_union_of_the_turned_pad_boxes():
    """The centre uses each pad's box after its own rotation."""
    pads = [
        Pad("1", -1.0, 0.0, 0.4, 1.0),
        Pad("2", 1.0, 0.0, 1.0, 0.4, rotation=90.0),
    ]

    # Pad 2 is 0.4 wide once its own 90 degrees are applied, so the box is -1.2 .. 1.2.
    assert pad_box_centre(pads) == (0.0, 0.0)
    assert pad_box_centre([]) is None


def test_an_unnumbered_soldered_pad_counts_in_the_pad_box_centre():
    """A soldered pad without a number widens the box, as it does in ``get_position``."""
    pads = [
        Pad("1", -1.0, 0.0, 0.4, 1.0),
        Pad("2", 1.0, 0.0, 1.0, 0.4, rotation=90.0),
        Pad("", 2.0, 0.0, 0.4, 0.4),
    ]

    # The unnumbered pad reaches 2.2, so the box is -1.2 .. 2.2 across and -0.5 .. 0.5 down.
    assert pad_box_centre(pads) == pytest.approx((0.5, 0.0))


def test_pad_pitch_is_the_nearest_two_terminals():
    """Pads sharing a number (a DPAK's tab and its stub) are one terminal and never set the pitch."""
    dpak = [
        Pad("1", -2.28, 4.0, 1.0, 1.6),
        Pad("3", 2.28, 4.0, 1.0, 1.6),
        Pad("2", 0.0, -1.0, 6.0, 6.0),
        Pad("2", 0.0, 4.0, 1.0, 1.6),
    ]
    # The two pad 2s are 5 mm apart and the stub sits 2.28 mm from pads 1 and 3.
    assert pad_pitch(dpak) == pytest.approx(2.28)
    assert (
        pad_pitch([Pad("2", 0.0, -1.0, 6.0, 6.0), Pad("2", 0.0, 4.0, 1.0, 1.6)]) is None
    )
    assert (
        pad_pitch([Pad("1", 0.0, 0.0, 1.0, 1.0), Pad("", 1.0, 0.0, 1.0, 1.0)]) is None
    )


def test_normalize_angle_is_half_open():
    """Angles land in (-180, 180]: 180 stays, -180 becomes 180."""
    assert [normalize_angle(a) for a in (0.0, 180.0, -180.0, 190.0, -190.0, 720.0)] == [
        0.0,
        180.0,
        180.0,
        -170.0,
        170.0,
        0.0,
    ]


def test_snap_to_90_rounds_to_the_nearest_quarter_turn():
    """The nearest multiple of 90 as 0, 90, 180 or 270; an exact 45 rounds to the even multiple."""
    assert [snap_to_90(a) for a in (44.0, 46.0, -92.0, 359.0, 45.0, 135.0)] == [
        0,
        90,
        270,
        0,
        0,
        180,
    ]


def test_ccw_correction_snaps_and_wraps():
    """The CPL correction is the snapped math-frame angle, modulo 360, with its sign kept."""
    assert [ccw_correction(a) for a in (0, 90, 180, 270)] == [0, 90, 180, 270]
    assert ccw_correction(-92.0) == 270
    assert ccw_correction(268.0) == 270
    assert ccw_correction(44.0) == 0
    assert ccw_correction(-270.0) == 90
    assert ccw_correction(450.0) == 90


def test_naming_rule_to_cpl_negates():
    """The naming-rule table's 90 is the CPL's 270; 0 and 180 are their own negatives."""
    assert [naming_rule_to_cpl(a) for a in (0, 90, 180, 270)] == [0, 270, 180, 90]


def test_mirror_box_flips_y():
    """A bottom-side box mirrors across the X axis and keeps its corners ordered."""
    assert mirror_box((1.0, 2.0, 3.0, 4.0)) == (1.0, -4.0, 3.0, -2.0)
