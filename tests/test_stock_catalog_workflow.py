"""Regress catalog caching through real window and BOM-estimator workflows."""

from collections.abc import Callable, Iterator
from contextlib import closing
import importlib
import json
from pathlib import Path
import sqlite3
import sys
from threading import Lock
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

from . import (
    part_preferences_test_support as preferences_storage,
    test_stock_concern_controller as controller_tests,
)
from .stock_test_support import stock_modules
from .test_stock_concern import part
from .test_stock_download_lifecycle import seed_catalog

workflow = controller_tests.workflow
mainwindow = preferences_storage.mainwindow
make_window = preferences_storage.make_window


@pytest.fixture
def catalog_window(
    workflow: types.SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., Any]:
    """Use the real estimator, model, and event handlers against a counted catalog."""
    helpers = importlib.import_module(f"{workflow.models.package}.helpers")
    helpers.HighResWxSize = lambda _window, size: size
    pcbnew = types.ModuleType("pcbnew")
    pcbnew.F_Cu = 0
    monkeypatch.setitem(sys.modules, "pcbnew", pcbnew)
    estimator = importlib.import_module(f"{workflow.models.package}.bom_widget")

    def make_window(
        records: list[dict[str, Any]], details: dict[str, dict[str, Any]]
    ) -> Any:
        """Provide stateful catalog and estimator outputs for the production window."""
        for record in records:
            record.update(pad_count=2, has_tht=0, component_product_type=0)
        window = workflow.make_window(records, {})
        window.library.state = workflow.mainwindow.LibraryState.INITIALIZED
        window.library.is_download_running.return_value = False
        window.library.get_parts_db_info.return_value = None
        window.SetTitle = MagicMock()
        window.library.get_part_details.side_effect = lambda lcsc: details.get(lcsc, {})
        window.bom_estimator_board_count = 5
        window.bom_estimator_force_standard = False
        window._why_standard_dialog = None
        window._part_selector = None
        window._project_storage_unavailable = False
        window.assembly_enrichment_generation = 0
        window.pending_assembly_enrichment = set()
        window.project_storage_status = MagicMock()
        window.right_toolbar = MagicMock()
        window.upper_toolbar = MagicMock()
        window.Layout = MagicMock()
        window.catalog_summaries = []
        window.bom_estimator_controller = estimator.BomEstimatorController(
            read_parts=window.store.read_all,
            get_part_details=window._bom_get_part_details,
            get_board=window.pcbnew.GetBoard,
            is_force_standard_enabled=lambda: window.bom_estimator_force_standard,
            set_price_label=window.partlist_data_model.set_bom_price,
            set_standard_only_refs=window.partlist_data_model.set_standard_only_refs,
            set_summary_text=window.catalog_summaries.append,
            set_details_button_label=lambda _label: None,
        )
        window.recompute_bom_estimate = types.MethodType(
            workflow.mainwindow.JLCPCBTools.recompute_bom_estimate, window
        )
        return window

    return make_window


@pytest.fixture
def configuration_window(
    make_window: Callable[..., Any],
    mainwindow: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[..., Any]]:
    """Exercise actual project/catalog storage and handlers behind stateful controls."""
    library_module = sys.modules[mainwindow.Library.__module__]
    monkeypatch.setattr(
        mainwindow.Library, "_start_initial_remote_corrections", lambda *_: None
    )
    monkeypatch.setattr(mainwindow, "PLUGIN_PATH", str(tmp_path))
    monkeypatch.setattr(
        mainwindow.kicad_pcbnew, "GetBuildVersion", lambda: "test", raising=False
    )
    with stock_modules() as models:

        def create(
            *, settings: Any = None, seed: bool = True, catalog_lcsc: str = "C100"
        ) -> Any:
            """Create a window, optionally reopening settings from its saved JSON."""
            if settings is None:
                settings = {"library": {"data_path": str(tmp_path / "catalog")}}
            window = make_window(settings=settings)
            del window.populate_footprint_list
            window.partlist_data_model = models.datamodel.PartListDataModel(1.0)
            window.SetTitle = MagicMock()
            window.init_fabrication = MagicMock()
            window._part_selector = None
            window._catalog_ready = False
            window._catalog_details = {}
            window._part_preferences_applied_on_open = True
            window.library = None
            if seed:
                config = library_module.LIBRARY_CONFIGS[library_module.DEFAULT_LIBRARY]
                path = Path(settings["library"]["data_path"]) / config.name
                if not path.exists():
                    seed_catalog(path, 1000, lcsc=catalog_lcsc)
            window.init_data()
            return window

        yield create


def details(stock: object, price: str = "0.10") -> dict[str, Any]:
    """Return raw catalog fields shared by display, availability, and estimation."""
    return {"stock": stock, "type": "Basic", "price": f"1-:{price}"}


def raw_stocks(window: Any) -> dict[str, object]:
    """Read exact model values rather than comparing rounded stock labels."""
    return {row[0]: row[5] for row in window.partlist_data_model.data}


def test_warmed_catalog_is_shared_across_real_estimate_concerns_and_bom_events(
    workflow: types.SimpleNamespace,
    catalog_window: Callable[..., Any],
) -> None:
    """Filtering and event bursts must not run catalog SQL after initial population."""
    window = catalog_window(
        [part("R1"), part("R2"), part("U1", "C2", exclude_from_pos=True)],
        {"C1": details(99), "C2": details(1000)},
    )
    window.hide_pos_parts = True
    window.populate_footprint_list()
    workflow.drain()
    assert raw_stocks(window) == {"R1": 99, "R2": 99}
    assert "Direct BOM Cost: $1.50" in window.catalog_summaries[-1]
    assert [call.args for call in window.library.get_part_details.call_args_list] == [
        ("C1",),
        ("C2",),
    ]

    for _ in range(3):
        window.on_bom_data_changed(types.SimpleNamespace())
    window._refresh_bom_after_enrichment_update()
    window.hide_pos_parts = False
    window.populate_footprint_list()
    window._set_bom_estimator_board_count(10)
    for enabled in (False, True):
        window.update_settings(
            types.SimpleNamespace(
                section="highlighting", setting="stock_concern", value=enabled
            )
        )
    workflow.drain()
    assert raw_stocks(window) == {"R1": 99, "R2": 99, "U1": 1000}
    assert window.partlist_data_model.stock_concern_refs == {"R1", "R2"}
    assert "Direct BOM Cost: $3.00" in window.catalog_summaries[-1]
    assert window.library.get_part_details.call_count == 2


def test_new_and_equivalently_spelled_lcsc_numbers_share_one_lookup(
    workflow: types.SimpleNamespace,
    catalog_window: Callable[..., Any],
) -> None:
    """Canonical IDs should serve table population, grouped concerns, and estimation."""
    window = catalog_window([part("R1")], {"C1": details(1000), "C2": details(99)})
    window.populate_footprint_list()
    workflow.drain()
    window.library.get_part_details.reset_mock()
    for reference, lcsc in (("R2", " c2 "), ("R3", "C2")):
        window.store.parts[reference] = part(reference, lcsc)
        window.footprints[reference] = controller_tests.Footprint(lcsc)
    window.populate_footprint_list()
    workflow.drain()
    assert raw_stocks(window) == {"R1": 1000, "R2": 99, "R3": 99}
    assert window.partlist_data_model.stock_concern_refs == {"R2", "R3"}
    window.library.get_part_details.assert_called_once_with("C2")


@pytest.mark.parametrize("failure", [sqlite3.OperationalError, OSError])
def test_catalog_caches_missing_records_but_retries_transient_failures(
    catalog_window: Callable[..., Any],
    failure: type[Exception],
) -> None:
    """An absent component is stable until refresh; a failed query is retryable."""
    window = catalog_window([], {})
    assert window._catalog_get_part_details("C404") == {}
    assert window._catalog_get_part_details(" c404 ") == {}
    window.library.get_part_details.assert_called_once_with("C404")

    window.library.get_part_details.reset_mock()
    window.library.get_part_details.side_effect = [
        failure("temporary catalog failure"),
        details(500),
    ]
    assert window._catalog_get_part_details("C1") == {}
    assert window._catalog_get_part_details("C1")["stock"] == 500
    assert window._catalog_get_part_details("C1")["stock"] == 500
    assert window.library.get_part_details.call_count == 2


def test_catalog_returns_defensive_copies_and_skips_unassigned_ids(
    catalog_window: Callable[..., Any],
) -> None:
    """A caller cannot alter later price or stock reads through a returned mapping."""
    record = details(500)
    window = catalog_window([], {"C1": record})
    first = window._catalog_get_part_details("C1")
    first["stock"] = 0
    first["price"] = "1-:100"
    assert window._catalog_get_part_details("C1") == details(500)
    assert record == details(500)
    assert window._catalog_get_part_details("") == {}
    assert window._catalog_get_part_details("  ") == {}
    window.library.get_part_details.assert_called_once_with("C1")


def test_successful_retry_updates_the_previously_blank_stock_cell(
    workflow: types.SimpleNamespace,
    catalog_window: Callable[..., Any],
) -> None:
    """A transient row lookup failure must not leave display and later concerns apart."""
    window = catalog_window([part("R1")], {})
    window.library.get_part_details.side_effect = [
        sqlite3.OperationalError("catalog is briefly locked"),
        details(99),
    ]
    window.populate_footprint_list()
    assert raw_stocks(window) == {"R1": ""}
    assert window.store is not None
    workflow.drain()
    assert raw_stocks(window) == {"R1": 99}
    assert window.partlist_data_model.stock_concern_refs == set()
    assert "Direct BOM Cost: $0.50" in window.catalog_summaries[-1]
    assert window.library.get_part_details.call_count == 2


def test_catalog_cache_belongs_to_the_window_and_active_library(
    catalog_window: Callable[..., Any],
) -> None:
    """Two project windows using different catalogs must not share part-detail entries."""
    first = catalog_window([], {"C1": details(500)})
    second = catalog_window([], {"C1": details(1000)})
    assert first._catalog_get_part_details("C1")["stock"] == 500
    assert second._catalog_get_part_details("C1")["stock"] == 1000
    assert first._catalog_get_part_details("C1")["stock"] == 500
    first.library.get_part_details.assert_called_once_with("C1")
    second.library.get_part_details.assert_called_once_with("C1")


@pytest.mark.parametrize("setting", ["selected_library", "data_path"])
def test_ready_catalog_switch_refreshes_stock_estimate_concerns_and_selector(
    workflow: types.SimpleNamespace,
    catalog_window: Callable[..., Any],
    setting: str,
) -> None:
    """A settings-driven catalog switch must replace every old catalog-dependent view."""
    catalog = {"C1": details(1000, "0.10")}
    window = catalog_window([part("R1")], catalog)
    window.populate_footprint_list()
    workflow.drain()
    assert "Direct BOM Cost: $0.50" in window.catalog_summaries[-1]
    assert window.partlist_data_model.stock_concern_refs == set()
    window._part_selector = MagicMock()

    def switch_catalog() -> None:
        """Keep the library identity while switching its underlying data source."""
        catalog["C1"] = details(49, "0.25")

    window.library.refresh_library_config.side_effect = switch_catalog
    window.library.get_part_details.reset_mock()
    window.update_settings(
        types.SimpleNamespace(section="library", setting=setting, value="catalog-B")
    )
    workflow.drain()
    assert raw_stocks(window) == {"R1": 49}
    assert window.partlist_data_model.stock_concern_refs == {"R1"}
    assert "Direct BOM Cost: $1.25" in window.catalog_summaries[-1]
    window.library.get_part_details.assert_called_once_with("C1")
    window._part_selector.refresh_catalog.assert_called_once_with()
    window.library.update.assert_not_called()


@pytest.mark.parametrize("setting", ["selected_library", "data_path"])
def test_missing_catalog_clears_stale_values_without_querying_then_download_recovers(
    workflow: types.SimpleNamespace,
    catalog_window: Callable[..., Any],
    setting: str,
) -> None:
    """A not-yet-downloaded catalog cannot retain old stock, cost, or concern marks."""
    catalog = {"C1": details(1, "0.10")}
    window = catalog_window([part("R1")], catalog)
    window.populate_footprint_list()
    workflow.drain()
    assert window.partlist_data_model.stock_concern_refs == {"R1"}
    window._part_selector = MagicMock()

    def switch_to_missing_catalog() -> None:
        """Model refresh_library_config finding no ready database at the new path."""
        window.library.state = workflow.mainwindow.LibraryState.UPDATE_NEEDED

    window.library.refresh_library_config.side_effect = switch_to_missing_catalog
    window.library.get_part_details.reset_mock()
    window.update_settings(
        types.SimpleNamespace(section="library", setting=setting, value="missing")
    )
    workflow.drain()
    assert raw_stocks(window)["R1"] in ("", None)
    assert window.partlist_data_model.stock_concern_refs == set()
    assert "Direct BOM Cost: $0.50" not in window.catalog_summaries[-1]
    window.library.get_part_details.assert_not_called()
    window.library.update.assert_not_called()
    window._part_selector.refresh_catalog.assert_called_once_with()

    catalog["C1"] = details(1000, "0.50")
    window.library.state = workflow.mainwindow.LibraryState.INITIALIZED
    window.download_completed()
    workflow.drain()
    assert raw_stocks(window) == {"R1": 1000}
    assert window.partlist_data_model.stock_concern_refs == set()
    assert "Direct BOM Cost: $2.50" in window.catalog_summaries[-1]
    window.library.get_part_details.assert_called_once_with("C1")
    assert window._part_selector.refresh_catalog.call_count == 2


def test_explicit_update_completion_invalidates_confirmed_missing_catalog_results(
    configuration_window: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The actual Update command publishes a downloaded record absent from the cache."""
    window = configuration_window(catalog_lcsc="C999")
    library = window.library
    window.populate_footprint_list()
    window.recompute_stock_concerns()
    assert window._catalog_get_part_details("C100") == {}
    assert window.partlist_data_model.stock_concern_refs == {"R1"}
    window._part_selector = MagicMock()
    library_module = sys.modules[type(library).__module__]
    workers: list[Any] = []
    monkeypatch.setattr(
        library_module,
        "Thread",
        lambda *, target: types.SimpleNamespace(start=lambda: workers.append(target)),
    )
    monkeypatch.setattr(
        library_module.requests,
        "get",
        lambda *_args, **_kwargs: types.SimpleNamespace(status_code=200, text="0"),
    )

    def extract(*_args: Any) -> None:
        """Replace the fixture database as the real extraction stage would."""
        Path(library.partsdb_file).unlink()
        seed_catalog(Path(library.partsdb_file), 500, lcsc="C100")

    monkeypatch.setattr(library_module, "unzip_parts", extract)
    window.update_library()
    assert len(workers) == 1
    assert window._catalog_get_part_details("C100") == {}
    workers[0]()
    events = [call.args[1] for call in library_module.wx.PostEvent.call_args_list]
    for event in events:
        if isinstance(event, library_module.DownloadCompletedEvent):
            window.download_completed(event)
        elif isinstance(event, library_module.DownloadFinishedEvent):
            window.download_finished(event)
    assert window.is_catalog_available() is True
    assert raw_stocks(window) == {"R1": 500}
    assert window._catalog_get_part_details("C100")["stock"] == 500
    assert window.partlist_data_model.stock_concern_refs == set()
    window._part_selector.refresh_catalog.assert_called_once_with()


@pytest.mark.parametrize("ready", [False, True])
def test_real_constructor_scopes_cache_and_readiness_to_initialized_library(
    monkeypatch: pytest.MonkeyPatch, ready: bool
) -> None:
    """The actual constructor and init_data gate lookups until catalog initialization."""
    from . import test_window_layout as layout_ui
    from .stock_test_support import stock_modules

    module = layout_ui.mainwindow
    monkeypatch.setattr(module.JLCPCBTools, "SetTitle", MagicMock(), raising=False)
    monkeypatch.setattr(
        module.kicad_pcbnew, "GetBuildVersion", lambda: "test", raising=False
    )
    with stock_modules() as models:
        monkeypatch.setattr(
            module, "PartListDataModel", models.datamodel.PartListDataModel
        )
        monkeypatch.setattr(
            module.dv.DataViewCtrl,
            "return_value",
            layout_ui._control(scale=2, client_width=0, stretch=True),
        )
        monkeypatch.setattr(
            module.JLCPCBTools,
            "load_settings",
            lambda window: setattr(
                window,
                "settings",
                {"general": {"bom_estimator_boards": 100, "bom_estimator_show": False}},
            ),
        )
        monkeypatch.setattr(
            module.JLCPCBTools,
            "init_logger",
            lambda window: setattr(window, "logger", MagicMock()),
        )
        monkeypatch.setattr(module.JLCPCBTools, "init_fabrication", MagicMock())
        initialize_store = MagicMock()
        monkeypatch.setattr(module.JLCPCBTools, "init_store", initialize_store)
        library = MagicMock()
        library.state = (
            module.LibraryState.INITIALIZED
            if ready
            else module.LibraryState.UPDATE_NEEDED
        )
        library.get_parts_db_info.return_value = None
        library.get_part_details.return_value = details(1000)
        monkeypatch.setattr(module, "Library", MagicMock(return_value=library))
        provider = MagicMock()
        provider.get_pcbnew().GetBoard().GetFileName.return_value = "test.kicad_pcb"
        window = module.JLCPCBTools(None, provider)
        assert window._catalog_details == {}
        assert window.is_catalog_available() is ready
        assert window.bom_estimator_board_count == 100
        assert window.bom_estimator_show is False
        assert initialize_store.call_count == int(ready)
        assert library.update.call_count == int(not ready)
        assert window._catalog_get_part_details("C1") == (
            details(1000) if ready else {}
        )
        assert library.get_part_details.call_count == int(ready)
        library.state = module.LibraryState.INITIALIZED
        library.get_part_details.return_value = details(49)
        window.init_library()
        assert window._catalog_details == {}
        assert window.is_catalog_available() is True
        assert window._catalog_get_part_details("C1") == details(49)
        window.save_settings = MagicMock()
        window.Close()
        layout_ui._drain_callbacks()


@pytest.mark.parametrize("fail_write", [False, True])
def test_assignment_publishes_selected_stock_to_cached_siblings_only_after_commit(
    workflow: types.SimpleNamespace,
    catalog_window: Callable[..., Any],
    fail_write: bool,
) -> None:
    """A failed assignment cannot publish supply; a committed one updates siblings."""
    window = catalog_window(
        [part("R1"), part("R2", "C2")], {"C1": details(99), "C2": details(1000)}
    )
    window.populate_footprint_list()
    workflow.drain()
    window.library.get_part_details.reset_mock()
    window.store.fail_write = fail_write
    window.assign_parts(
        types.SimpleNamespace(lcsc="C1", stock="999", type="Basic", references=["R2"])
    )
    workflow.drain()
    assert window._bom_get_part_details("C1")["stock"] == (99 if fail_write else "999")
    assert raw_stocks(window) == (
        {"R1": 99, "R2": 1000} if fail_write else {"R1": "999", "R2": "999"}
    )
    window.library.get_part_details.assert_not_called()


@pytest.mark.parametrize("failure", [sqlite3.OperationalError, OSError])
def test_assignment_catalog_failure_does_not_commit_incomplete_details(
    workflow: types.SimpleNamespace,
    catalog_window: Callable[..., Any],
    failure: type[Exception],
) -> None:
    """Retryable catalog errors cannot silently turn a selection into partial data."""
    window = catalog_window([part("R1")], {"C1": details(99)})
    window.populate_footprint_list()
    workflow.drain()
    window.library.get_part_details.side_effect = failure("cannot read selected part")
    window.assign_parts(
        types.SimpleNamespace(lcsc="C2", stock="999", type="Basic", references=["R1"])
    )
    workflow.drain()
    assert window.store.parts["R1"]["lcsc"] == "C1"
    assert raw_stocks(window) == {"R1": 99}
    assert "C2" not in window._catalog_details


@pytest.mark.parametrize("stock", [0.1, True, -1, "5+"])
@pytest.mark.parametrize("action", ["selector", "preferences"])
def test_assignment_preserves_unparseable_stock_as_unknown_in_storage(
    workflow: types.SimpleNamespace,
    catalog_window: Callable[..., Any],
    stock: object,
    action: str,
) -> None:
    """Malformed quantities must never become a fabricated exact inventory count."""
    window = catalog_window([part("R1")], {"C1": details(99), "C2": details(stock)})
    window.populate_footprint_list()
    workflow.drain()
    if action == "selector":
        window.assign_parts(
            types.SimpleNamespace(
                lcsc="C2", stock=stock, type="Basic", references=["R1"]
            )
        )
    else:
        window._apply_lcsc_assignments({"R1": "C2"})
    workflow.drain()
    assert window.store.parts["R1"]["lcsc"] == "C2"
    assert window.store.parts["R1"]["stock"] is None
    assert raw_stocks(window)["R1"] == stock
    assert window.partlist_data_model.stock_concern_refs == {"R1"}


@pytest.mark.parametrize("succeeded", [False, True])
def test_catalog_switch_waits_for_active_download_and_ignores_old_completion(
    workflow: types.SimpleNamespace,
    catalog_window: Callable[..., Any],
    succeeded: bool,
) -> None:
    """Only the latest selected catalog may become ready after an in-flight download."""
    catalog = {"C1": details(1)}
    window = catalog_window([part("R1")], catalog)
    library = window.library
    library.selected_library = "catalog-A"
    library.partsdb_file = "/catalog-A.db"
    window.populate_footprint_list()
    workflow.drain()
    library.is_download_running.return_value = True
    library.get_part_details.reset_mock()
    for selected in ("catalog-B", "catalog-C"):
        window.update_settings(
            types.SimpleNamespace(
                section="library", setting="selected_library", value=selected
            )
        )
    library.refresh_library_config.assert_not_called()
    library.update.assert_not_called()
    assert window.is_catalog_available() is False
    assert window.partlist_data_model.stock_concern_refs == set()
    assert raw_stocks(window) == {"R1": ""}
    event = types.SimpleNamespace(
        library=library, source=("catalog-A", "/catalog-A.db"), succeeded=succeeded
    )
    window.download_completed(event)
    assert window.is_catalog_available() is False
    library.get_part_details.assert_not_called()

    def select_latest() -> None:
        """Activate the most recent request only after the prior worker has stopped."""
        assert window.settings["library"]["selected_library"] == "catalog-C"
        library.selected_library = "catalog-C"
        library.partsdb_file = "/catalog-C.db"
        library.state = workflow.mainwindow.LibraryState.UPDATE_NEEDED

    library.refresh_library_config.side_effect = select_latest
    library.is_download_running.return_value = False
    window.download_finished(event)
    library.refresh_library_config.assert_called_once_with()
    library.update.assert_not_called()
    assert window.is_catalog_available() is False
    # A queued completion from A must not turn C ready even after config changes.
    window.download_completed(event)
    assert window.is_catalog_available() is False
    library.get_part_details.assert_not_called()
    catalog["C1"] = details(500)
    library.state = workflow.mainwindow.LibraryState.INITIALIZED
    window.download_completed(
        types.SimpleNamespace(library=library, source=("catalog-C", "/catalog-C.db"))
    )
    workflow.drain()
    assert window.is_catalog_available() is True
    assert raw_stocks(window) == {"R1": 500}
    library.get_part_details.assert_called_once_with("C1")


def test_queued_completion_cannot_publish_over_a_new_active_download(
    workflow: types.SimpleNamespace,
    catalog_window: Callable[..., Any],
) -> None:
    """Even an unchanged source must finish its current worker before publishing."""
    catalog = {"C1": details(1000)}
    window = catalog_window([part("R1")], catalog)
    window.populate_footprint_list()
    workflow.drain()
    library = window.library
    source = (library.selected_library, library.partsdb_file)
    event = types.SimpleNamespace(library=library, source=source)
    catalog["C1"] = details(49)
    library.get_part_details.reset_mock()
    library.is_download_running.return_value = True
    window.download_completed(event)
    assert raw_stocks(window) == {"R1": 1000}
    library.get_part_details.assert_not_called()
    library.is_download_running.return_value = False
    window.download_completed(event)
    workflow.drain()
    assert raw_stocks(window) == {"R1": 49}
    assert window.partlist_data_model.stock_concern_refs == {"R1"}
    library.get_part_details.assert_called_once_with("C1")


@pytest.mark.parametrize("failure", [sqlite3.DatabaseError, OSError])
def test_metadata_failure_shows_storage_error_and_settings_retry_recovers(
    configuration_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[Exception],
) -> None:
    """Unpublishable catalog metadata exposes storage recovery and a valid retry clears it."""
    window = configuration_window()
    library = window.library
    window._part_selector = MagicMock()
    original_metadata = library.get_parts_db_info
    monkeypatch.setattr(
        library,
        "get_parts_db_info",
        MagicMock(side_effect=failure("replacement metadata failed")),
    )
    window.download_completed()
    assert window.is_catalog_available() is False
    assert window._catalog_details == {}
    assert window._project_storage_unavailable is True
    assert window.store is None
    assert library.state != mainwindow.LibraryState.INITIALIZED
    assert window.upper_toolbar.enabled[mainwindow.ID_GENERATE] is False
    assert "unavailable" in window.project_storage_status.SetLabel.call_args.args[0]
    assert window.partlist_data_model.stock_concern_refs == set()
    monkeypatch.setattr(library, "get_parts_db_info", original_metadata)
    window.update_settings(
        types.SimpleNamespace(
            section="library", setting="data_path", value=library.datadir
        )
    )
    assert window.is_catalog_available() is True
    assert window._project_storage_unavailable is False
    assert window.store is not None
    assert raw_stocks(window) == {"R1": 1000}
    assert window.upper_toolbar.enabled[mainwindow.ID_GENERATE] is True


def test_failed_replacement_invalidates_stock_and_rejects_older_same_source_success(
    workflow: types.SimpleNamespace,
    catalog_window: Callable[..., Any],
) -> None:
    """After a newer attempt fails, queued success from an older attempt is stale."""
    window = catalog_window([part("R1")], {"C1": details(1000)})
    window.populate_footprint_list()
    workflow.drain()
    library = window.library
    source = (library.selected_library, library.partsdb_file)
    library.download_attempt = 2
    library.state = workflow.mainwindow.LibraryState.UPDATE_NEEDED
    window.download_finished(
        types.SimpleNamespace(
            library=library, source=source, attempt=2, succeeded=False
        )
    )
    assert window.is_catalog_available() is False
    assert window._catalog_details == {}
    assert raw_stocks(window) == {"R1": ""}
    window.download_completed(
        types.SimpleNamespace(library=library, source=source, attempt=1)
    )
    assert window.is_catalog_available() is False
    assert raw_stocks(window) == {"R1": ""}


@pytest.mark.parametrize("early_store", [False, True])
def test_first_download_source_switch_still_initializes_saved_part_preferences(
    make_window: Callable[..., Any], mainwindow: Any, tmp_path: Path, early_store: bool
) -> None:
    """Deferred catalog readiness must run saved assignments against real project DBs."""
    window = make_window(
        footprints=[preferences_storage.Footprint(lcsc="")],
        part_preferences={("R_0603", "10k"): "C200"},
    )
    with stock_modules() as models:
        window.partlist_data_model = models.datamodel.PartListDataModel(1.0)
        del window.populate_footprint_list
        library = window.library
        library.selected_library = "catalog-A"
        library.partsdb_file = "/unused-catalog-A.db"
        library._download_attempt = 1
        library.download_lock = Lock()
        running = [True]
        library.is_download_running = lambda: running[0]
        library.get_parts_db_info = MagicMock(return_value=None)
        library.update = MagicMock()
        window.SetTitle = MagicMock()
        window.save_settings = MagicMock()
        window._catalog_ready = False
        window.store = None
        library.state = mainwindow.LibraryState.DOWNLOAD_RUNNING
        if early_store:
            window.init_store()
            assert window.store.get_part("R1")["lcsc"] == ""
        window.update_settings(
            types.SimpleNamespace(
                section="library", setting="selected_library", value="catalog-B"
            )
        )

        def select_target() -> None:
            library.selected_library = "catalog-B"
            library.partsdb_file = "/unused-catalog-B.db"
            library.state = mainwindow.LibraryState.UPDATE_NEEDED

        library.refresh_library_config = MagicMock(side_effect=select_target)
        running[0] = False
        window.download_finished(
            types.SimpleNamespace(
                library=library,
                source=("catalog-A", "/unused-catalog-A.db"),
                attempt=1,
                succeeded=False,
            )
        )
        library._download_attempt = 2
        library.partsdb_file = str(tmp_path / "downloaded-catalog-B.db")
        seed_catalog(Path(library.partsdb_file), stock=27, lcsc="C200")
        library.state = mainwindow.LibraryState.INITIALIZED
        event = types.SimpleNamespace(
            library=library, source=("catalog-B", library.partsdb_file), attempt=2
        )
        window.download_completed(event)
        assert window.store.get_part("R1")["lcsc"] == "C200"
        assert (
            window.pcbnew.GetBoard().FindFootprintByReference("R1").field.text == "C200"
        )
        assert window.partlist_data_model.data[0][3] == "C200"
        assert window.partlist_data_model.data[0][5] == 27
        window.start_assembly_enrichment.assert_called_once_with()
        library.get_part_preference.assert_called_once_with("R_0603", "10k")
        # Re-publication cannot refill a part that the user explicitly cleared.
        window.footprint_list.GetSelections.return_value = [
            window.partlist_data_model.data[0]
        ]
        window.remove_lcsc_number()
        window.download_completed(event)
        assert window.store.get_part("R1")["lcsc"] == ""
        library.get_part_preference.assert_called_once_with("R_0603", "10k")


@pytest.mark.parametrize("failure", ["path", "sqlite"])
def test_failed_source_change_disables_actions_and_valid_retry_recovers(
    configuration_window: Callable[..., Any],
    mainwindow: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """A storage failure remains visible until a successful Settings retry restores it."""
    window = configuration_window()
    library = window.library
    old_path = library.partsdb_file
    old_contents = Path(old_path).read_bytes()
    bad_path = tmp_path / "bad-destination"
    if failure == "path":
        bad_path.write_text("not a directory")
    original_setup = library.setup
    if failure == "sqlite":

        def fail_setup() -> None:
            """Model a SQLite setup failure after destination paths have changed."""
            raise sqlite3.OperationalError("destination database locked")

        monkeypatch.setattr(library, "setup", fail_setup)
    library.update = MagicMock()
    window.update_settings(
        types.SimpleNamespace(
            section="library", setting="data_path", value=str(bad_path)
        )
    )
    assert window._project_storage_unavailable is True
    assert "unavailable" in window.project_storage_status.SetLabel.call_args.args[0]
    window.project_storage_status.Show.assert_called_with(True)
    assert window.upper_toolbar.enabled[mainwindow.ID_GENERATE] is False
    assert window.upper_toolbar.enabled.get(mainwindow.ID_SETTINGS, True) is True
    window.footprint_list.Enable.assert_called_with(False)
    window.right_toolbar.Enable.assert_called_with(False)
    assert library.state != mainwindow.LibraryState.INITIALIZED
    assert window.is_catalog_available() is False
    assert window._catalog_details == {}
    assert raw_stocks(window) == {}
    assert Path(old_path).read_bytes() == old_contents
    library.update.assert_not_called()

    monkeypatch.setattr(library, "setup", original_setup)
    window.update_settings(
        types.SimpleNamespace(
            section="library", setting="data_path", value=str(Path(old_path).parent)
        )
    )
    assert window._project_storage_unavailable is False
    assert window.project_storage_status.SetLabel.call_args.args[0] == ""
    window.project_storage_status.Show.assert_called_with(False)
    assert window.upper_toolbar.enabled[mainwindow.ID_GENERATE] is True
    window.footprint_list.Enable.assert_called_with(True)
    window.right_toolbar.Enable.assert_called_with(True)
    assert window.store is not None
    assert window.is_catalog_available() is True
    assert raw_stocks(window) == {"R1": 1000}
    library.update.assert_not_called()


def test_saved_invalid_path_reopens_with_error_then_settings_recovers_without_download(
    configuration_window: Callable[..., Any],
    mainwindow: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reopening persisted invalid storage keeps Settings usable and never hides failure."""
    window = configuration_window()
    good_path = window.settings["library"]["data_path"]
    invalid_path = tmp_path / "regular-file"
    invalid_path.write_text("not a directory")
    window.update_settings(
        types.SimpleNamespace(
            section="library", setting="data_path", value=str(invalid_path)
        )
    )
    with (tmp_path / "settings.json").open() as saved:
        saved_settings = json.load(saved)
    assert saved_settings["library"]["data_path"] == str(invalid_path)
    reopened = configuration_window(settings=saved_settings, seed=False)
    assert reopened.library is None
    assert reopened._project_storage_unavailable is True
    assert reopened.upper_toolbar.enabled[mainwindow.ID_GENERATE] is False
    assert reopened.upper_toolbar.enabled.get(mainwindow.ID_SETTINGS, True) is True
    start_download = MagicMock()
    monkeypatch.setattr(mainwindow.Library, "update", start_download)
    reopened.update_settings(
        types.SimpleNamespace(section="library", setting="data_path", value=good_path)
    )
    assert reopened._project_storage_unavailable is False
    assert reopened.is_catalog_available() is True
    assert raw_stocks(reopened) == {"R1": 1000}
    start_download.assert_not_called()


def test_library_none_settings_recovery_to_missing_catalog_waits_for_explicit_update(
    configuration_window: Callable[..., Any],
    mainwindow: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repairing failed bootstrap can select a missing catalog without starting its download."""
    invalid_path = tmp_path / "regular-file"
    invalid_path.write_text("not a directory")
    window = configuration_window(
        settings={"library": {"data_path": str(invalid_path)}}, seed=False
    )
    assert window.library is None
    start_download = MagicMock()
    monkeypatch.setattr(mainwindow.Library, "update", start_download)
    window.update_settings(
        types.SimpleNamespace(
            section="library",
            setting="data_path",
            value=str(tmp_path / "missing-catalog"),
        )
    )
    start_download.assert_not_called()
    assert window.library is not None
    assert window.library.state == mainwindow.LibraryState.UPDATE_NEEDED
    assert window.is_catalog_available() is False
    window.update_library()
    start_download.assert_called_once_with()


def test_failed_update_keeps_valid_catalog_outputs_and_ignores_older_success(
    workflow: types.SimpleNamespace, catalog_window: Callable[..., Any]
) -> None:
    """A failed attempt can retain usable old data without accepting older queued events."""
    window = catalog_window([part("R1")], {"C1": details(49, "0.25")})
    window.populate_footprint_list()
    workflow.drain()
    library = window.library
    source = (library.selected_library, library.partsdb_file)
    library.download_attempt = 2
    window._part_selector = MagicMock()
    window.download_finished(
        types.SimpleNamespace(
            library=library, source=source, attempt=2, succeeded=False
        )
    )
    workflow.drain()
    assert window.is_catalog_available() is True
    assert raw_stocks(window) == {"R1": 49}
    assert window.partlist_data_model.stock_concern_refs == {"R1"}
    assert "Direct BOM Cost: $1.25" in window.catalog_summaries[-1]
    library.get_part_details.reset_mock()
    window.download_completed(
        types.SimpleNamespace(library=library, source=source, attempt=1)
    )
    assert window.is_catalog_available() is True
    library.get_part_details.assert_not_called()


def test_missing_catalog_on_open_still_starts_real_download_worker(
    configuration_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Suppressing Settings-triggered downloads must retain the opening workflow."""
    library_module = sys.modules[mainwindow.Library.__module__]
    workers: list[Any] = []
    monkeypatch.setattr(
        library_module,
        "Thread",
        lambda *, target: types.SimpleNamespace(start=lambda: workers.append(target)),
    )
    window = configuration_window(seed=False)
    assert len(workers) == 1
    assert window.library.is_download_running() is True
    assert window.is_catalog_available() is False


def test_failed_real_update_keeps_existing_catalog_display_and_assignments(
    configuration_window: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport failure through Update and posted events preserves the usable catalog."""
    window = configuration_window()
    library = window.library
    library_module = sys.modules[type(library).__module__]
    workers: list[Any] = []
    monkeypatch.setattr(
        library_module,
        "Thread",
        lambda *, target: types.SimpleNamespace(start=lambda: workers.append(target)),
    )
    monkeypatch.setattr(
        library_module.requests,
        "get",
        lambda *_args, **_kwargs: types.SimpleNamespace(status_code=503),
    )
    old_bytes = Path(library.partsdb_file).read_bytes()
    window.update_library()
    workers[0]()
    events = [call.args[1] for call in library_module.wx.PostEvent.call_args_list]
    for event in events:
        if isinstance(event, library_module.DownloadFinishedEvent):
            window.download_finished(event)
    assert window.is_catalog_available() is True
    assert Path(library.partsdb_file).read_bytes() == old_bytes
    assert raw_stocks(window) == {"R1": 1000}
    assert window.store.get_part("R1")["lcsc"] == "C100"
    assert window._project_storage_unavailable is False


@pytest.mark.parametrize(
    "contents", [b"partial database", b"SQLite format 3\x00truncated"]
)
def test_reopening_nonempty_corrupt_catalog_never_publishes_readiness(
    configuration_window: Callable[..., Any],
    mainwindow: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contents: bytes,
) -> None:
    """Saved invalid catalog bytes cannot become ready just because a file exists."""
    library_module = sys.modules[mainwindow.Library.__module__]
    destination = tmp_path / "corrupt-on-open"
    destination.mkdir()
    name = library_module.LIBRARY_CONFIGS[library_module.DEFAULT_LIBRARY].name
    (destination / name).write_bytes(contents)
    workers: list[Any] = []
    monkeypatch.setattr(
        library_module,
        "Thread",
        lambda *, target: types.SimpleNamespace(start=lambda: workers.append(target)),
    )
    window = configuration_window(
        settings={"library": {"data_path": str(destination)}}, seed=False
    )
    assert window.is_catalog_available() is False
    assert window.library.state != mainwindow.LibraryState.INITIALIZED
    assert window._catalog_details == {}


def test_deferred_return_to_corrupt_catalog_does_not_publish_it_as_ready(
    configuration_window: Callable[..., Any],
    mainwindow: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selecting away and back during a failed extraction cannot bypass catalog validation."""
    window = configuration_window()
    library = window.library
    library_module = sys.modules[type(library).__module__]
    original_directory = library.datadir
    workers: list[Any] = []
    monkeypatch.setattr(
        library_module,
        "Thread",
        lambda *, target: types.SimpleNamespace(start=lambda: workers.append(target)),
    )
    monkeypatch.setattr(
        library_module.requests,
        "get",
        lambda *_args, **_kwargs: types.SimpleNamespace(status_code=200, text="0"),
    )

    def interrupted_extract(*_args: Any) -> None:
        """Leave nonempty damaged bytes where the prior catalog was stored."""
        path = Path(library.partsdb_file)
        path.unlink()
        seed_catalog(path, lcsc="C100")
        with closing(sqlite3.connect(path)) as database, database:
            database.execute("DROP TABLE parts")
        raise OSError("extraction interrupted")

    monkeypatch.setattr(library_module, "unzip_parts", interrupted_extract)
    window.update_library()
    for path in (str(tmp_path / "other-source"), original_directory):
        window.update_settings(
            types.SimpleNamespace(section="library", setting="data_path", value=path)
        )
    assert window._catalog_switch_pending is True
    workers[0]()
    events = [call.args[1] for call in library_module.wx.PostEvent.call_args_list]
    for event in events:
        if isinstance(event, library_module.DownloadFinishedEvent):
            window.download_finished(event)
    assert window.is_catalog_available() is False
    assert library.state != mainwindow.LibraryState.INITIALIZED
    assert window._catalog_details == {}
    assert window.partlist_data_model.stock_concern_refs == set()
    assert len(workers) == 1


@pytest.mark.parametrize("invalid_date", ["not-a-date", None])
@pytest.mark.parametrize("opening", [True, False])
def test_malformed_metadata_keeps_opening_and_settings_recovery_usable(
    configuration_window: Callable[..., Any],
    mainwindow: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_date: Any,
    opening: bool,
) -> None:
    """Malformed dates expose recovery instead of aborting construction or Settings."""
    library_module = sys.modules[mainwindow.Library.__module__]
    destination = tmp_path / "malformed-metadata"
    name = library_module.LIBRARY_CONFIGS[library_module.DEFAULT_LIBRARY].name
    path = destination / name
    seed_catalog(path, stock=1000, lcsc="C100")
    with closing(sqlite3.connect(path)) as database, database:
        database.execute("UPDATE meta SET last_update = ?", (invalid_date,))
    start_download = MagicMock()
    monkeypatch.setattr(mainwindow.Library, "update", start_download)
    if opening:
        window = configuration_window(
            settings={"library": {"data_path": str(destination)}}, seed=False
        )
    else:
        invalid_path = tmp_path / "regular-file"
        invalid_path.write_text("not a directory")
        window = configuration_window(
            settings={"library": {"data_path": str(invalid_path)}}, seed=False
        )
        assert window.library is None
        window.update_settings(
            types.SimpleNamespace(
                section="library", setting="data_path", value=str(destination)
            )
        )
    assert window._project_storage_unavailable is True
    assert "unavailable" in window.project_storage_status.SetLabel.call_args.args[0]
    assert window.is_catalog_available() is False
    assert window.library.state != mainwindow.LibraryState.INITIALIZED
    assert window.upper_toolbar.enabled[mainwindow.ID_GENERATE] is False
    assert window.upper_toolbar.enabled.get(mainwindow.ID_SETTINGS, True) is True
    with closing(sqlite3.connect(path)) as database, database:
        database.execute("UPDATE meta SET last_update = ?", ("2026-09-10",))
    window.update_settings(
        types.SimpleNamespace(
            section="library", setting="data_path", value=str(destination)
        )
    )
    assert window._project_storage_unavailable is False
    assert window.is_catalog_available() is True
    assert raw_stocks(window) == {"R1": 1000}
    start_download.assert_not_called()
