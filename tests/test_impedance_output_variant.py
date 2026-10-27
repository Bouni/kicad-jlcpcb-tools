"""Review the selected fabrication variant without switching the PCB editor."""

from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import replace
import importlib
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from zipfile import ZipFile

import pytest

from .test_impedance_controls import (
    PNG,
    MemoryDatabase,
    Store,
    _configuration,
    _record,
    _snapshot,
    harness,
)
from .test_impedance_text_context import Board, NativeText, module
from .test_mainwindow_impedance_flow import _window, runtime

__all__ = ["harness", "runtime"]


def _variant_controls(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, SimpleNamespace]:
    """Use real controls/storage callbacks with stateful native board boundaries."""
    state = SimpleNamespace(owners=[], sources=[], version="initial", selection="A")

    def board(name: str) -> Any:
        text = NativeText("variant-label", "${VARIANT}")
        text.context = {"VARIANT": name}
        text.serialized = '(gr_text "${VARIANT}" (uuid "variant-label"))'
        result = Board((text,))
        result.variant = name
        result.GetFileName = lambda: harness.board_path
        result.GetCurrentVariant = lambda: result.variant
        return result

    live = board("B")
    harness.board = live
    harness.pcbnew.GetBoard = lambda: live
    session = SimpleNamespace(output_variant="A")
    harness.parent._variant_controller = SimpleNamespace(session=session)
    harness.parent._legacy_board = lambda: live
    factory = module()
    adapter = importlib.import_module(
        f"{harness.integration.__package__}.pcbnew_adapter"
    )
    model_snapshot = _snapshot(harness)

    def snapshot(current: Any, _pcbnew: Any, *, netclass_source: Any = None) -> Any:
        assert netclass_source is None or netclass_source is live
        state.sources.append((current, netclass_source))
        return replace(
            model_snapshot,
            context_digest=adapter._context_digest(current, factory, ())
            + state.version,
        )

    monkeypatch.setattr(adapter, "snapshot_board", snapshot)

    class Owner:
        def __init__(self, name: str) -> None:
            self.board = board(name)
            self.closed = False
            self.close_calls = 0
            state.owners.append(self)

        def close(self) -> None:
            self.close_calls += 1
            assert all(
                renderer.closed
                for renderer in harness.renderers
                if renderer.board is self.board
            )
            self.closed = True

        def __enter__(self) -> Any:
            return self.board

        def __exit__(self, *_args: Any) -> None:
            self.close()

    harness.parent.fabrication = SimpleNamespace(copy_variant_board=Owner)
    controls = harness.integration.ImpedanceControls(harness.parent, harness.toolbar)
    harness.database = MemoryDatabase(_record(_configuration(harness, enabled=True)))
    harness.store = Store(Path(harness.board_path).parent / "jlcpcb" / "project.db")
    database_factory = MagicMock(return_value=harness.database)
    database_factory.validate_board_path = (
        harness.integration.ImpedanceDatabase.validate_board_path
    )
    monkeypatch.setattr(harness.integration, "ImpedanceDatabase", database_factory)
    controls.attach_store(harness.store)
    state.session, state.live = session, live
    return controls, state


def _approve(harness: SimpleNamespace, dialog: Any) -> None:
    """Approve the actual reviewed immutable rows, then save through the callback."""
    analysis = harness.matching.analyze(dialog.config, dialog.snapshot)
    for section in analysis.sections:
        dialog.guarded_preview(section)
    dialog.config = replace(
        dialog.config,
        reviewed_digest=analysis.digest,
        included_section_ids=tuple(section.section_id for section in analysis.sections),
    )


def test_configure_approves_output_a_with_editor_b_and_reopens_current(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    controls, state = _variant_controls(harness, monkeypatch)
    before = deepcopy(state.live.drawings[0].__dict__)
    source_bytes = Path(harness.board_path).read_bytes()
    assert not state.owners, "Read-only startup must not clone or register a board"
    harness.modal_result = harness.wx.ID_OK
    harness.modal_action = lambda dialog: _approve(harness, dialog)
    controls.on_configure(None)
    assert not harness.errors
    assert harness.database.saves
    assert harness.renderers[0].board.variant == "A"
    assert len(state.owners) == 1, (
        "Unchanged preview checks reuse the owned output board"
    )
    assert harness.dialogs[0].snapshot == controls.preflight(2).snapshot
    assert all(owner.closed for owner in state.owners)
    assert state.live.variant == "B"
    assert state.live.drawings[0].__dict__ == before
    assert Path(harness.board_path).read_bytes() == source_bytes
    assert any(source is state.live for _current, source in state.sources)
    assert all(renderer.closed for renderer in harness.renderers)
    harness.modal_result = harness.wx.ID_CANCEL
    harness.modal_action = None
    saved = deepcopy(harness.database.record)
    controls.on_configure(None)
    assert (
        harness.dialogs[-1].config.reviewed_digest
        == saved["payload"]["reviewed_digest"]
    )
    assert harness.database.record == saved
    assert all(owner.closed for owner in state.owners)


@pytest.mark.parametrize("failure", ["replaced", "unreliable"])
def test_variant_review_rejects_invalid_live_board_before_copying(
    harness: SimpleNamespace,
    runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """The window's board lifetime check must precede any output review copy."""
    controls, state = _variant_controls(harness, monkeypatch)
    parent = harness.parent
    parent._legacy_board = MethodType(runtime.JLCPCBTools._legacy_board, parent)
    parent._variant_mode = True
    parent._board_identity = runtime.board_identity(state.live)
    if failure == "replaced":
        prior_board = SimpleNamespace(GetFileName=state.live.GetFileName)
        parent._board_identity = runtime.board_identity(prior_board)
    else:
        parent._board_unreliable = True

    controls.on_configure(None)

    assert harness.errors
    assert not harness.dialogs
    assert not state.owners
    assert not harness.database.saves


@pytest.mark.parametrize("change", ["selection", "source"])
def test_changed_review_context_cannot_save_approval(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    controls, state = _variant_controls(harness, monkeypatch)
    original = deepcopy(harness.database.record)
    harness.modal_result = harness.wx.ID_OK

    def review_then_change(dialog: Any) -> None:
        _approve(harness, dialog)
        if change == "selection":
            state.session.output_variant = "B"
        else:
            state.version = "edited-geometry"
        dialog.verify_snapshot(dialog.snapshot)

    harness.modal_action = review_then_change
    controls.on_configure(None)
    assert harness.database.record == original
    assert not harness.database.saves
    assert harness.errors
    assert all(owner.closed for owner in state.owners)
    assert state.live.variant == "B"


def test_saved_approval_rejects_selection_change_even_without_variant_text(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    controls, state = _variant_controls(harness, monkeypatch)
    harness.modal_result = harness.wx.ID_OK
    harness.modal_action = lambda dialog: _approve(harness, dialog)
    controls.on_configure(None)
    plan = controls.preflight(2)
    assert plan is not None
    # Both copies now have no rendered text. Selection still belongs to approval.
    state.live.drawings = ()
    original_factory = harness.parent.fabrication.copy_variant_board

    def without_text(name: str) -> Any:
        owner = original_factory(name)
        owner.board.drawings = ()
        return owner

    harness.parent.fabrication.copy_variant_board = without_text
    harness.modal_action = lambda dialog: _approve(harness, dialog)
    controls.on_configure(None)
    plan = controls.preflight(2)
    assert plan is not None
    state.session.output_variant = ""
    with pytest.raises(harness.model.ValidationError):
        controls.verify_current(plan, 2)
    assert all(owner.closed for owner in state.owners)


def test_review_cache_releases_renderer_before_replacing_variant_board(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    controls, state = _variant_controls(harness, monkeypatch)

    def change_and_refresh(dialog: Any) -> None:
        section = harness.matching.analyze(dialog.config, dialog.snapshot).sections[0]
        assert dialog.appearance_context() == (harness.palette, "A")
        dialog.guarded_preview(section)
        first_owner = state.owners[-1]
        state.session.output_variant = "B"
        assert dialog.appearance_context() == (harness.palette, "B")
        current = dialog.refresh_snapshot()
        assert current != dialog.snapshot
        assert first_owner.closed
        assert harness.renderers[0].closed
        dialog.guarded_preview(section, current)
        assert harness.renderers[-1].board.variant == "B"

    harness.modal_action = change_and_refresh
    controls.on_configure(None)
    assert not harness.errors
    assert all(owner.closed for owner in state.owners)
    assert not harness.database.saves


@pytest.mark.parametrize(
    "failure", ["none", "selection", "source", "report", "publish"]
)
def test_selected_variant_generate_publishes_current_reports_or_preserves_prior_files(
    harness: SimpleNamespace,
    runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    """Real controls and Generate consume A approval while the live editor stays on B."""
    controls, state = _variant_controls(harness, monkeypatch)
    harness.modal_result = harness.wx.ID_OK
    harness.modal_action = lambda dialog: _approve(harness, dialog)
    controls.on_configure(None)
    assert not harness.errors
    source_bytes = Path(harness.board_path).read_bytes()
    source_text = deepcopy(state.live.drawings[0].__dict__)
    window, _steps = _window(runtime)
    copy_board = harness.parent.fabrication.copy_variant_board
    window.pcbnew = harness.pcbnew
    window._legacy_board = lambda: state.live
    window._impedance = controls
    window.fabrication.copy_variant_board = copy_board
    window._variant_controller = harness.parent._variant_controller
    window._variant_controller.begin_generation = MagicMock()
    window._variant_controller.end_generation = MagicMock()
    controls.parent = window
    archive = importlib.import_module(runtime.ArchiveEntry.__module__)
    service = importlib.import_module(harness.integration.prepare.__module__)
    staged = tmp_path / "staged"
    public = tmp_path / "public"
    staged.mkdir()
    public.mkdir()
    paths = {
        key: public / name
        for key, name in (("zip", "board.zip"), ("cpl", "cpl.csv"), ("bom", "bom.csv"))
    }
    for path in paths.values():
        path.write_bytes(b"previous public artifact")
    source = tmp_path / "copper.gbr"
    source.write_bytes(b"G04 current Gerber*\nM02*\n")
    rendered: list[str] = []
    counter = [0]

    class Renderer:
        def __init__(self, board: Any, _pcbnew: Any) -> None:
            assert board.variant == "A"
            self.closed = False

        def render(self, _section: Any, destination: Path, **_size: Any) -> Path:
            assert not self.closed
            rendered.append("A")
            destination.write_bytes(PNG)
            if failure == "selection":
                state.session.output_variant = "B"
            elif failure == "source":
                state.version = "edited-source"
            elif failure == "report":
                raise RuntimeError("report failed")
            return destination

        def close(self) -> None:
            self.closed = True

    def export(
        plan: Any, board: Any, pcbnew: Any, scratch: Path, **options: Any
    ) -> Any:
        """Exercise the real live-provenance/cache seam with the native output fake."""
        assert options["capture_identity"] == (
            str(Path(harness.board_path).resolve()),
            "A",
        )
        assert options["source_context"]() == plan.snapshot
        # These scenarios inject changes at the cold renderer boundary. Warm
        # review/export reuse has separate coverage and must not bypass this one.
        options["capture_cache"] = type(options["capture_cache"])()
        return service.export_reports(plan, board, pcbnew, scratch, Renderer, **options)

    def zip_files(entries: Any = ()) -> None:
        archive.build_archive(
            staged / paths["zip"].name,
            (source,) + tuple(entries),
        )

    def stage_csv(key: str) -> None:
        (staged / paths[key].name).write_bytes(b"new prepared CSV\n")

    @contextmanager
    def publication() -> Iterator[None]:
        with archive.artifact_publication(
            tuple((staged / path.name, path) for path in paths.values())
        ):
            if failure == "publish":
                raise OSError("publication failed")
            yield

    @contextmanager
    def generation_counter(*, expected_count: int) -> Iterator[int]:
        assert expected_count == counter[0]
        yield counter[0] + 1
        counter[0] += 1

    monkeypatch.setattr(runtime, "export_impedance_reports", export)
    monkeypatch.setattr(
        runtime, "ImpedanceValidationError", harness.model.ValidationError
    )
    window.fabrication.zip_gerber_excellon.side_effect = zip_files
    window.fabrication.write_cpl.side_effect = lambda _rows: stage_csv("cpl")
    window.fabrication.generate_bom.side_effect = lambda: stage_csv("bom")
    window.fabrication.generation_publication = publication
    window.store.generation_publication_lock = nullcontext
    window.store.generation_counter_transaction = generation_counter
    window.store.get_generation_count.side_effect = lambda: counter[0]
    with copy_board("A") as output_board:
        window.fabrication.get_report_board = lambda: output_board
        runtime.JLCPCBTools.generate_fabrication_data(window)
    assert state.live.variant == "B"
    assert Path(harness.board_path).read_bytes() == source_bytes
    assert state.live.drawings[0].__dict__ == source_text
    assert all(owner.closed for owner in state.owners)
    if failure != "none":
        assert counter == [0]
        assert {path.read_bytes() for path in paths.values()} == {
            b"previous public artifact"
        }
        runtime.wx.MessageBox.assert_called()
        return
    assert counter == [1]
    assert rendered == ["A"]
    runtime.wx.MessageBox.assert_not_called()
    with ZipFile(paths["zip"]) as result:
        assert set(result.namelist()) == {
            "copper.gbr",
            "Required_impedance_control.xlsx",
            "Required_impedance_control.html",
        }
        assert result.read("copper.gbr") == source.read_bytes()
    controls._save(replace(controls.config, enabled=False))
    runtime.JLCPCBTools.generate_fabrication_data(window)
    assert counter == [2]
    with ZipFile(paths["zip"]) as result:
        assert result.namelist() == ["copper.gbr"]


def test_renderer_close_failure_still_closes_output_owner_exactly_once(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    controls, state = _variant_controls(harness, monkeypatch)

    def capture(dialog: Any) -> None:
        section = harness.matching.analyze(dialog.config, dialog.snapshot).sections[0]
        dialog.guarded_preview(section)

    def failed_close() -> None:
        raise RuntimeError("native renderer cleanup failed")

    harness.modal_action = capture
    harness.close_action = failed_close
    controls.on_configure(None)
    assert harness.errors == ["native renderer cleanup failed"]
    assert len(state.owners) == 1
    assert state.owners[0].closed
    assert state.owners[0].close_calls == 1
    assert harness.renderers[0].closed
    assert not harness.database.saves


def test_variant_review_save_conflict_retains_edits_and_releases_owner(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    controls, state = _variant_controls(harness, monkeypatch)
    harness.modal_result = harness.wx.ID_OK

    def conflicting_save(dialog: Any) -> None:
        _approve(harness, dialog)
        harness.database.record["revision"] += 1

    def inspect_failed_save(dialog: Any) -> None:
        assert dialog.modal_open
        assert dialog.config.reviewed_digest
        assert dialog.config.included_section_ids
        assert not state.owners[0].closed

    harness.modal_action = conflicting_save
    harness.save_failure_action = inspect_failed_save
    controls.on_configure(None)
    assert harness.dialogs[0].save_errors == [
        "The configuration changed in another window."
    ]
    assert not harness.database.saves
    assert not harness.database.record["payload"]["reviewed_digest"]
    assert all(owner.closed and owner.close_calls == 1 for owner in state.owners)


@pytest.mark.parametrize("change", ["selection", "source"])
def test_variant_copy_changed_during_creation_is_not_adopted(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    controls, state = _variant_controls(harness, monkeypatch)
    original = harness.parent.fabrication.copy_variant_board

    def changing_copy(name: str) -> Any:
        owner = original(name)
        if change == "selection":
            state.session.output_variant = "B"
        else:
            state.version = "changed during native parse"
        return owner

    harness.parent.fabrication.copy_variant_board = changing_copy
    controls.on_configure(None)
    assert harness.errors
    assert not harness.dialogs
    assert not harness.database.saves
    assert all(owner.closed and owner.close_calls == 1 for owner in state.owners)


def test_source_change_during_capture_cannot_become_reviewed_image(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    controls, state = _variant_controls(harness, monkeypatch)
    harness.modal_result = harness.wx.ID_OK
    harness.modal_action = lambda dialog: _approve(harness, dialog)
    harness.render_action = lambda: setattr(state, "version", "changed while rendering")
    controls.on_configure(None)
    assert harness.errors
    assert not harness.database.saves
    assert all(renderer.closed for renderer in harness.renderers)
    assert all(owner.closed and owner.close_calls == 1 for owner in state.owners)
