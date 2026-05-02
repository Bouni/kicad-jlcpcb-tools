"""Helpers for deriving estimator metadata from KiCad footprints."""

import json

from .footprint_helpers import get_exclude_from_bom, get_exclude_from_pos, get_is_dnp


def get_footprint_pads(footprint):
    """Return an iterable of pads for a footprint across KiCad API variants."""
    pads_fn = getattr(footprint, "Pads", None)
    if callable(pads_fn):
        return pads_fn()

    get_pads_fn = getattr(footprint, "GetPads", None)
    if callable(get_pads_fn):
        return get_pads_fn()

    return []


def count_pad(pad) -> bool:
    """Return True when a pad should count as a solder joint."""
    is_npth_fn = getattr(pad, "IsNPTH", None)
    if callable(is_npth_fn) and is_npth_fn():
        return False

    is_plated_fn = getattr(pad, "IsPlated", None)
    if callable(is_plated_fn) and not is_plated_fn():
        return False

    get_attribute_fn = getattr(pad, "GetAttribute", None)
    if callable(get_attribute_fn):
        attribute_text = str(get_attribute_fn()).upper()
        if "NPTH" in attribute_text or "NONPLATED" in attribute_text:
            return False

    return True


def get_footprint_pad_count(footprint) -> int:
    """Count pads that likely correspond to electrical solder joints."""
    return sum(1 for pad in get_footprint_pads(footprint) if count_pad(pad))


def footprint_has_tht(footprint) -> bool:
    """Heuristically determine if a footprint has through-hole pads."""
    for pad in get_footprint_pads(footprint):
        if not count_pad(pad):
            continue

        has_hole_fn = getattr(pad, "HasHole", None)
        if callable(has_hole_fn) and has_hole_fn():
            return True

        get_drill_size_fn = getattr(pad, "GetDrillSize", None)
        if callable(get_drill_size_fn):
            drill_size = get_drill_size_fn()
            x = getattr(drill_size, "x", 0)
            y = getattr(drill_size, "y", 0)
            if x > 0 or y > 0:
                return True

    return False


def get_assembly_flags(footprint) -> str:
    """Build assembly-related footprint flags for estimator persistence."""
    flags = {
        "exclude_from_bom": bool(get_exclude_from_bom(footprint)),
        "exclude_from_pos": bool(get_exclude_from_pos(footprint)),
        "is_dnp": bool(get_is_dnp(footprint)),
    }
    return json.dumps(flags, sort_keys=True)
