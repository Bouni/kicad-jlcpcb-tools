"""Bounded Linux KiCad/GTK acceptance, separate from mocked unit tests.

Run with Python that can import KiCad and wxGTK, under Xvfb. Every stage runs in a
fresh process; unsupported bindings, crashes and timeouts are failures, not skips.
Only fresh artifact directories and copies of checked-in boards are writable.
The Generate stage exercises the actual plugin constructor and toolbar workflow.
"""

import argparse
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import ExitStack, contextmanager
import ctypes
from dataclasses import replace
from decimal import ROUND_HALF_UP, Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import posixpath
import re
import secrets
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from typing import Any, Optional
from xml.etree import ElementTree as ET
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.native_impedance_support import ProcessSupervisor  # noqa: E402

CASES = (
    "single-ended-50-ohm",
    "usb-differential-90-ohm",
)
STAGES = ("prerequisites", *CASES, "custom-palette", "dialogs", "workflow", "generate")


def require(condition: Any, message: str) -> None:
    """Keep acceptance checks active even when Python assertions are optimized."""
    if not condition:
        raise RuntimeError(message)


def claim_output(path: Path) -> Path:
    """Reject source aliases, existing outputs and symlink redirection before writes."""
    path = Path(os.path.abspath(path))
    resolved = path.resolve()
    if (
        path.exists()
        or any(part.is_symlink() for part in (path, *path.parents))
        or resolved == ROOT
        or ROOT in resolved.parents
        or resolved in ROOT.parents
        or resolved == Path.home()
    ):
        raise ValueError("Use a fresh output directory outside the source tree.")
    path.mkdir(parents=True, exist_ok=False)
    return path


def write_json(path: Path, value: Any) -> None:
    """Write a new diagnostic artifact, never silently overwrite another run."""
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def stage_command(stage: str, output: Path, token: str) -> list[str]:
    """Start a fresh native interpreter with an isolated, authenticated result."""
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        stage,
        "--output",
        str(output),
        "--private-display",
        "--run-token",
        token,
    ]


def run_stage(
    stage: str,
    output: Path,
    timeout: int,
    cancelled: threading.Event,
    *,
    label: Optional[str] = None,
    private_display: bool = True,
) -> dict[str, Any]:
    """Own one fresh child and require its current successful result before accepting it."""
    started = time.perf_counter()
    label = label or stage
    token = secrets.token_hex(16)
    record: dict[str, Any] = {
        "stage": stage,
        "status": "FAIL",
        "log": label + ".log",
    }
    log = output / record["log"]
    try:
        require(not cancelled.is_set(), "Cancelled after another stage failed")
        command = stage_command(stage, output / label, token)
        if not private_display:
            command.remove("--private-display")
        with ProcessSupervisor() as supervisor:
            outcome = supervisor.run(
                command, ROOT, dict(os.environ), log, timeout, cancelled=cancelled
            )
        require(not outcome["cancelled"], "Cancelled after another stage failed")
        require(not outcome["timed_out"], f"Native process exceeded {timeout} seconds")
        require(
            outcome["returncode"] == 0, f"Native process exited {outcome['returncode']}"
        )
        result_path = output / label / "result.json"
        require(
            not result_path.parent.is_symlink() and not result_path.is_symlink(),
            "Native result is a symlink",
        )
        details = json.loads(result_path.read_text(encoding="utf-8"))
        require(
            isinstance(details, dict)
            and details.get("stage") == stage
            and details.get("status") == "PASS"
            and details.get("run_token") == token
            and isinstance(details.get("evidence"), dict),
            "Missing, stale or invalid native PASS evidence",
        )
        require(not cancelled.is_set(), "Cancelled after another stage failed")
        record.update(details)
    except Exception as error:
        record.update(status="FAIL", error=str(error))
        with log.open("a", encoding="utf-8") as stream:
            stream.write("\n" + traceback.format_exc())
    record["duration_seconds"] = time.perf_counter() - started
    return record


@contextmanager
def stage_cancellation(cancelled: threading.Event) -> Iterator[None]:
    """Translate supervisor termination into bounded cleanup of detached sessions."""
    previous = {}

    def request_cancel(_number: int, _frame: Any) -> None:
        cancelled.set()

    if threading.current_thread() is threading.main_thread():
        for number in (signal.SIGTERM, signal.SIGINT):
            previous[number] = signal.signal(number, request_cancel)
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def schedule_parallel_stages(
    output: Path, timeout: int, cancelled: threading.Event
) -> list[dict[str, Any]]:
    """Collect every started worker and leave explicit NOT_RUN entries after failure."""
    records = {stage: {"stage": stage, "status": "NOT_RUN"} for stage in STAGES}
    records["prerequisites"] = run_stage("prerequisites", output, timeout, cancelled)
    sys.stdout.write("prerequisites: " + records["prerequisites"]["status"] + "\n")
    sys.stdout.flush()
    if records["prerequisites"]["status"] != "PASS":
        return list(records.values())
    pending = iter(("generate", "dialogs", *CASES, "custom-palette", "workflow"))
    with ThreadPoolExecutor(max_workers=2) as executor:
        active = {
            executor.submit(run_stage, stage, output, timeout, cancelled): stage
            for stage in (next(pending), next(pending))
        }
        try:
            while active:
                completed, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in completed:
                    stage = active.pop(future)
                    try:
                        records[stage] = future.result()
                    except Exception as error:
                        records[stage] = {
                            "stage": stage,
                            "status": "FAIL",
                            "error": str(error),
                        }
                    if records[stage]["status"] != "PASS":
                        cancelled.set()
                    sys.stdout.write(stage + ": " + records[stage]["status"] + "\n")
                    sys.stdout.flush()
                if not cancelled.is_set():
                    for _ in range(2 - len(active)):
                        stage = next(pending, None)
                        if stage is not None:
                            active[
                                executor.submit(
                                    run_stage,
                                    stage,
                                    output,
                                    timeout,
                                    cancelled,
                                )
                            ] = stage
        except BaseException:
            cancelled.set()
            raise
    return list(records.values())


def run(
    output: Path,
    timeout: int = 240,
    *,
    generate_only: bool = False,
    generate_runs: int = 1,
    generate_budget_seconds: Optional[float] = None,
    workers: int = 1,
) -> int:
    """Supervise the native processes and preserve explicit failure evidence."""
    if type(timeout) is not int or not 0 < timeout <= 3600:
        raise ValueError("The stage timeout must be between 1 and 3600 seconds.")
    if type(workers) is not int or workers not in (1, 2):
        raise ValueError("Use one or two native stage workers.")
    if workers != 1 and (generate_only or generate_budget_seconds is not None):
        raise ValueError("Generate benchmarks and budgets require sequential workers.")
    if (
        type(generate_only) is not bool
        or type(generate_runs) is not int
        or not 1 <= generate_runs <= 10
        or (not generate_only and generate_runs != 1)
        or (
            generate_budget_seconds is not None
            and (
                not math.isfinite(generate_budget_seconds)
                or not 0 < generate_budget_seconds <= 3600
            )
        )
    ):
        raise ValueError("Invalid Generate benchmark configuration.")
    output = claim_output(output)
    suite_started = time.monotonic()
    cancelled = threading.Event()
    with stage_cancellation(cancelled):
        if workers == 2:
            records = schedule_parallel_stages(output, timeout, cancelled)
        else:
            records = []
            blocked = False
            stages = ("generate",) * generate_runs if generate_only else STAGES
            for index, stage in enumerate(stages, 1):
                label = f"generate-run-{index}" if generate_only else stage
                record = (
                    {"stage": stage, "status": "NOT_RUN"}
                    if blocked
                    else run_stage(
                        stage,
                        output,
                        timeout,
                        cancelled,
                        label=label,
                        private_display=False,
                    )
                )
                blocked = blocked or record["status"] != "PASS"
                if generate_only:
                    record["run"] = index
                if (
                    "duration_seconds" in record
                    and stage == "generate"
                    and generate_budget_seconds is not None
                ):
                    record["budget_seconds"] = generate_budget_seconds
                    if record["duration_seconds"] > generate_budget_seconds:
                        record.update(
                            status="FAIL",
                            budget_error="Complete Generate group exceeded its wall-time budget",
                        )
                records.append(record)
                sys.stdout.write(f"{label}: {record['status']}\n")
                sys.stdout.flush()
    failed = cancelled.is_set() or any(record["status"] != "PASS" for record in records)
    write_json(
        output / "summary.json",
        {
            "schema_version": 1,
            "status": "FAIL" if failed else "PASS",
            "stages": records,
            "workers": workers,
            "elapsed_seconds": round(time.monotonic() - suite_started, 3),
            "summed_worker_seconds": round(
                sum(record.get("duration_seconds", 0) for record in records), 3
            ),
            "timing_mode": "parallel diagnostic"
            if workers == 2
            else "sequential benchmark",
            "scope": (
                "all seven native acceptance stages in isolated GUI sessions; parallel timings do not establish sequential performance budgets"
                if workers == 2
                else "complete Generate workers, including native initialization, fixture preparation, approvals, reports, publication scenarios and cleanup"
                if generate_only
                else "native pcbnew/GTK, Configure/calculation/review/Save/reopen/Generate, real fabrication reports and publication"
            ),
        },
    )
    return int(failed)


def native_runtime() -> tuple[Any, Any, Any, dict[str, Any]]:
    """Require real Linux bindings, a display and matching stable KiCad 10.0 tools."""
    require(sys.platform.startswith("linux"), "This acceptance runner requires Linux.")
    require(os.environ.get("DISPLAY"), "DISPLAY is missing; run under Xvfb.")
    import _pcbnew
    import pcbnew  # noqa: TID251 - standalone native validation needs KiCad bindings.
    import wx
    import wx.svg

    require(
        str(_pcbnew.__file__).endswith(".so"),
        "Expected the native Linux pcbnew extension",
    )
    require(
        any("gtk" in str(item).lower() for item in wx.PlatformInfo),
        "Expected real wxGTK",
    )
    require(
        pcbnew.GetBoard() is None,
        "Never run this standalone loader inside a PCB editor",
    )
    native_version = str(pcbnew.GetBuildVersion())
    native_release = re.match(r"\(?(10\.0\.\d+)(?![\d.])", native_version)
    require(
        native_release,
        "Unsupported pcbnew version: " + native_version,
    )
    cli = os.environ.get("KICAD_CLI") or shutil.which("kicad-cli")
    require(cli, "KICAD_CLI is missing")
    version = subprocess.run(
        [cli, "version", "--format", "plain"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    require(
        re.fullmatch(r"10\.0\.\d+", version),
        "Unsupported KiCad CLI version: " + version,
    )
    require(
        native_release is not None and native_release.group(1) == version,
        f"KiCad CLI and pcbnew versions do not match: {version} / {native_version}",
    )
    for name in (
        "PCB_IO_KICAD_SEXPR",
        "STRING_FORMATTER",
        "PLOT_CONTROLLER",
        "ZONE_FILLER",
    ):
        require(callable(getattr(pcbnew, name, None)), "Missing native API: " + name)
    require(
        callable(getattr(pcbnew.BOARD, "__swig_destroy__", None)),
        "Missing safe BOARD destructor",
    )
    app, size = checked_app(wx)
    return (
        pcbnew,
        wx,
        app,
        {
            "python": sys.version,
            "interpreter": sys.executable,
            "pcbnew": native_version,
            "pcbnew_extension": _pcbnew.__file__,
            "wx": wx.version(),
            "display": list(size),
            "cli": cli,
            "cli_version": version,
        },
    )


def checked_app(wx: Any) -> tuple[Any, Any]:
    """Release an initialized GTK app if native display validation fails."""
    app = wx.App(False)
    try:
        app.SetAppName("JLCPCB Linux native acceptance")
        size = wx.GetDisplaySize()
        require(
            size.GetWidth() >= 1280 and size.GetHeight() >= 900,
            "Xvfb display must be at least 1280x900",
        )
        return app, size
    except BaseException:
        app.Destroy()
        raise


def configure_palette(output: Path, custom: bool) -> None:
    """Initialize worker-local preferences for the supported stable KiCad 10.0 line."""
    base = output / "preferences"
    directory = base / "10.0"
    directory.mkdir(parents=True)
    theme = "smoke-light" if custom else "_builtin_default"
    write_json(directory / "pcbnew.json", {"appearance": {"color_theme": theme}})
    if custom:
        (directory / "colors").mkdir()
        write_json(
            directory / "colors" / "smoke-light.json",
            {
                "meta": {"version": 6},
                "board": {
                    "background": "rgb(247, 249, 251)",
                    "copper": {"f": "rgb(23, 43, 61)"},
                },
            },
        )
    os.environ["KICAD_CONFIG_HOME"] = str(base)


def fixture(name: str) -> tuple[Any, Path]:
    """Resolve only the bounded checked-in examples, not user-supplied paths."""
    from tests.rf_impedance_combined import COMBINED_BOARDS

    for case in COMBINED_BOARDS:
        if case.name == name:
            return case, ROOT / "examples" / "impedance" / f"{name}.kicad_pcb"
    raise ValueError("Unknown native fixture: " + name)


@contextmanager
def loaded_fixture(
    name: str, output: Path, pcbnew: Any
) -> Iterator[tuple[Any, Any, Path]]:
    """Load/refill only copies with full project metadata, owning native lifetime.

    The normal loader is intentional ONLY in this isolated standalone process:
    it attaches project net classes. Production editor capture still uses the
    raw loader and must never call this global-state helper.
    """
    from impedance.board_copy import NativeBoardOwner

    case, source = fixture(name)
    originals = {
        path: path.read_bytes()
        for path in (
            source.with_suffix(suffix)
            for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru")
        )
        if path.is_file()
    }
    require(
        source.with_suffix(".kicad_pro") in originals,
        "Fixture requires its native project",
    )
    directory = output / "board"
    directory.mkdir()
    for path in originals:
        shutil.copy2(path, directory / path.name)
    target = directory / source.name
    owner = None
    board = None
    project = None
    cleanup_errors: list[str] = []
    try:
        board = pcbnew.LoadBoard(str(target))
        require(board is not None, "Native loader returned no board")
        owner = NativeBoardOwner(board, pcbnew.BOARD.__swig_destroy__)
        require(
            callable(getattr(board, "ClearProject", None)),
            "Missing safe project release API",
        )
        project = board.GetProject()
        require(project is not None, "Native loader did not attach the fixture project")
        filler = pcbnew.ZONE_FILLER(board)
        fill_error = ""
        filled = False
        try:
            try:
                filled = filler.Fill(board.Zones())
            except BaseException:
                # Store text, not an exception traceback retaining the native
                # filler's bound `self` beyond board teardown.
                fill_error = traceback.format_exc()
        finally:
            del filler
        require(not fill_error, fill_error)
        require(filled, "Native zone filling failed")
        require(pcbnew.SaveBoard(str(target), board), "Native copy save failed")
        yield case, board, target
    finally:
        primary = sys.exc_info()[1]
        try:
            if board is not None and project is not None:
                board.ClearProject()
        except BaseException as error:
            cleanup_errors.append("ClearProject: " + str(error))
        board = None
        try:
            if owner is not None:
                owner.close()
        except BaseException as error:
            cleanup_errors.append("Board release: " + str(error))
        try:
            manager = pcbnew.GetSettingsManager()
            if project is None:
                project = manager.GetProject(str(target.with_suffix(".kicad_pro")))
            if project is not None:
                require(
                    manager.UnloadProject(project, False),
                    "Native project release failed",
                )
        except BaseException as error:
            cleanup_errors.append("Project unload: " + str(error))
        try:
            require(
                all(
                    path.read_bytes() == contents
                    for path, contents in originals.items()
                ),
                "Original fixture changed during native acceptance",
            )
        except BaseException as error:
            cleanup_errors.append("Source integrity: " + str(error))
        if cleanup_errors:
            message = "; ".join(cleanup_errors)
            if primary is None:
                raise RuntimeError(message)
            sys.stderr.write("Additional cleanup failure: " + message + "\n")


def class_plan(case: Any, board: Any, pcbnew: Any) -> Any:
    """Use actual native memberships/pairs and actual widths, never fixture snapshots."""
    from impedance.matching import analyze, is_pad_via_stub
    from impedance.model import EXCLUSION_COVERED_STUB, Config
    from impedance.pcbnew_adapter import snapshot_board
    from impedance.service import ExportPlan
    from impedance.spacing_checks import SpacingContext

    snapshot = snapshot_board(board, pcbnew)
    require(not snapshot.net_class_error, snapshot.net_class_error)
    require(not snapshot.differential_pair_error, snapshot.differential_pair_error)
    specifications = case.specifications()
    require(
        all(spec.net_class in snapshot.net_classes for spec in specifications),
        "Native custom class was not loaded",
    )
    members = dict(snapshot.net_class_memberships)
    require(
        all(
            circuit.net_class in members.get(net, ())
            for circuit in case.circuits
            for net in circuit.net_names
        ),
        "Native project class membership does not cover the declared signal nets",
    )
    expected = {
        trace.trace_id for trace in snapshot.traces if trace.net in case.net_names
    }
    require(
        {trace.net for trace in snapshot.traces if trace.net in case.net_names}
        == set(case.net_names),
        "Native board is missing a declared circuit's routed signal net",
    )
    config = Config(enabled=True, specifications=specifications)
    analysis = analyze(config, snapshot)
    actual = [
        trace.trace_id for section in analysis.sections for trace in section.traces
    ]
    require(
        set(actual) == expected and len(actual) == len(expected),
        "Native rows lost, duplicated or widened signal selection",
    )
    exportable = tuple(
        section for section in analysis.sections if not section.exclusion_reason
    )
    for section in analysis.sections:
        if section.exclusion_reason:
            require(
                section.exclusion_reason == EXCLUSION_COVERED_STUB
                and all(
                    is_pad_via_stub(trace, snapshot.copper_features)
                    for trace in section.traces
                ),
                "Native covered-stub audit row lacks same-net copper coverage proof",
            )
    if case.paired:
        pairs = {tuple(sorted(circuit.net_names)) for circuit in case.circuits}
        require(
            pairs <= {tuple(sorted(pair)) for pair in snapshot.differential_pairs},
            "Native differential mate lookup failed",
        )
        require(
            all(tuple(sorted(section.net_names)) in pairs for section in exportable),
            "A differential workbook row omitted a mate",
        )
    config = replace(
        config,
        reviewed_digest=analysis.digest,
        included_section_ids=tuple(section.section_id for section in exportable),
    )
    # Gallery/dialog stages need candidate geometry, not fabricated diagnostic
    # consent. Only the explicit fixture-review stage below calls real prepare.
    plan = ExportPlan(config, snapshot, analysis, exportable, SpacingContext(snapshot))
    require(
        plan is not None and plan.sections,
        "Native class selection produced no export plan",
    )
    return plan


def fixture_spacing_review(plan: Any, board: Any, pcbnew: Any, output: Path) -> Any:
    """Seed non-UI test consent only from freshly rendered native diagnostic PNGs.

    This is storage/export fixture setup, not evidence of user interaction. The
    separate Configure-through-Generate stage exercises actual approval buttons.
    The production preflight and export still verify these exact native bytes.
    """
    from impedance import review_tracking, service
    from impedance.render import SectionRenderer
    from impedance.spacing_checks import SpacingContext
    from impedance.spacing_review import spacing_review_entries

    context = plan.spacing_context or SpacingContext(plan.snapshot)
    entries = spacing_review_entries(
        plan.config, plan.snapshot, plan.analysis.sections, context=context
    )
    history = plan.config.review_tracking
    renderer = SectionRenderer(board, pcbnew)
    try:
        for index, entry in enumerate(entries, 1):
            destination = output / f"fixture-spacing-review-{index:04d}.png"
            result = Path(
                renderer.render_spacing(
                    entry, destination, entry.width_px, entry.height_px
                )
            )
            require(
                result.resolve() == destination.resolve(),
                "Fixture diagnostic renderer returned an unrelated PNG",
            )
            capture = service.CapturedImage.load(
                destination, viewport_nm=entry.viewport_nm
            )
            require(
                (capture.width, capture.height) == (entry.width_px, entry.height_px),
                "Fixture diagnostic PNG dimensions are invalid",
            )
            digest = review_tracking.spacing_capture_fingerprint(
                entry.input_digest, capture.sha256
            )
            history = review_tracking.put_spacing_image(
                history,
                review_tracking.SpacingImageReview(
                    entry.spec_id,
                    entry.layer,
                    entry.issue_id,
                    entry.view_id,
                    entry.input_digest,
                    digest,
                    "2026-10-24T12:00:00.000000Z",
                    digest,
                    "2026-10-24T12:00:01.000000Z",
                ),
            )
    finally:
        renderer.close()
    config = replace(plan.config, review_tracking=history)
    result = service.prepare(
        config, plan.snapshot, len(plan.snapshot.layers), spacing_context=context
    )
    require(
        result is not None,
        "Fixture diagnostic review did not produce a real export plan",
    )
    return result


def check_yellow_edges(rgb: bytes, box: tuple[float, ...]) -> None:
    """Require yellow coverage along all four borders, not just their corners."""
    left, top, right, bottom = box
    for index in range(17):
        fraction = index / 16
        for x, y in (
            (left + fraction * (right - left), top),
            (left + fraction * (right - left), bottom),
            (left, top + fraction * (bottom - top)),
            (right, top + fraction * (bottom - top)),
        ):
            require(
                any(
                    rgb[(row * 800 + column) * 3 : (row * 800 + column) * 3 + 3]
                    == b"\xff\xff\x00"
                    for row in range(max(0, round(y) - 2), min(420, round(y) + 3))
                    for column in range(max(0, round(x) - 2), min(800, round(x) + 3))
                ),
                f"Missing yellow edge near {x:g}, {y:g}",
            )


def check_png(
    path: Path, section: Any, board: Any, wx: Any, custom: bool = False
) -> dict[str, Any]:
    """Validate actual color pixels and whole-route yellow border geometry."""
    from impedance.render import board_outline_bounds, section_viewport
    from impedance.service import CapturedImage

    data = path.read_bytes()
    capture = CapturedImage.from_bytes(data)
    require(
        (capture.width, capture.height) == (800, 420),
        "Invalid native capture PNG",
    )
    image = wx.Image(str(path), wx.BITMAP_TYPE_PNG)
    require(image.IsOk(), "Native wx could not reload its capture")
    raw = bytes(image.GetData())
    require(len(raw) == 800 * 420 * 3, "Native capture has incomplete RGB pixels")
    # Counter consumes the aligned triples in C without retaining 336,000 small
    # byte objects. The same exact histogram proves distinctness and palette.
    colors = Counter(zip(raw[0::3], raw[1::3], raw[2::3]))
    require(len(colors) > 16, "Capture is blank or effectively monochrome")
    yellow = []
    offset = raw.find(b"\xff\xff\x00")
    while offset >= 0:
        # A byte pattern spanning neighboring pixels is not a yellow pixel.
        if offset % 3 == 0:
            index = offset // 3
            yellow.append((index % 800, index // 800))
        offset = raw.find(b"\xff\xff\x00", offset + 1)
    require(yellow, "Native capture has no yellow box")
    box = (
        min(x for x, _ in yellow),
        min(y for _, y in yellow),
        max(x for x, _ in yellow),
        max(y for _, y in yellow),
    )
    view = section_viewport(section, 800, 420, board_bounds=board_outline_bounds(board))
    scale = 800 / view.width
    expected = (
        (section.bounds[0] - view.left) * scale - 16,
        (section.bounds[1] - view.top) * scale - 16,
        (section.bounds[2] - view.left) * scale + 16,
        (section.bounds[3] - view.top) * scale + 16,
    )
    require(
        all(abs(a - b) < 2 for a, b in zip(box, expected)),
        "Yellow box does not frame the complete native section",
    )
    check_yellow_edges(raw, expected)
    for trace in section.traces:
        for x, y in trace.points:
            require(
                box[0] < (x - view.left) * scale < box[2]
                and box[1] < (y - view.top) * scale < box[3],
                "A selected native endpoint is outside the capture box",
            )
    palette_evidence = {}
    if custom:
        # A crop entirely inside a copper pour has no large exposed canvas.
        # Require the selected light background either directly or in its
        # surrounding copper mixed 50% toward that background and composited
        # at the native 60% zone opacity. Premultiplied-alpha rasterization can
        # round each channel by a few levels.
        background = (247, 249, 251)
        copper = {
            "F.Cu": (23, 43, 61),
            "In2.Cu": (206, 125, 44),
            "B.Cu": (77, 127, 196),
        }[section.layer]
        subdued_copper = tuple(
            round(0.5 * foreground + 0.5 * canvas)
            for foreground, canvas in zip(copper, background)
        )
        expected_plane = tuple(
            round(0.6 * foreground + 0.4 * canvas)
            for foreground, canvas in zip(subdued_copper, background)
        )
        canvas_count = colors[background]
        plane_count = sum(
            count
            for color, count in colors.items()
            if max(
                abs(actual - expected)
                for actual, expected in zip(color, expected_plane)
            )
            <= 3
        )
        require(
            canvas_count > 1000 or plane_count > 1000,
            "Selected light canvas was not rendered directly or beneath copper",
        )
        palette_evidence = {
            "light_canvas_pixels": canvas_count,
            "light_plane_pixels": plane_count,
        }
        if section.layer == "F.Cu":
            # The light background chooses black as its higher-contrast
            # endpoint. The represented native RGB moves 30% toward it.
            selected_copper = (16, 30, 43)
            require(
                any(
                    max(
                        abs(pixel[i] - channel)
                        for i, channel in enumerate(selected_copper)
                    )
                    < 8
                    for pixel in colors
                ),
                "Selected custom copper contrast boost was not rendered",
            )
    return {
        "png": path.name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "colors": len(colors),
        "yellow_bounds_px": box,
        "layer": section.layer,
        "nets": section.net_names,
        "trace_ids": [trace.trace_id for trace in section.traces],
        "width_nm": section.width_nm,
        **palette_evidence,
    }


def render_rows(
    plan: Any, board: Any, pcbnew: Any, wx: Any, output: Path, custom: bool = False
) -> list[dict[str, Any]]:
    """Exercise the real PLOT_CONTROLLER renderer and scoped native cleanup."""
    from impedance.pcbnew_adapter import snapshot_board
    from impedance.render import SectionRenderer

    before = snapshot_board(board, pcbnew)
    renderer = SectionRenderer(board, pcbnew)
    records = []
    try:
        for index, section in enumerate(plan.sections, 1):
            path = output / f"section-{index}.png"
            renderer.render(section, path, 800, 420)
            records.append(check_png(path, section, board, wx, custom))
    finally:
        renderer.close()
    require(
        snapshot_board(board, pcbnew) == before,
        "Native rendering changed its input board",
    )
    if custom:
        require(
            sum(
                record["light_canvas_pixels"] + record["light_plane_pixels"]
                for record in records
            )
            > 1000,
            "Custom-palette gallery never showed the selected light canvas directly or beneath copper",
        )
    return records


def stock_button(dialog: Any, identity: int) -> Any:
    """Find a descendant stock button, never a globally matching parent dialog."""
    control = dialog.FindWindow(identity)
    require(control is not None, "Native dialog is missing its stock button")
    return control


def grid_cell_click(wx: Any, control: Any, row: int, column: int) -> None:
    """Click a visible part of a real cell, including wider-than-viewport cells."""
    control.MakeCellVisible(row, column)
    wx.GetApp().Yield()
    rectangle = control.CellToRect(row, column)
    x, y = control.CalcScrolledPosition(rectangle.x, rectangle.y)
    window = control.GetGridWindow()
    visible = wx.Rect(x, y, rectangle.width, rectangle.height).Intersect(
        window.GetClientRect()
    )
    require(not visible.IsEmpty(), "Native workbook cell is outside the grid viewport")
    point = wx.Point(visible.x + visible.width // 2, visible.y + visible.height // 2)
    for kind in (wx.wxEVT_LEFT_DOWN, wx.wxEVT_LEFT_UP):
        command = wx.MouseEvent(kind)
        command.SetEventObject(window)
        command.SetPosition(point)
        command.SetLeftDown(kind == wx.wxEVT_LEFT_DOWN)
        window.GetEventHandler().ProcessEvent(command)


def event(wx: Any, control: Any, binder: Any, selection: Optional[int] = None) -> None:
    """Dispatch the native event type expected by the existing bound handler."""
    import wx.dataview as dv
    import wx.grid as gridlib

    if isinstance(control, gridlib.Grid) and selection is not None:
        require(
            binder.typeId
            in (
                gridlib.EVT_GRID_SELECT_CELL.typeId,
                gridlib.EVT_GRID_CELL_LEFT_CLICK.typeId,
            ),
            "Unsupported native workbook grid event",
        )
        column = 0 if binder.typeId == gridlib.EVT_GRID_CELL_LEFT_CLICK.typeId else 1
        grid_cell_click(wx, control, selection, column)
        return
    elif (
        binder.typeId == dv.EVT_DATAVIEW_SELECTION_CHANGED.typeId
        and selection is not None
    ):
        control.SelectRow(selection)
        item = control.RowToItem(selection)
        control.EnsureVisible(item)
        command = dv.DataViewEvent(binder.typeId, control, item)
    elif binder.typeId == wx.EVT_LIST_ITEM_SELECTED.typeId and selection is not None:
        control.Select(selection)
        control.Focus(selection)
        control.EnsureVisible(selection)
        command = wx.ListEvent(binder.typeId, control.GetId())
        command.SetIndex(selection)
    else:
        if selection is not None:
            control.SetSelection(selection)
        command = wx.CommandEvent(binder.typeId, control.GetId())
        if selection is not None:
            command.SetInt(selection)
    command.SetEventObject(control)
    require(
        control.GetEventHandler().ProcessEvent(command),
        "Native control event was not handled",
    )


def modal(wx: Any, dialog: Any, action: Callable[[], None]) -> int:
    """Drive a real modal loop, propagating callback failures instead of hanging."""
    from scripts.native_impedance_workflow import interact_modal

    try:
        with interact_modal(wx, type(dialog), lambda _dialog: action()):
            return dialog.ShowModal()
    finally:
        dialog.Destroy()


def _window_layout_geometry(dialog: Any) -> dict[str, Any]:
    """Read native allocation and visible review-control bounds in client coordinates."""

    def dimensions(value: Any) -> tuple[int, int]:
        return int(value.GetWidth()), int(value.GetHeight())

    controls = {
        name: getattr(dialog, name, None)
        for name in (
            "stackup_button",
            "approve_button",
            "spacing_approve_button",
            "approve_layer",
            "image_list",
            "sections",
            "specifications",
        )
    }
    pane = getattr(dialog, "preview_pane", None)
    if pane is not None:
        controls.update(preview=pane.window, canvas=pane.canvas)
    boxes = {}
    for name, control in controls.items():
        if control is None or not control.IsShownOnScreen():
            continue
        position = dialog.ScreenToClient(control.GetScreenPosition())
        width, height = dimensions(control.GetSize())
        boxes[name] = (int(position.x), int(position.y), width, height)
    return {
        "frame_size": dimensions(dialog.GetSize()),
        "client_size": dimensions(dialog.GetClientSize()),
        "minimum_size": dimensions(dialog.GetMinSize()),
        "sizer_size": dimensions(dialog.GetSizer().GetSize()),
        "controls": boxes,
    }


def settle_window_layout(app: Any, dialog: Any, timeout: float = 3.0) -> dict[str, Any]:
    """Await stable native sizing and reject visible controls clipped by the client area."""
    deadline = time.monotonic() + timeout
    previous, initial = None, None
    stable, samples = 0, 0
    geometry: dict[str, Any] = {}
    while time.monotonic() < deadline:
        app.Yield()
        dialog.Layout()
        dialog.Update()
        geometry = _window_layout_geometry(dialog)
        samples += 1
        if initial is None:
            initial = geometry
        stable = stable + 1 if geometry == previous else 1
        previous = geometry
        width, height = geometry["client_size"]
        frame_width, frame_height = geometry["frame_size"]
        client_fits_frame = width <= frame_width + 2 and height <= frame_height + 2
        contained = all(
            x >= -2 and y >= -2 and x + w <= width + 2 and y + h <= height + 2
            for x, y, w, h in geometry["controls"].values()
        )
        if (
            stable >= 3
            and geometry["sizer_size"] == geometry["client_size"]
            and client_fits_frame
            and contained
        ):
            return dict(
                geometry, settled=True, initial_geometry=initial, samples=samples
            )
        time.sleep(0.01)
    return dict(geometry, settled=False, initial_geometry=initial, samples=samples)


class _X11Image(ctypes.Structure):
    """Expose the public XImage header needed to decode existing Xvfb pixels."""

    _fields_ = [
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("xoffset", ctypes.c_int),
        ("format", ctypes.c_int),
        ("data", ctypes.c_void_p),
        ("byte_order", ctypes.c_int),
        ("bitmap_unit", ctypes.c_int),
        ("bitmap_bit_order", ctypes.c_int),
        ("bitmap_pad", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("bytes_per_line", ctypes.c_int),
        ("bits_per_pixel", ctypes.c_int),
        ("red_mask", ctypes.c_ulong),
        ("green_mask", ctypes.c_ulong),
        ("blue_mask", ctypes.c_ulong),
    ]


def _x11_screen_rgb(rectangle: tuple[int, int, int, int]) -> bytes:
    """Read actual visible X11 pixels, avoiding GTK's cached Cairo root surface."""
    x11 = ctypes.CDLL("libX11.so.6")
    display_type, window_type = ctypes.c_void_p, ctypes.c_ulong
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = display_type
    x11.XDefaultScreen.argtypes = [display_type]
    x11.XDefaultScreen.restype = ctypes.c_int
    x11.XDefaultRootWindow.argtypes = [display_type]
    x11.XDefaultRootWindow.restype = window_type
    for name in ("XDisplayWidth", "XDisplayHeight"):
        function = getattr(x11, name)
        function.argtypes = [display_type, ctypes.c_int]
        function.restype = ctypes.c_int
    x11.XGetImage.argtypes = [
        display_type,
        window_type,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_ulong,
        ctypes.c_int,
    ]
    x11.XGetImage.restype = ctypes.POINTER(_X11Image)
    x11.XDestroyImage.argtypes = [ctypes.POINTER(_X11Image)]
    x11.XCloseDisplay.argtypes = [display_type]
    display = x11.XOpenDisplay(None)
    require(display, "The native X11 screen cannot be opened for capture")
    try:
        screen = x11.XDefaultScreen(display)
        root = x11.XDefaultRootWindow(display)
        x, y, width, height = rectangle
        require(
            x >= 0
            and y >= 0
            and width > 0
            and height > 0
            and x + width <= x11.XDisplayWidth(display, screen)
            and y + height <= x11.XDisplayHeight(display, screen),
            "The native capture rectangle extends beyond the X11 screen",
        )
        native = x11.XGetImage(
            display, root, x, y, width, height, ctypes.c_ulong(-1), 2
        )
        require(native, "The native X11 screen cannot be read for capture")
        try:
            image = native.contents
            require(
                image.width == width
                and image.height == height
                and image.xoffset == 0
                and image.bits_per_pixel in (24, 32)
                and image.byte_order in (0, 1)
                and image.data,
                "Unsupported native X11 image format",
            )
            stride = image.bits_per_pixel // 8
            offsets = []
            for mask in (image.red_mask, image.green_mask, image.blue_mask):
                shift = (mask & -mask).bit_length() - 1
                require(
                    shift >= 0 and shift % 8 == 0 and mask == 255 << shift,
                    "Unsupported native X11 colour channel",
                )
                offset = shift // 8
                if image.byte_order:
                    offset = stride - 1 - offset
                require(0 <= offset < stride, "Invalid native X11 colour channel")
                offsets.append(offset)
            require(
                image.bytes_per_line >= width * stride,
                "The native X11 image has an incomplete scanline",
            )
            source = ctypes.string_at(image.data, image.bytes_per_line * height)
            rgb = bytearray(width * height * 3)
            for row in range(height):
                start, destination = row * image.bytes_per_line, row * width * 3
                scan = source[start : start + width * stride]
                for channel, offset in enumerate(offsets):
                    rgb[destination + channel : destination + width * 3 : 3] = scan[
                        offset::stride
                    ]
            return bytes(rgb)
        finally:
            x11.XDestroyImage(native)
    finally:
        x11.XCloseDisplay(display)


def capture_window(wx: Any, dialog: Any, output: Path) -> None:
    """Retain the actual Xvfb dialog pixels, not a reconstructed mockup."""
    geometry = settle_window_layout(wx.GetApp(), dialog)
    write_json(output.with_suffix(".layout.json"), geometry)
    require(
        geometry["settled"],
        "Native minimum layout did not settle without clipped controls: "
        + str(geometry),
    )
    size, position = dialog.GetSize(), dialog.GetScreenPosition()
    if getattr(wx, "Platform", None) == "__WXGTK__":
        image = wx.Image(size.GetWidth(), size.GetHeight())
        image.SetData(
            _x11_screen_rgb((position.x, position.y, size.GetWidth(), size.GetHeight()))
        )
        bitmap = wx.Bitmap(image)
    else:
        bitmap = wx.Bitmap(size.GetWidth(), size.GetHeight())
        dc = wx.MemoryDC(bitmap)
        require(
            dc.Blit(
                0,
                0,
                size.GetWidth(),
                size.GetHeight(),
                wx.ScreenDC(),
                position.x,
                position.y,
            ),
            "Native dialog screen capture failed",
        )
        dc.SelectObject(wx.NullBitmap)
    require(
        bitmap.SaveFile(str(output), wx.BITMAP_TYPE_PNG),
        "Native dialog PNG save failed",
    )


def wait_for_preview(
    app: Any, pane: Any, timeout: float = 15.0, *, expected_index: Optional[int] = None
) -> None:
    """Pump native events until a visible paint has produced verified evidence.

    Dispatching a selection queues capture and paint callbacks; neither a decoded
    PNG nor a pending paint is evidence that the user actually saw the image.
    """
    deadline = time.monotonic() + timeout
    while not pane.displayed or (
        expected_index is not None and pane._index != expected_index
    ):
        require(not pane.failure, pane.failure)
        require(time.monotonic() < deadline, "Native preview did not finish displaying")
        app.Yield()
        pane.canvas.Update()
        if not pane.displayed:
            time.sleep(0.01)
    require(
        pane.canvas.IsShownOnScreen()
        and pane.canvas._bitmap is not None
        and pane.canvas._bitmap.IsOk(),
        "Native preview has no visible painted bitmap",
    )


@contextmanager
def dialog_capture_session(
    board: Any, pcbnew: Any, wx: Any, directory: Path
) -> Iterator[Any]:
    """Own real toolbar controls and their production source reader for reviews."""
    from impedance.integration import CaptureSession, ImpedanceControls
    from kicad_backend.swig.read import board_identity
    from scripts.native_impedance_workflow import FixtureBoardProvider

    provider = FixtureBoardProvider(pcbnew, board)
    identity = board_identity(board)

    def live_board() -> Any:
        """Apply the main window's native-allocation/UUID/path lifetime guard."""
        current = provider.GetBoard()
        require(
            board_identity(current) == identity,
            "The dialog fixture PCB was closed, replaced, or renamed",
        )
        return current

    with ExitStack() as owner:
        host = wx.Frame(None, title="Native impedance dialog source", size=(640, 80))
        owner.callback(host.Destroy)
        owner.callback(setattr, provider, "board", None)
        host.pcbnew = provider
        host._legacy_board = live_board
        toolbar = host.CreateToolBar()
        controls = ImpedanceControls(host, toolbar)
        owner.callback(controls.close)
        toolbar.Realize()
        captures = CaptureSession(controls, directory)
        owner.callback(captures.close)
        yield captures


def dialogs(
    plan: Any, board: Any, pcbnew: Any, wx: Any, app: Any, output: Path
) -> dict[str, Any]:
    """Bound real GTK constructors, nested events, visible layer reviews and reopen."""
    from impedance.database import ImpedanceDatabase
    from impedance.dialog import ImpedanceDialog, SpecificationDialog
    from impedance.palette import read_theme_context
    from impedance.repository import ImpedanceRepository
    from impedance.service import CapturedImage
    from scripts.native_impedance_workflow import (
        approve_spacing_review,
        check_spacing_preview,
        interact_modal,
        select_spacing_preview,
        workbook_preview_indexes,
    )

    directory = output / "dialog-native-captures"
    directory.mkdir()
    owner = ExitStack()
    captures = owner.enter_context(dialog_capture_session(board, pcbnew, wx, directory))
    provider = captures.controls.parent.pcbnew
    previewed: list[dict[str, str]] = []
    checked: set[tuple[object, str]] = set()

    def current() -> Any:
        """Use the same freshly validated native source as the real Configure UI."""
        return captures.snapshot()

    def verify(expected: Any) -> None:
        """Reject cached pixels after a change to the source snapshot."""
        captures.verify(expected)

    def appearance() -> tuple[object, None]:
        """Bind visible approval to the actual native palette and ordinary output."""
        return read_theme_context(provider), None

    def preview(section: Any, *, refresh: bool = False) -> CapturedImage:
        """Review production-cached native PNGs, retaining each exact display artifact."""
        capture = captures.preview(section, refresh=refresh)
        path = output / f"dialog-capture-{len(previewed) + 1}.png"
        path.write_bytes(capture.data)
        if hasattr(section, "view_id"):
            identity, kind = section.view_id, "spacing"
            require(
                (capture.width, capture.height)
                == (section.width_px, section.height_px),
                "Diagnostic capture lost its readable native resolution",
            )
        else:
            identity, kind = section.section_id, "workbook"
            key = section, capture.sha256
            if key not in checked:
                check_png(path, section, board, wx)
                checked.add(key)
        previewed.append(
            {
                "entry_id": identity,
                "kind": kind,
                "sha256": capture.sha256,
                "file": path.name,
            }
        )
        return capture

    def offline_catalog(_layer_count: int, **_kwargs: Any) -> tuple[()]:
        """Keep native layout acceptance independent of live catalog availability."""
        return ()

    try:
        original = plan.config.specifications[0]
        main = ImpedanceDialog(
            None,
            plan.config,
            plan.snapshot,
            preview,
            current,
            verify_snapshot=verify,
            verify_captures=captures.verify_captures,
            verify_spacing_captures=captures.verify_spacing_captures,
            prepare_capture=captures.prepare,
            appearance_context=appearance,
            fetch_stackup_catalog=offline_catalog,
        )

        def drive_main() -> None:
            require(
                main.session.analysis is not None, "Automatic workbook rows missing"
            )
            wait_for_preview(app, main.preview_pane)
            entries = tuple(main.session.spacing_entries)
            require(entries, "Native dialog fixture has no spacing image to review")
            require(
                main.preview_pane.selected_entry == entries[0]
                and main.preview_pane._index == 0
                and previewed
                and previewed[0]["kind"] == "spacing",
                "The first native dialog paint did not show a spacing image",
            )
            for handler in (main._on_add, main._on_edit):
                before = main.session.config
                main.specifications.SelectRow(0)

                def cancel_child(child: Any) -> None:
                    child.label.SetValue("Cancelled draft must not persist")
                    event(wx, stock_button(child, wx.ID_CANCEL), wx.EVT_BUTTON)

                with interact_modal(wx, SpecificationDialog, cancel_child):
                    handler(wx.CommandEvent())
                require(
                    main.session.config == before,
                    "Add/Edit Cancel changed parent intent",
                )
            require(main.session.analysis is not None, "Automatic workbook rows lost")
            import wx.grid as gridlib

            for entry in entries:
                select_spacing_preview(main.preview_pane, entry)
                wait_for_preview(app, main.preview_pane)
                require(
                    check_spacing_preview(main)["view_id"] == entry.view_id,
                    "Diagnostic compact summary or physical detail is stale",
                )
                app.Yield()
                require(
                    main.spacing_approve_button.IsEnabled(),
                    "Visible diagnostic approval button remains disabled",
                )
                approve_spacing_review(
                    app,
                    wx,
                    main,
                    main.spacing_approve_button,
                    entry,
                    verify_guides=True,
                    capture_path=(
                        output / "main-spacing-readable.png"
                        if not (output / "main-spacing-readable.png").exists()
                        else None
                    ),
                )
                require(
                    main.session.spacing_review_current(entry),
                    "Separate spacing image lacks explicit approval",
                )
            preview_indexes = workbook_preview_indexes(main.preview_pane)
            for index, section_id in enumerate(main._row_ids):
                event(wx, main.sections, gridlib.EVT_GRID_SELECT_CELL, index)
                wait_for_preview(
                    app,
                    main.preview_pane,
                    expected_index=preview_indexes[section_id],
                )
            main.SetSize((980, 620))
            app.Yield()
            wait_for_preview(app, main.preview_pane)
            capture_window(wx, main, output / "main-minimum.png")
            event(wx, main.approve_button, wx.EVT_BUTTON)
            require(main.session.approved, "Main approval failed after actual previews")
            event(wx, stock_button(main, wx.ID_OK), wx.EVT_BUTTON)

        require(modal(wx, main, drive_main) == wx.ID_OK, "Main dialog did not accept")
        require(
            main.config.specifications == plan.config.specifications,
            "Parent dialog altered cancelled drafts",
        )
        editor = SpecificationDialog(
            None,
            current(),
            original,
            preview=preview,
            verify_snapshot=verify,
            appearance_context=appearance,
        )

        def drive_editor() -> None:
            for layer in tuple(editor._layer_names):
                event(wx, editor.layer, wx.EVT_CHOICE, editor._layer_names.index(layer))
                require(editor.preview_pane.sections, "Layer has no native capture")
                entries = tuple(editor.preview_pane.entries)
                for entry in entries:
                    if editor._active_layer != layer:
                        event(
                            wx,
                            editor.layer,
                            wx.EVT_CHOICE,
                            editor._layer_names.index(layer),
                        )
                    event(
                        wx,
                        editor.preview_pane.checklist,
                        wx.EVT_LIST_ITEM_SELECTED,
                        editor.preview_pane.entries.index(entry),
                    )
                    wait_for_preview(app, editor.preview_pane)
                    app.Yield()
                    require(
                        editor.approve_layer.IsEnabled(),
                        "Visible image approval button remains disabled",
                    )
                    if hasattr(entry, "view_id"):
                        approve_spacing_review(
                            app,
                            wx,
                            editor,
                            editor.approve_layer,
                            entry,
                            verify_guides=True,
                            capture_path=(
                                output / "specification-spacing-readable.png"
                                if not (
                                    output / "specification-spacing-readable.png"
                                ).exists()
                                else None
                            ),
                        )
                    else:
                        event(wx, editor.approve_layer, wx.EVT_BUTTON)
                require(
                    editor._layer_drafts[layer].confirmed,
                    "Layer approval did not persist",
                )
            editor.SetSize((980, 620))
            app.Yield()
            wait_for_preview(app, editor.preview_pane)
            capture_window(wx, editor, output / "specification-minimum.png")
            event(wx, stock_button(editor, wx.ID_OK), wx.EVT_BUTTON)

        require(
            modal(wx, editor, drive_editor) == wx.ID_OK
            and editor.specification is not None,
            "Reviewed class editor did not accept",
        )
        require(
            editor.specification == original,
            "Reopened class editor changed exact saved dimensions",
        )
        reopened = SpecificationDialog(
            None,
            current(),
            editor.specification,
            preview=preview,
            verify_snapshot=verify,
            appearance_context=appearance,
        )
        require(
            modal(wx, reopened, reopened.Close) == wx.ID_CANCEL,
            "Native window-close did not cancel",
        )
        draft_editor = SpecificationDialog(
            None,
            current(),
            original,
            preview=preview,
            verify_snapshot=verify,
            appearance_context=appearance,
        )

        def apply_unfinished() -> None:
            draft_editor.target.SetValue("")
            event(wx, stock_button(draft_editor, wx.ID_OK), wx.EVT_BUTTON)

        require(
            modal(wx, draft_editor, apply_unfinished) == wx.ID_OK
            and draft_editor.specification is not None
            and draft_editor.specification.target_ohms == "",
            "Apply settings did not accept an unfinished target without review",
        )
        require(
            not draft_editor.review_tracking.layers,
            "Applying unfinished settings created a layer approval",
        )
        draft = replace(
            plan.config,
            reviewed_digest="",
            included_section_ids=(),
            specifications=tuple(
                draft_editor.specification if spec.spec_id == original.spec_id else spec
                for spec in plan.config.specifications
            ),
        )
        database = ImpedanceDatabase(output / "dialog-project.db")
        board_id = database.resolve_board(board.GetFileName())
        repository = ImpedanceRepository(database, board_id)
        revisions: list[int] = []

        def persist(config: Any) -> None:
            revisions.append(repository.save(config, 0))

        draft_main = ImpedanceDialog(
            None,
            draft,
            current(),
            preview,
            current,
            verify_snapshot=verify,
            verify_captures=captures.verify_captures,
            verify_spacing_captures=captures.verify_spacing_captures,
            prepare_capture=captures.prepare,
            appearance_context=appearance,
            fetch_stackup_catalog=offline_catalog,
            save_config=persist,
        )
        require(
            modal(
                wx,
                draft_main,
                lambda: event(wx, stock_button(draft_main, wx.ID_OK), wx.EVT_BUTTON),
            )
            == wx.ID_OK,
            "Save draft and close rejected unfinished settings",
        )
        restored, revision = ImpedanceRepository(
            ImpedanceDatabase(database.path), board_id
        ).load()
        require(
            revisions == [revision]
            and restored == draft_main.config
            and restored.specifications[0].target_ohms == "",
            "SQLite reopening lost the unfinished native draft",
        )
        draft_reopened = SpecificationDialog(
            None,
            current(),
            restored.specifications[0],
            preview=preview,
            verify_snapshot=verify,
            appearance_context=appearance,
        )

        def cancel_draft() -> None:
            require(
                draft_reopened.target.GetValue() == "",
                "Reopened native editor did not restore the empty target",
            )
            event(wx, stock_button(draft_reopened, wx.ID_CANCEL), wx.EVT_BUTTON)

        before_cancel = database.path.read_bytes()
        require(
            modal(wx, draft_reopened, cancel_draft) == wx.ID_CANCEL
            and database.path.read_bytes() == before_cancel,
            "Cancelling an unfinished draft changed persisted settings",
        )
        return {
            "native_dialogs": [
                "main",
                "Add Cancel",
                "Edit Cancel",
                "specification OK",
                "reopen window-close",
                "Apply unfinished settings without review",
                "Save draft and close to SQLite",
                "reopen unfinished draft and Cancel",
            ],
            "preview_requests": len(previewed),
            "preview_images": previewed,
            "native_capture_files": len(tuple(directory.glob("*.png"))),
            "capture_provider": "production ImpedanceControls and CaptureSession with native rendering and live source/appearance checks",
            "approved_layers": [item.layer for item in original.layer_settings],
            "catalog": "deterministic empty response; no live network exercised",
            "evidence_scope": "real wx constructors/events; no full plugin Generate UI",
        }
    finally:
        owner.close()


def check_workbook_images(document: Path, images: tuple[Path, ...]) -> None:
    """Prove H-row anchors embed exactly the freshly rendered native image bytes."""
    from impedance.service import CapturedImage

    ns = {
        "x": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
        "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }
    with ZipFile(document) as package:
        names = package.namelist()
        require(
            package.testzip() is None and len(names) == len(set(names)),
            "Workbook image ZIP integrity failed",
        )
        drawing = ET.fromstring(package.read("xl/drawings/drawing1.xml"))  # noqa: S314 - generated workbook.
        relationships = ET.fromstring(  # noqa: S314 - generated workbook.
            package.read("xl/drawings/_rels/drawing1.xml.rels")
        )
        anchors = drawing.findall("x:oneCellAnchor", ns)
        require(
            len(anchors) == len(relationships) == len(images),
            "Workbook image/anchor count differs from native rows",
        )
        require(
            all(item.get("TargetMode") is None for item in relationships),
            "Workbook has external image links",
        )
        targets = {item.get("Id"): item.get("Target") for item in relationships}
        require(
            len(targets) == len(images), "Workbook has duplicate image relationship IDs"
        )
        expected_media = {
            f"xl/media/image{index}.png" for index in range(1, len(images) + 1)
        }
        require(
            {name for name in names if name.startswith("xl/media/")} == expected_media,
            "Workbook has stale or missing image media",
        )
        for index, (anchor, image) in enumerate(zip(anchors, images), 1):
            require(
                anchor.findtext("x:from/x:row", namespaces=ns) == str(index)
                and anchor.findtext("x:from/x:col", namespaces=ns) == "7",
                "Workbook image anchor is on the wrong row",
            )
            embed = anchor.find("x:pic/x:blipFill/a:blip", ns)
            require(embed is not None, "Workbook image anchor has no embedded image")
            identity = embed.get("{" + ns["r"] + "}embed")
            require(
                identity == f"rId{index}" and identity in targets,
                "Workbook image relationship is ambiguous",
            )
            target = targets[identity]
            target = (
                target.lstrip("/")
                if target.startswith("/")
                else posixpath.normpath(posixpath.join("xl/drawings", target))
            )
            require(
                target == f"xl/media/image{index}.png",
                "Workbook image points outside its expected media",
            )
            data = package.read(target)
            require(
                data == image.read_bytes(),
                "Workbook image is not the corresponding native row capture",
            )
            capture = CapturedImage.from_bytes(data)
            require(
                (capture.width, capture.height) == (800, 420),
                "Workbook native image dimensions are invalid",
            )


def check_workflow_reports(plan: Any, artifacts: Any, scratch: Path) -> None:
    """Independently validate each report/archive against the immutable export input."""
    from impedance import service
    from impedance.model import resolved_layer_settings
    from scripts.native_impedance_workflow import check_spacing_report

    document = artifacts.workbook
    with ZipFile(document) as package:
        require(package.testzip() is None, "Workbook CRC failure")
        namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        sheet = ET.fromstring(package.read("xl/worksheets/sheet1.xml"))  # noqa: S314 - locally generated vendor-template XML.
        rows = sheet.findall("s:sheetData/s:row", namespace)[1:]
        require(
            len(rows) == len(plan.sections) and len(rows) > 2,
            "Combined USB workbook lost independently paired rows",
        )
        specifications = {spec.spec_id: spec for spec in plan.config.specifications}
        for row, section in zip(rows, plan.sections):
            spec = specifications[section.spec_id]
            settings = resolved_layer_settings(spec, section.layer)
            cells = {re.sub(r"\d", "", cell.get("r", "")): cell for cell in row}

            def value(column: str, cells: dict[str, Any] = cells) -> str:
                cell = cells[column]
                return (
                    cell.findtext("s:is/s:t", namespaces=namespace)
                    or cell.findtext("s:v", namespaces=namespace)
                    or ""
                )

            require(
                Decimal(value("G")) == Decimal(spec.target_ohms) == 90
                and Decimal(value("D"))
                == (Decimal(section.width_nm) / 25400).quantize(
                    Decimal("0.0001"), rounding=ROUND_HALF_UP
                ),
                "USB target or native width changed in workbook",
            )
            require(
                value("C")
                == (
                    "Differential Pair (Coplanar)"
                    if spec.kind.endswith("_coplanar")
                    else "Differential Pair (Non coplanar)"
                ),
                "USB workbook type changed",
            )
            if spec.kind.endswith("_coplanar"):
                gaps = dict(line.split(": ") for line in value("F").splitlines())
                pair = Decimal(gaps["Pair"])
                require(
                    Decimal(gaps["Ground"])
                    == (Decimal(settings.ground_gap_nm) / 25400).quantize(
                        Decimal("0.0001"), rounding=ROUND_HALF_UP
                    ),
                    "USB ground gap changed in workbook",
                )
            else:
                pair = Decimal(value("F"))
            require(
                pair == Decimal(settings.spacing_nm) / 25400 == 8,
                "USB pair gap changed in workbook",
            )
            require(
                value("A") == f"L{plan.snapshot.layers.index(section.layer) + 1}"
                and value("B")
                == ", ".join(
                    f"L{plan.snapshot.layers.index(layer) + 1}"
                    for layer in settings.reference_layers
                ),
                "Signal/reference layer mapping changed",
            )
        media = [name for name in package.namelist() if name.startswith("xl/media/")]
        require(
            len(media) == len(plan.sections),
            "Workbook media count differs from paired rows",
        )
        for name in media:
            require(
                (
                    service.CapturedImage.from_bytes(package.read(name)).width,
                    service.CapturedImage.from_bytes(package.read(name)).height,
                )
                == (800, 420),
                "Workbook image integrity failure",
            )

    check_spacing_report(
        artifacts.html_report.read_text(encoding="utf-8"),
        plan.config,
        plan.snapshot,
        plan.sections,
        require_spacing_reviews=True,
        workbook_bytes=document.read_bytes(),
    )
    check_workbook_images(
        document,
        tuple(
            scratch / f"impedance-section-{index:04d}.png"
            for index in range(1, len(plan.sections) + 1)
        ),
    )


def workflow(plan: Any, board: Any, pcbnew: Any, output: Path) -> dict[str, Any]:
    """Use actual native PNG export, real XLSX/ZIP and board-scoped SQLite."""
    from impedance import service
    from impedance.catalog_cache import CatalogCache
    from impedance.database import ImpedanceDatabase
    from impedance.model import Config
    from impedance.repository import ImpedanceRepository

    plan = fixture_spacing_review(plan, board, pcbnew, output)
    shared = output / "shared-project"
    shared.mkdir()
    source = Path(board.GetFileName())
    first, second = shared / "main.kicad_pcb", shared / "second.kicad_pcb"
    for target in (first, second):
        shutil.copy2(source, target)
    database = ImpedanceDatabase(shared / "jlcpcb" / "project.db")
    first_id, second_id = database.resolve_board(first), database.resolve_board(second)
    require(first_id != second_id, "Two boards shared a database identity")
    one, two = (
        ImpedanceRepository(database, first_id),
        ImpedanceRepository(database, second_id),
    )
    revision = one.save(plan.config, 0)
    require(not two.load()[0].enabled, "Second board inherited enabled intent")
    second_config = replace(
        plan.config,
        enabled=False,
        reviewed_digest="",
        included_section_ids=(),
        specifications=tuple(
            replace(spec, label="Other board's independent intent")
            for spec in plan.config.specifications
        ),
    )
    second_revision = two.save(second_config, 0)
    require(
        one.load() == (plan.config, revision), "Saving second board changed first board"
    )
    require(
        ImpedanceRepository(ImpedanceDatabase(database.path), first_id).load()
        == (plan.config, revision),
        "Reopening SQLite lost board configuration",
    )
    cache = CatalogCache(checked_at_utc="2026-09-14T12:00:00.000000Z")
    one.save_stackup_catalog(len(plan.snapshot.layers), cache)
    require(
        two.load_stackup_catalog(len(plan.snapshot.layers)) == cache
        and one.load() == (plan.config, revision)
        and two.load() == (second_config, second_revision),
        "Project-shared catalog cache changed board configuration",
    )
    scratch = output / "export"
    scratch.mkdir()
    artifacts = service.export_reports(plan, board, pcbnew, scratch)
    check_workflow_reports(plan, artifacts, scratch)
    disabled = replace(plan.config, enabled=False)
    one.save(disabled, revision)
    require(
        service.prepare(one.load()[0], plan.snapshot) is None,
        "Disabled board still produces an export plan",
    )
    require(
        two.load() == (second_config, second_revision),
        "Disabling first board changed second board",
    )
    copied = shared / "copied.kicad_pcb"
    shutil.copy2(first, copied)
    copied_id = database.resolve_board(copied)
    require(
        copied_id not in (first_id, second_id)
        and ImpedanceRepository(database, copied_id).load() == (Config(), 0),
        "A new filename inherited another board's impedance settings",
    )
    renamed = shared / "renamed.kicad_pcb"
    second.rename(renamed)
    renamed_id = database.resolve_board(renamed)
    require(
        renamed_id not in (first_id, second_id, copied_id)
        and ImpedanceRepository(database, renamed_id).load() == (Config(), 0)
        and one.load()[0] == disabled
        and two.load() == (second_config, second_revision),
        "A renamed filename inherited settings or changed an existing record",
    )
    moved = output / "moved-project"
    shutil.copytree(shared, moved)
    moved_database = ImpedanceDatabase(moved / "jlcpcb" / "project.db")
    require(
        moved_database.resolve_board(moved / first.name) == first_id
        and ImpedanceRepository(moved_database, first_id).load() == one.load()
        and ImpedanceRepository(moved_database, first_id).load_stackup_catalog(
            len(plan.snapshot.layers)
        )
        == cache,
        "Moving the whole project lost relative identity or persisted settings",
    )
    token = database.config_reset_token(second_id)
    reset_revision = database.reset_config(second_id, token, Config().to_dict())
    with database.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    require(
        reset_revision > second_revision
        and two.load() == (Config(), reset_revision)
        and one.load()[0] == disabled
        and "board_feature_recovery" not in tables,
        "Explicit impedance reset failed, created an archive, or changed another board",
    )
    return {
        "scope": "real service.export_reports/Excel/HTML and board-scoped SQLite; Generate/fabrication is exercised separately",
        "rows": len(plan.sections),
        "boards": [first_id, second_id, copied_id, renamed_id],
        "new_filenames_start_fresh": True,
        "project_move_preserves_identity": True,
        "project_shared_catalog_preserved": True,
        "reset_replaces_config_without_archive": True,
        "distinct_board_intents_preserved": True,
        "html_spacing_before_workbook": True,
    }


@contextmanager
def private_display(output: Path) -> Iterator[None]:
    """Give a worker its own X server, window manager and desktop preferences."""
    require(sys.platform.startswith("linux"), "Private displays require Linux.")
    session = output / "desktop"
    session.mkdir()
    saved = {
        key: os.environ.get(key)
        for key in ("DISPLAY", "XDG_CONFIG_HOME", "XDG_CACHE_HOME")
    }
    read_fd, write_fd = os.pipe()
    server = manager = None
    try:
        with (session / "xvfb.log").open("x", encoding="utf-8") as log:
            server = subprocess.Popen(
                [
                    "Xvfb",
                    "-displayfd",
                    str(write_fd),
                    "-screen",
                    "0",
                    "1920x1080x24",
                    "-nolisten",
                    "tcp",
                    "-ac",
                    "-noreset",
                ],
                pass_fds=(write_fd,),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        os.close(write_fd)
        write_fd = -1
        require(
            select.select([read_fd], [], [], 15)[0],
            "Private Xvfb did not allocate a display",
        )
        number = os.read(read_fd, 64).decode("ascii").strip()
        require(
            number.isdecimal() and server.poll() is None, "Private Xvfb failed to start"
        )
        os.environ["DISPLAY"] = ":" + number
        for key, name in (("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache")):
            path = session / name
            path.mkdir()
            os.environ[key] = str(path)
        with (session / "openbox.log").open("x", encoding="utf-8") as log:
            manager = subprocess.Popen(
                ["openbox", "--sm-disable"], stdout=log, stderr=subprocess.STDOUT
            )
        deadline = time.monotonic() + 15
        while True:
            require(
                manager.poll() is None, "Private Openbox exited before native tests"
            )
            probe = subprocess.run(
                ["xprop", "-root", "_NET_SUPPORTING_WM_CHECK"],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )
            if probe.returncode == 0 and "window id #" in probe.stdout:
                break
            require(time.monotonic() < deadline, "Private Openbox did not become ready")
            time.sleep(0.02)
        yield
        require(server.poll() is None, "Private Xvfb exited during native tests")
        require(manager.poll() is None, "Private Openbox exited during native tests")
    finally:
        cleanup_errors = []
        for process in (manager, server):
            if process is not None:
                try:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                except BaseException as error:
                    cleanup_errors.append(str(error))
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        require(
            not cleanup_errors,
            "Private desktop cleanup failed: " + "; ".join(cleanup_errors),
        )


def worker(
    stage: str, output: Path, *, isolated_display: bool = False, run_token: str = ""
) -> int:
    """Fail closed with stage-local evidence even when native initialization fails."""
    from scripts.native_impedance_workflow import (
        measure_phase,
        record_call_timings,
        record_impedance_timings,
        record_renderer_timings,
    )

    started = time.perf_counter()
    phases: dict[str, Any] = {}
    output = claim_output(output)
    record: dict[str, Any] = {"stage": stage, "status": "FAIL", "evidence": {}}
    if run_token:
        record["run_token"] = run_token
    app = None
    desktop = ExitStack()
    try:
        require(stage in STAGES, "Unknown native stage")
        if isolated_display:
            with measure_phase(phases, "private_desktop"):
                desktop.enter_context(private_display(output))
        with measure_phase(phases, "native_initialization"):
            configure_palette(output, stage == "custom-palette")
            pcbnew, wx, app, versions = native_runtime()
        record["evidence"]["runtime"] = versions
        if stage != "prerequisites":
            name = (
                "usb-differential-90-ohm"
                if stage in ("dialogs", "workflow", "generate")
                else ("single-ended-50-ohm" if stage == "custom-palette" else stage)
            )
            with ExitStack() as owner:
                with measure_phase(phases, "fixture_preparation"):
                    case, board, path = owner.enter_context(
                        loaded_fixture(name, output, pcbnew)
                    )
                if stage == "generate":
                    from scripts.native_impedance_workflow import (
                        seed_spacing_acceptance_fixture,
                    )

                    with measure_phase(phases, "spacing_fixture_preparation"):
                        case = seed_spacing_acceptance_fixture(case, board, pcbnew)
                with measure_phase(phases, "class_plan"):
                    plan = class_plan(case, board, pcbnew)
                if stage != "generate":
                    from impedance import pcbnew_adapter, render
                    from scripts import native_impedance_workflow

                    dialog_module = (
                        __import__("impedance.dialog", fromlist=["dialog"])
                        if stage == "dialogs"
                        else None
                    )
                    owner.enter_context(
                        record_impedance_timings(phases, pcbnew_adapter, dialog_module)
                    )
                    owner.enter_context(
                        record_renderer_timings(render.SectionRenderer, phases)
                    )
                    owner.enter_context(
                        record_call_timings(
                            phases,
                            tuple(
                                (sys.modules[__name__], name, "acceptance." + name)
                                for name in (
                                    "check_png",
                                    "check_workbook_images",
                                    "fixture_spacing_review",
                                    "wait_for_preview",
                                    "capture_window",
                                    "modal",
                                )
                            ),
                        )
                    )
                    owner.enter_context(
                        record_call_timings(
                            phases,
                            tuple(
                                (
                                    native_impedance_workflow,
                                    name,
                                    "acceptance." + name,
                                )
                                for name in (
                                    "approve_spacing_review",
                                    "check_spacing_preview",
                                    "check_spacing_guide_pixels",
                                    "check_spacing_report",
                                )
                            ),
                        )
                    )
                    if stage == "workflow":
                        from impedance import service
                        from impedance.repository import ImpedanceRepository

                        owner.enter_context(
                            record_call_timings(
                                phases,
                                (
                                    (service, "export_reports", "reports.export"),
                                    (ImpedanceRepository, "save", "storage.save"),
                                    (ImpedanceRepository, "load", "storage.load"),
                                ),
                            )
                        )
                if stage == "dialogs":
                    with measure_phase(phases, "dialogs"):
                        evidence = dialogs(plan, board, pcbnew, wx, app, output)
                elif stage == "workflow":
                    with measure_phase(phases, "storage_and_report_workflow"):
                        evidence = workflow(plan, board, pcbnew, output)
                elif stage == "generate":
                    from scripts.native_impedance_workflow import generate_workflow

                    with measure_phase(phases, "generate_workflow"):
                        evidence = generate_workflow(
                            plan, board, pcbnew, wx, app, output
                        )
                else:
                    evidence = {
                        "captures": render_rows(
                            plan, board, pcbnew, wx, output, stage == "custom-palette"
                        ),
                        "renderer": "real pcbnew PLOT_CONTROLLER + production SectionRenderer + wx.svg",
                        "board": str(path.relative_to(output)),
                    }
                record["evidence"].update(evidence)
                with measure_phase(phases, "review_config_artifact"):
                    write_json(output / "review-config.json", plan.config.to_dict())
                # Do not retain a board wrapper beyond the owner's native scope.
                del board
        record["status"] = "PASS"
    except BaseException as error:
        record["error"] = str(error)
        record["traceback"] = traceback.format_exc()
        sys.stderr.write(record["traceback"] + "\n")
        sys.stderr.flush()
    finally:
        if app is not None:
            try:
                with measure_phase(phases, "app_cleanup"):
                    app.Destroy()
            except BaseException as error:
                record["status"] = "FAIL"
                record["app_cleanup_error"] = str(error)
        try:
            desktop.close()
        except BaseException as error:
            record["status"] = "FAIL"
            record["desktop_cleanup_error"] = str(error)
    record["performance"] = {
        "seconds": time.perf_counter() - started,
        "phases": phases,
        "diagnostic_only": stage == "generate"
        and os.environ.get("KICAD_IMPEDANCE_PROFILE_GENERATE") == "1",
        "scope": "complete worker including native initialization, fixture preparation and cleanup; supervisor duration also includes process startup and result publication",
        "phase_accounting": "inclusive wall times; nested and event-loop phases overlap and must not be added",
    }
    write_json(output / "result.json", record)
    return int(record["status"] != "PASS")


def main(argv: Optional[list[str]] = None) -> int:
    """Run the bounded suite or one supervisor-owned native stage."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=1)
    parser.add_argument(
        "--generate-only",
        action="store_true",
        help="Benchmark complete Generate workers",
    )
    parser.add_argument("--generate-runs", type=int, default=1)
    parser.add_argument("--generate-budget-seconds", type=float)
    parser.add_argument("--worker", choices=STAGES, help=argparse.SUPPRESS)
    parser.add_argument(
        "--private-display", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument("--run-token", default="", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        return (
            worker(
                args.worker,
                args.output,
                isolated_display=args.private_display,
                run_token=args.run_token,
            )
            if args.worker
            else run(
                args.output,
                args.timeout,
                generate_only=args.generate_only,
                generate_runs=args.generate_runs,
                generate_budget_seconds=args.generate_budget_seconds,
                workers=args.workers,
            )
        )
    except (ValueError, OSError) as error:
        sys.stderr.write(str(error) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
