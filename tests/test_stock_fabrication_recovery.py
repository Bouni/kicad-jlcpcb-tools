"""Exercise catalog recovery through real generation preflight before stopping at DRC."""

from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
from types import ModuleType, SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from . import part_preferences_test_support as storage
from .stock_test_support import stock_modules
from .test_stock_download_lifecycle import seed_catalog
from .wx_harness import load

mainwindow = storage.mainwindow
make_window = storage.make_window


class GenerationFootprint(storage.Footprint):
    """Supply placement geometry while retaining the real assignment fixture's fields."""

    def Pads(self) -> list[Any]:
        """Use the footprint origin when there are no pads."""
        return []

    def GetPosition(self) -> SimpleNamespace:
        """Return deterministic placement coordinates."""
        return SimpleNamespace(x=10, y=20)

    def GetOrientation(self) -> SimpleNamespace:
        """Return the unchanged board orientation."""
        return SimpleNamespace(AsDegrees=lambda: 0.0)


class GenerationBoard(storage.Board):
    """Expose read-only placement and zone inputs without a saved native board."""

    def __init__(self, path: Path) -> None:
        super().__init__([GenerationFootprint()])
        self.path = path

    def GetFileName(self) -> str:
        """Keep every constructor-created output directory inside the test project."""
        return str(self.path)

    def Footprints(self) -> list[storage.Footprint]:
        """Expose the same live footprints through the generation API."""
        return self.GetFootprints()

    def GetDesignSettings(self) -> SimpleNamespace:
        """Use a zero auxiliary origin."""
        return SimpleNamespace(GetAuxOrigin=lambda: SimpleNamespace(x=0, y=0))

    def Zones(self) -> list[Any]:
        """Exercise the real zone check with an empty board zone collection."""
        return []


class Status:
    """Retain the visible error, diagnostic tooltip, and banner visibility."""

    def __init__(self) -> None:
        self.label = ""
        self.tooltip = ""
        self.shown = False

    def SetLabel(self, label: str) -> None:
        """Replace the displayed error text."""
        self.label = label

    def SetToolTip(self, tooltip: str) -> None:
        """Replace the detailed diagnostic."""
        self.tooltip = tooltip

    def Show(self, shown: bool) -> None:
        """Retain banner visibility."""
        self.shown = shown


class ReadinessToolbar(storage.Toolbar):
    """Capture generator readiness at the instant Generate becomes enabled."""

    def __init__(self, window: Any, generate_id: int) -> None:
        super().__init__()
        self.window = window
        self.generate_id = generate_id
        self.ready_snapshots: list[SimpleNamespace] = []

    def EnableTool(self, tool: int, enabled: bool) -> None:
        """Record actual state before accepting a Generate enable transition."""
        if tool == self.generate_id and enabled:
            generator = getattr(self.window, "fabrication", None)
            self.ready_snapshots.append(
                SimpleNamespace(
                    generator=generator,
                    store=self.window.store,
                    unavailable=self.window._project_storage_unavailable,
                    directories_ready=generator is not None
                    and Path(generator.outputdir).is_dir()
                    and Path(generator.gerberdir).is_dir(),
                )
            )
        super().EnableTool(tool, enabled)


@pytest.fixture
def generation_window(
    mainwindow: ModuleType,
    make_window: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[Callable[[str], SimpleNamespace]]:
    """Combine real storage and Fabrication with stateful GUI and KiCad boundaries."""
    library_module = sys.modules[mainwindow.Library.__module__]
    monkeypatch.setattr(
        mainwindow.Library, "_start_initial_remote_corrections", lambda *_: None
    )
    monkeypatch.setattr(mainwindow, "PLUGIN_PATH", str(tmp_path))
    monkeypatch.setattr(
        mainwindow.kicad_pcbnew, "GetBuildVersion", lambda: "test", raising=False
    )
    pcbnew = MagicMock()
    pcbnew.F_Cu = 0
    pcbnew.B_Cu = 31
    pcbnew.FromMM = lambda value: value
    pcbnew.ToMM = lambda value: value
    pcbnew.wxPoint = lambda x, y: SimpleNamespace(x=x, y=y)
    fabrication = load(mainwindow.__package__, "fabrication", {"pcbnew": pcbnew})
    monkeypatch.setattr(mainwindow, "Fabrication", fabrication.Fabrication)
    constructors: list[Any] = []
    placements: list[tuple[tuple[Any, ...], ...]] = []
    consistency_checks: list[str] = []
    zone_checks: list[list[Any]] = []
    real_init = fabrication.Fabrication.__init__
    real_prepare = fabrication.Fabrication.prepare_cpl
    real_consistency = fabrication.Fabrication.get_part_consistency_warnings
    real_zones = fabrication.Fabrication.fill_zones

    def initialize(generator: Any, parent: Any, board: Any) -> None:
        """Count attempts while executing the complete production constructor."""
        constructors.append(generator)
        real_init(generator, parent, board)

    def prepare(
        generator: Any, corrections: Optional[tuple[Any, ...]] = None
    ) -> tuple[tuple[Any, ...], ...]:
        """Observe actual placement rows without replacing their calculation."""
        rows = real_prepare(generator, corrections)
        placements.append(rows)
        return rows

    def consistency(generator: Any) -> str:
        """Observe the real assignment consistency check."""
        result = real_consistency(generator)
        consistency_checks.append(result)
        return result

    def zones(generator: Any) -> list[Any]:
        """Observe the real zone check with zone refilling disabled."""
        result = real_zones(generator)
        zone_checks.append(result)
        return result

    monkeypatch.setattr(fabrication.Fabrication, "__init__", initialize)
    monkeypatch.setattr(fabrication.Fabrication, "prepare_cpl", prepare)
    monkeypatch.setattr(
        fabrication.Fabrication, "get_part_consistency_warnings", consistency
    )
    monkeypatch.setattr(fabrication.Fabrication, "fill_zones", zones)
    workers: list[Callable[[], None]] = []
    monkeypatch.setattr(
        library_module,
        "Thread",
        lambda *, target: SimpleNamespace(start=lambda: workers.append(target)),
    )
    monkeypatch.setattr(
        library_module.requests,
        "get",
        lambda *_args, **_kwargs: SimpleNamespace(status_code=200, text="0"),
    )
    monkeypatch.setattr(mainwindow.wx, "BeginBusyCursor", lambda: None, raising=False)
    monkeypatch.setattr(mainwindow.wx, "IsBusy", lambda: False, raising=False)
    message_box = MagicMock()
    monkeypatch.setattr(mainwindow.wx, "MessageBox", message_box, raising=False)
    with stock_modules() as models:

        def create(failure: str = "healthy") -> SimpleNamespace:
            """Open saved catalog state through the real startup methods."""
            directory = tmp_path / "catalog"
            config = library_module.LIBRARY_CONFIGS[library_module.DEFAULT_LIBRARY]
            catalog_path = directory / config.name
            if failure == "library-path":
                directory.write_text("A file where a catalog directory is required")
            else:
                seed_catalog(catalog_path, lcsc="C100")
                if failure in ("corrupt", "metadata"):
                    with closing(sqlite3.connect(catalog_path)) as database, database:
                        database.execute(
                            "DROP TABLE parts"
                            if failure == "corrupt"
                            else "UPDATE meta SET last_update = 'not-a-date'"
                        )
                elif failure in ("production_files", "gerber"):
                    output = tmp_path / "jlcpcb"
                    output.mkdir()
                    (output / failure).write_text("A file blocks the output directory")
            board = GenerationBoard(tmp_path / "board.kicad_pcb")
            window = make_window(
                settings={
                    "library": {"data_path": str(directory)},
                    "gerber": {"fill_zones": False},
                },
                board=board,
                fabrication_initialized=False,
            )
            # The shared fixture seeds persisted assignments; startup must reopen them.
            window.store = None
            del window.populate_footprint_list
            window.partlist_data_model = models.datamodel.PartListDataModel(1.0)
            window.SetTitle = MagicMock()
            window.project_storage_status = Status()
            window.upper_toolbar = ReadinessToolbar(window, mainwindow.ID_GENERATE)
            window.generate_button = SimpleNamespace(
                Enable=lambda enabled: window.upper_toolbar.EnableTool(
                    mainwindow.ID_GENERATE, enabled
                )
            )
            window.gauge = MagicMock()
            window.flush_generation_ui = lambda: None
            # The DRC boundary deliberately cancels before exports or board saves.
            window.run_drc_before_gerber_export = MagicMock(return_value=False)
            window._part_selector = None
            window._catalog_ready = False
            window._catalog_details = {}
            window._part_preferences_applied_on_open = True
            window.library = None
            window.init_data()
            return SimpleNamespace(
                window=window,
                module=mainwindow,
                library_module=library_module,
                fabrication=fabrication,
                path=catalog_path,
                directory=directory,
                project=tmp_path,
                constructors=constructors,
                placements=placements,
                consistency_checks=consistency_checks,
                zone_checks=zone_checks,
                workers=workers,
                message_box=message_box,
            )

        yield create


def retry_settings(context: SimpleNamespace) -> None:
    """Retry the selected source through its production Settings event handler."""
    context.window.update_settings(
        SimpleNamespace(
            section="library", setting="data_path", value=str(context.directory)
        )
    )


def assert_ready(context: SimpleNamespace) -> None:
    """Require complete storage and fabrication before any Generate enable event."""
    window = context.window
    assert not window._project_storage_unavailable
    assert not window.project_storage_status.shown
    assert window.project_storage_status.label == ""
    assert window.project_storage_status.tooltip == ""
    assert window.upper_toolbar.enabled[context.module.ID_GENERATE]
    assert isinstance(window.fabrication, context.fabrication.Fabrication)
    assert window.fabrication.parent is window
    assert window.fabrication.board is window.pcbnew.GetBoard()
    assert window.upper_toolbar.ready_snapshots
    for snapshot in window.upper_toolbar.ready_snapshots:
        assert isinstance(snapshot.generator, context.fabrication.Fabrication)
        assert snapshot.store is not None
        assert not snapshot.unavailable
        assert snapshot.directories_ready
    assert window.store.get_part("R1")["lcsc"] == "C100"
    assert window.pcbnew.GetBoard().FindFootprintByReference("R1").field.text == "C100"


def assert_generate_reaches_drc(context: SimpleNamespace) -> None:
    """Run real generation preflight and verify placements before the deliberate stop."""
    window = context.window
    generation_count = window.store.get_generation_count()
    window.generate_fabrication_data()
    context.message_box.assert_not_called()
    assert context.placements == [
        (("R1", "10k", "R_0603", "10.000000", "-20.000000", 0.0, "top"),)
    ]
    assert context.consistency_checks == [""]
    assert context.zone_checks == [[]]
    window.run_drc_before_gerber_export.assert_called_once_with()
    assert window.store.get_generation_count() == generation_count
    for pattern in ("*.csv", "*.zip", "*.gbr", "*.drl", "*.kicad_pcb"):
        assert not list(context.project.rglob(pattern))
    assert_ready(context)


def assert_output_error(context: SimpleNamespace, blocked: Path) -> None:
    """Keep the error visible and generation disabled after a directory retry fails."""
    window = context.window
    assert window._project_storage_unavailable
    assert window.store is None
    assert not hasattr(window, "fabrication")
    assert window.project_storage_status.shown
    assert "unavailable" in window.project_storage_status.label
    assert str(blocked) in window.project_storage_status.tooltip
    assert not window.upper_toolbar.enabled[context.module.ID_GENERATE]
    assert not window.upper_toolbar.ready_snapshots
    assert window.upper_toolbar.enabled.get(context.module.ID_SETTINGS, True)
    assert window.upper_toolbar.enabled[context.module.ID_DOWNLOAD]
    window.generate_fabrication_data()
    assert not context.placements
    context.message_box.assert_not_called()


@pytest.mark.parametrize("failure", ["corrupt", "metadata"])
@pytest.mark.parametrize("recovery", ["download", "settings"])
def test_catalog_recovery_initializes_fabrication_before_enabling_generate(
    generation_window: Callable[[str], SimpleNamespace],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    recovery: str,
) -> None:
    """Update and Settings must restore working generation in the existing window."""
    context = generation_window(failure)
    window = context.window
    assert window._project_storage_unavailable
    assert not window.upper_toolbar.enabled[context.module.ID_GENERATE]
    assert window.library is not None
    assert not context.constructors

    def replace_catalog(*_args: Any) -> None:
        """Install real SQLite catalog bytes at the download extraction boundary."""
        context.path.unlink()
        seed_catalog(context.path, lcsc="C100")

    if recovery == "download":
        monkeypatch.setattr(context.library_module, "unzip_parts", replace_catalog)
        window.update_library()
        assert len(context.workers) == 1
        context.workers[0]()
        events = [call.args[1] for call in context.module.wx.PostEvent.call_args_list]
        for event in events:
            if isinstance(event, context.library_module.DownloadCompletedEvent):
                window.download_completed(event)
            elif isinstance(event, context.library_module.DownloadFinishedEvent):
                window.download_finished(event)
    else:
        replace_catalog()
        retry_settings(context)
        assert not context.workers

    assert window.is_catalog_available()
    assert_ready(context)
    assert len(context.constructors) == 1
    assert_generate_reaches_drc(context)


@pytest.mark.parametrize("first_directory", ["production_files", "gerber"])
def test_output_directory_retries_remain_disabled_until_both_folders_are_ready(
    generation_window: Callable[[str], SimpleNamespace], first_directory: str
) -> None:
    """Repairing one folder must not hide failure while creating the other folder."""
    context = generation_window(first_directory)
    output = context.project / "jlcpcb"
    first = output / first_directory
    assert_output_error(context, first)
    retry_settings(context)
    assert_output_error(context, first)
    first.unlink()
    second = output / (
        "gerber" if first_directory == "production_files" else "production_files"
    )
    if second.exists():
        second.rmdir()
    second.write_text("The second output directory is also blocked")
    retry_settings(context)
    assert_output_error(context, second)
    second.unlink()
    retry_settings(context)
    assert_ready(context)
    assert len(context.constructors) == 4
    assert_generate_reaches_drc(context)


def test_healthy_initialization_reuses_the_existing_generator_on_store_retry(
    generation_window: Callable[[str], SimpleNamespace],
) -> None:
    """Ordinary startup and subsequent storage initialization retain one generator."""
    context = generation_window("healthy")
    assert_ready(context)
    original = context.window.fabrication
    context.window.init_store()
    retry_settings(context)
    assert context.window.fabrication is original
    assert context.constructors == [original]
    assert_generate_reaches_drc(context)


def test_settings_recovery_from_missing_library_constructs_real_fabrication(
    generation_window: Callable[[str], SimpleNamespace],
) -> None:
    """The existing full-startup retry remains usable after fixing the data path."""
    context = generation_window("library-path")
    assert context.window.library is None
    assert not context.constructors
    context.directory.unlink()
    seed_catalog(context.path, lcsc="C100")
    retry_settings(context)
    assert not context.workers
    assert_ready(context)
    assert len(context.constructors) == 1
    assert_generate_reaches_drc(context)


@pytest.mark.parametrize("failure", ["corrupt", "metadata"])
def test_settings_recovery_to_unfetched_catalog_preserves_generation(
    generation_window: Callable[[str], SimpleNamespace], failure: str
) -> None:
    """Offline project actions recover even when the replacement catalog is absent."""
    context = generation_window(failure)
    window = context.window
    assert window._project_storage_unavailable
    assert not context.constructors
    destination = context.project / "unfetched"

    window.update_settings(
        SimpleNamespace(section="library", setting="data_path", value=str(destination))
    )

    assert not window.is_catalog_available()
    assert window.library.state == context.module.LibraryState.UPDATE_NEEDED
    assert not context.workers
    assert getattr(window, "fabrication", None) is not None
    assert_ready(context)
    assert len(context.constructors) == 1
    assert_generate_reaches_drc(context)


def test_population_read_failure_survives_recovery_until_storage_retry_succeeds(
    generation_window: Callable[[str], SimpleNamespace],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recovered catalog cannot clear a later project read error or enable generation."""
    context = generation_window("healthy")
    window = context.window
    original_generator = window.fabrication
    real_store = context.module.Store
    error = "project assignments cannot be read"

    def unreadable_store(*args: Any, **kwargs: Any) -> Any:
        """Create real project storage before failing its first population read."""
        store = real_store(*args, **kwargs)
        store.read_all = MagicMock(side_effect=sqlite3.OperationalError(error))
        return store

    with monkeypatch.context() as failure:
        failure.setattr(
            window.store,
            "read_all",
            MagicMock(side_effect=sqlite3.OperationalError(error)),
        )
        failure.setattr(context.module, "Store", unreadable_store)
        for recover in (
            window.populate_footprint_list,
            window.download_completed,
            lambda: retry_settings(context),
        ):
            recover()
            assert window.store is None
            assert window._project_storage_unavailable
            assert window.project_storage_status.shown
            assert error in window.project_storage_status.tooltip
            assert not window.upper_toolbar.enabled[context.module.ID_GENERATE]
            assert window.upper_toolbar.enabled.get(context.module.ID_SETTINGS, True)
            window.footprint_list.Enable.assert_called_with(False)
            window.right_toolbar.Enable.assert_called_with(False)
            window.generate_fabrication_data()
            assert not context.placements
            context.message_box.assert_not_called()

    retry_settings(context)
    assert_ready(context)
    assert window.fabrication is original_generator
    assert context.constructors == [original_generator]
    assert_generate_reaches_drc(context)
