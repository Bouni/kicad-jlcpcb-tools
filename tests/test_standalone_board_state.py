"""Standalone board mutations must survive subsequent board-based reads."""

from .wx_harness import load, package_stubs

_PACKAGE = "standalone_board_state_tests"
_stubs = package_stubs(_PACKAGE)
standalone = load(_PACKAGE, "standalone_impl", _stubs)
helpers = load(_PACKAGE, "footprint_helpers", _stubs)


def test_assignment_and_clear_survive_lookup_and_board_refresh() -> None:
    """The normal LCSC helpers update the same footprint returned on every read."""
    board = standalone.BoardStub()
    footprint = board.GetFootprints()[0]
    assert board.FindFootprintByReference("R1") is footprint
    assert board.FindFootprintByReference("missing") is None
    assert footprint.GetFieldByName("LCSC") is None

    helpers.set_lcsc_value(board.FindFootprintByReference("R1"), "C123")

    assert helpers.get_lcsc_value(board.GetFootprints()[0]) == "C123"
    field = footprint.GetFieldByName("LCSC")
    assert field.GetText() == "C123"
    assert field.IsVisible() is False
    assert footprint.GetProperties() == {"LCSC": "C123"}

    helpers.set_lcsc_value(board.FindFootprintByReference("R1"), "")

    assert helpers.get_lcsc_value(footprint) == ""
    assert footprint.GetFields() == [field]
    assert field.GetText() == ""
    assert field.IsVisible() is False
    assert footprint.GetProperties() == {"LCSC": ""}


def test_alias_assignments_preserve_field_identity_visibility_and_other_fields() -> (
    None
):
    """Existing aliases are updated in place without altering descriptive fields."""
    footprint = standalone.BoardStub().GetFootprints()[0]
    footprint.SetField("LCSC", "C100")
    footprint.SetField("JLCPCB", "C100")
    footprint.SetField("JLC description", "Keep this description")
    original_fields = footprint.GetFields()
    original_fields[1].SetVisible(False)

    helpers.set_lcsc_value(footprint, "C200")

    assert footprint.GetFields() == original_fields
    assert footprint.GetProperties() == {
        "LCSC": "C200",
        "JLCPCB": "C200",
        "JLC description": "Keep this description",
    }
    assert original_fields[0].IsVisible() is True
    assert original_fields[1].IsVisible() is False

    helpers.set_lcsc_value(footprint, "")

    assert helpers.get_lcsc_value(footprint) == ""
    assert original_fields[0].GetText() == ""
    assert original_fields[1].GetText() == ""
    assert original_fields[2].GetText() == "Keep this description"


def test_exclusion_toggles_preserve_other_attributes_across_board_reads() -> None:
    """BOM/POS changes remain observable and preserve other footprint flags."""
    board = standalone.BoardStub()
    footprint = board.GetFootprints()[0]
    footprint.SetAttributes(1)

    assert helpers.toggle_exclude_from_bom(footprint) is True
    assert helpers.get_exclude_from_bom(board.FindFootprintByReference("R1")) is True
    assert helpers.get_exclude_from_pos(footprint) is False
    assert helpers.toggle_exclude_from_pos(footprint) is True
    assert footprint.GetAttributes() == 13
    assert helpers.toggle_exclude_from_bom(footprint) is False
    assert helpers.toggle_exclude_from_pos(footprint) is False
    assert footprint.GetAttributes() == 1
