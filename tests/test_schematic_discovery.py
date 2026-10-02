"""Native-only recovery requires proven absence, never a swallowed read error."""

import json
from pathlib import Path
from typing import Optional

import pytest

from schematic_discovery import discover_project_schematics


def test_standalone_board_has_confirmed_absence(tmp_path: Path) -> None:
    """Unrelated neighboring schematics do not create a project association."""
    result = discover_project_schematics(str(tmp_path), "board.kicad_pcb")
    assert result.status == "absent" and not result.paths and not result.diagnostics
    assert len(result.source_state) == 2
    (tmp_path / "unrelated.kicad_sch").write_text("unrelated", encoding="utf-8")
    assert discover_project_schematics(str(tmp_path), "board.kicad_pcb") == result


@pytest.mark.parametrize(
    "contents",
    ["bad json", "[]", '{"schematic": []}', '{"schematic": {"top_level_sheets": {}}}'],
)
def test_invalid_project_metadata_is_not_schematic_absence(
    tmp_path: Path, contents: str
) -> None:
    """Invalid metadata cannot grant native-only migration permission."""
    (tmp_path / "board.kicad_pro").write_text(contents, encoding="utf-8")
    result = discover_project_schematics(str(tmp_path), "board.kicad_pcb")
    assert result.status == "unresolved" and result.diagnostics


def test_declared_missing_root_is_not_absence(tmp_path: Path) -> None:
    """A missing declared file is a repairable problem, not a PCB-only project."""
    (tmp_path / "board.kicad_pro").write_text(
        json.dumps(
            {"schematic": {"top_level_sheets": [{"filename": "missing.kicad_sch"}]}}
        ),
        encoding="utf-8",
    )
    result = discover_project_schematics(str(tmp_path), "board.kicad_pcb")
    assert result.status == "unresolved" and not result.paths
    assert "missing.kicad_sch" in result.diagnostics[0]


def test_default_root_fallback_matches_kicad_and_is_fingerprinted(
    tmp_path: Path,
) -> None:
    """The default fallback remains usable and later source changes are detected."""
    (tmp_path / "board.kicad_pro").write_text(
        json.dumps(
            {"schematic": {"top_level_sheets": [{"filename": "missing.kicad_sch"}]}}
        ),
        encoding="utf-8",
    )
    root = tmp_path / "board.kicad_sch"
    root.write_text('(kicad_sch (uuid "root"))', encoding="utf-8")
    result = discover_project_schematics(str(tmp_path), "board.kicad_pcb")
    assert result.status == "present" and result.paths == (str(root),)
    assert not result.diagnostics
    root.write_text('(kicad_sch (uuid "changed"))', encoding="utf-8")
    assert (
        discover_project_schematics(str(tmp_path), "board.kicad_pcb").source_state
        != result.source_state
    )


def test_missing_one_of_multiple_roots_remains_unresolved(tmp_path: Path) -> None:
    """One readable root does not hide another declared missing root."""
    (tmp_path / "board.kicad_pro").write_text(
        json.dumps(
            {
                "schematic": {
                    "top_level_sheets": [
                        {"filename": "first.kicad_sch"},
                        {"filename": "missing.kicad_sch"},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "first.kicad_sch").write_text("root", encoding="utf-8")
    result = discover_project_schematics(str(tmp_path), "board.kicad_pcb")
    assert result.status == "unresolved" and len(result.paths) == 1


def test_unreadable_project_is_not_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read failures preserve recovery even when no root has been discovered."""
    original = Path.read_bytes

    def read(path: Path) -> bytes:
        if path.name == "board.kicad_pro":
            raise PermissionError("project unreadable")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    result = discover_project_schematics(str(tmp_path), "board.kicad_pcb")
    assert result.status == "unresolved"
    assert "unreadable" in result.diagnostics[0]


def test_authenticated_project_name_selects_the_associated_root(tmp_path: Path) -> None:
    """A renamed PCB still uses its authenticated project's schematic."""
    associated = tmp_path / "project.kicad_sch"
    associated.write_text("root", encoding="utf-8")
    (tmp_path / "board.kicad_sch").write_text("unrelated", encoding="utf-8")
    result = discover_project_schematics(str(tmp_path), "board.kicad_pcb", "project")
    assert result.status == "present" and result.paths == (str(associated),)


def test_unknown_project_identity_cannot_prove_absence(tmp_path: Path) -> None:
    """Unknown identity never authorizes native-only recovery."""
    assert discover_project_schematics(str(tmp_path), None).status == "unresolved"


def test_project_root_uuid_is_retained_for_native_path_resolution(
    tmp_path: Path,
) -> None:
    """KiCad 10's declared nonnil UUID overrides a root file's stored UUID."""
    declared = "11111111-2222-3333-4444-555555555555"
    (tmp_path / "board.kicad_pro").write_text(
        json.dumps(
            {
                "schematic": {
                    "top_level_sheets": [
                        {"filename": "root.kicad_sch", "uuid": declared}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    root = tmp_path / "root.kicad_sch"
    root.write_text(
        '(kicad_sch (uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"))', encoding="utf-8"
    )
    result = discover_project_schematics(str(tmp_path), "board.kicad_pcb")
    assert result.status == "present"
    assert result.root_uuids == ((str(root), declared),)


@pytest.mark.parametrize("declared", [None, "00000000-0000-0000-0000-000000000000"])
def test_missing_or_nil_project_uuid_preserves_file_identity(
    tmp_path: Path, declared: Optional[str]
) -> None:
    """Legacy projects and KiCad's nil marker keep the schematic's own root."""
    entry = {"filename": "board.kicad_sch"}
    if declared is not None:
        entry["uuid"] = declared
    (tmp_path / "board.kicad_pro").write_text(
        json.dumps({"schematic": {"top_level_sheets": [entry]}}), encoding="utf-8"
    )
    (tmp_path / "board.kicad_sch").write_text("root", encoding="utf-8")
    result = discover_project_schematics(str(tmp_path), "board.kicad_pcb")
    assert result.status == "present" and not result.root_uuids


def test_conflicting_declared_uuids_for_one_root_are_not_guessed(
    tmp_path: Path,
) -> None:
    """Conflicting project identities preserve recovery instead of selecting one."""
    (tmp_path / "board.kicad_pro").write_text(
        json.dumps(
            {
                "schematic": {
                    "top_level_sheets": [
                        {
                            "filename": "board.kicad_sch",
                            "uuid": "11111111-2222-3333-4444-555555555555",
                        },
                        {
                            "filename": "board.kicad_sch",
                            "uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "board.kicad_sch").write_text("root", encoding="utf-8")
    result = discover_project_schematics(str(tmp_path), "board.kicad_pcb")
    assert result.status == "unresolved" and result.diagnostics
