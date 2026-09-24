"""Keep one Default assignment snapshot authoritative throughout schematic export."""

from collections.abc import Iterable, Iterator
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any

import pytest

from .schematic_export_test_support import snapshot_from_parts
from .wx_harness import load_siblings, module


class AssignmentStore:
    """A stale named cache must never supply schematic assignments."""

    def __init__(self) -> None:
        self.variant_name = ""
        self.parts = [_part("R1")]
        self.read_count = 0

    def read_all(self) -> Iterable[dict[str, Any]]:
        """Fail loudly if either explicit or ordinary export consults the cache."""
        self.read_count += 1
        raise AssertionError("Schematic export read the assignment cache")


class LiveBoard:
    """Expose current fields through native-shaped stateful footprint objects."""

    def __init__(self) -> None:
        self.parts = [_part("R1")]
        self.read_count = 0
        self.unavailable = False

    def GetFootprints(self) -> list[SimpleNamespace]:
        """Reveal a second board capture by changing its assignment."""
        self.read_count += 1
        if self.unavailable:
            raise RuntimeError("PCB unavailable")
        if self.read_count > 1:
            self.parts[0]["fields"] = {"LCSC": "C456"}
        return [
            SimpleNamespace(
                GetReference=lambda part=row: part["reference"],
                GetAttributes=lambda part=row: part.get(
                    "attributes", 8 if part["exclude_from_bom"] else 0
                ),
                GetFields=lambda part=row: [
                    SimpleNamespace(
                        GetName=lambda key=name: key,
                        GetText=lambda content=value: content,
                    )
                    for name, value in part["fields"].items()
                ],
            )
            for row in self.parts
        ]


def _part(reference: str, **changes: Any) -> dict[str, Any]:
    """Supply live base fields and flags, with explicit exceptional cases."""
    return {
        "reference": reference,
        "lcsc": "C123",
        "exclude_from_bom": False,
        "fields": {"LCSC": changes.get("lcsc", "C123")},
        **changes,
    }


def _schematic(version: str) -> str:
    """Use literal placed-symbol syntax for each supported format family."""
    if version.startswith("7."):
        return """(kicad_sch
  (symbol (lib_id "Device:R")
    (property "Reference" "R1" (at 0 0 0))
    (property "LCSC" "C100" (at 0 1 0))
    (pin "1" (uuid "test-pin"))
  )
)
"""
    return """(kicad_sch
  (symbol
    (lib_id "Device:R")
    (property "Reference" "R1"
      (at 0 0 0)
    )
    (property "LCSC" "C100"
      (at 0 1 0)
    )
    (pin "1"
      (uuid "test-pin")
    )
  )
)
"""


@pytest.fixture
def source(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[SimpleNamespace]:
    """Open the production exporter with a changing board and protected files."""
    version = getattr(request, "param", "10.0.6")
    store = AssignmentStore()
    board = LiveBoard()
    paths = [tmp_path / f"{name}.kicad_sch" for name in ("first", "second")]
    original = _schematic(version)
    for path in paths:
        path.write_text(original, encoding="utf-8")
    parent = SimpleNamespace(
        store=store,
        board_name="board.kicad_pcb",
        project_path=str(tmp_path),
        pcbnew=SimpleNamespace(GetBoard=lambda: board),
    )
    with load_siblings(
        "_schematic_variant_tests",
        ("schematicexport",),
        {"pcbnew": module("pcbnew", GetBuildVersion=lambda: version)},
    ) as loaded:
        yield SimpleNamespace(
            exporter=loaded["schematicexport"].SchematicExport(parent),
            store=store,
            board=board,
            snapshot=lambda parts: snapshot_from_parts(
                loaded["schematicexport"].capture_board, parts
            ),
            paths=paths,
            original=original,
        )


@pytest.mark.parametrize(
    "invalid",
    [
        "named-output",
        "unvalidated-snapshot",
        "unavailable-board",
        "first-reference",
        "later-reference",
        "duplicate-reference",
        "invalid-field",
        "invalid-attributes",
    ],
)
@pytest.mark.parametrize("source", ["7.0.11", "10.0.6"], indirect=True)
def test_invalid_sources_preserve_every_original_and_backup(
    source: SimpleNamespace, invalid: str
) -> None:
    """Capture all native data before any format branch alters originals/backups."""
    backups = [path.with_name(path.name + "_old") for path in source.paths]
    backups[0].write_bytes(b"retained backup")
    source.board.parts = [_part("R1"), _part("R2")]
    arguments: dict[str, Any] = {}
    if invalid == "named-output":
        arguments = {"variant_name": "A"}
    elif invalid == "unvalidated-snapshot":
        arguments = {"snapshot": {"R1": "C999"}}
    elif invalid == "unavailable-board":
        source.board.unavailable = True
    elif invalid in ("first-reference", "later-reference"):
        index = 0 if invalid == "first-reference" else 1
        source.board.parts[index]["reference"] = ""
    elif invalid == "duplicate-reference":
        source.board.parts[1]["reference"] = "R1"
    elif invalid == "invalid-field":
        source.board.parts[1]["fields"]["LCSC"] = None
    elif invalid == "invalid-attributes":
        source.board.parts[1]["attributes"] = None

    error = TypeError if invalid == "unvalidated-snapshot" else ValueError
    with pytest.raises(error, match="Default"):
        source.exporter.load_schematic(map(str, source.paths), **arguments)

    assert source.store.read_count == 0
    assert all(
        path.read_text(encoding="utf-8") == source.original for path in source.paths
    )
    assert backups[0].read_bytes() == b"retained backup"
    assert not backups[1].exists()


@pytest.mark.parametrize("source", ["7.0.11", "10.0.6"], indirect=True)
@pytest.mark.parametrize(
    "explicit", [False, True], ids=["ordinary-board", "explicit-base"]
)
def test_default_source_is_captured_once_for_all_files(
    source: SimpleNamespace, explicit: bool
) -> None:
    """All selected files use one live capture; explicit snapshots need no source."""
    source.store.variant_name = "A"
    source.store.parts[0]["lcsc"] = "C999"
    arguments = {}
    if explicit:
        parts = [_part("R1")]
        arguments["snapshot"] = source.snapshot(parts)
        parts[0]["fields"]["LCSC"] = "C999"
        parts[0]["exclude_from_bom"] = True
        source.board.unavailable = True
    source.exporter.load_schematic(map(str, source.paths), **arguments)
    assert source.board.read_count == (0 if explicit else 1)
    assert source.store.read_count == 0
    if explicit:
        snapshot = arguments["snapshot"]
        assert snapshot.assignments == {"R1": "C123"}
        assert snapshot.bom_parts[0]["exclude_from_bom"] is False
    for path in source.paths:
        written = path.read_text(encoding="utf-8")
        assert '(property "LCSC" "C123"' in written
        assert "C999" not in written and "C456" not in written
        assert (
            path.with_name(path.name + "_old").read_text(encoding="utf-8")
            == source.original
        )


@pytest.mark.parametrize("source", ["7.0.11", "10.0.6"], indirect=True)
@pytest.mark.parametrize(
    "secondary,expected_bom",
    [
        ("excluded", "no"),
        ("included", "yes"),
        ("missing", "yes"),
        ("conflict", "no"),
    ],
)
def test_base_bom_observes_all_instances_and_preserves_variant_fields(
    source: SimpleNamespace, secondary: str, expected_bom: str
) -> None:
    """Textual export preserves native DNP, BOM, assigned and cleared field overrides."""
    path = source.paths[0]
    base = source.original.replace(
        '    (property "Reference"',
        '    (in_bom yes)\n    (uuid "symbol-uuid")\n    (property "Reference"',
        1,
    )
    overrides = """    (instances
      (project "board"
        (path "/first"
          (reference "R1")
          (unit 1)
          (variant (name "A")
            (in_bom no)
            (dnp yes)
            (field (name "LCSC") (value "C999"))
          )
          (variant (name "Cleared")
            (field (name "LCSC") (value ""))
          )
        )
        (path "/second" (reference "R2") (unit 1))
      )
    )
"""
    base = base.rsplit("  )\n)\n", 1)[0] + overrides + "  )\n)\n"
    path.write_text(base, encoding="utf-8")
    source.store.variant_name = "A"
    parts = [_part("R1", exclude_from_bom=True)]
    if secondary != "missing":
        parts.append(
            _part(
                "R2",
                lcsc="C456",
                exclude_from_bom=secondary in ("excluded", "conflict"),
            )
        )
    if secondary == "conflict":
        parts[1]["fields"]["JLCPCB"] = "C789"

    snapshot = source.snapshot(parts)
    if secondary == "conflict":
        assert snapshot.assignments["R2"] is None
    assert {part["reference"] for part in snapshot.bom_parts} == {
        part["reference"] for part in parts
    }
    source.exporter.load_schematic([str(path)], variant_name="", snapshot=snapshot)

    written = path.read_text(encoding="utf-8")
    assert source.store.read_count == 0
    assert '(property "LCSC" "C100"' in written
    assert re.findall(r"^\s*\(in_bom\s+(yes|no)\)", written, re.MULTILINE) == [
        expected_bom,
        "no",
    ]
    assert overrides in written
