"""Keep one Default assignment snapshot authoritative throughout schematic export."""

from collections.abc import Iterable, Iterator
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any

import pytest

from .wx_harness import load_siblings, module


class AssignmentStore:
    """Count reads and change subsequent data to expose mixed snapshots."""

    def __init__(self) -> None:
        self.variant_name = ""
        self.parts = [_part("R1")]
        self.parts[0].pop("variant_name")  # Ordinary stores predate per-row provenance.
        self.read_count = 0

    def read_all(self) -> Iterable[dict[str, Any]]:
        """Return newer data on a second read to expose mixed snapshots."""
        self.read_count += 1
        if self.read_count > 1:
            self.parts[0]["lcsc"] = "C456"
        return self.parts


def _part(reference: str, **changes: Any) -> dict[str, Any]:
    """Supply complete base provenance, with explicit exceptional fields per case."""
    return {
        "reference": reference,
        "lcsc": "C123",
        "exclude_from_bom": False,
        "variant_name": "",
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
    """Open the production exporter with a changing store and two protected files."""
    version = getattr(request, "param", "10.0.6")
    store = AssignmentStore()
    paths = [tmp_path / f"{name}.kicad_sch" for name in ("first", "second")]
    original = _schematic(version)
    for path in paths:
        path.write_text(original, encoding="utf-8")
    parent = SimpleNamespace(
        store=store, board_name="board.kicad_pcb", project_path=str(tmp_path)
    )
    with load_siblings(
        "_schematic_variant_tests",
        ("schematicexport",),
        {"pcbnew": module("pcbnew", GetBuildVersion=lambda: version)},
    ) as loaded:
        yield SimpleNamespace(
            exporter=loaded["schematicexport"].SchematicExport(parent),
            store=store,
            paths=paths,
            original=original,
        )


@pytest.mark.parametrize(
    "invalid",
    [
        "argument",
        "store",
        "first-row",
        "later-row",
        "reference",
        "lcsc",
        "exclude_from_bom",
    ],
)
def test_invalid_sources_preserve_every_original_and_backup(
    source: SimpleNamespace, invalid: str
) -> None:
    """Validate all source rows before any format branch reads or writes files."""
    backups = [path.with_name(path.name + "_old") for path in source.paths]
    backups[0].write_bytes(b"retained backup")
    parts = [_part("R1"), _part("R2")]
    arguments: dict[str, Any] = {"parts": parts}
    if invalid == "argument":
        arguments = {"variant_name": "A"}
    elif invalid == "store":
        source.store.variant_name = "B"
        arguments = {}
    elif invalid in ("first-row", "later-row"):
        index = 0 if invalid == "first-row" else 1
        parts[index]["variant_name"] = "A"
    else:
        del parts[1][invalid]

    with pytest.raises(ValueError, match="Default"):
        source.exporter.load_schematic(map(str, source.paths), **arguments)

    assert source.store.read_count == 0
    assert all(
        path.read_text(encoding="utf-8") == source.original for path in source.paths
    )
    assert backups[0].read_bytes() == b"retained backup"
    assert not backups[1].exists()


@pytest.mark.parametrize("source", ["7.0.11", "10.0.6"], indirect=True)
@pytest.mark.parametrize(
    "explicit", [False, True], ids=["ordinary-store", "explicit-base"]
)
def test_default_source_is_captured_once_for_all_files(
    source: SimpleNamespace, explicit: bool
) -> None:
    """Explicit base data ignores a named store; fallback reads ordinary data once."""
    if explicit:
        source.store.variant_name = "A"
        source.store.parts[0]["lcsc"] = "C999"
    source.exporter.load_schematic(
        map(str, source.paths), parts=[_part("R1")] if explicit else None
    )
    assert source.store.read_count == (0 if explicit else 1)
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
    [("excluded", "no"), ("included", "yes"), ("missing", "yes")],
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
        parts.append(_part("R2", lcsc="C456", exclude_from_bom=secondary == "excluded"))

    source.exporter.load_schematic([str(path)], variant_name="", parts=parts)

    written = path.read_text(encoding="utf-8")
    assert source.store.read_count == 0
    assert '(property "LCSC" "C123"' in written
    assert re.findall(r"^\s*\(in_bom\s+(yes|no)\)", written, re.MULTILINE) == [
        expected_bom,
        "no",
    ]
    assert overrides in written


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
            parts=[_part("R1")] if captured else None,
        )

    assert source.store.read_count == 0
    for path in source.paths:
        assert path.read_text(encoding="utf-8") == source.original
        assert not path.with_name(path.name + "_old").exists()
