"""Check smoke-result validation with synthetic archives, without native KiCad.

These tests establish what evidence the native workflow must produce. They do
not execute that workflow, plot fabrication data, or verify wx controls.
"""

from base64 import b64decode, b64encode
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import fields, replace
import importlib
from io import BytesIO
from pathlib import Path
import re
import sqlite3
import struct
import sys
from types import SimpleNamespace
from typing import Any, Optional
from zipfile import ZipFile
import zlib

import pytest

from scripts.native_impedance_workflow import check_generated_archive
from tests.wx_harness import load_siblings, wx_stubs

_WORKBOOK = "Required_impedance_control.xlsx"
_HTML = "Required_impedance_control.html"
_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
_GERBER = b"G04 Synthetic validation fixture*\n%FSLAX46Y46*%\n%MOMM*%\nM02*\n"
_DRILL = b"M48\nMETRIC\nT1C0.800\n%\nT1\nX1.0Y1.0\nM30\n"


def test_phase_measurements_include_failed_work_and_keep_nested_timings_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Perf-counter receipts cannot hide a failed or nested expensive operation."""
    from scripts import native_impedance_workflow as workflow

    now = iter((0.0, 1.0, 3.0, 5.0, 10.0, 14.0))
    monkeypatch.setattr(workflow.time, "perf_counter", lambda: next(now))
    phases: dict[str, Any] = {}
    with (
        pytest.raises(RuntimeError, match="failed phase"),
        workflow.measure_phase(phases, "all"),
        workflow.measure_phase(phases, "inner"),
    ):
        raise RuntimeError("failed phase")
    with workflow.measure_phase(phases, "inner"):
        pass
    assert phases == {
        "all": {"calls": 1, "seconds": 5.0},
        "inner": {"calls": 2, "seconds": 6.0},
    }


def test_call_timings_preserve_results_failures_and_restore_real_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Diagnostic observation must neither suppress errors nor retain patches."""
    from scripts import native_impedance_workflow as workflow

    events: list[str] = []

    def calculate(value: str, *, fail: bool = False) -> str:
        events.append(value)
        if fail:
            raise RuntimeError("native operation failed")
        return value.upper()

    owner = SimpleNamespace(calculate=calculate)
    now = iter((1.0, 3.0, 5.0, 8.0))
    monkeypatch.setattr(workflow.time, "perf_counter", lambda: next(now))
    phases: dict[str, Any] = {}
    with workflow.record_call_timings(phases, ((owner, "calculate", "native"),)):
        assert owner.calculate("success") == "SUCCESS"
        with pytest.raises(RuntimeError, match="native operation failed"):
            owner.calculate("failure", fail=True)
        assert owner.calculate.__wrapped__ is calculate
    assert owner.calculate is calculate
    assert events == ["success", "failure"]
    assert phases == {"native": {"calls": 2, "seconds": 5.0}}


@pytest.mark.parametrize("selected", [True, False])
def test_spacing_selection_skips_only_the_already_selected_entry(
    selected: bool,
) -> None:
    """Each actual spacing concern is still visited; queued paint stays mandatory."""
    from scripts.native_impedance_workflow import select_spacing_preview

    first, second = SimpleNamespace(view_id="one"), SimpleNamespace(view_id="two")
    selections: list[int] = []
    pane = SimpleNamespace(
        entries=(first, second),
        _index=0 if selected else 1,
        selected_entry=first if selected else second,
        select=selections.append,
    )
    select_spacing_preview(pane, first)
    assert selections == ([] if selected else [0])


def test_renderer_phase_receipts_copy_counts_without_duplicate_close_accounting() -> (
    None
):
    """Native renderer cleanup stays real; metrics cannot double-count disposal."""
    from scripts.native_impedance_workflow import record_renderer_timings

    class Renderer:
        def __init__(self) -> None:
            self.closed = 0
            self.render_timings = {"rasterize": {"calls": 3, "seconds": 1.25}}

        def close(self) -> None:
            self.closed += 1
            self.render_timings.clear()

    phases: dict[str, Any] = {}
    with record_renderer_timings(Renderer, phases):
        renderer = Renderer()
        renderer.close()
        renderer.close()
        assert renderer.closed == 2
        Renderer().close()
    assert phases == {"render.rasterize": {"calls": 6, "seconds": 2.5}}


def test_renderer_role_receipts_count_successful_actual_callbacks_without_replacing_them() -> (
    None
):
    """The native lane observes trace/composite/Original work rather than inventing it."""
    from collections import Counter

    from scripts.native_impedance_workflow import record_renderer_timings

    events: list[str] = []

    class Renderer:
        def render(self, value: Any) -> str:
            events.append(value.payload)
            return value.payload

        render_spacing = render
        render_spacing_original = render

        def close(self) -> None:
            events.append("native close")

    roles: Counter[str] = Counter()
    identities: dict[str, Counter[str]] = {}
    phases: dict[str, Any] = {}
    with record_renderer_timings(
        Renderer, phases, roles=roles, rendered_inputs=identities
    ):
        renderer = Renderer()
        assert (
            renderer.render(SimpleNamespace(section_id="row", payload="trace bytes"))
            == "trace bytes"
        )
        assert (
            renderer.render_spacing(
                SimpleNamespace(view_id="concern", payload="combined bytes")
            )
            == "combined bytes"
        )
        assert (
            renderer.render_spacing_original(
                SimpleNamespace(view_id="concern", payload="original bytes")
            )
            == "original bytes"
        )
        renderer.close()
    assert events == ["trace bytes", "combined bytes", "original bytes", "native close"]
    assert roles == {"trace": 1, "spacing": 1, "original": 1}
    assert identities == {
        "trace": {"row": 1},
        "spacing": {"concern": 1},
        "original": {"concern": 1},
    }
    assert all(phases["render_call." + role]["calls"] == 1 for role in roles)


@pytest.mark.parametrize("enabled", [False, True])
def test_diagnostic_profiler_profiles_only_first_actual_call(
    tmp_path: Path, enabled: bool
) -> None:
    """Opt-in statistics retain the actual operation and cannot profile every image."""
    import pstats

    from scripts.native_impedance_workflow import profile_first_call

    events: list[str] = []

    class Capture:
        def cold(self) -> str:
            events.append("actual capture")
            return "native bytes"

    destination = tmp_path / "diagnostic.pstats"
    with profile_first_call(Capture, "cold", destination, enabled=enabled):
        assert Capture().cold() == Capture().cold() == "native bytes"
    assert events == ["actual capture", "actual capture"]
    assert destination.exists() is enabled
    if enabled:
        measured = pstats.Stats(str(destination)).stats
        assert sum(value[1] for key, value in measured.items() if key[2] == "cold") == 1
        assert "diagnostic only" in destination.with_suffix(".txt").read_text()


def test_profile_dump_keeps_colliding_generated_dataclass_code_entries_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Equal filename/line/name keys cannot erase distinct constructor/hash work."""
    from dataclasses import make_dataclass
    import json
    from types import ModuleType

    from scripts.native_impedance_workflow import profile_native_operation

    module = ModuleType("impedance.generated_profile_fixture")
    first = make_dataclass(
        "First",
        [("value", int)],
        frozen=True,
        namespace={"__module__": module.__name__},
    )
    second = make_dataclass(
        "Second",
        [("value", int)],
        frozen=True,
        namespace={"__module__": module.__name__},
    )
    first.__module__ = second.__module__ = module.__name__
    module.First, module.Second = first, second
    monkeypatch.setitem(sys.modules, module.__name__, module)
    codes = (first.__init__.__code__, second.__init__.__code__)
    assert codes[0] is not codes[1]
    assert (codes[0].co_filename, codes[0].co_firstlineno, codes[0].co_name) == (
        codes[1].co_filename,
        codes[1].co_firstlineno,
        codes[1].co_name,
    )
    destination = tmp_path / "generated.pstats"
    with profile_native_operation(destination):
        left = first(1)
        right = second(2)
        second(3)
        hash(left)
        hash(right)
        hash(right)
    assert codes[0] is first.__init__.__code__
    assert codes[1] is second.__init__.__code__
    evidence = json.loads(destination.with_suffix(".getstats.json").read_text())
    methods = {
        label: entry
        for entry in evidence["entries"]
        for label in entry["qualified_names"]
    }
    one = methods[module.__name__ + ".First.__init__"]
    two = methods[module.__name__ + ".Second.__init__"]
    assert one["entry_id"] != two["entry_id"]
    assert (one["call_count"], two["call_count"]) == (1, 2)
    assert methods[module.__name__ + ".First.__hash__"]["call_count"] == 1
    assert methods[module.__name__ + ".Second.__hash__"]["call_count"] == 2
    assert evidence["getstats_entry_count"] == len(evidence["entries"])
    assert len({entry["entry_id"] for entry in evidence["entries"]}) == len(
        evidence["entries"]
    )
    assert evidence["generated_dataclass_totals"]["call_count"] >= 6
    assert all(
        "inline_seconds" in entry and "total_seconds" in entry for entry in (one, two)
    )
    summary = destination.with_suffix(".getstats.txt").read_text()
    assert "First.__init__" in summary and "Second.__init__" in summary


def test_failed_workflow_preserves_partial_real_phase_and_role_counters(
    tmp_path: Path,
) -> None:
    """A functional failure cannot discard the timing evidence needed to fix it."""
    import json

    from scripts.native_impedance_workflow import record_workflow_progress

    state = {"counts": {"spacing_explicit_approvals": 0}, "renderer_roles": {}}
    destination = tmp_path / "generate-performance.json"
    with (
        pytest.raises(RuntimeError, match="native cancellation failed"),
        record_workflow_progress(destination, lambda: state),
    ):
        state["counts"]["spacing_explicit_approvals"] = 130
        state["renderer_roles"]["trace"] = 32
        raise RuntimeError("native cancellation failed")
    recorded = json.loads(destination.read_text())
    assert recorded["status"] == "FAIL"
    assert recorded["error"] == "native cancellation failed"
    assert recorded["counts"]["spacing_explicit_approvals"] == 130
    assert recorded["renderer_roles"] == {"trace": 32}


def test_diagnostic_heartbeat_atomically_keeps_only_latest_complete_evidence(
    tmp_path: Path,
) -> None:
    """Live profiling retains bounded files and cannot destroy its last good snapshot."""
    import json

    from scripts.native_impedance_workflow import write_diagnostic_heartbeat

    destination = tmp_path / "generate-heartbeat.json"
    first = {"seconds": 10.0, "counts": {"spacing_explicit_approvals": 65}}
    write_diagnostic_heartbeat(destination, first, "approval pass 1")
    assert first == {"seconds": 10.0, "counts": {"spacing_explicit_approvals": 65}}
    assert (
        json.loads(destination.read_text())["latest_completed_phase"]
        == "approval pass 1"
    )
    second = {"seconds": 20.0, "counts": {"spacing_explicit_approvals": 130}}
    write_diagnostic_heartbeat(destination, second, "approval pass 2")
    recorded = json.loads(destination.read_text())
    assert recorded["counts"]["spacing_explicit_approvals"] == 130
    assert recorded["latest_completed_phase"] == "approval pass 2"
    assert recorded["diagnostic_only"] is True
    assert recorded["performance_acceptance_eligible"] is False
    assert list(tmp_path.iterdir()) == [destination]
    before = destination.read_bytes()
    with pytest.raises(TypeError):
        write_diagnostic_heartbeat(destination, {"invalid": object()}, "incomplete")
    assert destination.read_bytes() == before
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize("action_fails", [False, True])
def test_modal_driver_starts_on_actual_readiness_without_fixed_startup_delay(
    action_fails: bool,
) -> None:
    """Queued interaction waits for mapping and preserves pre-paint inspection."""
    from scripts.native_impedance_workflow import interact_modal

    pending: list[Callable[[], None]] = []
    events: list[str] = []
    delays: list[int] = []

    class Timer:
        def __init__(self, milliseconds: int, callback: Callable[[], None]) -> None:
            delays.append(milliseconds)
            self.active = True
            pending.append(lambda: callback() if self.active else None)

        def Stop(self) -> None:
            self.active = False

    class Dialog:
        modal = False
        shown = False

        def IsModal(self) -> bool:
            return self.modal

        def IsShownOnScreen(self) -> bool:
            return self.shown

        def EndModal(self, _result: int) -> None:
            self.modal = False
            events.append("closed")

        def ShowModal(self) -> int:
            self.modal = True
            # Dispatch once before the native window is mapped; this must not
            # call the action or replace before_show's unpainted inspection.
            pending.pop(0)()
            assert events == ["unpainted"]
            self.shown = True
            while self.modal:
                assert pending, "The modal readiness callback was lost"
                pending.pop(0)()
            return 2

    wx = SimpleNamespace(
        CallAfter=lambda callback: pending.append(callback),
        CallLater=Timer,
        ID_CANCEL=2,
    )
    dialog = Dialog()

    def before_show(current: Dialog) -> None:
        assert not current.modal and not current.shown
        events.append("unpainted")

    def action(current: Dialog) -> None:
        assert current.modal and current.shown
        events.append("explicit action")
        if action_fails:
            raise RuntimeError("native action failed")

    def run() -> None:
        with interact_modal(wx, Dialog, action, before_show=before_show) as visited:
            assert dialog.ShowModal() == 2
        assert visited == [dialog]

    if action_fails:
        with pytest.raises(RuntimeError, match="native action failed"):
            run()
    else:
        run()
    assert events == ["unpainted", "explicit action", "closed"]
    assert all(delay <= 10 for delay in delays)


def test_modal_driver_rejects_an_early_exit_and_disarms_queued_interaction() -> None:
    """A closed native window cannot become apparent coverage on a later event."""
    from scripts.native_impedance_workflow import interact_modal

    pending: list[Callable[[], None]] = []
    acted: list[Any] = []

    class Dialog:
        def ShowModal(self) -> int:
            return 2

    wx = SimpleNamespace(CallAfter=lambda callback: pending.append(callback))
    dialog = Dialog()
    with (
        pytest.raises(RuntimeError, match="mapped"),
        interact_modal(wx, Dialog, acted.append),
    ):
        assert dialog.ShowModal() == 2
    assert len(pending) == 1
    pending.pop()()
    assert not acted


@pytest.fixture
def saved_capture_clock(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Dispatch real wait-loop callbacks while cold image work advances virtual time."""
    from scripts import native_impedance_workflow as workflow

    clock = SimpleNamespace(now=0.0, timers=[], advance=lambda: None)

    class Timer:
        def __init__(self, _milliseconds: int, callback: Callable[[], None]) -> None:
            self.callback = callback
            self.active = True
            clock.timers.append(self)

        def Start(self, _milliseconds: int) -> None:
            self.active = True
            clock.timers.append(self)

        def Stop(self) -> None:
            self.active = False

    class EventLoop:
        def __init__(self) -> None:
            self.exited = False

        def ScheduleExit(self, _code: int) -> None:
            self.exited = True

        def Run(self) -> None:
            while not self.exited:
                assert clock.timers, "The wait loop lost its scheduled check"
                timer = clock.timers.pop(0)
                if timer.active:
                    clock.now += 0.6
                    clock.advance()
                    timer.callback()

    monkeypatch.setitem(
        sys.modules, "wx", SimpleNamespace(GUIEventLoop=EventLoop, CallLater=Timer)
    )
    monkeypatch.setattr(workflow, "time", SimpleNamespace(monotonic=lambda: clock.now))
    return clock


def _cold_saved_review(
    clock: SimpleNamespace, *, stalled: bool = False
) -> SimpleNamespace:
    """Keep historical consent separate from progressively verified current PNGs."""
    entries = tuple(f"image-{index}" for index in range(237))
    history = tuple((entry, "unchanged saved approval timestamp") for entry in entries)
    session = SimpleNamespace(
        spacing_entries=entries,
        spacing_capture_digests={},
        config=SimpleNamespace(review_tracking=history),
    )
    dialog = SimpleNamespace(
        _capture_prepare_sections=entries,
        _capture_restore_pending=True,
        session=session,
    )

    def advance() -> None:
        if not dialog._capture_prepare_sections:
            return
        if stalled and len(session.spacing_capture_digests) == 50:
            return
        entry, *remaining = dialog._capture_prepare_sections
        session.spacing_capture_digests[entry] = "fixture current PNG digest"
        dialog._capture_prepare_sections = tuple(remaining)
        dialog._capture_restore_pending = bool(remaining)

    clock.advance = advance
    return dialog


def test_cold_saved_image_restoration_can_finish_after_thirty_seconds(
    saved_capture_clock: SimpleNamespace,
) -> None:
    """Model 237 cold images taking 142s at the observed native idle pacing."""
    from scripts.native_impedance_workflow import wait_for_saved_capture_restore

    dialog = _cold_saved_review(saved_capture_clock)
    history = dialog.session.config.review_tracking
    wait_for_saved_capture_restore(object(), dialog)
    assert 140 < saved_capture_clock.now < 150
    assert not dialog._capture_restore_pending
    assert len(dialog.session.spacing_capture_digests) == 237
    assert dialog.session.config.review_tracking is history


def test_cold_saved_image_restoration_keeps_one_bounded_deadline_after_progress(
    saved_capture_clock: SimpleNamespace,
) -> None:
    """Partial progress cannot extend the functional allowance or fabricate consent."""
    from scripts.native_impedance_workflow import wait_for_saved_capture_restore

    dialog = _cold_saved_review(saved_capture_clock, stalled=True)
    history = dialog.session.config.review_tracking
    with pytest.raises(RuntimeError, match="Reopening did not verify"):
        wait_for_saved_capture_restore(object(), dialog)
    assert 300 <= saved_capture_clock.now < 301
    assert len(dialog.session.spacing_capture_digests) == 50
    assert dialog._capture_restore_pending
    assert dialog.session.config.review_tracking is history


def test_generic_native_transition_wait_remains_thirty_seconds(
    saved_capture_clock: SimpleNamespace,
) -> None:
    """Long bulk work must not loosen selected-image or interaction waits."""
    from scripts.native_impedance_workflow import wait_until

    dialog = _cold_saved_review(saved_capture_clock)
    with pytest.raises(RuntimeError, match="ordinary transition timed out"):
        wait_until(
            object(),
            lambda: not dialog._capture_restore_pending,
            "ordinary transition timed out",
        )
    assert 30 <= saved_capture_clock.now < 31
    assert dialog._capture_restore_pending


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "resized",
        "cropped",
        "stale_size",
        "small",
        "hidden_approve",
        "hidden_cancel",
        "forbidden_fit",
        "forbidden_original",
        "forbidden_issue",
        "forbidden_route",
        "presentation_crop",
    ],
)
def test_native_spacing_approval_uses_one_full_fit_paint_without_navigation(
    monkeypatch: pytest.MonkeyPatch, cancel: bool, fault: str
) -> None:
    """Receipts describe the real fitted paint, without synthesizing coverage."""
    from scripts import (
        linux_native_impedance_smoke as smoke,
        native_impedance_workflow as workflow,
    )

    canvas = SimpleNamespace(
        _recorded=False,
        _painted_scale=0.6,
        _visible_source_rect=(0.0, 0.0, 1600.0, 1000.0),
        _scaled_view=(0, 0, 1600, 1000),
        _dimensions=(960, 600),
        _painted_size=(1000, 700),
        _presentation_crop=None,
        _client_dimensions=(1000, 700),
        GetClientSize=lambda: SimpleNamespace(
            width=canvas._client_dimensions[0], height=canvas._client_dimensions[1]
        ),
        IsShownOnScreen=lambda: True,
    )
    approve = SimpleNamespace(
        GetLabel=lambda: "Approve spacing exception",
        GetChildren=lambda: [],
        IsEnabled=lambda: canvas._recorded,
        IsShownOnScreen=lambda: fault != "hidden_approve",
    )
    close = SimpleNamespace(
        GetLabel=lambda: "Cancel",
        GetChildren=lambda: [],
        IsShownOnScreen=lambda: fault != "hidden_cancel",
    )
    viewer = SimpleNamespace(
        canvas=canvas,
        approved=False,
        readable=False,
        approve_button=approve,
        FindWindow=lambda _identifier: close,
        GetChildren=lambda: [approve, close]
        + (
            [
                SimpleNamespace(
                    GetLabel=lambda: {
                        "forbidden_fit": "Fit image",
                        "forbidden_original": "Original image",
                        "forbidden_issue": "Fit issue",
                        "forbidden_route": "Fit route",
                    }[fault],
                    GetChildren=lambda: [],
                )
            ]
            if fault.startswith("forbidden_")
            else []
        ),
        caption=SimpleNamespace(
            IsShownOnScreen=lambda: True, GetLabel=lambda: "Spacing concern"
        ),
        status=SimpleNamespace(GetLabel=lambda: "Current full-source Fit paint"),
        _capture=SimpleNamespace(width=1600, height=1000),
    )
    wx = SimpleNamespace(ID_CANCEL=2, EVT_BUTTON=object())
    pane = SimpleNamespace()
    dialog = SimpleNamespace(preview_pane=pane)
    entry = SimpleNamespace(
        view_id="spacing-first",
        caption="Spacing concern",
        width_px=1600,
        height_px=1000,
        composition=SimpleNamespace(minimum_scale=0.35),
    )
    module = SimpleNamespace(EnlargedCaptureDialog=type(viewer))
    monkeypatch.setattr(workflow.importlib, "import_module", lambda _name: module)

    @contextmanager
    def show(_wx: Any, _kind: Any, action: Any, *, before_show: Any) -> Iterator[None]:
        before_show(viewer)
        yield
        canvas._recorded = viewer.readable = True
        action(viewer)

    def wait(_app: Any, predicate: Callable[[], bool], message: str) -> None:
        assert predicate(), message

    clicked: list[Any] = []

    def event(_wx: Any, control: Any, _binder: Any) -> None:
        clicked.append(control)
        if control is approve:
            viewer.approved = True
        elif control is not close:
            assert control is button, "The helper attempted image navigation"

    button = SimpleNamespace(IsEnabled=lambda: True)
    monkeypatch.setattr(workflow, "interact_modal", show)
    monkeypatch.setattr(workflow, "wait_until", wait)
    monkeypatch.setattr(smoke, "event", event)
    monkeypatch.setattr(smoke, "stock_button", lambda _viewer, _id: close)
    if fault == "cropped":
        canvas._visible_source_rect = (0.0, 0.0, 800.0, 1000.0)
    elif fault == "stale_size":
        canvas._painted_size = (1000, 699)
    elif fault == "small":
        canvas._painted_scale = 0.34
    elif fault == "presentation_crop":
        canvas._presentation_crop = (0, 0, 800, 1000)
    if fault not in {"none", "resized"}:
        with pytest.raises(RuntimeError, match="Fit|fit|controls|cropped|scale"):
            workflow.approve_spacing_review(
                object(), wx, dialog, button, entry, cancel=cancel
            )
        assert clicked == [button]
        assert not viewer.approved
        return

    def inspect(_viewer: Any) -> None:
        """Model the completed native resize performed by workflow inspectors."""
        if fault == "resized":
            canvas._dimensions = (1024, 640)
            canvas._painted_scale = 0.64
            canvas._painted_size = canvas._client_dimensions = (1060, 740)

    evidence = workflow.approve_spacing_review(
        object(), wx, dialog, button, entry, cancel=cancel, inspect=inspect
    )
    assert clicked == [button, close if cancel else approve]
    assert evidence["source_dimensions"] == (1600, 1000)
    assert evidence["fit_dimensions"] == (
        (1024, 640) if fault == "resized" else (960, 600)
    )
    assert evidence["fit_scale"] == (0.64 if fault == "resized" else 0.6)
    assert evidence["canvas_client_dimensions"] == (
        (1060, 740) if fault == "resized" else (1000, 700)
    )
    assert evidence["painted_source_rect"] == (0.0, 0.0, 1600.0, 1000.0)
    assert evidence["minimum_review_scale"] == 0.35
    assert not {"painted_review_regions", "required_review_regions"} & evidence.keys()


def test_workbook_preview_indexes_follow_spacing_first_entry_identity() -> None:
    """Section-only enumeration cannot address a mixed spacing-first image list."""
    from scripts.native_impedance_workflow import workbook_preview_indexes

    spacing = SimpleNamespace(view_id="spacing-first")
    first = SimpleNamespace(section_id="first-workbook")
    second = SimpleNamespace(section_id="second-workbook")
    pane = SimpleNamespace(entries=(spacing, first, second), sections=(first, second))
    assert workbook_preview_indexes(pane) == {
        "first-workbook": 1,
        "second-workbook": 2,
    }


def test_native_fixture_setup_acknowledges_its_existing_project_storage_notice(
    tmp_path: Path,
) -> None:
    """Fixture-created correction storage must not block the actual constructor."""
    from scripts.native_impedance_workflow import prepare_settings

    root = Path(__file__).resolve().parents[1]
    board = tmp_path / "sample.kicad_pcb"
    board.write_bytes(b"fixture board must remain unchanged")
    with load_siblings(
        "_native_workflow_fixture_setup",
        ("store", "library", "partselector_columns"),
        wx_stubs(),
    ) as loaded:
        modules = SimpleNamespace(
            store=loaded["store"],
            library=loaded["library"],
            columns=loaded["partselector_columns"],
        )
        prepare_settings(
            root, tmp_path, modules, SimpleNamespace(GetFileName=lambda: str(board))
        )
        store = modules.store.Store(None, str(tmp_path))
        assert store.is_schematic_storage_notice_acked()
        with sqlite3.connect(store.dbfile) as connection:
            assert connection.execute("SELECT * FROM correction").fetchall() == []
        assert store.get_generation_count() == 0
    assert board.read_bytes() == b"fixture board must remain unchanged"


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "missing",
        "pending",
        "wrong_nominal",
        "unproven_audit",
        "audit_nominal",
        "missing_comparison",
        "audit_comparison",
        "wrong_saved_nominal",
    ],
)
@pytest.mark.parametrize("expected_width_nm", [180_000, 181_405])
def test_native_width_acceptance_keeps_calculated_routes_and_proven_audit_rows(
    fault: str, expected_width_nm: int
) -> None:
    """Read real rendered row cells, including a 0.25 mm via-covered launch."""
    from impedance.matching import analyze, workbook_row_cells
    from impedance.model import (
        BoardSnapshot,
        Config,
        CopperFeature,
        LayerSettings,
        Specification,
        Trace,
    )
    from impedance.stackup_model import (
        Stackup,
        StackupLayer,
        WidthResult,
        calculation_fingerprint,
    )
    from impedance.width_checks import target_width_cell_text, width_check
    from scripts.native_impedance_workflow import check_nominal_width_rows

    spec = Specification(
        "signal",
        "RF",
        "50",
        "single_ended",
        "Controlled",
        (LayerSettings("F.Cu", ("B.Cu",)),),
    )
    snapshot = BoardSnapshot(
        ("F.Cu", "B.Cu"),
        (
            Trace("route", "F.Cu", "RF", 100_000, ((0, 0), (2_000_000, 0))),
            Trace("stub", "F.Cu", "RF", 100_000, ((3_000_000, 0), (3_250_000, 0))),
        ),
        net_classes=("Controlled",),
        net_class_memberships=(("RF", ("Controlled",)),),
        net_class_context_digest="native-width-fixture",
        copper_features=(
            CopperFeature(
                "via", "RF", "F.Cu", "via", "circle", (3_000_000, 0), 600_000, 600_000
            ),
        ),
    )
    stackup = Stackup(
        "sample",
        "Sample two-layer construction",
        2,
        "0.324",
        "1",
        "",
        calculator_id="sample-calculator",
        layers=(
            StackupLayer("L1", "copper", "0.035"),
            StackupLayer("Core", "core", "0.254", "FR4", "4.2"),
            StackupLayer("L2", "copper", "0.035"),
        ),
    )
    config = Config(
        True,
        (spec,),
        stackup=stackup,
        width_results=(
            WidthResult(
                spec.spec_id,
                "F.Cu",
                calculation_fingerprint(stackup, spec, "F.Cu"),
                "success",
                expected_width_nm + (fault == "wrong_saved_nominal"),
                "2026-09-23T00:00:00.000000Z",
                model="CoatedMicrostrip1B",
                calculation_digest="a" * 64,
            ),
        ),
    )
    analysis = analyze(config, snapshot)
    assert len(analysis.sections) == 2
    cells = [
        workbook_row_cells(
            row,
            spec,
            target_width=target_width_cell_text(
                width_check(config, spec, row.layer, row.width_nm)
            ),
        )
        for row in analysis.sections
    ]
    audit_index = next(
        i for i, row in enumerate(analysis.sections) if row.exclusion_reason
    )
    route_index = 1 - audit_index
    assert cells[audit_index][6] == ""
    targets = [row[6] for row in cells]
    comparisons = ["" if row.exclusion_reason else "!=" for row in analysis.sections]
    if fault in {"missing", "pending", "wrong_nominal"}:
        targets[route_index] = {
            "missing": "",
            "pending": "Pending",
            "wrong_nominal": "0.181 mm",
        }[fault]
    elif fault == "unproven_audit":
        snapshot = replace(snapshot, copper_features=())
    elif fault == "audit_nominal":
        targets[audit_index] = "0.18 mm"
    elif fault == "missing_comparison":
        comparisons[route_index] = ""
    elif fault == "audit_comparison":
        comparisons[audit_index] = "!="
    dialog = SimpleNamespace(
        session=SimpleNamespace(config=config, analysis=analysis, snapshot=snapshot),
        _row_ids=tuple(row.section_id for row in analysis.sections),
        sections=SimpleNamespace(
            GetItemCount=lambda: len(targets),
            GetTextValue=lambda index, column: {8: comparisons, 9: targets}[column][
                index
            ],
        ),
    )
    if fault == "none":
        check_nominal_width_rows(dialog, expected_width_nm)
        assert config.width_results[0].target_width_nm == expected_width_nm
        assert targets[route_index] == (
            "0.18141 mm" if expected_width_nm == 181_405 else "0.18 mm"
        )
    else:
        with pytest.raises(RuntimeError, match="nominal|covered-stub|marker"):
            check_nominal_width_rows(dialog, expected_width_nm)


def _spacing_fixture(partial: bool = False) -> tuple[object, object, object]:
    """Give smoke validators real independently defined target and copper gaps."""
    from impedance.matching import analyze
    from impedance.model import (
        BoardSnapshot,
        Config,
        LayerSettings,
        Specification,
        Trace,
    )

    config = Config(
        True,
        (
            Specification(
                "usb",
                "USB",
                "90",
                "differential",
                "USB",
                (LayerSettings("F.Cu", ("B.Cu",), 150_000),),
            ),
        ),
    )
    gap_nm = 150_000 if partial else 100_000
    traces = (
        Trace("p", "F.Cu", "USB_D+", 150_000, ((0, 0), (20_000_000, 0))),
        Trace(
            "n",
            "F.Cu",
            "USB_D-",
            150_000,
            ((0, gap_nm + 150_000), (20_000_000, gap_nm + 150_000)),
        ),
    )
    if partial:
        traces += (
            Trace(
                "unsupported-arc",
                "F.Cu",
                "USB_D+",
                150_000,
                ((30_000_000, 0), (31_000_000, 500_000), (32_000_000, 0)),
                is_native_arc=True,
            ),
        )
    snapshot = BoardSnapshot(
        ("F.Cu", "B.Cu"),
        traces,
        net_classes=("USB",),
        net_class_memberships=(("USB_D+", ("USB",)), ("USB_D-", ("USB",))),
        net_class_context_digest="native-spacing-validator-fixture",
        differential_pairs=(("USB_D+", "USB_D-"),),
    )
    return config, snapshot, analyze(config, snapshot)


@pytest.mark.parametrize("precise_pad_geometry", [False, True])
def test_long_native_entry_contract_keeps_outside_routing_without_precise_pads(
    precise_pad_geometry: bool,
) -> None:
    """Exercise the native acceptance oracle with both actual metadata contracts."""
    import math

    from impedance.model import BoardSnapshot, CopperFeature, Trace
    from impedance.spacing_checks import compare_pair_spacing, measure_pair_spacing
    from tests.test_impedance_spacing_native import _assert_long_entry_evidence

    traces = []
    pads = []
    for side, net in enumerate(("USB_D+", "USB_D-")):
        terminal_y = side * 1_000_000
        trunk_y = 400_000 + side * 300_000
        points = (
            (0, terminal_y),
            (2_000_000, trunk_y),
            (14_000_000, trunk_y),
            (16_000_000, terminal_y),
        )
        traces.extend(
            Trace(f"{side}-{index}", "F.Cu", net, 150_000, (start, end))
            for index, (start, end) in enumerate(zip(points, points[1:]))
        )
        pads.extend(
            CopperFeature(
                f"{side}-{index}", net, "F.Cu", "pad", "rect", point, 400_000, 400_000
            )
            for index, point in enumerate((points[0], points[-1]))
        )
    snapshot = BoardSnapshot(
        ("F.Cu", "B.Cu"),
        tuple(traces),
        copper_features=tuple(pads) if precise_pad_geometry else (),
        differential_pairs=(("USB_D+", "USB_D-"),),
    )
    check = compare_pair_spacing(
        measure_pair_spacing(snapshot, ("USB_D+", "USB_D-"), "F.Cu"), 150_000
    )
    total_nm = sum(
        round(math.dist(trace.points[0], trace.points[-1])) for trace in traces
    )
    _assert_long_entry_evidence(snapshot, check, total_nm)


def _spacing_grid(cells: list[tuple[str, str, str, str]]) -> SimpleNamespace:
    """Keep the four distinct cells and their actual fifteen-column positions."""
    labels = ["Other"] * 11 + ["Pair spacing", "", "Target spacing", "Spacing check"]
    rows = [list(row) for row in cells]
    return SimpleNamespace(
        labels=labels,
        rows=rows,
        GetColumnCount=lambda: len(labels),
        GetColLabelValue=lambda column: labels[column],
        GetTextValue=lambda row, column: rows[row][column - 11],
        GetSelectedRow=lambda: 0,
    )


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "missing_column",
        "duplicate_status_column",
        "missing_actual_column",
        "missing_target_column",
        "missing_actual_value",
        "wrong_actual_value",
        "missing_comparison",
        "missing_target_value",
        "wrong_target_value",
        "missing_status",
        "wrong_target",
        "wrong_gap",
        "stale_summary",
    ],
)
def test_native_spacing_acceptance_reads_dimensions_status_and_selected_details(
    fault: str,
) -> None:
    """A correct status alone cannot conceal missing or stale measured values."""
    from impedance.spacing_checks import (
        NmBounds,
        spacing_check_summary,
        spacing_checks_for_sections,
        spacing_workbook_cells,
    )
    from scripts.native_impedance_workflow import check_spacing_rows

    config, snapshot, analysis = _spacing_fixture()
    checks = spacing_checks_for_sections(config, snapshot, analysis.sections)
    section_ids = tuple(section.section_id for section in analysis.sections)
    assert len(section_ids) == 1
    check = checks[section_ids[0]]
    assert check.verdict == "deviation"
    assert check.declared_spacing_nm == 150_000
    assert check.minimum_gap_nm == NmBounds(100_000, 100_000)
    displayed = dict(checks)
    if fault == "wrong_target":
        displayed[section_ids[0]] = replace(check, declared_spacing_nm=151_000)
    elif fault == "wrong_gap":
        displayed[section_ids[0]] = replace(
            check, minimum_gap_nm=NmBounds(99_000, 99_000)
        )
    summary = spacing_check_summary(check)
    cells = list(spacing_workbook_cells(check))
    for issue, column, value in (
        ("missing_actual_value", 0, ""),
        ("wrong_actual_value", 0, "0.099 mm"),
        ("missing_comparison", 1, ""),
        ("missing_target_value", 2, ""),
        ("wrong_target_value", 2, "0.151 mm"),
        ("missing_status", 3, ""),
    ):
        if fault == issue:
            cells[column] = value
    grid = _spacing_grid([tuple(cells)])
    for issue, column, value in (
        ("missing_column", 14, "Other"),
        ("duplicate_status_column", 10, "Spacing check"),
        ("missing_actual_column", 11, "Other"),
        ("missing_target_column", 13, "Other"),
    ):
        if fault == issue:
            grid.labels[column] = value
    dialog = SimpleNamespace(
        session=SimpleNamespace(config=config, snapshot=snapshot, analysis=analysis),
        _row_ids=section_ids,
        _spacing_by_section=displayed,
        spacing_summary=SimpleNamespace(
            GetValue=lambda: "" if fault == "stale_summary" else summary
        ),
        sections=grid,
    )
    if fault == "none":
        evidence = check_spacing_rows(dialog)
        assert evidence["measured_rows"] == evidence["deviation_rows"] == 1
        assert evidence["rows"][0]["minimum_gap_nm"] == [100_000, 100_000]
    else:
        with pytest.raises(RuntimeError, match="spacing|Spacing"):
            check_spacing_rows(dialog)


def test_row_acceptance_uses_the_loaded_plugin_package_for_result_equality() -> None:
    """Reproduce package-separated result classes without claiming native execution."""
    from impedance.spacing_checks import spacing_checks_for_sections
    from scripts.native_impedance_workflow import (
        check_nominal_width_rows,
        check_spacing_rows,
    )

    config, snapshot, analysis = _spacing_fixture()
    canonical_checks = spacing_checks_for_sections(config, snapshot, analysis.sections)
    with load_siblings(
        "_spacing_smoke_plugin",
        (
            "impedance.model",
            "impedance.matching",
            "impedance.spacing_checks",
            "impedance.stackup_model",
        ),
        {},
    ) as loaded:
        model = loaded["impedance.model"]
        matching = loaded["impedance.matching"]
        spacing = loaded["impedance.spacing_checks"]
        plugin_config = model.Config.from_dict(config.to_dict())
        geometry = {
            field.name: getattr(snapshot, field.name) for field in fields(snapshot)
        }
        geometry["traces"] = tuple(
            model.Trace(
                **{field.name: getattr(trace, field.name) for field in fields(trace)}
            )
            for trace in snapshot.traces
        )
        plugin_snapshot = model.BoardSnapshot(**geometry)
        plugin_analysis = matching.analyze(plugin_config, plugin_snapshot)
        plugin_checks = spacing.spacing_checks_for_sections(
            plugin_config, plugin_snapshot, plugin_analysis.sections
        )
        # Same physical result, but dataclass equality requires the same class.
        assert plugin_checks != canonical_checks
        section_ids = tuple(section.section_id for section in plugin_analysis.sections)
        assert section_ids == tuple(canonical_checks)
        check = plugin_checks[section_ids[0]]
        dialog = SimpleNamespace(
            session=SimpleNamespace(
                config=plugin_config, snapshot=plugin_snapshot, analysis=plugin_analysis
            ),
            _row_ids=section_ids,
            _spacing_by_section=plugin_checks,
            spacing_summary=SimpleNamespace(
                GetValue=lambda: spacing.spacing_check_summary(check)
            ),
            sections=_spacing_grid([spacing.spacing_workbook_cells(check)]),
        )
        evidence = check_spacing_rows(dialog)
        assert evidence["deviation_rows"] == evidence["measured_rows"] == 1
        stackups = loaded["impedance.stackup_model"]
        stackup = stackups.Stackup(
            "sample", "Two-layer sample", 2, "0.324", "1", "", calculator_id="sample"
        )
        spec = plugin_config.specifications[0]
        exact_nominal = 181_405
        result = stackups.WidthResult(
            spec.spec_id,
            "F.Cu",
            stackups.calculation_fingerprint(stackup, spec, "F.Cu"),
            "success",
            exact_nominal,
            "2026-09-23T00:00:00.000000Z",
            model="CoatedMicrostrip1B",
            calculation_digest="a" * 64,
        )
        dialog.session.config = replace(
            plugin_config, stackup=stackup, width_results=(result,)
        )
        dialog.sections = SimpleNamespace(
            GetItemCount=lambda: 1,
            GetTextValue=lambda _row, column: {8: "!=", 9: "0.18141 mm"}[column],
        )
        check_nominal_width_rows(dialog, exact_nominal)
        assert result.target_width_nm == exact_nominal


@pytest.mark.parametrize("populated_column", [None, 0, 1, 2, 3])
def test_native_spacing_acceptance_requires_empty_single_ended_cells(
    populated_column: Optional[int],
) -> None:
    """Mixed tables must leave every spacing cell blank on single-ended rows."""
    from impedance.matching import analyze
    from impedance.model import LayerSettings, Trace
    from impedance.spacing_checks import (
        spacing_check_summary,
        spacing_checks_for_sections,
        spacing_workbook_cells,
    )
    from scripts.native_impedance_workflow import check_spacing_rows

    config, snapshot, _analysis = _spacing_fixture()
    single = replace(
        config.specifications[0],
        spec_id="clock",
        label="Clock",
        kind="single_ended",
        net_class="Clock",
        target_ohms="50",
        layer_settings=(LayerSettings("F.Cu", ("B.Cu",)),),
    )
    config = replace(config, specifications=(*config.specifications, single))
    snapshot = replace(
        snapshot,
        traces=(
            *snapshot.traces,
            Trace(
                "clock",
                "F.Cu",
                "CLK",
                100_000,
                ((0, 10_000_000), (20_000_000, 10_000_000)),
            ),
        ),
        net_classes=(*snapshot.net_classes, "Clock"),
        net_class_memberships=(*snapshot.net_class_memberships, ("CLK", ("Clock",))),
    )
    analysis = analyze(config, snapshot)
    checks = spacing_checks_for_sections(config, snapshot, analysis.sections)
    section_ids = tuple(section.section_id for section in analysis.sections)
    grid = _spacing_grid(
        [spacing_workbook_cells(checks.get(section_id)) for section_id in section_ids]
    )
    single_index = next(
        index
        for index, section in enumerate(analysis.sections)
        if section.spec_id == single.spec_id
    )
    assert grid.rows[single_index] == ["", "", "", ""]
    if populated_column is not None:
        grid.rows[single_index][populated_column] = "stale differential value"
    selected = checks.get(section_ids[0])
    dialog = SimpleNamespace(
        session=SimpleNamespace(config=config, snapshot=snapshot, analysis=analysis),
        _row_ids=section_ids,
        _spacing_by_section=checks,
        sections=grid,
        spacing_summary=SimpleNamespace(
            GetValue=lambda: spacing_check_summary(selected) if selected else ""
        ),
    )
    if populated_column is None:
        assert check_spacing_rows(dialog)["measured_rows"] == 1
    else:
        with pytest.raises(RuntimeError, match="Spacing row values"):
            check_spacing_rows(dialog)


def test_native_row_oracle_reuses_only_its_own_unchanged_immutable_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated UI assertions stay independent of product checks and target edits."""
    from impedance import spacing_checks as spacing
    from scripts.native_impedance_workflow import check_spacing_rows

    config, snapshot, analysis = _spacing_fixture()
    checks = spacing.spacing_checks_for_sections(config, snapshot, analysis.sections)
    section_ids = tuple(checks)
    check = checks[section_ids[0]]
    dialog = SimpleNamespace(
        session=SimpleNamespace(config=config, snapshot=snapshot, analysis=analysis),
        _row_ids=section_ids,
        _spacing_by_section=checks,
        spacing_summary=SimpleNamespace(
            GetValue=lambda: spacing.spacing_check_summary(check)
        ),
        sections=_spacing_grid([spacing.spacing_workbook_cells(check)]),
    )
    measure = spacing.measure_pair_spacing
    measured: list[Any] = []

    def independently_measure(*args: Any, **kwargs: Any) -> Any:
        measured.append(args[0])
        return measure(*args, **kwargs)

    monkeypatch.setattr(spacing, "measure_pair_spacing", independently_measure)
    first = check_spacing_rows(dialog)
    assert check_spacing_rows(dialog) == first
    assert measured == [snapshot]
    # An equal but freshly obtained native snapshot invalidates the oracle's
    # identity boundary; product render/approval caches are never consulted.
    dialog.session.snapshot = replace(snapshot)
    assert check_spacing_rows(dialog) == first
    assert len(measured) == 2 and measured[-1] is dialog.session.snapshot
    changed = replace(config.specifications[0].layer_settings[0], spacing_nm=151_000)
    dialog.session.config = replace(
        config,
        specifications=(replace(config.specifications[0], layer_settings=(changed,)),),
    )
    with pytest.raises(RuntimeError, match="stale"):
        check_spacing_rows(dialog)
    assert len(measured) == 2, "A target edit repeated target-independent geometry"


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "stale_caption",
        "hidden_caption",
        "missing_bitmap",
        "wrong_frame",
        "wrong_detail_crop",
        "wrong_painted_view",
        "wrong_visible_rect",
        "with_original",
        "wrong_original_frame",
        "region_gap",
        "region_gap_exact",
        "unknown_gap",
    ],
)
def test_native_spacing_preview_validator_requires_separate_unobscured_capture(
    fault: str,
) -> None:
    """Synthetic controls test the contract; native lanes prove visible painting."""
    from impedance.service import CapturedImage
    from impedance.spacing_checks import NmBounds
    from impedance.spacing_review import spacing_review_entries
    from scripts.native_impedance_workflow import check_spacing_preview

    config, snapshot, analysis = _spacing_fixture()
    entry = spacing_review_entries(config, snapshot, analysis.sections)[0]
    if fault in {"region_gap", "region_gap_exact", "unknown_gap"}:
        # A regional-only record has neither retained native cross-sections nor
        # an explicit representative. Do not invent a local exact gap.
        region = (
            None
            if fault == "unknown_gap"
            else NmBounds(100_000, 100_000 if fault == "region_gap_exact" else 125_000)
        )
        entry = replace(entry, members=(), representative=None, region_gap_nm=region)
    capture = _diagnostic_capture(entry)
    composition = entry.composition
    target = f"{entry.target_spacing_nm / 1_000_000:.6f}".rstrip("0").rstrip(".")
    if fault == "region_gap":
        assert composition.actual_gap_nm is None
        caption = f"Region gap 0.1–0.125 mm · target {target} mm"
    elif fault == "region_gap_exact":
        assert composition.actual_gap_nm is None
        caption = f"Region gap 0.1 mm · target {target} mm"
    elif fault == "unknown_gap":
        assert composition.actual_gap_nm is None
        caption = f"Gap details in spacing review · target {target} mm"
    else:
        actual = f"{composition.actual_gap_nm / 1_000_000:.6f}".rstrip("0").rstrip(".")
        caption = f"Shown gap {actual} mm · target {target} mm"
    if fault in {"with_original", "wrong_original_frame"}:
        original = _diagnostic_capture(
            SimpleNamespace(
                width_px=composition.detail_rect[2],
                height_px=composition.detail_rect[3],
                viewport_nm=entry.viewport_nm,
            )
        )
        if fault == "wrong_original_frame":
            original = replace(original, viewport_nm=(0, 0, 1, 1))
        capture = replace(capture, original=original)
    if fault == "wrong_frame":
        capture = CapturedImage.from_bytes(_PNG)
    dialog = SimpleNamespace(
        session=SimpleNamespace(config=config, spacing_entries=(entry,)),
        preview_pane=SimpleNamespace(
            selected_entry=entry,
            _capture=capture,
            caption=SimpleNamespace(
                GetLabel=lambda: "stale" if fault == "stale_caption" else caption,
                IsShown=lambda: fault != "hidden_caption",
            ),
            canvas=SimpleNamespace(
                _presentation_crop=(0, 0, 1, 1)
                if fault == "wrong_detail_crop"
                else composition.detail_rect,
                _scaled_view=(0, 0, 1, 1)
                if fault == "wrong_painted_view"
                else composition.detail_rect,
                _visible_source_rect=(0, 0, 1, 1)
                if fault == "wrong_visible_rect"
                else composition.detail_rect,
                _bitmap=None
                if fault == "missing_bitmap"
                else SimpleNamespace(IsOk=lambda: True),
            ),
        ),
    )
    if fault in {
        "none",
        "with_original",
        "region_gap",
        "region_gap_exact",
        "unknown_gap",
    }:
        assert check_spacing_preview(dialog) == {
            "view_id": entry.view_id,
            "diagnostic": True,
        }
    else:
        with pytest.raises(RuntimeError, match="Spacing|spacing|Thumbnail"):
            check_spacing_preview(dialog)


@pytest.mark.parametrize("fault", ["none", "crop", "painted_view", "visible_rect"])
def test_native_workbook_preview_requires_a_complete_painted_route(fault: str) -> None:
    """An ordinary route preview cannot pass with a partial image on the canvas."""
    from scripts.native_impedance_workflow import check_spacing_preview

    full = (0, 0, 800, 420)
    partial = (0, 0, 400, 420)
    dialog = SimpleNamespace(
        preview_pane=SimpleNamespace(
            selected_entry=SimpleNamespace(section_id="route"),
            _capture=SimpleNamespace(width=800, height=420),
            canvas=SimpleNamespace(
                _bitmap=SimpleNamespace(IsOk=lambda: True),
                _presentation_crop=partial if fault == "crop" else None,
                _scaled_view=partial if fault == "painted_view" else full,
                _visible_source_rect=partial if fault == "visible_rect" else full,
            ),
        )
    )
    if fault == "none":
        assert check_spacing_preview(dialog) == {
            "section_id": "route",
            "diagnostic": False,
        }
    else:
        with pytest.raises(RuntimeError, match="complete route"):
            check_spacing_preview(dialog)


def _diagnostic_capture(entry: Any) -> Any:
    """Supply actual PNG dimensions independently of capture metadata."""
    from impedance.service import CapturedImage

    def chunk(kind: bytes, data: bytes) -> bytes:
        """Write a real PNG without relying on a host imaging dependency."""
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data))
        )

    header = struct.pack(">IIBBBBB", entry.width_px, entry.height_px, 8, 2, 0, 0, 0)
    scanline = b"\0" + bytes((17, 43, 73)) * entry.width_px
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(scanline * entry.height_px))
        + chunk(b"IEND", b"")
    )
    return CapturedImage.from_bytes(
        png,
        viewport_nm=None if getattr(entry, "composition", None) else entry.viewport_nm,
    )


def test_spacing_report_parser_keeps_original_separate_from_approved_composite() -> (
    None
):
    """The comparison PNG cannot replace the exact image bound to consent."""
    from impedance.spacing_review import spacing_review_entries
    from scripts.native_impedance_workflow import _SpacingReportFields

    config, snapshot, analysis = _spacing_fixture()
    entry = spacing_review_entries(config, snapshot, analysis.sections)[0]
    combined_png = _diagnostic_capture(entry).data
    original_width, original_height = entry.composition.detail_rect[2:]
    original_png = _diagnostic_capture(
        SimpleNamespace(
            width_px=original_width, height_px=original_height, viewport_nm=None
        )
    ).data
    assert combined_png != original_png
    combined = b64encode(combined_png).decode("ascii")
    original = b64encode(original_png).decode("ascii")
    parser = _SpacingReportFields()
    parser.feed(
        '<article class="spacing-review"><div class="spacing-capture">'
        f'<img src="data:image/png;base64,{combined}" '
        f'width="{entry.width_px}" height="{entry.height_px}">'
        '</div><details class="spacing-original">'
        f'<img src="data:image/png;base64,{original}" '
        f'width="{original_width}" height="{original_height}">'
        "</details></article>"
    )
    assert parser.spacing_cards[0]["data"] == combined_png
    assert parser.spacing_cards[0]["width"] == entry.width_px
    assert parser.spacing_cards[0]["height"] == entry.height_px
    assert parser.spacing_cards[0]["original"] == {
        "data": original_png,
        "width": original_width,
        "height": original_height,
    }
    assert not parser.workbook_images
    assert parser.article_kinds == ["spacing"]


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "antialiased",
        "no_target",
        "no_tick",
        "no_witness",
        "unaligned_target",
        "wrong_frame",
        "legend_only",
    ],
)
def test_native_guide_evidence_requires_pixels_in_the_reviewed_capture(
    monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    """Native decoding results must include guide pixels, not merely SVG paths.

    This unit test substitutes only the native image decoder. Actual raster and
    physical guide placement are covered by the native guide fixture suite.
    """
    from impedance import spacing_overlays
    from impedance.spacing_review import spacing_review_entries
    from scripts.native_impedance_workflow import check_spacing_guide_pixels

    config, snapshot, analysis = _spacing_fixture()
    entry = spacing_review_entries(config, snapshot, analysis.sections)[0]

    # The physical detail occupies part of the combined PNG. Identically
    # colored legend or schematic marks cannot establish a guide on copper.
    class FixtureEntry(SimpleNamespace):
        pass

    FixtureEntry.__module__ = type(entry).__module__
    attributes = dict(vars(entry))
    attributes["composition"] = SimpleNamespace(detail_rect=(16, 54, 820, 594))
    entry = FixtureEntry(**attributes)
    paths = tuple(
        SimpleNamespace(kind=kind)
        for kind in ("target-edge", "measured-gap")
        if fault != "no_witness" or kind != "measured-gap"
    )
    monkeypatch.setattr(spacing_overlays, "spacing_guide_paths", lambda _entry: paths)
    target, tick = bytes((105, 228, 255)), bytes((255, 255, 255))
    if fault == "antialiased":
        # A 1.5px cyan / 1.2px white stroke on an integer pixel boundary
        # covers each neighbor by 75% / 60%, over the #10151d keyline.
        target, tick = bytes((83, 176, 198)), bytes((159, 161, 165))
    prefix = target + tick
    if fault == "no_target":
        prefix = tick * 2
    elif fault == "no_tick":
        prefix = target * 2
    elif fault == "unaligned_target":
        prefix = b"\0" + target + b"\0\0" + tick
    size = entry.width_px * entry.height_px * 3
    pixels = bytearray(bytes((17, 43, 73)) * (size // 3))
    offset = 0 if fault == "legend_only" else (60 * entry.width_px + 20) * 3
    pixels[offset : offset + len(prefix)] = prefix
    rgb = bytes(pixels)
    image = SimpleNamespace(
        IsOk=lambda: True,
        GetWidth=lambda: entry.width_px - (fault == "wrong_frame"),
        GetHeight=lambda: entry.height_px,
        GetData=lambda: rgb,
    )
    wx = SimpleNamespace(Image=lambda *_args: image, BITMAP_TYPE_PNG=1)
    capture = SimpleNamespace(
        data=b"native decoder fixture", sha256="exact reviewed PNG"
    )
    if fault in {"none", "antialiased"}:
        assert check_spacing_guide_pixels(entry, capture, wx) == {
            "guide_paths": {"target-edge": 1, "measured-gap": 1},
            "guide_png_sha256": capture.sha256,
        }
    else:
        with pytest.raises(RuntimeError, match="Spacing"):
            check_spacing_guide_pixels(entry, capture, wx)


@pytest.mark.parametrize(
    "fault", ["none", "transition_warning", "hidden_defect", "missing"]
)
def test_native_seeded_spacing_validator_distinguishes_fanouts_from_routing(
    fault: str,
) -> None:
    """The smoke must not substitute noisy USB fanouts for its explicit defect."""
    from scripts.native_impedance_workflow import (
        SPACING_DEFECT_NETS,
        SPACING_QUIET_NETS,
        check_seeded_spacing_rows,
    )

    quiet = SimpleNamespace(
        verdict="deviation" if fault == "transition_warning" else "match",
        issues=(),
        transition_length_nm=8_000_000,
    )
    defect = SimpleNamespace(
        verdict="match" if fault == "hidden_defect" else "deviation",
        issues=(SimpleNamespace(location=object()),),
        transition_length_nm=8_000_000,
    )
    dialog = SimpleNamespace(
        session=SimpleNamespace(
            analysis=SimpleNamespace(
                sections=(
                    SimpleNamespace(section_id="quiet", net_names=SPACING_QUIET_NETS),
                    SimpleNamespace(section_id="defect", net_names=SPACING_DEFECT_NETS),
                )
            )
        ),
        _spacing_by_section={"quiet": quiet, "defect": defect}
        if fault != "missing"
        else {},
    )
    if fault == "none":
        assert check_seeded_spacing_rows(dialog) == {"quiet_rows": 1, "defect_rows": 1}
    else:
        with pytest.raises(RuntimeError, match="spacing|fanouts|trunk"):
            check_seeded_spacing_rows(dialog)


@pytest.fixture
def report_namespace(request: pytest.FixtureRequest) -> Iterator[dict[str, Any]]:
    """Load actual report writers and models under the standalone or plugin package."""
    targets = (
        "impedance.model",
        "impedance.matching",
        "impedance.html_report",
        "impedance.review_tracking",
        "impedance.service",
        "impedance.workbook",
        "impedance.spacing_review",
    )
    if request.param == "impedance":
        yield {name: importlib.import_module(name) for name in targets}
    else:
        with load_siblings("_spacing_report_plugin", targets, {}) as loaded:
            yield loaded


@pytest.mark.parametrize("report_namespace", ["impedance", "private"], indirect=True)
@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "missing_card",
        "wrong_caption",
        "unapproved",
        "different_png",
        "missing_original",
        "wrong_original_dimensions",
        "wrong_original_pixels",
        "same_original",
        "excel_image",
        "wrong_order",
    ],
)
def test_native_spacing_report_validator_requires_individual_closeups_outside_excel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    report_namespace: dict[str, Any],
) -> None:
    """Exact diagnostic PNG consent and original spreadsheet media are independent."""
    from scripts.native_impedance_workflow import check_spacing_report

    html_report = report_namespace["impedance.html_report"]
    review_tracking = report_namespace["impedance.review_tracking"]
    service = report_namespace["impedance.service"]
    workbook = report_namespace["impedance.workbook"]
    model = report_namespace["impedance.model"]
    matching = report_namespace["impedance.matching"]
    spacing_review = report_namespace["impedance.spacing_review"]
    config, snapshot, analysis = _spacing_fixture()
    if type(config) is not model.Config:
        config = model.Config.from_dict(config.to_dict())
        geometry = {
            field.name: getattr(snapshot, field.name) for field in fields(snapshot)
        }
        geometry["traces"] = tuple(
            model.Trace(
                **{field.name: getattr(trace, field.name) for field in fields(trace)}
            )
            for trace in snapshot.traces
        )
        snapshot = model.BoardSnapshot(**geometry)
        analysis = matching.analyze(config, snapshot)
    entries = spacing_review.spacing_review_entries(config, snapshot, analysis.sections)
    diagnostics = []
    for entry in entries:
        capture = service.CapturedImage.from_bytes(
            _diagnostic_capture(entry).data, viewport_nm=entry.viewport_nm
        )
        original_width, original_height = entry.composition.detail_rect[2:]
        original = service.CapturedImage.from_bytes(
            _diagnostic_capture(
                SimpleNamespace(
                    width_px=original_width,
                    height_px=original_height,
                    viewport_nm=entry.viewport_nm,
                )
            ).data,
            viewport_nm=entry.viewport_nm,
        )
        if fault == "wrong_original_dimensions":
            original = service.CapturedImage.from_bytes(_PNG)
        elif fault == "same_original":
            original = capture
        if fault != "missing_original":
            capture = replace(capture, original=original)
        diagnostics.append(spacing_review.SpacingReviewImage(entry, capture))
    diagnostics = tuple(diagnostics)
    tracking = config.review_tracking
    for image in diagnostics:
        entry, capture = image.entry, image.capture
        digest = review_tracking.spacing_capture_fingerprint(
            entry.input_digest, capture.sha256
        )
        record = review_tracking.SpacingImageReview(
            entry.spec_id,
            entry.layer,
            entry.issue_id,
            entry.view_id,
            entry.input_digest,
            digest,
            "2026-10-24T12:00:00.000000Z",
            "" if fault == "unapproved" else digest,
            "" if fault == "unapproved" else "2026-10-24T12:00:01.000000Z",
        )
        tracking = review_tracking.put_spacing_image(tracking, record)
    config = replace(config, review_tracking=tracking)
    plan = service.ExportPlan(config, snapshot, analysis, analysis.sections)
    image = service.CapturedImage.from_bytes(_PNG)
    rows = service.report_rows(plan, (image,) * len(plan.sections))
    markup = html_report.write_html_report(
        rows,
        tmp_path / "spacing-review.html",
        spacing_reviews=diagnostics,
        review_tracking=tracking,
    ).read_text(encoding="utf-8")
    assert 'class="spacing-review"' in markup
    assert 'class="spacing-issue"' not in markup
    if fault == "missing_card":
        markup = markup.replace('class="spacing-review"', 'class="omitted-review"')
    elif fault == "wrong_caption":
        markup = markup.replace(entries[0].caption, "stale concern")
    elif fault == "different_png":
        markup = markup.replace(
            b64encode(diagnostics[0].capture.data).decode("ascii"),
            b64encode(_PNG).decode("ascii"),
        )
    elif fault == "wrong_original_pixels":
        markup = markup.replace(
            b64encode(diagnostics[0].capture.original.data).decode("ascii"),
            b64encode(_PNG).decode("ascii"),
        )
    elif fault == "wrong_order":
        first_article = markup.index("<article")
        cards_end = markup.rindex("</article>") + len("</article>")
        cards = re.findall(r"<article\b.*?</article>", markup, re.DOTALL)
        spacing = [card for card in cards if 'class="spacing-review"' in card]
        workbook_cards = [
            card for card in cards if 'class="spacing-review"' not in card
        ]
        markup = (
            markup[:first_article]
            + "".join(workbook_cards + spacing)
            + markup[cards_end:]
        )
    workbook_rows = rows
    if fault == "excel_image":
        workbook_rows = (replace(rows[0], image=diagnostics[0].capture),) + rows[1:]
    workbook_bytes = workbook.write_workbook(
        workbook_rows, tmp_path / "spacing.xlsx"
    ).read_bytes()
    if fault == "none":
        spacing = importlib.import_module(
            type(config).__module__.rsplit(".", 1)[0] + ".spacing_checks"
        )
        measure = spacing.measure_pair_spacing
        measured: list[tuple[str, str]] = []

        def measured_once(*args: Any, **kwargs: Any) -> Any:
            measured.append(args[1])
            return measure(*args, **kwargs)

        monkeypatch.setattr(spacing, "measure_pair_spacing", measured_once)
        evidence = check_spacing_report(
            markup,
            config,
            snapshot,
            analysis.sections,
            require_spacing_reviews=True,
            workbook_bytes=workbook_bytes,
        )
        assert evidence["rows"][0]["located_issues"] == 1
        assert measured == [("USB_D+", "USB_D-")], (
            "One independent report validation repeated identical immutable-board geometry"
        )
    else:
        with pytest.raises(RuntimeError, match="spacing|Spacing|Excel"):
            check_spacing_report(
                markup,
                config,
                snapshot,
                analysis.sections,
                require_spacing_reviews=True,
                workbook_bytes=workbook_bytes,
            )


@pytest.mark.parametrize(
    "fault", ["none", "missing", "target", "measurement", "partial_pass"]
)
def test_native_spacing_report_acceptance_rejects_missing_or_false_evidence(
    tmp_path: Path,
    fault: str,
) -> None:
    """Validate exact per-row rendered evidence including unresolved coverage."""
    from impedance import html_report, service
    from impedance.spacing_checks import NmBounds
    from scripts.native_impedance_workflow import check_spacing_report

    config, snapshot, analysis = _spacing_fixture(partial=fault == "partial_pass")
    plan = service.ExportPlan(config, snapshot, analysis, analysis.sections)
    image = service.CapturedImage.from_bytes(_PNG)
    rows = list(service.report_rows(plan, (image,) * len(plan.sections)))
    if fault == "target":
        rows[0] = replace(rows[0], spacing_nm=151_000)
    elif fault == "measurement":
        rows[0] = replace(
            rows[0],
            spacing_check=replace(
                rows[0].spacing_check, minimum_gap_nm=NmBounds(99_000, 99_000)
            ),
        )
    elif fault == "partial_pass":
        assert any(row.spacing_check.coverage != "complete" for row in rows)
        rows = [
            replace(
                row,
                spacing_check=replace(
                    row.spacing_check, verdict="match", coverage="complete"
                ),
            )
            for row in rows
        ]
    markup = html_report.write_html_report(rows, tmp_path / "spacing.html").read_text(
        encoding="utf-8"
    )
    if fault == "missing":
        markup = markup.replace("Pair spacing", "Omitted check")
    if fault == "none":
        evidence = check_spacing_report(markup, config, snapshot, analysis.sections)
        assert evidence["deviation_rows"] == 1
        assert evidence["rows"][0]["declared_spacing_nm"] == 150_000
    else:
        with pytest.raises(RuntimeError, match="spacing|Spacing"):
            check_spacing_report(markup, config, snapshot, analysis.sections)


def _workbook(images: bool = True) -> bytes:
    """Provide only a ZIP media container, not a generated report workbook."""
    stream = BytesIO()
    with ZipFile(stream, "w") as workbook:
        workbook.writestr("xl/worksheets/sheet1.xml", "<worksheet/>")
        if images:
            workbook.writestr("xl/media/image1.png", _PNG)
            workbook.writestr("xl/media/image2.png", _PNG)
    return stream.getvalue()


def _entries(enabled: bool = True) -> dict[str, bytes]:
    """Supply minimally marked fabrication data and optional report fixtures."""
    members = {
        "sample-F_Cu.gbr": _GERBER,
        "sample-B_Cu.gbr": _GERBER,
        "sample-PTH.drl": _DRILL,
    }
    if enabled:
        image = b64encode(_PNG).decode("ascii")
        members[_WORKBOOK] = _workbook()
        members[_HTML] = (
            f'<!doctype html><img src="data:image/png;base64,{image}">'
        ).encode()
    return members


def _archive(tmp_path: Path, entries: dict[str, bytes]) -> Path:
    """Keep every synthetic artifact inside the test's temporary directory."""
    path = tmp_path / "manufacturing.zip"
    with ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path


@pytest.mark.parametrize("enabled", [True, False])
def test_archive_evidence_counts_fabrication_and_enabled_report_media(
    tmp_path: Path, enabled: bool
) -> None:
    """Success reports fabrication counts and reflects the requested state."""
    result = check_generated_archive(_archive(tmp_path, _entries(enabled)), enabled)
    assert result["gerbers"] == 2
    assert result["drills"] == 1
    assert result["workbook_images"] == (2 if enabled else 0)
    assert result["enabled"] is enabled


@pytest.mark.parametrize("extension", [".gbr", ".drl"])
def test_reports_cannot_substitute_for_missing_fabrication(
    tmp_path: Path, extension: str
) -> None:
    """Both actual fabrication categories are required even with reports."""
    entries = {
        name: data for name, data in _entries().items() if not name.endswith(extension)
    }
    with pytest.raises(ValueError):
        check_generated_archive(_archive(tmp_path, entries), True)


@pytest.mark.parametrize("member", ["sample-F_Cu.gbr", "sample-PTH.drl"])
@pytest.mark.parametrize("content", [b"", b"(kicad_pcb (version 20260101))"])
def test_fabrication_extensions_do_not_make_empty_or_pcb_data_valid(
    tmp_path: Path, member: str, content: bytes
) -> None:
    """The prior PCB-as-fabrication stand-in must never count as native evidence."""
    entries = _entries()
    entries[member] = content
    with pytest.raises(ValueError):
        check_generated_archive(_archive(tmp_path, entries), True)


def test_board_file_instead_of_fabrication_is_rejected(tmp_path: Path) -> None:
    """A board plus reports cannot satisfy the Generate fabrication requirement."""
    entries = _entries()
    entries = {
        name: data for name, data in entries.items() if name in {_WORKBOOK, _HTML}
    }
    entries["sample.kicad_pcb"] = b"(kicad_pcb (version 20260101))"
    with pytest.raises(ValueError):
        check_generated_archive(_archive(tmp_path, entries), True)


@pytest.mark.parametrize(
    ("member", "content"),
    [
        ("sample-F_Cu.gbr", b"G04 Missing format*\nM02*\n"),
        ("sample-F_Cu.gbr", b"%FSLAX46Y46*%\nG04 Truncated output*\n"),
        ("sample-PTH.drl", b"METRIC\n%\nM30\n"),
        ("sample-PTH.drl", b"M48\nMETRIC\n%\nX1.0Y1.0\n"),
    ],
)
def test_fabrication_requires_format_header_and_completion_marker(
    tmp_path: Path, member: str, content: bytes
) -> None:
    """Incomplete plotting and drilling output cannot produce a green smoke."""
    entries = _entries()
    entries[member] = content
    with pytest.raises(ValueError):
        check_generated_archive(_archive(tmp_path, entries), True)


def test_duplicate_members_are_rejected(tmp_path: Path) -> None:
    """A ZIP reader selecting one duplicate cannot conceal ambiguous output."""
    path = _archive(tmp_path, _entries())
    with (
        ZipFile(path, "a") as archive,
        pytest.warns(UserWarning, match="Duplicate name"),
    ):
        archive.writestr("sample-F_Cu.gbr", _GERBER)
    with pytest.raises(ValueError):
        check_generated_archive(path, True)


@pytest.mark.parametrize("missing", [_WORKBOOK, _HTML])
def test_enabled_generation_requires_both_reports(tmp_path: Path, missing: str) -> None:
    """An enabled archive must contain the workbook and its HTML companion."""
    entries = _entries()
    entries.pop(missing)
    with pytest.raises(ValueError):
        check_generated_archive(_archive(tmp_path, entries), True)


@pytest.mark.parametrize("content", [b"", b"not an XLSX archive", _workbook(False)])
def test_workbook_requires_embedded_capture_media(
    tmp_path: Path, content: bytes
) -> None:
    """A workbook name or empty template is insufficient capture evidence."""
    entries = _entries()
    entries[_WORKBOOK] = content
    with pytest.raises(ValueError):
        check_generated_archive(_archive(tmp_path, entries), True)


@pytest.mark.parametrize(
    "content", [b"", b"<html>No capture</html>", b'<img src="sample.png">']
)
def test_html_requires_self_contained_capture_media(
    tmp_path: Path, content: bytes
) -> None:
    """A stale external image reference must not pass as a portable report."""
    entries = _entries()
    entries[_HTML] = content
    with pytest.raises(ValueError):
        check_generated_archive(_archive(tmp_path, entries), True)


@pytest.mark.parametrize("stale", [(_WORKBOOK,), (_HTML,), (_WORKBOOK, _HTML)])
def test_disabling_generation_excludes_each_stale_report(
    tmp_path: Path, stale: tuple[str, ...]
) -> None:
    """Regeneration with impedance disabled must remove both report members."""
    entries = _entries(False)
    enabled_entries = _entries()
    entries.update({name: enabled_entries[name] for name in stale})
    with pytest.raises(ValueError):
        check_generated_archive(_archive(tmp_path, entries), False)
