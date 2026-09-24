"""Isolated native budgets: strict desktop targets or an explicit 2x CI ceiling.

Control-only cases use immutable source/capture seams after the feature toolbar
has painted. Full KiCad cases start from the actual visible plugin host and use
the public Configure entry with a disposable 492-track native board, actual plane
filling, snapshots, matching, raster rendering, wx controls, SQLite, and cleanup.
Detailed cases preload feature modules for phase instrumentation. The separate
first-use gate includes their imports in three fresh plain-Python processes.
"""

from __future__ import annotations

from base64 import b64decode
from collections.abc import Callable
from contextlib import ExitStack
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from time import perf_counter
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from tests.impedance_native_metrics import (
    NativeMetrics,
    annotate_budget_evidence,
    assert_opening_budgets,
    assert_operation_budgets,
    isolate_with_metrics,
    native_budget_environment,
)
from tests.impedance_performance_fixtures import (
    REPEATED_SAMPLES,
    historical_approval,
    populate_native_board,
    routed_draft,
    routed_workbook,
)
from tests.native_wx_support import pump, run_native, wait_until

if TYPE_CHECKING:
    from impedance.model import BoardSnapshot, Config, Section
    from impedance.service import CapturedImage, CaptureEvidence

pytestmark = [
    pytest.mark.native_wx,
    pytest.mark.skipif(
        os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
        reason="native performance budgets require an enabled isolated desktop lane",
    ),
]


def _ready_toolbar_host(frame: Any, wx: Any) -> dict[str, object]:
    """Require the real feature toolbar to be visible before a Configure action."""
    toolbar = frame.GetToolBar()
    toolbar.Realize()
    frame.Layout()
    painted = []

    def on_paint(event: Any) -> None:
        event.Skip()
        wx.CallAfter(lambda: painted.append(True))

    frame.Bind(wx.EVT_PAINT, on_paint)
    try:
        frame.Refresh()
        wait_until(
            wx,
            lambda: bool(painted)
            and frame.IsShownOnScreen()
            and toolbar.IsShownOnScreen(),
            reason="Native feature toolbar did not become visible",
        )
    finally:
        frame.Unbind(wx.EVT_PAINT, handler=on_paint)
    return {
        "class": type(toolbar).__name__,
        "shown": toolbar.IsShownOnScreen(),
        "parent_painted": bool(painted),
    }


def _ready_plugin_host(
    main: Any, provider: Any, wx: Any, cleanup: ExitStack
) -> tuple[Any, dict[str, Any]]:
    """Share the real visible host setup without constructing impedance controls twice."""
    frame = main.JLCPCBTools(None, provider)

    def close_host() -> None:
        if frame:
            assert frame.Close(force=True)
            wait_until(
                wx, lambda: not bool(frame), reason="Actual plugin host did not close"
            )

    cleanup.callback(close_host)
    frame.settings["gerber"]["fill_zones"] = True
    paints: set[str] = set()

    def painted(event: Any) -> None:
        name = "frame" if event.GetEventObject() is frame else "content_panel"
        event.Skip()
        wx.CallAfter(paints.add, name)

    frame.Bind(wx.EVT_PAINT, painted)
    frame.content_panel.Bind(wx.EVT_PAINT, painted)
    frame.Show()
    wait_until(
        wx,
        lambda: paints == {"frame", "content_panel"}
        and all(
            getattr(frame, name).IsShownOnScreen()
            for name in ("upper_toolbar", "logbox", "footprint_list")
        ),
        timeout_ms=10000,
        reason="Actual plugin host controls did not paint visibly",
    )
    assert not frame._project_storage_unavailable
    assert frame._impedance.configure_button.IsEnabled()
    return frame, {
        "class": type(frame).__name__,
        "painted": sorted(paints),
        "controls": {
            name: {
                "class": type(getattr(frame, name)).__name__,
                "shown": getattr(frame, name).IsShownOnScreen(),
                "client_size": list(getattr(frame, name).GetClientSize()),
            }
            for name in ("upper_toolbar", "logbox", "footprint_list")
        },
    }


_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4/58BAAT/Af9dfQKHAAAAAElFTkSuQmCC"
)


def _instrument_dialog(
    package_name: str,
    monkeypatch: pytest.MonkeyPatch,
    metrics: NativeMetrics,
    current: dict[str, Any],
) -> None:
    """Observe coarse real UI phases and the actual selected-canvas transition."""
    dialog_api = importlib.import_module(f"{package_name}.dialog")
    wx = dialog_api.wx
    for owner, attribute, label in (
        (wx.Dialog, "__init__", "native_dialog_constructor"),
        (dialog_api.dv.DataViewListCtrl, "__init__", "specification_list_constructor"),
        (dialog_api.dv.DataViewListCtrl, "AppendTextColumn", "specification_columns"),
        (wx.Button, "__init__", "button_constructor"),
        (wx.TextCtrl, "__init__", "text_constructor"),
        (wx.StaticText, "__init__", "label_constructor"),
        (wx.Window, "Layout", "native_layout"),
        (wx.Window, "SetSizer", "native_set_sizer"),
        (wx.Dialog, "CentreOnParent", "native_center"),
        (wx.Dialog, "CreateButtonSizer", "native_dialog_buttons"),
        (dialog_api.ImpedanceDialog, "_refresh_rows", "refresh_rows"),
        (
            dialog_api.ImpedanceDialog,
            "_populate_specifications",
            "specification_projection",
        ),
        (dialog_api.ImpedanceDialog, "_populate_sections", "grid_population"),
        (dialog_api.ImpedanceDialog, "_fit_workbook_columns", "grid_sizing"),
        (
            dialog_api.ImpedanceDialog,
            "_verify_review_snapshot",
            "preview_source_verification",
        ),
        (
            dialog_api.ImpedanceDialog,
            "_verify_restored_capture_review",
            "saved_capture_verification",
        ),
        (dialog_api, "analyze", "matching"),
        (dialog_api.tracking, "source_fingerprint", "source_fingerprint"),
    ):
        if hasattr(owner, attribute):
            monkeypatch.setattr(
                owner, attribute, metrics.timed(label, getattr(owner, attribute))
            )
    original_main_paint = dialog_api.ImpedanceDialog._on_main_paint

    def main_paint(dialog: Any, event: Any) -> None:
        original_main_paint(dialog, event)
        if metrics.phase == "opening":
            metrics.sample.setdefault(
                "first_paint_seconds", perf_counter() - metrics.started
            )

    monkeypatch.setattr(dialog_api.ImpedanceDialog, "_on_main_paint", main_paint)
    original_init = dialog_api.ImpedanceDialog.__init__
    installed = False

    def construct(dialog: Any, *args: Any, **kwargs: Any) -> None:
        """Retain lazy imports in cold timing, including test setup conservatively."""
        nonlocal installed
        if not installed:
            preview_api = metrics.timed("preview_lazy_import", importlib.import_module)(
                f"{package_name}.dialog_preview"
            )
            grid_api = metrics.timed("grid_lazy_import", importlib.import_module)(
                f"{package_name}.workbook_grid"
            )
            width_api = metrics.timed("width_lazy_import", importlib.import_module)(
                f"{package_name}.width_checks"
            )
            for owner, attribute, label in (
                (width_api, "specification_nominal_summary", "nominal_summary"),
                (grid_api.WorkbookGrid, "__init__", "workbook_grid_constructor"),
                (grid_api.WorkbookGrid, "SetItems", "grid_install"),
                (preview_api.WorkbookPreview, "_show_selected", "preview_callback"),
                (
                    preview_api.WorkbookPreview,
                    "_record_display",
                    "preview_display_validation",
                ),
                (preview_api.PreviewCanvas, "set_capture", "preview_png_decode"),
                (preview_api.PreviewCanvas, "_paint", "preview_paint"),
            ):
                if hasattr(owner, attribute):
                    monkeypatch.setattr(
                        owner,
                        attribute,
                        metrics.timed(label, getattr(owner, attribute)),
                    )
            after_paint = metrics.timed(
                "preview_after_paint", preview_api.PreviewCanvas._after_paint
            )

            def record_paint(canvas: Any, generation: int) -> None:
                after_paint(canvas, generation)
                dialog = current.get("dialog")
                if dialog is not None:
                    opening = metrics.phase == "opening"
                    metrics.record_open(dialog, canvas)
                    if opening and metrics.phase == "background":
                        opened = current.get("opened")
                        if opened is not None:
                            opened()

            monkeypatch.setattr(preview_api.PreviewCanvas, "_after_paint", record_paint)
            installed = True
        original_init(dialog, *args, **kwargs)

    monkeypatch.setattr(
        dialog_api.ImpedanceDialog, "__init__", metrics.timed("constructor", construct)
    )
    capture_idle = metrics.timed(
        "background_capture_step", dialog_api.ImpedanceDialog._on_main_capture_idle
    )

    def record_restore(dialog: Any, event: Any) -> None:
        capture_idle(dialog, event)
        if metrics.phase != "background":
            return
        if not dialog._capture_restore_pending and dialog.session.approved:
            metrics.sample.setdefault(
                "saved_approval_restored_seconds", perf_counter() - metrics.started
            )
        if not dialog._capture_prepare_sections and not dialog._capture_restore_pending:
            metrics.sample.setdefault(
                "all_background_captures_seconds", perf_counter() - metrics.started
            )

    monkeypatch.setattr(
        dialog_api.ImpedanceDialog, "_on_main_capture_idle", record_restore
    )


def _pending_save(
    dialog: Any,
    wx: Any,
    metrics: NativeMetrics,
    repository: Any,
) -> None:
    """Exercise Save while restoration is pending without mixing opening timings."""
    if not dialog._capture_restore_pending:
        return
    before, revision = repository.load()
    published = dialog.config
    metrics.phase = "pending_save"
    started = perf_counter()
    try:
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, wx.ID_OK)
        assert dialog.GetEventHandler().ProcessEvent(event)
        assert dialog.IsModal()
        assert not dialog._closing
        assert dialog._capture_restore_pending
        assert dialog.config == published
        assert repository.load() == (before, revision)
    finally:
        metrics.sample["pending_save_seconds"] = perf_counter() - started
        metrics.sample["pending_save_functions"] = metrics.summary("pending_save")
        metrics.phase = "background"


@pytest.mark.parametrize("approved", [False, True], ids=["draft", "verified-approved"])
def test_native_control_open_and_save_budgets(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    record_property: Callable[[str, object], None],
    approved: bool,
) -> None:
    """Budget usable first paint and actual Save event through modal return."""
    if isolate_with_metrics(
        request, monkeypatch, record_property, "KICAD_IMPEDANCE_PERFORMANCE_CHILD"
    ):
        return
    package_name = "_impedance_performance_native"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "impedance")]
    monkeypatch.setitem(sys.modules, package_name, package)
    model = importlib.import_module(f"{package_name}.model")
    dialog_api = importlib.import_module(f"{package_name}.dialog")
    service = importlib.import_module(f"{package_name}.service")
    database_api = importlib.import_module(f"{package_name}.database")
    repository_api = importlib.import_module(f"{package_name}.repository")
    workbook = routed_workbook(
        SimpleNamespace(model=model, dialog=dialog_api),
        hashlib.sha256(_PNG).hexdigest(),
    )
    database = database_api.ImpedanceDatabase(tmp_path / "project.db")
    board_path = tmp_path / "representative.kicad_pcb"
    board_path.write_text("(kicad_pcb)", encoding="utf-8")
    repository = repository_api.ImpedanceRepository(
        database, database.resolve_board(board_path)
    )
    repository.save(workbook.approved if approved else workbook.draft, 0)
    capture = service.CapturedImage(_PNG)
    monkeypatch.setattr(
        dialog_api.ImpedanceDialog, "_sync_catalog_count", lambda *_: None
    )
    monkeypatch.setattr(
        dialog_api.ImpedanceDialog, "_ensure_catalog_started", lambda *_: None
    )
    metrics = NativeMetrics()
    samples = metrics.samples
    current: dict[str, Any] = {}
    _instrument_dialog(package_name, monkeypatch, metrics, current)

    def exercise(frame: Any, wx: Any) -> None:
        """Keep native window creation, showing, and destruction in one GUI owner."""
        integration_api = importlib.import_module(f"{package_name}.integration")
        frame.pcbnew = SimpleNamespace(
            GetBoard=lambda: SimpleNamespace(GetFileName=lambda: str(board_path))
        )
        host_controls = integration_api.ImpedanceControls(frame, frame.CreateToolBar())
        host_controls.attach_store(SimpleNamespace(dbfile=database.path))
        assert host_controls.configure_button.IsEnabled()
        toolbar_ready = _ready_toolbar_host(frame, wx)

        def sample() -> None:
            """Finish one owned modal before creating the next sample."""
            failures = []
            save_started = []
            source_reads = []
            appearance_reads = []

            def snapshot() -> BoardSnapshot:
                source_reads.append(True)
                return workbook.snapshot

            def appearance() -> str:
                appearance_reads.append(True)
                return "native-performance-fixed-verified-theme"

            def preview(_section: Section, *, refresh: bool = False) -> CapturedImage:
                return capture

            def evidence(
                expected: BoardSnapshot, sections: tuple[Section, ...]
            ) -> CaptureEvidence:
                assert expected == workbook.snapshot
                appearance()
                return service.CaptureEvidence(
                    True, dict.fromkeys(sections, capture.sha256)
                )

            metrics.begin()
            metrics.sample["toolbar_host_ready"] = toolbar_ready
            config, revision = repository.load()

            def persist(candidate: Config) -> None:
                repository.save(candidate, revision)

            dialog = dialog_api.ImpedanceDialog(
                frame,
                config,
                metrics.timed("snapshot", snapshot)(),
                metrics.timed("capture_provider", preview),
                metrics.timed("snapshot", snapshot),
                appearance_context=metrics.timed("appearance", appearance),
                verify_captures=evidence,
                save_config=persist,
            )
            current["dialog"] = dialog

            def opened() -> None:
                try:
                    if approved:
                        assert dialog._capture_restore_pending
                        _pending_save(dialog, wx, metrics, repository)
                except BaseException as error:
                    failures.append(error)
                    if dialog.IsModal():
                        dialog.EndModal(wx.ID_CANCEL)

            current["opened"] = opened

            def accept() -> None:
                """Catch callback failures and unwind only this owned modal."""
                try:
                    wait_until(
                        wx,
                        lambda: "open_seconds" in metrics.sample,
                        timeout_ms=5000,
                        reason="The native workbook never became painted and usable",
                    )
                    wait_until(
                        wx,
                        lambda: not dialog._capture_prepare_sections
                        and not dialog._capture_restore_pending,
                        timeout_ms=5000,
                        reason="The native workbook background verification did not finish",
                    )
                    assert dialog.session.approved is approved
                    metrics.sample["background_functions"] = metrics.summary(
                        "background"
                    )
                    source_reads.clear()
                    appearance_reads.clear()
                    metrics.phase = "save"
                    save_started.append(perf_counter())
                    event = wx.CommandEvent(wx.EVT_BUTTON.typeId, wx.ID_OK)
                    assert dialog.GetEventHandler().ProcessEvent(event)
                except BaseException as error:
                    failures.append(error)
                    if dialog.IsModal():
                        dialog.EndModal(wx.ID_CANCEL)

            wx.CallAfter(accept)
            try:
                result = dialog.ShowModal()
                finished = perf_counter()
                if failures:
                    raise failures[0]
                assert result == wx.ID_OK
                samples[-1]["save_close_seconds"] = finished - save_started[0]
                samples[-1]["save_functions"] = metrics.summary("save")
                metrics.phase = None
                assert source_reads == [True]
                if approved:
                    assert appearance_reads == [True]
                assert dialog._closing
                assert dialog.preview_pane._closed
                saved, saved_revision = repository.load()
                assert saved == dialog.config
                assert saved_revision == revision + 1
            finally:
                metrics.phase = None
                current.pop("dialog", None)
                current.pop("opened", None)
                if dialog:
                    dialog.Destroy()
                pump(wx)

        for _sample in range(REPEATED_SAMPLES):
            sample()

    try:
        run_native(exercise)
        assert_opening_budgets(samples)
        assert_operation_budgets(samples, "save_close_seconds")
        assert_operation_budgets(samples, "pending_save_seconds", optional=True)
    finally:
        metrics.dump(record_property)
        for name in tuple(sys.modules):
            if name.startswith(f"{package_name}."):
                del sys.modules[name]


@pytest.mark.native_kicad
@pytest.mark.kicad10_impedance
@pytest.mark.parametrize("approved", [False, True], ids=["draft", "approved"])
def test_native_kicad_configure_and_save_budgets(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    record_property: Callable[[str, object], None],
    approved: bool,
) -> None:
    """Measure public Configure with native copper, snapshots, raster, wx, and SQLite.

    Only catalog/provider network access is disabled. Normal plugin startup,
    fixture construction, and historical approval are setup. Each cold opening fills
    its planes, takes native snapshots, builds controls, and renders current pixels.
    Save timing ends after modal dismissal and integration resource cleanup.
    """
    if isolate_with_metrics(
        request,
        monkeypatch,
        record_property,
        "KICAD_IMPEDANCE_KICAD_PERFORMANCE_CHILD",
        native="kicad",
    ):
        return
    pcbnew = pytest.importorskip("pcbnew")
    from scripts.native_impedance_workflow import (
        FixtureBoardProvider,
        plugin_modules,
        prepare_settings,
    )

    root = Path(__file__).resolve().parents[1]
    plugin = plugin_modules(root)
    package_name = plugin.dialog.__package__
    api = SimpleNamespace(
        **{
            name: importlib.import_module(f"{package_name}.{module}")
            for name, module in {
                "model": "model",
                "dialog": "dialog",
                "integration": "integration",
                "database": "database",
                "repository": "repository",
                "adapter": "pcbnew_adapter",
                "render": "render",
                "copy": "board_copy",
                "service": "service",
            }.items()
        }
    )
    monkeypatch.setattr(
        api.dialog.ImpedanceDialog, "_sync_catalog_count", lambda *_: None
    )
    monkeypatch.setattr(
        api.dialog.ImpedanceDialog, "_ensure_catalog_started", lambda *_: None
    )
    original_modal = api.dialog.ImpedanceDialog.ShowModal
    metrics = NativeMetrics()
    samples = metrics.samples
    current: dict[str, Any] = {}
    _instrument_dialog(package_name, monkeypatch, metrics, current)

    for owner, attribute, label in (
        (api.adapter, "snapshot_board", "snapshot"),
        (api.render.SectionRenderer, "render", "render"),
        (api.render.SectionRenderer, "_plot_layer", "native_plot"),
        (api.render, "copy_for_render", "board_copy"),
        (api.render, "compose_layer_svgs", "svg_composition"),
        (api.render, "annotated_svg", "svg_annotation"),
        (api.render, "_wx_rasterize", "rasterization"),
        (api.render, "_validate_png", "png_validation"),
        (api.render, "read_theme_context", "render_appearance"),
        (api.integration, "read_theme_context", "appearance"),
        (api.repository.ImpedanceRepository, "load", "settings_load"),
        (api.repository.ImpedanceRepository, "save", "settings_save"),
    ):
        monkeypatch.setattr(
            owner, attribute, metrics.timed(label, getattr(owner, attribute))
        )
    monkeypatch.setattr(
        api.service.CapturedImage,
        "load",
        classmethod(metrics.timed("png_load", api.service.CapturedImage.load.__func__)),
    )

    def exercise(frame: Any, wx: Any) -> None:
        """Close all captures and controls before releasing the disposable board."""
        workbook = routed_workbook(api, hashlib.sha256(_PNG).hexdigest())
        path = tmp_path / "native-representative.kicad_pcb"
        with api.copy.empty_board(pcbnew) as board, ExitStack() as cleanup:
            populate_native_board(board, pcbnew, workbook, path)
            provider = FixtureBoardProvider(pcbnew, board)

            def fill_planes() -> bool:
                """Include native filling in every timed Configure request."""
                assert pcbnew.ZONE_FILLER(board).Fill(board.Zones()) is not False
                return True

            # Establish the prior approved source, not the next opening's result.
            fill_planes()
            source = api.adapter.snapshot_board(board, provider)
            assert len(source.traces) == len(workbook.snapshot.traces)
            assert not source.net_class_error
            assert {spec.net_class for spec in workbook.draft.specifications} <= set(
                source.net_classes
            )
            config = workbook.draft
            if approved:
                history_dir = tmp_path / "prior-review"
                history_dir.mkdir()
                with api.render.SectionRenderer(board, provider) as renderer:

                    def previous_pixels(section: Section) -> str:
                        image = renderer.render(
                            section, history_dir / f"{section.section_id}.png"
                        )
                        return api.service.CapturedImage.from_bytes(
                            image.read_bytes()
                        ).sha256

                    config = historical_approval(api, source, config, previous_pixels)
            settings_dir = prepare_settings(root, tmp_path, plugin, board)
            monkeypatch.setattr(plugin.main, "PLUGIN_PATH", settings_dir)
            database = api.database.ImpedanceDatabase(
                tmp_path / "jlcpcb" / "project.db"
            )
            repository = api.repository.ImpedanceRepository(
                database, database.resolve_board(path)
            )
            current["expected_revision"] = repository.save(config, 0)
            current["expected_config"] = config
            lazy_names = ("workbook_grid", "dialog_preview")
            before_host = {
                name: f"{package_name}.{name}" in sys.modules for name in lazy_names
            }
            # Mainwindow legitimately imports shared PreviewCanvas through
            # copper_zone_dialog; importing that module is normal host startup.
            assert before_host == {"workbook_grid": False, "dialog_preview": True}
            shared_preview = sys.modules[f"{package_name}.dialog_preview"]
            preview_instances = [0]
            original_preview_init = shared_preview.WorkbookPreview.__init__

            def count_preview(preview: Any, *args: Any, **kwargs: Any) -> None:
                preview_instances[0] += 1
                original_preview_init(preview, *args, **kwargs)

            monkeypatch.setattr(
                shared_preview.WorkbookPreview, "__init__", count_preview
            )
            frame, host_ready = _ready_plugin_host(plugin.main, provider, wx, cleanup)
            frame.prepare_copper_zones = metrics.timed(
                "copper_preparation", frame.prepare_copper_zones
            )
            controls = frame._impedance
            assert controls.repository is not None
            assert controls.repository.load() == repository.load()
            assert f"{package_name}.workbook_grid" not in sys.modules
            assert preview_instances == [0]
            host_ready.update(
                first_feature_modules_before_host=before_host,
                shared_preview_import_source="mainwindow -> copper_zone_dialog -> impedance.dialog_preview.PreviewCanvas",
                workbook_preview_instances_before_configure=preview_instances[0],
                first_feature_modules_before_configure={
                    name: f"{package_name}.{name}" in sys.modules for name in lazy_names
                },
            )
            clock = SimpleNamespace(started=0.0, save_started=0.0)
            failures = []
            monkeypatch.setattr(controls, "_show_error", failures.append)

            def modal(dialog: Any) -> int:
                current["dialog"] = dialog
                assert dialog.config == current["expected_config"]
                metrics.sample["reopened_persisted_configuration"] = (
                    metrics.sample["sample"] > 0
                )

                def opened() -> None:
                    try:
                        if approved:
                            assert dialog._capture_restore_pending
                            _pending_save(dialog, wx, metrics, repository)
                    except BaseException as error:
                        failures.append(error)
                        if dialog.IsModal():
                            dialog.EndModal(wx.ID_CANCEL)

                current["opened"] = opened

                def accept() -> None:
                    try:
                        wait_until(
                            wx,
                            lambda: "open_seconds" in metrics.sample,
                            timeout_ms=15000,
                            reason="Native source, rows, and selected pixels did not become usable",
                        )
                        # Exercise real native warming after recording usable
                        # opening, including input-dispatch latency per image.
                        pulses = [perf_counter()]
                        heartbeat = wx.Timer(dialog)

                        def pulse(_event: Any) -> None:
                            pulses.append(perf_counter())

                        dialog.Bind(wx.EVT_TIMER, pulse, heartbeat)
                        heartbeat.Start(5)
                        try:
                            wait_until(
                                wx,
                                lambda: not dialog._capture_prepare_sections
                                and not dialog._capture_restore_pending,
                                timeout_ms=15000,
                                reason="Native background captures did not finish",
                            )
                        finally:
                            heartbeat.Stop()
                            pulses.append(perf_counter())
                        samples[-1]["background_event_gap_seconds"] = max(
                            after - before for before, after in zip(pulses, pulses[1:])
                        )
                        assert dialog.session.approved is approved
                        samples[-1].update(
                            verification_seconds=perf_counter() - metrics.started,
                            background_functions=metrics.summary("background"),
                        )
                        metrics.phase = "save"
                        clock.save_started = perf_counter()
                        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, wx.ID_OK)
                        assert dialog.GetEventHandler().ProcessEvent(event)
                    except BaseException as error:
                        failures.append(error)
                        if dialog.IsModal():
                            dialog.EndModal(wx.ID_CANCEL)

                wx.CallAfter(accept)
                result = original_modal(dialog)
                if result == wx.ID_OK:
                    assert dialog._closing
                    assert dialog.preview_pane._closed
                    current["published_config"] = dialog.config
                return result

            monkeypatch.setattr(api.dialog.ImpedanceDialog, "ShowModal", modal)
            for _sample in range(REPEATED_SAMPLES):
                metrics.begin()
                metrics.sample["actual_plugin_host"] = host_ready
                accepted = controls.on_configure(None)
                finished = perf_counter()
                if failures:
                    raise failures[0]
                assert accepted, controls.error
                samples[-1].update(
                    save_close_seconds=finished - clock.save_started,
                    save_functions=metrics.summary("save"),
                )
                metrics.phase = None
                assert samples[-1]["opening_functions"]["matching"]["calls"] == 1
                assert samples[-1]["save_functions"]["snapshot"]["calls"] == 1
                assert "matching" not in samples[-1]["save_functions"]
                assert "render" not in samples[-1]["save_functions"]
                if approved:
                    assert samples[-1]["save_functions"]["appearance"]["calls"] == 1
                persisted, revision = repository.load()
                assert persisted == current["published_config"] == controls.config
                assert revision == current["expected_revision"] + 1
                current["expected_config"] = persisted
                current["expected_revision"] = revision
                metrics.sample.update(
                    persisted_revision=revision,
                    persisted_configuration_matches_published=True,
                )
                current.pop("dialog", None)
                current.pop("opened", None)
                pump(wx)

    try:
        run_native(exercise, timeout_ms=40000)
        assert_opening_budgets(samples)
        assert_operation_budgets(samples, "save_close_seconds")
        assert_operation_budgets(samples, "pending_save_seconds", optional=True)
        assert_operation_budgets(samples, "background_event_gap_seconds")
    finally:
        metrics.dump(record_property)
        for name in tuple(sys.modules):
            if name.startswith(f"{package_name}."):
                del sys.modules[name]


def _run_first_use(output: Path) -> None:
    """Measure one genuinely cold public action, without pytest session preimports."""
    import pcbnew

    from scripts.native_impedance_workflow import FixtureBoardProvider, prepare_settings

    metrics = NativeMetrics()
    root = Path(__file__).resolve().parents[1]
    prefix = "_impedance_first_use_native"
    package = ModuleType(prefix)
    package.__path__ = [str(root)]
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(
            "KICAD_IMPEDANCE_PERFORMANCE_ARTIFACT", str(output / "samples.json")
        )
        patch.setitem(sys.modules, prefix, package)
        patch.syspath_prepend(str(root / "lib"))
        main = importlib.import_module(f"{prefix}.mainwindow")
        package_name = f"{prefix}.impedance"
        # These modules are dependencies of ordinary plugin startup. Looking them
        # up must not import the dialog, grid, calculator, or raster runtime early.
        api = SimpleNamespace(
            **{
                name: sys.modules[f"{package_name}.{module}"]
                for name, module in {
                    "model": "model",
                    "copy": "board_copy",
                    "database": "database",
                    "repository": "repository",
                    "catalog": "catalog_cache",
                    "matching": "matching",
                    "adapter": "pcbnew_adapter",
                    "render": "render",
                    "integration": "integration",
                }.items()
            }
        )
        plugin = SimpleNamespace(
            main=main,
            **{
                name: sys.modules[f"{prefix}.{module}"]
                for name, module in {
                    "columns": "partselector_columns",
                    "library": "library",
                    "store": "store",
                }.items()
            },
        )

        def exercise(_host: Any, wx: Any) -> None:
            snapshot, config = routed_draft(SimpleNamespace(model=api.model))
            with api.copy.empty_board(pcbnew) as board, ExitStack() as cleanup:
                path = output / "representative.kicad_pcb"
                populate_native_board(
                    board,
                    pcbnew,
                    SimpleNamespace(snapshot=snapshot, draft=config),
                    path,
                )
                provider = FixtureBoardProvider(pcbnew, board)
                patch.setattr(
                    main, "PLUGIN_PATH", prepare_settings(root, output, plugin, board)
                )
                database = api.database.ImpedanceDatabase(output / "jlcpcb/project.db")
                repository = api.repository.ImpedanceRepository(
                    database, database.resolve_board(path)
                )
                revision = repository.save(config, 0)
                repository.save_stackup_catalog(
                    4,
                    api.catalog.CatalogCache(
                        (config.stackup,), api.catalog.catalog_checked_at()
                    ),
                )
                frame, ready = _ready_plugin_host(main, provider, wx, cleanup)
                controls = frame._impedance
                assert frame.pcbnew is provider and provider.GetBoard() is board
                assert controls.repository.load() == (config, revision)
                cold_modules = {
                    **{name: name in sys.modules for name in ("wx.grid", "wx.svg")},
                    **{
                        name: f"{package_name}.{name}" in sys.modules
                        for name in (
                            "dialog",
                            "workbook_grid",
                            "stackup_dialog",
                            "jlcpcb_calculator",
                        )
                    },
                }
                assert not any(cold_modules.values()), cold_modules
                for owner, attribute, label in (
                    (api.matching, "analyze", "matching"),
                    (api.adapter, "snapshot_board", "snapshot"),
                    (api.render.SectionRenderer, "render", "render"),
                    (api.integration, "read_theme_context", "appearance"),
                    (api.repository.ImpedanceRepository, "load", "settings_load"),
                    (api.repository.ImpedanceRepository, "save", "settings_save"),
                    (frame, "prepare_copper_zones", "copper_preparation"),
                ):
                    patch.setattr(
                        owner,
                        attribute,
                        metrics.timed(label, getattr(owner, attribute)),
                    )
                original_modal = wx.Dialog.ShowModal
                active: dict[str, Any] = {}
                failures: list[BaseException] = []

                def fail(error: BaseException) -> None:
                    failures.append(error)
                    dialog = active.get("dialog")
                    if dialog and dialog.IsModal():
                        dialog.EndModal(wx.ID_CANCEL)

                def save(dialog: Any) -> None:
                    try:
                        metrics.phase = "save"
                        active["save_started"] = perf_counter()
                        assert dialog.GetEventHandler().ProcessEvent(
                            wx.CommandEvent(wx.EVT_BUTTON.typeId, wx.ID_OK)
                        )
                        assert dialog._closing and dialog.preview_pane._closed
                        active["published"] = dialog.config
                    except BaseException as error:
                        fail(error)

                def modal(dialog: Any) -> int:
                    if (
                        dialog.GetParent() is frame
                        and type(dialog).__name__ == "ImpedanceDialog"
                    ):
                        active["dialog"] = dialog
                        canvas = dialog.preview_pane.canvas
                        original_after_paint = canvas._after_paint

                        def after_paint(generation: int) -> None:
                            try:
                                original_after_paint(generation)
                                if generation != canvas._generation:
                                    return
                                metrics.record_open(dialog, canvas)
                                if "open_seconds" in metrics.sample:
                                    canvas._after_paint = original_after_paint
                                    wx.CallAfter(save, dialog)
                            except BaseException as error:
                                fail(error)

                        canvas._after_paint = after_paint
                    return original_modal(dialog)

                patch.setattr(wx.Dialog, "ShowModal", modal)
                patch.setattr(
                    controls,
                    "_show_error",
                    lambda message: fail(AssertionError(message)),
                )
                metrics.begin()
                metrics.sample.update(
                    measurement="first_public_configure",
                    actual_plugin_host=ready,
                    feature_modules_before_configure=cold_modules,
                )
                assert controls.configure_button.GetEventHandler().ProcessEvent(
                    wx.CommandEvent(
                        wx.EVT_BUTTON.typeId, controls.configure_button.GetId()
                    )
                )
                finished = perf_counter()
                if failures:
                    raise failures[0]
                metrics.sample.update(
                    save_close_seconds=finished - active["save_started"],
                    save_functions=metrics.summary("save"),
                )
                metrics.phase = None
                assert metrics.sample["opening_functions"]["matching"]["calls"] == 1
                assert metrics.sample["opening_functions"]["render"]["calls"] == 1
                assert metrics.sample["save_functions"]["snapshot"]["calls"] == 1
                assert "matching" not in metrics.sample["save_functions"]
                assert "render" not in metrics.sample["save_functions"]
                persisted, saved_revision = repository.load()
                assert persisted == active["published"] == controls.config
                assert saved_revision == revision + 1
                wait_until(
                    wx,
                    lambda: not bool(active["dialog"]),
                    reason="Saved impedance dialog did not finish native destruction",
                )
                metrics.sample.update(
                    persisted_revision=saved_revision,
                    persisted_configuration_matches_published=True,
                )

        try:
            run_native(exercise, timeout_ms=30000)
        finally:
            metrics.dump(lambda _name, _value: None)


@pytest.mark.native_kicad
@pytest.mark.kicad10_impedance
def test_native_kicad_first_use_configure_budgets(
    tmp_path: Path, record_property: Callable[[str, object], None]
) -> None:
    """Include lazy imports in each of three fresh-process public Configure samples."""
    samples: list[dict[str, Any]] = []
    failures = []
    try:
        for index in range(REPEATED_SAMPLES):
            output = tmp_path / f"first-use-{index}"
            output.mkdir()
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "tests.test_impedance_performance_native",
                        str(output),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    env=native_budget_environment(),
                    timeout=45,
                )
                if result.returncode:
                    failures.append(result.stdout + result.stderr)
            except subprocess.TimeoutExpired as error:
                failures.append(str(error))
            artifact = output / "samples.json"
            if artifact.exists():
                evidence = json.loads(artifact.read_text(encoding="utf-8"))
                for sample in evidence:
                    sample["fresh_process_sample"] = index
                samples.extend(evidence)
            else:
                failures.append(
                    f"First-use process {index} produced no timing artifact"
                )
        assert not failures, "\n".join(failures)
        assert len(samples) == REPEATED_SAMPLES, samples
        assert_opening_budgets(samples)
        assert_operation_budgets(samples, "save_close_seconds")
    finally:
        annotate_budget_evidence(samples)
        record_property("native_performance_samples", samples)


if __name__ == "__main__":
    _run_first_use(Path(sys.argv[1]))
