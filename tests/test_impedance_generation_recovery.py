"""Compose real impedance controls with Generate across recovery and review gates."""

from copy import deepcopy
from dataclasses import replace
import importlib
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from zipfile import ZipFile

import pytest

from .modal_test_support import review_pane
from .test_impedance_controls import Store, _configuration, _controls, _record, harness
from .test_impedance_output_variant import _approve, _variant_controls
from .test_mainwindow_impedance_flow import _reports, _window, runtime

__all__ = ["harness", "runtime"]


def _compose(
    state: SimpleNamespace, runtime: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, Any]:
    """Keep native boundaries stateful while using real storage and event handlers."""
    controls = _controls(state, monkeypatch, _record(_configuration(state, True)))
    window, _steps = _window(runtime)
    window.pcbnew = state.pcbnew
    window.fabrication.board = state.board
    window._legacy_board = lambda: state.board
    controls.parent = window
    window._impedance = controls
    monkeypatch.setattr(
        runtime, "ImpedanceValidationError", state.model.ValidationError
    )
    monkeypatch.setattr(
        runtime,
        "ImpedanceStorageError",
        getattr(state.integration, "ImpedanceStorageError", RuntimeError),
        raising=False,
    )
    adapter = importlib.import_module(f"{state.integration.__package__}.pcbnew_adapter")
    monkeypatch.setattr(
        adapter, "snapshot_board", lambda *_args, **_kwargs: state.current_snapshot
    )
    return controls, window


def _choices(
    state: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    actions: list[int],
    action: Any = None,
) -> list[Any]:
    """Model the selected action, preserving the safety of the initial selection."""
    dialogs: list[Any] = []

    class Choice:
        def __init__(
            self, _parent: Any, message: str, _title: str, choices: list[str]
        ) -> None:
            self.message, self.choices = message, choices
            self.selected = -1
            self.destroyed = False
            dialogs.append(self)

        def SetSelection(self, selection: int) -> None:
            self.selected = selection

        def ShowModal(self) -> int:
            assert self.selected == 0
            if action is not None:
                action()
            self.selected = actions.pop(0)
            return state.wx.ID_OK if self.selected >= 0 else state.wx.ID_CANCEL

        def GetSelection(self) -> int:
            return self.selected

        def Destroy(self) -> None:
            self.destroyed = True

    monkeypatch.setattr(state.wx, "SingleChoiceDialog", Choice, raising=False)
    return dialogs


@pytest.mark.parametrize("selection", [0, -1, 99, 2])
@pytest.mark.parametrize("failure_stage", ["attach", "later"])
def test_storage_failure_requires_a_fresh_explicit_generation_choice(
    harness: SimpleNamespace,
    runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
    selection: int,
    failure_stage: str,
) -> None:
    """Cancel fails closed; explicit omission is local and never disables saved intent."""
    controls, window = _compose(harness, runtime, monkeypatch)
    original = deepcopy(harness.database.record)

    def unreadable(_board_id: str) -> Any:
        raise sqlite3.OperationalError("impedance settings locked")

    monkeypatch.setattr(harness.database, "load_config", unreadable)
    if failure_stage == "attach":
        controls.attach_store(harness.store)
    dialogs = _choices(harness, monkeypatch, [selection, selection])
    exports = MagicMock()
    monkeypatch.setattr(runtime, "export_impedance_reports", exports)
    for _attempt in range(2):
        runtime.JLCPCBTools.generate_fabrication_data(window)
    assert len(dialogs) == 2
    assert all(dialog.destroyed for dialog in dialogs)
    assert all("locked" in dialog.message for dialog in dialogs)
    assert controls.configure_button.enabled
    assert not controls.checkbox.enabled
    assert not harness.dialogs
    assert not harness.database.resets and not harness.database.saves
    assert harness.database.record == original
    exports.assert_not_called()
    if selection == 2:
        assert window.fabrication.zip_gerber_excellon.call_count == 2
        assert all(
            not call.args
            for call in window.fabrication.zip_gerber_excellon.call_args_list
        )
        assert window.store.increment_generation_count.call_count == 2
    else:
        window.fabrication.generate_geber.assert_not_called()
        window.fabrication.zip_gerber_excellon.assert_not_called()
        window.store.increment_generation_count.assert_not_called()
    window.logger.exception.assert_not_called()
    runtime.wx.MessageBox.assert_not_called()


def test_retry_storage_recovers_without_resetting_enabled_intent(
    harness: SimpleNamespace, runtime: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only Retry can recover attachment and use the existing saved config."""
    controls, window = _compose(harness, runtime, monkeypatch)
    controls._save(replace(controls.config, enabled=False))
    original = deepcopy(harness.database.record)
    load = harness.database.load_config
    monkeypatch.setattr(
        harness.database,
        "load_config",
        MagicMock(side_effect=sqlite3.OperationalError("locked")),
    )
    controls.attach_store(harness.store)
    dialogs = _choices(
        harness,
        monkeypatch,
        [1],
        lambda: monkeypatch.setattr(harness.database, "load_config", load),
    )
    runtime.JLCPCBTools.generate_fabrication_data(window)
    assert len(dialogs) == 1 and dialogs[0].destroyed
    assert controls.checkbox.enabled and controls.configure_button.enabled
    assert harness.database.record == original
    assert not harness.database.resets
    assert not harness.dialogs
    window.fabrication.zip_gerber_excellon.assert_called_once_with()
    window.store.increment_generation_count.assert_called_once()


def test_standalone_retry_routes_repaired_invalid_settings_to_guarded_reset(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retrying a readable malformed row offers the normal reset without reopening."""
    controls = _controls(harness, monkeypatch, _record(_configuration(harness, True)))
    load = harness.database.load_config
    corrupt = deepcopy(harness.database.record)
    corrupt["payload"] = {"malformed": True}
    monkeypatch.setattr(
        harness.database,
        "load_config",
        MagicMock(side_effect=sqlite3.OperationalError("locked")),
    )
    controls.attach_store(harness.store)

    def repair_storage() -> None:
        harness.database.record = corrupt
        monkeypatch.setattr(harness.database, "load_config", load)

    dialogs = _choices(harness, monkeypatch, [1], repair_storage)
    controls.on_configure(None)
    assert len(dialogs) == 1 and len(dialogs[0].choices) == 2
    assert len(harness.reset_dialogs) == 1
    assert harness.reset_dialogs[0].destroyed
    assert harness.database.record == corrupt
    assert not harness.database.resets and not harness.database.saves
    assert not harness.dialogs and not harness.errors


@pytest.mark.parametrize("when", ["before_open", "during_snapshot"])
def test_generation_configure_rejects_a_changed_prepared_source(
    harness: SimpleNamespace,
    runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    """Preparation reuse cannot adopt new copper before or during capture setup."""
    controls, window = _compose(harness, runtime, monkeypatch)
    prepared = controls.prepared_source_revision(window.fabrication.board)
    changed = replace(harness.current_snapshot, context_digest="new-unprepared-copper")
    if when == "before_open":
        harness.current_snapshot = changed
    else:

        def snapshot_after_change() -> Any:
            harness.current_snapshot = changed
            return changed

        monkeypatch.setattr(controls, "_snapshot", snapshot_after_change)
    controls.on_configure(None, prepared_source=prepared)
    assert not harness.dialogs
    assert not harness.database.saves and not harness.database.resets
    window.fabrication.fill_zone_regions.assert_not_called()
    assert len(harness.errors) == 1 and "prepared board changed" in harness.errors[0]


@pytest.mark.parametrize("reset", [False, True])
@pytest.mark.parametrize("repair_geometry_during_reset", [False, True])
def test_invalid_configuration_reset_remains_available_when_source_snapshot_fails(
    harness: SimpleNamespace,
    runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset: bool,
    repair_geometry_during_reset: bool,
) -> None:
    """A failed prepared snapshot cannot prevent an explicit guarded settings reset."""
    controls, window = _compose(harness, runtime, monkeypatch)
    corrupt = deepcopy(harness.database.record)
    corrupt["payload"] = {"malformed": True}
    harness.database.record = corrupt
    controls.attach_store(harness.store)
    harness.reset_result = harness.wx.ID_YES if reset else harness.wx.ID_NO
    adapter = importlib.import_module(
        f"{harness.integration.__package__}.pcbnew_adapter"
    )
    snapshot = MagicMock(side_effect=RuntimeError("unsupported snapshot geometry"))
    monkeypatch.setattr(adapter, "snapshot_board", snapshot)

    def during_reset() -> None:
        if repair_geometry_during_reset:
            snapshot.side_effect = None
            snapshot.return_value = replace(
                harness.current_snapshot, context_digest="edited-during-reset"
            )

    harness.reset_action = during_reset
    destination = tmp_path / "fabrication.zip"
    with ZipFile(destination, "w") as archive:
        archive.writestr("previous.gbr", "old copper")
        archive.writestr("Required_impedance_control.xlsx", "old workbook")
        archive.writestr("Required_impedance_control.html", "old HTML")
    previous = destination.read_bytes()

    def archive_files(entries: Any = ()) -> None:
        with ZipFile(destination, "w") as archive:
            archive.writestr("current.gbr", "new copper")
            for entry in entries:
                archive.write(entry.source, entry.archive_name)

    window.fabrication.zip_gerber_excellon.side_effect = archive_files
    runtime.JLCPCBTools.generate_fabrication_data(window)
    assert len(harness.reset_dialogs) == 1
    assert harness.reset_dialogs[0].destroyed
    assert snapshot.call_count == 1
    assert not harness.dialogs
    if reset:
        assert len(harness.database.resets) == 1
        assert controls.config == harness.model.Config()
        assert controls.revision == corrupt["revision"] + 1
        assert harness.errors == ["unsupported snapshot geometry"]
        with ZipFile(destination) as archive:
            assert archive.namelist() == ["current.gbr"]
        window.store.increment_generation_count.assert_called_once()
    else:
        assert not harness.database.resets
        assert harness.database.record == corrupt
        assert destination.read_bytes() == previous
        assert not harness.errors
        window.fabrication.zip_gerber_excellon.assert_not_called()
        window.store.increment_generation_count.assert_not_called()
    window.logger.exception.assert_not_called()


@pytest.mark.parametrize("when", ["reset_modal", "capture", "review_modal"])
def test_guarded_reset_stops_generation_after_prepared_source_changes(
    harness: SimpleNamespace,
    runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    """A retained disabled reset cannot permit export with stale copper findings."""
    controls, window = _compose(harness, runtime, monkeypatch)
    corrupt = deepcopy(harness.database.record)
    corrupt["payload"] = {"malformed": True}
    harness.database.record = corrupt
    controls.attach_store(harness.store)
    harness.reset_result = harness.wx.ID_YES

    def edit_copper() -> None:
        harness.current_snapshot = replace(
            harness.current_snapshot, context_digest="copper-edited-during-reset"
        )

    if when == "reset_modal":
        harness.reset_action = edit_copper
    elif when == "capture":

        def changed_snapshot() -> Any:
            edit_copper()
            return harness.current_snapshot

        monkeypatch.setattr(controls, "_snapshot", changed_snapshot)
    else:
        harness.modal_action = lambda _dialog: edit_copper()
    runtime.JLCPCBTools.generate_fabrication_data(window)
    assert len(harness.reset_dialogs) == 1
    assert len(harness.database.resets) == 1
    assert len(harness.dialogs) == int(when == "review_modal")
    assert controls.config == harness.model.Config()
    assert len(harness.errors) == 1 and "prepared board changed" in harness.errors[0]
    window.fabrication.fill_zones.assert_called_once_with()
    window.fabrication.fill_zone_regions.assert_not_called()
    window.fabrication.generate_geber.assert_not_called()
    window.fabrication.generate_excellon.assert_not_called()
    window.fabrication.zip_gerber_excellon.assert_not_called()
    window.store.increment_generation_count.assert_not_called()
    window.logger.exception.assert_not_called()


@pytest.mark.parametrize("accept", [False, True])
def test_generate_after_guarded_reset_respects_configuration_modal_result(
    harness: SimpleNamespace,
    runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
    accept: bool,
) -> None:
    """Cancel stops this generation even when the explicit reset retained defaults."""
    controls, window = _compose(harness, runtime, monkeypatch)
    corrupt = deepcopy(harness.database.record)
    corrupt["payload"] = {"malformed": True}
    harness.database.record = corrupt
    controls.attach_store(harness.store)
    harness.reset_result = harness.wx.ID_YES
    harness.modal_result = harness.wx.ID_OK if accept else harness.wx.ID_CANCEL
    runtime.JLCPCBTools.generate_fabrication_data(window)
    assert len(harness.reset_dialogs) == 1
    assert len(harness.database.resets) == 1
    assert len(harness.dialogs) == 1 and harness.dialogs[0].destroyed
    assert controls.config == harness.model.Config()
    if accept:
        window.fabrication.zip_gerber_excellon.assert_called_once_with()
        window.store.increment_generation_count.assert_called_once()
    else:
        window.fabrication.generate_geber.assert_not_called()
        window.fabrication.generate_excellon.assert_not_called()
        window.fabrication.zip_gerber_excellon.assert_not_called()
        window.store.increment_generation_count.assert_not_called()
    assert not harness.errors
    window.logger.exception.assert_not_called()


@pytest.mark.parametrize("when", ["before_capture", "capture", "after_modal"])
def test_known_prepared_source_read_failure_stops_generation_after_retained_reset(
    harness: SimpleNamespace,
    runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    """A later unreadable source cannot use the initial-unavailable recovery exception."""
    controls, window = _compose(harness, runtime, monkeypatch)
    corrupt = deepcopy(harness.database.record)
    corrupt["payload"] = {"malformed": True}
    harness.database.record = corrupt
    controls.attach_store(harness.store)
    harness.reset_result = harness.wx.ID_YES
    adapter = importlib.import_module(
        f"{harness.integration.__package__}.pcbnew_adapter"
    )
    failure = MagicMock(side_effect=RuntimeError("prepared source is now unreadable"))
    if when == "before_capture":
        harness.reset_action = lambda: monkeypatch.setattr(
            adapter, "snapshot_board", failure
        )
    elif when == "capture":
        monkeypatch.setattr(controls, "_snapshot", failure)
    else:
        harness.modal_action = lambda _dialog: monkeypatch.setattr(
            adapter, "snapshot_board", failure
        )
    runtime.JLCPCBTools.generate_fabrication_data(window)
    assert len(harness.database.resets) == 1
    assert controls.config == harness.model.Config()
    assert harness.errors == ["prepared source is now unreadable"]
    window.fabrication.generate_geber.assert_not_called()
    window.fabrication.generate_excellon.assert_not_called()
    window.fabrication.zip_gerber_excellon.assert_not_called()
    window.store.increment_generation_count.assert_not_called()
    window.drc_review_findings.assert_not_called()
    window.logger.exception.assert_not_called()


def test_generate_configure_reuses_prepared_copper_and_one_empty_zone_decision(
    harness: SimpleNamespace, runtime: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real Configure sees the filled revision without another fill or empty-zone pane."""
    controls, window = _compose(harness, runtime, monkeypatch)
    filled = replace(
        harness.current_snapshot, context_digest="generation-filled-copper"
    )

    def fill() -> list[str]:
        harness.current_snapshot = filled
        return ["GND on B.Cu"]

    window.fabrication.fill_zones.side_effect = fill
    harness.modal_result = harness.wx.ID_OK
    harness.modal_action = lambda dialog: _approve(harness, dialog)
    pane = review_pane(runtime.wx.ID_OK)
    monkeypatch.setattr(runtime, "GenerationReviewDialog", pane)
    empty_dialog = MagicMock()
    monkeypatch.setattr(runtime, "EmptyCopperZonesDialog", empty_dialog)
    monkeypatch.setattr(
        runtime,
        "export_impedance_reports",
        lambda _plan, _board, _pcbnew, scratch: _reports(scratch),
    )
    runtime.JLCPCBTools.generate_fabrication_data(window)
    window.fabrication.fill_zones.assert_called_once_with()
    window.fabrication.fill_zone_regions.assert_not_called()
    empty_dialog.assert_not_called()
    assert len(harness.dialogs) == 1
    assert harness.dialogs[0].snapshot == filled
    assert len(pane.instances) == 1
    assert [finding.check for finding in pane.instances[0].findings] == ["Copper zones"]
    window.fabrication.zip_gerber_excellon.assert_called_once()
    window.store.increment_generation_count.assert_called_once()
    assert not harness.errors
    window.logger.exception.assert_not_called()


@pytest.mark.parametrize("busy", [False, True])
def test_configure_from_generation_restores_initial_busy_state(
    runtime: Any, busy: bool
) -> None:
    """A helper invoked outside an existing busy scope cannot start one on return."""
    window, _steps = _window(runtime)
    error = runtime.ImpedanceValidationError("Review needed")
    window._impedance.preflight.side_effect = [error, None]
    runtime.wx.IsBusy.return_value = busy
    window._impedance_plan_for_generation(window._impedance, None)
    assert runtime.wx.EndBusyCursor.call_count == int(busy)
    assert runtime.wx.BeginBusyCursor.call_count == int(busy)


@pytest.mark.parametrize("failing_read", [2, 3])
@pytest.mark.parametrize("omit", [False, True])
def test_later_disabled_verification_failure_needs_explicit_archive_decision(
    harness: SimpleNamespace,
    runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failing_read: int,
    omit: bool,
) -> None:
    """Both disabled guards remain active and can recover a later read failure safely."""
    controls, window = _compose(harness, runtime, monkeypatch)
    controls._save(replace(controls.config, enabled=False))
    original = deepcopy(harness.database.record)
    load = harness.database.load_config
    reads = 0

    def load_until_failure(board_id: str) -> Any:
        nonlocal reads
        reads += 1
        if reads >= failing_read:
            raise sqlite3.OperationalError("later storage read failed")
        return load(board_id)

    monkeypatch.setattr(harness.database, "load_config", load_until_failure)
    dialogs = _choices(harness, monkeypatch, [2 if omit else 0])
    destination = tmp_path / "fabrication.zip"
    with ZipFile(destination, "w") as archive:
        archive.writestr("previous.gbr", "old copper")
        archive.writestr("Required_impedance_control.xlsx", "old workbook")
        archive.writestr("Required_impedance_control.html", "old HTML")
    previous = destination.read_bytes()

    def archive_files(entries: Any = ()) -> None:
        with ZipFile(destination, "w") as archive:
            archive.writestr("current.gbr", "new copper")
            for entry in entries:
                archive.write(entry.source, entry.archive_name)

    window.fabrication.zip_gerber_excellon.side_effect = archive_files
    runtime.JLCPCBTools.generate_fabrication_data(window)
    assert len(dialogs) == 1 and dialogs[0].destroyed
    assert harness.database.record == original
    if omit:
        with ZipFile(destination) as archive:
            assert archive.namelist() == ["current.gbr"]
        window.store.increment_generation_count.assert_called_once()
    else:
        assert destination.read_bytes() == previous
        window.store.increment_generation_count.assert_not_called()
    window.logger.exception.assert_not_called()


def test_real_damaged_feature_schema_can_be_omitted_without_repair_or_disable(
    harness: SimpleNamespace,
    runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Only an explicit generation choice can bypass unreadable feature tables."""
    database_type = harness.integration.ImpedanceDatabase
    controls, window = _compose(harness, runtime, monkeypatch)
    monkeypatch.setattr(harness.integration, "ImpedanceDatabase", database_type)
    path = tmp_path / "real" / "jlcpcb" / "project.db"
    database = database_type(path)
    board_id = database.resolve_board(harness.board_path)
    original = _configuration(harness, True)
    database.save_config(board_id, original.to_dict(), True, 0)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE impedance_stackup_catalog")
        connection.execute("CREATE TABLE impedance_stackup_catalog (wrong TEXT)")
        connection.execute("INSERT INTO impedance_stackup_catalog VALUES ('preserve')")
    before = path.read_bytes()
    controls.attach_store(Store(path))
    assert controls.repository is None
    dialogs = _choices(harness, monkeypatch, [2, 0])
    runtime.JLCPCBTools.generate_fabrication_data(window)
    assert path.read_bytes() == before
    reopened = harness.integration.ImpedanceControls(window, harness.toolbar)
    reopened.attach_store(Store(path))
    window._impedance = reopened
    runtime.JLCPCBTools.generate_fabrication_data(window)
    assert len(dialogs) == 2
    assert path.read_bytes() == before
    assert controls.configure_button.enabled and reopened.configure_button.enabled
    assert window.fabrication.zip_gerber_excellon.call_count == 1
    assert window.store.increment_generation_count.call_count == 1


@pytest.mark.parametrize("failure_stage", ["before_plot", "after_render"])
@pytest.mark.parametrize("omit", [False, True])
def test_enabled_verification_storage_failure_cannot_silently_drop_reports(
    harness: SimpleNamespace,
    runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_stage: str,
    omit: bool,
) -> None:
    """Both enabled verification gates preserve intent and require an archive decision."""
    controls, window = _compose(harness, runtime, monkeypatch)
    harness.modal_result = harness.wx.ID_OK
    harness.modal_action = lambda dialog: _approve(harness, dialog)
    controls.on_configure(None)
    assert not harness.errors
    assert controls.config.enabled and controls.config.reviewed_digest
    original = deepcopy(harness.database.record)
    dialogs = _choices(harness, monkeypatch, [2 if omit else 0])
    destination = tmp_path / "fabrication.zip"
    with ZipFile(destination, "w") as archive:
        archive.writestr("previous.gbr", "old copper")
        archive.writestr("Required_impedance_control.xlsx", "old workbook")
        archive.writestr("Required_impedance_control.html", "old HTML")
    previous = destination.read_bytes()

    def unavailable() -> None:
        monkeypatch.setattr(
            harness.database,
            "load_config",
            MagicMock(
                side_effect=sqlite3.OperationalError("enabled settings unreadable")
            ),
        )

    def hook(stage: str, *_args: Any, **_kwargs: Any) -> bool:
        if stage == "pre" and failure_stage == "before_plot":
            unavailable()
        return True

    def export(_plan: Any, _board: Any, _pcbnew: Any, scratch: Path) -> Any:
        reports = _reports(scratch)
        assert reports.workbook.read_bytes() == b"workbook"
        assert reports.html_report.read_text() == "<html>report</html>"
        if failure_stage == "after_render":
            unavailable()
        return reports

    def archive_files(entries: Any = ()) -> None:
        with ZipFile(destination, "w") as archive:
            archive.writestr("current.gbr", "new copper")
            for entry in entries:
                archive.write(entry.source, entry.archive_name)

    exports = MagicMock(side_effect=export)
    monkeypatch.setattr(runtime, "export_impedance_reports", exports)
    window.fabrication.zip_gerber_excellon.side_effect = archive_files
    window.run_generate_hook.side_effect = hook
    runtime.JLCPCBTools.generate_fabrication_data(window)
    assert len(dialogs) == 1 and dialogs[0].destroyed
    assert harness.database.record == original
    assert controls.config.enabled
    assert not harness.database.resets
    assert exports.call_count == int(failure_stage == "after_render")
    if omit:
        with ZipFile(destination) as archive:
            assert archive.namelist() == ["current.gbr"]
        window.store.increment_generation_count.assert_called_once()
    else:
        assert destination.read_bytes() == previous
        window.store.increment_generation_count.assert_not_called()
        window.fabrication.zip_gerber_excellon.assert_not_called()
    window.logger.exception.assert_not_called()


def test_native_identity_read_failure_is_never_a_storage_omission_choice(
    harness: SimpleNamespace,
    runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable active PCB cannot authorize export from uncertain identity."""
    _controls, window = _compose(harness, runtime, monkeypatch)
    dialogs = _choices(harness, monkeypatch, [2])
    monkeypatch.setattr(
        harness.board,
        "GetFileName",
        MagicMock(side_effect=RuntimeError("native board unavailable")),
    )
    runtime.JLCPCBTools.generate_fabrication_data(window)
    assert not dialogs
    window.fabrication.zip_gerber_excellon.assert_not_called()
    window.store.increment_generation_count.assert_not_called()
    assert not harness.database.saves and not harness.database.resets


@pytest.mark.parametrize("when", ["choice", "pre_hook"])
@pytest.mark.parametrize("identity", ["save_as", "registry", "native_error"])
def test_explicit_omission_does_not_bypass_board_identity_changes(
    harness: SimpleNamespace,
    runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
    identity: str,
) -> None:
    """A deliberate report omission still belongs to its original native PCB."""
    controls, window = _compose(harness, runtime, monkeypatch)
    monkeypatch.setattr(
        harness.database,
        "load_config",
        MagicMock(side_effect=sqlite3.OperationalError("locked")),
    )

    def change_identity() -> None:
        if identity == "save_as":
            harness.board_path += ".renamed"
        elif identity == "registry":
            harness.database.current = False
        else:
            monkeypatch.setattr(
                harness.board,
                "GetFileName",
                MagicMock(side_effect=RuntimeError("native board unavailable")),
            )

    dialogs = _choices(
        harness, monkeypatch, [2], change_identity if when == "choice" else None
    )
    if when == "pre_hook":

        def hook(stage: str, *_args: Any, **_kwargs: Any) -> bool:
            if stage == "pre":
                change_identity()
            return True

        window.run_generate_hook.side_effect = hook
    runtime.JLCPCBTools.generate_fabrication_data(window)
    assert len(dialogs) == 1
    window.fabrication.zip_gerber_excellon.assert_not_called()
    window.store.increment_generation_count.assert_not_called()
    assert not harness.database.resets and not harness.database.saves


def test_generate_configure_reuses_prepared_source_for_selected_output_variant(
    harness: SimpleNamespace,
    runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generate reviews a fresh copy of A from filled source while editor B is unchanged."""
    controls, state = _variant_controls(harness, monkeypatch)
    window, _steps = _window(runtime)
    window.pcbnew = harness.pcbnew
    window._legacy_board = lambda: state.live
    window._impedance = controls
    window.fabrication.board = state.live
    window.fabrication.copy_variant_board = (
        harness.parent.fabrication.copy_variant_board
    )
    window._variant_controller = harness.parent._variant_controller
    window._variant_controller.begin_generation = MagicMock()
    window._variant_controller.end_generation = MagicMock()
    controls.parent = window
    monkeypatch.setattr(
        runtime, "ImpedanceValidationError", harness.model.ValidationError
    )
    monkeypatch.setattr(
        runtime, "ImpedanceStorageError", harness.integration.ImpedanceStorageError
    )

    def fill() -> list[str]:
        state.version = "prepared-source"
        return ["GND on B.Cu"]

    window.fabrication.fill_zones.side_effect = fill
    harness.modal_result = harness.wx.ID_OK
    harness.modal_action = lambda dialog: _approve(harness, dialog)
    pane = review_pane(runtime.wx.ID_CANCEL)
    monkeypatch.setattr(runtime, "GenerationReviewDialog", pane)
    runtime.JLCPCBTools.generate_fabrication_data(window)
    assert len(harness.dialogs) == 1
    assert "prepared-source" in controls._live_snapshot().context_digest
    assert harness.dialogs[0].snapshot == controls._snapshot()
    assert state.live.variant == "B"
    assert all(renderer.board.variant == "A" for renderer in harness.renderers)
    assert all(owner.closed for owner in state.owners)
    window.fabrication.fill_zones.assert_called_once_with()
    window.fabrication.fill_zone_regions.assert_not_called()
    assert len(pane.instances) == 1
    window.fabrication.zip_gerber_excellon.assert_not_called()
    assert not harness.errors
