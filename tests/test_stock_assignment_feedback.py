"""Explain unavailable assignments without changing project or catalog state."""

from collections.abc import Callable
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from . import test_stock_catalog_workflow as catalog_workflow
from .part_preferences_test_support import act, project_rows
from .test_stock_download_lifecycle import seed_catalog

configuration_window = catalog_workflow.configuration_window
make_window = catalog_workflow.make_window
mainwindow = catalog_workflow.mainwindow


def _select_row(window: Any) -> None:
    """Select a current real model row through the GUI boundary."""
    window.footprint_list.GetSelections.return_value = [
        window.partlist_data_model.data[0]
    ]


def _state(window: Any, store: Any) -> tuple[Any, ...]:
    """Read durable assignments, displayed cells, and live board fields."""
    return (
        project_rows(SimpleNamespace(store=store)),
        deepcopy(window.partlist_data_model.data),
        {
            footprint.GetReference(): {
                field.GetName(): field.GetText() for field in footprint.GetFields()
            }
            for footprint in window.pcbnew.GetBoard().GetFootprints()
        },
    )


def _warnings(window: Any) -> list[str]:
    """Render warning messages as they appear in the product log."""
    return [
        str(call.args[0]) % call.args[1:]
        for call in window.logger.warning.call_args_list
    ]


def _watch_action(window: Any, mainwindow: Any) -> None:
    """Count real storage operations after the Settings transition has completed."""
    for name in (
        "get_part_details",
        "get_part_preference",
        "save_part_preferences",
        "update",
    ):
        setattr(window.library, name, MagicMock(wraps=getattr(window.library, name)))
    window.store.set_lcsc_assignments = MagicMock(
        wraps=window.store.set_lcsc_assignments
    )
    window.logger.reset_mock()
    window.start_assembly_enrichment.reset_mock()
    mainwindow.wx.PostEvent.reset_mock()


def _assert_refused(window: Any, library: Any, store: Any, before: Any) -> None:
    """Require one useful explanation and no assignment or catalog side effects."""
    assert _state(window, store) == before
    store.set_lcsc_assignments.assert_not_called()
    for name in (
        "get_part_details",
        "get_part_preference",
        "save_part_preferences",
        "update",
    ):
        getattr(library, name).assert_not_called()
    window.start_assembly_enrichment.assert_not_called()
    window.logger.info.assert_not_called()
    messages = _warnings(window)
    assert len(messages) == 1, "A refused user assignment needs one explanation"
    message = messages[0].lower()
    assert "catalog" in message
    assert "download" in message or "select" in message


def _switch_to_missing(window: Any, setting: str, tmp_path: Path) -> str:
    """Use the Settings handler to select an actual absent catalog file."""
    library_module = sys.modules[type(window.library).__module__]
    previous = (
        window.library.selected_library
        if setting == "selected_library"
        else window.library.datadir
    )
    missing = (
        next(key for key in library_module.LIBRARY_CONFIGS if key != previous)
        if setting == "selected_library"
        else str(tmp_path / "unfetched")
    )
    window.update_settings(
        SimpleNamespace(section="library", setting=setting, value=missing)
    )
    assert window.is_catalog_available() is False
    catalog_path = Path(window.library.partsdb_file)
    # Legacy preference migration may create an empty SQLite file in a new directory.
    assert not catalog_path.exists() or catalog_path.stat().st_size == 0
    assert window.store is not None
    assert window._project_storage_unavailable is False
    window.footprint_list.Enable.assert_called_with(True)
    window.right_toolbar.Enable.assert_called_with(True)
    window.library.save_part_preferences([("R_0603", "10k", "C200")])
    _select_row(window)
    return previous


def _download_catalog(window: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise Update and its real worker/events with deterministic transport."""
    library = window.library
    module = sys.modules[type(library).__module__]
    workers: list[Any] = []
    monkeypatch.setattr(
        module,
        "Thread",
        lambda *, target: SimpleNamespace(start=lambda: workers.append(target)),
    )
    monkeypatch.setattr(
        module.requests,
        "get",
        lambda *_args, **_kwargs: SimpleNamespace(status_code=200, text="0"),
    )
    monkeypatch.setattr(
        module,
        "unzip_parts",
        lambda *_args: seed_catalog(Path(library.partsdb_file), 73, lcsc="C200"),
    )
    module.wx.PostEvent.reset_mock()
    window.update_library()
    assert len(workers) == 1
    workers[0]()
    for call in module.wx.PostEvent.call_args_list:
        event = call.args[1]
        if isinstance(event, module.DownloadCompletedEvent):
            window.download_completed(event)
        elif isinstance(event, module.DownloadFinishedEvent):
            window.download_finished(event)
    assert window.is_catalog_available() is True


@pytest.mark.parametrize("action", ["paste", "apply", "picker"])
@pytest.mark.parametrize("setting", ["selected_library", "data_path"])
@pytest.mark.parametrize("recovery", ["download", "switch_back"])
def test_missing_catalog_explains_assignment_then_retry_survives_reopen(
    configuration_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    action: str,
    setting: str,
    recovery: str,
) -> None:
    """Every explicit assignment explains refusal and works after catalog recovery."""
    window = configuration_window(catalog_lcsc="C200")
    window.library.save_part_preferences([("R_0603", "10k", "C200")])
    previous = _switch_to_missing(window, setting, tmp_path)
    library, store = window.library, window.store
    _watch_action(window, mainwindow)
    before = _state(window, store)
    catalog_path = Path(library.partsdb_file)
    before_catalog = catalog_path.read_bytes() if catalog_path.exists() else None

    act(action, window, mainwindow, monkeypatch, "C200")

    _assert_refused(window, library, store, before)
    mainwindow.wx.PostEvent.assert_not_called()
    assert (
        catalog_path.read_bytes() if catalog_path.exists() else None
    ) == before_catalog
    if recovery == "download":
        _download_catalog(window, monkeypatch)
        expected_stock = 73
    else:
        window.update_settings(
            SimpleNamespace(section="library", setting=setting, value=previous)
        )
        assert window.is_catalog_available() is True
        library.update.assert_not_called()
        expected_stock = 1000
    _select_row(window)
    window.logger.reset_mock()
    if action == "picker":
        window.assign_parts(
            SimpleNamespace(
                references=["R1"], lcsc="C200", type="Basic", stock=expected_stock
            )
        )
    else:
        act(action, window, mainwindow, monkeypatch, "C200")
    assert window.store.get_part("R1")["lcsc"] == "C200"
    assert window.store.get_part("R1")["stock"] == expected_stock
    assert window.partlist_data_model.data[0][3] == "C200"
    assert window.partlist_data_model.data[0][5] == expected_stock
    assert window.pcbnew.GetBoard().FindFootprintByReference("R1").field.text == "C200"
    reopened = mainwindow.Store(window, window.project_path, window.pcbnew.GetBoard())
    assert reopened.get_part("R1")["lcsc"] == "C200"
    assert reopened.get_part("R1")["stock"] == expected_stock
    assert _warnings(window) == []


@pytest.mark.parametrize("action", ["paste", "apply", "picker"])
def test_pending_source_switch_explains_assignment_without_using_old_catalog(
    configuration_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    action: str,
) -> None:
    """A queued source switch cannot silently discard an action or use old details."""
    window = configuration_window(catalog_lcsc="C200")
    library = window.library
    module = sys.modules[type(library).__module__]
    monkeypatch.setattr(
        module, "Thread", lambda *, target: SimpleNamespace(start=lambda: None)
    )
    window.update_library()
    window.update_settings(
        SimpleNamespace(
            section="library", setting="data_path", value=str(tmp_path / "next")
        )
    )
    assert window._catalog_switch_pending is True
    assert library.is_download_running() is True
    assert window.is_catalog_available() is False
    _select_row(window)
    _watch_action(window, mainwindow)
    before = _state(window, window.store)

    act(action, window, mainwindow, monkeypatch, "C200")

    _assert_refused(window, library, window.store, before)
    mainwindow.wx.PostEvent.assert_not_called()


@pytest.mark.parametrize("action", ["paste", "apply", "picker"])
def test_late_assignment_with_no_library_explains_failure_before_optional_lookup(
    configuration_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    action: str,
) -> None:
    """Queued actions remain safe when failed reinitialization has removed the library."""
    window = configuration_window()
    _select_row(window)
    library, store = window.library, window.store
    invalid_path = tmp_path / "regular-file"
    invalid_path.write_text("not a directory")
    window.settings["library"]["data_path"] = str(invalid_path)
    window.library = None
    window.init_data(download_if_missing=False)
    assert window.library is None
    assert window.store is None
    assert window._project_storage_unavailable is True
    for name in (
        "get_part_details",
        "get_part_preference",
        "save_part_preferences",
        "update",
    ):
        setattr(library, name, MagicMock(wraps=getattr(library, name)))
    store.set_lcsc_assignments = MagicMock(wraps=store.set_lcsc_assignments)
    window.logger.reset_mock()
    window.start_assembly_enrichment.reset_mock()
    before = _state(window, store)

    act(action, window, mainwindow, monkeypatch, "C200")

    assert _state(window, store) == before
    library.get_part_preference.assert_not_called()
    library.get_part_details.assert_not_called()
    library.save_part_preferences.assert_not_called()
    library.update.assert_not_called()
    store.set_lcsc_assignments.assert_not_called()
    messages = _warnings(window)
    assert len(messages) == 1
    assert any(
        word in messages[0].lower()
        for word in ("settings", "reopen", "download", "select")
    )


def test_automatic_empty_assignments_and_refresh_stay_quiet(
    configuration_window: Callable[..., Any], mainwindow: Any, tmp_path: Path
) -> None:
    """Unavailable catalog background work must not repeat user-action warnings."""
    window = configuration_window()
    _switch_to_missing(window, "selected_library", tmp_path)
    _watch_action(window, mainwindow)
    before = _state(window, window.store)
    assert window._apply_lcsc_assignments({}, notify=False) == []
    window._initialize_catalog_parts()
    window.recompute_stock_concerns()
    window.recompute_bom_estimate()
    assert _state(window, window.store) == before
    assert _warnings(window) == []
    window.library.get_part_details.assert_not_called()
    window.library.get_part_preference.assert_not_called()
    window.store.set_lcsc_assignments.assert_not_called()


@pytest.mark.parametrize("failure", ["catalog_read", "project_write"])
@pytest.mark.parametrize("action", ["paste", "apply", "picker"])
def test_real_assignment_storage_failure_keeps_single_original_warning(
    configuration_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    failure: str,
) -> None:
    """Availability feedback must not double-report SQL failures from a ready catalog."""
    window = configuration_window(catalog_lcsc="C200")
    window.library.save_part_preferences([("R_0603", "10k", "C200")])
    if failure == "catalog_read":
        with (
            closing(sqlite3.connect(window.library.partsdb_file)) as connection,
            connection,
        ):
            connection.execute("DROP TABLE parts")
    else:
        with closing(sqlite3.connect(window.store.dbfile)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER reject_assignment BEFORE UPDATE OF lcsc ON part_info "
                "BEGIN SELECT RAISE(ABORT, 'assignment write rejected'); END"
            )
    _select_row(window)
    before = _state(window, window.store)
    window.logger.reset_mock()

    act(action, window, mainwindow, monkeypatch, "C200")

    assert _state(window, window.store) == before
    messages = _warnings(window)
    assert len(messages) == 1
    assert (
        "no such table" if failure == "catalog_read" else "assignment write rejected"
    ) in messages[0]


def test_save_preferences_and_clear_remain_available_without_catalog(
    configuration_window: Callable[..., Any], mainwindow: Any, tmp_path: Path
) -> None:
    """Working project-only actions remain usable while catalog assignments wait."""
    window = configuration_window()
    _switch_to_missing(window, "selected_library", tmp_path)
    window.logger.reset_mock()
    window.save_selected_part_preferences()
    assert window.library.get_part_preference("R_0603", "10k") == "C100"
    window.remove_lcsc_number()
    assert window.store.get_part("R1")["lcsc"] == ""
    assert window.pcbnew.GetBoard().FindFootprintByReference("R1").field.text == ""
    assert window.partlist_data_model.data[0][3] == ""
    reopened = mainwindow.Store(window, window.project_path, window.pcbnew.GetBoard())
    assert reopened.get_part("R1")["lcsc"] == ""
    window.footprint_list.Enable.assert_called_with(True)
    window.right_toolbar.Enable.assert_called_with(True)
    assert _warnings(window) == []


@pytest.mark.parametrize("action", ["paste", "apply"])
def test_known_lcsc_missing_from_available_catalog_still_assigns(
    configuration_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    """A successful lookup with no record is distinct from catalog unavailability."""
    window = configuration_window()
    window.library.save_part_preferences([("R_0603", "10k", "C200")])
    _select_row(window)
    window.logger.reset_mock()
    assert window.is_catalog_available() is True

    act(action, window, mainwindow, monkeypatch, "C200")

    assert window.store.get_part("R1")["lcsc"] == "C200"
    assert window.store.get_part("R1")["stock"] is None
    assert window.partlist_data_model.data[0][3] == "C200"
    assert window.partlist_data_model.data[0][5] == ""
    assert window.pcbnew.GetBoard().FindFootprintByReference("R1").field.text == "C200"
    assert _warnings(window) == []
