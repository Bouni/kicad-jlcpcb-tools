"""Native-authority cases with stateful doubles and opt-in KiCad bindings."""

# Stateful native API doubles retain the C++ method spellings.
# ruff: noqa: D101, D102, D103

from dataclasses import FrozenInstanceError
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from . import native_kicad_support
from .variant_native_support import (
    KICAD_FP_DNP,
    KICAD_FP_JUST_ADDED,
    Board,
    Footprint,
    Variant,
    native as native_module,
)

native_bindings = native_kicad_support.native_bindings

NativeVariantError = native_module.NativeVariantError
StaleVariantTarget = native_module.StaleVariantTarget
VariantEdit = native_module.VariantEdit
VariantNativeAdapter = native_module.VariantNativeAdapter
native_board_identity = native_module.native_board_identity


@pytest.fixture
def native() -> tuple[Board, VariantNativeAdapter]:
    board = Board()
    return board, VariantNativeAdapter(board, "project-board", variant_factory=Variant)


def edit(adapter: VariantNativeAdapter, name: str, **changes: Any) -> Any:
    snapshot = adapter.snapshot()
    return adapter.apply_edits(
        [VariantEdit(snapshot.target("component-1", name), tuple(changes.items()))]
    )


def test_default_a_default_assignments_do_not_leak(native: Any) -> None:
    board, adapter = native
    before = adapter.snapshot()
    assert [v.name for v in before.variants] == ["", "A", "B"]
    after = edit(adapter, "A", lcsc="c2", value="22k", pop=False, pos=False)
    assert after.get("component-1", "").lcsc == "C1"
    assert after.get("component-1", "A").lcsc == "C2"
    assert after.get("component-1", "B").value == "10k"
    assert not after.get("component-1", "A").pop
    assert board.current == "A"
    assert board.modified
    reset = adapter.apply_edits(
        [VariantEdit(after.target("component-1", "A"), use_base=("value",))]
    ).get("component-1", "A")
    assert (reset.value, reset.lcsc, reset.pop, reset.pos) == (
        "10k",
        "C2",
        False,
        False,
    )
    record = board.parts[0].GetVariant("A")
    assert record.HasFieldValue("LCSC") and not record.HasFieldValue("Value")
    edit(adapter, "", lcsc="C3")
    assert adapter.snapshot().get("component-1", "A").lcsc == "C2"


def test_invalid_conflicting_and_missing_are_distinct(native: Any) -> None:
    board, adapter = native
    fp = board.parts[0]
    del fp.fields["LCSC"]
    assert adapter.snapshot().get("component-1", "").assignment.status == "missing"
    fp.fields["LCSC"] = "placeholder"
    assert adapter.snapshot().get("component-1", "").assignment.status == "invalid"
    fp.fields.update(LCSC="C1", JLCPCB="C2")
    assert adapter.snapshot().get("component-1", "").assignment.status == "conflict"
    fp.AddVariant("A").SetFieldValue("LCSC", "")
    assert adapter.snapshot().get("component-1", "A").assignment.status == "empty"


def test_stale_target_is_rejected_but_other_variant_edit_keeps_it_valid(
    native: Any,
) -> None:
    board, adapter = native
    target = adapter.snapshot().target("component-1", "A")
    edit(adapter, "B", lcsc="C8")
    adapter.apply_edits([VariantEdit(target, (("lcsc", "C2"),))])
    with pytest.raises(StaleVariantTarget):
        adapter.apply_edits([VariantEdit(target, (("lcsc", "C3"),))])
    current = adapter.snapshot().target("component-1", "A")
    board.names.remove("A")
    with pytest.raises(NativeVariantError):
        adapter.apply_edits([VariantEdit(current, (("lcsc", "C9"),))])


def test_lcsc_edits_are_judged_and_written_canonically(native: Any) -> None:
    """A padded or lower-case number is the part it names; anything else is not.

    The adapter shares the plugin's one definition of a part number, so the
    field it writes is the canonical spelling every other write path stores.
    """
    board, adapter = native
    after = edit(adapter, "A", lcsc=" c2 ")
    assert after.get("component-1", "A").lcsc == "C2"
    assert board.parts[0].GetVariant("A").GetFieldValue("LCSC") == "C2"
    with pytest.raises(NativeVariantError, match="C followed by digits"):
        edit(adapter, "A", lcsc="C 2")
    with pytest.raises(NativeVariantError, match="C followed by digits"):
        edit(adapter, "A", lcsc="LCSC C2")


def test_complete_batch_is_validated_before_mutation(native: Any) -> None:
    board, adapter = native
    snap = adapter.snapshot()
    with pytest.raises(NativeVariantError):
        adapter.apply_edits(
            [
                VariantEdit(snap.target("component-1", "A"), (("lcsc", "C2"),)),
                VariantEdit(snap.target("component-1", "B"), (("pop", "False"),)),
            ]
        )
    assert not board.parts[0].variants


def test_snapshot_immutable_and_native_changes_invalidate(native: Any) -> None:
    board, adapter = native
    snap = adapter.snapshot()
    with pytest.raises(FrozenInstanceError):
        snap.components[0].lcsc = "C9"
    board.current = "B"
    assert adapter.snapshot().source_token == snap.source_token
    board.parts[0].fields["LCSC"] = "C8"
    assert adapter.snapshot().source_token != snap.source_token


def test_named_api_failure_never_falls_back_to_default(native: Any) -> None:
    board, adapter = native
    board.parts[0].GetDNPForVariant = None
    with pytest.raises(NativeVariantError):
        adapter.snapshot()
    assert board.parts[0].fields["LCSC"] == "C1"


def test_matching_board_uuid_and_references_do_not_allow_cross_board_edits(
    native: Any,
) -> None:
    _board, adapter = native
    other = VariantNativeAdapter(Board(), "project-board", variant_factory=Variant)
    target = adapter.snapshot().target("component-1", "A")
    with pytest.raises(StaleVariantTarget):
        other.apply_edits([VariantEdit(target, (("lcsc", "C9"),))])


def test_reset_nonexistent_override_does_not_create_record_or_mark_modified(
    native: Any,
) -> None:
    board, adapter = native
    before = adapter.snapshot()
    after = adapter.apply_edits(
        [VariantEdit(before.target("component-1", "A"), use_base=("lcsc",))]
    )
    assert after == before
    assert not board.modified
    assert not board.parts[0].variants


def test_estimator_ignores_nonplated_pads(native: Any) -> None:
    board, adapter = native
    pads = [
        SimpleNamespace(GetAttribute=lambda: 3, HasHole=lambda: True),
        SimpleNamespace(GetAttribute=lambda: 1, HasHole=lambda: False),
        SimpleNamespace(GetAttribute=lambda: 3, HasHole=lambda: True),
    ]
    board.parts[0].Pads = lambda: pads
    state = adapter.snapshot().get("component-1", "A")
    assert (state.pad_count, state.has_tht) == (1, False)


def test_assignment_aliases_preserve_unrelated_jlc_metadata(native: Any) -> None:
    board, adapter = native
    fp = board.parts[0]
    fp.fields.update(
        {
            "JLCPCB Rotation": "90",
            "LCSC URL": "https://lcsc.com/C1",
            "JLC custom assembly code": "C1",
        }
    )
    fp.AddVariant("A").SetFieldValue("JLCPCB Rotation", "180")
    assert adapter.snapshot().get("component-1", "A").assignment.status == "valid"
    after = edit(adapter, "A", lcsc="C7")
    assert after.get("component-1", "A").lcsc == "C7"
    assert fp.GetVariant("A").GetFieldValue("JLCPCB Rotation") == "180"
    assert not fp.GetVariant("A").HasFieldValue("LCSC URL")
    del fp.fields["LCSC"]
    assert adapter.snapshot().get("component-1", "").assignment.status == "missing"
    assert (
        edit(adapter, "", lcsc="").get("component-1", "").assignment.status == "empty"
    )
    assert fp.fields["JLC custom assembly code"] == "C1"
    assert fp.fields["JLCPCB Rotation"] == "90"
    assert fp.fields["LCSC URL"] == "https://lcsc.com/C1"


@pytest.mark.parametrize("name", ["JLCPCB", "JLCPCB Part #", "LCSC Part Number"])
def test_assignment_aliases_keep_provenance_through_clear_reset_and_reopen(
    native: Any, name: str
) -> None:
    board, adapter = native
    fp = board.parts[0]
    del fp.fields["LCSC"]
    fp.fields[name] = "placeholder"
    assert adapter.snapshot().get("component-1", "").assignment.status == "invalid"
    edit(adapter, "A", lcsc="C2")
    record = fp.GetVariant("A")
    record.SetFieldValue("Manufacturer", "keep")
    record.SetDNP(True)
    record.SetExcludedFromBOM(True)
    assert (
        edit(adapter, "", lcsc="").get("component-1", "").assignment.status == "empty"
    )
    edit(adapter, "", lcsc="C1")
    cleared = edit(adapter, "A", lcsc="")
    assert cleared.get("component-1", "A").assignment.status == "empty"
    assert cleared.get("component-1", "A").lcsc == ""
    inherited = adapter.apply_edits(
        (VariantEdit(cleared.target("component-1", "A"), use_base=("lcsc",)),)
    ).get("component-1", "A")
    assert inherited.lcsc == "C1" and inherited.assignment.inherited
    reopened = VariantNativeAdapter(board, "project-board", variant_factory=Variant)
    assert edit(reopened, "", lcsc="C3").get("component-1", "A").lcsc == "C3"
    assert fp.GetVariant("A").fields == {"Manufacturer": "keep"}
    assert fp.GetVariant("A").GetDNP() and fp.GetVariant("A").GetExcludedFromBOM()


def test_missing_modern_enumerator_fails_closed_even_when_default_selected(
    native: Any,
) -> None:
    board, adapter = native
    board.current = ""
    board.GetVariantNamesForUI = None
    with pytest.raises(NativeVariantError, match="GetVariantNamesForUI"):
        adapter.snapshot()


def test_native_modified_revision_is_in_returned_snapshot(native: Any) -> None:
    board, adapter = native
    board.timestamp = 0
    board.GetTimeStamp = lambda: board.timestamp

    original = board.parts[0].SetModified

    def mark_modified() -> None:
        original()
        board.timestamp += 1

    board.parts[0].SetModified = mark_modified
    after = edit(adapter, "A", value="22k")
    assert adapter.snapshot().source_token == after.source_token


@pytest.mark.parametrize("unrelated", [0, KICAD_FP_JUST_ADDED, 1 | 16 | 32 | 128])
def test_default_population_edits_use_native_dnp_bit_and_preserve_other_flags(
    native: Any, unrelated: int
) -> None:
    """Default POP changes effective population without changing JUST_ADDED."""
    board, adapter = native
    fp = board.parts[0]
    fp.attributes = unrelated
    fp.AddVariant("A")  # An explicit record keeps its existing population.

    excluded = edit(adapter, "", pop=False)
    assert excluded.get("component-1", "").pop is False
    assert excluded.get("component-1", "B").pop is False
    assert excluded.get("component-1", "A").pop is True
    assert fp.GetAttributes() == unrelated | KICAD_FP_DNP

    included = edit(adapter, "", pop=True)
    assert included.get("component-1", "").pop is True
    assert included.get("component-1", "B").pop is True
    assert fp.GetAttributes() == unrelated


@pytest.mark.parametrize(
    ("attributes", "populated"),
    [
        (KICAD_FP_DNP | 8 | 4, False),
        (KICAD_FP_JUST_ADDED | 8 | 4, True),
        (KICAD_FP_DNP | KICAD_FP_JUST_ADDED | 8 | 4, False),
    ],
)
def test_first_named_assignment_and_flag_edits_preserve_native_base_population(
    native: Any, attributes: int, populated: bool
) -> None:
    """First assignment and flag records copy base flags without confusing JUST_ADDED."""
    board, adapter = native
    fp = board.parts[0]
    fp.attributes = attributes

    changed = edit(adapter, "A", lcsc="C2")
    state = changed.get("component-1", "A")
    assert (state.bom, state.pos, state.pop) == (False, False, populated)
    assert changed.get("component-1", "").pop is populated
    assert changed.get("component-1", "B").pop is populated
    flag_only = edit(adapter, "B", bom=True).get("component-1", "B")
    assert (flag_only.bom, flag_only.pos, flag_only.pop) == (True, False, populated)
    assert fp.GetAttributes() == attributes
    assert fp.GetVariant("A").GetDNP() is not populated
    fp.SetAttributes(0)
    later = edit(adapter, "A", pos=True).get("component-1", "A")
    assert (later.bom, later.pos, later.pop, later.lcsc) == (
        False,
        True,
        populated,
        "C2",
    )


@pytest.mark.parametrize(
    ("variant", "field", "getter"),
    [
        ("", "pop", "IsDNP"),
        ("A", "pop", "GetDNPForVariant"),
        ("A", "bom", "GetExcludedFromBOMForVariant"),
        ("A", "pos", "GetExcludedFromPosFilesForVariant"),
    ],
)
def test_raw_native_write_cannot_succeed_with_unchanged_effective_flag(
    native: Any, variant: str, field: str, getter: str
) -> None:
    """Successful setters are insufficient if effective native read-back disagrees."""
    board, adapter = native
    fp = board.parts[0]
    setattr(fp, getter, lambda *_args: False)
    before = adapter.snapshot()

    with pytest.raises(NativeVariantError, match=f"effective {field}") as failure:
        edit(adapter, variant, **{field: False})

    assert f"R1/{variant or 'base'}" in str(failure.value)
    assert "restored" in str(failure.value)
    assert fp.GetAttributes() == 0
    assert not fp.variants
    assert adapter.snapshot().source_token == before.source_token
    assert adapter.unreliable is False


# Disposable native boards exercise serialization, not a running PCB Editor.
native_desktop_test = pytest.mark.skipif(
    os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
    reason="Native KiCad tests require an explicitly enabled desktop session",
)


@pytest.fixture
def native_pcbnew(native_bindings: SimpleNamespace) -> SimpleNamespace:
    """Create a standalone native board without accessing the active editor."""
    return SimpleNamespace(
        pcbnew=native_bindings.pcbnew, board=native_bindings.pcbnew.BOARD()
    )


@pytest.mark.native_kicad
@native_desktop_test
def test_native_alias_overrides_flags_and_descriptions_survive_board_reload(
    native_pcbnew: SimpleNamespace, tmp_path: Path
) -> None:
    """Clear/reset native fields, then verify their serialized effective state."""
    pcbnew, board = native_pcbnew.pcbnew, native_pcbnew.board
    path = str(tmp_path / "variants.kicad_pcb")
    board.SetFileName(path)
    board.AddVariant("A")
    board.AddVariant("B")
    board.AddVariant("Default")
    board.SetVariantDescription("A", "Production build A")
    footprint = pcbnew.FOOTPRINT(board)
    footprint.SetReference("R1")
    footprint.SetValue("10k")
    footprint.SetField("LCSC", "C1")
    footprint.SetField("JLCPCB", "C1")
    footprint.SetField("JLCPCB Rotation", "90")
    board.Add(footprint)
    board.SetCurrentVariant("B")
    component = str(footprint.m_Uuid.AsString())
    assert native_board_identity(board) == native_board_identity(footprint.GetBoard())
    adapter = VariantNativeAdapter(board, "native-test")
    before = adapter.snapshot()
    assert not board.IsModified()
    edited = adapter.apply_edits(
        [
            VariantEdit(
                before.target(component, "A"),
                (("lcsc", "C2"), ("value", "22k"), ("pop", False)),
            ),
            VariantEdit(
                before.target(component, "B"),
                (("lcsc", "C3"), ("bom", False), ("pos", False)),
            ),
        ]
    )
    assert adapter.snapshot().source_token == edited.source_token
    assert board.IsModified()
    assert str(board.GetCurrentVariant()) == "B"
    assert edited.get(component, "").lcsc == "C1"
    assert footprint.GetField("JLCPCB Rotation").GetText() == "90"
    assert not footprint.GetVariant("A").HasFieldValue("JLCPCB Rotation")
    footprint.GetVariant("A").SetFieldValue("Unrelated", "preserve")
    edited = adapter.snapshot()
    cleared = adapter.apply_edits(
        [VariantEdit(edited.target(component, "A"), (("lcsc", ""),))]
    )
    assert cleared.get(component, "A").assignment.status == "empty"
    assert footprint.GetVariant("A").HasFieldValue("LCSC")
    assert footprint.GetVariant("A").HasFieldValue("JLCPCB")
    assert pcbnew.SaveBoard(path, board)
    blank = VariantNativeAdapter(pcbnew.LoadBoard(path), "native-test").snapshot()
    assert blank.get(component, "A").assignment.status == "empty"
    assert blank.get(component, "A").lcsc == ""
    reset = adapter.apply_edits(
        [VariantEdit(cleared.target(component, "A"), use_base=("lcsc",))]
    )
    assert reset.get(component, "A").assignment.inherited
    assert not footprint.GetVariant("A").HasFieldValue("LCSC")
    assert not footprint.GetVariant("A").HasFieldValue("JLCPCB")

    assert pcbnew.SaveBoard(path, board)
    reloaded_board = pcbnew.LoadBoard(path)
    reloaded = VariantNativeAdapter(reloaded_board, "native-test").snapshot()
    assert [variant.name for variant in reloaded.variants] == ["", "A", "B", "Default"]
    assert len({variant.label for variant in reloaded.variants}) == 4
    assert reloaded.variants[1].description == "Production build A"
    assert reloaded.get(component, "").lcsc == "C1"
    assert reloaded.get(component, "A").value == "22k"
    assert reloaded.get(component, "A").lcsc == "C1"
    assert reloaded.get(component, "A").assignment.inherited
    assert not reloaded.get(component, "A").pop
    assert reloaded.get(component, "B").lcsc == "C3"
    assert not reloaded.get(component, "B").bom
    assert not reloaded.get(component, "B").pos
    saved = reloaded_board.FindFootprintByReference("R1")
    assert saved.GetField("JLCPCB Rotation").GetText() == "90"
    assert saved.GetVariant("A").GetFieldValue("Unrelated") == "preserve"
    # The loaded board starts at Default; exporting must choose its own target.
    assert reloaded.native_variant_name == ""
    assert str(board.GetCurrentVariant()) == "B"


@pytest.mark.native_kicad
@native_desktop_test
@pytest.mark.parametrize("field,mask", [("bom", 8), ("pos", 4), ("pop", 64)])
def test_native_default_flags_survive_reload_without_changing_explicit_variants(
    native_pcbnew: SimpleNamespace, tmp_path: Path, field: str, mask: int
) -> None:
    """Default uses native attributes; only variants without a record inherit it."""
    pcbnew, board = native_pcbnew.pcbnew, native_pcbnew.board
    board.AddVariant("Explicit")
    board.AddVariant("Inherited")
    footprint = pcbnew.FOOTPRINT(board)
    footprint.SetReference("R1")
    footprint.SetValue("10k")
    unrelated = pcbnew.FP_SMD | pcbnew.FP_BOARD_ONLY
    footprint.SetAttributes(unrelated)
    footprint.SetVariant(pcbnew.FOOTPRINT_VARIANT("Explicit"))
    board.Add(footprint)
    component = str(footprint.m_Uuid.AsString())
    path = str(tmp_path / "default-flags.kicad_pcb")
    board.SetFileName(path)
    adapter = VariantNativeAdapter(board, "native-test")
    for included in (False, True):
        adapter.apply_edits(
            (
                VariantEdit(
                    adapter.snapshot().target(component, ""), ((field, included),)
                ),
            )
        )
        assert pcbnew.SaveBoard(path, board)
        saved_board = pcbnew.LoadBoard(path)
        saved = VariantNativeAdapter(saved_board, "native-test").snapshot()
        for name in ("", "Inherited", "Explicit"):
            assert getattr(saved.get(component, name), field) is (
                included or name == "Explicit"
            )
        expected = unrelated if included else unrelated | mask
        assert saved_board.FindFootprintByReference("R1").GetAttributes() == expected


@pytest.mark.native_kicad
@native_desktop_test
def test_native_failed_assignment_removes_new_field_instead_of_leaving_empty(
    native_pcbnew: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compensation must restore a missing native field, not an empty override."""
    footprint = native_pcbnew.pcbnew.FOOTPRINT(native_pcbnew.board)
    footprint.SetReference("R1")
    native_pcbnew.board.Add(footprint)
    component = str(footprint.m_Uuid.AsString())

    adapter = VariantNativeAdapter(native_pcbnew.board, "native-test")
    read = adapter.snapshot

    def fail_after_native_write() -> Any:
        if footprint.HasField("LCSC"):
            assert footprint.GetField("LCSC").GetText() == "C9"
            raise RuntimeError("native readback failure")
        return read()

    monkeypatch.setattr(adapter, "snapshot", fail_after_native_write)
    before = adapter.snapshot()
    with pytest.raises(NativeVariantError, match="restored"):
        adapter.apply_edits(
            [VariantEdit(before.target(component, ""), (("lcsc", "C9"),))]
        )
    assert not footprint.HasField("LCSC")
    assert adapter.snapshot().components == before.components


@pytest.mark.parametrize("operation", ["set", "use_base"])
@pytest.mark.parametrize("field", ["value", "lcsc"])
def test_effective_text_readback_failure_restores_native_override(
    native: Any,
    operation: str,
    field: str,
) -> None:
    """Successful raw writes must agree with effective fields for edits and inheritance."""
    board, adapter = native
    fp = board.parts[0]
    name = "Value" if field == "value" else "LCSC"
    desired = "22k" if field == "value" else "C2"
    if operation == "use_base":
        fp.AddVariant("A").SetFieldValue(name, desired)
    before = adapter.snapshot()
    original = fp.GetFieldValueForVariant
    stale = original("A", name)
    fp.GetFieldValueForVariant = (
        lambda variant, key: stale if key == name else original(variant, key)
    )
    update = VariantEdit(
        before.target("component-1", "A"),
        changes=((field, desired),) if operation == "set" else (),
        use_base=(field,) if operation == "use_base" else (),
    )
    with pytest.raises(NativeVariantError, match="read-back"):
        adapter.apply_edits((update,))
    fp.GetFieldValueForVariant = original
    assert adapter.snapshot().components == before.components
    assert not adapter.unreliable


def test_snapshot_reads_physical_fields_and_pads_once_per_footprint(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Variant expansion reuses the same physical geometry and solder-joint facts."""
    board, adapter = native
    fp = board.parts[0]
    readers = {
        name: Mock(wraps=getattr(fp, name))
        for name in ("GetFields", "GetPosition", "Pads")
    }
    for name, read in readers.items():
        monkeypatch.setattr(fp, name, read)
    snapshot = adapter.snapshot()
    assert len(snapshot.components) == 3
    assert {name: read.call_count for name, read in readers.items()} == {
        "GetFields": 1,
        "GetPosition": 1,
        "Pads": 1,
    }
    assert {
        (part.pad_count, part.has_tht, part.x_mm, part.y_mm)
        for part in snapshot.components
    } == {(2, False, 10, 20)}


@pytest.mark.parametrize("duplicate", ["reference", "uuid"])
def test_ambiguous_native_inventory_is_rejected_before_editing(
    native: Any, duplicate: str
) -> None:
    """A board with repeated references or identities has no safe assignment target."""
    board, adapter = native
    source = adapter.snapshot()
    board.parts.append(
        Footprint(
            board,
            "component-1" if duplicate == "uuid" else "component-2",
            "R1" if duplicate == "reference" else "R2",
        )
    )
    with pytest.raises(NativeVariantError, match="ambiguous|Duplicate"):
        adapter.apply_edits(
            (VariantEdit(source.target("component-1", "A"), (("lcsc", "C9"),)),)
        )
    assert not any(part.variants for part in board.parts)
    assert not board.modified
