"""Exercise schematic export against live board state and real SQLite history."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any

import pytest

from .wx_harness import load_siblings, module, wx_stubs


@dataclass
class _Field:
    """Expose stored native field text rather than merely recording setters."""

    name: str
    text: str
    visible: bool = True

    def GetName(self) -> str:
        """Return the field's original spelling."""
        return self.name

    def GetText(self) -> str:
        """Return the current value after any native mutation."""
        return self.text

    def SetVisible(self, visible: bool) -> None:
        """Retain visibility when the production helper creates a field."""
        self.visible = visible


class _Footprint:
    """Retain native state used by both the real Store and export capture."""

    def __init__(self, fields: Mapping[str, str], attributes: int = 0) -> None:
        self.fields = [_Field(name, text) for name, text in fields.items()]
        self.attributes = attributes
        self.m_Uuid = SimpleNamespace(
            AsString=lambda: "11111111-1111-4111-8111-111111111111"
        )

    def GetFields(self) -> list[_Field]:
        """Return fields with the actual values written by SetField."""
        return self.fields

    def SetField(self, name: str, value: str) -> None:
        """Mutate an existing field or create the requested native field."""
        for field in self.fields:
            if field.name == name:
                field.text = value
                return
        self.fields.append(_Field(name, value))

    def GetReference(self) -> str:
        """Return the board-to-schematic component reference."""
        return "R1"

    def GetValue(self) -> str:
        """Supply the component value required by real Store construction."""
        return "10k"

    def GetFPID(self) -> Any:
        """Supply the native library identifier required by real persistence."""
        return SimpleNamespace(GetLibItemName=lambda: "R_0603")

    def GetAttributes(self) -> int:
        """Return the actual exclusion flags."""
        return self.attributes


@pytest.fixture(params=[7, 10], ids=["kicad7", "kicad8+"])
def source(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[SimpleNamespace]:
    """Load real persistence, assignment helpers, and export with only APIs stubbed."""
    footprint = _Footprint({"LCSC": "C777"})
    board = SimpleNamespace(GetFootprints=lambda: [footprint])
    pcbnew = module(
        "pcbnew", GetBuildVersion=lambda: f"{request.param}.0.0", GetBoard=lambda: board
    )
    parent = SimpleNamespace(
        board_name="board.kicad_pcb",
        project_path=str(tmp_path),
        settings={"general": {"lcsc_priority": True}},
        pcbnew=pcbnew,
    )
    with load_siblings(
        "_schematic_boundary_tests",
        ("store", "footprint_helpers", "schematicexport"),
        {**wx_stubs(), "pcbnew": pcbnew},
    ) as loaded:
        yield SimpleNamespace(
            version=request.param,
            path=tmp_path / "board.kicad_sch",
            parent=parent,
            board=board,
            footprint=footprint,
            Store=loaded["store"].Store,
            helpers=loaded["footprint_helpers"],
            exporter=loaded["schematicexport"].SchematicExport(parent),
        )


def _schematic(version: int, fields: Mapping[str, str], comment: str = "") -> str:
    """Serialize one base symbol with native field ordering for each format."""
    start = (
        '  (symbol (lib_id "Device:R")\n'
        if version == 7
        else '  (symbol\n    (lib_id "Device:R")\n'
    )
    properties = []
    for name, text in {"Reference": "R1", **fields}.items():
        if version == 7:
            properties.append(f'    (property "{name}" "{text}" (at 0 1 0))\n')
        else:
            properties.append(
                f'    (property "{name}" "{text}"\n      (at 0 1 0)\n    )\n'
            )
    return (
        "(kicad_sch\n  (lib_symbols)\n"
        + start
        + comment
        + "    (in_bom yes)\n"
        + "".join(properties)
        + '    (pin "1" (uuid "pin-id"))\n  )\n)\n'
    )


def _fields(source: SimpleNamespace) -> dict[str, str]:
    """Read original and exported field values independently of assignment policy."""
    return dict(
        re.findall(
            r'\(property\s+"([^"]*)"\s+"([^"]*)"',
            source.path.read_text(encoding="utf-8"),
        )
    )


def _open_store(
    source: SimpleNamespace,
    fields: Mapping[str, str],
    *,
    existing: bool,
    board_priority: bool,
) -> None:
    """Open real SQLite state, optionally retaining a previously assigned part."""
    source.parent.settings["general"]["lcsc_priority"] = board_priority
    if existing:
        source.Store(source.parent, source.parent.project_path, source.board)
    source.footprint.fields = [_Field(name, text) for name, text in fields.items()]
    source.parent.store = source.Store(
        source.parent, source.parent.project_path, source.board
    )


@pytest.mark.parametrize("existing", [False, True], ids=["fresh", "reopened"])
@pytest.mark.parametrize("board_priority", [False, True])
@pytest.mark.parametrize(
    "board_fields",
    [
        {"LCSC": "C100", "JLCPCB": "C200"},
        {"LCSC": "", "JLCPCB": "C100"},
        {"LCSC": "invalid"},
        {},
    ],
    ids=["conflicting-aliases", "blank-canonical-conflict", "invalid", "missing"],
)
def test_unresolved_live_assignment_preserves_schematic_across_cache_history(
    source: SimpleNamespace,
    existing: bool,
    board_priority: bool,
    board_fields: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cache's empty or stale value cannot turn unresolved board data into a write."""
    _open_store(source, board_fields, existing=existing, board_priority=board_priority)
    original_fields = board_fields or {"JLCPCB Part Number": "C800"}
    source.path.write_text(
        _schematic(source.version, original_fields), encoding="utf-8"
    )

    source.exporter.load_schematic([str(source.path)])

    assert _fields(source) == {"Reference": "R1", **original_fields}
    assert {field.name: field.text for field in source.footprint.fields} == board_fields
    assert any("R1" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("existing", [False, True], ids=["fresh", "reopened"])
@pytest.mark.parametrize("board_priority", [False, True])
def test_export_captures_current_board_assignment_instead_of_sqlite_cache(
    source: SimpleNamespace, existing: bool, board_priority: bool
) -> None:
    """Export uses current native values even when an ordinary cache prefers history."""
    _open_store(
        source,
        {"JLCPCB Part Number": "C200"},
        existing=existing,
        board_priority=board_priority,
    )
    # A native edit after the last refresh makes every history/priority stale.
    source.footprint.SetField("JLCPCB Part Number", "C300")
    source.path.write_text(
        _schematic(source.version, {"JLCPCB Part Number": "C100"}), encoding="utf-8"
    )

    source.exporter.load_schematic([str(source.path)])

    assert _fields(source) == {"Reference": "R1", "JLCPCB Part Number": "C300"}


@pytest.mark.parametrize("board_priority", [False, True])
def test_native_clear_survives_store_reopen_and_schematic_export(
    source: SimpleNamespace, board_priority: bool
) -> None:
    """A real helper clear is retained by native fields when SQLite retains history."""
    _open_store(
        source,
        {"LCSC": "C200", "JLCPCB Part Number": "C200"},
        existing=True,
        board_priority=board_priority,
    )
    source.helpers.set_lcsc_value(source.footprint, "")
    assert {field.text for field in source.footprint.GetFields()} == {""}
    source.parent.store = source.Store(
        source.parent, source.parent.project_path, source.board
    )
    source.path.write_text(
        _schematic(source.version, {"LCSC": "C100", "JLCPCB Part Number": "C100"}),
        encoding="utf-8",
    )

    source.exporter.load_schematic([str(source.path)])

    assert _fields(source) == {
        "Reference": "R1",
        "LCSC": "",
        "JLCPCB Part Number": "",
    }


@pytest.mark.parametrize("separator", ["\u2028", "\x85", "\v"])
def test_unicode_separator_in_native_comment_does_not_capture_bom_update(
    source: SimpleNamespace, separator: str
) -> None:
    """Only physical newlines end a KiCad comment; its text must remain unchanged."""
    source.footprint.attributes = 8
    _open_store(source, {"LCSC": "C200"}, existing=False, board_priority=True)
    comment = f"    # comment {separator}(in_bom yes)\n"
    source.path.write_text(
        _schematic(source.version, {"LCSC": "C100"}, comment), encoding="utf-8"
    )

    source.exporter.load_schematic([str(source.path)])

    written = source.path.read_text(encoding="utf-8")
    assert comment in written
    assert "    (in_bom no)" in written.split("\n")
    assert _fields(source)["LCSC"] == "C200"
