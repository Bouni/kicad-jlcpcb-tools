"""One-time schematic storage notice for projects that already have project.db."""

from pathlib import Path
import sqlite3
from types import MethodType, ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from .wx_harness import load, load_mainwindow, package_stubs, wx_stubs

_STORE_PACKAGE = "_schematic_storage_notice_store"
_WINDOW_PACKAGE = "_schematic_storage_notice_window"


@pytest.fixture
def store_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Use the real Store with footprint adapters stubbed."""
    module = load(
        _STORE_PACKAGE, "store", {**package_stubs(_STORE_PACKAGE), **wx_stubs()}
    )
    monkeypatch.setattr(
        module, "get_valid_footprints", lambda board: board.GetFootprints()
    )
    monkeypatch.setattr(
        module,
        "get_lcsc_assignment",
        lambda footprint: (SimpleNamespace(status="unassigned"), ""),
    )
    monkeypatch.setattr(module, "get_exclude_from_bom", lambda footprint: False)
    monkeypatch.setattr(module, "get_exclude_from_pos", lambda footprint: False)
    monkeypatch.setattr(module, "get_is_dnp", lambda footprint: False)
    monkeypatch.setattr(
        module, "get_footprint_pad_metadata", lambda footprint: (2, False)
    )
    monkeypatch.setattr(module, "get_assembly_flags", lambda footprint: "[]")
    return module


def _board(project: Path, name: str = "board") -> Any:
    """Minimal board with one footprint for Store construction."""
    filename = project / f"{name}.kicad_pcb"
    filename.write_text("(kicad_pcb)\n", encoding="utf-8")
    footprint = SimpleNamespace(
        GetReference=lambda: "R1",
        GetValue=lambda: "10k",
        GetFPID=lambda: SimpleNamespace(GetLibItemName=lambda: "R_0603"),
    )
    return SimpleNamespace(
        GetFileName=lambda: str(filename), GetFootprints=lambda: [footprint]
    )


def _store(module: ModuleType, project: Path) -> Any:
    """Construct an ordinary Store against project."""
    return module.Store(SimpleNamespace(settings={}), str(project), _board(project))


def _legacy_database(store: Any) -> Path:
    """Create an old assignment database with no notice metadata."""
    dbfile = Path(store.dbfile)
    dbfile.parent.mkdir()
    with sqlite3.connect(dbfile) as connection:
        connection.execute("CREATE TABLE part_info (reference TEXT, lcsc TEXT)")
        connection.execute("INSERT INTO part_info VALUES ('R1', 'C999')")
    return dbfile


@pytest.mark.parametrize("existing_directory", [False, True])
def test_store_notice_read_does_not_create_storage(
    tmp_path: Path, store_module: ModuleType, existing_directory: bool
) -> None:
    """Constructing and reading ordinary parts or notice state remains lazy."""
    datadir = tmp_path / "jlcpcb"
    if existing_directory:
        datadir.mkdir()
    store = _store(store_module, tmp_path)

    assert store.read_all()[0]["reference"] == "R1"
    assert not store.is_schematic_storage_notice_acked()
    assert not Path(store.dbfile).exists()
    assert datadir.exists() == existing_directory


def test_store_notice_read_without_metadata_preserves_legacy_database(
    tmp_path: Path, store_module: ModuleType
) -> None:
    """Reading a legacy database does not add the missing metadata table."""
    store = _store(store_module, tmp_path)
    dbfile = _legacy_database(store)
    before = dbfile.read_bytes()

    assert not store.is_schematic_storage_notice_acked()

    assert dbfile.read_bytes() == before


def test_store_schematic_storage_notice_ack_round_trips(
    tmp_path: Path, store_module: ModuleType
) -> None:
    """Ack helpers persist in project.db metadata and survive reopen."""
    store = _store(store_module, tmp_path)
    assert not store.is_schematic_storage_notice_acked()

    store.set_schematic_storage_notice_acked()
    assert store.is_schematic_storage_notice_acked()

    reopened = _store(store_module, tmp_path)
    assert reopened.is_schematic_storage_notice_acked()
    with sqlite3.connect(store.dbfile) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        assert tables == [("metadata",)]
        assert connection.execute("SELECT * FROM metadata").fetchall() == [
            (store.SCHEMATIC_STORAGE_NOTICE_ACKED_KEY, "1")
        ]


@pytest.mark.parametrize("existing_metadata", [False, True])
def test_store_notice_ack_preserves_legacy_rows(
    tmp_path: Path, store_module: ModuleType, existing_metadata: bool
) -> None:
    """Acknowledging creates only metadata and preserves legacy assignments."""
    store = _store(store_module, tmp_path)
    dbfile = _legacy_database(store)
    with sqlite3.connect(dbfile) as connection:
        if existing_metadata:
            connection.execute(
                "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO metadata VALUES ('generation_count', '7')")

    assert not store.is_schematic_storage_notice_acked()
    store.set_schematic_storage_notice_acked()
    store.set_schematic_storage_notice_acked()

    assert _store(store_module, tmp_path).is_schematic_storage_notice_acked()
    with sqlite3.connect(dbfile) as connection:
        assert connection.execute("SELECT * FROM part_info").fetchall() == [
            ("R1", "C999")
        ]
        metadata = dict(connection.execute("SELECT * FROM metadata").fetchall())
    expected = {store.SCHEMATIC_STORAGE_NOTICE_ACKED_KEY: "1"}
    if existing_metadata:
        expected[store.GENERATION_COUNT_KEY] = "7"
    assert metadata == expected


@pytest.fixture
def mainwindow_module() -> Any:
    """Isolated mainwindow with MessageDialog stubbed for the notice."""
    return load_mainwindow(
        _WINDOW_PACKAGE,
        wx=wx_stubs(
            Frame=type("Frame", (), {}),
            NewIdRef=MagicMock(side_effect=object),
            MessageDialog=MagicMock(),
            GenericMessageDialog=MagicMock(),
        ),
    )


def _notice_window(mainwindow: Any, project_path: str, store: Any) -> SimpleNamespace:
    """Bind the production notice helper onto a minimal frame."""
    dialog = MagicMock()
    dialog.ShowModal.return_value = mainwindow.wx.ID_OK
    mainwindow.wx.MessageDialog.return_value = dialog
    window = SimpleNamespace(
        store=store,
        logger=MagicMock(),
        project_path=project_path,
    )
    window._maybe_show_schematic_storage_notice = MethodType(
        mainwindow.JLCPCBTools._maybe_show_schematic_storage_notice, window
    )
    return window


def test_notice_shown_for_preexisting_project_db_and_acked(
    mainwindow_module: Any, store_module: ModuleType, tmp_path: Path
) -> None:
    """Existing plugin projects see the notice once, then the ack is recorded."""
    store = _store(store_module, tmp_path)
    _legacy_database(store)
    window = _notice_window(mainwindow_module, str(tmp_path), store)

    window._maybe_show_schematic_storage_notice(True)

    mainwindow_module.wx.MessageDialog.assert_called_once()
    title = mainwindow_module.wx.MessageDialog.call_args.args[2]
    message = mainwindow_module.wx.MessageDialog.call_args.args[1]
    assert title == "Schematic storage"
    assert "settings live from now on" in message
    assert mainwindow_module.SCHEMATIC_PRE_WRITE_BACKUP_ZIP in message
    assert store.is_schematic_storage_notice_acked()


def test_notice_skipped_for_greenfield_projects(
    mainwindow_module: Any, store_module: ModuleType, tmp_path: Path
) -> None:
    """Brand-new projects skip the migration dialog and record the ack immediately."""
    store = _store(store_module, tmp_path)
    window = _notice_window(mainwindow_module, str(tmp_path), store)

    window._maybe_show_schematic_storage_notice(False)

    mainwindow_module.wx.MessageDialog.assert_not_called()
    assert store.is_schematic_storage_notice_acked()


def test_greenfield_ack_suppresses_notice_on_later_open(
    mainwindow_module: Any, store_module: ModuleType, tmp_path: Path
) -> None:
    """After a greenfield open, a later preexisting-db open must stay silent."""
    store = _store(store_module, tmp_path)
    window = _notice_window(mainwindow_module, str(tmp_path), store)

    window._maybe_show_schematic_storage_notice(False)
    reopened = _notice_window(
        mainwindow_module, str(tmp_path), _store(store_module, tmp_path)
    )
    reopened._maybe_show_schematic_storage_notice(True)

    mainwindow_module.wx.MessageDialog.assert_not_called()
    assert store.is_schematic_storage_notice_acked()


def test_notice_not_shown_again_after_ack(
    mainwindow_module: Any, store_module: ModuleType, tmp_path: Path
) -> None:
    """Reopening an acknowledged project suppresses the dialog."""
    store = _store(store_module, tmp_path)
    store.set_schematic_storage_notice_acked()
    window = _notice_window(mainwindow_module, str(tmp_path), store)

    window._maybe_show_schematic_storage_notice(True)

    mainwindow_module.wx.MessageDialog.assert_not_called()


def test_default_wx_stubs_survive_storage_notice(
    store_module: ModuleType, tmp_path: Path
) -> None:
    """Default wx_stubs supply MessageDialog so init_store callers do not crash."""
    mainwindow = load_mainwindow(
        "_schematic_storage_notice_default_wx",
        wx=wx_stubs(
            Frame=type("Frame", (), {}),
            NewIdRef=MagicMock(side_effect=object),
        ),
    )
    store = _store(store_module, tmp_path)
    _legacy_database(store)
    window = _notice_window(mainwindow, str(tmp_path), store)

    window._maybe_show_schematic_storage_notice(True)

    mainwindow.wx.MessageDialog.assert_called_once()
    assert store.is_schematic_storage_notice_acked()
