"""Bounded Linux KiCad/GTK acceptance, separate from mocked unit tests.

Run with Python that can import KiCad and wxGTK, under Xvfb. Every stage runs in a
fresh process; unsupported bindings, crashes and timeouts are failures, not skips.
Only fresh artifact directories and copies of checked-in boards are writable.
The Generate stage exercises the actual plugin constructor and toolbar workflow.
"""

import argparse
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
import sys
import time
import traceback
from typing import Any, Optional
from xml.etree import ElementTree as ET
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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


def run(output: Path, timeout: int = 240) -> int:
    """Supervise the native processes and preserve explicit failure evidence."""
    if type(timeout) is not int or not 0 < timeout <= 3600:
        raise ValueError("The stage timeout must be between 1 and 3600 seconds.")
    output = claim_output(output)
    records = []
    failed = False
    for stage in STAGES:
        record: dict[str, Any] = {"stage": stage, "status": "NOT_RUN"}
        if not failed:
            started = time.monotonic()
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker",
                stage,
                "--output",
                str(output / stage),
            ]
            log = ""
            try:
                result = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=ROOT,
                )
                log = result.stdout + "\n" + result.stderr
                require(
                    result.returncode == 0, f"Native process exited {result.returncode}"
                )
                result_path = output / stage / "result.json"
                require(not result_path.is_symlink(), "Native result is a symlink")
                details = json.loads(result_path.read_text(encoding="utf-8"))
                require(
                    isinstance(details, dict)
                    and details.get("stage") == stage
                    and details.get("status") == "PASS"
                    and isinstance(details.get("evidence"), dict),
                    "Missing or invalid native PASS evidence",
                )
                record.update(details)
            except Exception as error:
                if isinstance(error, subprocess.TimeoutExpired):
                    captured = error.stdout or ""
                    log += (
                        captured.decode(errors="replace")
                        if isinstance(captured, bytes)
                        else captured
                    )
                record.update(status="FAIL", error=str(error))
                failed = True
                log += "\n" + traceback.format_exc()
            record["duration_seconds"] = round(time.monotonic() - started, 3)
            record["log"] = f"{stage}.log"
            (output / record["log"]).write_text(log, encoding="utf-8")
        records.append(record)
        sys.stdout.write(f"{stage}: {record['status']}\n")
        sys.stdout.flush()
    write_json(
        output / "summary.json",
        {
            "schema_version": 1,
            "status": "FAIL" if failed else "PASS",
            "stages": records,
            "scope": "native pcbnew/GTK, Configure/calculation/review/Save/reopen/Generate, real fabrication reports and publication",
        },
    )
    return int(failed)


def native_runtime() -> tuple[Any, Any, Any, dict[str, Any]]:
    """Require real Linux bindings, a display and matching stable KiCad 10.0 tools."""
    require(sys.platform.startswith("linux"), "This acceptance runner requires Linux.")
    require(os.environ.get("DISPLAY"), "DISPLAY is missing; run under Xvfb.")
    import _pcbnew
    import pcbnew
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
    from impedance.matching import analyze
    from impedance.model import Config
    from impedance.pcbnew_adapter import snapshot_board
    from impedance.service import prepare

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
    if case.paired:
        pairs = {tuple(sorted(circuit.net_names)) for circuit in case.circuits}
        require(
            pairs <= {tuple(sorted(pair)) for pair in snapshot.differential_pairs},
            "Native differential mate lookup failed",
        )
        require(
            all(
                tuple(sorted(section.net_names)) in pairs
                for section in analysis.sections
            ),
            "A differential workbook row omitted a mate",
        )
    config = replace(
        config,
        reviewed_digest=analysis.digest,
        included_section_ids=tuple(section.section_id for section in analysis.sections),
    )
    plan = prepare(config, snapshot, len(snapshot.layers))
    require(
        plan is not None and plan.sections,
        "Native class selection produced no export plan",
    )
    return plan


def check_yellow_edges(pixels: list[bytes], box: tuple[float, ...]) -> None:
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
                    pixels[row * 800 + column] == b"\xff\xff\x00"
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
    pixels = [raw[index : index + 3] for index in range(0, len(raw), 3)]
    require(len(set(pixels)) > 16, "Capture is blank or effectively monochrome")
    yellow = [
        (index % 800, index // 800)
        for index, pixel in enumerate(pixels)
        if pixel == b"\xff\xff\x00"
    ]
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
    check_yellow_edges(pixels, expected)
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
        # documented 60%-opaque copper composite. Native premultiplied-alpha
        # rasterization can round each channel by a few levels.
        colors = Counter(pixels)
        background = (247, 249, 251)
        copper = {
            "F.Cu": (23, 43, 61),
            "In2.Cu": (206, 125, 44),
            "B.Cu": (77, 127, 196),
        }[section.layer]
        expected_plane = tuple(
            round(0.6 * foreground + 0.4 * canvas)
            for foreground, canvas in zip(copper, background)
        )
        canvas_count = colors[bytes(background)]
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
            require(
                any(
                    max(
                        abs(pixel[i] - channel)
                        for i, channel in enumerate((23, 43, 61))
                    )
                    < 8
                    for pixel in pixels
                ),
                "Selected custom copper color was not rendered",
            )
    return {
        "png": path.name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "colors": len(set(pixels)),
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
            sum(record["light_canvas_pixels"] for record in records) > 1000,
            "Custom-palette gallery never exposed the selected light canvas",
        )
    return records


def stock_button(dialog: Any, identity: int) -> Any:
    """Find a descendant stock button, never a globally matching parent dialog."""
    control = dialog.FindWindow(identity)
    require(control is not None, "Native dialog is missing its stock button")
    return control


def event(wx: Any, control: Any, binder: Any, selection: Optional[int] = None) -> None:
    """Dispatch a real native command event through the existing bound handler."""
    if selection is not None:
        control.SetSelection(selection)
    command = wx.CommandEvent(binder.typeId, control.GetId())
    command.SetEventObject(control)
    if selection is not None:
        command.SetInt(selection)
    require(
        control.GetEventHandler().ProcessEvent(command),
        "Native control event was not handled",
    )


def modal(wx: Any, dialog: Any, action: Callable[[], None]) -> int:
    """Drive a real modal loop, propagating callback failures instead of hanging."""
    failures = []

    def drive() -> None:
        try:
            action()
        except BaseException as error:
            failures.append(error)
            if dialog.IsModal():
                dialog.EndModal(wx.ID_CANCEL)

    timer = wx.CallLater(250, drive)
    try:
        result = dialog.ShowModal()
        if failures:
            raise failures[0]
        return result
    finally:
        timer.Stop()
        dialog.Destroy()


def capture_window(wx: Any, dialog: Any, output: Path) -> None:
    """Retain the actual Xvfb dialog pixels, not a reconstructed mockup."""
    dialog.Layout()
    dialog.Update()
    size, position = dialog.GetSize(), dialog.GetScreenPosition()
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


def wait_for_preview(app: Any, pane: Any, timeout: float = 15.0) -> None:
    """Pump native events until a visible paint has produced verified evidence.

    Dispatching a selection queues capture and paint callbacks; neither a decoded
    PNG nor a pending paint is evidence that the user actually saw the image.
    """
    deadline = time.monotonic() + timeout
    while not pane.ready:
        require(not pane.failure, pane.failure)
        require(time.monotonic() < deadline, "Native preview did not finish displaying")
        app.Yield()
        pane.canvas.Update()
        if not pane.ready:
            time.sleep(0.01)
    require(
        pane.canvas.IsShownOnScreen()
        and pane.canvas._bitmap is not None
        and pane.canvas._bitmap.IsOk(),
        "Native preview has no visible painted bitmap",
    )


def dialogs(
    plan: Any, board: Any, pcbnew: Any, wx: Any, app: Any, output: Path
) -> dict[str, Any]:
    """Bound real GTK constructors, nested events, visible layer reviews and reopen."""
    from impedance.dialog import ImpedanceDialog, SpecificationDialog
    from impedance.pcbnew_adapter import snapshot_board
    from impedance.render import SectionRenderer
    from impedance.service import CapturedImage

    previewed = []
    renderer = SectionRenderer(board, pcbnew)

    def current() -> Any:
        return snapshot_board(board, pcbnew)

    def verify(expected: Any) -> None:
        require(current() == expected, "Board changed; rescan and review")

    def preview(section: Any, *, refresh: bool = False) -> CapturedImage:
        """Use the current provider contract; this harness renders on every call."""
        path = output / f"dialog-capture-{len(previewed) + 1}.png"
        renderer.render(section, path, 800, 420)
        check_png(path, section, board, wx)
        previewed.append(section.section_id)
        return CapturedImage.load(path)

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
            fetch_stackup_catalog=offline_catalog,
        )

        def drive_main() -> None:
            require(
                main.session.analysis is not None, "Automatic workbook rows missing"
            )
            wait_for_preview(app, main.preview_pane)
            for handler in (main._on_add, main._on_edit):
                before = main.session.config
                main.specifications.Select(0)
                errors = []

                def cancel_child(errors: list[BaseException] = errors) -> None:
                    try:
                        children = [
                            window
                            for window in wx.GetTopLevelWindows()
                            if isinstance(window, SpecificationDialog)
                            and window.IsModal()
                        ]
                        require(
                            len(children) == 1,
                            "Expected one real nested specification dialog",
                        )
                        child = children[0]
                        child.label.SetValue("Cancelled draft must not persist")
                        event(wx, stock_button(child, wx.ID_CANCEL), wx.EVT_BUTTON)
                    except BaseException as error:
                        errors.append(error)
                        for window in wx.GetTopLevelWindows():
                            if (
                                window is not main
                                and isinstance(window, wx.Dialog)
                                and window.IsModal()
                            ):
                                window.EndModal(wx.ID_CANCEL)

                timer = wx.CallLater(250, cancel_child)
                try:
                    handler(wx.CommandEvent())
                finally:
                    timer.Stop()
                require(not errors, str(errors))
                require(
                    main.session.config == before,
                    "Add/Edit Cancel changed parent intent",
                )
            require(main.session.analysis is not None, "Automatic workbook rows lost")
            for index in range(len(main.session.analysis.sections)):
                event(wx, main.sections, wx.EVT_LISTBOX, index)
                wait_for_preview(app, main.preview_pane)
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
            None, current(), original, preview=preview, verify_snapshot=verify
        )

        def drive_editor() -> None:
            for layer in tuple(editor._layer_names):
                event(wx, editor.layer, wx.EVT_CHOICE, editor._layer_names.index(layer))
                require(editor.preview_pane.sections, "Layer has no native capture")
                for index in range(len(editor.preview_pane.sections)):
                    event(wx, editor.preview_pane.rows, wx.EVT_CHOICE, index)
                    wait_for_preview(app, editor.preview_pane)
                editor.preview_pane.ensure_reviewed()
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
        )
        require(
            modal(wx, reopened, reopened.Close) == wx.ID_CANCEL,
            "Native window-close did not cancel",
        )
        return {
            "native_dialogs": [
                "main",
                "Add Cancel",
                "Edit Cancel",
                "specification OK",
                "reopen window-close",
            ],
            "rendered_previews": len(previewed),
            "approved_layers": [item.layer for item in original.layer_settings],
            "catalog": "deterministic empty response; no live network exercised",
            "evidence_scope": "real wx constructors/events; no full plugin Generate UI",
        }
    finally:
        renderer.close()


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


def workflow(plan: Any, board: Any, pcbnew: Any, output: Path) -> dict[str, Any]:
    """Use actual native PNG export, real XLSX/ZIP and board-scoped SQLite."""
    from impedance import service
    from impedance.catalog_cache import CatalogCache
    from impedance.database import ImpedanceDatabase
    from impedance.model import Config, resolved_layer_settings
    from impedance.repository import ImpedanceRepository

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
    document = artifacts.workbook
    check_workbook_images(
        document,
        tuple(
            scratch / f"impedance-section-{index:04d}.png"
            for index in range(1, len(plan.sections) + 1)
        ),
    )
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
                and abs(Decimal(value("D")) - Decimal(section.width_nm) / 25400)
                < Decimal("1e-12"),
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
                    Decimal(gaps["Ground"]) == Decimal(settings.ground_gap_nm) / 25400,
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
    }


def worker(stage: str, output: Path) -> int:
    """Fail closed with stage-local evidence even when native initialization fails."""
    output = claim_output(output)
    record: dict[str, Any] = {"stage": stage, "status": "FAIL", "evidence": {}}
    app = None
    try:
        require(stage in STAGES, "Unknown native stage")
        configure_palette(output, stage == "custom-palette")
        pcbnew, wx, app, versions = native_runtime()
        record["evidence"]["runtime"] = versions
        if stage != "prerequisites":
            name = (
                "usb-differential-90-ohm"
                if stage in ("dialogs", "workflow", "generate")
                else ("single-ended-50-ohm" if stage == "custom-palette" else stage)
            )
            with loaded_fixture(name, output, pcbnew) as (case, board, path):
                plan = class_plan(case, board, pcbnew)
                if stage == "dialogs":
                    evidence = dialogs(plan, board, pcbnew, wx, app, output)
                elif stage == "workflow":
                    evidence = workflow(plan, board, pcbnew, output)
                elif stage == "generate":
                    from scripts.native_impedance_workflow import generate_workflow

                    evidence = generate_workflow(plan, board, pcbnew, wx, app, output)
                else:
                    evidence = {
                        "captures": render_rows(
                            plan, board, pcbnew, wx, output, stage == "custom-palette"
                        ),
                        "renderer": "real pcbnew PLOT_CONTROLLER + production SectionRenderer + wx.svg",
                        "board": str(path.relative_to(output)),
                    }
                record["evidence"].update(evidence)
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
                app.Destroy()
            except BaseException as error:
                record["status"] = "FAIL"
                record["app_cleanup_error"] = str(error)
    write_json(output / "result.json", record)
    return int(record["status"] != "PASS")


def main(argv: Optional[list[str]] = None) -> int:
    """Run the bounded suite or one supervisor-owned native stage."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--worker", choices=STAGES, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        return (
            worker(args.worker, args.output)
            if args.worker
            else run(args.output, args.timeout)
        )
    except (ValueError, OSError) as error:
        sys.stderr.write(str(error) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
