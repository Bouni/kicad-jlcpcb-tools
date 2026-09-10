"""Check portable SQLite paths and real existing-file connection guarantees."""

from collections.abc import Iterator
from contextlib import closing
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
import sqlite3
import sys
from types import ModuleType
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest

from tests.wx_harness import load_correction_modules


@pytest.fixture
def library_module() -> Iterator[ModuleType]:
    """Import the production storage code with isolated GUI dependencies."""
    with load_correction_modules() as loaded:
        yield loaded.library


class ResolvedPath:
    """Supply only the Windows filesystem facts needed before SQLite opens."""

    def __init__(self, path: PurePath) -> None:
        self.path = path

    def resolve(self) -> PurePath:
        """Expose a Windows absolute path without requiring Windows I/O."""
        return self.path

    def is_file(self) -> bool:
        """Represent an existing database for the transaction precondition."""
        return True


@pytest.mark.parametrize("read_only", [True, False], ids=["read", "transaction"])
@pytest.mark.parametrize("host", ["nas", "localhost"])
def test_unc_connection_boundaries_preserve_host_and_mode(
    library_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    read_only: bool,
    host: str,
) -> None:
    """Both production opens must avoid URI authorities, including localhost.

    Only path resolution and disk access are replaced: the original connection
    string still traverses SQLite's URI parser in memory mode. Separate real-file
    tests below verify the original read-only and existing-only open modes.
    """
    target = PureWindowsPath(f"//{host}/share/corrections.db")
    expected = f"file:////{host}/share/corrections.db"
    original_connect = sqlite3.connect
    observed = []

    def resolve_path(value: str) -> ResolvedPath:
        assert value == str(target)
        return ResolvedPath(target)

    def connect_in_memory(database: str, *, uri: bool = False) -> sqlite3.Connection:
        observed.append((database, uri))
        connection = original_connect(
            database.rsplit("?", 1)[0] + "?mode=memory", uri=uri
        )
        try:
            assert database == expected + ("?mode=ro" if read_only else "?mode=rw")
            assert uri is True
            connection.execute(
                "CREATE TABLE correction (regex, rotation, offset_x, offset_y)"
            )
            return connection
        except BaseException:
            connection.close()
            raise

    monkeypatch.setattr(library_module, "Path", resolve_path)
    monkeypatch.setattr(sqlite3, "connect", connect_in_memory)
    library = library_module.Library.__new__(library_module.Library)
    if read_only:
        with closing(library._read_database(str(target))) as connection:
            assert connection.execute("SELECT COUNT(*) FROM correction").fetchone() == (
                0,
            )
    else:
        with library._correction_transaction(str(target)) as connection:
            connection.execute("INSERT INTO correction VALUES ('R1', 90, 0, 0)")
    assert len(observed) == 1


@pytest.mark.parametrize(
    ("path", "expected", "decoded"),
    [
        (
            PureWindowsPath("//nas/share/corrections.db"),
            "file:////nas/share/corrections.db",
            "//nas/share/corrections.db",
        ),
        (
            PureWindowsPath("//localhost/share/corrections.db"),
            "file:////localhost/share/corrections.db",
            "//localhost/share/corrections.db",
        ),
        (
            PureWindowsPath("//nas/share/"),
            "file:////nas/share/",
            "//nas/share/",
        ),
        (
            PureWindowsPath("//nas/shared folder/nested/café #100%23.db"),
            "file:////nas/shared%20folder/nested/caf%C3%A9%20%23100%2523.db",
            "//nas/shared folder/nested/café #100%23.db",
        ),
        (
            PureWindowsPath("//nás/share/corrections.db"),
            "file:////n%C3%A1s/share/corrections.db",
            "//nás/share/corrections.db",
        ),
        (
            PureWindowsPath("C:/data/corrections.db"),
            "file:///C:/data/corrections.db",
            "/C:/data/corrections.db",
        ),
        (
            PureWindowsPath("d:/café #100%23/corrections.db"),
            "file:///d:/caf%C3%A9%20%23100%2523/corrections.db",
            "/d:/café #100%23/corrections.db",
        ),
        (
            PurePosixPath("/data/corrections.db"),
            "file:///data/corrections.db",
            "/data/corrections.db",
        ),
        (
            PurePosixPath("/data/café #100%?mode=rwc.db"),
            "file:///data/caf%C3%A9%20%23100%25%3Fmode%3Drwc.db",
            "/data/café #100%?mode=rwc.db",
        ),
        (
            PurePosixPath("/data/%23%3F%25.db"),
            "file:///data/%2523%253F%2525.db",
            "/data/%23%3F%25.db",
        ),
        (
            PurePosixPath("//data/corrections.db"),
            "file:////data/corrections.db",
            "//data/corrections.db",
        ),
    ],
)
def test_sqlite_uri_preserves_absolute_path(
    library_module: ModuleType, path: PurePath, expected: str, decoded: str
) -> None:
    """Keep the host in the path and encode filename syntax exactly once."""
    uri = library_module._sqlite_file_uri(path)
    assert uri == expected
    parsed = urlsplit(uri)
    assert parsed.scheme == "file"
    assert parsed.netloc == parsed.query == parsed.fragment == ""
    assert unquote(parsed.path) == decoded
    # Exercise the native parser without claiming to open a Windows share.
    with closing(sqlite3.connect(uri + "?mode=memory", uri=True)) as connection:
        assert connection.execute("SELECT 1").fetchone() == (1,)


@pytest.mark.parametrize(
    "path", [PureWindowsPath("C:corrections.db"), PurePosixPath("corrections.db")]
)
def test_sqlite_uri_requires_an_absolute_path(
    library_module: ModuleType, path: PurePath
) -> None:
    """The caller must resolve drive-relative and ordinary relative filenames."""
    with pytest.raises(ValueError, match="relative path"):
        library_module._sqlite_file_uri(path)


@pytest.mark.parametrize("as_string", [True, False], ids=["str", "pathlike"])
@pytest.mark.parametrize(
    "filename",
    [
        "corrections.db",
        "café #100%23%3F.db",
        pytest.param(
            "question?mode=rwc#100%.db",
            marks=pytest.mark.skipif(
                sys.platform == "win32", reason="Windows filenames cannot contain ?"
            ),
        ),
    ],
)
def test_real_file_modes_creation_and_reopening(
    library_module: ModuleType, tmp_path: Path, as_string: bool, filename: str
) -> None:
    """Intended creation and existing-only updates survive reopen with exact names."""
    database = tmp_path / "nested café #100%23" / filename
    target = str(database) if as_string else database
    library = library_module.Library.__new__(library_module.Library)
    assert not database.exists()
    library.create_correction_table(target)
    library.save_correction_data("R1", 90, (1.25, -2.5), db_path=target)
    assert database.is_file()

    with closing(library._read_database(target)) as connection:
        assert connection.execute("SELECT * FROM correction").fetchall() == [
            ("R1", 90, 1.25, -2.5)
        ]
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("UPDATE correction SET rotation=270")

    with library._correction_transaction(target) as connection:
        connection.execute("UPDATE correction SET rotation=180")
    reopened = library_module.Library.__new__(library_module.Library)
    assert reopened.get_correction_data("R1", target) == ("R1", 180, 1.25, -2.5)

    with (
        pytest.raises(RuntimeError, match="abort this update"),
        reopened._correction_transaction(target) as connection,
    ):
        connection.execute("UPDATE correction SET rotation=270")
        raise RuntimeError("abort this update")
    assert library.get_correction_data("R1", target) == ("R1", 180, 1.25, -2.5)
    assert set(database.parent.iterdir()) == {database}


@pytest.mark.parametrize("read_only", [True, False], ids=["read", "transaction"])
@pytest.mark.parametrize("parent_exists", [True, False])
def test_existing_file_opens_never_create_missing_storage(
    library_module: ModuleType,
    tmp_path: Path,
    read_only: bool,
    parent_exists: bool,
) -> None:
    """Missing storage raises an error and leaves the filesystem untouched."""
    directory = tmp_path if parent_exists else tmp_path / "missing-directory"
    target = directory / "missing café #100%23.db"
    library = library_module.Library.__new__(library_module.Library)
    if read_only:
        with pytest.raises(sqlite3.OperationalError):
            library._read_database(target)
    else:
        with (
            pytest.raises(library_module.CorrectionDataError, match="does not exist"),
            library._correction_transaction(target),
        ):
            pytest.fail("a missing database cannot start a transaction")
    assert not target.exists()
    assert directory.exists() == parent_exists


@pytest.mark.parametrize("read_only", [True, False], ids=["read", "transaction"])
def test_deletion_immediately_before_connect_does_not_recreate_database(
    library_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_only: bool,
) -> None:
    """SQLite's open mode closes the race after the transaction existence check."""
    target = tmp_path / "disappearing.db"
    library = library_module.Library.__new__(library_module.Library)
    library.create_correction_table(target)
    original_connect = sqlite3.connect
    attempts = []

    def delete_then_connect(database: Any, **kwargs: Any) -> sqlite3.Connection:
        assert target.is_file()
        target.unlink()
        attempts.append(database)
        return original_connect(database, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", delete_then_connect)
    if read_only:
        with pytest.raises(sqlite3.OperationalError, match="unable to open"):
            library._read_database(target)
    else:
        with (
            pytest.raises(library_module.CorrectionDataError, match="unable to open"),
            library._correction_transaction(target),
        ):
            pytest.fail("a deleted database cannot start a transaction")
    assert len(attempts) == 1
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []
