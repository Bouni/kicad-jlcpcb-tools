"""Unit tests for schematic_safety module."""

import errno
import json
import os
from pathlib import Path
import re
import socket
import stat

import pytest

from schematic_safety import (
    SchematicLockedError,
    assert_schematics_not_locked,
    atomic_write_schematic,
    check_schematic_lock,
    collect_schematic_hierarchy,
    get_schematic_lock_path,
    resolve_project_schematic,
)


def test_lock_path_format() -> None:
    """Verify lock file path convention matches KiCad '~<basename>.lck'."""
    path = "/path/to/project/main.kicad_sch"
    expected = "/path/to/project/~main.kicad_sch.lck"
    assert get_schematic_lock_path(path) == expected


def test_check_schematic_lock_no_file(tmp_path: Path) -> None:
    """When no lockfile exists, check_schematic_lock returns None."""
    sch = tmp_path / "test.kicad_sch"
    sch.write_text("(kicad_sch)\n", encoding="utf-8")
    assert check_schematic_lock(str(sch)) is None


@pytest.mark.parametrize(
    "hostname",
    [socket.gethostname(), socket.gethostname().split(".")[0], "other-machine"],
    ids=["this-host", "this-host-short-name", "other-host"],
)
def test_kicad_lockfile_counts_as_locked(tmp_path: Path, hostname: str) -> None:
    """KiCad's lockfile blocks export whichever host wrote it.

    KiCad writes only the owner's username and hostname (wx drops everything
    after the first dot of the hostname), holds no OS lock on the file and
    records no process ID, so nothing shows that its session has ended.
    """
    sch = tmp_path / "test.kicad_sch"
    sch.write_text("(kicad_sch)\n", encoding="utf-8")
    record = {"hostname": hostname, "username": "alice"}
    (tmp_path / "~test.kicad_sch.lck").write_text(
        json.dumps(record, separators=(",", ":")), encoding="utf-8"
    )

    assert check_schematic_lock(str(sch)) == record
    with pytest.raises(
        SchematicLockedError, match=f"locked by alice@{re.escape(hostname)}"
    ):
        assert_schematics_not_locked([str(sch)])


def test_unreadable_lockfile_counts_as_locked(tmp_path: Path) -> None:
    """A lockfile that is not KiCad's JSON still blocks export."""
    sch = tmp_path / "test.kicad_sch"
    sch.write_text("(kicad_sch)\n", encoding="utf-8")
    (tmp_path / "~test.kicad_sch.lck").write_bytes(b"\xff\xfe not json")

    assert check_schematic_lock(str(sch)) is not None
    with pytest.raises(SchematicLockedError, match="unknown user@unknown host"):
        assert_schematics_not_locked([str(sch)])


def test_lock_error_names_the_lockfile(tmp_path: Path) -> None:
    """The error names the lockfile, which a crash may have left behind."""
    sch = tmp_path / "test.kicad_sch"
    error = SchematicLockedError(str(sch), {"username": "alice", "hostname": "mac"})

    assert "locked by alice@mac" in str(error)
    assert str(error).endswith(str(tmp_path / "~test.kicad_sch.lck"))


def test_resolve_project_schematic_exact_match(tmp_path: Path) -> None:
    """Detects <project_dir>/<board_stem>.kicad_sch when present."""
    board = "my_project.kicad_pcb"
    sch = tmp_path / "my_project.kicad_sch"
    sch.write_text("(kicad_sch)\n", encoding="utf-8")

    resolved = resolve_project_schematic(str(tmp_path), board)
    assert resolved == str(sch)


def test_resolve_project_schematic_ignores_another_boards_schematic(
    tmp_path: Path,
) -> None:
    """A lone schematic named for another board is not taken as this board's."""
    (tmp_path / "board_a.kicad_sch").write_text("(kicad_sch)\n", encoding="utf-8")

    assert resolve_project_schematic(str(tmp_path), "board_b.kicad_pcb") is None


def test_resolve_project_schematic_multiple_or_none(tmp_path: Path) -> None:
    """Returns None when no schematics or multiple ambiguous schematics exist."""
    assert resolve_project_schematic(str(tmp_path), "board.kicad_pcb") is None

    (tmp_path / "sch1.kicad_sch").write_text("(kicad_sch)\n", encoding="utf-8")
    (tmp_path / "sch2.kicad_sch").write_text("(kicad_sch)\n", encoding="utf-8")
    assert resolve_project_schematic(str(tmp_path), "board.kicad_pcb") is None


def test_collect_schematic_hierarchy(tmp_path: Path) -> None:
    """Recursively collects all subsheets referenced via Sheetfile."""
    root = tmp_path / "root.kicad_sch"
    sub1 = tmp_path / "sub1.kicad_sch"
    sub2 = tmp_path / "sub2.kicad_sch"

    root.write_text(
        '(kicad_sch\n  (sheet (property "Sheetfile" "sub1.kicad_sch"))\n)\n',
        encoding="utf-8",
    )
    sub1.write_text(
        '(kicad_sch\n  (sheet (property "Sheetfile" "sub2.kicad_sch"))\n)\n',
        encoding="utf-8",
    )
    sub2.write_text("(kicad_sch)\n", encoding="utf-8")

    hierarchy = collect_schematic_hierarchy(str(root))
    assert hierarchy == [
        str(root.resolve()),
        str(sub1.resolve()),
        str(sub2.resolve()),
    ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_atomic_write_schematic(tmp_path: Path) -> None:
    """Creates backup file and atomically replaces destination, preserving mode."""
    sch = tmp_path / "test.kicad_sch"
    sch.write_text("original content\n", encoding="utf-8")
    sch.chmod(0o640)
    os.utime(sch, (1_000_000_000, 1_000_000_000))

    atomic_write_schematic(str(sch), "new content\n", make_backup=True)

    assert sch.read_text(encoding="utf-8") == "new content\n"
    backup = tmp_path / "test.kicad_sch_old"
    assert backup.read_text(encoding="utf-8") == "original content\n"
    assert stat.S_IMODE(sch.stat().st_mode) == 0o640
    assert stat.S_IMODE(backup.stat().st_mode) == 0o640
    assert backup.stat().st_mtime == 1_000_000_000
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "test.kicad_sch",
        "test.kicad_sch_old",
    ]


def test_failed_backup_leaves_schematic_unchanged(tmp_path: Path) -> None:
    """The schematic is not replaced when its backup cannot be written."""
    sch = tmp_path / "test.kicad_sch"
    sch.write_text("original content\n", encoding="utf-8")
    # A backup file cannot be renamed over a directory.
    (tmp_path / "test.kicad_sch_old").mkdir()

    with pytest.raises(OSError):
        atomic_write_schematic(str(sch), "new content\n", make_backup=True)

    assert sch.read_text(encoding="utf-8") == "original content\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "test.kicad_sch",
        "test.kicad_sch_old",
    ]


def test_interrupted_backup_keeps_the_previous_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backup that fails part-way leaves the earlier backup and the schematic."""
    sch = tmp_path / "test.kicad_sch"
    sch.write_text("original content\n", encoding="utf-8")
    backup = tmp_path / "test.kicad_sch_old"
    backup.write_text("previous backup\n", encoding="utf-8")

    def disk_full(fd: int) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "fsync", disk_full)
    with pytest.raises(OSError, match="No space left on device"):
        atomic_write_schematic(str(sch), "new content\n", make_backup=True)

    assert backup.read_text(encoding="utf-8") == "previous backup\n"
    assert sch.read_text(encoding="utf-8") == "original content\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "test.kicad_sch",
        "test.kicad_sch_old",
    ]


def test_collect_schematic_hierarchy_cyclic(tmp_path: Path) -> None:
    """Cyclic subsheet references terminate safely and deduplicate."""
    root = tmp_path / "root.kicad_sch"
    sub = tmp_path / "sub.kicad_sch"
    root.write_text(
        '(kicad_sch\n  (sheet (property "Sheetfile" "sub.kicad_sch"))\n)\n',
        encoding="utf-8",
    )
    sub.write_text(
        '(kicad_sch\n  (sheet (property "Sheetfile" "root.kicad_sch"))\n)\n',
        encoding="utf-8",
    )

    hierarchy = collect_schematic_hierarchy(str(root))
    assert hierarchy == [str(root.resolve()), str(sub.resolve())]


def test_collect_schematic_hierarchy_missing_file(tmp_path: Path) -> None:
    """A reference to a missing subsheet stops the walk and names the sheet."""
    root = tmp_path / "root.kicad_sch"
    root.write_text(
        '(kicad_sch\n  (sheet (property "Sheetfile" "nonexistent.kicad_sch"))\n)\n',
        encoding="utf-8",
    )

    with pytest.raises(
        FileNotFoundError,
        match="Sheet file 'nonexistent.kicad_sch' used in 'root.kicad_sch'",
    ):
        collect_schematic_hierarchy(str(root))


def test_collect_schematic_hierarchy_nested_folders(tmp_path: Path) -> None:
    """Sheet paths resolve against the folder of the sheet that uses them."""
    root = tmp_path / "root.kicad_sch"
    power = tmp_path / "blocks" / "power.kicad_sch"
    regulator = tmp_path / "blocks" / "regulator.kicad_sch"
    power.parent.mkdir()
    root.write_text(
        '(kicad_sch\n  (sheet (property "Sheetfile" "blocks/power.kicad_sch"))\n)\n',
        encoding="utf-8",
    )
    power.write_text(
        '(kicad_sch\n  (sheet (property "Sheetfile" "regulator.kicad_sch"))\n)\n',
        encoding="utf-8",
    )
    regulator.write_text("(kicad_sch)\n", encoding="utf-8")

    assert collect_schematic_hierarchy(str(root)) == [
        str(root.resolve()),
        str(power.resolve()),
        str(regulator.resolve()),
    ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_atomic_write_schematic_new_file_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new file gets the umask's default mode without os.umask being called.

    os.umask changes the mask for the whole process, including worker threads.
    """

    def process_wide_umask(mask: int) -> int:
        raise AssertionError("os.umask changes the mask for every thread")

    set_umask = os.umask
    previous = set_umask(0o027)
    monkeypatch.setattr(os, "umask", process_wide_umask)
    sch = tmp_path / "new_sch.kicad_sch"
    try:
        atomic_write_schematic(str(sch), "new file\n", make_backup=False)
    finally:
        set_umask(previous)

    assert sch.read_text(encoding="utf-8") == "new file\n"
    assert stat.S_IMODE(sch.stat().st_mode) == 0o640
