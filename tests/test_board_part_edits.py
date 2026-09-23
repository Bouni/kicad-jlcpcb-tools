"""Native footprint transactions preserve assignments and recover partial failures."""

from collections.abc import Iterator
from copy import deepcopy
import os
from types import ModuleType
from typing import Any, Optional

import pytest

from .wx_harness import load_siblings


@pytest.fixture
def edits() -> Iterator[ModuleType]:
    """Load native-independent production transaction code."""
    with load_siblings(
        "_ordinary_board_edit_tests", ("board_part_edits",), {}
    ) as modules:
        yield modules["board_part_edits"]


class Field:
    """A stateful native field preserving text and visibility."""

    def __init__(self, name: str, text: str, visible: bool = True) -> None:
        self.name = name
        self.text = text
        self.visible = visible

    def GetName(self) -> str:
        """Read the native field name."""
        return self.name

    def GetText(self) -> str:
        """Read the native field value."""
        return self.text

    def IsVisible(self) -> bool:
        """Read the field's actual current visibility."""
        return self.visible

    def SetVisible(self, visible: bool) -> None:
        """Update the native visibility state."""
        self.visible = visible


class Footprint:
    """A KiCad 8+ field-based footprint with injectable native write failures."""

    def __init__(self, fields: Optional[dict[str, str]] = None) -> None:
        self.fields = {name: Field(name, text) for name, text in (fields or {}).items()}
        self.attributes = 65
        self.modified = 0
        self.fail_after = ""
        self.reject_text = ""
        self.silent_failure = False

    def GetFields(self) -> list[Field]:
        """Read every actual field."""
        return list(self.fields.values())

    def SetField(self, name: str, value: str) -> None:
        """Emulate native text writes, including a setter that mutates then fails."""
        if self.silent_failure:
            return
        if value == self.reject_text:
            raise RuntimeError("restoration rejected")
        self.fields.setdefault(name, Field(name, "")).text = value
        if name == self.fail_after:
            self.fail_after = ""
            raise RuntimeError("native setter changed the field then failed")

    def Remove(self, field: Field) -> None:
        """Remove a native PCB_FIELD, matching KiCad's public API."""
        del self.fields[field.name]

    def GetAttributes(self) -> int:
        """Read the complete attribute mask, including unrelated bits."""
        return self.attributes

    def SetAttributes(self, value: int) -> None:
        """Update the complete attribute mask."""
        self.attributes = value

    def SetModified(self) -> None:
        """Record a completed batch's dirty notification."""
        self.modified += 1


def state(footprint: Footprint) -> tuple[dict[str, tuple[str, bool]], int]:
    """Snapshot the independently asserted native state."""
    return {
        field.name: (field.text, field.visible) for field in footprint.GetFields()
    }, footprint.GetAttributes()


@pytest.mark.parametrize("lcsc", [" c777 ", ""])
def test_assign_and_clear_updates_all_aliases_preserving_metadata(
    edits: ModuleType, lcsc: str
) -> None:
    """A hidden stale assignment never revives after a visible alias is edited."""
    fp = Footprint(
        {
            "LCSC Part #": " c100 ",
            "LCSC": "C200",
            "JLCPCB Rotation": "90",
            "Manufacturer": "Acme",
        }
    )
    fp.fields["LCSC"].visible = False
    fp.reject_text = "never reject"
    edits.apply_board_part_edits([(fp, {"lcsc": lcsc, "exclude_from_bom": True})])

    expected = lcsc.strip().upper()
    assert state(fp) == (
        {
            "LCSC Part #": (expected, True),
            "LCSC": (expected, False),
            "JLCPCB Rotation": ("90", True),
            "Manufacturer": ("Acme", True),
        },
        73,
    )
    assert fp.modified == 1


def test_new_assignment_is_hidden_and_noop_does_not_mark_modified(
    edits: ModuleType,
) -> None:
    """New LCSC metadata remains invisible on the PCB and repeated edits are inert."""
    fp = Footprint({"Manufacturer": "Acme"})
    edits.apply_board_part_edits([(fp, {"lcsc": "C123"})])
    assert fp.fields["LCSC"].text == "C123"
    assert fp.fields["LCSC"].visible is False
    edits.apply_board_part_edits([(fp, {"lcsc": "C123", "exclude_from_bom": False})])
    assert fp.modified == 1


@pytest.mark.parametrize("lcsc", ["C777", ""])
def test_batch_repairs_empty_and_invalid_aliases_without_changing_other_metadata(
    edits: ModuleType, lcsc: str
) -> None:
    """Native verification uses the same recognized aliases as explicit assignment."""
    fp = Footprint(
        {"LCSC": "", "JLCPCB Part Number": "invalid", "JLCPCB Customer ID": "C100"}
    )
    fp.reject_text = "never reject"
    edits.apply_board_part_edits([(fp, {"lcsc": lcsc})])
    assert {name: field.text for name, field in fp.fields.items()} == {
        "LCSC": lcsc,
        "JLCPCB Part Number": lcsc,
        "JLCPCB Customer ID": "C100",
    }


def test_flag_edits_preserve_dnp_and_other_attributes(edits: ModuleType) -> None:
    """Only the requested exclusion bits change, including explicit reenabling."""
    fp = Footprint({"LCSC": "C100"})
    edits.apply_board_part_edits(
        [(fp, {"exclude_from_bom": True, "exclude_from_pos": True})]
    )
    assert fp.attributes == 77
    edits.apply_board_part_edits([(fp, {"exclude_from_bom": False})])
    assert fp.attributes == 69


def test_later_partial_native_failure_restores_exact_aliases_and_flags(
    edits: ModuleType,
) -> None:
    """Rollback preserves conflicting raw values rather than just one normalized LCSC."""
    first = Footprint({"LCSC": " c100 ", "JLCPCB": "C101", "JLCPCB Rotation": "90"})
    first.fields["JLCPCB"].visible = False
    second = Footprint({"LCSC": "C200", "JLCPCB": "C201"})
    second.fail_after = "LCSC"
    before = [deepcopy(state(first)), deepcopy(state(second))]

    with pytest.raises(RuntimeError, match="native setter changed"):
        edits.apply_board_part_edits(
            [
                (first, {"lcsc": "C999", "exclude_from_pos": True}),
                (second, {"lcsc": "C999"}),
            ]
        )

    assert [state(first), state(second)] == before
    assert first.modified == second.modified == 0


def test_rollback_removes_new_field_even_if_first_setter_mutates_then_raises(
    edits: ModuleType,
) -> None:
    """An aborted assignment must not leave even an empty LCSC field behind."""
    fp = Footprint({"Manufacturer": "Acme"})
    fp.fail_after = "LCSC"
    before = deepcopy(state(fp))
    with pytest.raises(RuntimeError, match="native setter changed"):
        edits.apply_board_part_edits([(fp, {"lcsc": "C999"})])
    assert state(fp) == before


@pytest.mark.parametrize(
    "changes",
    [{"lcsc": "invalid"}, {"lcsc": 1}, {"exclude_from_bom": 1}, {"unknown": True}],
)
def test_entire_batch_is_validated_before_any_mutation(
    edits: ModuleType, changes: dict[str, Any]
) -> None:
    """An invalid later target cannot make an earlier valid assignment durable."""
    first = Footprint({"LCSC": "C100"})
    second = Footprint({"LCSC": "C200"})
    with pytest.raises((ValueError, TypeError)):
        edits.apply_board_part_edits([(first, {"lcsc": "C999"}), (second, changes)])
    assert first.fields["LCSC"].text == "C100"
    assert first.modified == 0


def test_silent_native_failure_is_detected_and_earlier_edits_restored(
    edits: ModuleType,
) -> None:
    """A native setter returning successfully must still have changed the board."""
    first = Footprint({"LCSC": "C100"})
    second = Footprint({"LCSC": "C200"})
    second.silent_failure = True
    with pytest.raises(RuntimeError, match="did not preserve"):
        edits.apply_board_part_edits(
            [(first, {"lcsc": "C999"}), (second, {"lcsc": "C999"})]
        )
    assert first.fields["LCSC"].text == "C100"
    assert second.fields["LCSC"].text == "C200"


def test_attribute_setter_that_mutates_then_fails_restores_fields_and_flags(
    edits: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LCSC and both exclusion bits are rolled back together after a flag failure."""
    fp = Footprint({"LCSC": "C100"})
    before = deepcopy(state(fp))

    def reject_changed_attributes(value: int) -> None:
        fp.attributes = value
        if value != before[1]:
            raise RuntimeError("attribute native write failed")

    monkeypatch.setattr(fp, "SetAttributes", reject_changed_attributes)
    with pytest.raises(RuntimeError, match="attribute native write failed"):
        edits.apply_board_part_edits(
            [(fp, {"lcsc": "C999", "exclude_from_bom": True, "exclude_from_pos": True})]
        )
    assert state(fp) == before


def test_later_unreadable_footprint_prevents_first_write(
    edits: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full selection must be readable before any selected footprint is changed."""
    first = Footprint({"LCSC": "C100"})
    second = Footprint({"LCSC": "C200"})

    def reject_read() -> list[Field]:
        raise RuntimeError("footprint no longer readable")

    monkeypatch.setattr(second, "GetFields", reject_read)
    with pytest.raises(RuntimeError, match="no longer readable"):
        edits.apply_board_part_edits(
            [(first, {"lcsc": "C999"}), (second, {"lcsc": "C999"})]
        )
    assert first.fields["LCSC"].text == "C100"
    assert first.modified == 0


def test_duplicate_native_field_names_are_rejected_before_any_write(
    edits: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ambiguous native field name must not defeat raw-value recovery."""
    first = Footprint({"LCSC": "C100"})
    second = Footprint({"LCSC": "C200"})
    duplicate = Field("LCSC", "C201")
    monkeypatch.setattr(second, "GetFields", lambda: [second.fields["LCSC"], duplicate])
    with pytest.raises(ValueError, match="Duplicate native footprint field"):
        edits.apply_board_part_edits(
            [(first, {"lcsc": "C999"}), (second, {"lcsc": "C999"})]
        )
    assert first.fields["LCSC"].text == "C100"
    assert [field.text for field in second.GetFields()] == ["C200", "C201"]


def test_failed_attribute_read_does_not_prevent_independent_field_recovery(
    edits: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A readback failure still attempts raw field restoration before verification."""
    fp = Footprint({"LCSC": "C100"})

    def read_attributes() -> int:
        if fp.fields["LCSC"].text != "C100":
            raise RuntimeError("attributes temporarily unavailable")
        return fp.attributes

    monkeypatch.setattr(fp, "GetAttributes", read_attributes)
    with pytest.raises(RuntimeError, match="attributes temporarily unavailable"):
        edits.apply_board_part_edits([(fp, {"lcsc": "C999"})])
    assert fp.fields["LCSC"].text == "C100"


def test_failed_field_read_does_not_prevent_raw_value_recovery(
    edits: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Captured names still allow restoration when a native collection read fails."""
    fp = Footprint({"LCSC": "C100"})

    def read_fields() -> list[Field]:
        if fp.fields["LCSC"].text != "C100":
            raise RuntimeError("fields temporarily unavailable")
        return list(fp.fields.values())

    monkeypatch.setattr(fp, "GetFields", read_fields)
    with pytest.raises(RuntimeError, match="fields temporarily unavailable"):
        edits.apply_board_part_edits([(fp, {"lcsc": "C999"})])
    assert fp.fields["LCSC"].text == "C100"


def test_failed_recovery_raises_special_error_and_still_restores_other_footprints(
    edits: ModuleType,
) -> None:
    """The UI can stop further editing if one footprint's native rollback is rejected."""
    first = Footprint({"LCSC": "C100"})
    second = Footprint({"LCSC": "C200"})
    second.fail_after = "LCSC"
    second.reject_text = "C200"
    with pytest.raises(edits.BoardEditRecoveryError, match="recovery failed"):
        edits.apply_board_part_edits(
            [(first, {"lcsc": "C999"}), (second, {"lcsc": "C999"})]
        )
    assert first.fields["LCSC"].text == "C100"
    assert second.fields["LCSC"].text == "C999"


def test_absent_dirty_method_keeps_native_assignment_supported(
    edits: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native mutation can run when a host does not provide per-item dirty marking."""
    fp = Footprint({"LCSC": "C100"})
    monkeypatch.setattr(fp, "SetModified", None)
    edits.apply_board_part_edits([(fp, {"lcsc": "C999"})])
    assert fp.fields["LCSC"].text == "C999"


def test_dirty_notification_failure_restores_all_native_values(
    edits: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure while finishing the batch cannot leave accepted-looking assignments."""
    first = Footprint({"LCSC": "C100"})
    second = Footprint({"LCSC": "C200"})

    def reject_modified() -> None:
        raise RuntimeError("native dirty notification failed")

    monkeypatch.setattr(second, "SetModified", reject_modified)
    with pytest.raises(RuntimeError, match="native dirty notification failed"):
        edits.apply_board_part_edits(
            [(first, {"lcsc": "C999"}), (second, {"lcsc": "C999"})]
        )
    assert first.fields["LCSC"].text == "C100"
    assert second.fields["LCSC"].text == "C200"


class LegacyFootprint(Footprint):
    """KiCad 7 exposes property maps without user field objects."""

    def __init__(self, properties: dict[str, str]) -> None:
        super().__init__()
        self.properties = dict(properties)
        self.fail_properties = False

    def GetFields(self) -> list[Field]:
        """Model the absence of GetFields in KiCad 7."""
        raise AttributeError("GetFields")

    def GetProperties(self) -> dict[str, str]:
        """Read a copy of the live native property map."""
        return dict(self.properties)

    def SetProperties(self, properties: dict[str, str]) -> None:
        """Replace the live native map, optionally failing after native mutation."""
        self.properties = dict(properties)
        if self.fail_properties:
            self.fail_properties = False
            raise RuntimeError("legacy native write failed")


def test_kicad7_assigns_and_rolls_back_full_property_maps(edits: ModuleType) -> None:
    """No variant or field APIs are needed for ordinary legacy board assignments."""
    first = LegacyFootprint({"Manufacturer": "Acme"})
    second = LegacyFootprint(
        {"LCSC": " c100 ", "JLCPCB": "C200", "JLCPCB Rotation": "90"}
    )
    second.fail_properties = True
    before = [first.GetProperties(), second.GetProperties()]
    with pytest.raises(RuntimeError, match="legacy native write failed"):
        edits.apply_board_part_edits(
            [(first, {"lcsc": "C999"}), (second, {"lcsc": "C999"})]
        )
    assert [first.GetProperties(), second.GetProperties()] == before
    edits.apply_board_part_edits([(second, {"lcsc": "C999"})])
    assert second.GetProperties() == {
        "LCSC": "C999",
        "JLCPCB": "C999",
        "JLCPCB Rotation": "90",
    }


@pytest.mark.native_kicad
@pytest.mark.skipif(
    os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
    reason="Native KiCad tests require an explicit opt-in",
)
def test_native_fields_save_reload_and_rollback(
    edits: ModuleType, tmp_path: Any
) -> None:
    """Exercise real native field creation/removal and persistence on a saved board."""
    pcbnew = pytest.importorskip("pcbnew")
    board = pcbnew.BOARD()
    first = pcbnew.FOOTPRINT(board)
    first.SetReference("R1")
    first.SetField("JLCPCB Rotation", "90")
    board.Add(first)
    second = Footprint({"LCSC": "C200"})
    second.fail_after = "LCSC"
    with pytest.raises(RuntimeError, match="native setter changed"):
        edits.apply_board_part_edits(
            [(first, {"lcsc": "C999"}), (second, {"lcsc": "C999"})]
        )
    assert first.GetField("LCSC") is None
    edits.apply_board_part_edits([(first, {"lcsc": "C123", "exclude_from_bom": True})])
    assert first.GetField("LCSC").IsVisible() is False
    filename = str(tmp_path / "ordinary.kicad_pcb")
    assert pcbnew.SaveBoard(filename, board)
    loaded_board = pcbnew.LoadBoard(filename)
    loaded = loaded_board.FindFootprintByReference("R1")
    assert loaded.GetField("LCSC").GetText() == "C123"
    assert loaded.GetField("JLCPCB Rotation").GetText() == "90"
    assert loaded.IsExcludedFromBOM() is True
