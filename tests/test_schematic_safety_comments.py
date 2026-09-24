"""Keep backup discovery and child-lock preflight safe around native comments."""

import json
from pathlib import Path
import zipfile

import pytest

from schematic_safety import (
    SchematicLockedError,
    backup_schematics,
    collect_schematic_hierarchy,
)


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])
@pytest.mark.parametrize(
    "comment",
    [
        '  # (sheet (property "Sheetfile" "missing.kicad_sch"))',
        "  # ignored closing parentheses )))",
        '  # ignored opening parentheses ((( and an "unterminated quote',
    ],
    ids=["fake-sheet", "unbalanced-parentheses", "unclosed-quote"],
)
def test_backup_comments_preserve_complete_hierarchy_and_child_lock_retry(
    tmp_path: Path, newline: str, comment: str
) -> None:
    """A real child stays locked until approval and is archived with original bytes."""
    root = tmp_path / "board.kicad_sch"
    child = tmp_path / "child#literal.kicad_sch"
    root_bytes = newline.join(
        [
            "(kicad_sch",
            "  (sheet",
            comment,
            f'    (property "Sheetfile" "{child.name}")',
            "  )",
            ")",
            "",
        ]
    ).encode()
    child_bytes = (
        f'(kicad_sch{newline}  (text "résistance Ω"){newline}){newline}'.encode()
    )
    root.write_bytes(root_bytes)
    child.write_bytes(child_bytes)
    lock = tmp_path / f"~{child.name}.lck"
    lock.write_text(json.dumps({"hostname": "mac", "username": "alice"}))

    assert collect_schematic_hierarchy(str(root)) == [str(root), str(child)]
    with pytest.raises(SchematicLockedError, match="locked by alice@mac") as raised:
        backup_schematics(str(tmp_path), "board.kicad_pcb", "before.zip")

    approved = [path for path, _info in raised.value.locks]
    assert approved == [str(child)]
    assert root.read_bytes() == root_bytes
    assert child.read_bytes() == child_bytes
    assert not (tmp_path / "jlcpcb").exists()

    archive_path = backup_schematics(
        str(tmp_path), "board.kicad_pcb", "before.zip", approved_locks=approved
    )
    assert archive_path is not None
    with zipfile.ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == {root.name, child.name}
        assert archive.read(root.name) == root_bytes
        assert archive.read(child.name) == child_bytes
    assert root.read_bytes() == root_bytes
    assert child.read_bytes() == child_bytes
    assert not (tmp_path / "missing.kicad_sch").exists()
    assert not list(tmp_path.glob("*_old"))


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])
def test_comment_quote_cannot_hide_missing_real_child_from_backup(
    tmp_path: Path, newline: str
) -> None:
    """An unreadable hierarchy stops the permanent backup before creating a zip."""
    root = tmp_path / "board.kicad_sch"
    original = newline.join(
        [
            "(kicad_sch",
            "  (sheet",
            '    # ignored "unterminated quote',
            '    (property "Sheetfile" "real-missing.kicad_sch")',
            "  )",
            ")",
            "",
        ]
    ).encode()
    root.write_bytes(original)

    with pytest.raises(FileNotFoundError, match="real-missing.kicad_sch"):
        backup_schematics(str(tmp_path), "board.kicad_pcb", "before.zip")

    assert root.read_bytes() == original
    assert not (tmp_path / "jlcpcb").exists()
