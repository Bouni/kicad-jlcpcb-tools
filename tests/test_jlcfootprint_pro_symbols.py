"""Tests for the Pro-host parsers: the batch device lookup and symbol documents."""

import json

import pytest

from jlcfootprint.easyeda_parse import (
    DeviceHit,
    SymbolPin,
    assemble_record,
    parse_component_response,
    parse_devices_response,
    parse_pro_pins,
    parse_puuid_response,
    parse_symbol_response,
    pro_doctype,
    pro_pin_records,
    pro_shape_lines,
)

from .jlcfootprint_support import (
    FIXTURES,
    PRO_FIXTURES,
    recorded_devices,
    recorded_document,
)
from .test_jlcfootprint_easyeda_parse import UNHASHABLE_CODE_REFUSALS, WITHOUT_SUCCESS

SYMBOLS = {
    # C2132, C81598 and C88744: recorded from the Pro host by symbol uuid.
    "C2132": "c7fc7a92fb9f4171a873988b8332913e",
    "C81598": "3861a833159347518b9253018065656d",
    "C88744": "7c5fd9c52c42478ea0b2dc7dd589563c",
}


def _classic_pins(lcsc):
    body = json.loads((FIXTURES / f"{lcsc}.json").read_text(encoding="utf-8"))
    return sorted(
        (p.number, p.label) for p in parse_component_response(body, lcsc).symbol_pins
    )


def test_pro_symbols_carry_the_same_pins_as_the_classic_responses():
    """Numbers and labels read from PIN and ATTR records equal the classic symbol's pins."""
    for lcsc, uuid in SYMBOLS.items():
        record = parse_symbol_response(recorded_document("symbol", uuid), uuid)
        assert record.status == "ok", (lcsc, record.error)
        assert record.uuid == uuid
        assert record.skipped_pins == 0
        assert record.shapes[0] == '["DOCTYPE","SYMBOL","1.1"]'
        assert sorted((p.number, p.label) for p in record.pins) == _classic_pins(lcsc)
    assert (
        parse_symbol_response(recorded_document("symbol", SYMBOLS["C2132"]), "x").title
        == "2SA812_C2132"
    )


def test_parse_pro_pins_reads_attributes_by_pin_id_in_record_order():
    """A pin's NAME and NUMBER come from its own ATTR records; a pin without a number is skipped."""
    lines = [
        '["DOCTYPE","SYMBOL","1.1"]',
        '["PIN","e3",1,null,10,20,10,270,null,0,0,1]',
        '["ATTR","e4","e3","NAME","K",false,false,13,5,90,"st4",0]',
        '["ATTR","e5","e3","NUMBER"," 1 ",false,false,9,14,90,"st3",0]',
        '["PIN","e7",1,null,-10,0,10,0,null,0,0,1]',
        '["ATTR","e9","e7","NUMBER","2",false,false,-6,0,0,"st4",0]',
        '["PIN","e11",1,null,0,0,10,0,null,0,0,1]',
        '["ATTR","e12","e11","NAME","NC",false,false,0,0,0,"st3",0]',
        '["ATTR","e13","e2","NUMBER","9",false,false,0,0,0,"st3",0]',
        "not json",
        '["ATTR"]',
    ]
    pins, skipped = parse_pro_pins(lines)
    assert pins == [SymbolPin(number="1", label="K"), SymbolPin(number="2", label="")]
    assert skipped == 1
    assert pro_doctype(lines) == "SYMBOL"
    assert pro_doctype(lines[1:]) == ""


def test_symbol_response_status_rules():
    """Not-found is none, a footprint body or a classic body is an error, malformed input never raises."""
    assert (
        parse_symbol_response(
            {"success": False, "code": 404, "message": "gone"}, "u"
        ).status
        == "none"
    )
    assert (
        parse_symbol_response(
            {"success": False, "code": 500, "message": "boom"}, "u"
        ).status
        == "error"
    )
    assert (
        parse_symbol_response({"success": True, "result": None}, "u").status == "none"
    )
    footprint = {
        "success": True,
        "result": {
            "dataStr": '["DOCTYPE","FOOTPRINT","1.8"]\n["PAD","e1",0,"",1,"1",0,0,0,null,["RECT",10,10,0]]'
        },
    }
    record = parse_symbol_response(footprint, "u")
    assert record.status == "error" and "names a footprint" in record.error
    classic = {"success": True, "result": {"dataStr": {"head": {}, "shape": []}}}
    assert "Pro text" in parse_symbol_response(classic, "u").error
    empty = {
        "success": True,
        "result": {"dataStr": '["DOCTYPE","SYMBOL","1.1"]\n["POLY","e1",[]]'},
    }
    assert "no readable pins" in parse_symbol_response(empty, "u").error
    assert parse_symbol_response("nonsense", "u").status == "error"
    assert (
        parse_symbol_response({"success": True, "result": [1, 2]}, "u").status
        == "error"
    )
    assert pro_shape_lines("\n\n") == []


@pytest.mark.parametrize("body", UNHASHABLE_CODE_REFUSALS)
def test_a_symbol_refusal_whose_code_is_a_list_or_a_dict_is_an_error(body):
    """The parser never raises: an unhashable code is an error with the code kept."""
    record = parse_symbol_response(body, "u")
    assert (record.status, record.error) == ("error", f"code {body['code']}")


@pytest.mark.parametrize("body", WITHOUT_SUCCESS)
def test_a_symbol_body_without_a_boolean_success_flag_is_an_error(body):
    """Neither an answer nor EasyEDA's refusal: it is asked again, never taken as 'none'."""
    assert parse_symbol_response(body, "u").status == "error"


def test_devices_response_classifies_hits_and_misses():
    """Every asked code is a hit with both uuids or a miss; a record without a footprint is a miss."""
    codes, body = recorded_devices("misses")
    result = parse_devices_response(body, codes)
    assert result.error == ""
    assert len(result.hits) == 25
    assert set(result.hits) | set(result.missing) == set(codes)
    assert all(
        hit.symbol_uuid and hit.puuid and hit.package_name
        for hit in result.hits.values()
    )
    assert result.hits["C494818"].package_name == "CAP-SMD_BD4.0-L4.3-W4.3-FD"
    assert result.missing[:2] == ["C1369890", "C1744286"]

    listed = {
        "success": True,
        "result": {
            "lists": [
                {
                    "product_code": "C1",
                    "footprint": {"uuid": "f1", "display_title": "SOT-23"},
                    "attributes": {"Symbol": "s1"},
                },
                {
                    "product_code": "C2",
                    "attributes": {"Symbol": "s2", "Footprint": "f2"},
                },
                {"product_code": "C3", "attributes": {"Symbol": "s3"}},
            ]
        },
    }
    result = parse_devices_response(listed, ["C1", "C2", "C3", "C4"])
    assert result.hits["C1"].puuid == "f1" and result.hits["C1"].symbol_uuid == "s1"
    assert result.hits["C2"].puuid == "f2" and result.hits["C2"].package_name == ""
    assert result.missing == ["C3", "C4"]


def test_devices_response_failures_classify_nothing():
    """A refused, malformed or unexpected answer carries an error and no hits or misses.

    An answer that is not the expected list of device records says nothing about any
    code, so every code is asked again rather than taken as a miss.
    """
    record = {"product_code": "C1", "footprint": {"uuid": "f1"}}
    for body in (
        "nope",
        {"success": False, "code": 401, "message": "denied"},
        {"success": False, "code": ["401"], "message": "denied"},
        {"code": 500, "result": [record]},
        {"success": "true", "result": [record]},
        {"success": True, "result": None},
        {"success": True, "result": {}},
        {"success": True, "result": {"lists": 5}},
        {"success": True, "result": [record, "garbage"]},
    ):
        result = parse_devices_response(body, ["C1"])
        assert result.error and result.hits == {} and result.missing == []
    result = parse_devices_response({"success": True, "result": []}, ["C1"])
    assert result.error == "" and result.missing == ["C1"]


@pytest.mark.parametrize(
    "batch", ["corner_case", "pin_functions_and_marks", "pin_functions_and_marks_c8"]
)
def test_the_gate_boards_recordings_are_complete_and_names_agree(batch):
    """Every hit's footprint and symbol is recorded, and the batch's package name equals the document's."""
    codes, body = recorded_devices(batch)
    result = parse_devices_response(body, codes)
    assert result.missing == [] and len(result.hits) == len(codes)
    for hit in result.hits.values():
        footprint = parse_puuid_response(
            recorded_document("footprint", hit.puuid), hit.puuid
        )
        assert footprint.status == "ok", hit
        assert footprint.package_name == hit.package_name, hit
        if hit.symbol_uuid:
            symbol = parse_symbol_response(
                recorded_document("symbol", hit.symbol_uuid), hit.symbol_uuid
            )
            assert symbol.status == "ok", hit


def test_the_recorded_batches_are_the_gate_boards_and_the_misses():
    """The batch answers are the two gate boards' (the second in two parts) and one of misses."""
    assert sorted(p.name for p in PRO_FIXTURES.glob("devices_*.json")) == [
        "devices_corner_case.json",
        "devices_misses.json",
        "devices_pin_functions_and_marks.json",
        "devices_pin_functions_and_marks_c8.json",
    ]


def test_the_joined_record_keeps_the_footprint_s_error_and_unreadable_shapes():
    """A refused footprint's reason and a footprint's skipped pads reach the record; a failed symbol leaves no pins."""
    hit = DeviceHit("C1", "s1", "f1", "0603")
    refused = parse_puuid_response(
        {"success": False, "code": 500, "message": "busy"}, "f1"
    )
    record = assemble_record("C1", hit, refused, None)
    assert (record.status, record.error) == ("error", "code 500: busy")
    one_unreadable = parse_puuid_response(
        {
            "success": True,
            "result": {
                "dataStr": '["DOCTYPE","FOOTPRINT","1.8"]\n'
                '["PAD","e1",0,"",1,"1",0,0,0,null,["RECT",1,1,0]]\n'
                '["PAD","e2",0,"",1,"2",0,0,"x",null,["RECT",1,1,0]]'
            },
        },
        "f1",
    )
    symbol = parse_symbol_response({"success": False, "code": 500}, "s1")
    record = assemble_record("C1", hit, one_unreadable, symbol)
    assert (record.status, record.skipped_shapes) == ("ok", 1)
    assert (record.error, record.symbol_pins) == ("", [])


def test_polygon_pads_take_their_bounding_box():
    """The USB-C's four merged pads are POLY outlines; they count with their box, unrotated."""
    codes, body = recorded_devices("corner_case")
    hit = parse_devices_response(body, codes).hits["C165948"]
    footprint = parse_puuid_response(
        recorded_document("footprint", hit.puuid), hit.puuid
    )
    assert footprint.status == "ok" and footprint.skipped_shapes == 0
    by_number = {pad["number"]: pad for pad in footprint.pads}
    assert len(by_number) == 16
    merged = by_number["A1B12"]
    assert (round(merged["w"], 2), round(merged["h"], 2), merged["rotation"]) == (
        2.36,
        5.12,
        0.0,
    )
    assert merged["shape"] == "POLY"


def test_pro_pin_numbers_lose_their_leading_zeros_like_classic_ones():
    """A Pro symbol that numbers a pin "01" matches the footprint's pad 1."""
    lines = [
        '["DOCTYPE","SYMBOL","1.1"]',
        '["PIN","e3",1,null,10,20,10,270,null,0,0,1]',
        '["ATTR","e4","e3","NAME","K",false,false,13,5,90,"st4",0]',
        '["ATTR","e5","e3","NUMBER","01",false,false,9,14,90,"st3",0]',
        '["PIN","e7",1,null,-10,0,10,0,null,0,0,1]',
        '["ATTR","e9","e7","NUMBER","A02",false,false,-6,0,0,"st4",0]',
    ]
    pins, skipped = parse_pro_pins(lines)
    assert (skipped, [pin.number for pin in pins]) == (0, ["1", "A02"])
    assert pro_pin_records(lines)[0] == ("1", "K", 10.0, 20.0)
