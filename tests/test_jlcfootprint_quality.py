"""Tests for ``jlcfootprint.quality.angular_rms``: the RMS and its degenerate cases.

All tests are offline (no network access, no KiCad dependency).  Pad centres are
synthetic, laid out like a SOT-23.
"""

from __future__ import annotations

import pytest

from jlcfootprint.geometry import rotate
from jlcfootprint.quality import angular_rms

# SOT-23-3 reference geometry (footprint-local, mm): pads 1 and 2 on the right,
# pad 3 on the left.
_SOT23 = [(1.235, 0.950), (1.235, -0.950), (-1.235, 0.0)]


def test_identical_centres_have_zero_rms():
    """Matching KiCad and JLC centres at the identity rotation have no bearing error."""
    assert angular_rms(_SOT23, _SOT23, 0) == 0.0


def test_a_consistent_rotation_has_zero_rms():
    """Every pad's bearing shifts by exactly the placement's rotation: no error either way."""
    jlc = [rotate(x, y, 90.0) for x, y in _SOT23]
    assert angular_rms(_SOT23, jlc, 90) < 1e-9


def test_a_wrong_rotation_has_a_large_rms():
    """A true 45-degree misalignment forced to a 0-degree placement shows up as a 45-degree error.

    Every pad's bearing from its centroid shifts by the same 45 degrees the JLC
    side was actually rotated by, so the RMS lands exactly there too: a rotation
    that does not explain the pads shows up as a large angular error.
    """
    jlc = [rotate(x, y, 45.0) for x, y in _SOT23]
    assert angular_rms(_SOT23, jlc, 0) == pytest.approx(45.0, abs=1e-9)


def test_empty_pads_report_zero_rms():
    """No matching pads: nothing to average, so the error reports as zero rather than dividing by it."""
    assert angular_rms([], [], 0) == 0.0


def test_a_single_pad_reports_zero_rms():
    """One pad sits on its own centroid, so it has no bearing to compare and reports zero."""
    assert angular_rms([(1.0, 2.0)], [(5.0, -3.0)], 37) == 0.0


def test_coincident_pads_report_zero_rms():
    """Every KiCad pad on the same point has no bearing from the KiCad centroid either."""
    kicad = [(1.0, 1.0), (1.0, 1.0), (1.0, 1.0)]
    jlc = [(4.0, -2.0), (5.0, 9.0), (-3.0, 0.5)]
    assert angular_rms(kicad, jlc, 0) == 0.0


def test_a_pad_on_the_centroid_is_excluded_from_the_average():
    """A pad near the centroid has a bearing a hundredth of a millimetre can swing by tens of degrees.

    Eight pads form a symmetric ring (centroid at the origin); a ninth pad sits
    right at that centre with a slightly different position on each side, the way
    an exposed pad or a DPAK tab would.  Excluding it keeps the RMS small; folding
    its wildly unstable bearing in would not.
    """
    ring = [
        (-0.75, 1.5),
        (-0.25, 1.5),
        (0.25, 1.5),
        (0.75, 1.5),
        (0.75, -1.5),
        (0.25, -1.5),
        (-0.25, -1.5),
        (-0.75, -1.5),
    ]
    kicad = ring + [(0.02, 0.0)]
    jlc = ring + [(0.0, 0.01)]
    assert angular_rms(kicad, jlc, 0) < 0.5
