"""Tests for the footprint ``+`` reader's absolute floor: a ``+`` of ordinary size beside a large part's pads."""

import pytest

from jlcfootprint.drawing import (
    footprint_bar_range,
    footprint_positive_pad,
    symbol_positive_pin,
)

from .jlcfootprint_drawing_support import TWO_PADS, plus_at, pro_pad

# A CR123A holder's pads in a Pro drawing (mils): 1,481.88 mil (37.6 mm) apart, so
# 5 % of the spacing is 74.1 mil (1.88 mm).
LARGE_PRO_PADS = [pro_pad("1", -740.94, 0), pro_pad("2", 740.94, 0)]
# The same pads in a classic drawing (10-mil canvas units, origin 4000/3000).
LARGE_CLASSIC_PADS = [
    "PAD~ELLIPSE~3925.906~3000~7.0866~7.0866~11~~1~1.1811~~0~gge1~0~~Y~0~0~0~",
    "PAD~ELLIPSE~4074.094~3000~7.0866~7.0866~11~~2~1.1811~~0~gge2~0~~Y~0~0~0~",
]


def classic_plus_at(x, y, arm):
    """Return a classic silk + of two TRACK strokes, each ``2 * arm`` long, centred on (x, y)."""
    return [
        f"TRACK~0.6~3~~{x - arm} {y} {x + arm} {y}~ggh~0",
        f"TRACK~0.6~3~~{x} {y - arm} {x} {y + arm}~ggv~0",
    ]


def test_a_plus_beside_a_large_part_is_read_below_five_percent_of_the_spacing():
    """A 1.6 mm + beside pads 37.6 mm apart (4.3 % of the spacing) names its pad."""
    assert footprint_positive_pad(LARGE_PRO_PADS + plus_at(13, -740, -77, 31.5)) == "1"
    assert footprint_positive_pad(LARGE_PRO_PADS + plus_at(13, 740, -77, 31.5)) == "2"


def test_the_floor_follows_the_pads_when_they_are_drawn_along_y():
    """Pads 1,481.88 mil apart along Y: the 63-mil + beside either pad is read."""
    pads = [pro_pad("1", 0, 740.94), pro_pad("2", 0, -740.94)]
    assert footprint_positive_pad(pads + plus_at(13, -77, 740, 31.5)) == "1"
    assert footprint_positive_pad(pads + plus_at(13, -77, -740, 31.5)) == "2"


def test_the_symbol_reader_keeps_its_five_percent_bound_on_a_long_pin_span():
    """The floor is the footprint's alone: a symbol's 4 % + is not read, a 6 % one is."""
    pins = [
        '["PIN","a",1,null,-500,0,10,0,null,0,0,1]',
        '["ATTR","a1","a","NUMBER","1"]',
        '["PIN","b",1,null,500,0,10,180,null,0,0,1]',
        '["ATTR","b1","b","NUMBER","2"]',
    ]

    def plus(arm):
        """Return a symbol + of two strokes, each ``2 * arm`` long, beside pin 1."""
        return [
            f'["POLY","h",[{-400 - arm},-40,{-400 + arm},-40],0,"st1",0]',
            f'["POLY","v",[-400,{-40 - arm},-400,{-40 + arm}],0,"st1",0]',
        ]

    assert symbol_positive_pin(pins + plus(30)) == "1"
    assert symbol_positive_pin(pins + plus(20)) is None


def test_a_cross_well_below_the_absolute_floor_is_not_read_on_the_same_pads():
    """A 0.4 mm cross beside the same pads stays too short to be a mark."""
    tiny = plus_at(13, -740, -77, 8.0, thick=3.0)
    assert footprint_positive_pad(LARGE_PRO_PADS + tiny) is None


@pytest.mark.parametrize(
    ("arm", "expected"),
    [(15.0, "1"), (14.5, None)],
    ids=["30_mil_read", "29_mil_not_read"],
)
def test_the_absolute_floor_is_three_quarters_of_a_millimetre(arm, expected):
    """The floor is 0.75 mm (29.5 mil): a 30-mil + is read on a large part, a 29-mil one is not."""
    plus = plus_at(13, -740, -77, arm)
    assert footprint_positive_pad(LARGE_PRO_PADS + plus) == expected


@pytest.mark.parametrize(
    ("arm", "expected"),
    [(5.0, "1"), (3.5, None)],
    ids=["six_percent_read", "four_percent_not_read"],
)
def test_a_small_part_keeps_the_five_percent_floor(arm, expected):
    """On pads 160 mil apart 5 % (8 mil) is below the absolute floor and stays the bound."""
    plus = plus_at(13, -60, 0, arm, thick=2.0)
    assert footprint_positive_pad(TWO_PADS + plus) == expected


@pytest.mark.parametrize(
    ("arm", "expected"),
    [(3.15, "1"), (0.8, None), (1.5, "1"), (1.45, None)],
    ids=["1.6_mm_read", "0.4_mm_not_read", "30_mil_read", "29_mil_not_read"],
)
def test_a_classic_drawing_takes_the_floor_in_its_own_units(arm, expected):
    """Classic canvas units are 10 mil: the same marks on the same pads read the same way."""
    plus = classic_plus_at(3926, 2992.3, arm)
    assert footprint_positive_pad(LARGE_CLASSIC_PADS + plus) == expected


def test_the_bar_range_converts_the_floor_per_format():
    """0.75 mm is 29.53 mil in a Pro drawing and 2.953 canvas units in a classic one."""
    low, high = footprint_bar_range(LARGE_PRO_PADS, 1481.88)
    assert low * 1481.88 == pytest.approx(0.75 / 0.0254)
    assert high == 0.70
    low, high = footprint_bar_range(LARGE_CLASSIC_PADS, 148.188)
    assert low * 148.188 == pytest.approx(0.75 / 0.254)
    assert high == 0.70
    assert footprint_bar_range(TWO_PADS, 160.0) == (0.05, 0.70)
    assert footprint_bar_range(TWO_PADS, 0.0) == (0.05, 0.70)
