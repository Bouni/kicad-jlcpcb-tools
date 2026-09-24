"""250 ms Python workflow budgets on a representative routed workbook.

Run with the repository myenv and normal nonnative tests. Every measured sample
includes real matching, model validation, JSON/SQLite storage, dialog construction
or its bound Save handler, queued UI callbacks, and teardown. Native wx painting,
KiCad snapshot acquisition, zone fill, and raster rendering remain explicit native
boundaries; these tests cannot establish their platform timing. No clock is faked.

The budget is the maximum of three measured samples, not an average. The temporary
database uses normal SQLite durability. Fixture generation, prior approval, and
optional warm-cache preparation are deliberately outside the measured interval.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
import importlib
from pathlib import Path
import subprocess
from time import perf_counter
from types import SimpleNamespace
from typing import TYPE_CHECKING, Optional

import pytest

from tests.impedance_performance_fixtures import (
    CLASS_COUNT,
    NETS_PER_CLASS,
    PERFORMANCE_BUDGET_SECONDS,
    REPEATED_SAMPLES,
    SEGMENTS_PER_ROUTE,
    routed_workbook,
)
from tests.test_impedance_save_evidence import (
    constructor_api as _constructor_api,
    draft_api as _draft_api,
    save_api as _save_api,
)

constructor_api = _constructor_api
draft_api = _draft_api
save_api = _save_api

if TYPE_CHECKING:
    from impedance.dialog import ImpedanceDialog
    from impedance.model import Config
    from impedance.repository import ImpedanceRepository


@dataclass
class Measurements:
    """Observe actual calls without changing scheduling or returned values."""

    seconds: dict[str, list[float]] = field(default_factory=dict)

    def wrap(self, name: str, callback: Callable[..., object]) -> Callable[..., object]:
        """Time successful and failing calls using the real monotonic clock."""

        def measured(*args: object, **kwargs: object) -> object:
            started = perf_counter()
            try:
                return callback(*args, **kwargs)
            finally:
                self.seconds.setdefault(name, []).append(perf_counter() - started)

        return measured

    def report(self) -> dict[str, dict[str, object]]:
        """Retain per-function counts and totals for a useful budget failure."""
        return {
            name: {"calls": len(values), "seconds": sum(values), "max": max(values)}
            for name, values in self.seconds.items()
        }


def _instrument(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> Measurements:
    """Include table rebuilding, evidence, matching, and closure in diagnostics."""
    measurements = Measurements()
    for owner, attribute, label in (
        (env.api.dialog, "analyze", "matching"),
        (env.api.dialog.ImpedanceDialog, "_populate_specifications", "specifications"),
        (env.api.dialog.ImpedanceDialog, "_populate_sections", "workbook"),
        (env.api.dialog.ImpedanceDialog, "_fit_workbook_columns", "column_fit"),
        (env.api.dialog.ImpedanceDialog, "_verify_restored_capture_review", "evidence"),
        (env.api.dialog.ImpedanceDialog, "_cancel_calculation", "calculation_teardown"),
        (env.api.dialog.ReviewSession, "save_result", "validation"),
        (env.captures, "snapshot", "snapshot"),
        (env.captures, "verify_captures", "capture_verification"),
    ):
        monkeypatch.setattr(
            owner, attribute, measurements.wrap(label, getattr(owner, attribute))
        )
    original_clear = env.api.dv.DataViewListCtrl.DeleteAllItems
    measured_clear = measurements.wrap("specification_clears", original_clear)
    titles = tuple(title for title, _width in env.api.dialog._SPECIFICATION_COLUMNS)

    def clear(control: object) -> None:
        """Count the specification table, excluding shared-double preview lists."""
        actual = tuple(title for title, _width in getattr(control, "columns", ()))
        if actual == titles:
            measured_clear(control)
        else:
            original_clear(control)

    monkeypatch.setattr(env.api.dv.DataViewListCtrl, "DeleteAllItems", clear)
    return measurements


def _repository(env: SimpleNamespace, tmp_path: Path) -> ImpedanceRepository:
    """Use real durable settings access rather than a timing-only persistence fake."""
    package = env.api.dialog.__package__
    database_api = importlib.import_module(f"{package}.database")
    repository_api = importlib.import_module(f"{package}.repository")
    board_path = tmp_path / "representative.kicad_pcb"
    board_path.write_text("(kicad_pcb)", encoding="utf-8")
    database = database_api.ImpedanceDatabase(tmp_path / "project.db")
    board_id = database.resolve_board(board_path)
    return repository_api.ImpedanceRepository(database, board_id)


def _show_and_finish(env: SimpleNamespace, dialog: ImpedanceDialog) -> None:
    """Deliver first show, queued readiness, selected preview, and paint acknowledgment."""
    dialog.on_screen = True
    dialog.bindings[env.api.wx.EVT_SHOW, -1](
        SimpleNamespace(
            GetEventObject=lambda: dialog, IsShown=lambda: True, Skip=lambda: None
        )
    )
    env.api.drain()
    first_paint = dialog.bindings.get((env.api.wx.EVT_PAINT, -1))
    if first_paint is not None:
        first_paint(SimpleNamespace(Skip=lambda: None))
        env.api.drain()
    env.api.paint(dialog.preview_pane)
    if hasattr(dialog, "_capture_prepare_timer"):
        env.api.finish_preparation(dialog)


def _counts(env: SimpleNamespace) -> dict[str, int]:
    """Report expensive source, appearance, capture, and matching operations."""
    return {
        name: len(getattr(env.state, name))
        for name in (
            "source_reads",
            "appearance_reads",
            "provider_calls",
            "renders",
            "analyses",
        )
    }


def test_performance_fixture_has_representative_routing(
    save_api: SimpleNamespace,
) -> None:
    """Keep the budget from silently shrinking to a one-track example."""
    workbook = routed_workbook(save_api.api, save_api.api.capture.sha256)
    snapshot = workbook.snapshot
    assert len(workbook.draft.specifications) == CLASS_COUNT
    assert len(snapshot.net_class_memberships) == CLASS_COUNT * NETS_PER_CLASS + 1
    assert (
        len(snapshot.traces)
        == CLASS_COUNT * NETS_PER_CLASS * (SEGMENTS_PER_ROUTE + 1) + 96
    )
    assert len(snapshot.layers) == 4
    assert len(snapshot.copper_features) == CLASS_COUNT * NETS_PER_CLASS
    assert workbook.draft.stackup.calculator_id
    assert any(trace.is_native_arc for trace in snapshot.traces)
    assert len({trace.width_nm for trace in snapshot.traces}) >= 3
    assert len(workbook.approved.review_tracking.images) >= CLASS_COUNT * NETS_PER_CLASS
    assert len(workbook.approved.review_tracking.layers) == CLASS_COUNT * 2
    session = save_api.api.dialog.ReviewSession(workbook.approved, snapshot)
    analysis = session.refresh()
    assert session.approved
    assert sum(bool(section.exclusion_reason) for section in analysis.sections) == 12
    assert all(
        section.section_id not in session.included
        for section in analysis.sections
        if section.exclusion_reason
    )


def test_draft_performance_fixture_needs_no_dialog_or_saved_captures(
    save_api: SimpleNamespace,
) -> None:
    """First-use inputs must be constructible without loading the measured UI."""
    from tests import impedance_performance_fixtures as fixtures

    snapshot, config = fixtures.routed_draft(SimpleNamespace(model=save_api.api.model))
    assert len(snapshot.traces) == 492
    assert len(config.specifications) == CLASS_COUNT
    assert len(config.width_results) == CLASS_COUNT * 2
    assert config.stackup is not None
    assert not config.review_tracking.images
    assert not config.review_tracking.layers


@pytest.mark.parametrize("failure", ["budget", "process"])
def test_first_use_native_gate_retains_every_fresh_sample_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    """A slow or failed child cannot hide evidence or trigger replacement samples."""
    import json

    from tests import test_impedance_performance_native as native

    monkeypatch.setenv("KICAD_IMPEDANCE_NATIVE_BUDGET_PROFILE", "strict")
    processes = []
    properties = {}

    def child(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command[1:3] == ["-m", "tests.test_impedance_performance_native"]
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert environment["KICAD_IMPEDANCE_NATIVE_BUDGET_PROFILE"] == "strict"
        directory = Path(command[3])
        processes.append(directory)
        (directory / "samples.json").write_text(
            json.dumps(
                [
                    {
                        "cold": True,
                        "open_seconds": 0.451 if failure == "budget" else 0.001,
                        "save_close_seconds": 0.001,
                    }
                ]
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            command, 1 if failure == "process" else 0, "", "child failure"
        )

    monkeypatch.setattr(native.subprocess, "run", child)
    with pytest.raises(AssertionError):
        native.test_native_kicad_first_use_configure_budgets(
            tmp_path, lambda name, value: properties.update({name: value})
        )
    assert len(set(processes)) == REPEATED_SAMPLES
    assert [
        sample["fresh_process_sample"]
        for sample in properties["native_performance_samples"]
    ] == list(range(REPEATED_SAMPLES))


@pytest.mark.parametrize(
    "open_seconds,save_seconds,accepted",
    [(0.450, 0.250, True), (0.450001, 0.001, False), (0.001, 0.250001, False)],
)
def test_first_use_native_gate_keeps_separate_opening_and_save_limits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    open_seconds: float,
    save_seconds: float,
    accepted: bool,
) -> None:
    """Allow 450 ms first use without relaxing Save or discarding slow samples."""
    import json

    from tests import test_impedance_performance_native as native

    monkeypatch.setenv("KICAD_IMPEDANCE_NATIVE_BUDGET_PROFILE", "strict")
    properties = {}

    def child(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        (Path(command[3]) / "samples.json").write_text(
            json.dumps(
                [
                    {
                        "cold": True,
                        "open_seconds": open_seconds,
                        "save_close_seconds": save_seconds,
                    }
                ]
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(native.subprocess, "run", child)

    def check() -> None:
        native.test_native_kicad_first_use_configure_budgets(
            tmp_path, lambda name, value: properties.update({name: value})
        )

    if accepted:
        check()
    else:
        with pytest.raises(AssertionError):
            check()
    assert len(properties["native_performance_samples"]) == REPEATED_SAMPLES


@pytest.mark.parametrize("variant", [None, "Assembly A"])
@pytest.mark.parametrize("approved", [False, True], ids=["draft", "approved"])
@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
def test_open_workbook_maximum_250ms_without_duplicate_work(
    save_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    record_property: Callable[[str, object], None],
    variant: Optional[str],
    approved: bool,
    warm: bool,
) -> None:
    """Time settings load through usable rows and all immediately queued work."""
    env = save_api
    workbook = routed_workbook(env.api, env.api.capture.sha256)
    env.state.snapshot = workbook.snapshot
    env.state.variant = variant
    repository = _repository(env, tmp_path)
    repository.save(workbook.approved if approved else workbook.draft, 0)
    measurements = _instrument(env, monkeypatch)
    durations = []
    samples = []
    for _sample in range(REPEATED_SAMPLES):
        if not warm:
            env.captures.close()
        elif approved:
            # Explicitly warm the real capture ledger without manufacturing views.
            session = env.api.dialog.ReviewSession(workbook.approved, workbook.snapshot)
            for section in session.refresh().sections:
                if section.section_id in session.included:
                    env.captures.preview(section)
        env.reset_counts()
        measurements.seconds.clear()
        started = perf_counter()
        config, _revision = measurements.wrap("settings_load", repository.load)()
        dialog = env.main(config)
        constructor_elapsed = perf_counter() - started
        before_paint = _counts(env)
        _show_and_finish(env, dialog)
        durations.append(perf_counter() - started)
        samples.append(
            {
                "seconds": durations[-1],
                "constructor_seconds": constructor_elapsed,
                "before_first_paint": before_paint,
                "counts": _counts(env),
                "functions": measurements.report(),
            }
        )
        assert dialog.session.analysis is not None
        assert dialog._row_ids
        assert dialog.session.approved is approved
        dialog.emit(env.api.wx.EVT_BUTTON, env.api.wx.ID_CANCEL)
        env.api.drain()
    record_property("opening_samples", samples)
    assert max(durations) <= PERFORMANCE_BUDGET_SECONDS, samples
    assert all(sample["counts"]["analyses"] == 1 for sample in samples), samples
    assert all(sample["before_first_paint"]["renders"] == 0 for sample in samples), (
        samples
    )
    assert all(
        sample["functions"]["specification_clears"]["calls"] == 1 for sample in samples
    ), samples


@pytest.mark.parametrize("variant", [None, "Assembly A"])
@pytest.mark.parametrize("scenario", ["unchanged", "png_evicted", "edited"])
def test_save_and_close_maximum_250ms_reuses_verified_work(
    save_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    record_property: Callable[[str, object], None],
    variant: Optional[str],
    scenario: str,
) -> None:
    """Measure bound Save through durable SQLite write, cancellation, and dismissal."""
    env = save_api
    workbook = routed_workbook(env.api, env.api.capture.sha256)
    env.state.snapshot = workbook.snapshot
    env.state.variant = variant
    repository = _repository(env, tmp_path)
    repository.save(workbook.approved, 0)
    measurements = _instrument(env, monkeypatch)
    samples = []
    for _sample in range(REPEATED_SAMPLES):
        config, revision = repository.load()
        dialog = env.main(config)
        _show_and_finish(env, dialog)
        assert dialog.session.approved
        if scenario == "png_evicted":
            env.captures.images.clear()
            env.captures.image_bytes = 0
        elif scenario == "edited":
            dialog.session.config = replace(dialog.session.config, enabled=False)

        def persist(candidate: Config, expected_revision: int = revision) -> None:
            repository.save(candidate, expected_revision)

        dialog.save_config = measurements.wrap("settings_write", persist)
        env.reset_counts()
        measurements.seconds.clear()
        started = perf_counter()
        dialog.emit(env.api.wx.EVT_BUTTON, env.api.wx.ID_OK)
        env.api.drain()
        samples.append(
            {
                "seconds": perf_counter() - started,
                "counts": _counts(env),
                "functions": measurements.report(),
            }
        )
        assert dialog.modal_result == env.api.wx.ID_OK
        assert dialog._closing
        assert dialog.preview_pane._closed
        assert not dialog._width_refresh_timer.IsRunning()
        assert not dialog._width_slow_timer.IsRunning()
        saved, saved_revision = repository.load()
        assert saved_revision == revision + 1
        assert saved == dialog.config
    record_property("save_close_samples", samples)
    assert max(sample["seconds"] for sample in samples) <= PERFORMANCE_BUDGET_SECONDS, (
        samples
    )
    assert all(
        sample["counts"]
        == {
            "source_reads": 1,
            "appearance_reads": 1,
            "provider_calls": 0,
            "renders": 0,
            "analyses": 0,
        }
        for sample in samples
    ), samples


@pytest.mark.parametrize("approved", [False, True], ids=["draft", "approved"])
def test_configure_entry_and_save_cleanup_maximum_250ms(
    save_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    record_property: Callable[[str, object], None],
    approved: bool,
) -> None:
    """Include actual toolbar integration, cache reads, identity checks, and cleanup."""
    env = save_api
    workbook = routed_workbook(env.api, env.api.capture.sha256)
    env.state.snapshot = workbook.snapshot
    repository = _repository(env, tmp_path)
    repository.save(workbook.approved if approved else workbook.draft, 0)
    integration = importlib.import_module(f"{env.api.dialog.__package__}.integration")
    board_path = tmp_path / "representative.kicad_pcb"
    source = SimpleNamespace(GetFileName=lambda: str(board_path))
    preparations = []

    def prepare_copper() -> bool:
        """Include the native preparation boundary in actual Configure dispatch."""
        preparations.append(True)
        return True

    parent = SimpleNamespace(
        pcbnew=SimpleNamespace(GetBoard=lambda: source),
        prepare_copper_zones=prepare_copper,
        settings={},
    )
    controls = integration.ImpedanceControls(
        parent, SimpleNamespace(AddControl=lambda _control: None)
    )
    controls.attach_store(SimpleNamespace(dbfile=repository.database.path))

    def snapshot() -> object:
        """Keep real source identity validation around the native geometry seam."""
        controls.check_board()
        env.state.source_reads.append(workbook.snapshot)
        return workbook.snapshot

    monkeypatch.setattr(controls, "_snapshot", snapshot)
    monkeypatch.setattr(controls, "_live_snapshot", snapshot)
    measurements = _instrument(env, monkeypatch)
    samples = []
    opened = []
    clock = SimpleNamespace(started=0.0, save_started=0.0)

    def modal(dialog: ImpedanceDialog) -> int:
        """Run the real constructor and handlers inside actual on_configure."""
        opened.append(dialog)
        _show_and_finish(env, dialog)
        samples.append(
            {
                "open_seconds": perf_counter() - clock.started,
                "opening_counts": _counts(env),
                "opening_functions": measurements.report(),
            }
        )
        assert dialog.session.approved is approved
        env.reset_counts()
        measurements.seconds.clear()
        clock.save_started = perf_counter()
        dialog.emit(env.api.wx.EVT_BUTTON, env.api.wx.ID_OK)
        env.api.drain()
        assert dialog.modal_result is not None
        return dialog.modal_result

    monkeypatch.setattr(
        env.api.dialog.ImpedanceDialog, "ShowModal", modal, raising=False
    )
    for _sample in range(REPEATED_SAMPLES):
        env.reset_counts()
        measurements.seconds.clear()
        clock.started = perf_counter()
        accepted = controls.on_configure(None)
        finished = perf_counter()
        assert accepted, env.api.errors
        samples[-1].update(
            save_close_seconds=finished - clock.save_started,
            save_counts=_counts(env),
            save_functions=measurements.report(),
        )
        assert not opened[-1].alive
        assert opened[-1].preview_pane._closed
        assert controls.revision == repository.load()[1]
    record_property("configure_entry_samples", samples)
    assert preparations == [True] * REPEATED_SAMPLES
    assert (
        max(sample["open_seconds"] for sample in samples) <= PERFORMANCE_BUDGET_SECONDS
    ), samples
    assert (
        max(sample["save_close_seconds"] for sample in samples)
        <= PERFORMANCE_BUDGET_SECONDS
    ), samples
    assert all(sample["opening_counts"]["analyses"] == 1 for sample in samples), samples
    assert all(
        sample["save_counts"]
        == {
            "source_reads": 1,
            "appearance_reads": 1 if approved else 0,
            "provider_calls": 0,
            "renders": 0,
            "analyses": 0,
        }
        for sample in samples
    ), samples
