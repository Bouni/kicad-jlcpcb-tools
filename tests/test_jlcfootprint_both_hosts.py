"""One part, two hosts: the classic and the Pro recordings of a part read the same.

The readers convert EasyEDA Pro's pads to the classic form as they read them, and
the drawing reader reads both formats; every part recorded from both hosts checks
that the two readings agree.  The drawing reader also lists a footprint's pads
itself, to name the pad a ``+`` stands beside, and every recorded footprint
checks that it lists the pads the pad parser reads.
"""

import json

import pytest

from jlcfootprint.drawing import (
    footprint_body_box,
    footprint_pads,
    footprint_positive_pad,
    symbol_positive_pin,
)
from jlcfootprint.easyeda_parse import parse_puuid_response
from jlcfootprint.geometry import easyeda_pads_to_mm
from tests.jlcfootprint_support import (
    FIXTURES,
    PRO_FIXTURES,
    pro_hit,
    pro_record,
    recorded,
)


def _recorded_by_both() -> list[str]:
    """Return the parts recorded from the classic host that the Pro recordings have too."""
    parts = []
    for path in sorted(FIXTURES.glob("C*.json")):
        try:
            pro_hit(path.stem)
        except FileNotFoundError:
            continue
        parts.append(path.stem)
    return parts


def _pads_mm(record) -> list[tuple]:
    return sorted(
        (
            pad.number,
            round(pad.x, 3),
            round(pad.y, 3),
            round(pad.width, 3),
            round(pad.height, 3),
        )
        for pad in easyeda_pads_to_mm(record.pads)
    )


BOTH = _recorded_by_both()


def test_most_classic_recordings_have_a_pro_recording():
    """The comparison below covers the parts recorded from both hosts."""
    assert len(BOTH) == 35


@pytest.mark.parametrize("lcsc", BOTH)
def test_a_part_reads_the_same_from_both_hosts(lcsc):
    """Package name, pads in millimetres, body box and both drawings' + marks agree."""
    classic, pro = recorded(lcsc), pro_record(lcsc)
    assert pro.package_name == classic.package_name
    assert _pads_mm(pro) == _pads_mm(classic)
    classic_box = footprint_body_box(classic.footprint_shapes, classic.footprint_origin)
    pro_box = footprint_body_box(pro.footprint_shapes)
    assert (classic_box is None) == (pro_box is None)
    if pro_box is not None:
        assert pro_box == pytest.approx(classic_box, abs=0.01)
    assert footprint_positive_pad(pro.footprint_shapes) == footprint_positive_pad(
        classic.footprint_shapes
    )
    if lcsc == "C553979":
        # EasyEDA's batch answer names no symbol for this part, so Pro has no drawing.
        assert pro.symbol_shapes == []
    else:
        assert symbol_positive_pin(pro.symbol_shapes) == symbol_positive_pin(
            classic.symbol_shapes
        )


def _recorded_footprints() -> list[tuple[str, list, list[dict]]]:
    """Return ``(name, drawing, parsed pads)`` for every recorded footprint, both formats."""
    footprints = [
        (f"classic-{path.stem}", record.footprint_shapes, record.pads)
        for path in sorted(FIXTURES.glob("C*.json"))
        for record in [recorded(path.stem)]
    ]
    for path in sorted(PRO_FIXTURES.glob("footprint_*.json")):
        uuid = path.stem[len("footprint_") :]
        record = parse_puuid_response(
            json.loads(path.read_text(encoding="utf-8")), uuid
        )
        footprints.append((f"pro-{uuid[:8]}", record.footprint_shapes, record.pads))
    return footprints


@pytest.mark.parametrize(
    ("name", "shapes", "pads"),
    _recorded_footprints(),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_the_drawing_lists_the_pads_the_parser_reads(name, shapes, pads):
    """A + names a pad from the drawing's own list, which must be the parser's set of pads."""
    assert sorted(number for number, _x, _y in footprint_pads(shapes)) == sorted(
        pad["number"] for pad in pads
    )
