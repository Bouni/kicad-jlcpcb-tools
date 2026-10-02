"""Capture identity and explicit-clear provenance without store or catalog reads."""

from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from .wx_harness import load_siblings, module


class NativeFootprint:
    """Stateful native field/flag/path double with observable read counts."""

    def __init__(self, fields: dict[str, str], identity: str = "pcb-1") -> None:
        self.fields = fields
        self.attributes = 8
        self.path = "/root/sheet/symbol"
        self.reads: dict[str, int] = {}
        self.m_Uuid = SimpleNamespace(AsString=lambda: identity)

    def GetFields(self) -> list[Any]:
        """Return field accessors against mutable native state."""
        self.reads["fields"] = self.reads.get("fields", 0) + 1
        return [
            SimpleNamespace(
                GetName=lambda name=name: name,
                GetText=lambda name=name: self.fields[name],
            )
            for name in self.fields
        ]

    def GetReference(self) -> str:
        """Use a duplicate-able label to prevent accidental reference identity."""
        return "R1"

    def GetPath(self) -> Any:
        """Expose the current native symbol linkage once."""
        self.reads["path"] = self.reads.get("path", 0) + 1
        return SimpleNamespace(AsString=lambda: self.path)

    def GetAttributes(self) -> int:
        """Return native Default attributes, independent of named UI selection."""
        self.reads["attributes"] = self.reads.get("attributes", 0) + 1
        return self.attributes


@pytest.mark.parametrize(
    "fields,status,value",
    [
        ({}, "missing", None),
        ({"LCSC": ""}, "empty", ""),
        ({"LCSC": " C100 "}, "valid", "C100"),
        ({"LCSC": "garbage"}, "invalid", None),
        ({"LCSC": "", "LCSC PartNr": "C200"}, "conflict", None),
        ({"LCSC": "C100", "JLCPCB Part #": "C200"}, "conflict", None),
        ({"LCSC": "C100", "JLCPCB Part #": "   "}, "valid", None),
    ],
)
def test_native_capture_detaches_identity_fields_and_bom_once(
    fields: dict[str, str],
    status: str,
    value: Any,
) -> None:
    """Missing and whitespace cannot masquerade as an explicit clear or value."""
    footprint = NativeFootprint(fields)
    with load_siblings("_snapshot_tests", ("schematic_snapshot",), {}) as modules:
        module = modules["schematic_snapshot"]
        snapshot = module.capture_board(
            SimpleNamespace(GetFootprints=lambda: [footprint])
        )
        record = snapshot.parts[0]
        assert (record.assignment.status, record.value) == (status, value)
        assert record.schematic_path == "/root/sheet/symbol"
        assert record.exclude_from_bom is True
        assert footprint.reads == {"fields": 1, "path": 1, "attributes": 1}
        footprint.fields.clear()
        footprint.attributes = 0
        footprint.path = "/other"
        assert (record.assignment.status, record.value) == (status, value)
        assert record.schematic_path == "/root/sheet/symbol"
        assert record.exclude_from_bom is True
        with pytest.raises(FrozenInstanceError):
            record.lcsc = "C999"


@pytest.mark.parametrize("alias", ["LCSCPartNr", "LCSC Part Nr", "JLCPCB PartNr"])
def test_historical_partnr_aliases_are_occupied_native_fields(alias: str) -> None:
    """An older supported field prevents migration from treating a part as missing."""
    with load_siblings("_snapshot_alias_tests", ("schematic_snapshot",), {}) as modules:
        module = modules["schematic_snapshot"]
        footprint = NativeFootprint({alias: "C123"})
        snapshot = module.capture_board(
            SimpleNamespace(GetFootprints=lambda: [footprint])
        )
        assert snapshot.parts[0].assignment.status == "valid"
        assert snapshot.parts[0].value == "C123"


def test_duplicate_references_keep_distinct_native_identities() -> None:
    """Stale annotation labels cannot make linked source capture ambiguous."""
    with load_siblings("_snapshot_duplicates", ("schematic_snapshot",), {}) as modules:
        module = modules["schematic_snapshot"]
        footprints = [NativeFootprint({"LCSC": "C123"}, f"pcb-{i}") for i in (1, 2)]
        snapshot = module.capture_board(
            SimpleNamespace(GetFootprints=lambda: footprints)
        )
        assert [part.component_id for part in snapshot.parts] == ["pcb-1", "pcb-2"]
        assert [part.reference for part in snapshot.parts] == ["R1", "R1"]


def test_live_whitespace_alias_preserves_schematic_and_explains_the_alias_problem(
    tmp_path: Path,
) -> None:
    """Raw native alias state survives export even when the resolved status is valid."""
    path = tmp_path / "board.kicad_sch"
    path.write_text(
        '(kicad_sch (uuid "root") (symbol (lib_id "Device:R") '
        '(uuid "symbol") (in_bom yes) (property "Reference" "R1") '
        '(property "LCSC" "C900")))',
        encoding="utf-8",
    )
    footprint = NativeFootprint({"LCSC": "C100", "JLCPCB Part #": "   "})
    footprint.path = "/root/symbol"
    board = SimpleNamespace(GetFootprints=lambda: [footprint])
    parent = SimpleNamespace(
        board=board,
        board_name="board.kicad_pcb",
        project_path=str(tmp_path),
    )
    with load_siblings(
        "_snapshot_whitespace_export",
        ("schematicexport",),
        {"pcbnew": module("pcbnew", GetBuildVersion=lambda: "10.0")},
    ) as modules:
        exporter = modules["schematicexport"].SchematicExport(parent)
        result = exporter.load_schematic([str(path)])
    assert '(property "LCSC" "C900")' in path.read_text(encoding="utf-8")
    assert "(in_bom no)" in path.read_text(encoding="utf-8")
    assert result.skipped == ("pcb-1",)
    assert not result.retirement_eligible
    assert any("pcb-1" in text and "whitespace" in text for text in result.diagnostics)
