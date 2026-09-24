"""Exercise draft editing and explicit review through real native wx controls."""

from base64 import b64decode
from collections.abc import Callable
from dataclasses import replace
import importlib
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from .native_wx_support import isolated_native, pump, run_native, wait_until
from .test_impedance_draft_dialog import _board, _intent
from .test_impedance_spec_width_hint import _stackup

pytestmark = [
    pytest.mark.native_wx,
    pytest.mark.skipif(
        os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
        reason="native wx checks require an enabled desktop session",
    ),
]

_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _api(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Import the production feature without importing the KiCad plugin entrypoint."""
    name = "_impedance_settings_native"
    package = ModuleType(name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "impedance")]
    monkeypatch.setitem(sys.modules, name, package)
    return SimpleNamespace(
        model=importlib.import_module(f"{name}.model"),
        dialog=importlib.import_module(f"{name}.dialog"),
        repository=importlib.import_module(f"{name}.repository"),
        database=importlib.import_module(f"{name}.database"),
    )


def _click(wx: Any, button: Any) -> None:
    """Dispatch a button command through its actual bound native event handler."""
    event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
    event.SetEventObject(button)
    assert button.GetEventHandler().ProcessEvent(event)


def _select(wx: Any, control: Any, index: int) -> None:
    """Deliver a native choice event after changing its actual selected item."""
    control.SetSelection(index)
    event = wx.CommandEvent(wx.EVT_CHOICE.typeId, control.GetId())
    event.SetInt(index)
    event.SetEventObject(control)
    assert control.GetEventHandler().ProcessEvent(event)


def _modal(wx: Any, dialog: Any, drive: Callable[[], None]) -> int:
    """Drive a true modal window and propagate callback failures after its loop exits."""
    errors: list[BaseException] = []

    def interact() -> None:
        try:
            drive()
        except BaseException as error:
            errors.append(error)
        finally:
            if dialog and dialog.IsModal():
                dialog.EndModal(wx.ID_CANCEL)

    timer = wx.CallLater(50, interact)
    try:
        result = dialog.ShowModal()
    finally:
        timer.Stop()
    if errors:
        raise errors[0]
    return result


@pytest.mark.parametrize("unfinished", ["spacing", "target", "references", "none"])
def test_native_apply_settings_accepts_unreviewed_drafts(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    unfinished: str,
) -> None:
    """Applying settings accepts blanks and complete fields without creating approval."""
    if isolated_native(request, "KICAD_IMPEDANCE_SETTINGS_CHILD"):
        return
    api = _api(monkeypatch)

    def exercise(frame: Any, wx: Any) -> None:
        snapshot = _board(api)
        spec = replace(
            _intent(api).specifications[0],
            kind="differential",
            target_ohms="100",
            layer_settings=(api.model.LayerSettings("F.Cu", ("B.Cu",), 200_000),),
        )
        dialog = api.dialog.SpecificationDialog(frame, snapshot, specification=spec)
        dialog._draft_calc_timer.Stop()

        def apply() -> None:
            if unfinished == "references":
                for index in range(dialog.references.GetCount()):
                    dialog.references.Check(index, False)
                event = wx.CommandEvent(wx.EVT_CHECKLISTBOX.typeId)
                event.SetEventObject(dialog.references)
                dialog.references.GetEventHandler().ProcessEvent(event)
            elif unfinished != "none":
                getattr(dialog, unfinished).SetValue("")
            pump(wx)
            _click(wx, dialog.FindWindow(wx.ID_OK))

        result = _modal(wx, dialog, apply)
        assert result == wx.ID_OK, (
            "Apply settings must accept unreviewed settings without requiring "
            f"capture review: {dialog.validation_message.GetLabel()}"
        )
        accepted = dialog.specification
        assert accepted is not None
        assert accepted.target_ohms == ("" if unfinished == "target" else "100")
        settings = accepted.layer_settings[0]
        assert settings.spacing_nm == (None if unfinished == "spacing" else 200_000)
        assert settings.reference_layers == (
            () if unfinished == "references" else ("B.Cu",)
        )
        assert dialog.review_tracking.images == ()
        assert dialog.review_tracking.layers == ()
        dialog.Destroy()

    run_native(exercise)


def test_native_draft_edit_save_database_reopen_and_cancel(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    """A true nested edit can save unfinished intent, reopen, and cancel safely."""
    if isolated_native(request, "KICAD_IMPEDANCE_SETTINGS_PERSIST_CHILD"):
        return
    api = _api(monkeypatch)
    source = tmp_path / "fixture.kicad_pcb"
    source.write_text("isolated database identity fixture", encoding="utf-8")
    database = api.database.ImpedanceDatabase(tmp_path / "project.db")
    board_id = database.resolve_board(source)
    repository = api.repository.ImpedanceRepository(database, board_id)
    revision = [repository.save(_intent(api), 0)]
    service = importlib.import_module(f"{api.dialog.__package__}.service")
    capture = service.CapturedImage.from_bytes(_PNG)
    snapshot = _board(api)

    def save(config: Any) -> None:
        revision[0] = repository.save(config, revision[0])

    def exercise(frame: Any, wx: Any) -> None:
        def open_main(config: Any) -> Any:
            return api.dialog.ImpedanceDialog(
                frame,
                config,
                snapshot,
                lambda *_args, **_kwargs: capture,
                lambda: snapshot,
                fetch_stackup_catalog=lambda *_args, **_kwargs: (),
                save_config=save,
            )

        original, _revision = repository.load()
        main = open_main(original)

        def edit_and_save() -> None:
            errors: list[BaseException] = []

            def apply_child() -> None:
                children = [
                    window
                    for window in wx.GetTopLevelWindows()
                    if isinstance(window, api.dialog.SpecificationDialog)
                    and window.IsModal()
                ]
                try:
                    assert len(children) == 1
                    child = children[0]
                    child.label.SetValue("Partly configured RF")
                    child.target.SetValue("")
                    _click(wx, child.FindWindow(wx.ID_OK))
                    assert not child.IsModal(), child.validation_message.GetLabel()
                except BaseException as error:
                    errors.append(error)
                finally:
                    for child in children:
                        if child and child.IsModal():
                            child.EndModal(wx.ID_CANCEL)

            main.specifications.SelectRow(0)
            timer = wx.CallLater(50, apply_child)
            try:
                _click(wx, main._selection_buttons[0])
            finally:
                timer.Stop()
            if errors:
                raise errors[0]
            assert main.session.config.specifications[0].target_ohms == ""
            assert repository.load()[0] == original
            assert main.FindWindow(wx.ID_OK).GetLabel() == "Save draft and close"
            _click(wx, main.FindWindow(wx.ID_OK))

        assert _modal(wx, main, edit_and_save) == wx.ID_OK
        accepted = main.config
        main.Destroy()
        restored, saved_revision = api.repository.ImpedanceRepository(
            api.database.ImpedanceDatabase(database.path), board_id
        ).load()
        assert saved_revision == revision[0]
        assert restored == accepted
        assert restored.specifications[0].target_ohms == ""
        assert restored.specifications[0].label == "Partly configured RF"
        assert restored.reviewed_digest == ""
        assert restored.review_tracking.layers == ()
        before_cancel = database.path.read_bytes()
        reopened = open_main(restored)

        def cancel_reopened() -> None:
            assert reopened.session.config == restored
            _click(wx, reopened.FindWindow(wx.ID_CANCEL))

        assert _modal(wx, reopened, cancel_reopened) == wx.ID_CANCEL
        reopened.Destroy()
        assert database.path.read_bytes() == before_cancel

    run_native(exercise)


def test_native_approve_image_reviews_images_before_advancing(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Explicit approval presents pending images and advances only after review."""
    if isolated_native(request, "KICAD_IMPEDANCE_SETTINGS_REVIEW_CHILD"):
        return
    api = _api(monkeypatch)
    service = importlib.import_module(f"{api.dialog.__package__}.service")
    capture = service.CapturedImage.from_bytes(_PNG)

    def exercise(frame: Any, wx: Any) -> None:
        original = _board(api)
        snapshot = replace(
            original,
            traces=original.traces
            + tuple(
                replace(trace, trace_id=trace.trace_id + "-bottom", layer="B.Cu")
                for trace in original.traces
                if trace.layer == "F.Cu"
            ),
        )
        spec = replace(
            _intent(api).specifications[0],
            layer_settings=(
                api.model.LayerSettings("F.Cu", ("B.Cu",)),
                api.model.LayerSettings("B.Cu", ("F.Cu",)),
            ),
        )
        dialog = api.dialog.SpecificationDialog(
            frame,
            snapshot,
            specification=spec,
            preview=lambda *_args, **_kwargs: capture,
        )

        def approve_then_apply() -> None:
            assert dialog._active_layer == "F.Cu"
            assert len(dialog.preview_pane.sections) == 2
            wait_until(wx, lambda: dialog.preview_pane.displayed)
            _click(wx, dialog.approve_layer)
            assert dialog._active_layer == "F.Cu"
            assert not dialog._layer_drafts["F.Cu"].confirmed
            wait_until(wx, lambda: dialog.preview_pane.displayed)
            _click(wx, dialog.approve_layer)
            assert dialog._layer_drafts["F.Cu"].confirmed
            assert dialog._active_layer == "B.Cu"
            assert not dialog._layer_drafts["B.Cu"].confirmed
            assert len(dialog.review_tracking.layers) == 1
            assert len(dialog.review_tracking.layers[0].captures) == 2
            assert dialog.review_tracking.layers[0].approved_at_utc
            _click(wx, dialog.FindWindow(wx.ID_OK))

        assert _modal(wx, dialog, approve_then_apply) == wx.ID_OK
        assert dialog.specification is not None
        assert len(dialog.review_tracking.layers) == 1
        dialog.Destroy()

    run_native(exercise)


def test_native_linked_edits_units_and_override_reopen(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Native synchronous text events preserve exact linked dimensions and overrides."""
    if isolated_native(request, "KICAD_IMPEDANCE_SETTINGS_LINK_CHILD"):
        return
    api = _api(monkeypatch)
    stackup_model = importlib.import_module(f"{api.dialog.__package__}.stackup_model")
    original = _board(api)
    snapshot = replace(
        original,
        traces=original.traces
        + tuple(
            replace(trace, trace_id=trace.trace_id + "-bottom", layer="B.Cu")
            for trace in original.traces
            if trace.layer == "F.Cu"
        ),
    )
    stackup = _stackup(api)

    def calculate(
        construction: Any, specification: Any, layer: str, **_options: Any
    ) -> Any:
        """Keep this native settings test independent of supplier requests."""
        return stackup_model.WidthResult(
            spec_id=specification.spec_id,
            layer=layer,
            input_digest=stackup_model.calculation_fingerprint(
                construction, specification, layer
            ),
            status="unsupported",
            calculated_at_utc=api.dialog.tracking.utc_now(),
            message="Deterministic settings fixture; supplier calculation excluded.",
        )

    def exercise(frame: Any, wx: Any) -> None:
        dialog = api.dialog.SpecificationDialog(
            frame, snapshot, stackup=stackup, calculate_width=calculate
        )

        def edit() -> None:
            _select(wx, dialog.net_class, dialog._net_class_names.index("RF") + 1)
            coplanar = next(
                index
                for index, (kind, _label) in enumerate(api.dialog.KIND_CHOICES)
                if kind == "differential_coplanar"
            )
            _select(wx, dialog.kind, coplanar)
            dialog.spacing.SetValue("0.200001")
            dialog.ground_gap.SetValue("0.100001")
            _select(wx, dialog.layer, dialog._layer_names.index("B.Cu"))
            assert dialog.spacing.GetValue() == "0.200001"
            assert dialog.ground_gap.GetValue() == "0.100001"
            assert dialog.reference_selection.selected == ("F.Cu",)
            assert "F.Cu" in dialog.link_hint.GetLabel()
            _select(wx, dialog.units, 1)
            _select(wx, dialog.layer, dialog._layer_names.index("F.Cu"))
            dialog.spacing.SetValue("0.225003")
            _select(wx, dialog.layer, dialog._layer_names.index("B.Cu"))
            assert dialog.units.GetStringSelection() == "mil"
            assert dialog._read_dimension(dialog.spacing.GetValue(), "mil") == 225_003
            dialog.independent_layer.SetValue(True)
            checkbox = wx.CommandEvent(
                wx.EVT_CHECKBOX.typeId, dialog.independent_layer.GetId()
            )
            checkbox.SetEventObject(dialog.independent_layer)
            assert dialog.independent_layer.GetEventHandler().ProcessEvent(checkbox)
            _select(wx, dialog.units, 0)
            dialog.spacing.SetValue("0.250007")
            _select(wx, dialog.layer, dialog._layer_names.index("F.Cu"))
            assert dialog.spacing.GetValue() == "0.225003"
            _click(wx, dialog.FindWindow(wx.ID_OK))

        assert _modal(wx, dialog, edit) == wx.ID_OK
        accepted = dialog.specification
        assert accepted is not None
        assert accepted.independent_layers == ("B.Cu",)
        assert tuple(setting.spacing_nm for setting in accepted.layer_settings) == (
            225_003,
            250_007,
        )
        assert dialog.review_tracking.layers == ()
        dialog.Destroy()
        reopened = api.dialog.SpecificationDialog(
            frame,
            snapshot,
            specification=accepted,
            stackup=stackup,
            calculate_width=calculate,
        )

        def verify_reopen() -> None:
            _select(wx, reopened.layer, reopened._layer_names.index("B.Cu"))
            assert reopened.independent_layer.GetValue()
            assert (
                reopened._read_dimension(reopened.spacing.GetValue(), "mm") == 250_007
            )
            _click(wx, reopened.FindWindow(wx.ID_CANCEL))

        assert _modal(wx, reopened, verify_reopen) == wx.ID_CANCEL
        reopened.Destroy()

    run_native(exercise)


@pytest.mark.parametrize("removed_from_stack", [False, True])
def test_native_unused_saved_signal_layer_does_not_block_apply(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    removed_from_stack: bool,
) -> None:
    """The reported stale In3.Cu entry cannot trap real native Apply on B.Cu."""
    if isolated_native(request, "KICAD_IMPEDANCE_SETTINGS_UNUSED_CHILD"):
        return
    api = _api(monkeypatch)
    original = _board(api)
    snapshot = replace(
        original,
        layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
        if removed_from_stack
        else ("F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"),
        traces=original.traces
        + tuple(
            replace(trace, trace_id=trace.trace_id + "-bottom", layer="B.Cu")
            for trace in original.traces
            if trace.layer == "F.Cu"
        ),
    )
    spec = replace(
        _intent(api).specifications[0],
        layer_settings=(
            api.model.LayerSettings("F.Cu", ("In1.Cu",)),
            api.model.LayerSettings("In3.Cu", ("In2.Cu", "B.Cu")),
            api.model.LayerSettings("B.Cu", ("In2.Cu",)),
        ),
    )

    def exercise(frame: Any, wx: Any) -> None:
        dialog = api.dialog.SpecificationDialog(frame, snapshot, specification=spec)

        def edit() -> None:
            assert dialog._layer_names == ["F.Cu", "B.Cu"]
            _select(wx, dialog.kind, 1)
            dialog.target.SetValue("90")
            for layer, spacing in (("F.Cu", "0.200001"), ("B.Cu", "0.300003")):
                _select(wx, dialog.layer, dialog._layer_names.index(layer))
                dialog.spacing.SetValue(spacing)
            _click(wx, dialog.FindWindow(wx.ID_OK))

        assert _modal(wx, dialog, edit) == wx.ID_OK
        assert tuple(item.layer for item in dialog.specification.layer_settings) == (
            "F.Cu",
            "B.Cu",
        )
        assert tuple(
            item.spacing_nm for item in dialog.specification.layer_settings
        ) == (200_001, 300_003)
        assert not dialog.review_tracking.layers
        assert len(spec.layer_settings) == 3
        dialog.Destroy()

    run_native(exercise)
