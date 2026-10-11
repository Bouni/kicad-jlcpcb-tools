"""Tests for jlcfootprint.easyeda_parse against recorded per-LCSC responses."""

import json
from pathlib import Path

import pytest

from jlcfootprint.easyeda_parse import (
    SymbolPin,
    parse_component_response,
    parse_footprint_pads,
    parse_puuid_response,
    parse_symbol_pins,
    parse_symbol_response,
)
from jlcfootprint.geometry import easyeda_pads_to_mm, named_pads
from jlcfootprint.records import pin1_polarity

FIXTURES = Path(__file__).parent / "fixtures" / "jlcfootprint" / "easyeda"


def load(lcsc):
    """Parse one recorded response."""
    return parse_component_response(
        json.loads((FIXTURES / f"{lcsc}.json").read_text()), lcsc
    )


def test_c14663_is_a_two_pad_0603_capacitor():
    """The symbol is per part and the footprint per puuid; both come out of one response."""
    record = load("C14663")
    assert record.status == "ok"
    assert record.puuid == "f8151fdc728a41ebbc304e11d39e5437"
    assert record.package_name == "C0603"
    assert record.footprint_source == "component"
    assert len(record.pads) == 2
    assert {pad["number"] for pad in record.pads} == {"1", "2"}
    assert len(record.symbol_pins) == 2
    assert record.symbol_uuid and record.symbol_uuid != record.puuid


def test_two_parts_sharing_a_puuid_have_different_symbols():
    """C14663 and C15849 both use C0603 but carry their own symbol uuid."""
    a, b = load("C14663"), load("C15849")
    assert a.puuid == b.puuid
    assert a.symbol_uuid != b.symbol_uuid


def test_c2132_sot23_pads_match_the_name():
    """SOT-23-3 -BR: pads 1 and 2 on one side, 1.90 mm apart (the name's P1.90), pad 3 opposite."""
    record = load("C2132")
    assert record.package_name == "SOT-23-3_L2.9-W1.6-P1.90-LS2.8-BR"
    assert record.puuid == "b3b82869fa924bae820e3a6cfb44d689"
    pads = {pad["number"]: pad for pad in record.pads}
    assert set(pads) == {"1", "2", "3"}
    assert pads["1"]["x"] == pytest.approx(pads["2"]["x"])
    assert abs(pads["1"]["y"] - pads["2"]["y"]) * 0.254 == pytest.approx(1.90, abs=0.01)
    assert pads["3"]["x"] < pads["1"]["x"]
    assert pads["1"]["w"] * 0.254 == pytest.approx(1.07, abs=0.01)


def test_c2040_lqfn56_has_all_signal_pads():
    """A 56-pin QFN with an exposed pad parses every numbered pad."""
    record = load("C2040")
    assert record.package_name == "LQFN-56_L7.0-W7.0-P0.4-EP"
    numbered = [pad for pad in record.pads if pad["number"].isdigit()]
    assert len(numbered) >= 56


def test_c72043_led_symbol_pins_are_read():
    """The LED record carries two symbol pins and a footprint named with -RD."""
    record = load("C72043")
    assert record.package_name == "LED0603-RD"
    assert len(record.symbol_pins) == 2
    assert {pin.number for pin in record.symbol_pins} == {"1", "2"}


def test_parse_symbol_pins_reads_number_and_label():
    """The pin number is field 3 of section 0 and the label field 4 of section 3."""
    shape = (
        "P~show~0~1~25~0~0~gge27~0^^25~0^^M 25 0 h -10~#880000^^0~12~3~0~A~end~~~#0000FF"
        "^^0~18~-1~0~1~start~~~#0000FF^^0~18~0^^0~M 15 -3 L 12 0 L 15 3"
    )
    pins, skipped = parse_symbol_pins(
        [shape, "PL~-2 -5~#880000~1~0~none~gge41~0", "P~show"]
    )
    assert (pins, skipped) == ([SymbolPin("1", "A")], 1)


def test_a_classic_pin_without_a_number_is_skipped_and_counted_as_pro_ones_are():
    """A pin that cannot be matched to a pad is left out; the numbered pins are unaffected."""
    numbered = "P~show~0~1~25~0~0~gge27~0^^25~0^^M 25 0 h -10~#880000^^0~12~3~0~A~end~~~#0000FF"
    unnumbered = "P~show~0~~25~10~0~gge28~0^^25~10^^M 25 10 h -10~#880000^^0~12~13~0~~end~~~#0000FF"
    pins, skipped = parse_symbol_pins([numbered, unnumbered])
    assert ([(pin.number, pin.label) for pin in pins], skipped) == ([("1", "A")], 1)


@pytest.mark.parametrize(
    ("pins", "expected"),
    [
        ([SymbolPin("1", "K"), SymbolPin("2", "A")], "K"),
        ([SymbolPin("1", "A"), SymbolPin("2", "K")], "A"),
        ([SymbolPin("1", "+"), SymbolPin("2", "-")], "A"),
        ([SymbolPin("1", "1"), SymbolPin("2", "2")], None),
        ([SymbolPin("2", "K")], None),
    ],
)
def test_pin1_polarity(pins, expected):
    """Pin-1 polarity follows the label sets; unlabelled pins give None."""
    assert pin1_polarity(pins) == expected


def test_parse_footprint_pads_subtracts_origin_and_reads_rotation():
    """PAD fields: shape, x, y, w, h, layer, net, number, hole, points, rotation."""
    shape = "PAD~RECT~4004.863~3003.74~4.2126~2.3622~1~~1~0~pts~90~gge1~0~~Y~0~0~0.4~4004.863,3003.74"
    (pad,), skipped = parse_footprint_pads(
        [shape, "TRACK~1~3~~x~gge2~0", "PAD~RECT~1"], 4000.0, 3000.0
    )
    assert skipped == 1
    assert pad["number"] == "1"
    assert pad["x"] == pytest.approx(4.863)
    assert pad["y"] == pytest.approx(3.74)
    assert (pad["w"], pad["h"]) == (4.2126, 2.3622)
    assert pad["rotation"] == 90.0
    assert pad["shape"] == "RECT"
    assert (pad["layer"], pad["hole"]) == ("1", 0.0)


def test_none_and_error_responses():
    """A null result is 'none' (checkerboard part); junk is 'error'."""
    assert (
        parse_component_response({"success": True, "result": None}, "C1").status
        == "none"
    )
    assert (
        parse_component_response({"success": False, "code": 404}, "C1").status == "none"
    )
    assert parse_component_response("not json", "C1").status == "error"
    assert (
        parse_component_response(
            {"success": True, "result": {"dataStr": {}}}, "C1"
        ).status
        == "error"
    )


def test_named_pads_after_conversion_keep_every_named_pad():
    """Conversion keeps every pad, exposed pad included, and all of them carry names."""
    pads = easyeda_pads_to_mm(load("C2040").pads)
    assert len(named_pads(pads)) == len(pads) >= 57


@pytest.mark.parametrize(
    "body",
    [
        {"success": True, "result": {"dataStr": []}},
        {"success": True, "result": {"dataStr": 5}},
        {"success": True, "result": {"dataStr": "[1, 2]"}},
        {"success": True, "result": {"dataStr": "not json"}},
        {"success": True, "result": {"dataStr": {"head": "x"}}},
        {"success": True, "result": {"dataStr": {"head": {"c_para": []}, "shape": 7}}},
        {"success": True, "result": {"packageDetail": "x"}},
        {"success": True, "result": {"packageDetail": {"dataStr": {"head": []}}}},
        {
            "success": True,
            "result": {
                "dataStr": {"head": {"puuid": "p"}},
                "packageDetail": {"dataStr": {"head": {"x": "?"}}},
            },
        },
    ],
)
def test_malformed_bodies_never_raise(body):
    """Any shape of junk is an error record with its reason, never an exception."""
    record = parse_component_response(body, "C1")
    assert record.status == "error" and record.error


# A footprint whose only shape is a silkscreen track: no pad to read.
_PADLESS_FOOTPRINT = {"head": {"x": 0, "y": 0}, "shape": ["TRACK~1~3~~0 0 10 0~gge1~0"]}


def test_a_classic_answer_without_a_readable_footprint_is_an_error():
    """It is asked again, as the per-uuid parser asks again for a footprint without pads."""
    padless = {
        "success": True,
        "result": {
            "dataStr": {"head": {}},
            "packageDetail": {"uuid": "p", "dataStr": _PADLESS_FOOTPRINT},
        },
    }
    record = parse_component_response(padless, "C1")
    assert (record.status, record.error, record.pads) == (
        "error",
        "packageDetail without readable pads",
        [],
    )
    as_document = {"success": True, "result": {"dataStr": _PADLESS_FOOTPRINT}}
    assert parse_puuid_response(as_document, "p").status == "error"
    without = {"success": True, "result": {"dataStr": {"head": {"puuid": "p"}}}}
    record = parse_component_response(without, "C1")
    assert (record.status, record.error) == (
        "error",
        "no packageDetail in the response",
    )


# EasyEDA's answers for a part (C999999999) and a footprint uuid (all zeros) it does
# not have, recorded from the classic per-part endpoint and both per-uuid endpoints.
NOT_FOUND = Path(__file__).parent / "fixtures" / "jlcfootprint" / "easyeda_not_found"


@pytest.mark.parametrize(
    ("name", "parse"),
    [
        ("classic_component", parse_component_response),
        ("classic_document", parse_puuid_response),
        ("pro_document", parse_puuid_response),
    ],
)
def test_easyeda_s_recorded_not_found_answer_reads_as_none(name, parse):
    """HTTP 200 with success false and code 404: EasyEDA has nothing for it, so none."""
    body = json.loads((NOT_FOUND / f"{name}.json").read_text(encoding="utf-8"))
    assert body == {"success": False, "code": 404, "message": "Component not found"}
    assert parse(body, "u").status == "none"


@pytest.mark.parametrize(
    "head", [{}, {"x": "4000.0a", "y": "3000"}], ids=["missing", "unreadable"]
)
def test_a_classic_footprint_without_its_origin_is_an_error(head):
    """Its pads would sit about a metre off, so it is asked again rather than taken as read."""
    body = json.loads((FIXTURES / "C2132.json").read_text())
    body["result"]["packageDetail"]["dataStr"]["head"] = head
    record = parse_component_response(body, "C2132")
    assert (record.status, record.pads) == ("error", [])
    assert "origin" in record.error
    footprint = dict(body["result"]["packageDetail"]["dataStr"])
    document = {"success": True, "result": {"dataStr": footprint}}
    assert parse_puuid_response(document, "u").status == "error"


def test_error_codes_are_errors_not_none():
    """A refused or failed call must be retried, not parked as a checkerboard part."""
    assert (
        parse_component_response({"success": False, "code": 403}, "C1").status
        == "error"
    )
    failed = parse_component_response(
        {"success": False, "code": 500, "message": "boom"}, "C1"
    )
    assert (failed.status, failed.error) == ("error", "code 500: boom")
    # Without EasyEDA's not-found code a refusal is not EasyEDA saying it has nothing.
    assert parse_component_response({"success": False}, "C1").status == "error"
    assert (
        parse_component_response({"success": False, "code": 0}, "C1").status == "error"
    )
    assert (
        parse_component_response({"success": False, "code": 404}, "C1").status == "none"
    )
    assert (
        parse_component_response({"success": True, "result": None}, "C1").status
        == "none"
    )


# Bodies without a boolean success flag: none at all, an empty object, a string, a number.
WITHOUT_SUCCESS = [
    {"code": 500, "message": "busy"},
    {},
    {"success": "false", "code": 500},
    {"success": 1, "result": {"dataStr": {}}},
]


@pytest.mark.parametrize("body", WITHOUT_SUCCESS)
def test_a_body_without_a_boolean_success_flag_is_an_error(body):
    """Neither an answer nor EasyEDA's refusal: it is asked again, never taken as 'none'."""
    record = parse_component_response(body, "C1")
    assert record.status == "error"
    assert record.error.startswith("response has no boolean success flag")


@pytest.mark.parametrize(
    "parse", [parse_component_response, parse_puuid_response, parse_symbol_response]
)
@pytest.mark.parametrize(
    "body, status",
    [
        ({"success": True, "result": None}, "none"),
        # EasyEDA's own answer for a part or document it does not have.
        ({"success": False, "code": 404, "message": "Component not found"}, "none"),
        ({"success": False}, "error"),
        ({"success": False, "code": 0}, "error"),
        ({"success": False, "code": 500, "message": "busy"}, "error"),
        ({"code": 500}, "error"),
        ({}, "error"),
        ({"success": "false", "code": 500}, "error"),
    ],
)
def test_the_success_flag_decides_answer_refusal_or_unreadable(parse, body, status):
    """True is an answer, False is EasyEDA's refusal (its 404 is 'none'), anything else is an error."""
    assert parse(body, "u").status == status


# Refusals whose code is a list or a dict, as a fuzz of the parsers produced them.
UNHASHABLE_CODE_REFUSALS = [
    {
        "code": {"result": None, "x": "x", "lists": -1, "uuid": -1, "shape": -1},
        "product_code": [[[], '["PIN","p1"]'], 2.5],
        "success": False,
    },
    {"success": False, "code": [{"dataStr": False, "lists": {"success": False}}]},
    {"success": False, "code": [404]},
]


@pytest.mark.parametrize("body", UNHASHABLE_CODE_REFUSALS)
def test_a_refusal_whose_code_is_a_list_or_a_dict_is_an_error(body):
    """The parser never raises: an unhashable code is an error with the code kept."""
    record = parse_component_response(body, "C1")
    assert (record.status, record.error) == ("error", f"code {body['code']}")


def test_displayed_pin_number_wins_over_the_spice_field():
    """C2132's symbol carries SPICE numbers 2/1/2 but displays 3/1/2, which match its pads."""
    record = load("C2132")
    assert {pin.number for pin in record.symbol_pins} == {"1", "2", "3"}


def test_footprint_origin_is_subtracted():
    """C58069's package origin is far from zero; pads come back footprint-local."""
    record = load("C58069")
    assert record.pads and all(
        abs(pad["x"]) < 40 and abs(pad["y"]) < 40 for pad in record.pads
    )


def test_3d_model_nodes_are_dropped_from_footprint_shapes():
    """SVGNODE entries are 3D artwork, not geometry, and are half the bytes."""
    record = load("C14663")
    assert record.footprint_shapes
    assert not any(shape.startswith("SVGNODE~") for shape in record.footprint_shapes)


def test_string_valued_package_datastr_is_decoded():
    """A JSON-string packageDetail.dataStr is decoded like the symbol's."""
    package = json.dumps(
        {"head": {"x": 10, "y": 20}, "shape": ["PAD~RECT~11~22~3~4~1~~1~0~~0"]}
    )
    body = {
        "success": True,
        "result": {
            "dataStr": {"head": {"puuid": "abc", "uuid": "sym"}, "shape": []},
            "packageDetail": {"uuid": "abc", "title": "X0603", "dataStr": package},
        },
    }
    record = parse_component_response(body, "C1")
    assert (record.status, record.footprint_source, record.package_name) == (
        "ok",
        "component",
        "X0603",
    )
    assert record.pads[0]["x"] == pytest.approx(1.0) and record.pads[0][
        "y"
    ] == pytest.approx(2.0)


def test_pin_records_carry_positions_and_the_classic_origin_travels_with_the_record():
    """Both pin readers give (number, label, x, y); a classic record remembers its head x/y."""
    from jlcfootprint.easyeda_parse import classic_pin_records, pro_pin_records
    from tests.jlcfootprint_support import pro_record, recorded

    classic = recorded("C16133")
    assert classic_pin_records(classic.symbol_shapes) == [
        ("1", "1", 30.0, 0.0),
        ("2", "2", 0.0, 0.0),
    ]
    assert classic.footprint_origin == (4000.0, 3000.0)
    pro = pro_record("C7175")
    assert pro_pin_records(pro.symbol_shapes) == [
        ("1", "1", 15.0, 0.0),
        ("2", "2", -15.0, 0.0),
    ]
    assert pro.footprint_origin == (0.0, 0.0)
    assert pro_pin_records(
        ['["PIN","x",1,null,"a","b"]', '["ATTR","y","x","NUMBER","7"]']
    ) == [("7", "", 0.0, 0.0)]
