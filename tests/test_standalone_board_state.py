"""Standalone board mutations survive subsequent native-style reads."""

from collections.abc import Iterator
from types import ModuleType

import pytest

from .wx_harness import load_siblings


@pytest.fixture
def modules() -> Iterator[dict[str, ModuleType]]:
    """Exercise production stubs and edit helpers together without a GUI."""
    with load_siblings(
        "_standalone_board_state_tests",
        ("standalone_impl", "footprint_helpers", "board_part_edits"),
        {},
    ) as loaded:
        yield loaded


def test_assignment_and_clear_survive_board_lookup(
    modules: dict[str, ModuleType],
) -> None:
    """Assignment writes update the same footprint returned on each board read."""
    board = modules["standalone_impl"].BoardStub()
    helpers = modules["footprint_helpers"]
    apply = modules["board_part_edits"].apply_board_part_edits
    footprint = board.GetFootprints()[0]
    assert board.FindFootprintByReference("R1") is footprint
    assert board.FindFootprintByReference("missing") is None
    assert footprint.GetFieldByName("LCSC") is None

    apply([(board.FindFootprintByReference("R1"), {"lcsc": " c123 "})])

    assert helpers.get_lcsc_value(board.GetFootprints()[0]) == "C123"
    field = footprint.GetFieldByName("LCSC")
    assert field.GetText() == "C123"
    assert field.IsVisible() is False
    assert footprint.GetProperties() == {"LCSC": "C123"}
    assert footprint.modified is True

    apply([(board.FindFootprintByReference("R1"), {"lcsc": ""})])

    assert helpers.get_lcsc_value(footprint) == ""
    assert footprint.GetFields() == [field]
    assert field.GetText() == ""
    assert field.IsVisible() is False
    assert footprint.GetProperties() == {"LCSC": ""}


def test_alias_clear_preserves_identity_visibility_and_unrelated_metadata(
    modules: dict[str, ModuleType],
) -> None:
    """Clearing all assignment aliases preserves descriptive fields and flags."""
    board = modules["standalone_impl"].BoardStub()
    footprint = board.GetFootprints()[0]
    footprint.SetField("LCSC", "C100")
    footprint.SetField("JLCPCB", "C200")
    footprint.SetField("JLC description", "Keep this description")
    original_fields = footprint.GetFields()
    original_fields[1].SetVisible(False)
    footprint.SetAttributes(65)

    modules["board_part_edits"].apply_board_part_edits(
        [(footprint, {"lcsc": "", "exclude_from_bom": True, "exclude_from_pos": True})]
    )

    assert footprint.GetFields() == original_fields
    assert footprint.GetProperties() == {
        "LCSC": "",
        "JLCPCB": "",
        "JLC description": "Keep this description",
    }
    assert original_fields[0].IsVisible() is True
    assert original_fields[1].IsVisible() is False
    assert footprint.GetAttributes() == 77
    helpers = modules["footprint_helpers"]
    assert helpers.get_lcsc_value(board.FindFootprintByReference("R1")) == ""
    assert helpers.get_exclude_from_bom(board.FindFootprintByReference("R1")) is True
    assert helpers.get_exclude_from_pos(board.FindFootprintByReference("R1")) is True


def test_failed_batch_removes_new_field_and_restores_existing_aliases(
    modules: dict[str, ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later failed setter rolls back new metadata and preceding native edits."""
    standalone = modules["standalone_impl"]
    board = standalone.BoardStub()
    first = board.GetFootprints()[0]
    first.SetField("Manufacturer", "Acme")
    first.SetAttributes(1)
    second = standalone.Footprint_Stub("R2", "100", standalone.LIB_ID_Stub("R"))
    board.footprints.append(second)
    second.SetField("LCSC", "C200")
    second.SetField("JLCPCB", "C201")
    second.GetFieldByName("LCSC").SetVisible(False)
    setter = second.SetField

    def fail_after_alias_write(name: str, text: str) -> None:
        setter(name, text)
        if name == "JLCPCB" and text == "C900":
            raise RuntimeError("standalone native write failed")

    monkeypatch.setattr(second, "SetField", fail_after_alias_write)
    with pytest.raises(RuntimeError, match="standalone native write failed"):
        modules["board_part_edits"].apply_board_part_edits(
            [
                (first, {"lcsc": "C900", "exclude_from_bom": True}),
                (second, {"lcsc": "C900"}),
            ]
        )

    assert first.GetProperties() == {"Manufacturer": "Acme"}
    assert first.GetFieldByName("LCSC") is None
    assert first.GetAttributes() == 1
    assert second.GetProperties() == {"LCSC": "C200", "JLCPCB": "C201"}
    assert second.GetFieldByName("LCSC").IsVisible() is False
    assert second.GetFieldByName("JLCPCB").IsVisible() is True
    assert first.modified is False
    assert second.modified is False


def test_board_filename_survives_readback(modules: dict[str, ModuleType]) -> None:
    """A selected standalone board path remains available to project storage."""
    board = modules["standalone_impl"].BoardStub()
    assert board.GetFileName() == "fake_test_board.kicad_pcb"
    board.SetFileName("project/assembly.kicad_pcb")
    assert board.GetFileName() == "project/assembly.kicad_pcb"
