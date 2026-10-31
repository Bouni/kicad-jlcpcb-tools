"""Resolve a part from its parsed EasyEDA record.

This module is the join above the package's two halves.  It imports the parsers
(:mod:`jlcfootprint.easyeda_parse` for the record type,
:mod:`jlcfootprint.drawing` for the polarity marks) and the resolver core
(:mod:`jlcfootprint.resolver`), so neither of those has to import the other and
the package's import graph stays a tree with no deferred import in it.
"""

from __future__ import annotations

from typing import Optional

from .drawing import drawing_marks
from .easyeda_parse import ComponentRecord
from .geometry import Pad, easyeda_pads_to_mm
from .resolver import Verdict, resolve


def resolve_record(
    kicad_pads: list[Pad],
    kicad_footprint_name: str,
    record: ComponentRecord,
    kicad_courtyard: Optional[tuple[float, float, float, float]] = None,
) -> Verdict:
    """Resolve one footprint against a part's EasyEDA record.

    Builds the two arguments a ``ComponentRecord`` does not hold ready, the raw
    pads converted to millimetres and the drawings' polarity marks and body box,
    then calls :func:`jlcfootprint.resolver.resolve` with all eight.
    """
    return resolve(
        kicad_pads,
        kicad_footprint_name,
        record.status,
        record.package_name,
        easyeda_pads_to_mm(record.pads),
        record.symbol_pins,
        drawing_marks(
            record.symbol_shapes, record.footprint_shapes, record.footprint_origin
        ),
        kicad_courtyard,
    )
