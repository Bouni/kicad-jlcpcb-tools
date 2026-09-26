"""Exercise publication and bookkeeping through the real generation handler."""

from contextlib import closing
from dataclasses import dataclass, replace
from functools import partial
import importlib
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from zipfile import ZipFile

import pytest

from .fabrication_test_support import Point, modules as fabrication_modules
from .test_mainwindow_empty_zone_warning import (
    _make_window,
    mainwindow_module as handler_module,
)
from .test_mainwindow_impedance_flow import _reports, _reviewed_plan
from .variant_data_support import _native_session

modules = fabrication_modules
mainwindow_module = handler_module


@dataclass
class Generation:
    """Hold a real export operation with UI and native plotting boundaries."""

    module: Any
    wx: Any
    window: Any
    session: Any
    store: Any
    exporter: Any
    archive: Any
    paths: dict[str, Path]
    previous: dict[str, bytes]
    hooks: list[str]

    def run(self) -> None:
        """Invoke the production button handler without creating native windows."""
        self.module.JLCPCBTools.generate_fabrication_data(self.window)

    def assert_publication_error(self, detail: str) -> str:
        """Keep failure attribution distinct from earlier generation and hooks."""
        self.wx.MessageBox.assert_called_once()
        message, title, _style = self.wx.MessageBox.call_args.args
        assert title == "Generate fabrication data"
        assert message.startswith(
            "Fabrication data generation failed during: Publishing fabrication files\n"
        )
        assert detail in message
        return message

    def assert_released(self) -> None:
        """Verify both the session guard and filesystem operation were released."""
        assert not self.session.generating
        assert self.exporter.output_snapshot is None
        assert not list(Path(self.exporter.outputdir).glob(".jlcpcb-generation-*"))
        assert not list(Path(self.exporter.outputdir).glob(".jlcpcb-recovery-*"))

    def assert_previous_outputs(self) -> None:
        """Read final paths again so in-memory mocks cannot hide replaced files."""
        assert {key: path.read_bytes() for key, path in self.paths.items()} == (
            self.previous
        )

    def assert_generated_outputs(self) -> None:
        """Verify the real ZIP and CSV writers published this variant's data."""
        with ZipFile(self.paths["gerber_zip"]) as archive:
            assert archive.read("copper.gbr") == b"new variant copper\n"
            assert archive.read("drill.drl") == b"new variant drill\n"
        assert "C200" in self.paths["bom_csv"].read_text()
        assert "R1" in self.paths["cpl_csv"].read_text()
        assert all(
            path.read_bytes() != self.previous[key] for key, path in self.paths.items()
        )


@pytest.fixture
def generation(mainwindow_module: Any, modules: Any, tmp_path: Path) -> Generation:
    """Combine a real native-adapter session, SQLite store, exporter and handler."""
    window_module, wx = mainwindow_module
    _, session, store, _adapter, board = _native_session(tmp_path)
    store.increment_generation_count()
    board.Footprints = board.GetFootprints
    board.GetDesignSettings = lambda: SimpleNamespace(GetAuxOrigin=lambda: Point(0, 0))
    for footprint in board.parts:
        footprint.GetFPID = lambda: SimpleNamespace(GetLibItemName=lambda: "R0603")
        footprint.GetOrientation = lambda: SimpleNamespace(AsDegrees=lambda: 90.0)
        footprint.Pads = lambda: []
    session.refresh()
    session.set_output_variant("A")

    window, _steps = _make_window(window_module, [])
    window.store = store
    exporter = modules.fabrication.Fabrication(window, board)
    window.fabrication = exporter
    exporter.variant_name = "A"
    paths = {key: Path(value) for key, value in exporter.get_artifact_paths().items()}
    for key, destination in paths.items():
        destination.write_bytes(("previous " + key).encode())
    previous = {key: path.read_bytes() for key, path in paths.items()}

    def begin(corrections: Any) -> None:
        """Use the controller's production session/exporter call sequence."""
        snapshot, name = session.begin_generation()
        exporter.begin_generation(
            snapshot, name, corrections, session.validate_generation
        )

    def end() -> None:
        """Release real exporter/session state as the UI controller does."""
        exporter.abort_generation()
        session.end_generation()
        session.refresh()

    window._variant_controller = SimpleNamespace(
        begin_generation=begin, end_generation=end, session=session, output_name="A"
    )
    window.project_path = str(tmp_path)
    window.pcbnew = SimpleNamespace(GetBoard=lambda: board)
    window._variant_mode = True
    window._get_current_board = window_module.JLCPCBTools._get_current_board.__get__(
        window
    )
    window.build_generate_hook_env = (
        window_module.JLCPCBTools.build_generate_hook_env.__get__(window)
    )
    exporter.fill_zones = lambda: []

    def plot(*_args: Any) -> None:
        """Represent native plotter output with an actual nonempty file."""
        (Path(exporter.gerberdir) / "copper.gbr").write_bytes(b"new variant copper\n")

    def drill() -> None:
        """Represent native drill output with an actual nonempty file."""
        (Path(exporter.gerberdir) / "drill.drl").write_bytes(b"new variant drill\n")

    exporter.generate_geber = plot
    exporter.generate_excellon = drill
    hooks: list[str] = []

    def hook(stage: str, _env: Any, allow_continue: bool) -> bool:
        """Observe the handler's hook boundary without executing shell commands."""
        hooks.append(stage)
        return True

    window.run_generate_hook = hook
    archive = importlib.import_module(
        modules.fabrication.artifact_publication.__module__
    )
    return Generation(
        window_module,
        wx,
        window,
        session,
        store,
        exporter,
        archive,
        paths,
        previous,
        hooks,
    )


def test_readonly_counter_preserves_published_files(generation: Generation) -> None:
    """A counter reservation error leaves all old artifacts and count untouched."""
    database = Path(generation.store.dbfile)
    database.chmod(0o444)
    try:
        generation.run()
        generation.assert_publication_error("readonly database")
        generation.assert_previous_outputs()
        assert generation.store.get_generation_count() == 1
        assert generation.hooks == ["pre"]
        generation.assert_released()
    finally:
        database.chmod(0o644)


def test_counter_commit_failure_restores_published_files(
    generation: Generation, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real SQLite reader blocks COMMIT after all files have been replaced."""
    replaced: list[Path] = []
    replace = generation.archive.os.replace

    def record_replace(source: Any, destination: Any) -> None:
        """Observe staged publications while keeping actual filesystem writes."""
        replace(source, destination)
        if Path(
            destination
        ) in generation.paths.values() and ".jlcpcb-generation-" in str(source):
            replaced.append(Path(destination))

    monkeypatch.setattr(generation.archive.os, "replace", record_replace)
    uri = Path(generation.store.dbfile).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as reader:
        reader.execute("BEGIN")
        assert reader.execute(
            "SELECT value FROM metadata WHERE key = 'generation_count'"
        ).fetchone() == ("1",)
        generation.run()
        generation.assert_publication_error("database is locked")
        assert set(replaced) == set(generation.paths.values())
        generation.assert_previous_outputs()
        assert generation.store.get_generation_count() == 1
        assert generation.hooks == ["pre"]
        generation.assert_released()


def test_locked_counter_preserves_published_files(generation: Generation) -> None:
    """An existing SQLite writer prevents export without replacing prior output."""
    with closing(sqlite3.connect(generation.store.dbfile)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        generation.run()
        generation.assert_publication_error("database is locked")
        generation.assert_previous_outputs()
        assert generation.store.get_generation_count() == 1
        assert generation.hooks == ["pre"]
        generation.assert_released()


def test_handler_respects_publication_guard_and_retries_after_release(
    generation: Generation, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A busy publication guard prevents the handler replacing another export."""
    store_module = importlib.import_module(type(generation.store).__module__)
    guard = store_module.publication_guard
    monkeypatch.setattr(store_module, "publication_guard", partial(guard, timeout=0.02))
    with guard(generation.store.dbfile):
        generation.run()
        generation.assert_previous_outputs()
        generation.assert_publication_error(
            "Another KiCad window or process is publishing fabrication files"
        )
        assert generation.store.get_generation_count() == 1
        assert generation.hooks == ["pre"]
        generation.assert_released()

    generation.wx.MessageBox.reset_mock()
    generation.hooks.clear()
    generation.run()
    generation.wx.MessageBox.assert_not_called()
    generation.assert_generated_outputs()
    assert generation.store.get_generation_count() == 2
    assert generation.hooks == ["pre", "post"]
    generation.assert_released()


@pytest.mark.parametrize("stage", ["plot", "validation"])
def test_board_copy_error_releases_generation_and_allows_retry(
    generation: Generation, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """Catch board-copy errors through the real handler before publishing or counting."""
    plot = generation.exporter.generate_geber

    def fail_copy(*_args: Any) -> None:
        """Represent the catchable error required from the native save/load boundary."""
        raise RuntimeError("temporary board save/load failed")

    def plot_before_validation(*args: Any) -> None:
        """Finish plotting and require the real publication path to check its source."""
        plot(*args)
        generation.exporter._generation.plot_source_digest = "captured source"

    with monkeypatch.context() as patch:
        if stage == "plot":
            patch.setattr(generation.exporter, "generate_geber", fail_copy)
        else:
            patch.setattr(generation.exporter, "generate_geber", plot_before_validation)
            patch.setattr(generation.exporter, "_serialize_board", fail_copy)
        generation.run()

    generation.wx.MessageBox.assert_called_once()
    message, title, _style = generation.wx.MessageBox.call_args.args
    assert title == "Generate fabrication data"
    assert "temporary board save/load failed" in message
    if stage == "validation":
        assert "Publishing fabrication files" in message
    generation.assert_previous_outputs()
    assert generation.store.get_generation_count() == 1
    assert generation.hooks == ["pre"]
    generation.assert_released()

    generation.wx.MessageBox.reset_mock()
    generation.hooks.clear()
    generation.run()
    generation.wx.MessageBox.assert_not_called()
    generation.assert_generated_outputs()
    assert generation.store.get_generation_count() == 2
    assert generation.hooks == ["pre", "post"]
    generation.assert_released()


def test_second_publication_failure_rolls_back_counter_and_first_file(
    generation: Generation, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure midway through publication restores files and cancels the count."""
    replaced: list[Path] = []
    replace = generation.archive.os.replace

    def fail_second(source: Any, destination: Any) -> None:
        """Reject just the second staged artifact, allowing recovery to succeed."""
        if Path(
            destination
        ) in generation.paths.values() and ".jlcpcb-generation-" in str(source):
            if len(replaced) == 1:
                raise OSError("second artifact replacement failed")
            replaced.append(Path(destination))
        replace(source, destination)

    monkeypatch.setattr(generation.archive.os, "replace", fail_second)
    generation.run()
    generation.assert_publication_error("second artifact replacement failed")
    assert len(replaced) == 1
    generation.assert_previous_outputs()
    assert generation.store.get_generation_count() == 1
    assert generation.hooks == ["pre"]
    generation.assert_released()


def test_failed_restoration_keeps_recovery_files_after_handler_cleanup(
    generation: Generation, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handler releases its session without deleting failed recovery copies."""
    replaced: list[Path] = []
    replace = generation.archive.os.replace

    def fail_publication_and_restore(source: Any, destination: Any) -> None:
        """Reject the second publication and the subsequent old-file restore."""
        if Path(destination) in generation.paths.values():
            if ".jlcpcb-recovery-" in str(source):
                raise OSError("restore failed")
            if ".jlcpcb-generation-" in str(source):
                if replaced:
                    raise OSError("second artifact replacement failed")
                replaced.append(Path(destination))
        replace(source, destination)

    monkeypatch.setattr(generation.archive.os, "replace", fail_publication_and_restore)
    generation.run()
    message = generation.assert_publication_error("could not be restored")
    recovery = list(Path(generation.exporter.outputdir).glob(".jlcpcb-recovery-*"))
    assert len(recovery) == 1
    assert str(recovery[0]) in message
    assert str(replaced[0]) in message
    assert {path.read_bytes() for path in recovery[0].iterdir()} == set(
        generation.previous.values()
    )
    assert generation.store.get_generation_count() == 1
    assert generation.hooks == ["pre"]
    assert not generation.session.generating
    assert generation.exporter.output_snapshot is None
    assert not list(Path(generation.exporter.outputdir).glob(".jlcpcb-generation-*"))


def test_changed_counter_after_pre_hook_rejects_stale_generation(
    generation: Generation,
) -> None:
    """Another completed generation invalidates this operation's captured count."""

    def hook(stage: str, env: dict[str, str], allow_continue: bool) -> bool:
        """Change the persistent count through an independent connection."""
        generation.hooks.append(stage)
        if stage == "pre":
            assert env["JLCPCB_GENERATION_COUNT"] == "1"
            with closing(sqlite3.connect(generation.store.dbfile)) as other, other:
                other.execute(
                    "UPDATE metadata SET value = '2' WHERE key = 'generation_count'"
                )
        return True

    generation.window.run_generate_hook = hook
    generation.run()
    generation.assert_previous_outputs()
    assert generation.store.get_generation_count() == 2
    generation.assert_publication_error("generation count changed from 1 to 2")
    assert generation.hooks == ["pre"]
    generation.assert_released()


def test_success_commits_once_and_releases_writer_before_post_hook(
    generation: Generation,
) -> None:
    """The post hook observes published files, committed count and no writer lock."""

    def hook(stage: str, env: dict[str, str], allow_continue: bool) -> bool:
        """Inspect final artifacts and reserve a writer from a separate connection."""
        generation.hooks.append(stage)
        expected = "1" if stage == "pre" else "2"
        assert env["JLCPCB_GENERATION_COUNT"] == expected
        assert env["JLCPCB_VARIANT"] == "A"
        if stage == "post":
            generation.assert_generated_outputs()
            with closing(sqlite3.connect(generation.store.dbfile, timeout=0)) as writer:
                writer.execute("BEGIN IMMEDIATE")
                assert writer.execute(
                    "SELECT value FROM metadata WHERE key = 'generation_count'"
                ).fetchone() == ("2",)
                writer.rollback()
        return True

    generation.window.run_generate_hook = hook
    generation.run()
    generation.wx.MessageBox.assert_not_called()
    generation.assert_generated_outputs()
    assert generation.store.get_generation_count() == 2
    assert generation.hooks == ["pre", "post"]
    generation.assert_released()


def test_post_hook_failure_warns_after_committing_outputs(
    generation: Generation, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use the actual hook UI handler to distinguish post-hook and export errors."""

    def configured_hook(**kwargs: Any) -> Any:
        """Stand in for the subprocess result only; preserve the hook UI behavior."""
        stage = kwargs["stage"]
        generation.hooks.append(stage)
        return SimpleNamespace(
            command=["example-hook"],
            succeeded=stage == "pre",
            returncode=7,
            stdout="",
            stderr="post-hook example failed",
            error_message=None,
        )

    monkeypatch.setattr(generation.module, "run_configured_hook", configured_hook)
    generation.window.run_generate_hook = (
        generation.module.JLCPCBTools.run_generate_hook.__get__(generation.window)
    )
    generation.run()
    generation.wx.MessageBox.assert_called_once()
    message, title = generation.wx.MessageBox.call_args.args
    assert "post-generate hook failed after generation completed" in message
    assert title == "Post-generate hook failed"
    assert (
        generation.wx.MessageBox.call_args.kwargs["style"] == generation.wx.ICON_WARNING
    )
    generation.window.logger.exception.assert_not_called()
    generation.assert_generated_outputs()
    assert generation.store.get_generation_count() == 2
    assert generation.hooks == ["pre", "post"]
    generation.assert_released()


@pytest.mark.parametrize("matching_review", [False, True])
def test_impedance_reports_use_reviewed_output_variant_before_publication(
    generation: Generation,
    monkeypatch: pytest.MonkeyPatch,
    matching_review: bool,
) -> None:
    """Only a reviewed output clone can accompany the variant's real ZIP and CSVs."""
    plan = _reviewed_plan(generation.module)
    report_board = object()
    snapshot = plan.snapshot
    if not matching_review:
        snapshot = replace(snapshot, context_digest="different output variant text")
    generation.window._impedance = SimpleNamespace(
        preflight=MagicMock(return_value=plan),
        verify_current=MagicMock(),
        verify_disabled=MagicMock(),
    )
    monkeypatch.setattr(
        generation.module, "ArchiveEntry", generation.archive.ArchiveEntry
    )
    monkeypatch.setattr(
        generation.exporter,
        "get_report_board",
        MagicMock(return_value=report_board),
        raising=False,
    )
    read_snapshot = MagicMock(return_value=snapshot)
    monkeypatch.setattr(
        generation.module,
        "snapshot_impedance_board",
        read_snapshot,
        raising=False,
    )
    rendered_boards = []
    scratch_paths = []

    def export(_plan: Any, board: Any, _pcbnew: Any, scratch: Path) -> Any:
        """Write real report members while observing the selected native boundary."""
        rendered_boards.append(board)
        scratch_paths.append(scratch)
        return _reports(scratch)

    monkeypatch.setattr(generation.module, "export_impedance_reports", export)
    generation.run()

    assert all(not path.exists() for path in scratch_paths)
    if matching_review:
        generation.wx.MessageBox.assert_not_called()
        assert rendered_boards == [report_board]
        generation.assert_generated_outputs()
        with ZipFile(generation.paths["gerber_zip"]) as archive:
            assert archive.read("Required_impedance_control.xlsx") == b"workbook"
            assert (
                archive.read("Required_impedance_control.html")
                == b"<html>report</html>"
            )
        assert generation.store.get_generation_count() == 2
        assert generation.hooks == ["pre", "post"]
    else:
        generation.wx.MessageBox.assert_called_once()
        message = generation.wx.MessageBox.call_args.args[0]
        assert "Validating controlled-impedance output variant" in message
        assert rendered_boards == []
        generation.assert_previous_outputs()
        assert generation.store.get_generation_count() == 1
        assert generation.hooks == ["pre"]
    read_snapshot.assert_called_once_with(
        report_board,
        generation.window.pcbnew,
        netclass_source=generation.window.pcbnew.GetBoard(),
    )
    generation.assert_released()
