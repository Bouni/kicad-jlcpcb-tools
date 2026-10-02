"""Keep one Default assignment snapshot authoritative throughout schematic export."""

from collections.abc import Iterable, Iterator
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any

import pytest

from .wx_harness import load_siblings, module

ROOT_FIRST = "11111111-1111-1111-1111-111111111111"
ROOT_SECOND = "22222222-2222-2222-2222-222222222222"
SYMBOL = "33333333-3333-3333-3333-333333333333"
FIRST_SHEET = "44444444-4444-4444-4444-444444444444"
SECOND_SHEET = "55555555-5555-5555-5555-555555555555"
CHILD_ROOT = "66666666-6666-6666-6666-666666666666"


class AssignmentStore:
    """Expose named-variant data that must never enter a base export."""

    def __init__(self) -> None:
        self.variant_name = "A"
        self.read_count = 0

    def read_all(self) -> Iterable[dict[str, Any]]:
        """Record any accidental read of the active variant's store view."""
        self.read_count += 1
        return [_part("R1", lcsc="C999", variant_name="A")]


class Footprint:
    """Retain live native fields and count each Default field read."""

    def __init__(self, reference: str, root: str) -> None:
        self.reference = reference
        self.fields = {"LCSC": "C123"}
        self.field_reads = 0
        self.attributes = 0
        self.schematic_path = f"/{root}/{SYMBOL}"
        self.m_Uuid = SimpleNamespace(AsString=lambda: f"footprint-{reference}")

    def GetReference(self) -> str:
        """Return the native display label."""
        return self.reference

    def GetPath(self) -> Any:
        """Return the persisted footprint-to-symbol identity link."""
        return SimpleNamespace(AsString=lambda: self.schematic_path)

    def GetFields(self) -> list[Any]:
        """Read the real current fields rather than record setter calls."""
        self.field_reads += 1
        return [
            SimpleNamespace(GetName=lambda: "LCSC", GetText=lambda: self.fields["LCSC"])
        ]

    def GetAttributes(self) -> int:
        """Return the live BOM/POS flags."""
        return self.attributes


class Board:
    """Change subsequent native reads to expose mixed export snapshots."""

    def __init__(self) -> None:
        self.footprints = [Footprint("R1", ROOT_FIRST), Footprint("R2", ROOT_SECOND)]
        self.inventory_reads = 0

    def GetFootprints(self) -> list[Footprint]:
        """Return changed data on a second inventory read."""
        self.inventory_reads += 1
        if self.inventory_reads > 1:
            for footprint in self.footprints:
                footprint.fields["LCSC"] = "C456"
        return self.footprints


def _part(reference: str, **changes: Any) -> dict[str, Any]:
    """Supply complete Default identity and provenance for an explicit record."""
    root = ROOT_FIRST if reference == "R1" else ROOT_SECOND
    return {
        "component_id": f"footprint-{reference}",
        "schematic_path": f"/{root}/{SYMBOL}",
        "reference": reference,
        "lcsc": "C123",
        "assignment_status": "valid",
        "exclude_from_bom": False,
        "variant_name": "",
        **changes,
    }


def _schematic(version: str, root: str) -> str:
    """Use literal placed-symbol syntax for each supported format family."""
    if version.startswith("7."):
        return f'''(kicad_sch
  (uuid "{root}")
  (symbol (lib_id "Device:R")
    (in_bom yes)
    (uuid "{SYMBOL}")
    (property "Reference" "R1" (at 0 0 0))
    (property "LCSC" "C100" (at 0 1 0))
    (pin "1" (uuid "test-pin"))
  )
)
'''
    return f'''(kicad_sch
  (uuid "{root}")
  (symbol
    (lib_id "Device:R")
    (in_bom yes)
    (uuid "{SYMBOL}")
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
'''


@pytest.fixture
def source(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[SimpleNamespace]:
    """Open production capture/export against live native state and two roots."""
    version = getattr(request, "param", "10.0.6")
    store = AssignmentStore()
    board = Board()
    paths = [tmp_path / f"{name}.kicad_sch" for name in ("first", "second")]
    originals = [_schematic(version, root) for root in (ROOT_FIRST, ROOT_SECOND)]
    for path, original in zip(paths, originals):
        path.write_text(original, encoding="utf-8")
    parent = SimpleNamespace(
        store=store,
        board=board,
        board_name="board.kicad_pcb",
        project_path=str(tmp_path),
    )
    with load_siblings(
        "_schematic_variant_tests",
        ("schematicexport", "schematic_snapshot"),
        {"pcbnew": module("pcbnew", GetBuildVersion=lambda: version)},
    ) as loaded:
        yield SimpleNamespace(
            exporter=loaded["schematicexport"].SchematicExport(parent),
            capture=loaded["schematic_snapshot"].capture_board,
            store=store,
            board=board,
            version=version,
            paths=paths,
            originals=originals,
        )


@pytest.mark.parametrize(
    "invalid",
    [
        "argument",
        "first-row",
        "later-row",
        "component_id",
        "schematic_path",
        "assignment_status",
        "reference",
        "lcsc",
        "exclude_from_bom",
    ],
)
def test_invalid_sources_preserve_every_original_and_backup(
    source: SimpleNamespace, invalid: str
) -> None:
    """Validate explicit contracts and variants before reading or writing files."""
    backups = [path.with_name(path.name + "_old") for path in source.paths]
    backups[0].write_bytes(b"retained backup")
    parts = [_part("R1"), _part("R2")]
    arguments: dict[str, Any] = {"parts": parts}
    if invalid == "argument":
        arguments = {"variant_name": "A"}
    elif invalid in ("first-row", "later-row"):
        index = 0 if invalid == "first-row" else 1
        parts[index]["variant_name"] = "A"
    else:
        del parts[1][invalid]

    with pytest.raises(ValueError, match="Default"):
        source.exporter.load_schematic(map(str, source.paths), **arguments)

    assert source.store.read_count == 0
    assert source.board.inventory_reads == 0
    assert all(
        path.read_text(encoding="utf-8") == original
        for path, original in zip(source.paths, source.originals)
    )
    assert backups[0].read_bytes() == b"retained backup"
    assert not backups[1].exists()


@pytest.mark.parametrize("source", ["7.0.11", "10.0.6"], indirect=True)
@pytest.mark.parametrize(
    "explicit", [False, True], ids=["native-default", "explicit-base"]
)
def test_default_source_is_captured_once_for_all_files(
    source: SimpleNamespace, explicit: bool
) -> None:
    """Named active views cannot influence a single authoritative Default capture."""
    source.exporter.load_schematic(
        map(str, source.paths), parts=[_part("R1"), _part("R2")] if explicit else None
    )
    assert source.store.read_count == 0
    assert source.board.inventory_reads == (0 if explicit else 1)
    assert [footprint.field_reads for footprint in source.board.footprints] == [
        0 if explicit else 1,
        0 if explicit else 1,
    ]
    for path, original in zip(source.paths, source.originals):
        written = path.read_text(encoding="utf-8")
        assert '(property "LCSC" "C123"' in written
        assert "C999" not in written and "C456" not in written
        assert (
            path.with_name(path.name + "_old").read_text(encoding="utf-8") == original
        )


def test_explicit_snapshot_is_detached_from_later_board_edits(
    source: SimpleNamespace,
) -> None:
    """A captured Default snapshot retains its fields and links for the whole save."""
    snapshot = source.capture(source.board)
    for footprint in source.board.footprints:
        footprint.fields["LCSC"] = "C456"
        footprint.schematic_path = "/unrelated/symbol"
        footprint.reference = "RENAMED"

    source.exporter.load_schematic(map(str, source.paths), snapshot=snapshot)

    assert source.board.inventory_reads == 1
    assert source.store.read_count == 0
    for path in source.paths:
        written = path.read_text(encoding="utf-8")
        assert '(property "LCSC" "C123"' in written
        assert "C456" not in written


def test_native_default_uses_the_guarded_current_board(source: SimpleNamespace) -> None:
    """Use the verified board handle once when the window owns a context guard."""
    stale_board = Board()
    for footprint in stale_board.footprints:
        footprint.fields["LCSC"] = "C999"
    source.exporter.parent.board = stale_board
    guard_calls = []

    def current_board() -> Board:
        """Return the owning window's verified board identity."""
        guard_calls.append(True)
        return source.board

    source.exporter.parent._get_current_board = current_board

    source.exporter.load_schematic(map(str, source.paths))

    assert guard_calls == [True]
    assert stale_board.inventory_reads == 0
    assert source.board.inventory_reads == 1
    for path in source.paths:
        assert '(property "LCSC" "C123"' in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("source", ["7.0.11", "10.0.6"], indirect=True)
@pytest.mark.parametrize(
    "secondary,expected_bom,expected_lcsc",
    [
        ("excluded", "no", "C123"),
        ("included", "yes", "C123"),
        ("missing", "no", "C100"),
        ("absent", "yes", "C100"),
    ],
)
def test_base_bom_observes_all_instances_and_preserves_variant_fields(
    source: SimpleNamespace, secondary: str, expected_bom: str, expected_lcsc: str
) -> None:
    """Shared child decisions preserve native variant overrides byte for byte."""
    root = source.paths[0]
    child = root.with_name("child.kicad_sch")
    root.write_text(
        f'''(kicad_sch
  (uuid "{ROOT_FIRST}")
  (sheet (uuid "{FIRST_SHEET}") (property "Sheetfile" "child.kicad_sch"))
  (sheet (uuid "{SECOND_SHEET}") (property "Sheetfile" "child.kicad_sch"))
)
''',
        encoding="utf-8",
    )
    overrides = """    (instances
      (project "old-project-name"
        (path "/stale-path"
          (reference "STALE_REFERENCE")
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
      )
    )
"""
    original = _schematic(source.version, CHILD_ROOT)
    original = original.rsplit("  )\n)\n", 1)[0] + overrides + "  )\n)\n"
    child.write_text(original, encoding="utf-8")
    parts = [
        _part(
            "R1",
            exclude_from_bom=True,
            schematic_path=f"/{ROOT_FIRST}/{FIRST_SHEET}/{SYMBOL}",
        )
    ]
    if secondary != "absent":
        parts.append(
            _part(
                "R2",
                lcsc="" if secondary == "missing" else "C123",
                assignment_status="missing" if secondary == "missing" else "valid",
                exclude_from_bom=secondary != "included",
                schematic_path=f"/{ROOT_FIRST}/{SECOND_SHEET}/{SYMBOL}",
            )
        )

    outcome = source.exporter.load_schematic([str(root)], variant_name="", parts=parts)

    written = child.read_text(encoding="utf-8")
    assert source.store.read_count == 0
    assert f'(property "LCSC" "{expected_lcsc}"' in written
    assert re.findall(r"^\s*\(in_bom\s+(yes|no)\)", written, re.MULTILINE) == [
        expected_bom,
        "no",
    ]
    assert overrides in written
    assert bool(outcome.diagnostics) == (secondary != "excluded")


@pytest.mark.parametrize("captured", [False, True])
def test_export_rejects_replaced_board_before_native_read_or_file_write(
    source: SimpleNamespace, captured: bool
) -> None:
    """Closing a board while the file dialog is open cannot export its stale data."""

    def changed_board() -> None:
        """Model the owning window's stale native board guard."""
        raise RuntimeError("Board context changed; reopen the plugin")

    source.exporter.parent._get_current_board = changed_board
    with pytest.raises(RuntimeError, match="Board context changed"):
        source.exporter.load_schematic(
            [str(path) for path in source.paths],
            parts=[_part("R1"), _part("R2")] if captured else None,
        )

    assert source.store.read_count == 0
    assert source.board.inventory_reads == 0
    for path, original in zip(source.paths, source.originals):
        assert path.read_text(encoding="utf-8") == original
        assert not path.with_name(path.name + "_old").exists()
