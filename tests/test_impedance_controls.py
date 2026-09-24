"""Verify toolbar transactions, identity guards, and temporary preview cleanup."""

from __future__ import annotations

from base64 import b64decode
from collections.abc import Callable, Iterator
from copy import deepcopy
from dataclasses import replace
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4/58BAAT/Af9dfQKHAAAAAElFTkSuQmCC"
)


class Widget:
    """Record the small wx control interface used by the toolbar controller."""

    def __init__(self, _parent: Any, label: str) -> None:
        self.label = label
        self.value = False
        self.enabled = True
        self.bindings: list[tuple[Any, Callable[..., Any]]] = []

    def Enable(self, enabled: bool) -> None:
        """Record whether the user can operate this control."""
        self.enabled = enabled

    def SetValue(self, value: bool) -> None:
        """Record checkbox state without synthesizing a user event."""
        self.value = value

    def GetValue(self) -> bool:
        """Read the requested checkbox state."""
        return self.value

    def SetLabel(self, label: str) -> None:
        """Retain a human-readable status message."""
        self.label = label

    def SetToolTip(self, text: str) -> None:
        """Accept the toolbar's help text."""
        self.tooltip = text

    def Bind(self, event: Any, handler: Callable[..., Any]) -> None:
        """Retain event bindings for constructor verification."""
        self.bindings.append((event, handler))


class MemoryDatabase:
    """Implement optimistic feature storage while recording writes and resets."""

    def __init__(self, record: Optional[dict[str, Any]] = None) -> None:
        self.record = deepcopy(record)
        self.loads: list[str] = []
        self.saves: list[tuple[str, dict[str, Any], bool, int]] = []
        self.resets: list[tuple[str, str, dict[str, Any]]] = []
        self.tokens: list[str] = []
        self.current = True
        self.checks = 0

    def resolve_board(self, board_path: str) -> str:
        """Identify impedance settings independently of the parts store."""
        self.board_path = board_path
        return "board-a"

    def find_board(self, board_path: str) -> str:
        """Model a previously registered board without changing saved settings."""
        return self.resolve_board(board_path)

    def ensure_current_board(self, board_id: str, board_path: str) -> None:
        """Reject a window whose impedance board record has been reconnected."""
        self.checks += 1
        assert board_id == "board-a"
        assert board_path == self.board_path
        if not self.current:
            raise ValueError("This board registration changed. Reopen JLCPCB Tools.")

    def load_config(self, board_id: str) -> Optional[dict[str, Any]]:
        """Read detached data for the explicitly selected board."""
        self.loads.append(board_id)
        return deepcopy(self.record)

    def save_config(
        self,
        board_id: str,
        payload: dict[str, Any],
        enabled: bool,
        expected_revision: int,
    ) -> int:
        """Reject a stale editor without modifying the winning configuration."""
        current = self.record["revision"] if self.record is not None else 0
        if current != expected_revision:
            raise RuntimeError("The configuration changed in another window.")
        self.saves.append((board_id, deepcopy(payload), enabled, expected_revision))
        self.record = {
            "version": 1,
            "revision": expected_revision + 1,
            "enabled": enabled,
            "payload": deepcopy(payload),
        }
        return expected_revision + 1

    def config_reset_token(self, board_id: str) -> str:
        """Return an opaque representation of the record shown to the user."""
        self.tokens.append(board_id)
        return json.dumps(self.record, sort_keys=True)

    def reset_config(
        self, board_id: str, expected_token: str, empty_payload: dict[str, Any]
    ) -> int:
        """Replace the old record only if the approved reset token is current."""
        if json.dumps(self.record, sort_keys=True) != expected_token:
            raise RuntimeError("The configuration changed before reset.")
        self.resets.append((board_id, expected_token, deepcopy(empty_payload)))
        revision = self.record["revision"] + 1 if self.record is not None else 1
        self.record = {
            "version": 1,
            "revision": revision,
            "enabled": False,
            "payload": deepcopy(empty_payload),
        }
        return revision


class Store:
    """Expose only the established parts-store database path."""

    def __init__(self, dbfile: Path) -> None:
        self.dbfile = dbfile


@pytest.fixture
def harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SimpleNamespace]:
    """Import private feature modules with scoped native UI and rendering fakes."""
    package_name = "_impedance_controls_tests"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "impedance")]
    monkeypatch.setitem(sys.modules, package_name, package)
    state = SimpleNamespace(
        board_path=str(tmp_path / "board.kicad_pcb"),
        errors=[],
        dialogs=[],
        reset_dialogs=[],
        scratch=[],
        renders=[],
        renderers=[],
        modal_result=2,
        modal_action=None,
        save_failure_action=None,
        dialog_config=None,
        dialog_error=None,
        render_error=None,
        render_action=None,
        close_action=None,
        palette="original-palette",
        palette_error=None,
        reset_result=4,
        reset_action=None,
        preparations=[],
        preparation_action=None,
        preparation_error=None,
        preparation_result=True,
        events=[],
        review_session_class=None,
    )
    Path(state.board_path).write_text("(kicad_pcb)", encoding="utf-8")
    wx = ModuleType("wx")
    wx.CheckBox = Widget
    wx.Button = Widget
    wx.StaticText = Widget
    for name, value in {
        "ID_OK": 1,
        "ID_CANCEL": 2,
        "ID_YES": 3,
        "ID_NO": 4,
        "EVT_CHECKBOX": 10,
        "EVT_BUTTON": 11,
        "OK": 16,
        "ICON_ERROR": 32,
        "YES_NO": 64,
        "NO_DEFAULT": 128,
        "ICON_WARNING": 256,
    }.items():
        setattr(wx, name, value)

    def message_box(message: str, *_args: Any) -> None:
        """Capture errors presented to the user."""
        state.errors.append(message)

    class ResetDialog:
        """Allow a test to simulate user choice and changes while a prompt is open."""

        def __init__(self, _parent: Any, message: str, *_args: Any) -> None:
            self.message = message
            self.destroyed = False
            state.reset_dialogs.append(self)

        def SetYesNoLabels(self, yes: str, no: str) -> None:
            """Record the explicit reset and cancellation actions."""
            self.labels = (yes, no)

        def ShowModal(self) -> int:
            """Return the user's answer after an optional concurrent change."""
            if state.reset_action is not None:
                state.reset_action()
            return state.reset_result

        def Destroy(self) -> None:
            """Record native prompt cleanup."""
            self.destroyed = True

    class ConfigurationDialog:
        """Exercise acceptance and preview callbacks without a native event loop."""

        def __init__(
            self,
            _parent: Any,
            config: Any,
            snapshot: Any,
            preview: Callable[..., Any],
            refresh_snapshot: Callable[..., Any],
            verify_snapshot: Optional[Callable[[Any], None]] = None,
            appearance_context: Optional[Callable[..., Any]] = None,
            load_stackup_catalog: Optional[Callable[..., Any]] = None,
            save_stackup_catalog: Optional[Callable[..., Any]] = None,
            load_stackup_colors: Optional[Callable[..., Any]] = None,
            save_config: Optional[Callable[[Any], None]] = None,
        ) -> None:
            if state.dialog_error is not None:
                raise state.dialog_error
            state.events.append("dialog")
            self.config = (
                state.dialog_config if state.dialog_config is not None else config
            )
            self.snapshot = snapshot
            self.review_session = None
            if state.review_session_class is not None:
                self.review_session = state.review_session_class(config, snapshot)
                self.review_session.refresh()
            self.preview = preview
            self.refresh_snapshot = refresh_snapshot
            self.verify_snapshot = verify_snapshot
            self.save_config = save_config
            self.appearance_context = appearance_context
            self.load_stackup_catalog = load_stackup_catalog
            self.save_stackup_catalog = save_stackup_catalog
            self.load_stackup_colors = load_stackup_colors
            self.modal_open = False
            self.save_errors: list[str] = []
            self.destroyed = False
            state.dialogs.append(self)

        def ShowModal(self) -> int:
            """Model saving in-modal, then explicit close or Cancel after a failure.

            This verifies integration callback ownership, not the real dialog's
            event handling. Failure assertions run before the simulated user
            cancels, while the working configuration and modal are still alive.
            """
            self.modal_open = True
            try:
                if state.modal_action is not None:
                    state.modal_action(self)
                if state.modal_result == wx.ID_OK and self.save_config is not None:
                    try:
                        self.save_config(self.config)
                    except Exception as error:
                        self.save_errors.append(str(error))
                        if state.save_failure_action is not None:
                            state.save_failure_action(self)
                        return wx.ID_CANCEL
                return state.modal_result
            finally:
                self.modal_open = False

        def Destroy(self) -> None:
            """Record native configuration dialog cleanup."""
            self.destroyed = True

        def guarded_preview(self, section: Any, expected: Any = None) -> Any:
            """Model the UI owner's checks around its exact snapshot's capture."""
            snapshot = self.snapshot if expected is None else expected
            if self.verify_snapshot is not None:
                self.verify_snapshot(snapshot)
            capture = self.preview(section)
            if self.verify_snapshot is not None:
                self.verify_snapshot(snapshot)
            return capture

    class Renderer:
        """Write a scratch artifact or fail after opening its destination."""

        def __init__(self, board: Any, pcbnew: Any) -> None:
            self.board = board
            self.pcbnew = pcbnew
            self.closed = False
            self.close_calls = 0
            state.renderers.append(self)

        def __enter__(self) -> Renderer:
            """Keep the native snapshot available only during this preview."""
            assert not self.closed
            return self

        def __exit__(self, *args: Any) -> None:
            """Release native resources on successful and failing previews."""
            self.close()

        def close(self) -> None:
            """Model an observable native-resource lifetime, not just a call log."""
            self.close_calls += 1
            self.closed = True
            if state.close_action is not None:
                state.close_action()

        def render(self, section: Any, destination: Path) -> Path:
            """Write a real PNG so the integration retains and validates its bytes."""
            assert not self.closed
            destination.write_bytes(PNG)
            state.renders.append((section, destination))
            if state.render_action is not None:
                state.render_action()
            if state.render_error is not None:
                raise state.render_error
            return destination

    def temporary_directory(prefix: str) -> TemporaryDirectory:
        """Track scratch directories while retaining real context-manager cleanup."""
        directory = TemporaryDirectory(prefix=prefix, dir=tmp_path)
        state.scratch.append(Path(directory.name))
        return directory

    wx.MessageBox = message_box
    wx.MessageDialog = ResetDialog
    monkeypatch.setitem(sys.modules, "wx", wx)
    for module_name, symbols in (
        ("dialog", {"ImpedanceDialog": ConfigurationDialog}),
        ("render", {"SectionRenderer": Renderer}),
    ):
        module = ModuleType(f"{package_name}.{module_name}")
        module.__dict__.update(symbols)
        monkeypatch.setitem(sys.modules, module.__name__, module)
    state.model = importlib.import_module(f"{package_name}.model")
    state.matching = importlib.import_module(f"{package_name}.matching")
    state.integration = importlib.import_module(f"{package_name}.integration")
    state.wx = wx
    monkeypatch.setattr(state.integration, "TemporaryDirectory", temporary_directory)

    def read_palette(_pcbnew: Any) -> str:
        """Keep appearance revisions controllable without mocking cache behavior."""
        if state.palette_error is not None:
            raise state.palette_error
        return state.palette

    monkeypatch.setattr(state.integration, "read_theme_context", read_palette)
    state.board = SimpleNamespace(GetFileName=lambda: state.board_path)
    state.pcbnew = SimpleNamespace(GetBoard=lambda: state.board)

    def prepare_copper_zones(*, for_review: bool = False) -> bool:
        """Model preparation changing the actual snapshot consumed by review."""
        state.events.append("prepare")
        state.preparations.append((for_review, state.pcbnew.GetBoard()))
        if state.preparation_action is not None:
            state.preparation_action()
        if state.preparation_error is not None:
            raise state.preparation_error
        return state.preparation_result

    state.parent = SimpleNamespace(
        pcbnew=state.pcbnew, prepare_copper_zones=prepare_copper_zones
    )
    state.toolbar_controls = []
    state.toolbar = SimpleNamespace(AddControl=state.toolbar_controls.append)
    try:
        yield state
    finally:
        for name in tuple(sys.modules):
            if name.startswith(f"{package_name}."):
                del sys.modules[name]


def _configuration(harness: SimpleNamespace, enabled: bool = False) -> Any:
    """Provide a complete specification with no native KiCad dependencies."""
    model = harness.model
    return model.Config(
        enabled=enabled,
        specifications=(
            model.Specification(
                "clock",
                "Clock",
                "50",
                "single_ended",
                "RF",
                (model.LayerSettings("F.Cu", ("B.Cu",)),),
            ),
        ),
    )


def _snapshot(harness: SimpleNamespace) -> Any:
    """Provide one short candidate on a physically valid two-layer stack."""
    model = harness.model
    return model.BoardSnapshot(
        ("F.Cu", "B.Cu"),
        (model.Trace("track-a", "F.Cu", "CLK", 150000, ((0, 0), (2_000_000, 0))),),
        net_classes=("Default", "RF"),
        net_class_memberships=(("CLK", ("RF",)),),
        net_class_context_digest="native-classes",
    )


def _record(config: Any, revision: int = 7) -> dict[str, Any]:
    """Encode a database envelope around a valid feature configuration."""
    return {
        "version": 1,
        "revision": revision,
        "enabled": config.enabled,
        "payload": config.to_dict(),
    }


def _controls(
    harness: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    record: Optional[dict[str, Any]] = None,
) -> Any:
    """Attach an in-memory database and a counted immutable-board provider."""
    controls = harness.integration.ImpedanceControls(harness.parent, harness.toolbar)
    harness.database = MemoryDatabase(record)
    harness.store = Store(Path(harness.board_path).parent / "jlcpcb" / "project.db")
    database_factory = MagicMock(return_value=harness.database)
    database_factory.validate_board_path = (
        harness.integration.ImpedanceDatabase.validate_board_path
    )
    monkeypatch.setattr(
        harness.integration,
        "ImpedanceDatabase",
        database_factory,
    )
    controls.attach_store(harness.store)
    harness.snapshots = []
    harness.current_snapshot = _snapshot(harness)

    def snapshot() -> Any:
        """Record geometry reads while preserving the real identity safety guard."""
        controls.check_board()
        harness.events.append("snapshot")
        harness.snapshots.append(True)
        return harness.current_snapshot

    monkeypatch.setattr(controls, "_snapshot", snapshot)
    return controls


def test_toolbar_disabled_until_store_attaches(harness: SimpleNamespace) -> None:
    """Construction cannot expose writes before a database board is identified."""
    controls = harness.integration.ImpedanceControls(harness.parent, harness.toolbar)
    assert harness.toolbar_controls == [
        controls.checkbox,
        controls.configure_button,
        controls.status,
    ]
    assert not controls.checkbox.enabled
    assert not controls.configure_button.enabled
    assert controls.checkbox.bindings == [(harness.wx.EVT_CHECKBOX, controls.on_toggle)]
    assert controls.configure_button.bindings == [
        (harness.wx.EVT_BUTTON, controls.on_configure)
    ]


def test_parts_store_needs_only_its_existing_database_path(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Impedance neither depends on nor migrates parts-store board ownership."""
    controls = _controls(harness, monkeypatch)
    assert vars(harness.store) == {
        "dbfile": Path(harness.board_path).parent / "jlcpcb" / "project.db"
    }
    assert controls.repository.database is harness.database
    assert controls.repository.board_id == "board-a"
    assert controls.config == harness.model.Config()
    assert controls.checkbox.enabled
    assert controls.configure_button.enabled


@pytest.mark.parametrize("board_state", ["empty", "missing", "wrong_suffix"])
@pytest.mark.parametrize("existing_parts", [False, True])
def test_attach_rejects_unsaved_board_before_any_feature_database_writes(
    harness: SimpleNamespace,
    tmp_path: Path,
    board_state: str,
    existing_parts: bool,
) -> None:
    """Actual attachment validates the PCB before creating feature directories/tables."""
    invalid_path = tmp_path / (
        "board.txt" if board_state == "wrong_suffix" else "missing.kicad_pcb"
    )
    if board_state == "wrong_suffix":
        invalid_path.write_text("not a PCB", encoding="utf-8")
    harness.board_path = "" if board_state == "empty" else str(invalid_path)
    dbfile = tmp_path / "jlcpcb" / "project.db"
    before = None
    if existing_parts:
        dbfile.parent.mkdir()
        with sqlite3.connect(dbfile) as connection:
            connection.execute("CREATE TABLE part_info (reference TEXT PRIMARY KEY)")
            connection.execute("INSERT INTO part_info VALUES ('R1')")
            connection.execute(
                "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)"
            )
            connection.execute("INSERT INTO metadata VALUES ('generation_count', '7')")
        connection.close()
        before = dbfile.read_bytes()

    controls = harness.integration.ImpedanceControls(harness.parent, harness.toolbar)
    controls.attach_store(Store(dbfile))

    assert controls.repository is None
    assert not controls.checkbox.enabled
    assert not controls.configure_button.enabled
    assert controls.status.label == "Impedance settings unavailable"
    assert "PCB" in controls.error
    if existing_parts:
        assert dbfile.read_bytes() == before
    else:
        assert not dbfile.parent.exists()


@pytest.mark.parametrize("failure_stage", ["construct", "resolve", "load"])
def test_feature_database_failure_does_not_escape_parts_initialization(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, failure_stage: str
) -> None:
    """Unavailable feature storage is local to impedance and blocks unsafe export."""
    controls = harness.integration.ImpedanceControls(harness.parent, harness.toolbar)
    database = MemoryDatabase()

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        """Represent an inaccessible catalog/config database, not a parts error."""
        raise sqlite3.OperationalError("Impedance database is locked")

    if failure_stage != "construct":
        monkeypatch.setattr(
            database,
            "resolve_board" if failure_stage == "resolve" else "load_config",
            fail,
        )
    database_factory = MagicMock(
        side_effect=fail if failure_stage == "construct" else None,
        return_value=database,
    )
    database_factory.validate_board_path = (
        harness.integration.ImpedanceDatabase.validate_board_path
    )
    monkeypatch.setattr(harness.integration, "ImpedanceDatabase", database_factory)
    store = Store(Path(harness.board_path).parent / "jlcpcb" / "project.db")
    controls.attach_store(store)
    assert not controls.checkbox.enabled
    assert not controls.configure_button.enabled
    assert controls.status.label == "Impedance settings unavailable"
    assert "locked" in controls.error
    assert "locked" in controls.status.tooltip
    with pytest.raises((harness.model.ValidationError, sqlite3.Error), match="locked"):
        controls.preflight(2)
    assert database.saves == []


def test_real_feature_database_keeps_two_boards_independent_of_parts_store(
    harness: SimpleNamespace, tmp_path: Path
) -> None:
    """Reopening each toolbar retrieves its own settings from one project.db."""
    store = Store(tmp_path / "jlcpcb" / "project.db")
    path_a = Path(harness.board_path)
    path_a.write_text("(kicad_pcb)", encoding="utf-8")
    path_b = tmp_path / "second.kicad_pcb"
    path_b.write_text("(kicad_pcb)", encoding="utf-8")
    controls_a = harness.integration.ImpedanceControls(harness.parent, harness.toolbar)
    controls_a.attach_store(store)
    config_a = _configuration(harness)
    controls_a._save(config_a)

    harness.board_path = str(path_b)
    controls_b = harness.integration.ImpedanceControls(harness.parent, harness.toolbar)
    controls_b.attach_store(store)
    assert controls_b.config == harness.model.Config()
    config_b = replace(
        config_a,
        specifications=(replace(config_a.specifications[0], label="Second board"),),
    )
    controls_b._save(config_b)
    assert controls_b.repository.board_id != controls_a.repository.board_id

    harness.board_path = str(path_a)
    reopened_a = harness.integration.ImpedanceControls(harness.parent, harness.toolbar)
    reopened_a.attach_store(store)
    assert reopened_a.config == config_a
    assert reopened_a.repository.board_id == controls_a.repository.board_id
    harness.board_path = str(path_b)
    assert controls_b.reload() == config_b


@pytest.mark.parametrize("existing_parts", [False, True])
def test_unused_impedance_controls_do_not_create_or_change_storage(
    harness: SimpleNamespace, tmp_path: Path, existing_parts: bool
) -> None:
    """Opening, refreshing and ordinary export leave unused feature storage alone."""
    path = tmp_path / "jlcpcb" / "project.db"
    if existing_parts:
        path.parent.mkdir()
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE part_info (reference TEXT PRIMARY KEY)")
            connection.execute("INSERT INTO part_info VALUES ('R1')")
        connection.close()
    before = path.read_bytes() if path.exists() else None
    controls = harness.integration.ImpedanceControls(harness.parent, harness.toolbar)
    controls.attach_store(Store(path))
    assert controls.checkbox.enabled and controls.configure_button.enabled
    assert controls.config == harness.model.Config()
    assert controls.reload() == harness.model.Config()
    assert controls.preflight(2) is None
    controls.verify_disabled()
    assert (path.read_bytes() if path.exists() else None) == before
    if not existing_parts:
        assert not path.parent.exists()


def test_unregistered_board_detects_concurrent_first_save(
    harness: SimpleNamespace, tmp_path: Path
) -> None:
    """Disabled export must notice another window creating the first settings row."""
    store = Store(tmp_path / "jlcpcb" / "project.db")
    first = harness.integration.ImpedanceControls(harness.parent, harness.toolbar)
    second = harness.integration.ImpedanceControls(harness.parent, harness.toolbar)
    first.attach_store(store)
    second.attach_store(store)
    assert first.preflight(2) is None
    assert not Path(store.dbfile).exists()
    second._save(_configuration(harness, True))
    with pytest.raises(harness.model.ValidationError, match="changed during"):
        first.verify_disabled()


def test_reopening_impedance_settings_never_writes_the_database(
    harness: SimpleNamespace, tmp_path: Path
) -> None:
    """Saved review and enabled intent reload without registering another board."""
    store = Store(tmp_path / "jlcpcb" / "project.db")
    first = harness.integration.ImpedanceControls(harness.parent, harness.toolbar)
    first.attach_store(store)
    config = replace(
        _configuration(harness, True),
        reviewed_digest="a" * 64,
        included_section_ids=("historical-section",),
    )
    first._save(config)
    before = Path(store.dbfile).read_bytes()
    reopened = harness.integration.ImpedanceControls(harness.parent, harness.toolbar)
    reopened.attach_store(store)
    assert reopened.config == config
    assert reopened.checkbox.GetValue() == config.enabled
    second_path = tmp_path / "second.kicad_pcb"
    second_path.write_text("(kicad_pcb)", encoding="utf-8")
    harness.board_path = str(second_path)
    second = harness.integration.ImpedanceControls(harness.parent, harness.toolbar)
    second.attach_store(store)
    assert second.config == harness.model.Config()
    assert Path(store.dbfile).read_bytes() == before


def test_cancel_first_configuration_and_catalog_read_create_no_storage(
    harness: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opening an empty dialog and reading its cache do not persist settings."""
    path = tmp_path / "jlcpcb" / "project.db"
    controls = harness.integration.ImpedanceControls(harness.parent, harness.toolbar)
    controls.attach_store(Store(path))
    monkeypatch.setattr(controls, "_snapshot", lambda: _snapshot(harness))

    def read_catalog(dialog: Any) -> None:
        """Exercise the real database read through the dialog's catalog callback."""
        assert dialog.load_stackup_catalog(2).stackups == ()

    harness.modal_action = read_catalog
    controls.on_configure(None)
    assert len(harness.dialogs) == 1
    assert not harness.errors
    assert not path.parent.exists()


def test_two_unregistered_windows_cannot_overwrite_first_save(
    harness: SimpleNamespace, tmp_path: Path
) -> None:
    """Lazy board registration retains optimistic concurrency on the first save."""
    store = Store(tmp_path / "jlcpcb" / "project.db")
    first = harness.integration.ImpedanceControls(harness.parent, harness.toolbar)
    second = harness.integration.ImpedanceControls(harness.parent, harness.toolbar)
    first.attach_store(store)
    second.attach_store(store)
    config = _configuration(harness)
    first._save(config)
    with pytest.raises(RuntimeError, match="changed"):
        second._save(replace(config, enabled=False))
    assert first.reload() == config


def test_first_catalog_save_reopens_without_saving_board_configuration(
    harness: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful cache callback persists project data independently of intent."""
    path = tmp_path / "jlcpcb" / "project.db"
    store = Store(path)
    controls = harness.integration.ImpedanceControls(harness.parent, harness.toolbar)
    controls.attach_store(store)
    monkeypatch.setattr(controls, "_snapshot", lambda: _snapshot(harness))
    stackups = importlib.import_module(
        f"{harness.integration.__package__}.stackup_model"
    )
    cache = harness.integration.CatalogCache(
        (stackups.Stackup("test-stackup", "Test stackup", 2, "1.6", "1", "1"),),
        "2026-09-22T01:00:00.000000Z",
    )

    def save_catalog(dialog: Any) -> None:
        """Simulate a completed catalog check through its production save callback."""
        dialog.save_stackup_catalog(2, cache)
        assert dialog.load_stackup_catalog(2) == cache

    harness.modal_action = save_catalog
    controls.on_configure(None)
    assert not harness.errors
    assert controls.config == harness.model.Config() and controls.revision == 0
    assert controls.database.load_config(controls.repository.board_id) is None
    before = path.read_bytes()
    # A different PCB can read the shared catalog before it has a registry row.
    board_path = tmp_path / "second.kicad_pcb"
    board_path.write_text("(kicad_pcb)", encoding="utf-8")
    harness.board_path = str(board_path)
    reopened = harness.integration.ImpedanceControls(harness.parent, harness.toolbar)
    reopened.attach_store(store)
    monkeypatch.setattr(reopened, "_snapshot", lambda: _snapshot(harness))

    def read_catalog(dialog: Any) -> None:
        """Verify persistence through the next dialog's real catalog read callback."""
        assert dialog.load_stackup_catalog(2) == cache

    harness.modal_action = read_catalog
    reopened.on_configure(None)
    assert not harness.errors and len(harness.dialogs) == 2
    assert reopened.config == harness.model.Config() and reopened.revision == 0
    assert reopened.repository is None
    assert path.read_bytes() == before


def test_disabled_preflight_reads_no_board_geometry(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordinary generation stays independent of rendering and trace extraction."""
    controls = _controls(harness, monkeypatch)

    def unexpected(*_args: Any) -> None:
        """Fail if disabled configuration enters the optional impedance pipeline."""
        pytest.fail("Disabled impedance must not prepare the board")

    monkeypatch.setattr(harness.integration, "prepare", unexpected)
    assert controls.preflight(2) is None
    controls.verify_disabled()
    assert harness.snapshots == []
    assert harness.database.saves == []
    assert not controls.checkbox.value


@pytest.mark.parametrize("enabled", [False, True])
def test_disabled_export_rejects_concurrent_configuration_change(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    """A same-board concurrent edit cannot silently omit newly requested documentation."""
    controls = _controls(harness, monkeypatch, _record(_configuration(harness)))
    assert controls.preflight(2) is None
    harness.database.save_config(
        "board-a", _configuration(harness, enabled).to_dict(), enabled, 7
    )
    with pytest.raises(harness.model.ValidationError, match="changed"):
        controls.verify_disabled()


def test_disabling_retains_specifications_without_snapshot(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unchecking persists only enablement and retains the original requirements."""
    original = _configuration(harness, True)
    controls = _controls(harness, monkeypatch, _record(original))
    controls.checkbox.SetValue(False)
    controls.on_toggle(None)
    assert harness.snapshots == []
    assert harness.preparations == []
    assert harness.database.saves == [
        ("board-a", replace(original, enabled=False).to_dict(), False, 7)
    ]
    assert not controls.checkbox.value
    assert controls.config.specifications == original.specifications


def test_enabling_current_review_saves_initial_revision(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successfully validated stored review enables without reopening the editor."""
    original = _configuration(harness)
    controls = _controls(harness, monkeypatch, _record(original))
    prepared = []
    monkeypatch.setattr(
        harness.integration, "prepare", lambda *args: prepared.append(args)
    )
    controls.checkbox.SetValue(True)
    controls.on_toggle(None)
    assert prepared == [(replace(original, enabled=True), _snapshot(harness))]
    assert harness.database.saves == [
        ("board-a", replace(original, enabled=True).to_dict(), True, 7)
    ]
    assert controls.checkbox.value
    assert harness.dialogs == []


def test_first_enable_then_cancel_leaves_database_untouched(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Canceling initial setup restores the unchecked control and empty database."""
    controls = _controls(harness, monkeypatch)
    controls.checkbox.SetValue(True)
    controls.on_toggle(None)
    assert harness.database.saves == []
    assert harness.database.record is None
    assert not controls.checkbox.value
    assert len(harness.dialogs) == 1
    assert harness.dialogs[0].config.enabled
    assert harness.dialogs[0].destroyed
    assert all(not path.exists() for path in harness.scratch)


def test_configure_cancel_preserves_existing_configuration(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An edited dialog working copy is discarded when the user presses Cancel."""
    original = _configuration(harness)
    controls = _controls(harness, monkeypatch, _record(original))
    harness.dialog_config = replace(original, enabled=True)
    controls.on_configure(None)
    assert controls.config == original
    assert harness.database.record == _record(original)
    assert harness.database.saves == []
    assert harness.dialogs[0].destroyed


@pytest.mark.parametrize("entry", ["configure", "enable"])
def test_review_preparation_precedes_its_snapshot_and_dialog(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """The first review receives copper produced by the shared export preparation."""
    controls = _controls(harness, monkeypatch)
    filled = replace(harness.current_snapshot, context_digest="filled-copper")

    def fill_live_board() -> None:
        """Change the board revision, not only record an expected method call."""
        harness.current_snapshot = filled

    harness.preparation_action = fill_live_board
    if entry == "configure":
        controls.on_configure(None)
    else:
        controls.checkbox.SetValue(True)
        controls.on_toggle(None)

    assert not harness.errors
    assert harness.preparations == [(True, harness.board)]
    assert harness.events[-3:] == ["prepare", "snapshot", "dialog"]
    assert harness.dialogs[0].snapshot == filled
    assert harness.current_snapshot == filled
    assert not harness.database.saves
    assert not controls.checkbox.value


def test_reopening_review_prepares_again_without_filling_each_preview(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each opening prepares its live board once; cached/retried PNGs stay read-only."""
    controls = _controls(harness, monkeypatch, _record(_configuration(harness)))

    def fill_live_board() -> None:
        """Give each opening new copper so reusing a prior preparation is detectable."""
        harness.current_snapshot = replace(
            harness.current_snapshot,
            context_digest=f"filled-opening-{len(harness.preparations)}",
        )

    def inspect_previews(dialog: Any) -> None:
        """Exercise ordinary navigation, explicit Retry, and snapshot verification."""
        count = len(harness.preparations)
        section = harness.matching.analyze(dialog.config, dialog.snapshot).sections[0]
        first = dialog.guarded_preview(section)
        assert dialog.guarded_preview(section) is first
        assert dialog.preview(section, refresh=True).data == first.data
        assert dialog.refresh_snapshot() == dialog.snapshot
        dialog.verify_snapshot(dialog.snapshot)
        assert len(harness.preparations) == count

    harness.preparation_action = fill_live_board
    harness.modal_action = inspect_previews
    controls.on_configure(None)
    controls.on_configure(None)

    assert not harness.errors
    assert harness.preparations == [(True, harness.board), (True, harness.board)]
    assert [dialog.snapshot.context_digest for dialog in harness.dialogs] == [
        "filled-opening-1",
        "filled-opening-2",
    ]
    assert not harness.database.saves


@pytest.mark.parametrize("outcome", ["cancel", "failure"])
def test_review_preparation_abort_prevents_snapshot_dialog_and_save(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """Declined empty-zone warnings and fill failures cannot open a reviewable draft."""
    original = _configuration(harness)
    controls = _controls(harness, monkeypatch, _record(original))
    harness.modal_result = harness.wx.ID_OK
    if outcome == "cancel":
        harness.preparation_result = False
    else:
        harness.preparation_error = RuntimeError("Copper fill failed")

    controls.on_configure(None, requested_enabled=True)

    assert harness.preparations == [(True, harness.board)]
    assert not harness.snapshots
    assert not harness.dialogs
    assert not harness.scratch
    assert not harness.database.saves
    assert harness.database.record == _record(original)
    assert not controls.checkbox.value
    assert harness.errors == (["Copper fill failed"] if outcome == "failure" else [])


@pytest.mark.parametrize("identity", ["save_as", "registry"])
def test_changed_board_identity_is_rejected_before_review_preparation(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, identity: str
) -> None:
    """An obsolete toolbar must not refill another board before discovering its owner."""
    controls = _controls(harness, monkeypatch, _record(_configuration(harness)))
    if identity == "save_as":
        harness.board_path += ".renamed"
    else:
        harness.database.current = False

    controls.on_configure(None)

    assert not harness.preparations
    assert not harness.snapshots
    assert not harness.dialogs
    assert not harness.database.saves
    assert harness.errors and "changed" in harness.errors[-1]


@pytest.mark.parametrize("changed", [False, True])
def test_review_revalidates_saved_approval_against_prepared_copper(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, changed: bool
) -> None:
    """The production review session preserves or revokes approval after the refill."""
    monkeypatch.setattr(harness.wx, "Dialog", object, raising=False)
    name = f"{harness.integration.__package__}.review_session_tests"
    path = Path(__file__).resolve().parents[1] / "impedance" / "dialog.py"
    module_spec = importlib.util.spec_from_file_location(name, path)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    monkeypatch.setitem(sys.modules, name, module)
    module_spec.loader.exec_module(module)
    harness.review_session_class = module.ReviewSession
    initial = _snapshot(harness)
    session = module.ReviewSession(_configuration(harness, True), initial)
    session.refresh()
    session.approve()
    approved = session.save_result()
    controls = _controls(harness, monkeypatch, _record(approved))

    def fill_live_board() -> None:
        """Only changed filled geometry changes the review fingerprint."""
        harness.current_snapshot = (
            replace(initial, context_digest="different-filled-copper")
            if changed
            else initial
        )

    harness.preparation_action = fill_live_board
    controls.on_configure(None)

    assert not harness.errors
    assert harness.preparations == [(True, harness.board)]
    reopened = harness.dialogs[0].review_session
    assert reopened.snapshot == harness.current_snapshot
    assert reopened.approved is not changed
    if changed:
        assert reopened.config.reviewed_digest == ""
        assert not reopened.config.included_section_ids
    else:
        assert reopened.save_result() == approved
    assert harness.database.record == _record(approved)
    assert not harness.database.saves


def test_configure_accept_saves_and_refreshes_revision(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Save commits once while the modal and its working state are still open."""
    controls = _controls(harness, monkeypatch)
    original_save = harness.database.save_config

    def save_while_open(*args: Any) -> int:
        """Reject the previous close-first integration at its database write."""
        dialog = harness.dialogs[0]
        assert dialog.modal_open
        assert not dialog.destroyed
        return original_save(*args)

    monkeypatch.setattr(harness.database, "save_config", save_while_open)
    harness.dialog_config = _configuration(harness, True)
    harness.modal_result = harness.wx.ID_OK
    controls.on_configure(None)
    assert harness.database.saves == [
        ("board-a", harness.dialog_config.to_dict(), True, 0)
    ]
    assert controls.revision == 1
    assert controls.checkbox.value
    assert harness.dialogs[0].destroyed
    assert not harness.errors


@pytest.mark.parametrize("enabled", [False, True])
def test_stackup_only_draft_saves_and_reopens_without_approval(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    """A chosen construction survives reopening without authorizing fabrication."""
    controls = _controls(harness, monkeypatch)
    stackup = harness.model.Stackup("two-layer", "Two layer", 2, "1.6", "1", "")
    draft = harness.model.Config(enabled=enabled, stackup=stackup)
    harness.dialog_config = draft
    harness.modal_result = harness.wx.ID_OK
    controls.on_configure(None)
    assert not harness.errors
    assert controls.config == draft
    assert controls.checkbox.value is enabled
    assert "Needs review" in controls.status.label
    assert controls.revision == 1
    harness.dialog_config = None
    harness.modal_result = harness.wx.ID_CANCEL
    controls.on_configure(None)
    assert harness.dialogs[-1].config == draft
    assert controls.repository.load() == (draft, 1)
    assert len(harness.database.saves) == 1
    if enabled:
        with pytest.raises(harness.model.ValidationError):
            controls.preflight(2)
    else:
        assert controls.preflight(2) is None


def test_enable_then_save_draft_keeps_requested_enablement(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Initial setup may remain unreviewed without silently disabling the report."""
    controls = _controls(harness, monkeypatch)
    harness.modal_result = harness.wx.ID_OK
    controls.checkbox.SetValue(True)
    controls.on_toggle(None)
    assert not harness.errors
    assert harness.dialogs[0].config.enabled
    assert controls.config.enabled
    assert controls.checkbox.value
    assert "Needs review" in controls.status.label
    assert not controls.config.reviewed_digest
    with pytest.raises(harness.model.ValidationError):
        controls.preflight(2)


def test_successful_save_does_not_depend_on_a_postcommit_read(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once committed, a later read failure must not misreport the save as failed."""
    controls = _controls(harness, monkeypatch)
    original_save = harness.database.save_config

    def fail_read(_board_id: str) -> None:
        """Model database read availability changing immediately after commit."""
        raise OSError("Later database read failed")

    def save_then_disable_reads(*args: Any) -> int:
        """Return the actual new revision while making reload unavailable."""
        revision = original_save(*args)
        monkeypatch.setattr(harness.database, "load_config", fail_read)
        return revision

    monkeypatch.setattr(harness.database, "save_config", save_then_disable_reads)
    harness.dialog_config = _configuration(harness, True)
    harness.modal_result = harness.wx.ID_OK
    controls.on_configure(None)
    assert controls.config == harness.dialog_config
    assert controls.revision == 1
    assert not harness.errors
    assert not harness.dialogs[0].save_errors


def test_save_callback_is_independent_of_toolbar_lifetime(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead native status control cannot turn a committed write into Save failure."""
    controls = _controls(harness, monkeypatch)
    draft = _configuration(harness, True)
    refresh_calls: list[bool] = []

    def fail_status_refresh() -> None:
        """Model a native widget whose C++ owner has already been destroyed."""
        refresh_calls.append(True)
        raise RuntimeError("Wrapped C/C++ object has been deleted")

    monkeypatch.setattr(controls, "_update_saved_status", fail_status_refresh)
    controls._save(draft)
    assert controls.config == draft
    assert controls.revision == 1
    assert harness.database.record == _record(draft, 1)
    assert not refresh_calls


def test_database_write_failure_returns_to_unchanged_working_copy(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed commit changes neither persisted settings nor the open draft."""
    original = _configuration(harness)
    controls = _controls(harness, monkeypatch, _record(original))
    harness.dialog_config = replace(original, enabled=True)
    harness.modal_result = harness.wx.ID_OK

    def fail_write(*_args: Any) -> int:
        """Model failure before committing the optimistic write."""
        raise OSError("Project database is unavailable")

    def inspect_failed_save(dialog: Any) -> None:
        """Inspect failure before the simulated user explicitly cancels."""
        assert dialog.modal_open
        assert not dialog.destroyed
        assert dialog.config == harness.dialog_config
        assert harness.database.record == _record(original)
        assert controls.config == original
        assert controls.revision == 7

    monkeypatch.setattr(harness.database, "save_config", fail_write)
    harness.save_failure_action = inspect_failed_save
    controls.on_configure(None)
    assert not harness.database.saves
    assert not harness.errors
    assert harness.dialogs[0].save_errors == ["Project database is unavailable"]


def test_stored_approval_status_is_not_a_live_board_validation(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Toolbar reload identifies historical approval without scanning the PCB."""
    approved = replace(
        _configuration(harness, True),
        reviewed_digest="a" * 64,
        included_section_ids=("saved-section",),
    )
    controls = _controls(harness, monkeypatch, _record(approved))
    controls.reload()
    assert "saved approval" in controls.status.label
    assert not harness.snapshots
    assert not harness.database.saves


def test_concurrent_modal_save_preserves_winning_revision(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older modal editor cannot overwrite another window's successful save."""
    original = _configuration(harness)
    controls = _controls(harness, monkeypatch, _record(original))
    harness.dialog_config = replace(original, enabled=True)
    harness.modal_result = harness.wx.ID_OK
    winner = replace(
        original,
        specifications=(replace(original.specifications[0], target_ohms="55"),),
    )
    harness.modal_action = lambda _dialog: harness.database.save_config(
        "board-a", winner.to_dict(), False, 7
    )

    def inspect_failed_save(dialog: Any) -> None:
        """Check that a conflicting write retains the still-open draft before Cancel."""
        assert dialog.modal_open
        assert not dialog.destroyed
        assert dialog.config == harness.dialog_config
        assert controls.config == original
        assert controls.revision == 7

    harness.save_failure_action = inspect_failed_save
    controls.on_configure(None)
    assert harness.database.record == _record(winner, 8)
    assert len(harness.database.saves) == 1
    assert "changed in another window" in harness.dialogs[0].save_errors[-1]
    assert not harness.errors
    assert harness.dialogs[0].destroyed
    assert all(not path.exists() for path in harness.scratch)


@pytest.mark.parametrize("change", ["save_as", "registry"])
def test_modal_identity_change_prevents_save(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """A modal editor cannot commit to an identity that changed while it was open."""
    controls = _controls(harness, monkeypatch)
    harness.dialog_config = _configuration(harness, True)
    harness.modal_result = harness.wx.ID_OK

    def change_identity(_dialog: Any) -> None:
        """Simulate Save As or an explicit board-registry reconnection."""
        if change == "save_as":
            harness.board_path += ".renamed"
        else:
            harness.database.current = False

    harness.modal_action = change_identity

    def inspect_failed_save(dialog: Any) -> None:
        """Identity failures return to the live draft instead of dropping it."""
        assert dialog.modal_open
        assert not dialog.destroyed
        assert dialog.config == harness.dialog_config
        assert controls.revision == 0

    harness.save_failure_action = inspect_failed_save
    controls.on_configure(None)
    assert harness.database.saves == []
    assert "changed" in harness.dialogs[0].save_errors[-1]
    assert not harness.errors
    assert harness.dialogs[0].destroyed


@pytest.mark.parametrize(
    "outcome", ["cancel", "accept", "render_error", "modal_error", "constructor_error"]
)
def test_preview_scratch_is_removed_on_all_modal_exit_paths(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """Previews and partial renderer output never outlive the configuration dialog."""
    controls = _controls(harness, monkeypatch, _record(_configuration(harness)))
    section = harness.matching.analyze(
        _configuration(harness), _snapshot(harness)
    ).sections[0]
    harness.modal_result = (
        harness.wx.ID_OK if outcome == "accept" else harness.wx.ID_CANCEL
    )
    if outcome == "constructor_error":
        harness.dialog_error = RuntimeError("Dialog initialization failed")
    if outcome == "render_error":
        harness.render_error = RuntimeError("Renderer failed")

    def preview(dialog: Any) -> None:
        """Create real scratch artifacts and optionally fail within the modal call."""
        capture = dialog.preview(section)
        assert isinstance(capture, harness.integration.CapturedImage)
        assert capture.data == PNG
        assert harness.renders[-1][1].exists()
        if outcome == "modal_error":
            raise RuntimeError("Modal event failed")

    harness.modal_action = preview
    controls.on_configure(None)
    assert len(harness.scratch) == 1
    assert not harness.scratch[0].exists()
    assert all(dialog.destroyed for dialog in harness.dialogs)
    assert all(
        renderer.closed and renderer.close_calls == 1 for renderer in harness.renderers
    )
    if outcome.endswith("error"):
        assert harness.errors
        assert harness.database.saves == []
    else:
        assert not harness.errors


def test_repeated_preview_reuses_capture_and_live_renderer(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One unchanged revision shares native plots and immutable PNG bytes."""
    controls = _controls(harness, monkeypatch, _record(_configuration(harness)))
    section = harness.matching.analyze(
        _configuration(harness), _snapshot(harness)
    ).sections[0]

    def preview_twice(dialog: Any) -> None:
        """Revisiting a row returns its capture without opening another renderer."""
        first = dialog.preview(section)
        assert isinstance(first, harness.integration.CapturedImage)
        assert dialog.preview(section) is first
        assert not harness.renderers[0].closed

    harness.modal_action = preview_twice
    controls.on_configure(None)
    assert not harness.errors
    assert len(harness.renderers) == 1
    assert len(harness.renders) == 1
    assert all(
        renderer.closed and renderer.close_calls == 1 for renderer in harness.renderers
    )


@pytest.mark.parametrize("change", ["board", "palette"])
def test_changed_revision_replaces_renderer_and_cached_capture(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """Identical row identity cannot retain pixels from a different board or theme."""
    controls = _controls(harness, monkeypatch, _record(_configuration(harness)))
    section = harness.matching.analyze(
        _configuration(harness), harness.current_snapshot
    ).sections[0]

    def preview_changed_revision(dialog: Any) -> None:
        """Let the parent adopt a changed revision before selecting the updated row."""
        original = dialog.preview(section)
        if change == "board":
            harness.current_snapshot = replace(
                harness.current_snapshot, context_digest="changed-fill-and-text"
            )
            dialog.snapshot = dialog.refresh_snapshot()
        else:
            harness.palette = "changed-palette"
        updated = dialog.preview(section)
        assert updated is not original
        assert harness.renderers[0].closed
        assert not harness.renderers[1].closed
        assert dialog.preview(section) is updated

    harness.modal_action = preview_changed_revision
    controls.on_configure(None)
    assert not harness.errors
    assert len(harness.renderers) == len(harness.renders) == 2
    assert all(renderer.close_calls == 1 for renderer in harness.renderers)


def test_explicit_refresh_recaptures_without_discarding_other_rows(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retry replaces only the selected PNG while retaining unchanged native plots."""
    controls = _controls(harness, monkeypatch, _record(_configuration(harness)))
    section = harness.matching.analyze(
        _configuration(harness), harness.current_snapshot
    ).sections[0]
    other = replace(section, section_id="another-workbook-row")

    def refresh_selected(dialog: Any) -> None:
        """Bypass the selected image's cache entry with the refresh keyword."""
        original = dialog.preview(section)
        other_capture = dialog.preview(other)
        refreshed = dialog.preview(section, refresh=True)
        assert refreshed is not original
        assert refreshed.data == original.data
        assert dialog.preview(section) is refreshed
        assert dialog.preview(other) is other_capture
        assert len(harness.renderers) == 1
        assert not harness.renderers[0].closed

    harness.modal_action = refresh_selected
    controls.on_configure(None)
    assert not harness.errors
    assert [section for section, _path in harness.renders] == [section, other, section]
    assert harness.renderers[0].close_calls == 1


@pytest.mark.parametrize("phase", ["before_cached", "during_capture"])
def test_unreadable_palette_closes_renderer_and_cannot_return_cached_pixels(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    """Unreadable appearance is never treated as the previous known palette."""
    controls = _controls(harness, monkeypatch, _record(_configuration(harness)))
    section = harness.matching.analyze(
        _configuration(harness), harness.current_snapshot
    ).sections[0]

    def fail_palette() -> None:
        """Model a theme file read becoming unavailable during the review."""
        harness.palette_error = OSError("The palette could not be read")

    def inspect_palette_failure(dialog: Any) -> None:
        """Retry with a new owner and no stale captures once appearance is readable."""
        first = dialog.preview(section)
        if phase == "before_cached":
            fail_palette()
        else:
            harness.render_action = fail_palette
        with pytest.raises(OSError, match="palette could not be read"):
            dialog.preview(section, refresh=phase == "during_capture")
        assert harness.renderers[0].closed
        assert harness.renderers[0].close_calls == 1
        harness.palette_error = None
        harness.render_action = None
        assert dialog.preview(section) is not first

    harness.modal_action = inspect_palette_failure
    controls.on_configure(None)
    assert not harness.errors
    assert len(harness.renderers) == 2
    assert all(renderer.close_calls == 1 for renderer in harness.renderers)


@pytest.mark.parametrize("failure", ["save_as", "registry", "snapshot_read"])
def test_cached_preview_still_checks_identity_and_snapshot_readability(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Previously captured bytes cannot bypass live board ownership or read failures."""
    controls = _controls(harness, monkeypatch, _record(_configuration(harness)))
    section = harness.matching.analyze(
        _configuration(harness), harness.current_snapshot
    ).sections[0]

    def unreadable_snapshot() -> Any:
        """Model failure while collecting the board's current render context."""
        raise RuntimeError("Cannot read the current board")

    def request_cached_image(dialog: Any) -> None:
        """Invalidate a live cache and inspect cleanup before the dialog closes."""
        assert dialog.preview(section).data == PNG
        if failure == "save_as":
            harness.board_path += ".renamed"
        elif failure == "registry":
            harness.database.current = False
        else:
            monkeypatch.setattr(controls, "_snapshot", unreadable_snapshot)
        with pytest.raises((ValueError, RuntimeError), match="changed|Cannot read"):
            dialog.preview(section)
        assert harness.renderers[0].closed
        assert harness.renderers[0].close_calls == 1
        assert len(harness.renders) == 1

    harness.modal_action = request_cached_image
    controls.on_configure(None)
    assert not harness.errors
    assert not harness.database.saves
    assert harness.renderers[0].close_calls == 1


@pytest.mark.parametrize(
    "change",
    [
        "geometry",
        "layers",
        "render_context",
        "class_membership",
        "class_context",
        "pairing",
        "class_api",
        "pair_api",
    ],
)
def test_preview_guard_rejects_each_changed_snapshot_dimension_before_render(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """The preview's box and board context must share the dialog's exact revision."""
    controls = _controls(harness, monkeypatch, _record(_configuration(harness)))
    initial = harness.current_snapshot
    section = harness.matching.analyze(_configuration(harness), initial).sections[0]
    changes = {
        "geometry": {
            "traces": (replace(initial.traces[0], points=((0, 0), (20_000_000, 0))),)
        },
        "layers": {"layers": ("F.Cu", "In1.Cu", "B.Cu")},
        "render_context": {"context_digest": "changed-board-text-and-pours"},
        "class_membership": {"net_class_memberships": (("CLK", ("Default",)),)},
        "class_context": {"net_class_context_digest": "changed-native-constraints"},
        "pairing": {"differential_pairs": (("CLK", "CLK_MATE"),)},
        "class_api": {"net_class_error": "Native class metadata became unavailable"},
        "pair_api": {
            "differential_pair_error": "Native pairing metadata became unavailable"
        },
    }

    def change_and_preview(dialog: Any) -> None:
        """Run the failing guard before allocating a native renderer."""
        harness.current_snapshot = replace(initial, **changes[change])
        with pytest.raises(harness.model.ValidationError, match="changed.*Review"):
            dialog.guarded_preview(section)
        assert not harness.renderers

    harness.modal_action = change_and_preview
    controls.on_configure(None)
    assert not harness.errors
    assert not harness.database.saves
    assert harness.dialogs[0].destroyed
    assert all(not path.exists() for path in harness.scratch)


def test_preview_guard_rechecks_after_render_and_closes_stale_owner(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A board change during capture cannot return PNGs from obsolete native plots."""
    controls = _controls(harness, monkeypatch, _record(_configuration(harness)))
    initial = harness.current_snapshot
    section = harness.matching.analyze(_configuration(harness), initial).sections[0]

    def mutate_context() -> None:
        """Model changed native context during a renderer operation."""
        harness.current_snapshot = replace(
            initial, context_digest="changed-during-capture"
        )

    harness.render_action = mutate_context

    def stale_capture(dialog: Any) -> None:
        """Observe the actual resource state when the post-render guard rejects."""
        with pytest.raises(harness.model.ValidationError, match="changed.*Review"):
            dialog.guarded_preview(section)
        assert len(harness.renderers) == 1
        assert harness.renderers[0].closed
        assert harness.renderers[0].close_calls == 1

    harness.modal_action = stale_capture
    controls.on_configure(None)
    assert not harness.errors
    assert not harness.database.saves
    assert all(not path.exists() for path in harness.scratch)


def test_child_refresh_does_not_rebind_cancelled_parent_preview_snapshot(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Add/Edit then Cancel must not authorize old outer rows against new context."""
    controls = _controls(harness, monkeypatch, _record(_configuration(harness)))
    initial = harness.current_snapshot
    section = harness.matching.analyze(_configuration(harness), initial).sections[0]

    def refresh_child_then_cancel(dialog: Any) -> None:
        """Use two explicit snapshots even though their trace geometry is identical."""
        harness.current_snapshot = replace(initial, context_digest="new-child-context")
        child_snapshot = dialog.refresh_snapshot()
        assert child_snapshot != dialog.snapshot
        child_capture = dialog.guarded_preview(section, child_snapshot)
        assert child_capture.data == PNG
        with pytest.raises(harness.model.ValidationError, match="changed.*Review"):
            dialog.guarded_preview(section)
        assert len(harness.renderers) == 1
        assert dialog.guarded_preview(section, child_snapshot) is child_capture

    harness.modal_action = refresh_child_then_cancel
    controls.on_configure(None)
    assert not harness.errors
    assert len(harness.renderers) == 1
    assert all(
        renderer.closed and renderer.close_calls == 1 for renderer in harness.renderers
    )


@pytest.mark.parametrize("change", ["save_as", "registry"])
def test_preview_snapshot_guard_retains_board_identity_checks(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """An identical geometry snapshot cannot bypass Save As or registry ownership."""
    controls = _controls(harness, monkeypatch, _record(_configuration(harness)))

    def verify_changed_identity(dialog: Any) -> None:
        """Exercise the injected guard, including when a UI reuses cached pixels."""
        if change == "save_as":
            harness.board_path += ".renamed"
        else:
            harness.database.current = False
        assert callable(dialog.verify_snapshot)
        with pytest.raises(
            (harness.model.ValidationError, ValueError), match="changed"
        ):
            dialog.verify_snapshot(dialog.snapshot)

    harness.modal_action = verify_changed_identity
    controls.on_configure(None)
    assert not harness.errors
    assert not harness.renderers
    assert not harness.database.saves


@pytest.mark.parametrize("phase", ["before", "after"])
def test_unreadable_snapshot_cannot_return_a_reviewable_preview(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    """Fail closed when a snapshot read itself fails, not only on unequal records."""
    controls = _controls(harness, monkeypatch, _record(_configuration(harness)))
    initial = harness.current_snapshot
    section = harness.matching.analyze(_configuration(harness), initial).sections[0]
    failed = False

    def snapshot() -> Any:
        """Expose an actual read failure after opening the dialog successfully."""
        if failed:
            raise RuntimeError("Cannot read changed native board context")
        return initial

    def fail_snapshot() -> None:
        """Simulate a native getter or project context becoming unreadable."""
        nonlocal failed
        failed = True

    monkeypatch.setattr(controls, "_snapshot", snapshot)

    def preview_unreadable_context(dialog: Any) -> None:
        """Never treat unknown context as unchanged or display its scratch output."""
        if phase == "before":
            fail_snapshot()
        else:
            harness.render_action = fail_snapshot
        with pytest.raises(RuntimeError, match="Cannot read changed native"):
            dialog.guarded_preview(section)
        assert len(harness.renderers) == (phase == "after")
        assert all(
            renderer.closed and renderer.close_calls == 1
            for renderer in harness.renderers
        )

    harness.modal_action = preview_unreadable_context
    controls.on_configure(None)
    assert not harness.errors
    assert not harness.database.saves
    assert all(not path.exists() for path in harness.scratch)


def test_corruption_is_visible_and_export_cannot_silently_disable_it(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed saved intent remains in the database and blocks fabrication preflight."""
    corrupt = _record(_configuration(harness))
    corrupt["payload"] = {"malformed": True}
    controls = _controls(harness, monkeypatch, corrupt)
    assert controls.checkbox.value
    assert controls.status.label == "Settings need attention"
    with pytest.raises(harness.model.ValidationError):
        controls.preflight(2)
    assert harness.database.record == corrupt
    assert harness.database.saves == []
    assert harness.snapshots == []


def test_canceling_reset_preserves_raw_record(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corruption prompt cannot erase settings without explicit reset acceptance."""
    corrupt = _record(_configuration(harness))
    corrupt["payload"] = "broken payload"
    controls = _controls(harness, monkeypatch, corrupt)
    controls.on_configure(None)
    assert harness.database.record == corrupt
    assert harness.database.resets == []
    assert harness.dialogs == []
    assert harness.reset_dialogs[0].destroyed
    assert harness.reset_dialogs[0].labels == ("Reset settings", "Cancel")
    assert "will be replaced" in harness.reset_dialogs[0].message
    assert "cannot be undone" in harness.reset_dialogs[0].message
    assert "retained" not in harness.reset_dialogs[0].message


def test_explicit_reset_replaces_record_with_exact_token(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A confirmed reset replaces only the record whose token the user approved."""
    corrupt = _record(_configuration(harness))
    corrupt["payload"] = {"malformed": True}
    controls = _controls(harness, monkeypatch, corrupt)
    harness.reset_result = harness.wx.ID_YES
    controls.on_configure(None)
    assert harness.database.resets == [
        (
            "board-a",
            json.dumps(corrupt, sort_keys=True),
            harness.model.Config().to_dict(),
        )
    ]
    assert controls.config == harness.model.Config()
    assert harness.database.record["payload"] == harness.model.Config().to_dict()
    assert harness.database.record["revision"] == corrupt["revision"] + 1
    assert not controls.checkbox.value
    assert harness.dialogs[0].destroyed
    assert harness.database.saves == []


@pytest.mark.parametrize("change", ["save_as", "registry", "concurrent_record"])
def test_reset_rechecks_identity_and_record_after_confirmation(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """Accepting a prompt cannot reset another board or overwrite a newer record."""
    corrupt = _record(_configuration(harness))
    corrupt["payload"] = {"malformed": True}
    controls = _controls(harness, monkeypatch, corrupt)
    harness.reset_result = harness.wx.ID_YES

    def concurrent_change() -> None:
        """Modify ownership or data while the reset confirmation is displayed."""
        if change == "save_as":
            harness.board_path += ".renamed"
        elif change == "registry":
            harness.database.current = False
        else:
            harness.database.record = _record(_configuration(harness, True), 8)

    harness.reset_action = concurrent_change
    controls.on_configure(None)
    assert harness.database.resets == []
    assert harness.errors
    assert harness.reset_dialogs[0].destroyed
    assert harness.dialogs == []
    expected = (
        _record(_configuration(harness, True), 8)
        if change == "concurrent_record"
        else corrupt
    )
    assert harness.database.record == expected


def test_real_database_reset_replaces_malformed_json_without_archive(
    harness: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit toolbar reset replaces the row without creating an archive."""
    name = "_impedance_controls_database"
    path = Path(__file__).resolve().parents[1] / "impedance" / "database.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    board_path = Path(harness.board_path)
    board_path.write_text("(kicad_pcb)", encoding="utf-8")
    database = module.ImpedanceDatabase(tmp_path / "jlcpcb" / "project.db")
    board_id = database.resolve_board(board_path)
    original = "{ exact invalid JSON : Ω }"
    with database.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO board_feature_config (board_id, feature, version, revision, enabled, payload_json) VALUES (?, 'impedance', 1, 3, 1, ?)",
            (board_id, original),
        )
    controls = harness.integration.ImpedanceControls(harness.parent, harness.toolbar)
    controls.attach_store(Store(tmp_path / "jlcpcb" / "project.db"))
    monkeypatch.setattr(controls, "_snapshot", lambda: _snapshot(harness))
    harness.reset_result = harness.wx.ID_YES
    controls.on_configure(None)
    with database.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "board_feature_recovery" not in tables
    assert database.load_config(board_id)["payload"] == harness.model.Config().to_dict()
    assert controls.revision == 4
    assert not controls.checkbox.value


def test_concurrent_repair_before_reset_token_preserves_valid_settings(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An old corruption error cannot authorize resetting newly repaired settings."""
    corrupt = _record(_configuration(harness))
    corrupt["payload"] = {"malformed": True}
    controls = _controls(harness, monkeypatch, corrupt)
    repaired = _record(_configuration(harness, True), 8)
    original_token = harness.database.config_reset_token

    def repair_before_token(board_id: str) -> str:
        """Simulate another window repairing the record after the failed load."""
        harness.database.record = deepcopy(repaired)
        return original_token(board_id)

    monkeypatch.setattr(harness.database, "config_reset_token", repair_before_token)
    harness.reset_result = harness.wx.ID_YES
    controls.on_configure(None)
    assert harness.database.resets == []
    assert harness.database.record == repaired
    assert harness.reset_dialogs == []
