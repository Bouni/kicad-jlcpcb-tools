"""Production windows with real wx controls and isolated board/catalog boundaries."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import partial
import importlib
import json
import logging
import os
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock, Mock, patch

import pytest

from .correction_test_support import make_library
from .native_kicad_support import native_bindings
from .native_wx_support import pump, run_native, wait_until
from .variant_native_support import Board, Footprint, Variant
from .wx_harness import ROOT, module, package_stubs, temporary_modules

__all__ = ["native_bindings", "window_ui"]


class _SelectedItem:
    """Retain selection flags like native board items."""

    def __init__(self) -> None:
        self.selected = False

    def SetSelected(self) -> None:
        self.selected = True

    def ClearSelected(self) -> None:
        self.selected = False

    def IsSelected(self) -> bool:
        return self.selected


class _SelectableFootprint(Footprint, _SelectedItem):
    """Combine native variant state with observable board selection."""

    def __init__(self, board: Any, component: str, ref: str = "R1") -> None:
        Footprint.__init__(self, board, component, ref)
        _SelectedItem.__init__(self)

    def GetFPID(self) -> Any:
        return SimpleNamespace(
            GetLibItemName=lambda: self.GetFPIDAsString().rsplit(":", 1)[-1]
        )

    def GetOrientation(self) -> Any:
        return SimpleNamespace(AsDegrees=self.GetOrientationDegrees)


class _Pcbnew:
    """Return the live board and its current selection, including other items."""

    def __init__(self, board: Any) -> None:
        self.board = board
        self.other_items: list[_SelectedItem] = []
        self.Refresh = MagicMock()

    def GetBoard(self) -> Any:
        return self.board

    def GetCurrentSelection(self) -> list[Any]:
        return [
            item
            for item in (*self.board.GetFootprints(), *self.other_items)
            if item.IsSelected()
        ]


def button(wx: Any, control: Any) -> None:
    """Dispatch the command through its real control binding."""
    event = wx.CommandEvent(wx.wxEVT_BUTTON, control.GetId())
    event.SetEventObject(control)
    control.GetEventHandler().ProcessEvent(event)


def choose_output(ui: Any, variant: str) -> None:
    """Change the real choice and dispatch its native command handler."""
    choice = ui.controller.output_choice
    index = [item.name for item in ui.controller.session.snapshot.variants].index(
        variant
    )
    choice.SetSelection(index)
    event = ui.wx.CommandEvent(ui.wx.wxEVT_CHOICE, choice.GetId())
    event.SetInt(index)
    event.SetEventObject(choice)
    choice.GetEventHandler().ProcessEvent(event)


def select_result(ui: Any, selector: Any) -> None:
    """Deliver the actual DataView selection event and check button availability."""
    item = selector.part_list_model.ObjectToItem(selector.part_list_model.data[0])
    selector.part_list.Select(item)
    event = ui.wx.dataview.DataViewEvent(
        ui.wx.dataview.wxEVT_DATAVIEW_SELECTION_CHANGED, selector.part_list, item
    )
    selector.part_list.GetEventHandler().ProcessEvent(event)
    assert selector.select_part_button.IsEnabled()


def focus(
    ui: Any, variant: Optional[str] = "A", field: str = "lcsc", row: int = 0
) -> Any:
    """Select one component block and focus its requested field in the real grid."""
    view = ui.controller.view
    view.select_components((view.model.rows[row].component_id,), variant, field)
    pump(ui.wx)
    return view.selected_target


def activate_popup(ui: Any, variant: str, field: str, label: str) -> None:
    """Select a real popup item with native keys, without injecting menu commands."""
    from .variant_matrix_native_test_support import click_native_cell

    view, wx = ui.controller.view, ui.wx
    ui.dialog.Raise()
    view.SetFocus()
    wait_until(wx, ui.dialog.IsActive, reason="Plugin frame did not become active")
    delivered: list[bool] = []
    timers: list[Any] = []

    def press(key: int) -> None:
        simulator = wx.UIActionSimulator()
        delivered.append(simulator.KeyDown(key) and simulator.KeyUp(key))

    def opened(event: Any) -> None:
        labels = [item.GetItemLabelText() for item in event.GetMenu().GetMenuItems()]
        if label in labels:
            keys = [wx.WXK_DOWN] * (labels.index(label) + 1) + [wx.WXK_RETURN]
            timers.extend(
                wx.CallLater(100 + index * 75, press, key)
                for index, key in enumerate(keys)
            )
        event.Skip()

    view.Bind(wx.EVT_MENU_OPEN, opened)
    escape = wx.CallLater(1500, press, wx.WXK_ESCAPE)
    try:
        click_native_cell(
            view, wx, 0, view.model.column_for(variant, field), right=True
        )
    finally:
        view.Unbind(wx.EVT_MENU_OPEN, handler=opened)
        for timer in [escape, *timers]:
            timer.Stop()
    assert len(delivered) >= 2 and all(delivered), "Native menu input was not delivered"


@contextmanager
def modal_handler(
    ui: Any, dialog_type: type, interact: Callable[[Any], None]
) -> Iterator[list[Any]]:
    """Interact inside a real modal loop, surfacing errors after safe unwinding."""
    visited: list[Any] = []
    failures: list[BaseException] = []
    original = dialog_type.ShowModal

    def show(dialog: Any) -> int:
        visited.append(dialog)

        def inspect() -> None:
            try:
                interact(dialog)
            except BaseException as error:
                failures.append(error)
            finally:
                if dialog and dialog.IsModal():
                    dialog.EndModal(ui.wx.ID_CANCEL)

        timer = ui.wx.CallLater(1, inspect)
        try:
            result = original(dialog)
        finally:
            timer.Stop()
        if failures:
            raise failures[0]
        return result

    with patch.object(dialog_type, "ShowModal", show):
        yield visited
    if failures:
        raise failures[0]


def run_frames(ui: Any, *checks: Callable[[Any], None]) -> None:
    """Exercise actual modeless plugin windows after their action has returned."""

    def run_one(host: Any, wx: Any, check: Callable[[Any], None]) -> None:
        errors: list[BaseException] = []
        frames: list[Any] = []
        completed = False

        def inspect(frame: Any, check: Callable[[Any], None] = check) -> None:
            nonlocal completed
            try:
                assert frame.IsShown() and frame.IsEnabled()
                assert host.IsEnabled()
                pump(wx)
                ui.dialog = frame
                ui.controller = frame._variant_controller
                ui.cache = (
                    ui.controller.cache if ui.controller is not None else frame.store
                )
                check(ui)
            except BaseException as error:
                errors.append(error)
            finally:
                try:
                    controller = frame._variant_controller
                    if controller is not None and controller.session.generating:
                        controller.end_generation()
                    if frame and not frame.IsBeingDeleted():
                        frame.Close()
                    assert frame._part_selector is None
                except BaseException as error:
                    errors.append(error)
                completed = True

        def constructor(parent: Any) -> Any:
            ui.defaults_path.write_text(json.dumps(ui.settings))
            frame = ui.mainwindow.JLCPCBTools(parent, ui.provider)
            frames.append(frame)
            return frame

        previous_loop = wx.EventLoopBase.GetActive()
        factory = ui.plugin.JLCPCBTools
        ui.plugin.JLCPCBTools = constructor
        try:
            ui.plugin.JLCPCBPlugin().Run()
        finally:
            ui.plugin.JLCPCBTools = factory
        assert len(frames) == 1, ui.messages
        frame = frames[0]
        assert frame.IsShown() and host.IsEnabled()
        assert wx.EventLoopBase.GetActive() is previous_loop
        wx.CallAfter(inspect, frame)
        wait_until(wx, lambda: completed)
        assert completed
        assert host.IsEnabled()
        assert wx.EventLoopBase.GetActive() is previous_loop
        wait_until(wx, lambda: not frame)
        if errors:
            raise errors[0]

    def exercise(host: Any, wx: Any) -> None:
        for check in checks:
            run_one(host, wx, check)

    run_native(exercise)


@pytest.fixture
def window_ui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[Any]:
    """Load actual windows once; replace native PCB and supplier data only."""
    if os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1":
        pytest.skip("native wx checks require an enabled desktop session")
    wx = pytest.importorskip("wx")
    package = "_variant_windows"
    if request.node.get_closest_marker("native_kicad"):
        pcbnew = request.getfixturevalue("native_bindings").pcbnew
        board = pcbnew.BOARD()
        for name in ("A", "B", "Default"):
            board.AddVariant(name)
        for index in range(3):
            fp = pcbnew.FOOTPRINT(board)
            fp.SetReference(f"R{index + 1}")
            fp.SetValue("10k")
            fp.SetFPID(pcbnew.LIB_ID("Resistor_SMD", "R_0603_1608Metric"))
            fp.SetField("LCSC", "C1")
            board.Add(fp)
        board.SetFileName(str(tmp_path / "native-dialog.kicad_pcb"))
        assert pcbnew.SaveBoard(board.GetFileName(), board, True)
    else:
        board = Board()
        board.parts = [_SelectableFootprint(board, "component-1")]
        board.Footprints = board.GetFootprints
        path = tmp_path / "board.kicad_pcb"
        path.write_text("(kicad_pcb)\n")
        board.GetFileName = lambda: str(path)
        board.GetDesignSettings = lambda: SimpleNamespace(
            GetAuxOrigin=lambda: SimpleNamespace(x=0, y=0)
        )
        pcbnew = module(
            "pcbnew",
            **dict.fromkeys(
                "EXCELLON_WRITER PCB_PLOT_PARAMS PCB_VIA PLOT_CONTROLLER ZONE_FILLER".split(),
                object,
            ),
            **dict.fromkeys(
                "PLOT_FORMAT_GERBER B_Cu B_Mask B_Paste B_SilkS Edge_Cuts F_Cu F_Mask "
                "F_Paste F_SilkS DRILL_MARKS_NO_DRILL_SHAPE".split(),
                0,
            ),
            FromMM=lambda value: int(value * 1_000_000),
            ToMM=lambda value: value / 1_000_000,
            wxPoint=lambda x, y: SimpleNamespace(x=x, y=y),
            IsCopperLayer=lambda layer: layer in (0, 31),
            Refresh=lambda: None,
            GetBuildVersion=lambda: "10.0-test",
            FOOTPRINT_VARIANT=Variant,
            ActionPlugin=object,
        )
    pending_threads: list[Callable[[], None]] = []
    ui = SimpleNamespace(
        wx=wx,
        board=board,
        path=tmp_path,
        messages=[],
        created_libraries=[],
        failure="healthy",
        pcbnew=pcbnew,
        supplier=SimpleNamespace(fetch_iter=Mock(return_value=iter(()))),
        pending_threads=pending_threads,
    )

    def defer_thread(
        *, target: Callable[..., None], daemon: bool, args: tuple[Any, ...] = ()
    ) -> Any:
        return SimpleNamespace(
            start=lambda: pending_threads.append(partial(target, *args))
        )

    def run_worker() -> None:
        """Run the next production worker off-thread; leave wx delivery queued."""
        target = pending_threads.pop(0)
        failures: list[BaseException] = []

        def run() -> None:
            try:
                target()
            except BaseException as error:
                failures.append(error)

        worker = Thread(target=run, daemon=True)
        worker.start()
        worker.join(timeout=1)
        assert not worker.is_alive(), "Deterministic supplier worker did not finish"
        if failures:
            raise failures[0]

    ui.run_worker = run_worker
    ui.provider = SimpleNamespace(get_pcbnew=lambda: _Pcbnew(board))
    monkeypatch.syspath_prepend(str(ROOT / "lib"))
    levels = {name: logging.getLogger(name).level for name in ("requests", "urllib3")}
    with temporary_modules(
        {**package_stubs(package), "pcbnew": pcbnew}, namespaces=(package,)
    ):
        main = importlib.import_module(package + ".mainwindow")
        controller = importlib.import_module(package + ".variant.controller")
        library = importlib.import_module(package + ".library")
        ui.mainwindow = ui.main = main
        ui.module = controller
        ui.view = importlib.import_module(package + ".variant.matrix_view")
        ui.plugin = importlib.import_module(package + ".plugin")
        ui.selector_module = importlib.import_module(package + ".partselector")
        columns = importlib.import_module(package + ".partselector_columns")

        class Catalog(library.Library):
            """Deterministic supplier state with real temporary correction storage."""

            def __init__(self, parent: Any) -> None:
                self.__dict__.update(vars(make_library(library, tmp_path)))
                self.dialog = self.parent = parent
                self.localcorrectionsdb_file = str(
                    Path(parent.project_path) / "jlcpcb" / "project.db"
                )
                self.stock = 10
                self.config_refreshes = 0
                self.selected_library = "old"
                self.partsdb_file = "old/parts.db"
                self.state = (
                    main.LibraryState.UPDATE_NEEDED
                    if ui.failure == "missing"
                    else main.LibraryState.INITIALIZED
                )
                self.download_attempt = 1
                self.download_running = False
                self.usable = ui.failure != "unreadable"
                self.category_map = {}
                self.reads: list[str] = []
                self.parts: dict[str, dict[str, Any]] = {}
                self.update = MagicMock()
                self.search = MagicMock(side_effect=self._search)
                ui.catalog = self
                ui.created_libraries.append(self)

            download_attempt = 1

            def get_parts_db_info(self) -> Any:
                if ui.failure == "metadata_io_error":
                    raise OSError("Catalog metadata is unavailable")
                return (
                    SimpleNamespace(last_update=42)
                    if ui.failure == "invalid_metadata"
                    else None
                )

            def get_part_details(self, lcsc: str) -> dict[str, Any]:
                self.reads.append(lcsc)
                return dict(
                    self.parts.get(
                        lcsc,
                        {"stock": self.stock, "type": "Basic", "price": "1-:0.10"},
                    )
                )

            def is_download_running(self) -> bool:
                return self.download_running

            def has_usable_parts_catalog(self, *, check_integrity: bool = True) -> bool:
                return self.usable

            def refresh_library_config(self) -> bool:
                if self.download_running:
                    return False
                self.config_refreshes += 1
                settings = self.dialog.settings["library"]
                self.selected_library = settings["selected_library"]
                self.partsdb_file = settings["data_path"] + "/parts.db"
                self.usable = "missing" not in settings.values()
                self.state = (
                    main.LibraryState.INITIALIZED
                    if self.usable
                    else main.LibraryState.UPDATE_NEEDED
                )
                self.stock = 90
                return True

            @property
            def categories(self) -> list[str]:
                return []

            def get_subcategories(self, category: str) -> list[str]:
                return []

            def _search(self, parameters: Any) -> list[tuple[str, ...]]:
                row = {"LCSC Part": "C321", "Library Type": "Basic", "Stock": "10000"}
                return [tuple(row.get(field, "") for field in columns.DB_FIELDS)]

        settings = json.loads((ROOT / "default_settings.json").read_text())
        settings["library"].update(
            selected_library="old", data_path=str(tmp_path / "catalog")
        )
        settings["part_preferences"]["remember_lcsc_assignments"] = False
        ui.settings = settings
        ui.defaults_path = tmp_path / "default_settings.json"
        ui.settings_path = tmp_path / "settings.json"
        ui.defaults_path.write_text(json.dumps(settings))
        monkeypatch.setattr(main, "PLUGIN_PATH", tmp_path)
        monkeypatch.setattr(
            importlib.import_module(package + ".corrections"), "PLUGIN_PATH", tmp_path
        )
        monkeypatch.setattr(main, "Library", Catalog)
        monkeypatch.setattr(
            main.JLCPCBTools,
            "init_logger",
            lambda frame: setattr(frame, "logger", logging.getLogger(__name__)),
        )
        for worker_module in (main, controller):
            monkeypatch.setattr(worker_module, "Thread", defer_thread)
            monkeypatch.setattr(
                worker_module,
                "LCSCAssemblyMetadataProvider",
                lambda **_kwargs: ui.supplier,
            )
        requests = importlib.import_module("requests")
        monkeypatch.setattr(
            requests.sessions.Session,
            "request",
            MagicMock(
                side_effect=AssertionError("Window fixtures must not contact suppliers")
            ),
        )
        monkeypatch.setattr(
            wx, "MessageBox", lambda text, *args, **kwargs: ui.messages.append(text)
        )

        ui.run = partial(run_frames, ui)
        try:
            yield ui
        finally:
            for name, level in levels.items():
                logging.getLogger(name).setLevel(level)
