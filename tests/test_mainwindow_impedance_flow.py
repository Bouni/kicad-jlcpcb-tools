"""Exercise impedance configuration, export sequencing, and rollback at the UI seam."""

from dataclasses import replace
from pathlib import Path
import sqlite3
import sys
from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from impedance.service import ExportArtifacts

from .test_mainwindow_empty_zone_warning import _make_window
from .wx_harness import load_mainwindow, module, wx_stubs


@pytest.fixture
def runtime() -> Any:
    """Load the real generation handler with a minimal isolated wx runtime."""
    return load_mainwindow(
        "impedance_generation_tests",
        wx=wx_stubs(
            Frame=type("Frame", (), {}),
            Dialog=type("Dialog", (), {}),
            NewIdRef=MagicMock(side_effect=object),
            BeginBusyCursor=MagicMock(),
            EndBusyCursor=MagicMock(),
            IsBusy=MagicMock(return_value=True),
            MessageBox=MagicMock(),
            MessageDialog=MagicMock(),
        ),
    )


def _window(runtime: Any, plan: Any = None) -> tuple[Any, list[str]]:
    """Supply controlled-impedance state without replacing generation logic."""
    window, steps = _make_window(runtime, [])
    window._impedance = SimpleNamespace(
        preflight=MagicMock(return_value=plan),
        verify_current=MagicMock(),
        verify_disabled=MagicMock(),
    )
    return window, steps


def _reports(scratch: Path) -> ExportArtifacts:
    """Create both enabled outputs for the separately tested report backend."""
    workbook = scratch / "Required_impedance_control.xlsx"
    companion = scratch / "Required_impedance_control.html"
    workbook.write_bytes(b"workbook")
    companion.write_text("<html>report</html>", encoding="utf-8")
    return ExportArtifacts(workbook, companion)


def _reviewed_plan(runtime: Any) -> Any:
    """Prepare a real reviewed configuration in the window's isolated package."""
    service = sys.modules[runtime.export_impedance_reports.__module__]
    model = sys.modules[f"{service.__package__}.model"]
    snapshot = model.BoardSnapshot(
        ("F.Cu", "B.Cu"),
        (model.Trace("clock", "F.Cu", "CLK", 150000, ((0, 0), (2000000, 0))),),
        "reviewed variant text",
        net_classes=("Clock",),
        net_class_memberships=(("CLK", ("Clock",)),),
        net_class_context_digest="clock constraints",
    )
    config = model.Config(
        enabled=True,
        specifications=(
            model.Specification(
                "clock",
                "Clock",
                "50",
                "single_ended",
                "Clock",
                (model.LayerSettings("F.Cu", ("B.Cu",)),),
            ),
        ),
    )
    analysis = service.analyze(config, snapshot)
    reviewed = replace(
        config,
        reviewed_digest=analysis.digest,
        included_section_ids=tuple(section.section_id for section in analysis.sections),
    )
    plan = service.prepare(reviewed, snapshot)
    assert plan is not None
    return plan


@pytest.mark.parametrize(
    ("enabled", "checkpoint", "expected_stage"),
    [
        (
            True,
            0,
            "Validating controlled-impedance review after board preparation",
        ),
        (
            True,
            1,
            "Validating controlled-impedance review after report rendering",
        ),
        (
            False,
            0,
            "Checking controlled impedance remains disabled after board preparation",
        ),
        (
            False,
            1,
            "Checking controlled impedance remains disabled before Gerber archive",
        ),
    ],
)
def test_validation_failure_reports_its_own_stage_and_preserves_failure_safety(
    runtime: Any,
    monkeypatch: Any,
    enabled: bool,
    checkpoint: int,
    expected_stage: str,
) -> None:
    """A failed guard names itself, not successful DRC, rendering, or drilling."""
    plan = object() if enabled else None
    window, _steps = _window(runtime, plan)
    window.layer_selection.GetString.return_value = "4 layers"
    window.generation_step = MethodType(runtime.JLCPCBTools.generation_step, window)
    window.run_generation_step = MethodType(
        runtime.JLCPCBTools.run_generation_step, window
    )
    error = RuntimeError(
        "The board, impedance specifications, or capture policy changed; "
        "scan and review the sections again."
        if enabled
        else "Controlled impedance was enabled during generation; restart the export."
    )
    verification = (
        window._impedance.verify_current
        if enabled
        else window._impedance.verify_disabled
    )
    verification.side_effect = [None] * checkpoint + [error]
    caught_errors: list[BaseException] = []
    scratch_paths: list[Path] = []

    def record_error(*_args: Any) -> None:
        """Observe the exception the real handler logs without replacing it."""
        exception = sys.exc_info()[1]
        assert exception is not None
        caught_errors.append(exception)

    def export(_plan: Any, _board: Any, _pcbnew: Any, scratch: Path) -> ExportArtifacts:
        """Complete image/workbook staging before the second enabled guard."""
        scratch_paths.append(scratch)
        return _reports(scratch)

    window.logger.exception.side_effect = record_error
    exporter = MagicMock(side_effect=export)
    monkeypatch.setattr(runtime, "export_impedance_reports", exporter)
    runtime.JLCPCBTools.generate_fabrication_data(window)

    runtime.wx.MessageBox.assert_called_once()
    message = runtime.wx.MessageBox.call_args.args[0]
    assert message == (
        f"Fabrication data generation failed during: {expected_stage}\n\n{error}"
    )
    assert caught_errors == [error]
    window.logger.exception.assert_called_once_with(
        "Fabrication data generation failed during %s", expected_stage
    )
    reports = [call.args[0] for call in window.report_generation_step.call_args_list]
    assert any(
        report.startswith("Running pre-export DRC check done (") for report in reports
    )
    assert reports[-1] == f"{expected_stage}..."
    assert verification.call_count == checkpoint + 1
    if enabled:
        verification.assert_called_with(plan, 4)
        window._impedance.verify_disabled.assert_not_called()
    else:
        verification.assert_called_with()
        window._impedance.verify_current.assert_not_called()
    if checkpoint == 0:
        window.fabrication.generate_geber.assert_not_called()
        window.fabrication.generate_excellon.assert_not_called()
    else:
        window.fabrication.generate_geber.assert_called_once_with(4)
        window.fabrication.generate_excellon.assert_called_once_with()
    assert exporter.call_count == int(enabled and checkpoint == 1)
    assert all(not path.exists() for path in scratch_paths)
    window.fabrication.zip_gerber_excellon.assert_not_called()
    window.fabrication.write_cpl.assert_not_called()
    window.fabrication.generate_bom.assert_not_called()
    window.store.increment_generation_count.assert_not_called()
    assert [call.args[0] for call in window.run_generate_hook.call_args_list] == ["pre"]
    assert window.generate_button.Enable.call_args.args == (True,)
    assert window._current_generation_step == "initialization"
    runtime.wx.EndBusyCursor.assert_called_once_with()


def test_disabled_does_not_generate_workbook(runtime: Any, monkeypatch: Any) -> None:
    """A disabled board follows the existing archive path without image work."""
    window, steps = _window(runtime)
    export = MagicMock()
    monkeypatch.setattr(runtime, "export_impedance_reports", export)
    runtime.JLCPCBTools.generate_fabrication_data(window)
    export.assert_not_called()
    window.fabrication.zip_gerber_excellon.assert_called_once_with()
    assert steps.index("Checking controlled-impedance configuration") < steps.index(
        "Filling copper zones"
    )
    assert (
        steps.index("Running pre-export DRC check")
        < steps.index(
            "Checking controlled impedance remains disabled after board preparation"
        )
        < steps.index("Plotting Gerbers")
        < steps.index("Generating Excellon drill/map files")
        < steps.index(
            "Checking controlled impedance remains disabled before Gerber archive"
        )
        < steps.index("Creating Gerber archive (.zip)")
    )
    assert window._impedance.verify_disabled.call_count == 2
    window._impedance.verify_current.assert_not_called()


def test_concurrent_enable_blocks_disabled_archive(runtime: Any) -> None:
    """Another window enabling the form cannot result in a silently missing form."""
    window, _steps = _window(runtime)
    window._impedance.verify_disabled.side_effect = [
        None,
        RuntimeError("settings changed"),
    ]
    runtime.JLCPCBTools.generate_fabrication_data(window)
    window.fabrication.zip_gerber_excellon.assert_not_called()
    window.store.increment_generation_count.assert_not_called()
    assert "settings changed" in runtime.wx.MessageBox.call_args.args[0]


def test_enabled_embeds_workbook_before_scratch_cleanup(
    runtime: Any, monkeypatch: Any
) -> None:
    """The workbook exists for packaging and scratch files vanish afterward."""
    plan = object()
    window, steps = _window(runtime, plan)
    paths = []

    def export(_plan: Any, _board: Any, _pcbnew: Any, scratch: Path) -> ExportArtifacts:
        """Stand in for the separately tested renderer and workbook backend."""
        (scratch / "snippet.png").write_bytes(b"image")
        reports = _reports(scratch)
        paths.extend((reports.workbook, reports.html_report))
        return reports

    def archive(entries: Any) -> None:
        """Verify explicit member names while the scratch workbook is alive."""
        assert len(entries) == 2
        assert entries[0].source.read_bytes() == b"workbook"
        assert entries[0].archive_name == "Required_impedance_control.xlsx"
        assert entries[1].source.read_text(encoding="utf-8") == "<html>report</html>"
        assert entries[1].archive_name == "Required_impedance_control.html"

    monkeypatch.setattr(runtime, "export_impedance_reports", export)
    window.fabrication.zip_gerber_excellon.side_effect = archive
    runtime.JLCPCBTools.generate_fabrication_data(window)
    runtime.wx.MessageBox.assert_not_called()
    assert (
        steps.index("Running pre-export DRC check")
        < steps.index("Validating controlled-impedance review after board preparation")
        < steps.index("Plotting Gerbers")
        < steps.index("Generating Excellon drill/map files")
        < steps.index("Generating controlled-impedance workbook and HTML report")
        < steps.index("Validating controlled-impedance review after report rendering")
        < steps.index("Creating Gerber archive (.zip)")
    )
    assert not paths[0].parent.exists()
    assert window._impedance.verify_current.call_count == 2
    window.store.increment_generation_count.assert_called_once()


@pytest.mark.parametrize(
    "failure", ["preflight", "after_hook", "render", "before_zip", "zip"]
)
def test_failure_stops_publication_and_restores_controls(
    runtime: Any, monkeypatch: Any, failure: str
) -> None:
    """No failed impedance run increments the counter or runs the post hook."""
    window, _steps = _window(runtime, object())
    scratch_paths = []

    def export(_plan: Any, _board: Any, _pcbnew: Any, scratch: Path) -> ExportArtifacts:
        """Leave partial files to exercise real temporary-directory cleanup."""
        scratch_paths.append(scratch)
        reports = _reports(scratch)
        if failure == "render":
            raise RuntimeError("render failed")
        return reports

    monkeypatch.setattr(runtime, "export_impedance_reports", export)
    if failure == "preflight":
        window._impedance.preflight.side_effect = RuntimeError("invalid setup")
    elif failure == "after_hook":
        window._impedance.verify_current.side_effect = RuntimeError("board changed")
    elif failure == "before_zip":
        window._impedance.verify_current.side_effect = [
            None,
            RuntimeError("board changed"),
        ]
    elif failure == "zip":
        window.fabrication.zip_gerber_excellon.side_effect = OSError("disk full")

    runtime.JLCPCBTools.generate_fabrication_data(window)
    runtime.wx.MessageBox.assert_called_once()
    window.store.increment_generation_count.assert_not_called()
    assert all(
        call.args[0] != "post" for call in window.run_generate_hook.call_args_list
    )
    assert all(not path.exists() for path in scratch_paths)
    window.generate_button.Enable.assert_any_call(True)
    runtime.wx.EndBusyCursor.assert_called_once()
    if failure == "preflight":
        window.fabrication.fill_zones.assert_not_called()
        window.run_drc_before_gerber_export.assert_not_called()
        window.run_generate_hook.assert_not_called()
    if failure != "zip":
        window.fabrication.zip_gerber_excellon.assert_not_called()


def test_parts_store_initialization_has_no_board_ownership_workflow(
    runtime: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    """The feature attaches to ordinary parts storage without a migration prompt."""
    board_path = tmp_path / "board.kicad_pcb"
    board = SimpleNamespace(GetFileName=lambda: str(board_path))
    store = object()
    constructor = MagicMock(return_value=store)
    monkeypatch.setattr(runtime, "Store", constructor)
    window = SimpleNamespace(
        _variant_mode=False,
        assembly_lookup=SimpleNamespace(invalidate=MagicMock()),
        project_path=str(tmp_path),
        fabrication=object(),
        _set_project_storage_error=MagicMock(),
        _initialize_catalog_parts=MagicMock(),
        pcbnew=SimpleNamespace(GetBoard=lambda: board),
        _get_current_board=lambda: board,
        _impedance=SimpleNamespace(attach_store=MagicMock()),
        library=SimpleNamespace(state=object()),
    )
    runtime.JLCPCBTools.init_store(window)
    constructor.assert_called_once_with(window, str(tmp_path), board)
    window._impedance.attach_store.assert_called_once_with(store)
    window._set_project_storage_error.assert_called_once_with(None)
    window._initialize_catalog_parts.assert_called_once_with()
    runtime.wx.MessageDialog.assert_not_called()


def test_feature_database_failure_keeps_parts_initialization_available(
    runtime: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    """The real toolbar attachment cannot turn an impedance error into parts loss."""
    board = SimpleNamespace(GetFileName=lambda: str(tmp_path / "board.kicad_pcb"))
    store = SimpleNamespace(dbfile=tmp_path / "jlcpcb" / "project.db")
    monkeypatch.setattr(runtime, "Store", MagicMock(return_value=store))
    module = sys.modules[runtime.ImpedanceControls.__module__]
    for name in ("CheckBox", "Button", "StaticText"):
        monkeypatch.setattr(module.wx, name, MagicMock(), raising=False)
    monkeypatch.setattr(
        module,
        "ImpedanceDatabase",
        MagicMock(side_effect=sqlite3.OperationalError("Feature storage unavailable")),
    )
    window = SimpleNamespace(
        _variant_mode=False,
        assembly_lookup=SimpleNamespace(invalidate=MagicMock()),
        project_path=str(tmp_path),
        fabrication=object(),
        _set_project_storage_error=MagicMock(),
        _initialize_catalog_parts=MagicMock(),
        pcbnew=SimpleNamespace(GetBoard=lambda: board),
        _get_current_board=lambda: board,
    )
    window._impedance = runtime.ImpedanceControls(window, MagicMock())
    runtime.JLCPCBTools.init_store(window)

    assert window.store is store
    window._set_project_storage_error.assert_called_once_with(None)
    window._initialize_catalog_parts.assert_called_once_with()
    window._impedance.status.SetLabel.assert_called_once_with(
        "Impedance settings unavailable"
    )
    assert window._impedance.repository is None


def test_variant_store_attaches_impedance_before_controller_startup(
    runtime: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The matrix path must open the same feature storage as ordinary projects."""
    board = object()
    store = object()
    constructor = MagicMock(return_value=store)
    controller = SimpleNamespace(start_enrichment=MagicMock())
    window = SimpleNamespace(
        _variant_mode=True,
        assembly_lookup=SimpleNamespace(invalidate=MagicMock()),
        project_path=str(tmp_path),
        fabrication=object(),
        _set_project_storage_error=MagicMock(),
        _initialize_catalog_parts=MagicMock(),
        pcbnew=SimpleNamespace(GetBoard=lambda: board),
        _get_current_board=lambda: board,
        _impedance=SimpleNamespace(attach_store=MagicMock()),
    )

    def create_controller(parent: Any, cache: Any) -> Any:
        """Check attachment at the variant controller's construction boundary."""
        assert parent is window and cache is store
        window._impedance.attach_store.assert_called_once_with(store)
        return controller

    for name, symbols in (
        ("store", {"VariantStore": constructor}),
        ("controller", {"VariantMainController": create_controller}),
    ):
        module_name = f"{runtime.__package__}.variant.{name}"
        monkeypatch.setitem(sys.modules, module_name, module(module_name, **symbols))

    runtime.JLCPCBTools.init_store(window)

    assert window.store is store and window._variant_controller is controller
    constructor.assert_called_once_with(window, str(tmp_path), board)
    controller.start_enrichment.assert_called_once_with()
    window._initialize_catalog_parts.assert_not_called()


def test_variant_store_recovery_reattaches_disabled_impedance_controls(
    runtime: Any,
) -> None:
    """Recover feature controls alongside the retained matrix session/cache."""
    store = object()
    controller = SimpleNamespace(
        session=SimpleNamespace(_check_board=MagicMock(), reliable=True),
        cache=store,
        refresh=MagicMock(),
        _update_enabled=MagicMock(),
    )
    window = SimpleNamespace(
        _variant_controller=controller,
        _set_project_storage_error=MagicMock(),
        _impedance=SimpleNamespace(attach_store=MagicMock()),
    )

    runtime.JLCPCBTools.init_store(window)

    assert window.store is store
    window._impedance.attach_store.assert_called_once_with(store)
    controller.refresh.assert_called_once_with()
    controller._update_enabled.assert_called_once_with()
