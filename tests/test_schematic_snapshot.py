"""Capture live Default assignments without losing clear/unknown provenance."""

# Stateful board doubles retain the native API's method names.
# ruff: noqa: D101, D102, D103

from dataclasses import FrozenInstanceError, replace
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import Any, Optional

import pytest

from .variant_native_support import Board, Variant, native


@pytest.fixture
def api() -> ModuleType:
    package_name = "_schematic_snapshot_tests"
    package = sys.modules.setdefault(package_name, ModuleType(package_name))
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    return importlib.import_module(f"{package_name}.schematic_snapshot")


class Field:
    def __init__(self, name: str, text: str) -> None:
        self.name, self.text = name, text

    def GetName(self) -> str:
        return self.name

    def GetText(self) -> str:
        return self.text


class Footprint:
    def __init__(self, fields: dict[str, str], reference: str = "R1") -> None:
        self.fields = fields
        self.reference = reference
        self.attributes = 0

    def GetFields(self) -> list[Field]:
        return [Field(name, text) for name, text in self.fields.items()]

    def GetReference(self) -> str:
        return self.reference

    def GetAttributes(self) -> int:
        return self.attributes


class LegacyFootprint(Footprint):
    def GetFields(self) -> list[Field]:
        raise AttributeError("GetFields")

    def GetProperties(self) -> dict[str, str]:
        return dict(self.fields)


def board_with(*footprints: Any) -> Any:
    return SimpleNamespace(GetFootprints=lambda: footprints)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize(
    "fields,expected,reason",
    [
        ({"LCSC PartNr": " c100 "}, "C100", ""),
        ({"LCSC": "C100", "JLCPCB": "c100"}, "C100", ""),
        ({"LCSC": "", "JLCPCB": ""}, "", ""),
        ({}, None, "missing"),
        ({"JLCPCB Customer ID": "C100"}, None, "missing"),
        ({"LCSC": "C100", "JLCPCB": "C200"}, None, "conflict"),
        ({"LCSC": "", "JLCPCB": "C100"}, None, "conflict"),
        ({"LCSC": "invalid"}, None, "invalid"),
        ({"LCSC": " "}, None, "whitespace"),
        ({"LCSC": "", "JLCPCB": " "}, None, "whitespace"),
        ({"LCSC": "C100", "JLCPCB": " "}, None, "whitespace"),
    ],
)
def test_board_capture_preserves_unsafe_sources(
    api: ModuleType,
    legacy: bool,
    fields: dict[str, str],
    expected: Optional[str],
    reason: str,
) -> None:
    footprint = (LegacyFootprint if legacy else Footprint)(fields)
    captured = api.capture_board(board_with(footprint))
    assert dict(captured.assignments) == {"R1": expected}
    assert captured.bom_parts == ({"reference": "R1", "exclude_from_bom": False},)
    if reason:
        assert len(captured.warnings) == 1
        assert "R1" in captured.warnings[0] and reason in captured.warnings[0]
    else:
        assert captured.warnings == ()
    assert footprint.fields == fields


def test_reopening_captures_current_fields_without_changing_old_snapshot(
    api: ModuleType,
) -> None:
    footprint = Footprint({"LCSC": "C100"})
    board = board_with(footprint)
    first = api.capture_board(board)
    footprint.fields["LCSC"] = ""
    footprint.attributes = 8
    cleared = api.capture_board(board)
    footprint.fields.clear()
    missing = api.capture_board(board)
    assert first.assignments["R1"] == "C100"
    assert cleared.assignments["R1"] == ""
    assert missing.assignments["R1"] is None
    assert first.bom_parts[0]["exclude_from_bom"] is False
    assert cleared.bom_parts[0]["exclude_from_bom"] is True
    with pytest.raises(TypeError):
        first.assignments["R1"] = "C999"
    with pytest.raises(TypeError):
        first.bom_parts[0]["exclude_from_bom"] = True
    with pytest.raises(FrozenInstanceError):
        first.warnings = ()


@pytest.mark.parametrize(
    "fields", [{"LCSC": "C100", "JLCPCB": "C200"}, {"LCSC": "", "JLCPCB": " "}, {}]
)
def test_native_capture_preserves_default_provenance_and_ignores_named_values(
    api: ModuleType, fields: dict[str, str]
) -> None:
    board = Board()
    footprint = board.parts[0]
    footprint.fields = {"Reference": "R1", "Value": "10k", **fields}
    footprint.AddVariant("A").SetFieldValue("LCSC", "C999")
    adapter = native.VariantNativeAdapter(board, "capture", variant_factory=Variant)
    captured = api.capture_native(adapter.snapshot())
    assert dict(captured.assignments) == {"R1": None}
    assert len(captured.warnings) == 1
    assert "R1" in captured.warnings[0]


def test_native_valid_and_empty_capture_use_same_default_state(api: ModuleType) -> None:
    board = Board()
    adapter = native.VariantNativeAdapter(board, "capture", variant_factory=Variant)
    first = api.capture_native(adapter.snapshot())
    board.parts[0].fields["LCSC"] = ""
    board.parts[0].attributes = 8
    second = api.capture_native(adapter.snapshot())
    assert first.assignments["R1"] == "C1"
    assert second.assignments["R1"] == ""
    assert second.bom_parts[0]["exclude_from_bom"] is True


def test_native_capture_retains_reported_conflict_even_with_valid_text(
    api: ModuleType,
) -> None:
    state = (
        native.VariantNativeAdapter(Board(), "capture").snapshot().for_variant("")[0]
    )
    conflicting = replace(
        state, assignment=replace(state.assignment, status="conflict")
    )
    captured = api.capture_native(
        SimpleNamespace(for_variant=lambda _name: [conflicting])
    )
    assert captured.assignments["R1"] is None
    assert "conflict" in captured.warnings[0]


@pytest.mark.parametrize("reference", ["", " ", "R1"])
def test_board_capture_rejects_empty_or_duplicate_references(
    api: ModuleType, reference: str
) -> None:
    with pytest.raises(ValueError, match="reference"):
        api.capture_board(
            board_with(Footprint({"LCSC": "C100"}), Footprint({}, reference))
        )


@pytest.mark.parametrize("capability", ["GetFields", "GetAttributes", "GetReference"])
def test_board_read_failure_cannot_return_a_partial_snapshot(
    api: ModuleType, capability: str
) -> None:
    broken = Footprint({"LCSC": "C200"}, "R2")

    def fail() -> Any:
        raise RuntimeError("read failed")

    setattr(broken, capability, fail)
    with pytest.raises(ValueError, match="read failed"):
        api.capture_board(board_with(Footprint({"LCSC": "C100"}), broken))


@pytest.mark.parametrize(
    "malformed",
    [
        "named",
        "duplicate",
        "empty",
        "missing-capability",
        "missing-status",
        "unknown-status",
        "metadata-alias",
    ],
)
def test_native_capture_rejects_malformed_sources(
    api: ModuleType, malformed: str
) -> None:
    state = (
        native.VariantNativeAdapter(Board(), "capture").snapshot().for_variant("")[0]
    )
    states = [state]
    if malformed == "named":
        states = [replace(state, variant_name="A")]
    elif malformed == "duplicate":
        states.append(state)
    elif malformed == "empty":
        states = [replace(state, reference="")]
    elif malformed == "missing-capability":
        states = [SimpleNamespace(reference="R1")]
    elif malformed == "missing-status":
        states = [replace(state, assignment=SimpleNamespace(aliases=()))]
    elif malformed == "unknown-status":
        states = [replace(state, assignment=replace(state.assignment, status="unread"))]
    else:
        alias = replace(state.assignment.aliases[0], name="Manufacturer", text="")
        states = [
            replace(
                state,
                assignment=replace(state.assignment, status="empty", aliases=(alias,)),
            )
        ]
    with pytest.raises(ValueError):
        api.capture_native(SimpleNamespace(for_variant=lambda _name: states))


@pytest.mark.parametrize(
    "assignments", [{"R1": "garbage"}, {"R1": " c100 "}, {"": "C100"}]
)
def test_public_snapshot_rejects_unvalidated_assignment_values(
    api: ModuleType, assignments: dict[str, str]
) -> None:
    with pytest.raises(ValueError):
        api.DefaultSchematicSnapshot(
            assignments, ({"reference": "R1", "exclude_from_bom": False},), ()
        )


def test_public_snapshot_requires_consistent_bom_inventory(api: ModuleType) -> None:
    with pytest.raises(ValueError, match="reference"):
        api.DefaultSchematicSnapshot(
            {"R1": None}, ({"reference": "R2", "exclude_from_bom": False},), ()
        )
