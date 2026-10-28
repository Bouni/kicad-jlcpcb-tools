"""Tests for the per-uuid footprint parser: EasyEDA Pro text and the classic dict form."""

import json
from pathlib import Path

import pytest

from jlcfootprint.easyeda_parse import (
    parse_component_response,
    parse_pro_pads,
    parse_puuid_response,
    pro_shape_lines,
)

from .test_jlcfootprint_easyeda_parse import UNHASHABLE_CODE_REFUSALS, WITHOUT_SUCCESS

FIXTURES = Path(__file__).parent / "fixtures" / "jlcfootprint"
UUID = "b3b82869fa924bae820e3a6cfb44d689"  # C2132's classic footprint uuid

# C2132's footprint as the EasyEDA Pro host writes it (mils, Y up): the same pads the
# classic per-LCSC response carries in canvas units with Y down.
PRO_SOT23 = [
    '["PAD","e9",0,"",1,"1",48.625,-37.4,0,null,["RECT",42.126,23.622,0],[],-0.008,-0.002,0,1,0,null,null,null,null,0]',
    '["PAD","e10",0,"",1,"2",48.625,37.4,0,null,["RECT",42.126,23.622,0],[],-0.008,0.002,0,1,0,null,null,null,null,0]',
    '["PAD","e11",0,"",1,"3",-48.625,0,0,null,["RECT",42.126,23.622,0],[],-0.002,0,0,1,0,null,null,null,null,0]',
]
PRO_DIP_PAD = '["PAD","e8",0,"",12,"2",-50,-150,90,["ROUND",35.434,35.434],["ELLIPSE",59.055,59.055],[],0,0,0,1,0,null,null,null,null,0]'


def _classic_pads(lcsc: str) -> list[dict]:
    body = json.loads((FIXTURES / "easyeda" / f"{lcsc}.json").read_text())
    return parse_component_response(body, lcsc).pads


def _rounded(pads: list[dict]) -> list[tuple]:
    return [
        (
            p["number"],
            round(p["x"], 4),
            round(p["y"], 4),
            round(p["w"], 4),
            round(p["h"], 4),
        )
        for p in pads
    ]


def test_pro_pads_convert_to_classic_canvas_units_with_y_down():
    """Mils become tenths of a canvas unit and Y flips, matching the classic response."""
    pads, skipped = parse_pro_pads(PRO_SOT23)
    assert skipped == 0
    assert _rounded(pads) == _rounded(_classic_pads("C2132"))
    assert pads[0]["shape"] == "RECT"
    assert pads[0]["layer"] == "1"
    assert pads[0]["rotation"] == 0.0
    assert pads[0]["hole"] == 0.0


def test_pro_pads_keep_rotation_shape_and_hole_radius():
    """A round THT pad keeps its angle, its shape name and layer code as Pro writes them, and its hole radius."""
    (pad,), skipped = parse_pro_pads([PRO_DIP_PAD])
    assert skipped == 0
    assert (pad["number"], pad["x"], pad["y"]) == ("2", -5.0, 15.0)
    assert pad["shape"] == "ELLIPSE"
    assert pad["rotation"] == 90.0
    assert pad["layer"] == "12"
    assert round(pad["hole"], 4) == round(35.434 / 10 / 2, 4)


@pytest.mark.parametrize(
    "command",
    [["ARC", 90], ["CIRCLE", 5], ["R", 4, 4]],
    ids=["arc", "circle", "unknown"],
)
def test_a_polygon_pad_with_any_command_but_l_is_unreadable(command):
    """An arc, a circle or a command the reader does not know: the outline cannot be boxed."""
    outline = [0, 0, "L", 10, 0, *command, 10, 10, 0, 10]
    record = ["PAD", "e1", 0, "", 1, "1", 0, 0, 0, None, ["POLY", outline], []]
    assert parse_pro_pads([json.dumps(record)]) == ([], 1)
    record[10] = ["POLY", [0, 0, "L", 10, 0, 10, 10, 0, 10]]
    (pad,), skipped = parse_pro_pads([json.dumps(record)])
    assert (skipped, pad["w"], pad["h"]) == (0, 1.0, 1.0)


def test_pro_pads_skip_unreadable_records_and_other_shapes():
    """Non-PAD lines are ignored silently; a PAD record that cannot be read is counted."""
    lines = [
        '["LAYER",1,"TOP","Top Layer",3,"#FF0000",1,"#7F0000",1]',
        '["PAD","e1",0,"",1,"1","x",0,0,null,["RECT",1,1,0]]',
        '["PAD","e2",0,"",1,"2",10,0,0,null,"notalist"]',
        "not json",
        *PRO_SOT23[:1],
    ]
    pads, skipped = parse_pro_pads(lines)
    assert [p["number"] for p in pads] == ["1"]
    assert skipped == 2


def test_a_polygon_pad_drawn_with_an_arc_is_unreadable():
    """Its arc's angle is no coordinate and its end points miss the bulge: the pad is counted as skipped."""
    straight = '["PAD","e1",0,"",1,"1",0,0,0,null,["POLY",[-10,-10,"L",10,-10,10,10,-10,10]],[]]'
    arced = '["PAD","e2",0,"",1,"2",50,0,0,null,["POLY",[40,-10,"L",60,-10,"ARC",180,40,10]],[]]'
    pads, skipped = parse_pro_pads([straight, arced])
    assert [(pad["number"], pad["w"], pad["h"]) for pad in pads] == [("1", 2.0, 2.0)]
    assert skipped == 1


def test_pro_shape_lines_drop_blank_lines():
    """Splitting keeps every record and nothing else."""
    assert pro_shape_lines("\n" + "\n\n".join(PRO_SOT23) + "\n  \n") == PRO_SOT23


def test_recorded_classic_host_response_matches_the_component_footprint():
    """The classic host's per-uuid body yields C2132's own pads and package name."""
    body = json.loads((FIXTURES / "easyeda_uuid" / f"uuid_{UUID}.json").read_text())
    record = parse_puuid_response(body, UUID)
    assert record.status == "ok"
    assert record.package_name == "SOT-23-3_L2.9-W1.6-P1.90-LS2.8-BR"
    assert record.footprint_source == "puuid-endpoint"
    assert _rounded(record.pads) == _rounded(_classic_pads("C2132"))
    assert record.footprint_shapes and all(
        not s.startswith("SVGNODE~") for s in record.footprint_shapes
    )


def test_pro_text_response_parses_with_its_title():
    """The Pro host answers with newline-delimited records; ``display_title`` names the package."""
    body = {
        "success": True,
        "result": {
            "display_title": "SOT-23-3_L2.9-W1.6-P1.90-LS2.8-BR",
            "dataStr": "\n".join(PRO_SOT23),
        },
    }
    record = parse_puuid_response(body, "abc")
    assert record.status == "ok"
    assert record.package_name == "SOT-23-3_L2.9-W1.6-P1.90-LS2.8-BR"
    assert record.footprint_shapes == PRO_SOT23
    assert _rounded(record.pads) == _rounded(_classic_pads("C2132"))


def test_puuid_response_statuses():
    """Not-found is none, other failures are error, and nothing raises on junk."""
    assert (
        parse_puuid_response(
            {"success": False, "code": 404, "message": "x"}, "u"
        ).status
        == "none"
    )
    failed = parse_puuid_response(
        {"success": False, "code": 500, "message": "boom"}, "u"
    )
    assert failed.status == "error"
    assert "500" in failed.error
    assert parse_puuid_response({"success": True, "result": None}, "u").status == "none"
    assert parse_puuid_response({"success": True, "result": []}, "u").status == "none"
    assert parse_puuid_response({"success": True, "result": 5}, "u").status == "error"
    assert parse_puuid_response("junk", "u").status == "error"
    empty = parse_puuid_response({"success": True, "result": {"title": "T"}}, "u")
    assert empty.status == "error"
    assert "no readable pads" in empty.error
    assert empty.package_name == "T"


@pytest.mark.parametrize("body", UNHASHABLE_CODE_REFUSALS)
def test_a_footprint_refusal_whose_code_is_a_list_or_a_dict_is_an_error(body):
    """The parser never raises: an unhashable code is an error with the code kept."""
    record = parse_puuid_response(body, "u")
    assert (record.status, record.error) == ("error", f"code {body['code']}")


@pytest.mark.parametrize("body", WITHOUT_SUCCESS)
def test_a_footprint_body_without_a_boolean_success_flag_is_an_error(body):
    """Neither an answer nor EasyEDA's refusal: it is asked again, never taken as 'none'."""
    assert parse_puuid_response(body, "u").status == "error"


def test_pro_pad_numbers_lose_their_leading_zeros_like_classic_ones():
    """A Pro drawing that writes "01" names the same pad as a classic "1"."""
    padded = PRO_SOT23[0].replace('"",1,"1"', '"",1,"01"')
    pads, skipped = parse_pro_pads([padded])
    assert (skipped, [pad["number"] for pad in pads]) == (0, ["1"])
    zero, _ = parse_pro_pads([PRO_SOT23[0].replace('"",1,"1"', '"",1,"00"')])
    assert [pad["number"] for pad in zero] == ["0"]
    lettered, _ = parse_pro_pads([PRO_SOT23[0].replace('"",1,"1"', '"",1,"0A1"')])
    assert [pad["number"] for pad in lettered] == ["0A1"]
