"""Pro drawing records built for the drawing tests: pads, filled bars, strokes and a ``+``.

The records are lines of EasyEDA Pro's footprint text, in mils with Y up, as the
drawing reader reads them.
"""

from __future__ import annotations

import json


def pro_pad(number, x, y, w=59.0, h=53.0):
    """Return one Pro PAD record line in mils."""
    return json.dumps(
        [
            "PAD",
            f"p{number}",
            0,
            "",
            1,
            number,
            x,
            y,
            0,
            None,
            ["RECT", w, h, 0],
            [],
            0,
            0,
            0,
            1,
            0,
        ]
    )


def pro_fill(layer, x1, y1, x2, y2, ident="f"):
    """Return one filled rectangle on ``layer`` as a Pro FILL record line."""
    return json.dumps(
        [
            "FILL",
            ident,
            0,
            "",
            layer,
            0.2,
            0,
            [[x1, y1, "L", x1, y2, x2, y2, x2, y1, x1, y1]],
            0,
        ]
    )


def pro_line(layer, x1, y1, x2, y2, ident="l"):
    """Return one stroke on ``layer`` as a Pro POLY record line."""
    return json.dumps(["POLY", ident, 0, "", layer, 6, [x1, y1, "L", x2, y2], 0])


def plus_at(layer, x, y, arm=15.0, thick=8.0, ident="p"):
    """Return a + of two filled bars centred on (x, y)."""
    return [
        pro_fill(layer, x - arm, y - thick / 2, x + arm, y + thick / 2, ident + "h"),
        pro_fill(layer, x - thick / 2, y - arm, x + thick / 2, y + arm, ident + "v"),
    ]


TWO_PADS = [pro_pad("1", -80, 0), pro_pad("2", 80, 0)]
