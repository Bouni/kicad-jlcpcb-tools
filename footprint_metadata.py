"""Helpers for deriving estimator metadata from KiCad footprints."""

from collections.abc import Iterable
import json
from typing import Any

from .footprint_helpers import get_exclude_from_bom, get_exclude_from_pos, get_is_dnp

# PAD_ATTRIB in KiCad's padstack.h: PTH=0, SMD=1, CONN=2, NPTH=3.
# pcbnew exposes these enum values as integers, not enum-name strings.
_PAD_ATTRIB_NPTH = 3


def get_footprint_pads(footprint: Any) -> Iterable[Any]:
    """Return an iterable of pads for a footprint across KiCad API variants."""
    pads_fn = getattr(footprint, "Pads", None)
    if callable(pads_fn):
        return pads_fn()

    get_pads_fn = getattr(footprint, "GetPads", None)
    if callable(get_pads_fn):
        return get_pads_fn()

    return []


def count_pad(pad: Any) -> bool:
    """Return True when a pad should count as a solder joint."""
    return pad.GetAttribute() != _PAD_ATTRIB_NPTH


def get_footprint_pad_count(footprint: Any) -> int:
    """Count pads that likely correspond to electrical solder joints."""
    return sum(1 for pad in get_footprint_pads(footprint) if count_pad(pad))


def get_footprint_pad_metadata(footprint: Any) -> tuple[int, bool]:
    """Read electrical solder-joint count and plated-hole presence in one pass."""
    count, has_tht = 0, False
    for pad in get_footprint_pads(footprint):
        if count_pad(pad):
            count += 1
            has_tht = has_tht or pad.HasHole()
    return count, has_tht


def footprint_has_tht(footprint: Any) -> bool:
    """Determine whether a footprint contains a plated through-hole pad."""
    return get_footprint_pad_metadata(footprint)[1]


def get_assembly_flags(footprint: Any) -> str:
    """Build assembly-related footprint flags for estimator persistence."""
    flags = {
        "exclude_from_bom": bool(get_exclude_from_bom(footprint)),
        "exclude_from_pos": bool(get_exclude_from_pos(footprint)),
        "is_dnp": bool(get_is_dnp(footprint)),
    }
    return json.dumps(flags, sort_keys=True)
