"""Keep catalog identity stable until each download has actually terminated."""

from collections.abc import Iterator
import contextlib
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest

from tests.wx_harness import load_correction_modules


def seed_catalog(path: Path, stock: int = 1000, lcsc: str = "C1") -> None:
    """Create an actual searchable catalog, including selector and metadata tables."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(
            """
            CREATE VIRTUAL TABLE parts USING fts5(
                "LCSC Part", "MFR.Part", "Package", "Library Type", "Stock",
                "Manufacturer", "Description", "Price", "First Category",
                "Second Category", "Solder Joint"
            );
            CREATE TABLE categories ("First Category", "Second Category");
            INSERT INTO categories VALUES ('Resistors', 'Chip Resistors');
            CREATE TABLE meta (last_update, size, partcount);
            INSERT INTO meta VALUES ('2026-09-10', 1, 1);
            """
        )
        connection.execute(
            "INSERT INTO parts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                lcsc,
                "TEST-1",
                "0603",
                "Basic",
                stock,
                "Test Manufacturer",
                "Test resistor",
                "1-:0.10",
                "Resistors",
                "Chip Resistors",
                2,
            ),
        )


@pytest.fixture
def download_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SimpleNamespace]:
    """Construct a real catalog with isolated storage and captured GUI events."""
    with load_correction_modules(names=("events",)) as modules:
        monkeypatch.setattr(
            modules.library.Library,
            "_start_initial_remote_corrections",
            lambda *_args: None,
        )
        parent = SimpleNamespace(
            settings={"library": {"data_path": str(tmp_path / "first")}},
            project_path=str(tmp_path / "project"),
        )
        library = modules.library.Library(parent)
        yield SimpleNamespace(
            modules=modules, parent=parent, library=library, tmp_path=tmp_path
        )


def posted_events(context: SimpleNamespace, event_class: type) -> list[Any]:
    """Read only events of the requested real event type."""
    return [
        call.args[1]
        for call in context.modules.wx.PostEvent.call_args_list
        if isinstance(call.args[1], event_class)
    ]


def hold_workers(
    context: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> list[Any]:
    """Delay execution while preserving the actual update and worker callbacks."""
    workers: list[Any] = []

    def thread(*, target: Any) -> SimpleNamespace:
        return SimpleNamespace(start=lambda: workers.append(target))

    monkeypatch.setattr(context.modules.library, "Thread", thread)
    return workers


def test_duplicate_update_cannot_start_worker_after_public_state_changes(
    download_context: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """State refreshes must not release the separate live-worker claim."""
    context = download_context
    workers = hold_workers(context, monkeypatch)
    context.library.update()
    context.library.state = context.modules.library.LibraryState.UPDATE_NEEDED

    context.library.update()

    assert len(workers) == 1
    assert context.library.is_download_running()
    assert context.library.download_attempt == 1


def test_settings_cannot_rewire_a_running_download(
    download_context: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Apply the latest settings only after the old worker finishes, even on failure."""
    context = download_context
    workers = hold_workers(context, monkeypatch)
    original_path = context.library.partsdb_file
    original_directory = context.library.datadir
    context.library.update()
    context.parent.settings["library"]["data_path"] = str(context.tmp_path / "next")

    assert context.library.refresh_library_config() is False
    assert context.library.partsdb_file == original_path
    assert context.library.datadir == original_directory
    assert not (context.tmp_path / "next").exists()

    monkeypatch.setattr(context.library, "download", lambda: False)
    workers[0]()

    assert not context.library.is_download_running()
    assert context.library.refresh_library_config() is True
    assert context.library.datadir == str(context.tmp_path / "next")
    finished = posted_events(context, context.modules.events.DownloadFinishedEvent)
    assert len(finished) == 1
    assert finished[0].library is context.library
    assert finished[0].source[1] == original_path
    assert finished[0].succeeded is False
    context.library.update()
    assert context.library.download_attempt == 2


@pytest.mark.parametrize("exceptional", [False, True])
def test_failed_download_emits_finished_without_marking_catalog_ready(
    download_context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    exceptional: bool,
) -> None:
    """Both an HTTP error and an unexpected exception release queued settings."""
    context = download_context
    workers = hold_workers(context, monkeypatch)
    context.library.update()
    if exceptional:

        def fail() -> bool:
            raise OSError("worker failure")

        monkeypatch.setattr(context.library, "download", fail)
    else:
        monkeypatch.setattr(
            context.modules.library.requests,
            "get",
            lambda *_args, **_kwargs: SimpleNamespace(status_code=503),
        )

    workers[0]()

    assert not context.library.is_download_running()
    assert context.library.state == context.modules.library.LibraryState.UPDATE_NEEDED
    assert not posted_events(context, context.modules.events.DownloadCompletedEvent)
    finished = posted_events(context, context.modules.events.DownloadFinishedEvent)
    assert len(finished) == 1
    assert finished[0].succeeded is False
    assert finished[0].attempt == context.library.download_attempt == 1
    assert finished[0].library is context.library


@pytest.mark.parametrize(
    "catalog_kind",
    [
        "valid",
        "empty",
        "nonempty garbage",
        "truncated database",
        "missing parts",
        "non-FTS parts",
        "missing metadata",
        "empty metadata",
        "missing categories",
        "missing solder joints",
    ],
)
def test_completion_requires_usable_catalog_and_keeps_source_identity(
    download_context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    catalog_kind: str,
) -> None:
    """Nonempty extracted bytes alone cannot establish searchable catalog readiness."""
    context = download_context
    workers = hold_workers(context, monkeypatch)
    source = (context.library.selected_library, context.library.partsdb_file)
    monkeypatch.setattr(
        context.modules.library.requests,
        "get",
        lambda *_args, **_kwargs: SimpleNamespace(status_code=200, text="0"),
    )

    def extract(*_args: Any) -> None:
        path = Path(context.library.partsdb_file)
        if catalog_kind == "empty":
            path.write_bytes(b"")
            return
        if catalog_kind == "nonempty garbage":
            path.write_bytes(b"partial ZIP extraction is not a catalog")
            return
        seed_catalog(path)
        if catalog_kind == "truncated database":
            contents = path.read_bytes()
            path.write_bytes(contents[: len(contents) // 2])
            return
        with contextlib.closing(sqlite3.connect(path)) as connection, connection:
            if catalog_kind == "missing parts":
                connection.execute("DROP TABLE parts")
            elif catalog_kind == "non-FTS parts":
                connection.execute("ALTER TABLE parts RENAME TO search_parts")
                connection.execute("CREATE TABLE parts AS SELECT * FROM search_parts")
            elif catalog_kind == "missing metadata":
                connection.execute("DROP TABLE meta")
            elif catalog_kind == "empty metadata":
                connection.execute("DELETE FROM meta")
            elif catalog_kind == "missing categories":
                connection.execute("DROP TABLE categories")
            elif catalog_kind == "missing solder joints":
                fields = ", ".join(
                    f'"{row[1]}"'
                    for row in connection.execute("PRAGMA table_info(parts)")
                    if row[1] != "Solder Joint"
                )
                connection.execute("ALTER TABLE parts RENAME TO original_parts")
                connection.execute(f"CREATE VIRTUAL TABLE parts USING fts5({fields})")
                connection.execute(
                    f"INSERT INTO parts SELECT {fields} FROM original_parts"
                )
                connection.execute("DROP TABLE original_parts")

    monkeypatch.setattr(context.modules.library, "unzip_parts", extract)
    context.library.update()

    workers[0]()

    usable = catalog_kind in ("valid", "missing metadata", "empty metadata")
    completed = posted_events(context, context.modules.events.DownloadCompletedEvent)
    assert len(completed) == int(usable)
    if usable:
        assert completed[0].library is context.library
        assert completed[0].source == source
        assert context.library.get_part_details("C1")["stock"] == 1000
    finished = posted_events(context, context.modules.events.DownloadFinishedEvent)
    assert len(finished) == 1
    assert finished[0].source == source
    assert finished[0].succeeded is usable
    assert not context.library.is_download_running()
    expected_state = (
        context.modules.library.LibraryState.INITIALIZED
        if usable
        else context.modules.library.LibraryState.UPDATE_NEEDED
    )
    assert context.library.state == expected_state


@pytest.mark.parametrize("existing_catalog", [False, True])
def test_thread_start_failure_releases_claim_and_preserves_remaining_catalog(
    download_context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    existing_catalog: bool,
) -> None:
    """A worker that never starts cannot strand deferred settings forever."""
    context = download_context
    if existing_catalog:
        seed_catalog(Path(context.library.partsdb_file))
        context.library.check_library()

    def fail_start() -> None:
        raise RuntimeError("cannot start worker")

    monkeypatch.setattr(
        context.modules.library,
        "Thread",
        lambda **_kwargs: SimpleNamespace(start=fail_start),
    )

    with pytest.raises(RuntimeError, match="cannot start worker"):
        context.library.update()

    assert not context.library.is_download_running()
    finished = posted_events(context, context.modules.events.DownloadFinishedEvent)
    assert len(finished) == 1
    assert finished[0].succeeded is False
    assert finished[0].attempt == context.library.download_attempt == 1
    expected_state = (
        context.modules.library.LibraryState.INITIALIZED
        if existing_catalog
        else context.modules.library.LibraryState.UPDATE_NEEDED
    )
    assert context.library.state == expected_state
    assert not posted_events(context, context.modules.events.DownloadCompletedEvent)


def test_success_events_publish_only_after_worker_claim_is_released(
    download_context: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even immediate UI dispatch must observe a finished, initialized catalog."""
    context = download_context
    workers = hold_workers(context, monkeypatch)
    observations: list[tuple[type, bool, Any]] = []
    monkeypatch.setattr(
        context.modules.library.requests,
        "get",
        lambda *_args, **_kwargs: SimpleNamespace(status_code=200, text="0"),
    )
    monkeypatch.setattr(
        context.modules.library,
        "unzip_parts",
        lambda *_args: seed_catalog(Path(context.library.partsdb_file)),
    )

    def dispatch(_parent: Any, event: Any) -> None:
        if not isinstance(
            event,
            (
                context.modules.events.DownloadCompletedEvent,
                context.modules.events.DownloadFinishedEvent,
            ),
        ):
            return
        observations.append(
            (type(event), context.library.is_download_running(), context.library.state)
        )

    context.modules.wx.PostEvent.side_effect = dispatch
    context.library.update()

    workers[0]()

    initialized = context.modules.library.LibraryState.INITIALIZED
    assert observations == [
        (context.modules.events.DownloadCompletedEvent, False, initialized),
        (context.modules.events.DownloadFinishedEvent, False, initialized),
    ]


def test_queued_success_has_older_identity_after_same_catalog_retry_fails(
    download_context: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delivered-late success cannot identify itself as a failed newer attempt."""
    context = download_context
    workers = hold_workers(context, monkeypatch)
    monkeypatch.setattr(
        context.modules.library.requests,
        "get",
        lambda *_args, **_kwargs: SimpleNamespace(status_code=200, text="0"),
    )
    monkeypatch.setattr(
        context.modules.library,
        "unzip_parts",
        lambda *_args: seed_catalog(Path(context.library.partsdb_file)),
    )
    context.library.update()
    workers[0]()
    # Leave every posted event queued while a retry fails at the very same source.
    monkeypatch.setattr(
        context.modules.library.requests,
        "get",
        lambda *_args, **_kwargs: SimpleNamespace(status_code=503),
    )
    context.library.update()
    workers[1]()

    completed = posted_events(context, context.modules.events.DownloadCompletedEvent)
    finished = posted_events(context, context.modules.events.DownloadFinishedEvent)
    started = posted_events(context, context.modules.events.DownloadStartedEvent)
    assert len(completed) == 1
    assert completed[0].source == finished[-1].source
    assert [event.attempt for event in started] == [1, 2]
    assert [(event.attempt, event.succeeded) for event in finished] == [
        (1, True),
        (2, False),
    ]
    assert completed[0].attempt < context.library.download_attempt == 2
    assert not context.library.is_download_running()
    assert context.library.state == context.modules.library.LibraryState.INITIALIZED
    assert context.library.get_part_details("C1")["stock"] == 1000


@pytest.mark.parametrize("existing_catalog", [False, True])
@pytest.mark.parametrize(
    "failure",
    [
        "manifest HTTP",
        "manifest network",
        "chunk HTTP",
        "chunk network",
        "chunk stream",
    ],
)
def test_transfer_failure_retains_only_an_existing_usable_catalog_without_writes(
    download_context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    existing_catalog: bool,
    failure: str,
) -> None:
    """Failed transfers preserve old stock without reinitializing auxiliary storage."""
    context = download_context
    path = Path(context.library.partsdb_file)
    if existing_catalog:
        seed_catalog(path, stock=1234)
        context.library.check_library()
        assert context.library.get_part_details("C1")["stock"] == 1234
    snapshot_paths = [
        path,
        Path(context.library.correctionsdb_file),
        Path(context.library.part_preferences_db_file),
    ]
    snapshots = {
        candidate: candidate.read_bytes() if candidate.exists() else None
        for candidate in snapshot_paths
    }

    def no_storage_initialization() -> None:
        pytest.fail("Failure recovery must validate read-only, not run check_library")

    monkeypatch.setattr(context.library, "check_library", no_storage_initialization)

    def stream(*, chunk_size: int) -> Iterator[bytes]:
        assert chunk_size > 0
        yield b"incomplete chunk"
        raise context.modules.library.requests.ConnectionError("connection interrupted")

    requests_made = 0

    def get(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        nonlocal requests_made
        requests_made += 1
        if requests_made == 1:
            if failure == "manifest HTTP":
                return SimpleNamespace(status_code=503)
            if failure == "manifest network":
                raise context.modules.library.requests.ConnectionError(
                    "manifest offline"
                )
            return SimpleNamespace(status_code=200, text="1")
        if failure == "chunk HTTP":
            return SimpleNamespace(status_code=503)
        if failure == "chunk network":
            raise context.modules.library.requests.ConnectionError("chunk offline")
        return SimpleNamespace(
            status_code=200, headers={"Content-Length": "100"}, iter_content=stream
        )

    monkeypatch.setattr(context.modules.library.requests, "get", get)
    workers = hold_workers(context, monkeypatch)
    observations: list[tuple[bool, Any]] = []

    def observe_finished(_parent: Any, event: Any) -> None:
        if isinstance(event, context.modules.events.DownloadFinishedEvent):
            observations.append(
                (context.library.is_download_running(), context.library.state)
            )

    context.modules.wx.PostEvent.side_effect = observe_finished
    context.library.update()
    workers[0]()

    for candidate, original in snapshots.items():
        assert (candidate.read_bytes() if candidate.exists() else None) == original
    expected_state = (
        context.modules.library.LibraryState.INITIALIZED
        if existing_catalog
        else context.modules.library.LibraryState.UPDATE_NEEDED
    )
    assert observations == [(False, expected_state)]
    assert context.library.state == expected_state
    assert not posted_events(context, context.modules.events.DownloadCompletedEvent)
    finished = posted_events(context, context.modules.events.DownloadFinishedEvent)
    assert len(finished) == 1
    assert finished[0].succeeded is False
    assert finished[0].attempt == 1
    if existing_catalog:
        assert context.library.get_part_details("C1")["stock"] == 1234
        results = context.library.search(
            {
                "keyword": "resistor",
                "basic": True,
                "extended": False,
                "preferred": False,
                "stock": False,
                "solder_joints": "2",
            }
        )
        assert len(results) == 1
        assert results[0][0] == "C1"


@pytest.mark.parametrize("replacement", ["intact", "missing", "corrupt", "truncated"])
def test_extraction_exception_checks_remaining_catalog_before_retry(
    download_context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    """A failed extractor may leave old usable bytes or destroy the active catalog."""
    context = download_context
    path = Path(context.library.partsdb_file)
    seed_catalog(path, stock=1234)
    context.library.check_library()
    workers = hold_workers(context, monkeypatch)
    monkeypatch.setattr(
        context.modules.library.requests,
        "get",
        lambda *_args, **_kwargs: SimpleNamespace(status_code=200, text="0"),
    )

    def extract(*_args: Any) -> None:
        if replacement == "missing":
            path.unlink()
        elif replacement == "corrupt":
            path.write_bytes(b"incomplete database")
        elif replacement == "truncated":
            contents = path.read_bytes()
            path.write_bytes(contents[: len(contents) // 2])
        raise OSError("extraction interrupted")

    monkeypatch.setattr(context.modules.library, "unzip_parts", extract)
    context.library.update()
    workers[0]()

    expected_state = (
        context.modules.library.LibraryState.INITIALIZED
        if replacement == "intact"
        else context.modules.library.LibraryState.UPDATE_NEEDED
    )
    assert context.library.state == expected_state
    assert not context.library.is_download_running()
    assert not posted_events(context, context.modules.events.DownloadCompletedEvent)
    finished = posted_events(context, context.modules.events.DownloadFinishedEvent)
    assert [(event.attempt, event.succeeded) for event in finished] == [(1, False)]
    if replacement == "missing":
        assert not path.exists(), "Read-only validation must not create a missing DB"
    if replacement == "intact":
        assert context.library.get_part_details("C1")["stock"] == 1234

    def successful_extract(*_args: Any) -> None:
        path.unlink(missing_ok=True)
        seed_catalog(path, stock=9876)

    monkeypatch.setattr(context.modules.library, "unzip_parts", successful_extract)
    context.library.update()
    workers[1]()

    assert context.library.state == context.modules.library.LibraryState.INITIALIZED
    assert context.library.get_part_details("C1")["stock"] == 9876
    completed = posted_events(context, context.modules.events.DownloadCompletedEvent)
    assert [event.attempt for event in completed] == [2]
    finished = posted_events(context, context.modules.events.DownloadFinishedEvent)
    assert [(event.attempt, event.succeeded) for event in finished] == [
        (1, False),
        (2, True),
    ]


@pytest.mark.parametrize("failure", ["existing file", "permission", "SQLite"])
def test_configuration_failure_revokes_old_readiness_and_valid_retry_recovers(
    download_context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Changing storage cannot carry INITIALIZED forward after setup raises."""
    context = download_context
    seed_catalog(Path(context.library.partsdb_file), stock=1234)
    context.library.check_library()
    assert context.library.state == context.modules.library.LibraryState.INITIALIZED
    destination = context.tmp_path / "invalid-target"
    if failure == "existing file":
        destination.write_text("This is a file, not a catalog directory.")
    context.parent.settings["library"]["data_path"] = str(destination)

    with monkeypatch.context() as failure_patch:
        if failure == "permission":

            def setup_denied() -> None:
                raise PermissionError("catalog directory denied")

            failure_patch.setattr(context.library, "setup", setup_denied)
        elif failure == "SQLite":

            def storage_failed() -> None:
                raise sqlite3.OperationalError("unable to open database file")

            failure_patch.setattr(context.library, "check_library", storage_failed)
        with pytest.raises((OSError, sqlite3.Error)):
            context.library.refresh_library_config()

    assert context.library.state == context.modules.library.LibraryState.UPDATE_NEEDED
    assert not context.library.is_download_running()
    assert context.library.download_attempt == 0

    valid_destination = context.tmp_path / "valid-target"
    seed_catalog(
        valid_destination / Path(context.library.partsdb_file).name, stock=9876
    )
    context.parent.settings["library"]["data_path"] = str(valid_destination)
    assert context.library.refresh_library_config() is True
    assert context.library.state == context.modules.library.LibraryState.INITIALIZED
    assert context.library.get_part_details("C1")["stock"] == 9876
