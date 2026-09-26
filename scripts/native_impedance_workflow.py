"""Actual Linux plugin workflow with isolated files and deterministic supplier data.

Only the catalog and remote calculator responses are substituted. Native board,
wx controls, modal loops, SQLite, Gerber/Excellon plotting, report rendering, and
atomic publication use production implementations. Never run inside the editor.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
import hashlib
import importlib
from io import BytesIO
import json
import logging
from pathlib import Path
import sqlite3
import sys
from threading import Event
import time
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch
from zipfile import BadZipFile, ZipFile

WORKBOOK = "Required_impedance_control.xlsx"
HTML = "Required_impedance_control.html"


def check_generated_archive(path: Path, enabled: bool) -> dict[str, Any]:
    """Reject stand-ins, stale documents and corrupt native fabrication artifacts."""
    try:
        with ZipFile(path) as archive:
            names = archive.namelist()
            if archive.testzip() is not None or len(names) != len(set(names)):
                raise ValueError(
                    "Fabrication archive is corrupt or has duplicate names"
                )
            if any(name.endswith((".kicad_pcb", ".kicad_pro")) for name in names):
                raise ValueError("A source PCB cannot stand in for fabrication output")
            gerbers = [name for name in names if name.endswith(".gbr")]
            drills = [name for name in names if name.endswith(".drl")]
            if not gerbers or not drills:
                raise ValueError(
                    "Fabrication archive is missing Gerber or Excellon output"
                )
            for name in gerbers:
                data = archive.read(name)
                if b"%FSL" not in data or b"M02*" not in data:
                    raise ValueError("Invalid Gerber data: " + name)
            for name in drills:
                data = archive.read(name)
                if not data.startswith(b"M48") or b"M30" not in data:
                    raise ValueError("Invalid Excellon data: " + name)
            images = 0
            if enabled:
                if WORKBOOK not in names or HTML not in names:
                    raise ValueError(
                        "Enabled fabrication archive is missing impedance reports"
                    )
                with ZipFile(BytesIO(archive.read(WORKBOOK))) as workbook:
                    media = [
                        name
                        for name in workbook.namelist()
                        if name.startswith("xl/media/")
                    ]
                    if workbook.testzip() is not None or not media:
                        raise ValueError(
                            "Workbook is missing embedded images or is corrupt"
                        )
                    if any(
                        not workbook.read(name).startswith(b"\x89PNG\r\n\x1a\n")
                        for name in media
                    ):
                        raise ValueError("Workbook contains an invalid embedded PNG")
                    images = len(media)
                if b"data:image/png;base64," not in archive.read(HTML):
                    raise ValueError("HTML report is missing embedded captures")
            elif WORKBOOK in names or HTML in names:
                raise ValueError(
                    "Disabled regeneration retained stale impedance reports"
                )
            return {
                "gerbers": len(gerbers),
                "drills": len(drills),
                "workbook_images": images,
                "enabled": enabled,
            }
    except BadZipFile as error:
        raise ValueError("Invalid fabrication or workbook ZIP") from error


def wait_until(
    app: Any, condition: Callable[[], bool], description: str, timeout: float = 30.0
) -> None:
    """Pump genuine wx events with a bounded wait for asynchronous transitions."""
    import wx

    if condition():
        return
    loop = wx.GUIEventLoop()
    deadline = time.monotonic() + timeout
    failures: list[BaseException] = []

    def check() -> None:
        try:
            done = condition() or time.monotonic() >= deadline
        except BaseException as error:
            failures.append(error)
            done = True
        if done:
            loop.ScheduleExit(0)
        else:
            timer.Start(10)

    timer = wx.CallLater(10, check)
    try:
        loop.Run()
    finally:
        timer.Stop()
    if failures:
        raise failures[0]
    if not condition():
        raise RuntimeError(description)


@contextmanager
def interact_modal(
    wx: Any, dialog_type: type, action: Callable[[Any], None]
) -> Iterator[list[Any]]:
    """Drive the real modal loop while propagating callback errors to the runner."""
    native_show = dialog_type.ShowModal
    visited = []
    failures = []

    def show(dialog: Any) -> int:
        visited.append(dialog)

        def drive() -> None:
            try:
                action(dialog)
            except BaseException as error:
                failures.append(error)
            finally:
                if dialog and dialog.IsModal():
                    dialog.EndModal(wx.ID_CANCEL)

        timer = wx.CallLater(200, drive)
        try:
            return native_show(dialog)
        finally:
            timer.Stop()

    with patch.object(dialog_type, "ShowModal", show):
        yield visited
    if failures:
        raise failures[0]
    if len(visited) != 1:
        raise RuntimeError("Expected exactly one native " + dialog_type.__name__)


def source_contents(board: Any) -> dict[Path, bytes]:
    """Snapshot the disposable PCB and project; rendering must not rewrite either."""
    source = Path(board.GetFileName())
    return {
        path: path.read_bytes()
        for path in (
            source.with_suffix(suffix)
            for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru")
        )
        if path.is_file()
    }


def output_contents(directory: Path) -> dict[str, bytes]:
    """Capture all published fabrication outputs, not only the ZIP size or name."""
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def plugin_modules(root: Path) -> SimpleNamespace:
    """Import the uninstalled plugin package without running its ActionPlugin hook."""
    name = "_native_impedance_plugin"
    package = ModuleType(name)
    package.__path__ = [str(root)]
    sys.modules[name] = package
    sys.path.insert(0, str(root / "lib"))
    return SimpleNamespace(
        **{
            key: importlib.import_module(name + "." + target)
            for key, target in {
                "main": "mainwindow",
                "model": "impedance.model",
                "dialog": "impedance.dialog",
                "picker": "impedance.stackup_dialog",
                "stackup": "impedance.stackup_model",
                "calculator": "impedance.jlcpcb_calculator",
                "catalog": "impedance.jlcpcb_stackups",
                "archive": "fabrication_archive",
                "columns": "partselector_columns",
                "adapter": "impedance.pcbnew_adapter",
                "library": "library",
            }.items()
        }
    )


def prepare_settings(root: Path, output: Path, modules: Any, board: Any) -> Path:
    """Supply an empty real SQLite parts catalog without external supplier access."""
    settings_dir = output / "settings"
    catalog_dir = output / "catalog"
    settings_dir.mkdir()
    catalog_dir.mkdir()
    settings = json.loads((root / "default_settings.json").read_text(encoding="utf-8"))
    settings["library"].update(data_path=str(catalog_dir), selected_library="empty")
    settings["gerber"].update(force_drc=False, fill_zones=False)
    settings["general"]["order_number"] = False
    settings["part_preferences"].update(
        fill_empty_lcsc_assignments_on_open=False, remember_lcsc_assignments=False
    )
    (settings_dir / "default_settings.json").write_text(
        json.dumps(settings), encoding="utf-8"
    )
    fields = tuple(
        dict.fromkeys((*modules.columns.DB_FIELDS, "Second Category", "Solder Joint"))
    )
    with sqlite3.connect(catalog_dir / "empty-parts-fts5.db") as connection:
        connection.execute(
            "CREATE VIRTUAL TABLE parts USING fts5("
            + ", ".join('"' + field + '"' for field in fields)
            + ")"
        )
        connection.execute(
            'CREATE TABLE categories ("First Category" TEXT, "Second Category" TEXT)'
        )
    # A local empty correction table intentionally selects project corrections;
    # global corrections would start a separate optional supplier seed download.
    database = Path(board.GetFileName()).parent / "jlcpcb" / "project.db"
    database.parent.mkdir(exist_ok=True)
    with sqlite3.connect(database) as connection:
        modules.library.Library._correction_schema(connection)
    return settings_dir


def sample_stackup(modules: Any, layers: tuple[str, ...]) -> Any:
    """Return labelled deterministic physical construction for the native UI lane."""
    layer_type = modules.stackup.StackupLayer
    construction = []
    for index, layer in enumerate(layers):
        if index:
            construction.append(
                layer_type(f"Dielectric {index}", "core", "0.25", "FR4", "4.2")
            )
        construction.append(layer_type(layer, "copper", "0.035", "Copper"))
    return modules.stackup.Stackup(
        "native-sample",
        "Deterministic native acceptance stackup",
        len(layers),
        "1.6",
        "1",
        "1",
        preferred=True,
        charge_status="none",
        layers=tuple(construction),
        calculator_id="native-sample",
        retrieved_at_utc="2026-09-23T12:00:00.000000Z",
    )


class FixtureBoardProvider:
    """Expose only the worker-owned board while delegating all native KiCad APIs."""

    def __init__(self, pcbnew: Any, board: Any) -> None:
        self.pcbnew, self.board = pcbnew, board

    def get_pcbnew(self) -> "FixtureBoardProvider":
        """Return the real API with only board lookup scoped to our fixture."""
        return self

    def GetBoard(self) -> Any:
        """Return the disposable worker-owned board."""
        return self.board

    def __getattr__(self, name: str) -> Any:
        """Delegate every other lookup to actual native bindings."""
        return getattr(self.pcbnew, name)


def generate_workflow(
    plan: Any, board: Any, pcbnew: Any, wx: Any, app: Any, output: Path
) -> dict[str, Any]:
    """Run the modeless plugin in a genuine main event loop, including deletion."""
    host = wx.Frame(None, title="Native impedance workflow host", size=(1280, 900))
    host.Show()
    results: list[dict[str, Any]] = []
    failures: list[BaseException] = []

    def exercise() -> None:
        try:
            results.append(_generate_workflow(plan, board, pcbnew, wx, app, output))
        except BaseException as error:
            failures.append(error)
        finally:
            app.ExitMainLoop()

    wx.CallAfter(exercise)
    try:
        app.MainLoop()
    finally:
        host.Destroy()
        wait_until(app, lambda: not bool(host), "Native workflow host did not close")
    if failures:
        raise failures[0]
    if not results:
        raise RuntimeError("Native main event loop exited before workflow completion")
    return results[0]


def _generate_workflow(
    plan: Any, board: Any, pcbnew: Any, wx: Any, app: Any, output: Path
) -> dict[str, Any]:
    """Configure, calculate, fail/retry, reopen and generate through real handlers."""
    from scripts.linux_native_impedance_smoke import (
        ROOT,
        event,
        require,
        stock_button,
        wait_for_preview,
    )

    modules = plugin_modules(ROOT)
    original_files = source_contents(board)
    original_snapshot = modules.adapter.snapshot_board(board, pcbnew)
    settings_dir = prepare_settings(ROOT, output, modules, board)
    construction = sample_stackup(modules, tuple(plan.snapshot.layers))
    alternative = replace(
        construction,
        stackup_id="native-alternative",
        name="Other deterministic stackup",
        thickness_mm="1.8",
    )
    provider = FixtureBoardProvider(pcbnew, board)
    messages: list[str] = []
    calculator_calls: list[tuple[str, str]] = []
    frames: list[Any] = []

    def fetch_catalog(layer_count: int, **_kwargs: Any) -> tuple[Any, ...]:
        require(
            layer_count == construction.layer_count,
            "Catalog requested wrong layer count",
        )
        return (construction, alternative)

    def calculate(
        _client: Any, stackup: Any, spec: Any, layer: str, **_kwargs: Any
    ) -> Any:
        calculator_calls.append((spec.spec_id, layer))
        digest = modules.stackup.calculation_fingerprint(stackup, spec, layer)
        return modules.stackup.WidthResult(
            spec.spec_id,
            layer,
            digest,
            "success",
            180000,
            "2026-09-23T12:00:00.000000Z",
            model="Deterministic native sample",
            assumptions=("Sample numerical result; no live supplier request.",),
            calculation_digest=hashlib.sha256((digest + "180000").encode()).hexdigest(),
        )

    def message(text: str, *_args: Any, **_kwargs: Any) -> int:
        messages.append(text)
        return wx.OK

    def new_frame() -> Any:
        frame = modules.main.JLCPCBTools(None, provider)
        frames.append(frame)
        frame.Show()
        app.Yield()
        require(
            not frame._project_storage_unavailable,
            "Native plugin storage initialization failed",
        )
        require(
            frame._impedance.configure_button.IsEnabled(),
            "Native Configure button disabled",
        )
        return frame

    def close_frame(frame: Any) -> None:
        require(frame.Close(), "Native plugin close was vetoed")
        wait_until(app, lambda: not bool(frame), "Native plugin frame did not close")

    def configure(frame: Any, action: Callable[[Any], None]) -> None:
        with interact_modal(wx, modules.dialog.ImpedanceDialog, action):
            event(wx, frame._impedance.configure_button, wx.EVT_BUTTON)
        require(not messages, "Configure unexpectedly failed: " + str(messages))

    def approve(dialog: Any) -> None:
        require(dialog.session.analysis is not None, "No workbook rows to approve")
        dialog.summary_tabs.SetSelection(0)
        for index in range(len(dialog.session.analysis.sections)):
            event(wx, dialog.sections, wx.EVT_LISTBOX, index)
            wait_for_preview(app, dialog.preview_pane)
        event(wx, dialog.approve_button, wx.EVT_BUTTON)
        require(
            dialog.session.approved,
            "Review approval failed: " + dialog.status.GetValue(),
        )

    def wait_calculation(dialog: Any) -> None:
        wait_until(
            app,
            lambda: dialog._calculation_cancel is None,
            "Native calculation callback did not finish",
        )

    def select_stackup(dialog: Any, selected: Any = construction) -> None:
        wait_until(
            app,
            lambda: dialog._catalog_controller is not None
            and not dialog._catalog_controller.state.loading,
            "Stackup catalog did not finish",
        )

        def select(picker: Any) -> None:
            require(
                picker.choices.GetItemCount() == 2,
                "Deterministic catalog not displayed",
            )
            index = next(
                index
                for index, item in enumerate(picker._shown)
                if item.stackup_id == selected.stackup_id
            )
            picker.choices.Select(index)
            app.Yield()
            require(
                picker.accept_button.IsEnabled(),
                "Native stackup selection event was not applied",
            )
            event(wx, picker.accept_button, wx.EVT_BUTTON)

        with interact_modal(wx, modules.picker.StackupDialog, select):
            event(wx, dialog.stackup_button, wx.EVT_BUTTON)
        require(
            dialog.session.config.stackup == selected,
            "Chosen stackup was not retained",
        )

    def configure_initial(dialog: Any) -> None:
        logging.getLogger(__name__).info(
            "Native workflow: select stackup and wait for automatic widths"
        )
        select_stackup(dialog)
        wait_calculation(dialog)
        saved_results = dialog.session.config.width_results
        expected_jobs = sum(
            len(spec.layer_settings) for spec in dialog.session.config.specifications
        )
        require(
            len(saved_results) == expected_jobs
            and all(result.status == "success" for result in saved_results),
            "Nominal width calculation did not populate every layer",
        )
        require(
            dialog.width_comparisons.GetItemCount() > 0,
            "Actual-versus-nominal comparison table is empty",
        )
        require(
            dialog.width_comparisons.IsShownOnScreen(),
            "Width comparison controls are not visibly selected",
        )
        actual_widths = {trace.width_nm for trace in dialog.session.snapshot.traces}
        for row in range(dialog.width_comparisons.GetItemCount()):
            actual, nominal, delta = (
                Decimal(dialog.width_comparisons.GetItemText(row, column).split()[0])
                * 1_000_000
                for column in (2, 3, 4)
            )
            require(
                actual in actual_widths
                and nominal == 180000
                and delta == actual - nominal,
                "Native comparisons do not show the actual width, calculated width and signed difference",
            )

        def unavailable(*args: Any, **kwargs: Any) -> Any:
            baseline = calculate(*args, **kwargs)
            return replace(
                baseline,
                status="unavailable",
                target_width_nm=None,
                message="Deterministic calculator rejection",
                calculation_digest="",
            )

        with patch.object(
            modules.calculator.JlcpcbCalculator, "calculate_width", unavailable
        ):
            event(wx, dialog.calculate_button, wx.EVT_BUTTON)
            wait_calculation(dialog)
        require(
            dialog.session.config.width_results == saved_results
            and "rejection" in dialog.status.GetValue(),
            "Failed calculation replaced prior successful results",
        )

        approve(dialog)
        require(
            dialog.session.approved,
            "Successful results must be approved before invalidation scenarios",
        )
        for scenario in ("cancel", "stale"):
            require(
                dialog.session.approved,
                "Prior approval must exist before cancellation/stale result checks",
            )
            logging.getLogger(__name__).info(
                "Native workflow: %s calculation", scenario
            )
            entered, release, returned = Event(), Event(), Event()

            def blocked(
                *args: Any,
                entered: Event = entered,
                release: Event = release,
                returned: Event = returned,
                **kwargs: Any,
            ) -> Any:
                entered.set()
                require(release.wait(15), "Native worker release was not delivered")
                try:
                    baseline = calculate(*args, **kwargs)
                    candidate = replace(
                        baseline,
                        target_width_nm=181000,
                        calculated_at_utc="2026-09-23T12:01:00.000000Z",
                        calculation_digest=hashlib.sha256(
                            (baseline.input_digest + "181000").encode()
                        ).hexdigest(),
                    )
                    require(
                        candidate not in saved_results,
                        "Delayed result must differ from prior saved widths",
                    )
                    return candidate
                finally:
                    returned.set()

            with patch.object(
                modules.calculator.JlcpcbCalculator, "calculate_width", blocked
            ):
                dialog._schedule_width_refresh(force=True)
                wait_until(
                    app, entered.is_set, "Deterministic calculator worker did not start"
                )
                track = next(iter(board.GetTracks()))
                width = track.GetWidth()
                try:
                    if scenario == "cancel":
                        dialog._cancel_calculation()
                    else:
                        track.SetWidth(width + 1000)
                    release.set()
                    wait_until(
                        app,
                        returned.is_set,
                        "Deterministic calculator worker did not return",
                    )
                    wait_calculation(dialog)
                    require(
                        dialog.session.config.width_results == saved_results,
                        scenario + " calculation replaced prior successful results",
                    )
                    if scenario == "cancel":
                        require(
                            dialog.session.approved,
                            "Unchanged-input cancellation discarded prior review approval",
                        )
                    if scenario == "stale":
                        require(
                            not dialog.session.approved,
                            "Board changes retained obsolete review approval",
                        )
                finally:
                    track.SetWidth(width)
                    release.set()
            event(wx, dialog.retry_button, wx.EVT_BUTTON)
            wait_for_preview(app, dialog.preview_pane)
        logging.getLogger(__name__).info(
            "Native workflow: approve previews and exercise save failure"
        )
        approve(dialog)
        before = frame._impedance.repository.load()
        with patch.object(
            frame._impedance.repository,
            "save",
            side_effect=sqlite3.OperationalError("Deterministic save failure"),
        ):
            event(wx, stock_button(dialog, wx.ID_OK), wx.EVT_BUTTON)
        require(
            dialog.IsModal() and not dialog._closing,
            "Failed save closed the native working copy",
        )
        require(
            frame._impedance.repository.load() == before
            and dialog.session.config.width_results == saved_results,
            "Failed save changed persisted intent or working results",
        )
        require(
            "save failure" in dialog.status.GetValue(),
            "Failed save was not exposed in the native dialog",
        )
        event(wx, stock_button(dialog, wx.ID_OK), wx.EVT_BUTTON)

    def generate(frame: Any) -> None:
        logging.getLogger(__name__).info("Native workflow: Generate")
        command = wx.CommandEvent(wx.EVT_TOOL.typeId, modules.main.ID_GENERATE)
        command.SetEventObject(frame.upper_toolbar)
        require(
            frame.GetEventHandler().ProcessEvent(command),
            "Generate toolbar event was not handled",
        )
        app.Yield()
        require(
            frame.generate_button.IsEnabled() and not frame._generating,
            "Generate did not restore native control state",
        )

    # Any accidental network path is an acceptance failure; only explicit
    # deterministic catalog/calculator responses above cross the supplier seam.
    import requests

    with (
        patch.object(modules.main, "PLUGIN_PATH", settings_dir),
        patch.object(modules.catalog, "fetch_stackups", fetch_catalog),
        patch.object(modules.calculator.JlcpcbCalculator, "calculate_width", calculate),
        patch.object(
            requests.sessions.Session,
            "request",
            side_effect=AssertionError("Native workflow must not contact suppliers"),
        ),
        patch.object(wx, "MessageBox", message),
    ):
        try:
            frame = new_frame()
            config = modules.model.Config.from_dict(plan.config.to_dict())
            frame._impedance._save(
                replace(config, reviewed_digest="", included_section_ids=())
            )
            frame._impedance._update_saved_status()
            configure(frame, configure_initial)
            saved_config = frame._impedance.config
            require(
                saved_config.reviewed_digest and saved_config.stackup == construction,
                "Reviewed selected stackup was not persisted",
            )
            require(
                frame._impedance.repository.load()[0] == saved_config,
                "Native Save did not commit results",
            )
            database_path = Path(frame.store.dbfile)
            database_before_reopen = database_path.read_bytes()
            logging.getLogger(__name__).info("Native workflow: reopen saved plugin")
            close_frame(frame)
            frame = new_frame()
            require(
                frame._impedance.config == saved_config
                and frame._impedance.checkbox.GetValue(),
                "Reopening lost enabled configuration or numerical results",
            )

            def cancel_reopened(dialog: Any) -> None:
                require(
                    dialog.session.config == saved_config,
                    "Reopened dialog lost persisted settings",
                )
                select_stackup(dialog, alternative)
                require(
                    dialog.session.config != saved_config,
                    "Cancelled dialog must contain an observable working edit",
                )
                event(wx, stock_button(dialog, wx.ID_CANCEL), wx.EVT_BUTTON)

            configure(frame, cancel_reopened)
            require(
                database_path.read_bytes() == database_before_reopen,
                "Reopening/cancelling rewrote saved database",
            )
            generate(frame)
            require(not messages, "Native Generate failed: " + str(messages))
            paths = frame.fabrication.get_artifact_paths()
            archive = Path(paths["gerber_zip"])
            enabled = check_generated_archive(archive, True)
            require(
                enabled["workbook_images"] == len(plan.sections),
                "Generated workbook lost native section captures",
            )
            (output / "enabled.zip").write_bytes(archive.read_bytes())
            published_dir = Path(frame.fabrication.outputdir)
            prior_outputs = output_contents(published_dir)
            prior_count = frame.store.get_generation_count()

            def cancel_placeholder(text: str, *_args: Any, **_kwargs: Any) -> int:
                require(
                    "placeholder not present" in text,
                    "Unexpected Generate cancellation prompt: " + text,
                )
                messages.append(text)
                return wx.CANCEL

            frame.settings["general"]["order_number"] = True
            try:
                with patch.object(wx, "MessageBox", cancel_placeholder):
                    generate(frame)
            finally:
                frame.settings["general"]["order_number"] = False
            require(
                len(messages) == 1,
                "Generate did not reach the real order-number prompt",
            )
            messages.clear()
            require(
                output_contents(published_dir) == prior_outputs
                and frame.store.get_generation_count() == prior_count,
                "Cancelled Generate changed published outputs or advanced its counter",
            )
            require(
                frame._impedance.repository.load()[0] == saved_config,
                "Cancelled Generate changed persisted impedance configuration",
            )
            require(
                all(
                    path.read_bytes() == contents
                    for path, contents in original_files.items()
                ),
                "Cancelled Generate changed source PCB/project files",
            )

            for failure in ("report", "publication"):
                if failure == "report":
                    injection = patch.object(
                        modules.main,
                        "export_impedance_reports",
                        side_effect=OSError("Deterministic report failure"),
                    )
                else:
                    native_replace = modules.archive.os.replace
                    failed = False

                    def replace_once(
                        source: Any,
                        destination: Any,
                        native_replace: Callable[..., Any] = native_replace,
                    ) -> None:
                        nonlocal failed
                        if Path(destination) == archive and not failed:
                            failed = True
                            raise OSError("Deterministic publication failure")
                        native_replace(source, destination)

                    injection = patch.object(
                        modules.archive.os, "replace", replace_once
                    )
                with injection:
                    generate(frame)
                require(
                    len(messages) == 1
                    and f"Deterministic {failure} failure" in messages[0],
                    "Expected native generation failure not reported: " + str(messages),
                )
                messages.clear()
                require(
                    output_contents(published_dir) == prior_outputs,
                    failure + " failure changed prior published fabrication output",
                )
                require(
                    frame.store.get_generation_count() == prior_count,
                    failure + " failure advanced generation count",
                )
                require(
                    frame._impedance.repository.load()[0] == saved_config,
                    failure + " failure changed persisted impedance results",
                )
            frame._impedance.checkbox.SetValue(False)
            event(wx, frame._impedance.checkbox, wx.EVT_CHECKBOX)
            require(
                not frame._impedance.repository.load()[0].enabled,
                "Disable event did not persist",
            )
            generate(frame)
            require(not messages, "Disabled native Generate failed: " + str(messages))
            disabled = check_generated_archive(archive, False)
            (output / "disabled.zip").write_bytes(archive.read_bytes())
            require(
                frame.store.get_generation_count() == prior_count + 1,
                "Only successful regeneration may increment generation count",
            )
            close_frame(frame)
            require(
                all(
                    path.read_bytes() == contents
                    for path, contents in original_files.items()
                ),
                "Configure or Generate changed PCB/project source files",
            )
            require(
                modules.adapter.snapshot_board(board, pcbnew) == original_snapshot,
                "Configure or Generate changed the native board",
            )
            return {
                "scope": "real JLCPCBTools constructor and toolbar handlers, GTK modal controls, native Gerber/Excellon/PNG rendering, SQLite and report/ZIP publication",
                "supplier_boundary": "deterministic catalog and calculator responses; HTTP requests blocked",
                "fixture": "copied six-layer USB sample; existing specification intent seeded before Configure",
                "drc": "optional force-DRC disabled; fixture zones prefilled once before source preservation baseline",
                "enabled_archive": enabled,
                "disabled_archive": disabled,
                "calculator_calls": len(calculator_calls),
                "accepted_scenarios": [
                    "select stackup",
                    "calculate all layers",
                    "visible width comparisons",
                    "calculator rejection retains results",
                    "cancel calculation retains results",
                    "board change discards stale callback",
                    "review native previews",
                    "save failure retains open working copy",
                    "save and reopen",
                    "cancel edited reopening preserves database",
                    "cancel Generate prompt preserves output and counter",
                    "Generate toolbar produces Gerber/Excellon/workbook/HTML",
                    "report failure preserves published output",
                    "ZIP replacement failure preserves output and counter",
                    "disabled regeneration removes reports",
                    "source PCB/project bytes and native board preserved",
                ],
            }
        finally:
            for window in reversed(frames):
                if window:
                    window.Close(force=True)
            app.Yield()
