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
    resolve_project_schematics,
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
    error = SchematicLockedError([(str(sch), {"username": "alice", "hostname": "mac"})])

    assert "locked by alice@mac" in str(error)
    assert str(error).endswith(str(tmp_path / "~test.kicad_sch.lck"))


def _lock(sch: Path, username: str) -> None:
    """Write the lockfile KiCad writes for an open schematic."""
    (sch.parent / f"~{sch.name}.lck").write_text(
        json.dumps({"hostname": "mac", "username": username}), encoding="utf-8"
    )


def test_every_locked_schematic_is_reported(tmp_path: Path) -> None:
    """One error names every locked schematic, not just the first."""
    first, free, second = (tmp_path / f"{n}.kicad_sch" for n in ("a", "b", "c"))
    _lock(first, "alice")
    _lock(second, "bob")

    with pytest.raises(SchematicLockedError) as raised:
        assert_schematics_not_locked([str(first), str(free), str(second)])

    assert [path for path, _info in raised.value.locks] == [str(first), str(second)]
    message = str(raised.value)
    assert message.startswith("2 schematic files are locked.")
    assert f"'a.kicad_sch' by alice@mac: {tmp_path / '~a.kicad_sch.lck'}" in message
    assert f"'c.kicad_sch' by bob@mac: {tmp_path / '~c.kicad_sch.lck'}" in message


def test_approved_locks_are_not_reported_again(tmp_path: Path) -> None:
    """Only locks the user has not already approved are reported."""
    first, second = (str(tmp_path / f"{n}.kicad_sch") for n in ("a", "b"))
    _lock(tmp_path / "a.kicad_sch", "alice")
    _lock(tmp_path / "b.kicad_sch", "bob")

    with pytest.raises(SchematicLockedError) as raised:
        assert_schematics_not_locked([first, second], approved=[first])
    assert [path for path, _info in raised.value.locks] == [second]

    assert_schematics_not_locked([first, second], approved=[first, second])


def _project(tmp_path: Path, name: str, *top_level_sheets: str) -> None:
    """Write a KiCad 10 project file listing the given top-level sheets."""
    sheets = [
        {"filename": file_name, "name": Path(file_name).stem, "uuid": f"uuid-{index}"}
        for index, file_name in enumerate(top_level_sheets)
    ]
    (tmp_path / f"{name}.kicad_pro").write_text(
        json.dumps(
            {
                "meta": {"filename": f"{name}.kicad_pro"},
                "schematic": {"top_level_sheets": sheets},
            }
        ),
        encoding="utf-8",
    )


def _schematics(tmp_path: Path, *names: str) -> list[str]:
    """Create empty schematics and return their absolute paths."""
    paths = []
    for name in names:
        (tmp_path / name).write_text("(kicad_sch)\n", encoding="utf-8")
        paths.append(str(tmp_path / name))
    return paths


def test_resolve_project_schematics_without_a_sheet_list(tmp_path: Path) -> None:
    """A project without a top-level sheet list has <board_stem>.kicad_sch as root."""
    assert resolve_project_schematics(str(tmp_path), "my_project.kicad_pcb") == []

    expected = _schematics(tmp_path, "my_project.kicad_sch")
    assert resolve_project_schematics(str(tmp_path), "my_project.kicad_pcb") == expected

    _project(tmp_path, "my_project")  # KiCad 7 to 9 files have no list
    assert resolve_project_schematics(str(tmp_path), "my_project.kicad_pcb") == expected


def test_resolve_project_schematics_ignores_another_boards_schematic(
    tmp_path: Path,
) -> None:
    """A lone schematic named for another board is not taken as this board's."""
    _schematics(tmp_path, "board_a.kicad_sch")

    assert resolve_project_schematics(str(tmp_path), "board_b.kicad_pcb") == []


def test_resolve_project_schematics_reads_every_top_level_sheet(tmp_path: Path) -> None:
    """Every top-level sheet the KiCad 10 project lists is a root, in its order."""
    expected = _schematics(tmp_path, "board.kicad_sch", "power.kicad_sch")
    _project(tmp_path, "board", "board.kicad_sch", "power.kicad_sch")

    assert resolve_project_schematics(str(tmp_path), "board.kicad_pcb") == expected


def test_resolve_project_schematics_follows_the_project_not_the_file_name(
    tmp_path: Path,
) -> None:
    """The project's listed root wins over an unlisted <board_stem>.kicad_sch."""
    listed, _orphan = _schematics(tmp_path, "main.kicad_sch", "board.kicad_sch")
    _project(tmp_path, "board", "main.kicad_sch")

    assert resolve_project_schematics(str(tmp_path), "board.kicad_pcb") == [listed]


def test_resolve_project_schematics_handles_missing_sheets_like_kicad(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A missing listed sheet becomes <project>.kicad_sch if that exists, else is skipped."""
    (root,) = _schematics(tmp_path, "board.kicad_sch")
    _project(tmp_path, "board", "template.kicad_sch")
    assert resolve_project_schematics(str(tmp_path), "board.kicad_pcb") == [root]

    (tmp_path / "board.kicad_sch").unlink()
    (power,) = _schematics(tmp_path, "power.kicad_sch")
    _project(tmp_path, "board", "gone.kicad_sch", "power.kicad_sch")
    assert resolve_project_schematics(str(tmp_path), "board.kicad_pcb") == [power]
    assert (
        "Top-level sheet 'gone.kicad_sch' listed in board.kicad_pro does not exist"
        in (caplog.messages)
    )


def test_resolve_project_schematics_survives_a_damaged_project_file(
    tmp_path: Path,
) -> None:
    """An unreadable project file falls back to <board_stem>.kicad_sch."""
    expected = _schematics(tmp_path, "board.kicad_sch")
    (tmp_path / "board.kicad_pro").write_text("{not json", encoding="utf-8")

    assert resolve_project_schematics(str(tmp_path), "board.kicad_pcb") == expected


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


def test_metadata_copy_failure_is_logged_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A filesystem refusing chmod leaves the new contents in place and says so."""
    sch = tmp_path / "test.kicad_sch"
    sch.write_text("original content\n", encoding="utf-8")

    def refuse_chmod(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "chmod", refuse_chmod)
    atomic_write_schematic(str(sch), "new content\n", make_backup=True)

    assert sch.read_text(encoding="utf-8") == "new content\n"
    backup = tmp_path / "test.kicad_sch_old"
    assert backup.read_text(encoding="utf-8") == "original content\n"
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warned) == 2
    assert f"to '{backup}'" in warned[0]
    assert f"to '{sch}'" in warned[1]
    assert all("Operation not permitted" in message for message in warned)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "test.kicad_sch",
        "test.kicad_sch_old",
    ]


@pytest.mark.skipif(
    not hasattr(os, "chflags") or not hasattr(stat, "UF_IMMUTABLE"),
    reason="BSD file flags",
)
def test_locked_file_leaves_no_temporary_files(tmp_path: Path) -> None:
    """A schematic locked in Finder fails to write and leaves nothing behind."""
    sch = tmp_path / "test.kicad_sch"
    sch.write_text("original content\n", encoding="utf-8")
    os.chflags(sch, stat.UF_IMMUTABLE)
    try:
        with pytest.raises(PermissionError):
            atomic_write_schematic(str(sch), "new content\n", make_backup=True)
        leftovers = [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    finally:
        for path in tmp_path.iterdir():
            os.chflags(path, 0)

    assert leftovers == []
    assert sch.read_text(encoding="utf-8") == "original content\n"


def test_failed_replace_removes_a_read_only_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup still removes a temp file that took the schematic's read-only mode.

    Windows refuses to replace a read-only target or to delete a read-only
    file; both are simulated here.
    """
    sch = tmp_path / "test.kicad_sch"
    sch.write_text("original content\n", encoding="utf-8")
    sch.chmod(0o444)

    def refuse_replace(_source: str, _target: str) -> None:
        raise PermissionError(errno.EACCES, "Access is denied")

    remove = os.remove

    def windows_remove(path: str) -> None:
        if not os.access(path, os.W_OK):
            raise PermissionError(errno.EACCES, "Access is denied", path)
        remove(path)

    monkeypatch.setattr(os, "replace", refuse_replace)
    monkeypatch.setattr(os, "remove", windows_remove)
    try:
        with pytest.raises(PermissionError):
            atomic_write_schematic(str(sch), "new content\n", make_backup=True)
    finally:
        sch.chmod(0o644)

    assert [p.name for p in tmp_path.iterdir()] == ["test.kicad_sch"]


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


def test_collect_schematic_hierarchy_ignores_symbol_sheetfile_fields(
    tmp_path: Path,
) -> None:
    """A symbol's custom Sheetfile property does not name a sheet."""
    root = tmp_path / "root.kicad_sch"
    sub = tmp_path / "sub.kicad_sch"
    root.write_text(
        "(kicad_sch\n"
        '  (symbol (lib_id "Device:R") (property "Sheetfile" "notes.kicad_sch"))\n'
        '  (sheet (property "Sheetname" "Sub") (property "Sheetfile" "sub.kicad_sch"))\n'
        ")\n",
        encoding="utf-8",
    )
    sub.write_text("(kicad_sch)\n", encoding="utf-8")

    assert collect_schematic_hierarchy(str(root)) == [
        str(root.resolve()),
        str(sub.resolve()),
    ]


def test_collect_schematic_hierarchy_reads_the_older_sheet_file_name(
    tmp_path: Path,
) -> None:
    """Sheets saved with the older "Sheet file" property name are found."""
    root = tmp_path / "root.kicad_sch"
    sub = tmp_path / "sub.kicad_sch"
    root.write_text(
        '(kicad_sch\n  (sheet (property "Sheet file" "sub.kicad_sch"))\n)\n',
        encoding="utf-8",
    )
    sub.write_text("(kicad_sch)\n", encoding="utf-8")

    assert collect_schematic_hierarchy(str(root)) == [
        str(root.resolve()),
        str(sub.resolve()),
    ]


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
