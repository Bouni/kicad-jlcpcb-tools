"""An old modeless window must never dereference a replaced native board."""

from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from . import part_preferences_test_support as support
from .wx_harness import load

mainwindow = support.mainwindow
make_window = support.make_window


class NativeBoard(support.Board):
    """Model native destruction by rejecting every later attribute access."""

    def __init__(self, path: Path, identity: str) -> None:
        self._invalidated = False
        super().__init__([support.Footprint()])
        self.path = path
        self.m_Uuid = SimpleNamespace(AsString=lambda: identity)

    def __getattribute__(self, name: str) -> Any:
        """Fail on stale access instead of letting a fake hide native use-after-free."""
        if object.__getattribute__(self, "_invalidated"):
            raise AssertionError(f"Dereferenced destroyed native board: {name}")
        return super().__getattribute__(name)

    def GetFileName(self) -> str:
        """Keep both board instances at the same path, as on reload."""
        return str(self.path)


@pytest.fixture
def context_window(make_window: Callable[..., Any], tmp_path: Path) -> SimpleNamespace:
    """Capture the original context, then expose an explicit editor reload."""
    original = NativeBoard(tmp_path / "board.kicad_pcb", "original-board")
    current = NativeBoard(tmp_path / "board.kicad_pcb", "reloaded-board")
    window = make_window(board=original)
    window._board_uuid = original.m_Uuid.AsString()
    window._board_filename = original.GetFileName()
    state = SimpleNamespace(board=original)
    window.pcbnew.GetBoard = lambda: state.board
    window.populate_footprint_list = type(window).populate_footprint_list.__get__(
        window
    )

    def reload_board() -> None:
        """Destroy the old native object before delivering queued UI events."""
        object.__setattr__(original, "_invalidated", True)
        state.board = current

    return SimpleNamespace(
        window=window, original=original, current=current, reload=reload_board
    )


def assert_window_disabled(window: Any, module: ModuleType) -> None:
    """Require visible invalidation, cleared rows, and disabled generation."""
    assert window._project_storage_unavailable
    assert window.store is None
    assert window.test_rows == {}
    assert not window.upper_toolbar.enabled[module.ID_GENERATE]
    window.footprint_list.Enable.assert_called_with(False)
    label = window.project_storage_status.SetLabel.call_args.args[0]
    assert "reopen" in label.lower()
    assert "board" in label.lower() or "pcb" in label.lower()


@pytest.mark.parametrize("closed", [False, True])
def test_current_board_guard_rejects_reload_without_accessing_old_board(
    context_window: SimpleNamespace,
    closed: bool,
) -> None:
    """Reloading the same path creates a different native board context."""
    window = context_window.window
    assert window._get_current_board() is context_window.original
    context_window.reload()
    if closed:
        window.pcbnew.GetBoard = lambda: None
    with pytest.raises(RuntimeError, match="PCB|board"):
        window._get_current_board()


def test_store_reads_guarded_current_board(context_window: SimpleNamespace) -> None:
    """A reused Store must validate before enumerating native footprints."""
    window = context_window.window
    assert window.store.read_all()[0]["lcsc"] == "C100"
    context_window.reload()
    with pytest.raises(RuntimeError, match="PCB|board"):
        window.store.read_all()


def test_save_as_cannot_retarget_or_reactivate_the_old_window(
    context_window: SimpleNamespace,
    mainwindow: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returning to the old filename must not undo a window's permanent invalidation."""
    window = context_window.window
    old_path = context_window.original.path
    context_window.original.path = tmp_path / "elsewhere.kicad_pcb"
    window.populate_footprint_list()
    assert_window_disabled(window, mainwindow)

    context_window.original.path = old_path
    exporter = MagicMock()
    monkeypatch.setattr(mainwindow, "Fabrication", exporter)
    window._initialize_catalog_parts = MagicMock()
    window.init_store()

    assert_window_disabled(window, mainwindow)
    exporter.assert_not_called()
    window._initialize_catalog_parts.assert_not_called()


def test_details_request_does_not_open_after_recompute_invalidates_board(
    context_window: SimpleNamespace,
    mainwindow: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nested recompute failure must stop the enclosing details-window action."""
    window = context_window.window
    window.bom_estimator_decision = None
    window.bom_estimator_board_count = 5
    window._why_standard_dialog = None
    window.recompute_bom_estimate = type(window).recompute_bom_estimate.__get__(window)
    window.bom_estimator_controller = SimpleNamespace(
        recompute=lambda _count: window.store.read_all()
    )
    dialog = MagicMock()
    monkeypatch.setattr(mainwindow, "WhyStandardDialog", dialog)
    context_window.reload()

    window.show_assembly_mode_details()

    assert_window_disabled(window, mainwindow)
    dialog.assert_not_called()


def test_fabrication_reads_guarded_current_board(
    context_window: SimpleNamespace, mainwindow: ModuleType
) -> None:
    """A constructed exporter cannot retain access to the replaced native board."""
    window = context_window.window
    fabrication = load(mainwindow.__package__, "fabrication", {"pcbnew": MagicMock()})
    exporter = fabrication.Fabrication(window, context_window.original)
    assert exporter.board is context_window.original
    context_window.reload()
    with pytest.raises(RuntimeError, match="PCB|board"):
        _ = exporter.board


@pytest.mark.parametrize(
    "callback",
    ["populate", "stock", "estimate", "start", "progress", "clear", "toggle", "assign"],
)
def test_queued_board_read_disables_old_window(
    context_window: SimpleNamespace, mainwindow: ModuleType, callback: str
) -> None:
    """Normal callbacks handle context loss and clear actionable old rows."""
    window = context_window.window
    window.bom_estimator_board_count = 5
    window._why_standard_dialog = None
    window.bom_estimator_controller = SimpleNamespace(
        recompute=lambda _count: window.store.read_all()
    )
    callbacks = {
        "populate": window.populate_footprint_list,
        "stock": window.recompute_stock_concerns,
        "clear": window.remove_lcsc_number,
        "toggle": window.toggle_bom,
        "assign": lambda: window.assign_parts(
            SimpleNamespace(references=["R1"], lcsc="C200", type="Basic", stock=27)
        ),
        "estimate": lambda: mainwindow.JLCPCBTools.recompute_bom_estimate(window),
        "start": lambda: mainwindow.JLCPCBTools.start_assembly_enrichment(window),
        "progress": lambda: window.on_assembly_enrichment_progress(
            SimpleNamespace(
                lcsc="C100", metadata={"assembly_process": "SMT"}, generation=0
            )
        ),
    }
    context_window.reload()
    callbacks[callback]()
    assert_window_disabled(window, mainwindow)


def test_rejected_mutation_refresh_cannot_dereference_destroyed_board(
    context_window: SimpleNamespace, mainwindow: ModuleType
) -> None:
    """Recovering a rejected native edit must not itself access destroyed data."""
    window = context_window.window
    window._board_action = MagicMock(side_effect=RuntimeError("Original PCB replaced"))
    mutation = MagicMock()
    context_window.reload()
    assert not window._apply_board_change(mutation, "assign a part")
    mutation.assert_not_called()
    assert_window_disabled(window, mainwindow)


def test_generation_rejects_replaced_board_before_side_effects(
    context_window: SimpleNamespace,
    mainwindow: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old Generate action must stop before preflight or file operations."""
    window = context_window.window
    window.generate_button = MagicMock()
    window.reset_gauge = MagicMock()
    window.run_generation_step = MagicMock(return_value=())
    window.read_valid_corrections_for_generation = MagicMock(return_value=())
    for name in ("BeginBusyCursor", "EndBusyCursor", "MessageBox"):
        monkeypatch.setattr(mainwindow.wx, name, MagicMock(), raising=False)
    monkeypatch.setattr(mainwindow.wx, "IsBusy", lambda: False, raising=False)
    context_window.reload()
    window.generate_fabrication_data()
    assert_window_disabled(window, mainwindow)
    window.run_generation_step.assert_not_called()
    assert all(
        not call.args[0] for call in window.generate_button.Enable.call_args_list
    )


@pytest.mark.parametrize("delivery", ["progress", "completion"])
def test_worker_tolerates_window_destroyed_immediately_before_delivery(
    context_window: SimpleNamespace,
    mainwindow: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    delivery: str,
) -> None:
    """Handle native destruction between the generation check and event delivery."""
    metadata = [("C100", {"assembly_process": "SMT"})] if delivery == "progress" else []
    monkeypatch.setattr(
        mainwindow,
        "LCSCAssemblyMetadataProvider",
        lambda **_kwargs: SimpleNamespace(fetch_iter=lambda _codes: metadata),
    )
    post = MagicMock(side_effect=RuntimeError("wrapped C/C++ object has been deleted"))
    monkeypatch.setattr(mainwindow.wx, "PostEvent", post)

    context_window.window._assembly_enrichment_worker(["C100"], 0)

    assert post.call_count == 1


def test_close_cancels_already_queued_estimate_refresh(
    context_window: SimpleNamespace,
    mainwindow: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued CallAfter cannot touch estimator controls after the native frame closes."""
    window = context_window.window
    queued: list[Callable[[], None]] = []
    monkeypatch.setattr(mainwindow.wx, "CallAfter", queued.append, raising=False)
    window._bom_recompute_scheduled = False
    window.Destroy = MagicMock()
    window.recompute_stock_concerns = MagicMock(
        side_effect=RuntimeError("native model has been destroyed")
    )
    window.on_bom_data_changed(None)
    window.quit_dialog()
    window.Destroy.assert_called_once()

    queued.pop()()

    window.recompute_stock_concerns.assert_not_called()
    window.recompute_bom_estimate.assert_not_called()
