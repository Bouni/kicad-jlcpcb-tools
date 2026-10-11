"""Angular RMS: how well a solved rotation explains matched pad positions.

For pads paired between a KiCad footprint and its JLC counterpart, compares
each pad's bearing from its side's centroid to the other side's bearing for
the same pad, after removing the transform's rotation.  The result is the
root-mean-square of those per-pad errors, in degrees: how far the pads' layout
departs from a pure rotation of the other side's.  Unlike the solver's
residual, which is a distance, it does not grow with the part's size.  Pads
sitting near their side's centroid (an exposed pad, a DPAK tab) have too
unstable a bearing to judge and are left out of the average.

Python 3.9 compatible; stdlib only.
"""

from __future__ import annotations

import math

from .geometry import normalize_angle


def angular_rms(
    kicad_centres: list[tuple[float, float]],
    jlc_centres: list[tuple[float, float]],
    rotation_deg: float,
) -> float:
    """Return the RMS bearing error, in degrees, between matched pad centres.

    ``kicad_centres`` and ``jlc_centres`` are the same pads' (x, y) centres in
    each footprint's own frame, paired by position: the n-th KiCad centre and
    the n-th JLC centre are one pad.  ``rotation_deg`` is the turn that carries
    the KiCad side onto the JLC side, in the frame both share (the solver's
    angle).  Empty input reports zero error rather than dividing by zero, so a
    footprint with no matching pads has no bearing error to report.
    """
    n = len(kicad_centres)
    if n == 0:
        return 0.0

    # Centroids in each frame (using *original* KiCad centres, not transformed).
    ck_x = sum(p[0] for p in kicad_centres) / n
    ck_y = sum(p[1] for p in kicad_centres) / n
    cj_x = sum(p[0] for p in jlc_centres) / n
    cj_y = sum(p[1] for p in jlc_centres) / n

    radii = [math.hypot(kx - ck_x, ky - ck_y) for (kx, ky) in kicad_centres] + [
        math.hypot(jx - cj_x, jy - cj_y) for (jx, jy) in jlc_centres
    ]
    mean_radius = sum(radii) / len(radii) if radii else 0.0
    # A pad near the centroid (an exposed pad, a DPAK tab) has a bearing that a
    # hundredth of a millimetre can swing by tens of degrees; leave it out.
    near_centre = 0.1 * mean_radius

    angular_errs_sq = []
    for (kx, ky), (jx, jy) in zip(kicad_centres, jlc_centres):
        dk_x = kx - ck_x
        dk_y = ky - ck_y
        dj_x = jx - cj_x
        dj_y = jy - cj_y
        if (
            math.hypot(dk_x, dk_y) <= near_centre
            or math.hypot(dj_x, dj_y) <= near_centre
        ):
            continue
        bearing_kicad = math.degrees(math.atan2(dk_y, dk_x))
        bearing_jlc = math.degrees(math.atan2(dj_y, dj_x))
        err = normalize_angle(bearing_jlc - bearing_kicad - rotation_deg)
        angular_errs_sq.append(err * err)

    if angular_errs_sq:
        angular_rms_deg = math.sqrt(sum(angular_errs_sq) / len(angular_errs_sq))
    else:
        # Every pad sits near a centroid, so no bearing can be judged: no error.
        angular_rms_deg = 0.0

    return angular_rms_deg
