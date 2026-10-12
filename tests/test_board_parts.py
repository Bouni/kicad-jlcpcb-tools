"""Parts-list facts read from board records, without wx or KiCad."""

from collections.abc import Iterator
import json
from types import SimpleNamespace

import pytest

from .wx_harness import load_siblings


@pytest.fixture
def parts() -> Iterator[SimpleNamespace]:
    """Load the pure module and the fake backend under a scoped name."""
    with load_siblings(
        "_board_parts_tests", ("board_parts", "kicad_backend.fake"), {}
    ) as modules:
        yield SimpleNamespace(
            board_parts=modules["board_parts"], fake=modules["kicad_backend.fake"]
        )


def test_part_facts_read_the_default_state(parts: SimpleNamespace) -> None:
    """Value, footprint name, assignment and flags come from the records."""
    fake = parts.fake
    record = fake.component(
        "U1",
        value="LM358",
        footprint="Package_SO:SOIC-8",
        fields={"LCSC": " c7950 ", "JLCPCB Part #": "C7950"},
        exclude_from_pos=True,
        dnp=True,
        pads=[
            fake.pad("pth"),
            fake.pad("smd"),
            fake.pad("npth"),
        ],
    )

    facts = parts.board_parts.part_facts(record)

    assert facts == {
        "reference": "U1",
        "value": "LM358",
        "footprint": "SOIC-8",
        "lcsc": "C7950",
        "assignment_status": "valid",
        "exclude_from_bom": False,
        "exclude_from_pos": True,
        "is_dnp": True,
        "pad_count": 2,
        "has_tht": True,
        "assembly_flags": json.dumps(
            {"exclude_from_bom": False, "exclude_from_pos": True, "is_dnp": True},
            sort_keys=True,
        ),
    }


def test_two_fields_of_one_name_list_their_assignment_and_kicads_value(
    parts: SimpleNamespace,
) -> None:
    """The assignment reads the later LCSC field; the value is KiCad's own field.

    KiCad's footprint dialog accepts a second field with a name the
    footprint already has. KiCad keeps the later one when the board is
    reopened; its own Value field, which it shows, comes first.
    """
    fake, board_parts = parts.fake, parts.board_parts
    record = fake.component(
        "R1",
        value="10k",
        fields=(("LCSC", "C1"), ("LCSC", "C2"), ("Value", "10k"), ("Value", "22k")),
    )
    snapshot = fake.FakeBackend([fake.component("C1"), record]).snapshot()

    facts = board_parts.part_facts(record)
    assert (facts["lcsc"], facts["value"]) == ("C2", "10k")
    assert board_parts.repeated_field_names(snapshot) == [
        ("R1", "LCSC"),
        ("R1", "Value"),
    ]


@pytest.mark.parametrize(
    "fields, status",
    [
        ({}, "missing"),
        ({"LCSC": ""}, "empty"),
        ({"LCSC": "C1", "JLC": "C2"}, "conflict"),
    ],
)
def test_unresolved_assignments_keep_their_status(
    parts: SimpleNamespace, fields: dict[str, str], status: str
) -> None:
    """A part without a usable number reports why, with an empty LCSC."""
    facts = parts.board_parts.part_facts(parts.fake.component("R1", fields=fields))
    assert (facts["lcsc"], facts["assignment_status"]) == ("", status)


@pytest.mark.parametrize(
    "lib_id, name",
    [("Lib:R_0603", "R_0603"), ("R_0603", "R_0603"), ("A:B:C", "B:C"), ("", "")],
)
def test_library_item_name_is_what_follows_the_first_colon(
    parts: SimpleNamespace, lib_id: str, name: str
) -> None:
    """KiCad's LIB_ID splits its nickname at the first colon."""
    assert parts.board_parts.library_item_name(lib_id) == name


@pytest.mark.parametrize(
    "reference, listed",
    [("R1", True), ("REF**", True), ("_1", True), ("?", False), ("", False)],
)
def test_listed_references_start_with_a_word_character_or_hyphen(
    parts: SimpleNamespace, reference: str, listed: bool
) -> None:
    """The parts list keeps the reference rule it has always used."""
    assert parts.board_parts.is_listed(reference) is listed


def test_lookups_find_the_first_footprint_in_board_order(
    parts: SimpleNamespace,
) -> None:
    """A repeated reference resolves as KiCad's own lookup does; sides follow it."""
    fake = parts.fake
    snapshot = fake.FakeBackend(
        [
            fake.component("R1", uuid="b", side="bottom"),
            fake.component("R1", uuid="a"),
            fake.component("C1", uuid="c"),
        ]
    ).snapshot()

    assert parts.board_parts.find_component(snapshot, "R1").uuid == "b"
    assert parts.board_parts.find_component(snapshot, "R9") is None
    assert parts.board_parts.placement_sides(snapshot, ["R1", "C1", "R9"]) == {
        "R1": "bottom",
        "C1": "top",
    }
