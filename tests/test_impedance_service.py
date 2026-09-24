"""Exercise persistence and reviewed-board export without a live KiCad process."""

import builtins
from copy import deepcopy
from dataclasses import replace
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from typing import Any, Optional

import pytest

from impedance import service, workbook
from impedance.matching import analyze
from impedance.model import (
    BoardSnapshot,
    Config,
    LayerSettings,
    Section,
    Specification,
    Trace,
    ValidationError,
)
from impedance.repository import ImpedanceRepository
from tests.test_impedance_workbook import _png


class MemoryDatabase:
    """Record the persistence protocol without hiding malformed stored values."""

    def __init__(self, record: Optional[dict[str, Any]] = None) -> None:
        self.record = deepcopy(record)
        self.loads: list[str] = []
        self.saves: list[tuple[str, dict[str, Any], bool, int]] = []

    def load_config(self, board_id: str) -> Optional[dict[str, Any]]:
        """Return a detached database record while recording its board scope."""
        self.loads.append(board_id)
        return deepcopy(self.record)

    def save_config(
        self,
        board_id: str,
        payload: dict[str, Any],
        enabled: bool,
        expected_revision: int,
    ) -> int:
        """Record the exact configuration and revision sent to the database."""
        self.saves.append((board_id, deepcopy(payload), enabled, expected_revision))
        revision = expected_revision + 1
        self.record = {
            "version": 1,
            "revision": revision,
            "enabled": enabled,
            "payload": deepcopy(payload),
        }
        return revision


def _board() -> BoardSnapshot:
    """Create three disconnected routes across a four-layer copper stack."""
    return BoardSnapshot(
        ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"),
        (
            Trace("back", "B.Cu", "CLK_B", 150000, ((0, 4000000), (2000000, 4000000))),
            Trace("right", "F.Cu", "CLK_R", 150000, ((4000000, 0), (6000000, 0))),
            Trace("left", "F.Cu", "CLK_L", 150000, ((0, 0), (2000000, 0))),
        ),
        "board-context",
        net_classes=("Clock",),
        net_class_context_digest="clock-class-constraints",
        net_class_memberships=tuple(
            (net, ("Clock",)) for net in ("CLK_B", "CLK_R", "CLK_L")
        ),
    )


def _configuration() -> Config:
    """Provide an enabled requirement with references distinct from signal layers."""
    return Config(
        enabled=True,
        specifications=(
            Specification(
                "clock",
                "Clock",
                "50.25",
                "single_ended",
                "Clock",
                (
                    LayerSettings("F.Cu", ("In1.Cu", "In2.Cu")),
                    LayerSettings("B.Cu", ("In1.Cu", "In2.Cu")),
                ),
            ),
        ),
    )


def _review(config: Config, snapshot: BoardSnapshot) -> Config:
    """Explicitly include every analyzed section using the current digest."""
    analysis = analyze(config, snapshot)
    return replace(
        config,
        reviewed_digest=analysis.digest,
        included_section_ids=tuple(section.section_id for section in analysis.sections),
    )


def _record(config: Config) -> dict[str, Any]:
    """Represent the database envelope separately from the nested feature schema."""
    return {
        "version": 1,
        "revision": 7,
        "enabled": config.enabled,
        "payload": config.to_dict(),
    }


@pytest.fixture
def plan() -> service.ExportPlan:
    """Prepare a fully reviewed export that uses the supplied vendor template."""
    snapshot = _board()
    result = service.prepare(
        _review(_configuration(), snapshot), snapshot, layer_count=4
    )
    assert result is not None
    return result


def test_absent_configuration_is_disabled_without_database_write() -> None:
    """Opening an untouched board does not create a feature configuration."""
    database = MemoryDatabase()
    repository = ImpedanceRepository(database, "board-a")
    assert repository.load() == (Config(), 0)
    assert database.loads == ["board-a"]
    assert database.saves == []


def test_repository_preserves_review_and_disabled_specifications() -> None:
    """Disabling retains user intent and revision rather than dropping specifications."""
    original = replace(_review(_configuration(), _board()), enabled=False)
    database = MemoryDatabase(_record(original))
    repository = ImpedanceRepository(database, "board-a")
    assert repository.load() == (original, 7)
    enabled = replace(original, enabled=True)
    assert repository.save(enabled, 7) == 8
    assert database.saves == [("board-a", enabled.to_dict(), True, 7)]
    assert repository.load() == (enabled, 8)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", 2),
        ("version", True),
        ("version", "1"),
        ("revision", 0),
        ("revision", -1),
        ("revision", True),
        ("revision", "7"),
        ("enabled", False),
        ("enabled", 1),
        ("payload", []),
        ("payload", {}),
    ],
)
def test_corrupt_envelope_is_reported_without_repair(field: str, value: Any) -> None:
    """Never erase or silently disable a configuration that cannot be decoded."""
    stored = _record(_configuration())
    stored[field] = value
    database = MemoryDatabase(stored)
    with pytest.raises((ValidationError, ValueError, TypeError)):
        ImpedanceRepository(database, "board-a").load()
    assert database.record == stored
    assert database.saves == []


@pytest.mark.parametrize("field", ["version", "revision", "enabled", "payload"])
def test_incomplete_envelope_is_rejected(field: str) -> None:
    """Require all database contract fields without substituting defaults."""
    stored = _record(_configuration())
    del stored[field]
    database = MemoryDatabase(stored)
    with pytest.raises((ValidationError, ValueError, TypeError)):
        ImpedanceRepository(database, "board-a").load()
    assert database.saves == []


@pytest.mark.parametrize("revision", [-1, True, False, "1", 1.0, None])
def test_save_rejects_invalid_revision_before_database_access(revision: Any) -> None:
    """Optimistic revision checks require an exact nonnegative integer."""
    database = MemoryDatabase()
    with pytest.raises((ValidationError, ValueError, TypeError)):
        ImpedanceRepository(database, "board-a").save(_configuration(), revision)
    assert database.saves == []


def test_save_retains_enabled_draft_without_authorizing_export() -> None:
    """An explicit draft save must not silently disable the requested report."""
    database = MemoryDatabase(_record(Config()))
    repository = ImpedanceRepository(database, "board-a")
    draft = Config(enabled=True)
    assert repository.save(draft, 7) == 8
    assert repository.load() == (draft, 8)
    with pytest.raises(ValidationError, match="requires at least one specification"):
        service.prepare(draft, BoardSnapshot(("F.Cu", "B.Cu"), ()))


def test_real_database_roundtrip_isolation_and_edit_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two boards and two editors retain independent data with conflict detection."""
    name = "_test_impedance_service_database"
    path = Path(__file__).resolve().parents[1] / "impedance" / "database.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    database = module.ImpedanceDatabase(tmp_path / "jlcpcb" / "project.db")
    first_path, second_path = tmp_path / "a.kicad_pcb", tmp_path / "b.kicad_pcb"
    first_path.write_text("(kicad_pcb)", encoding="utf-8")
    second_path.write_text("(kicad_pcb)", encoding="utf-8")
    first_id = database.resolve_board(first_path)
    second_id = database.resolve_board(second_path)
    first = ImpedanceRepository(database, first_id)
    second = ImpedanceRepository(database, second_id)
    stale_editor = ImpedanceRepository(database, first_id)
    original = _review(_configuration(), _board())
    assert first.save(original, 0) == 1
    assert stale_editor.load() == (original, 1)
    assert second.load() == (Config(), 0)
    disabled = replace(original, enabled=False)
    assert first.save(disabled, 1) == 2
    with pytest.raises(module.ConfigConflictError):
        stale_editor.save(original, 1)
    assert first.load() == (disabled, 2)
    assert second.load() == (Config(), 0)


def test_disabled_prepare_does_not_analyze_or_import_workbook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary generation does not depend on impedance rendering or templates."""
    original_import = builtins.__import__

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        """Reject imports of the optional output stack during disabled preflight."""
        assert name not in {"workbook", "impedance.workbook", "wx", "pcbnew"}
        return original_import(name, *args, **kwargs)

    def unexpected_analysis(*args: Any, **kwargs: Any) -> None:
        """Fail immediately if an inactive feature analyzes board geometry."""
        pytest.fail("Disabled impedance configuration must not analyze the board")

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(service, "analyze", unexpected_analysis)
    assert service.prepare(Config(), _board(), layer_count=2) is None


def test_prepare_selects_sections_in_analysis_order() -> None:
    """Checkbox click order never changes the workbook's deterministic row order."""
    snapshot = _board()
    reviewed = _review(_configuration(), snapshot)
    included = (reviewed.included_section_ids[2], reviewed.included_section_ids[0])
    reviewed = replace(reviewed, included_section_ids=included)
    prepared = service.prepare(reviewed, snapshot, layer_count=4)
    assert prepared is not None
    assert [section.section_id for section in prepared.sections] == list(
        reversed(included)
    )
    assert prepared.config == reviewed
    assert prepared.snapshot == snapshot


@pytest.mark.parametrize("layer_count", [2, 6])
def test_prepare_rejects_fabrication_stack_mismatch(layer_count: int) -> None:
    """A workbook cannot describe a different stack from the fabrication export."""
    snapshot = _board()
    with pytest.raises(ValidationError):
        service.prepare(_review(_configuration(), snapshot), snapshot, layer_count)


def test_prepare_requires_current_explicit_review() -> None:
    """Matching traces alone do not authorize documentation of impedance intent."""
    with pytest.raises(ValidationError):
        service.prepare(_configuration(), _board())
    reviewed = replace(_review(_configuration(), _board()), included_section_ids=())
    with pytest.raises(ValidationError):
        service.prepare(reviewed, _board())


def test_prepare_checks_template_before_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Template corruption blocks preflight before Gerber generation begins."""
    error = workbook.WorkbookError("missing vendor template")

    def invalid_template(*args: Any, **kwargs: Any) -> None:
        """Report the underlying template error without creating a file."""
        raise error

    monkeypatch.setattr(workbook, "validate_template", invalid_template)
    with pytest.raises(workbook.WorkbookError) as caught:
        service.prepare(_review(_configuration(), _board()), _board())
    assert caught.value is error


def test_prepare_blocks_template_row_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An oversized report cannot be silently truncated by downstream writers."""
    monkeypatch.setattr(service, "MAX_ROWS", 2)
    with pytest.raises((ValidationError, workbook.WorkbookError)):
        service.prepare(_review(_configuration(), _board()), _board())


def test_current_plan_accepts_unchanged_board(plan: service.ExportPlan) -> None:
    """Checking a stable board is an idempotent pre-export operation."""
    assert service.validate_current(plan, plan.config, plan.snapshot, 4) is None


def test_current_plan_accepts_reordered_board_enumeration(
    plan: service.ExportPlan,
) -> None:
    """SWIG iteration order does not invalidate an otherwise unchanged board."""
    reordered = replace(plan.snapshot, traces=tuple(reversed(plan.snapshot.traces)))
    assert service.validate_current(plan, plan.config, reordered, 4) is None


@pytest.mark.parametrize("field", ["target", "width"])
def test_prepare_validates_vendor_numeric_bounds(field: str) -> None:
    """Reject unsupported workbook values before rendering or fabrication begins."""
    config, snapshot = _configuration(), _board()
    spec = config.specifications[0]
    if field == "target":
        spec = replace(spec, target_ohms="1e20")
    else:
        width = 10**12 + 1
        snapshot = replace(
            snapshot,
            traces=tuple(replace(trace, width_nm=width) for trace in snapshot.traces),
        )
    config = _review(replace(config, specifications=(spec,)), snapshot)
    with pytest.raises(workbook.WorkbookError):
        service.prepare(config, snapshot)


def test_prepare_preserves_coplanar_ground_gap_and_inner_layer_references() -> None:
    """Preflight and both outputs share canonical, per-layer manufacturing fields."""
    original = _configuration()
    spec = replace(
        original.specifications[0],
        kind="single_ended_coplanar",
        layer_settings=tuple(
            replace(settings, ground_gap_nm=200000)
            for settings in original.specifications[0].layer_settings
        ),
    )
    config = _review(replace(original, specifications=(spec,)), _board())
    plan = service.prepare(config, _board())
    assert plan is not None
    captures = tuple(service.CapturedImage.from_bytes(_png()) for _ in plan.sections)
    rows = service.report_rows(plan, captures)
    assert len(rows) == 3
    assert all(row.ground_gap_nm == 200000 for row in rows)
    assert all(row.reference_layers == ("In1.Cu", "In2.Cu") for row in rows)
    assert all(row.physical_reference_layers == ("L2", "L3") for row in rows)
    assert all(row.kind == "single_ended_coplanar" for row in rows)


@pytest.mark.parametrize(
    "change", ["disabled", "selection", "target", "geometry", "context"]
)
def test_current_plan_rejects_changes_even_after_new_review(
    plan: service.ExportPlan, change: str
) -> None:
    """Export remains tied to the configuration captured at initial preflight."""
    config, snapshot = plan.config, plan.snapshot
    if change == "disabled":
        config = replace(config, enabled=False)
    elif change == "selection":
        config = replace(config, included_section_ids=config.included_section_ids[:-1])
    elif change == "target":
        config = replace(
            config,
            specifications=(replace(config.specifications[0], target_ohms="60"),),
        )
        config = _review(config, snapshot)
    elif change == "geometry":
        trace = snapshot.traces[0]
        snapshot = replace(
            snapshot,
            traces=(
                replace(trace, points=((0, 5000000), (2000000, 5000000))),
                *snapshot.traces[1:],
            ),
        )
        config = _review(config, snapshot)
    else:
        snapshot = replace(snapshot, context_digest="new-zone-fill")
        config = _review(config, snapshot)
    with pytest.raises(ValidationError):
        service.validate_current(plan, config, snapshot, 4)


def test_export_reuses_renderer_maps_stack_and_keeps_outputs_in_scratch(
    plan: service.ExportPlan, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Render each included section once and pass physically numbered layers to Excel."""
    board, pcbnew = object(), ModuleType("fake_pcbnew")
    factories: list[tuple[object, ModuleType]] = []
    render_calls: list[tuple[Section, Path, int, int]] = []
    writer_calls: list[tuple[list[service.ReportRow], Path]] = []
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    class Renderer:
        """Produce test images while retaining exact section and viewport arguments."""

        def render(
            self, section: Section, destination: Path, width_px: int, height_px: int
        ) -> Path:
            """Write one stand-in PNG for the injected workbook writer."""
            render_calls.append((section, destination, width_px, height_px))
            destination.write_bytes(_png())
            return destination

    def renderer_factory(current_board: object, module: ModuleType) -> Renderer:
        """Count renderer construction independently from individual sections."""
        factories.append((current_board, module))
        return Renderer()

    def writer(rows: list[service.ReportRow], destination: Path) -> Path:
        """Record mapped manufacturing fields and provide a contained report file."""
        writer_calls.append((list(rows), destination))
        destination.write_bytes(b"test workbook")
        return destination

    original_import = builtins.__import__

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        """Injected dependencies should never load the live KiCad or wx runtime."""
        assert name != "pcbnew" and not name.startswith("wx")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    output = service.export_reports(
        plan, board, pcbnew, scratch, renderer_factory, writer
    )
    assert output.workbook == scratch / workbook.OUTPUT_FILENAME
    assert factories == [(board, pcbnew)]
    assert [call[0] for call in render_calls] == list(plan.sections)
    assert all(call[2:] == (800, 420) for call in render_calls)
    images = [call[1] for call in render_calls]
    assert len(set(images)) == len(plan.sections)
    assert all(path.parent == scratch and path.suffix == ".png" for path in images)
    assert len(writer_calls) == 1
    rows, destination = writer_calls[0]
    assert destination == output.workbook
    assert [row.physical_signal_layer for row in rows] == ["L1", "L1", "L4"]
    assert all(row.physical_reference_layers == ("L2", "L3") for row in rows)
    assert all(row.target_ohms == "50.25" and row.width_nm == 150000 for row in rows)
    assert all(row.kind == "single_ended" and row.spacing_nm is None for row in rows)
    assert [row.image.data for row in rows] == [path.read_bytes() for path in images]
    assert output.html_report.is_file()
    assert set(scratch.iterdir()) == {output.workbook, output.html_report, *images}
    assert list(tmp_path.iterdir()) == [scratch]


@pytest.mark.parametrize("failure", ["none", "render", "missing_image", "writer"])
def test_export_releases_native_renderer_before_workbook_on_every_path(
    plan: service.ExportPlan, tmp_path: Path, failure: str
) -> None:
    """Native snapshots end at the rendering boundary, including partial failures."""

    class OwnedRenderer:
        """Represent resources that cannot be used after explicit release."""

        def __init__(self) -> None:
            self.closed = False
            self.close_calls = 0

        def render(
            self, section: Section, destination: Path, width_px: int, height_px: int
        ) -> Path:
            """Provide an image or simulate a failure after native allocation."""
            assert not self.closed
            if failure == "render":
                raise RuntimeError("native plot failed")
            if failure != "missing_image":
                destination.write_bytes(_png())
            return destination

        def close(self) -> None:
            """Release the native allocation once before workbook processing."""
            self.close_calls += 1
            self.closed = True

    renderer = OwnedRenderer()

    def factory(board: Any, module: Any) -> OwnedRenderer:
        """Expose the same stateful resource to the complete export workflow."""
        return renderer

    def writer(rows: Any, destination: Path) -> Path:
        """Native board resources must already be released before writing Excel."""
        assert renderer.closed and renderer.close_calls == 1
        if failure == "writer":
            raise RuntimeError("workbook failed")
        destination.write_bytes(b"workbook")
        return destination

    try:
        if failure == "none":
            service.export_reports(plan, object(), object(), tmp_path, factory, writer)
        else:
            with pytest.raises((RuntimeError, ValidationError)):
                service.export_reports(
                    plan, object(), object(), tmp_path, factory, writer
                )
    finally:
        assert renderer.closed and renderer.close_calls == 1


def test_renderer_failure_skips_writer_and_propagates(
    plan: service.ExportPlan,
    tmp_path: Path,
) -> None:
    """A partial set of snippets must never produce an incomplete workbook."""
    failure = RuntimeError("plot failed")
    written: list[Path] = []

    class Renderer:
        """Fail immediately to exercise export's incomplete-report path."""

        def render(self, *args: Any, **kwargs: Any) -> Path:
            """Propagate the concrete rendering exception to the caller."""
            raise failure

    def renderer_factory(*args: Any) -> Renderer:
        """Supply a renderer without importing pcbnew or wx."""
        return Renderer()

    def writer(rows: list[service.ReportRow], destination: Path) -> Path:
        """Record erroneous publication attempts after rendering failure."""
        written.append(destination)
        return destination

    with pytest.raises(RuntimeError) as caught:
        service.export_reports(
            plan,
            object(),
            ModuleType("fake_pcbnew"),
            tmp_path,
            renderer_factory,
            writer,
        )
    assert caught.value is failure
    assert written == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure_stage", ["second_row", "cancel", "close"])
def test_export_cleanup_after_partial_render_cancellation_and_close_failure(
    plan: service.ExportPlan, tmp_path: Path, failure_stage: str
) -> None:
    """Partial images or failed cleanup cannot proceed to workbook publication."""
    error = (
        KeyboardInterrupt("render cancelled")
        if failure_stage == "cancel"
        else RuntimeError(f"failed at {failure_stage}")
    )
    writes: list[Path] = []

    class Renderer:
        """Retain state across rows and expose one observable cleanup attempt."""

        def __init__(self) -> None:
            self.render_calls = 0
            self.close_calls = 0
            self.closed = False

        def render(
            self, section: Section, destination: Path, width_px: int, height_px: int
        ) -> Path:
            """Leave one successful snippet before simulating interrupted work."""
            assert not self.closed
            self.render_calls += 1
            if self.render_calls == 2 and failure_stage != "close":
                raise error
            destination.write_bytes(_png())
            return destination

        def close(self) -> None:
            """Do not claim resources were released if native cleanup itself fails."""
            self.close_calls += 1
            if failure_stage == "close":
                raise error
            self.closed = True

    renderer = Renderer()

    def factory(board: Any, module: Any) -> Renderer:
        """Provide the stateful renderer used throughout the export."""
        return renderer

    def writer(rows: Any, destination: Path) -> Path:
        """Record any invalid attempt to publish an incomplete report."""
        writes.append(destination)
        return destination

    with pytest.raises(type(error)) as caught:
        service.export_reports(plan, object(), object(), tmp_path, factory, writer)
    assert caught.value is error
    assert renderer.close_calls == 1
    assert renderer.closed is (failure_stage != "close")
    assert renderer.render_calls == (
        len(plan.sections) if failure_stage == "close" else 2
    )
    assert len(list(tmp_path.glob("*.png"))) == (
        len(plan.sections) if failure_stage == "close" else 1
    )
    assert writes == []
    assert not (tmp_path / workbook.OUTPUT_FILENAME).exists()


def test_writer_failure_propagates_without_false_success(
    plan: service.ExportPlan,
    tmp_path: Path,
) -> None:
    """Workbook validation or disk failures cannot masquerade as successful export."""
    failure = workbook.WorkbookError("image invalid")

    class Renderer:
        """Supply generated paths so the workbook failure is the first error."""

        def render(
            self, section: Section, destination: Path, width_px: int, height_px: int
        ) -> Path:
            """Create a test image at the service-provided location."""
            destination.write_bytes(_png())
            return destination

    def renderer_factory(*args: Any) -> Renderer:
        """Supply a pure test adapter."""
        return Renderer()

    def writer(rows: list[service.ReportRow], destination: Path) -> Path:
        """Simulate a workbook failure before the final document is published."""
        raise failure

    with pytest.raises(workbook.WorkbookError) as caught:
        service.export_reports(
            plan,
            object(),
            ModuleType("fake_pcbnew"),
            tmp_path,
            renderer_factory,
            writer,
        )
    assert caught.value is failure
    assert not (tmp_path / workbook.OUTPUT_FILENAME).exists()


@pytest.mark.parametrize(
    "failure", ["renderer_path", "renderer_missing", "writer_path", "writer_missing"]
)
def test_export_rejects_unexpected_adapter_outputs(
    plan: service.ExportPlan,
    tmp_path: Path,
    failure: str,
) -> None:
    """Adapters must create and return their exact requested scratch destinations."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    writer_calls: list[Path] = []

    class Renderer:
        """Optionally misreport or omit a rendered image to exercise adapter guards."""

        def render(
            self, section: Section, destination: Path, width_px: int, height_px: int
        ) -> Path:
            """Leave all writes inside scratch even when returning an invalid path."""
            if failure != "renderer_missing":
                destination.write_bytes(_png())
            return (
                tmp_path / "outside.png" if failure == "renderer_path" else destination
            )

    def renderer_factory(*args: Any) -> Renderer:
        """Supply a deterministic adapter requiring no native dependencies."""
        return Renderer()

    def writer(rows: list[service.ReportRow], destination: Path) -> Path:
        """Optionally omit or misreport workbook publication."""
        writer_calls.append(destination)
        if failure != "writer_missing":
            destination.write_bytes(b"test workbook")
        return tmp_path / "outside.xlsx" if failure == "writer_path" else destination

    with pytest.raises(ValidationError):
        service.export_reports(
            plan, object(), ModuleType("fake_pcbnew"), scratch, renderer_factory, writer
        )
    assert bool(writer_calls) == failure.startswith("writer")
    assert list(tmp_path.iterdir()) == [scratch]


def test_export_requires_existing_scratch_directory(
    plan: service.ExportPlan,
    tmp_path: Path,
) -> None:
    """Caller-owned temporary storage must exist before any renderer is created."""
    scratch = tmp_path / "missing"

    def unexpected_factory(*args: Any) -> Any:
        """Prevent a missing destination from starting optional rendering machinery."""
        pytest.fail("Missing scratch directory must be rejected before rendering")

    with pytest.raises(ValidationError):
        service.export_reports(
            plan, object(), ModuleType("fake_pcbnew"), scratch, unexpected_factory
        )
    assert not scratch.exists()
