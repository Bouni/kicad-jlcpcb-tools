"""Actual Linux plugin workflow with isolated files and deterministic supplier data.

Only the catalog and remote calculator responses are substituted. Native board,
wx controls, modal loops, SQLite, Gerber/Excellon plotting, report rendering, and
atomic publication use production implementations. Never run inside the editor.
"""

from base64 import b64decode
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, is_dataclass, replace
from decimal import Decimal
from functools import wraps
import hashlib
from html.parser import HTMLParser
import importlib
from io import BytesIO, StringIO
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
from threading import Event
import time
from types import CodeType, FunctionType, ModuleType, SimpleNamespace
from typing import Any, Optional
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5
from weakref import WeakKeyDictionary, WeakSet
from zipfile import BadZipFile, ZipFile

WORKBOOK = "Required_impedance_control.xlsx"
HTML = "Required_impedance_control.html"
SPACING_FIXTURE_CLASS = "Spacing acceptance"
SPACING_QUIET_NETS = ("SPACING_ACCEPTANCE_QUIET_D+", "SPACING_ACCEPTANCE_QUIET_D-")
SPACING_DEFECT_NETS = ("SPACING_ACCEPTANCE_DEFECT_D+", "SPACING_ACCEPTANCE_DEFECT_D-")


@contextmanager
def measure_phase(phases: dict[str, Any], name: str) -> Iterator[None]:
    """Accumulate wall time, including failures; nested phases are not additive."""
    started = time.perf_counter()
    try:
        yield
    finally:
        measurement = phases.setdefault(name, {"calls": 0, "seconds": 0.0})
        measurement["calls"] += 1
        measurement["seconds"] += time.perf_counter() - started


@contextmanager
def record_call_timings(
    phases: dict[str, Any], targets: tuple[tuple[Any, str, str], ...]
) -> Iterator[None]:
    """Observe real callbacks, including failures, with overlapping wall timings."""

    def observe(native: Callable[..., Any], name: str) -> Callable[..., Any]:
        @wraps(native)
        def measured(*args: Any, **kwargs: Any) -> Any:
            with measure_phase(phases, name):
                return native(*args, **kwargs)

        return measured

    with ExitStack() as owner:
        for target, attribute, name in targets:
            native = getattr(target, attribute)
            owner.enter_context(patch.object(target, attribute, observe(native, name)))
        yield


@contextmanager
def record_source_snapshot_timings(
    phases: dict[str, Any], source: Any
) -> Iterator[None]:
    """Observe fresh evidence and full builds independently at each reader call."""
    native_read = source.SourceSnapshotReader.read
    reported: WeakKeyDictionary[Any, dict[str, int]] = WeakKeyDictionary()

    def counters(reader: Any) -> dict[str, int]:
        statistics = getattr(reader, "statistics", {})
        values = asdict(statistics) if is_dataclass(statistics) else statistics
        result = {
            name: value
            for name, value in values.items()
            if type(value) is int and value >= 0
        }
        result.update(
            ("fallback_reason." + reason, count)
            for reason, count in values.get("fallback_reasons", {}).items()
            if type(count) is int and count >= 0
        )
        return result

    @wraps(native_read)
    def read(
        reader: Any,
        acquire: Callable[[], Any],
        build: Callable[[], Any],
        *,
        owner_key: Any,
        force: bool = False,
    ) -> Any:
        if reader not in reported:
            reported[reader] = counters(reader)

        def measured_acquire() -> Any:
            with measure_phase(phases, "source_snapshot.evidence"):
                return acquire()

        def measured_build() -> Any:
            with measure_phase(phases, "source_snapshot.build"):
                return build()

        try:
            with measure_phase(phases, "source_snapshot.read"):
                return native_read(
                    reader,
                    measured_acquire,
                    measured_build,
                    owner_key=owner_key,
                    force=force,
                )
        finally:
            current = counters(reader)
            previous = reported[reader]
            reported[reader] = current
            for name, value in current.items():
                delta = value - previous.get(name, 0)
                if delta > 0:
                    receipt = phases.setdefault(
                        "source_snapshot.count." + name,
                        {"calls": 0, "seconds": 0.0},
                    )
                    receipt["calls"] += delta

    with (
        patch.object(source.SourceSnapshotReader, "read", read),
        record_call_timings(
            phases,
            ((source, "acquire_source_evidence", "source_snapshot.native_evidence"),),
        ),
    ):
        yield


@contextmanager
def record_impedance_timings(
    phases: dict[str, Any], adapter: Any, dialog: Any = None
) -> Iterator[None]:
    """Separate native snapshot and GUI work without reusing freshness results.

    Every counter is inclusive wall time. In particular snapshot suboperations,
    synchronous updates, event-loop paint and modal interaction overlap their
    callers; these values must never be added to estimate a worker duration.
    """
    targets = [
        (adapter, "snapshot_board", "board_snapshot"),
        (adapter, "_copper_features", "board_snapshot.copper_features"),
        (adapter, "_context_digest", "board_snapshot.context_digest"),
        (adapter, "netclass_context", "board_snapshot.netclass_context"),
        (adapter, "pairing_context", "board_snapshot.pairing_context"),
    ]
    model = importlib.import_module(adapter.__package__ + ".model")
    targets.append((model.BoardSnapshot, "__eq__", "snapshot_equality"))
    if dialog is not None:
        preview = importlib.import_module(dialog.__package__ + ".dialog_preview")
        targets.extend(
            (
                (dialog.ImpedanceDialog, "__init__", "dialog.main.construct"),
                (
                    dialog.SpecificationDialog,
                    "__init__",
                    "dialog.specification.construct",
                ),
                (dialog.ImpedanceDialog, "_refresh_rows", "dialog.main.refresh_rows"),
                (
                    dialog.ImpedanceDialog,
                    "_update_spacing_summary",
                    "dialog.main.spacing_summary",
                ),
                (
                    dialog.ImpedanceDialog,
                    "_update_selection_buttons",
                    "dialog.main.selection_buttons",
                ),
                (
                    dialog.ImpedanceDialog,
                    "_populate_specifications",
                    "dialog.main.specifications",
                ),
                (
                    dialog.ImpedanceDialog,
                    "_populate_workbook_status",
                    "dialog.main.workbook_status",
                ),
                (
                    dialog.ImpedanceDialog,
                    "_populate_review_status",
                    "dialog.main.review_status",
                ),
                (
                    dialog.SpecificationDialog,
                    "_update_controls",
                    "dialog.specification.update_controls",
                ),
                (
                    dialog.SpecificationDialog,
                    "_refresh_preview",
                    "dialog.specification.refresh_preview",
                ),
                (preview.WorkbookPreview, "select", "preview.select"),
                (preview.WorkbookPreview, "_record_display", "preview.record_display"),
                (
                    preview.WorkbookPreview,
                    "refresh_review_labels",
                    "preview.review_labels",
                ),
                (preview.PreviewCanvas, "_paint", "preview.paint"),
                (
                    preview.EnlargedCaptureDialog,
                    "__init__",
                    "dialog.enlarged.construct",
                ),
            )
        )
        if hasattr(dialog.ReviewSession, "spacing_review_presentation"):
            targets.append(
                (
                    dialog.ReviewSession,
                    "spacing_review_presentation",
                    "dialog.spacing_review_presentation",
                )
            )
    source = importlib.import_module(adapter.__package__ + ".source_evidence")
    with (
        record_source_snapshot_timings(phases, source),
        record_call_timings(phases, tuple(targets)),
    ):
        yield


@contextmanager
def record_renderer_timings(
    renderer_type: type,
    phases: dict[str, Any],
    *,
    roles: Optional[Counter[str]] = None,
    rendered_inputs: Optional[dict[str, Counter[str]]] = None,
) -> Iterator[None]:
    """Copy renderer counters before native cleanup, without retaining its board."""
    native_close = renderer_type.close
    recorded: WeakSet[Any] = WeakSet()

    def close(renderer: Any) -> None:
        if renderer not in recorded:
            recorded.add(renderer)
            for name, measurement in getattr(renderer, "render_timings", {}).items():
                total = phases.setdefault(
                    "render." + name, {"calls": 0, "seconds": 0.0}
                )
                total["calls"] += measurement["calls"]
                total["seconds"] += measurement["seconds"]
        native_close(renderer)

    def observe(role: str, native: Callable[..., Any]) -> Callable[..., Any]:
        def render(renderer: Any, *args: Any, **kwargs: Any) -> Any:
            with measure_phase(phases, "render_call." + role):
                result = native(renderer, *args, **kwargs)
            if roles is not None:
                roles[role] += 1
            if rendered_inputs is not None:
                item = (
                    args[0]
                    if args
                    else kwargs.get("section" if role == "trace" else "entry")
                )
                identity = getattr(item, "section_id" if role == "trace" else "view_id")
                rendered_inputs.setdefault(role, Counter())[identity] += 1
            return result

        return render

    with ExitStack() as owner:
        owner.enter_context(patch.object(renderer_type, "close", close))
        if roles is not None or rendered_inputs is not None:
            for name, role in (
                ("render", "trace"),
                ("render_spacing", "spacing"),
                ("render_spacing_original", "original"),
            ):
                native = getattr(renderer_type, name)
                owner.enter_context(
                    patch.object(renderer_type, name, observe(role, native))
                )
        yield


def unique_profile_statistics(profiler: Any) -> dict[str, Any]:
    """Preserve code identity when generated methods share pstats display keys."""
    labels: dict[int, list[str]] = {}
    seen_classes: set[int] = set()
    for module_name, module in tuple(sys.modules.items()):
        if "impedance" not in module_name or not isinstance(module, ModuleType):
            continue
        for candidate in tuple(vars(module).values()):
            if (
                not isinstance(candidate, type)
                or id(candidate) in seen_classes
                or not is_dataclass(candidate)
            ):
                continue
            seen_classes.add(id(candidate))
            for name, member in vars(candidate).items():
                if isinstance(member, (staticmethod, classmethod)):
                    member = member.__func__
                seen_functions: set[int] = set()
                while (
                    isinstance(member, FunctionType)
                    and id(member) not in seen_functions
                ):
                    seen_functions.add(id(member))
                    label = (
                        candidate.__module__ + "." + candidate.__qualname__ + "." + name
                    )
                    code_labels = labels.setdefault(id(member.__code__), [])
                    if label not in code_labels:
                        code_labels.append(label)
                    member = getattr(member, "__wrapped__", None)

    def describe(entry: Any) -> dict[str, Any]:
        code = entry.code
        python_code = isinstance(code, CodeType)
        qualified_names = sorted(labels.get(id(code), ()))
        return {
            "code_id": hex(id(code)),
            "name": code.co_name if python_code else str(code),
            "filename": code.co_filename if python_code else "",
            "line": code.co_firstlineno if python_code else 0,
            "qualified_names": qualified_names,
            "generated_dataclass": bool(
                python_code and code.co_filename == "<string>" and qualified_names
            ),
            "call_count": entry.callcount,
            "recursive_calls": entry.reccallcount,
            "total_seconds": entry.totaltime,
            "inline_seconds": entry.inlinetime,
        }

    raw = profiler.getstats()
    entries = []
    for index, entry in enumerate(raw):
        record = describe(entry)
        record["entry_id"] = "entry-" + str(index)
        record["calls"] = [describe(child) for child in entry.calls or ()]
        entries.append(record)
    entries.sort(key=lambda entry: entry["inline_seconds"], reverse=True)
    generated = [entry for entry in entries if entry["generated_dataclass"]]
    return {
        "schema_version": 1,
        "diagnostic_only": True,
        "performance_acceptance_eligible": False,
        "getstats_entry_count": len(raw),
        "entries": entries,
        "generated_dataclass_entry_ids": [entry["entry_id"] for entry in generated],
        "generated_dataclass_totals": {
            "entry_count": len(generated),
            "call_count": sum(entry["call_count"] for entry in generated),
            "recursive_calls": sum(entry["recursive_calls"] for entry in generated),
            "inline_seconds": sum(entry["inline_seconds"] for entry in generated),
            "total_seconds": sum(entry["total_seconds"] for entry in generated),
        },
        "time_accounting": "inline time excludes nested calls; inclusive total times overlap and must not be summed as wall time",
    }


def unique_profile_summary(evidence: dict[str, Any]) -> str:
    """Show generated method costs and every code entry without pstats collisions."""
    totals = evidence["generated_dataclass_totals"]
    lines = [
        "Profiling diagnostic only; not performance acceptance.",
        "Code identity is preserved; generated methods are read without mutation.",
        evidence["time_accounting"],
        "Generated dataclass totals: " + json.dumps(totals, sort_keys=True),
        "",
        "Generated dataclass entries, descending inline time:",
    ]
    generated = [entry for entry in evidence["entries"] if entry["generated_dataclass"]]
    for heading, entries in (
        (None, generated),
        ("All getstats entries, descending inline time:", evidence["entries"]),
    ):
        if heading:
            lines.extend(("", heading))
        lines.append(
            "calls  inline seconds  inclusive seconds  code identity and qualified name"
        )
        for entry in entries:
            names = " | ".join(entry["qualified_names"]) or entry["name"]
            lines.append(
                f"{entry['call_count']:8d} {entry['inline_seconds']:14.6f} "
                f"{entry['total_seconds']:17.6f} {entry['entry_id']} {entry['code_id']} "
                f"{names} ({entry['filename']}:{entry['line']})"
            )
    return "\n".join(lines) + "\n"


@contextmanager
def profile_native_operation(destination: Path) -> Iterator[None]:
    """Write opt-in diagnostic statistics; these runs cannot establish speed."""
    import cProfile
    import pstats

    profiler = cProfile.Profile()
    previous = sys.getprofile()
    profiler.enable()
    try:
        yield
    finally:
        profiler.disable()
        try:
            profiler.dump_stats(str(destination))
            summary = StringIO()
            summary.write("Profiling diagnostic only; not performance acceptance.\n")
            pstats.Stats(profiler, stream=summary).strip_dirs().sort_stats(
                "cumulative"
            ).print_stats(80)
            destination.with_suffix(".txt").write_text(
                summary.getvalue(), encoding="utf-8"
            )
            evidence = unique_profile_statistics(profiler)
            destination.with_suffix(".getstats.json").write_text(
                json.dumps(evidence, indent=2, sort_keys=True, allow_nan=False) + "\n",
                encoding="utf-8",
            )
            destination.with_suffix(".getstats.txt").write_text(
                unique_profile_summary(evidence), encoding="utf-8"
            )
        finally:
            if callable(getattr(previous, "enable", None)):
                previous.enable()


@contextmanager
def profile_first_call(
    owner_type: type, name: str, destination: Path, *, enabled: bool
) -> Iterator[None]:
    """Profile one actual native callback without changing its inputs or outputs."""
    if not enabled:
        yield
        return
    native = getattr(owner_type, name)
    used = False

    def first(owner: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal used
        if used:
            return native(owner, *args, **kwargs)
        used = True
        with profile_native_operation(destination):
            return native(owner, *args, **kwargs)

    with patch.object(owner_type, name, first):
        yield


@contextmanager
def record_workflow_progress(
    destination: Path, evidence: Callable[[], dict[str, Any]]
) -> Iterator[None]:
    """Flush actual partial counters even when the native workflow fails."""
    failure = None
    try:
        yield
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            result = evidence()
            result["status"] = "FAIL" if failure is not None else "PASS"
            if failure is not None:
                result["error"] = str(failure)
            with destination.open("x", encoding="utf-8") as stream:
                json.dump(result, stream, indent=2, sort_keys=True)
                stream.write("\n")
        except BaseException:
            if failure is None:
                raise
            logging.getLogger(__name__).exception(
                "Could not preserve partial native performance evidence"
            )


def write_diagnostic_heartbeat(
    destination: Path, evidence: dict[str, Any], phase: str
) -> None:
    """Atomically replace one bounded profiling snapshot outside the source tree."""
    snapshot = dict(evidence)
    snapshot.update(
        latest_completed_phase=phase,
        diagnostic_only=True,
        performance_acceptance_eligible=False,
    )
    content = json.dumps(snapshot, indent=2, sort_keys=True, allow_nan=False) + "\n"
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(destination)


def seed_native_item_identity(item: Any, pcbnew: Any, identity: str) -> None:
    """Stabilize disposable fixture identities, including generated child graphics."""
    item.SetUuid(
        pcbnew.KIID(
            str(uuid5(NAMESPACE_URL, "kicad-jlcpcb-tools/native-spacing/" + identity))
        )
    )
    children = getattr(item, "GraphicalItems", None)
    if callable(children):
        for index, child in enumerate(children()):
            seed_native_item_identity(child, pcbnew, f"{identity}/graphic/{index}")
    for name in ("Reference", "Value"):
        child = getattr(item, name, None)
        if callable(child):
            seed_native_item_identity(child(), pcbnew, identity + "/" + name)


def seed_spacing_acceptance_fixture(case: Any, board: Any, pcbnew: Any) -> Any:
    """Add explicit routing evidence only to the worker-owned disposable PCB.

    Existing USB examples are not required to produce warnings from connector
    fanout. These independent native pairs establish quiet terminal transitions
    and an actionable wrong-gap trunk before the source-integrity baseline.
    """
    from impedance.model import LayerSettings, Specification
    from scripts.linux_native_impedance_smoke import require
    from tests.impedance_performance_fixtures import populate_native_terminal_pair

    settings = board.GetDesignSettings().m_NetSettings
    settings.SetNetclass(SPACING_FIXTURE_CLASS, pcbnew.NETCLASS(SPACING_FIXTURE_CLASS))
    settings.SetNetclassPatternAssignment("SPACING_ACCEPTANCE_*", SPACING_FIXTURE_CLASS)
    circuits = []
    for index, names in enumerate((SPACING_QUIET_NETS, SPACING_DEFECT_NETS)):
        nets = tuple(pcbnew.NETINFO_ITEM(board, name) for name in names)
        for net in nets:
            board.Add(net)
        terminal_pair = populate_native_terminal_pair(
            board,
            pcbnew,
            nets,
            origin_nm=(30_000_000, 30_000_000 + index * 5_000_000),
            gap_nm=150_000 if index == 0 else 300_000,
        )
        for group in ("tracks", "pads", "footprints"):
            for item_index, item in enumerate(getattr(terminal_pair, group)):
                seed_native_item_identity(item, pcbnew, f"{index}/{group}/{item_index}")
        circuits.append(
            SimpleNamespace(net_class=SPACING_FIXTURE_CLASS, net_names=names)
        )
    specification = Specification(
        "native-spacing-acceptance",
        "Deterministic terminal fanouts and routed spacing defect",
        "90",
        "differential",
        SPACING_FIXTURE_CLASS,
        (LayerSettings("F.Cu", ("In1.Cu",), 150_000),),
    )
    specifications = case.specifications() + (specification,)
    require(
        pcbnew.SaveBoard(board.GetFileName(), board),
        "Could not save disposable spacing acceptance fixture",
    )
    return SimpleNamespace(
        paired=case.paired,
        circuits=case.circuits + tuple(circuits),
        net_names=case.net_names + SPACING_QUIET_NETS + SPACING_DEFECT_NETS,
        specifications=lambda: specifications,
    )


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


def wait_for_saved_capture_restore(
    app: Any, dialog: Any, timeout: float = 300.0
) -> None:
    """Bound bulk cold restoration separately from ordinary interaction waits."""
    last_progress: Optional[tuple[int, int, bool]] = None

    def restored() -> bool:
        nonlocal last_progress
        queued = len(dialog._capture_prepare_sections)
        verified = sum(
            bool(digest) for digest in dialog.session.spacing_capture_digests.values()
        )
        pending = bool(dialog._capture_restore_pending)
        if (
            last_progress is None
            or pending != last_progress[2]
            or abs(queued - last_progress[0]) >= 25
            or abs(verified - last_progress[1]) >= 25
        ):
            logging.getLogger(__name__).info(
                "Native workflow: saved capture restoration queued=%d "
                "verified_spacing=%d required_spacing=%d pending=%s",
                queued,
                verified,
                len(dialog.session.spacing_entries),
                pending,
            )
            last_progress = queued, verified, pending
        return not pending

    wait_until(
        app,
        restored,
        "Reopening did not verify saved spacing PNG evidence",
        timeout=timeout,
    )


def select_spacing_preview(pane: Any, entry: Any) -> None:
    """Avoid requeueing an identical spacing selection; callers still await paint."""
    index = pane.entries.index(entry)
    if pane._index != index or pane.selected_entry != entry:
        pane.select(index)


@contextmanager
def interact_modal(
    wx: Any,
    dialog_type: type,
    action: Callable[[Any], None],
    *,
    before_show: Callable[[Any], None] = lambda _dialog: None,
) -> Iterator[list[Any]]:
    """Drive a mapped native modal without imposing an arbitrary startup delay."""
    native_show = dialog_type.ShowModal
    visited = []
    driven = []
    failures = []

    def show(dialog: Any) -> int:
        visited.append(dialog)
        before_show(dialog)
        active = True
        timer = None
        deadline = time.monotonic() + 30.0

        def drive() -> None:
            nonlocal timer
            if not active:
                return
            finished = False
            try:
                if not (dialog.IsModal() and dialog.IsShownOnScreen()):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Native modal did not become visible")
                    timer = wx.CallLater(1, drive)
                    return
                finished = True
                driven.append(dialog)
                action(dialog)
            except BaseException as error:
                finished = True
                failures.append(error)
            finally:
                if finished and dialog and dialog.IsModal():
                    dialog.EndModal(wx.ID_CANCEL)

        wx.CallAfter(drive)
        try:
            return native_show(dialog)
        finally:
            active = False
            if timer is not None:
                timer.Stop()

    with patch.object(dialog_type, "ShowModal", show):
        yield visited
    if failures:
        raise failures[0]
    if len(visited) != 1:
        raise RuntimeError("Expected exactly one native " + dialog_type.__name__)
    if driven != visited:
        raise RuntimeError("Native modal exited before mapped interaction")


def check_spacing_guide_pixels(entry: Any, capture: Any, wx: Any) -> dict[str, Any]:
    """Require the reviewed PNG itself to contain target guides and gap ticks.

    This native harness check reads the immutable capture, rather than accepting
    a transient canvas overlay or SVG attributes as evidence of exported pixels.
    Detailed physical guide placement is checked by the native fixture suite.
    """
    from scripts.linux_native_impedance_smoke import require

    package = type(entry).__module__.rsplit(".", 1)[0]
    annotations = importlib.import_module(package + ".spacing_overlays")
    rendering = importlib.import_module(package + ".render")
    paths = annotations.spacing_guide_paths(entry)
    kinds = Counter(path.kind for path in paths)
    require(
        kinds["target-edge"] + kinds["target-gap"] > 0 and kinds["measured-gap"] > 0,
        "Spacing image has no physical target guide or measured-gap witness",
    )
    image = wx.Image(BytesIO(capture.data), wx.BITMAP_TYPE_PNG)
    require(
        image.IsOk()
        and (image.GetWidth(), image.GetHeight()) == (entry.width_px, entry.height_px),
        "Spacing guide PNG has invalid native dimensions",
    )
    rgb = bytes(image.GetData())
    require(
        len(rgb) == entry.width_px * entry.height_px * 3,
        "Spacing guide PNG has incomplete native RGB pixels",
    )
    composition = getattr(entry, "composition", None)
    left, top, width, height = (
        composition.detail_rect
        if composition is not None
        else (0, 0, entry.width_px, entry.height_px)
    )
    require(
        all(isinstance(value, int) for value in (left, top, width, height))
        and left >= 0
        and top >= 0
        and width > 0
        and height > 0
        and left + width <= entry.width_px
        and top + height <= entry.height_px,
        "Spacing physical detail has invalid native pixel bounds",
    )
    # Summary labels and the legend may deliberately reuse the guide colors.
    # Only pixels inside the native physical detail can establish their presence
    # on the reviewed copper image.
    rgb = b"".join(
        rgb[
            (row * entry.width_px + left) * 3 : (row * entry.width_px + left + width)
            * 3
        ]
        for row in range(top, top + height)
    )

    def contains_color(value: str, tolerance: int) -> bool:
        """Allow native antialiasing, preserving RGB channel alignment."""
        color = tuple(int(value[index : index + 2], 16) for index in (1, 3, 5))
        pattern = b"".join(
            b"["
            + re.escape(
                bytes(
                    range(
                        max(0, channel - tolerance), min(256, channel + tolerance + 1)
                    )
                )
            )
            + b"]"
            for channel in color
        )
        return any(
            match.start() % 3 == 0
            for match in re.finditer(b"(?=" + pattern + b")", rgb)
        )

    require(
        # Allow native antialiasing at the dashed target-guide edges.
        contains_color(rendering.SPACING_GUIDE_COLOR, 60),
        "Spacing PNG is missing the actual target-guide pixels",
    )
    require(
        # Allow native antialiasing at the measured-ruler edges.
        contains_color(rendering.SPACING_TICK_COLOR, 100),
        "Spacing PNG is missing the actual measured-gap tick pixels",
    )
    return {"guide_paths": dict(kinds), "guide_png_sha256": capture.sha256}


def approve_spacing_review(
    app: Any,
    wx: Any,
    dialog: Any,
    button: Any,
    entry: Any,
    *,
    cancel: bool = False,
    inspect: Callable[[Any], None] = lambda _viewer: None,
    capture_path: Optional[Path] = None,
    verify_guides: bool = False,
) -> dict[str, Any]:
    """Approve only the current full-source native Fit paint, without navigation."""
    from scripts.linux_native_impedance_smoke import (
        capture_window,
        event,
        require,
        stock_button,
    )

    preview_module = importlib.import_module(type(dialog.preview_pane).__module__)
    evidence: dict[str, Any] = {}

    def unpainted(viewer: Any) -> None:
        """Reject approval enabled merely by opening an unseen image window."""
        require(
            not viewer.approved
            and not viewer.readable
            and not viewer.approve_button.IsEnabled(),
            "An unpainted spacing window can already approve an image",
        )

    def review(viewer: Any) -> None:
        """Inspect one complete, readable Fit paint before explicit consent."""
        composition = getattr(entry, "composition", None)
        canvas = viewer.canvas
        wait_until(
            app,
            lambda: canvas._recorded
            and viewer.readable
            and viewer.approve_button.IsEnabled(),
            "The enlarged spacing image did not finish a readable Fit paint",
        )
        require(
            viewer.caption.IsShownOnScreen()
            and viewer.caption.GetLabel().replace("\n", " ") == entry.caption,
            "The readable spacing image lost its external caption",
        )
        require(
            canvas._presentation_crop is None,
            "The spacing approval image is cropped",
        )
        source = viewer._capture
        source_dimensions = (source.width, source.height)
        full_source = (0, 0, source.width, source.height)
        client = canvas.GetClientSize()
        client_dimensions = (client.width, client.height)
        require(
            source_dimensions == (entry.width_px, entry.height_px)
            and canvas._scaled_view == full_source
            and canvas._visible_source_rect == full_source
            and canvas._painted_size == client_dimensions
            and canvas.IsShownOnScreen(),
            "The spacing approval image lacks a current full-source Fit paint",
        )
        cancel_button = stock_button(viewer, wx.ID_CANCEL)
        require(
            viewer.approve_button.IsShownOnScreen() and cancel_button.IsShownOnScreen(),
            "The spacing approval controls are not visible",
        )
        descendants = list(viewer.GetChildren())
        while descendants:
            control = descendants.pop()
            label = getattr(control, "GetLabel", lambda: "")()
            require(
                label.replace("&", "").strip()
                not in {"Fit image", "Original image", "Fit issue", "Fit route"},
                "Obsolete spacing image controls remain in the review window",
            )
            descendants.extend(control.GetChildren())
        width, height = canvas._dimensions
        minimum_scale = composition.minimum_scale if composition is not None else 2 / 3
        require(
            canvas._painted_scale + 1e-9 >= minimum_scale
            and abs(
                canvas._painted_scale
                - min(width / source.width, height / source.height)
            )
            <= 1e-9,
            "The spacing approval image is below its readable display scale",
        )
        if verify_guides:
            evidence.update(check_spacing_guide_pixels(entry, viewer._capture, wx))
        inspect(viewer)
        if not cancel:
            wait_until(
                app,
                lambda: viewer.readable and viewer.approve_button.IsEnabled(),
                "The enlarged spacing image lost its readable paint before approval",
            )
        # Native inspectors may resize the real viewer before taking screenshots.
        # Read every geometry field from that same final paint, including cancel
        # paths which deliberately invalidate the previously readable source.
        width, height = canvas._dimensions
        client = canvas.GetClientSize()
        client_dimensions = (client.width, client.height)
        evidence.update(
            view_id=entry.view_id,
            source_dimensions=source_dimensions,
            display_dimensions=(width, height),
            fit_dimensions=(width, height),
            fit_scale=canvas._painted_scale,
            canvas_client_dimensions=client_dimensions,
            painted_source_rect=canvas._visible_source_rect,
            minimum_review_scale=minimum_scale,
            paint_current=bool(canvas._recorded and viewer.readable),
            cancelled=cancel,
        )
        if capture_path is not None:
            capture_window(wx, viewer, capture_path)
            evidence["screen_capture"] = capture_path.name
        event(
            wx,
            stock_button(viewer, wx.ID_CANCEL) if cancel else viewer.approve_button,
            wx.EVT_BUTTON,
        )
        require(
            viewer.approved is (not cancel),
            "The spacing window returned an incorrect approval state: "
            + viewer.status.GetLabel(),
        )

    require(button.IsEnabled(), "The spacing review button is disabled")
    with interact_modal(
        wx, preview_module.EnlargedCaptureDialog, review, before_show=unpainted
    ):
        event(wx, button, wx.EVT_BUTTON)
    return evidence


def workbook_preview_indexes(preview: Any) -> dict[str, int]:
    """Locate workbook images by identity inside the spacing-first display list."""
    return {
        entry.section_id: index
        for index, entry in enumerate(preview.entries)
        if hasattr(entry, "section_id")
    }


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
                "render": "impedance.render",
                "integration": "impedance.integration",
                "library": "library",
                "store": "store",
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
    # This fixture starts with existing local storage, as an already informed
    # user would. Do not leave an unrelated constructor notice awaiting input.
    modules.store.Store(
        None, str(database.parent.parent)
    ).set_schematic_storage_notice_acked()
    return settings_dir


def check_nominal_width_rows(dialog: Any, expected_width_nm: int) -> None:
    """Check the actual displayed nominal values after native calculation refresh."""
    from impedance.matching import is_pad_via_stub
    from impedance.model import EXCLUSION_COVERED_STUB
    from scripts.linux_native_impedance_smoke import require

    package = type(dialog.session.config).__module__.rsplit(".", 1)[0]
    widths = importlib.import_module(package + ".width_checks")
    by_id = {row.section_id: row for row in dialog.session.analysis.sections}
    specs = {spec.spec_id: spec for spec in dialog.session.config.specifications}
    for row in range(dialog.sections.GetItemCount()):
        target_text = dialog.sections.GetTextValue(row, 9)
        comparison = dialog.sections.GetTextValue(row, 8)
        section = by_id[dialog._row_ids[row]]
        if section.exclusion_reason:
            require(
                section.exclusion_reason == EXCLUSION_COVERED_STUB
                and all(
                    is_pad_via_stub(trace, dialog.session.snapshot.copper_features)
                    for trace in section.traces
                )
                and target_text == ""
                and comparison == "",
                "Covered-stub nominal must stay blank and have same-net copper proof",
            )
            continue
        require(
            target_text not in {"", "Pending", "Not calculated"},
            "Target width did not show a calculated nominal after refresh",
        )
        check = widths.width_check(
            dialog.session.config,
            specs[section.spec_id],
            section.layer,
            section.width_nm,
        )
        require(
            check.result_current and check.target_width_nm == expected_width_nm,
            "Native calculated nominal is missing, incorrect, or stale",
        )
        require(
            target_text == widths.target_width_cell_text(check),
            "Native Target width does not match the calculated nominal display",
        )
        require(
            comparison
            == (
                "!="
                if widths.width_row_mismatch(section.width_nm, expected_width_nm)
                else ""
            ),
            "Workbook width comparison marker disagrees with its dimensions",
        )


def _spacing_evidence(checks: dict[str, Any]) -> dict[str, Any]:
    """Keep measured bounds and targets in JSON smoke evidence, not just a label."""
    records = []
    for section_id, check in checks.items():
        records.append(
            {
                "section_id": section_id,
                "declared_spacing_nm": check.declared_spacing_nm,
                "verdict": check.verdict,
                "coverage": check.coverage,
                "transition_length_nm": check.transition_length_nm,
                "located_issues": sum(
                    issue.location is not None for issue in check.issues
                ),
                "minimum_gap_nm": (
                    [check.minimum_gap_nm.lower_nm, check.minimum_gap_nm.upper_nm]
                    if check.minimum_gap_nm is not None
                    else None
                ),
                "maximum_gap_nm": (
                    [check.maximum_gap_nm.lower_nm, check.maximum_gap_nm.upper_nm]
                    if check.maximum_gap_nm is not None
                    else None
                ),
            }
        )
    return {
        "rows": records,
        "measured_rows": sum(
            record["minimum_gap_nm"] is not None for record in records
        ),
        "deviation_rows": sum(record["verdict"] == "deviation" for record in records),
    }


def check_spacing_rows(dialog: Any) -> dict[str, Any]:
    """Check numeric grid cells, statuses and exact independently measured state."""
    from scripts.linux_native_impedance_smoke import require

    # KiCad loads this plugin under its own package name. Use that same module
    # so immutable result equality does not compare two different dataclasses.
    package = type(dialog.session.config).__module__.rsplit(".", 1)[0]
    spacing = importlib.import_module(package + ".spacing_checks")
    context = getattr(dialog, "_native_spacing_oracle", None)
    if context is None or context.snapshot is not dialog.session.snapshot:
        context = spacing.SpacingContext(dialog.session.snapshot)
        dialog._native_spacing_oracle = context
    checks = spacing.spacing_checks_for_sections(
        dialog.session.config,
        dialog.session.snapshot,
        dialog.session.analysis.sections,
        context=context,
    )
    require(checks, "Native fixture has no differential spacing checks")
    columns = [
        column
        for column in range(dialog.sections.GetColumnCount())
        if dialog.sections.GetColLabelValue(column) == "Spacing check"
    ]
    require(len(columns) == 1, "Missing or duplicate Spacing check column")
    status_column = columns[0]
    require(
        status_column >= 3
        and tuple(
            dialog.sections.GetColLabelValue(column)
            for column in range(status_column - 3, status_column)
        )
        == ("Pair spacing", "", "Target spacing"),
        "Missing or incorrect spacing value/comparison columns",
    )
    require(
        dialog._spacing_by_section == checks,
        "Displayed spacing measurements are missing, wrong, or stale",
    )
    for row, section_id in enumerate(dialog._row_ids):
        expected = spacing.spacing_workbook_cells(checks.get(section_id))
        require(
            tuple(
                dialog.sections.GetTextValue(row, column)
                for column in range(status_column - 3, status_column + 1)
            )
            == expected,
            "Spacing row values or status disagree with measured dimensions or coverage",
        )
    check_selected_spacing_summary(dialog)
    evidence = _spacing_evidence(checks)
    require(evidence["measured_rows"], "Native fixture spacing is wholly unverified")
    return evidence


def check_selected_spacing_summary(dialog: Any) -> None:
    """Read the selected native text control after the real grid selection event."""
    from impedance.spacing_checks import spacing_check_summary
    from scripts.linux_native_impedance_smoke import require

    selected = dialog.sections.GetSelectedRow()
    if not 0 <= selected < len(dialog._row_ids):
        return
    check = dialog._spacing_by_section.get(dialog._row_ids[selected])
    expected = spacing_check_summary(check) if check is not None else ""
    require(
        dialog.spacing_summary.GetValue() == expected,
        "Selected spacing summary has missing, incorrect, or stale measured values",
    )


def check_spacing_preview(dialog: Any) -> dict[str, Any]:
    """Read distinct native close-ups without painting annotations over copper."""
    from scripts.linux_native_impedance_smoke import require

    pane = dialog.preview_pane
    entry = pane.selected_entry
    require(entry is not None, "No selected spacing preview entry")
    capture = pane._capture
    require(capture is not None, "Spacing preview has no captured PCB image")
    require(
        pane.canvas._bitmap is not None and pane.canvas._bitmap.IsOk(),
        "Spacing preview has no displayed native PCB bitmap",
    )
    if hasattr(entry, "section_id"):
        require(
            pane.canvas._presentation_crop is None
            and pane.canvas._scaled_view == (0, 0, capture.width, capture.height)
            and pane.canvas._visible_source_rect
            == (0, 0, capture.width, capture.height),
            "Thumbnail no longer shows the complete route",
        )
        return {"section_id": entry.section_id, "diagnostic": False}
    require(entry in dialog.session.spacing_entries, "Stale spacing review entry")
    composition = getattr(entry, "composition", None)
    expected_caption = entry.caption
    if composition is not None:
        actual, target = composition.actual_gap_nm, entry.target_spacing_nm
        if target is not None:
            target_mm = format(Decimal(target) / Decimal(1_000_000), "f")
            if "." in target_mm:
                target_mm = target_mm.rstrip("0").rstrip(".")
            if actual is not None:
                actual_mm = f"{actual / 1_000_000:.6f}".rstrip("0").rstrip(".")
                summary = f"Shown gap {actual_mm} mm"
            else:
                region = composition.region_gap_nm
                if region is None:
                    summary = "Gap details in spacing review"
                else:
                    lower = f"{region.lower_nm / 1_000_000:.6f}".rstrip("0").rstrip(".")
                    upper = f"{region.upper_nm / 1_000_000:.6f}".rstrip("0").rstrip(".")
                    extent = lower if lower == upper else f"{lower}–{upper}"
                    summary = f"Region gap {extent} mm"
            expected_caption = f"{summary} · target {target_mm} mm"
        require(
            pane.canvas._presentation_crop == composition.detail_rect
            and pane.canvas._scaled_view == composition.detail_rect
            and pane.canvas._visible_source_rect == composition.detail_rect,
            "Spacing thumbnail does not show the physical detail panel",
        )
    require(
        pane.caption.IsShown()
        and pane.caption.GetLabel().replace("\n", " ") == expected_caption,
        "Spacing close-up has no external or current caption",
    )
    require(
        capture.viewport_nm == (None if composition is not None else entry.viewport_nm)
        and (capture.width, capture.height) == (entry.width_px, entry.height_px),
        "Spacing close-up used the workbook frame or insufficient native resolution",
    )
    if composition is not None and capture.original is not None:
        require(
            capture.original.viewport_nm == entry.viewport_nm
            and (capture.original.width, capture.original.height)
            == composition.detail_rect[2:],
            "Spacing original comparison has a different physical detail transform",
        )
    return {"view_id": entry.view_id, "diagnostic": True}


def check_seeded_spacing_rows(dialog: Any) -> dict[str, int]:
    """Distinguish intentional terminal fanouts from a known wrong-gap trunk."""
    from scripts.linux_native_impedance_smoke import require

    quiet = []
    defect = []
    for section in dialog.session.analysis.sections:
        check = dialog._spacing_by_section.get(section.section_id)
        if check is None:
            continue
        if set(section.net_names) == set(SPACING_QUIET_NETS):
            quiet.append(check)
        elif set(section.net_names) == set(SPACING_DEFECT_NETS):
            defect.append(check)
    require(quiet and defect, "Explicit native spacing fixtures are missing")
    require(
        all(
            check.verdict in {"match", "not_applicable"}
            and not check.issues
            and check.transition_length_nm > 0
            for check in quiet
        ),
        "Terminal fanouts created a false routed spacing warning",
    )
    require(
        all(
            check.verdict == "deviation"
            and check.transition_length_nm > 0
            and any(issue.location is not None for issue in check.issues)
            for check in defect
        ),
        "Terminal exclusion concealed the explicit wrong-gap routed trunk",
    )
    return {"quiet_rows": len(quiet), "defect_rows": len(defect)}


class _SpacingReportFields(HTMLParser):
    """Read rendered definition-list values separately for each exported row."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, str]] = []
        self.spacing_cards: list[dict[str, Any]] = []
        self.workbook_images: list[bytes] = []
        self.article_kinds: list[str] = []
        self._spacing: Any = None
        self._row: Any = None
        self._field = ""
        self._tag = ""
        self._text = ""
        self._spacing_original = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Any]]) -> None:
        if tag == "article":
            self._row = {}
            self._spacing_original = False
            attributes = dict(attrs)
            if attributes.get("class") == "spacing-review":
                self.article_kinds.append("spacing")
                self._spacing = {"fields": self._row}
                self.spacing_cards.append(self._spacing)
            else:
                self.article_kinds.append("workbook")
                self.rows.append(self._row)
        elif self._spacing is not None and tag == "details":
            self._spacing_original = dict(attrs).get("class") == "spacing-original"
        elif self._row is not None and tag == "img":
            attributes = dict(attrs)
            source = attributes.get("src", "")
            data = b64decode(
                source.removeprefix("data:image/png;base64,"), validate=True
            )
            if self._spacing is None:
                self.workbook_images.append(data)
            else:
                image = {
                    "data": data,
                    "width": int(attributes.get("width", 0)),
                    "height": int(attributes.get("height", 0)),
                }
                if self._spacing_original:
                    self._spacing["original"] = image
                elif "data" in self._spacing:
                    raise RuntimeError(
                        "Spacing report repeats its primary review image"
                    )
                else:
                    self._spacing.update(image)
        elif self._row is not None and tag in {"dt", "dd"}:
            self._tag, self._text = tag, ""
        elif self._tag == "dd" and tag == "br":
            self._text += "\n"

    def handle_data(self, data: str) -> None:
        if self._row is not None and self._tag:
            self._text += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "article":
            self._row = None
            self._spacing = None
            self._spacing_original = False
        elif tag == "details":
            self._spacing_original = False
        elif self._row is not None and tag == self._tag:
            if tag == "dt":
                self._field = self._text
            else:
                self._row[self._field] = self._text
            self._tag, self._text = "", ""


def check_spacing_report(
    markup: str,
    config: Any,
    snapshot: Any,
    sections: tuple[Any, ...],
    *,
    require_spacing_reviews: bool = False,
    workbook_bytes: bytes = b"",
) -> dict[str, Any]:
    """Reject missing/stale HTML targets, measurements, status, or coverage."""
    from scripts.linux_native_impedance_smoke import require

    # The real plugin owns a separate model namespace from standalone fixtures.
    # Preserve its strict snapshot/record types when deriving report evidence.
    package = type(config).__module__.rsplit(".", 1)[0]
    model = importlib.import_module(package + ".model")
    tracking = importlib.import_module(package + ".review_tracking")
    spacing = importlib.import_module(package + ".spacing_checks")
    reviews = importlib.import_module(package + ".spacing_review")
    service = importlib.import_module(package + ".service")
    widths = importlib.import_module(package + ".width_checks")
    resolved_layer_settings = model.resolved_layer_settings
    find_spacing_image = tracking.find_spacing_image
    spacing_capture_fingerprint = tracking.spacing_capture_fingerprint
    spacing_image_current = tracking.spacing_image_current
    spacing_check_details = spacing.spacing_check_details
    spacing_check_label = spacing.spacing_check_label
    spacing_checks_for_sections = spacing.spacing_checks_for_sections
    spacing_review_entries = reviews.spacing_review_entries
    format_width_nm = widths.format_width_nm

    # This independent oracle owns a fresh context, never the product's cached
    # verdicts. Its row and close-up assertions share identical immutable input.
    context = spacing.SpacingContext(snapshot)
    checks = spacing_checks_for_sections(config, snapshot, sections, context=context)
    require(checks, "Native report has no differential spacing checks")
    parser = _SpacingReportFields()
    parser.feed(markup)
    require(len(parser.rows) == len(sections), "Spacing report row count is wrong")
    specifications = {spec.spec_id: spec for spec in config.specifications}
    for section, fields in zip(sections, parser.rows):
        check = checks.get(section.section_id)
        if check is None:
            continue
        target = resolved_layer_settings(specifications[section.spec_id], section.layer)
        require(
            fields.get("Declared pair spacing") == format_width_nm(target.spacing_nm),
            "Report declared pair spacing disagrees with saved manufacturing intent",
        )
        require(
            fields.get("Pair spacing") == spacing_check_label(check),
            "Report spacing verdict is missing or disagrees with measured coverage",
        )
        require(
            fields.get("Spacing assessment") == spacing_check_details(check),
            "Report spacing measurements are missing, incorrect, or stale",
        )
    if require_spacing_reviews:
        entries = spacing_review_entries(config, snapshot, sections, context=context)
        require(entries, "Native report has no separate spacing close-ups")
        require(
            len(parser.spacing_cards) == len(entries),
            "Report separate spacing close-ups are missing or duplicated",
        )
        require(
            parser.article_kinds
            == ["spacing"] * len(entries) + ["workbook"] * len(sections),
            "Report spacing images must precede workbook trace images",
        )
        require(
            'class="spacing-issue"' not in markup,
            "Report retained copper-obscuring badges",
        )
        for entry, card in zip(entries, parser.spacing_cards):
            fields = card["fields"]
            require(
                fields.get("Spacing concern") == entry.caption
                and fields.get("Nets") == ", ".join(entry.pair)
                and fields.get("Signal copper layer") == entry.layer
                and (card.get("width"), card.get("height"))
                == (entry.width_px, entry.height_px),
                "Report separate spacing close-up is stale or unreadable",
            )
            original = card.get("original")
            original_dimensions = (
                entry.composition.detail_rect[2:]
                if entry.composition is not None
                else (entry.width_px, entry.height_px)
            )
            require(
                original is not None
                and (original.get("width"), original.get("height"))
                == original_dimensions
                and hashlib.sha256(original["data"]).digest()
                != hashlib.sha256(card["data"]).digest(),
                "Report spacing Original is missing, incorrectly sized, or duplicates its annotated PNG",
            )
            original_png = service.CapturedImage.from_bytes(original["data"])
            require(
                (original_png.width, original_png.height) == original_dimensions,
                "Report spacing Original pixels disagree with the physical detail dimensions",
            )
            record = find_spacing_image(
                config.review_tracking, entry.spec_id, entry.layer, entry.view_id
            )
            require(
                spacing_image_current(
                    record,
                    entry.input_digest,
                    spacing_capture_fingerprint(
                        entry.input_digest, hashlib.sha256(card["data"]).hexdigest()
                    ),
                    approved=True,
                )
                and fields.get("Image approval", "").startswith("Approved ·"),
                "Report spacing close-up lacks individual approval of its exact PNG",
            )
    if workbook_bytes:
        with ZipFile(BytesIO(workbook_bytes)) as workbook:
            images = [
                workbook.read(name)
                for name in workbook.namelist()
                if name.startswith("xl/media/")
            ]
            require(
                Counter(images) == Counter(parser.workbook_images),
                "Excel embedded images differ from the original manufacturing PNGs",
            )
            xml = b"\n".join(
                workbook.read(name)
                for name in workbook.namelist()
                if name.endswith(".xml")
            )
            require(
                b"Spacing concern" not in xml and b"Spacing image" not in xml,
                "Diagnostic spacing rows leaked into Excel",
            )
    return _spacing_evidence(checks)


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

    started = time.perf_counter()
    phases: dict[str, Any] = {}
    counts: Counter[str] = Counter()
    renderer_roles: Counter[str] = Counter()
    renderer_inputs: dict[str, Counter[str]] = {}
    render_receipts: dict[str, Any] = {}
    diagnostic_profile = os.environ.get("KICAD_IMPEDANCE_PROFILE_GENERATE") == "1"
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
    configured_spacing: list[dict[str, Any]] = []
    spacing_previews: list[dict[str, Any]] = []

    def capture_state(frame: Any) -> dict[str, int]:
        cache = getattr(frame._impedance, "_owned_capture_cache", None)
        return {
            "entries": len(getattr(cache, "_images", ())),
            "hits": getattr(cache, "hits", 0),
            "misses": getattr(cache, "misses", 0),
        }

    def render_activity(
        before_roles: Counter[str],
        before_cache: dict[str, int],
        frame: Any,
        before_inputs: dict[str, Counter[str]],
    ) -> dict[str, Any]:
        after = capture_state(frame)
        return {
            "rendered": {
                role: renderer_roles[role] - before_roles[role]
                for role in ("trace", "spacing", "original")
            },
            "rendered_inputs": {
                role: dict(
                    renderer_inputs.get(role, Counter())
                    - before_inputs.get(role, Counter())
                )
                for role in ("trace", "spacing", "original")
            },
            "cache_hits": after["hits"] - before_cache["hits"],
            "cache_misses": after["misses"] - before_cache["misses"],
            "cache_entries_before": before_cache["entries"],
            "cache_entries_after": after["entries"],
        }

    def performance_evidence() -> dict[str, Any]:
        return {
            "seconds": time.perf_counter() - started,
            "phases": phases,
            "counts": dict(counts),
            "renderer_roles": dict(renderer_roles),
            "renderer_inputs": {
                role: dict(identities) for role, identities in renderer_inputs.items()
            },
            "render_receipts": render_receipts,
            "diagnostic_only": diagnostic_profile,
            "performance_acceptance_eligible": not diagnostic_profile,
            "phase_accounting": "nested phases overlap; worker total includes initialization and cleanup",
        }

    def heartbeat(phase: str) -> None:
        write_diagnostic_heartbeat(
            output / "generate-heartbeat.json", performance_evidence(), phase
        )

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
        with (
            measure_phase(phases, "configure"),
            interact_modal(wx, modules.dialog.ImpedanceDialog, action),
        ):
            event(wx, frame._impedance.configure_button, wx.EVT_BUTTON)
        require(not messages, "Configure unexpectedly failed: " + str(messages))
        if diagnostic_profile:
            heartbeat("Configure " + getattr(action, "__name__", "completed"))

    def edit_target_and_save(dialog: Any, target: str) -> None:
        """Apply a native child draft without requiring capture approval first."""

        def apply(editor: Any) -> None:
            editor.target.SetValue(target)
            event(wx, stock_button(editor, wx.ID_OK), wx.EVT_BUTTON)
            require(
                editor.specification is not None
                and editor.specification.target_ohms == target
                and not editor.IsModal(),
                "Apply settings required review or rejected an unfinished draft: "
                + editor.validation_message.GetLabel(),
            )

        dialog.specifications.SelectRow(0)
        with interact_modal(wx, modules.dialog.SpecificationDialog, apply):
            event(wx, dialog._selection_buttons[0], wx.EVT_BUTTON)
        require(
            dialog.session.config.specifications[0].target_ohms == target,
            "Applying child settings did not update the parent draft",
        )
        require(not dialog.session.approved, "Applying settings created approval")
        event(wx, stock_button(dialog, wx.ID_OK), wx.EVT_BUTTON)

    def check_row_numbers(dialog: Any) -> None:
        """Read every displayed reference, including candidates omitted from export."""
        require(
            dialog.sections.GetColLabelValue(1) == "Row",
            "Workbook table lost its Row reference column",
        )
        count = dialog.sections.GetItemCount()
        row_numbers = {
            section.section_id: str(index + 1)
            for index, section in enumerate(dialog.session.analysis.sections)
        }
        require(
            [dialog.sections.GetTextValue(index, 1) for index in range(count)]
            == [row_numbers[section_id] for section_id in dialog._row_ids],
            "Workbook Row references lost their canonical section numbers",
        )
        require(
            count == len(dialog._row_ids),
            "Workbook references no longer match displayed section identities",
        )
        checked = [dialog.sections.GetToggleValue(index, 0) for index in range(count)]
        require(
            checked == sorted(checked, reverse=True),
            "Workbook checked rows are not grouped before unchecked rows",
        )

    def approve(dialog: Any) -> None:
        with measure_phase(phases, "approve_all_images"):
            approve_all_images(dialog)
        if diagnostic_profile:
            heartbeat("approval pass " + str(counts["approval_passes"]))

    def approve_all_images(dialog: Any) -> None:
        import wx.grid as gridlib

        counts["approval_passes"] += 1
        require(dialog.session.analysis is not None, "No workbook rows to approve")
        check_row_numbers(dialog)
        entries = dialog.session.spacing_entries
        require(entries, "Explicit routed spacing defect has no separate review image")
        if dialog.session.pending_spacing_reviews:
            require(
                not dialog.session.approved,
                "Workbook implicitly approved pending spacing images",
            )
        for entry in entries:
            if diagnostic_profile and not counts["spacing_explicit_approvals"]:
                with profile_native_operation(
                    output / "first-spacing-iteration.pstats"
                ):
                    review_spacing_entry(dialog, entry)
            else:
                review_spacing_entry(dialog, entry)
        require(
            not dialog.session.pending_spacing_reviews,
            "Spacing image approval skipped an entry",
        )
        preview_indexes = workbook_preview_indexes(dialog.preview_pane)
        for index, section_id in enumerate(dialog._row_ids):
            event(wx, dialog.sections, gridlib.EVT_GRID_SELECT_CELL, index)
            wait_until(
                app,
                lambda section_id=section_id: dialog.preview_pane._index
                == preview_indexes[section_id],
                "Workbook selection did not reach the preview",
            )
            with measure_phase(phases, "preview.await_current_paint"):
                wait_for_preview(app, dialog.preview_pane)
            check_selected_spacing_summary(dialog)
            spacing_previews.append(check_spacing_preview(dialog))
            counts["workbook_preview_visits"] += 1
        event(wx, dialog.approve_button, wx.EVT_BUTTON)
        require(
            dialog.session.approved,
            "Review approval failed: " + dialog.status.GetValue(),
        )
        check_row_numbers(dialog)

    def review_spacing_entry(dialog: Any, entry: Any) -> None:
        select_spacing_preview(dialog.preview_pane, entry)
        with measure_phase(phases, "preview.await_current_paint"):
            wait_for_preview(app, dialog.preview_pane)
        spacing_previews.append(check_spacing_preview(dialog))
        app.Yield()
        require(
            dialog.spacing_approve_button.IsEnabled(),
            "Displayed spacing close-up cannot be approved",
        )
        spacing_previews.append(
            approve_spacing_review(
                app,
                wx,
                dialog,
                dialog.spacing_approve_button,
                entry,
                verify_guides=True,
                capture_path=(
                    output / "configure-spacing-readable.png"
                    if not (output / "configure-spacing-readable.png").exists()
                    else None
                ),
            )
        )
        require(
            dialog.session.spacing_review_current(entry),
            "Individual spacing image approval was not recorded",
        )
        counts["spacing_explicit_approvals"] += 1

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
            dialog.sections.GetItemCount() > 0,
            "Workbook rows are empty after width calculation",
        )
        check_row_numbers(dialog)
        check_nominal_width_rows(dialog, 180000)
        spacing = check_spacing_rows(dialog)
        check_seeded_spacing_rows(dialog)
        configured_spacing.append(spacing)
        wait_for_preview(app, dialog.preview_pane)
        require(
            dialog.session.spacing_entries
            and dialog.preview_pane.selected_entry == dialog.session.spacing_entries[0]
            and dialog.preview_pane._index == 0,
            "The first native preview did not paint a spacing image",
        )
        approve(dialog)

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
            dialog._schedule_width_refresh(force=True)
            wait_calculation(dialog)
        require(
            dialog.session.config.width_results == saved_results
            and dialog.session.approved
            and "rejection" in dialog.status.GetValue(),
            "Failed calculation replaced prior approved results",
        )

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
            check_row_numbers(dialog)
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
        check_row_numbers(dialog)
        event(wx, stock_button(dialog, wx.ID_OK), wx.EVT_BUTTON)

    def generate(frame: Any) -> None:
        logging.getLogger(__name__).info("Native workflow: Generate")
        with measure_phase(phases, "generate_toolbar"):
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
        counts["generate_toolbar_events"] += 1
        if diagnostic_profile:
            heartbeat("Generate toolbar " + str(counts["generate_toolbar_events"]))

    # Any accidental network path is an acceptance failure; only explicit
    # deterministic catalog/calculator responses above cross the supplier seam.
    import requests

    with (
        record_workflow_progress(
            output / "generate-performance.json", performance_evidence
        ),
        patch.object(modules.main, "PLUGIN_PATH", settings_dir),
        patch.object(modules.catalog, "fetch_stackups", fetch_catalog),
        patch.object(modules.calculator.JlcpcbCalculator, "calculate_width", calculate),
        patch.object(
            requests.sessions.Session,
            "request",
            side_effect=AssertionError("Native workflow must not contact suppliers"),
        ),
        patch.object(wx, "MessageBox", message),
        record_impedance_timings(phases, modules.adapter, modules.dialog),
        record_call_timings(
            phases,
            tuple(
                (sys.modules[__name__], name, "acceptance." + name)
                for name in (
                    "approve_spacing_review",
                    "check_spacing_guide_pixels",
                    "check_spacing_preview",
                    "check_spacing_rows",
                    "check_selected_spacing_summary",
                    "check_spacing_report",
                    "check_generated_archive",
                )
            ),
        ),
        record_renderer_timings(
            modules.render.SectionRenderer,
            phases,
            roles=renderer_roles,
            rendered_inputs=renderer_inputs,
        ),
        profile_first_call(
            modules.integration.CaptureSession,
            "_render_spacing",
            output / "first-cold-spacing-capture.pstats",
            enabled=diagnostic_profile,
        ),
    ):
        try:
            if diagnostic_profile:
                heartbeat("workflow setup")
            frame = new_frame()
            config = modules.model.Config.from_dict(plan.config.to_dict())
            frame._impedance._save(
                replace(config, reviewed_digest="", included_section_ids=())
            )
            frame._impedance._update_saved_status()
            logging.getLogger(__name__).info(
                "Native workflow: save incomplete target, reopen and resume editing"
            )
            configure(frame, lambda dialog: edit_target_and_save(dialog, ""))
            unfinished = frame._impedance.repository.load()[0]
            require(
                unfinished.specifications[0].target_ohms == ""
                and not unfinished.reviewed_digest,
                "Unfinished draft did not persist without approval",
            )
            close_frame(frame)
            frame = new_frame()
            require(
                frame._impedance.config == unfinished,
                "Reopening lost the unfinished draft",
            )
            configure(
                frame,
                lambda dialog: edit_target_and_save(
                    dialog, config.specifications[0].target_ohms
                ),
            )
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
            cold_before_roles = renderer_roles.copy()
            cold_before_inputs = {
                role: value.copy() for role, value in renderer_inputs.items()
            }
            cold_before_cache = capture_state(frame)
            require(
                cold_before_cache["entries"] == 0,
                "Cold saved reopening inherited a prior frame's PNG cache",
            )

            def cancel_reopened(dialog: Any) -> None:
                require(
                    dialog.session.config == saved_config,
                    "Reopened dialog lost persisted settings",
                )
                check_row_numbers(dialog)
                require(
                    check_spacing_rows(dialog) == configured_spacing[0],
                    "Reopening changed spacing targets, measurements, or coverage",
                )
                with measure_phase(phases, "saved_capture_restore"):
                    wait_for_saved_capture_restore(app, dialog)
                cold = render_activity(
                    cold_before_roles, cold_before_cache, frame, cold_before_inputs
                )
                render_receipts["cold_saved_reopen"] = cold
                require(
                    all(
                        cold["rendered_inputs"]["trace"].get(section.section_id) == 1
                        for section in plan.sections
                    )
                    and cold["rendered_inputs"]["spacing"]
                    == {entry.view_id: 1 for entry in dialog.session.spacing_entries}
                    and cold["rendered"]["original"] == 0,
                    "Cold saved reopening did not render every trace/spacing PNG or eagerly rendered Originals",
                )
                require(
                    dialog.session.approved
                    and not dialog.session.pending_spacing_reviews
                    and all(
                        dialog.session.spacing_review_current(entry)
                        for entry in dialog.session.spacing_entries
                    )
                    and dialog.session.config.review_tracking.spacing_images
                    == saved_config.review_tracking.spacing_images,
                    "Reopening changed individual spacing approvals or timestamps",
                )
                if diagnostic_profile:
                    heartbeat("cold saved capture restoration")
                select_stackup(dialog, alternative)
                check_row_numbers(dialog)
                require(
                    check_spacing_rows(dialog) == configured_spacing[0],
                    "Stackup selection changed independent spacing diagnostics",
                )
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
            enabled_before_roles = renderer_roles.copy()
            enabled_before_inputs = {
                role: value.copy() for role, value in renderer_inputs.items()
            }
            enabled_before_cache = capture_state(frame)
            generate(frame)
            require(not messages, "Native Generate failed: " + str(messages))
            first_export = render_activity(
                enabled_before_roles, enabled_before_cache, frame, enabled_before_inputs
            )
            render_receipts["first_enabled_export"] = first_export
            require(
                first_export["rendered"] == {"trace": 0, "spacing": 0, "original": 65}
                and first_export["rendered_inputs"]["original"]
                == render_receipts["cold_saved_reopen"]["rendered_inputs"]["spacing"]
                and first_export["cache_hits"] >= 97,
                "Enabled Generate did not reuse reviewed PNGs while rendering every deferred Original",
            )
            if diagnostic_profile:
                heartbeat("first enabled export")
            paths = frame.fabrication.get_artifact_paths()
            archive = Path(paths["gerber_zip"])
            enabled = check_generated_archive(archive, True)
            with ZipFile(archive) as generated:
                spacing_report = check_spacing_report(
                    generated.read(HTML).decode("utf-8"),
                    saved_config,
                    original_snapshot,
                    plan.sections,
                    require_spacing_reviews=True,
                    workbook_bytes=generated.read(WORKBOOK),
                )
            require(
                enabled["workbook_images"] == len(plan.sections),
                "Generated workbook lost native section captures",
            )
            if diagnostic_profile:
                heartbeat("enabled report validation")
            (output / "enabled.zip").write_bytes(archive.read_bytes())
            published_dir = Path(frame.fabrication.outputdir)
            prior_outputs = output_contents(published_dir)
            prior_count = frame.store.get_generation_count()

            def cancel_placeholder(dialog: Any) -> None:
                require(
                    any(
                        finding is not None
                        and finding.check == "Order number"
                        and "No order-number placeholder" in finding.title
                        for finding in dialog._rows
                    ),
                    "Generate review did not show the missing order-number placeholder",
                )
                event(wx, dialog.cancel_button, wx.EVT_BUTTON)
                wait_until(
                    app,
                    lambda: not dialog.IsModal(),
                    "Generate review Cancel did not close the native dialog",
                )
                require(
                    dialog.GetReturnCode() == wx.ID_CANCEL,
                    "Generate review Cancel returned the wrong decision",
                )

            frame.settings["general"]["order_number"] = True
            try:
                with interact_modal(
                    wx, modules.main.GenerationReviewDialog, cancel_placeholder
                ) as reviews:
                    generate(frame)
            finally:
                frame.settings["general"]["order_number"] = False
            require(
                len(reviews) == 1 and not messages,
                "Generate did not reach the real findings review",
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
            require(
                modules.adapter.snapshot_board(board, pcbnew) == original_snapshot,
                "Cancelled Generate changed the live native board",
            )

            for failure in ("report", "publication"):
                failed_before_roles = renderer_roles.copy()
                failed_before_inputs = {
                    role: value.copy() for role, value in renderer_inputs.items()
                }
                failed_before_cache = capture_state(frame)
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
                failed_export = render_activity(
                    failed_before_roles,
                    failed_before_cache,
                    frame,
                    failed_before_inputs,
                )
                render_receipts[failure + "_failure"] = failed_export
                require(
                    failed_export["rendered"]
                    == {"trace": 0, "spacing": 0, "original": 0}
                    and (failure != "publication" or failed_export["cache_hits"] >= 97),
                    "Warm failed Generate repeated native rendering or skipped owned PNG verification",
                )
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
                require(
                    all(
                        path.read_bytes() == contents
                        for path, contents in original_files.items()
                    ),
                    failure + " failure changed source PCB/project files",
                )
                require(
                    modules.adapter.snapshot_board(board, pcbnew) == original_snapshot,
                    failure + " failure changed the live native board",
                )
                if diagnostic_profile:
                    heartbeat(failure + " failure verification")
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
            require(
                counts["approval_passes"] == 2
                and counts["spacing_explicit_approvals"] == 130
                and counts["workbook_preview_visits"] == 72
                and enabled["workbook_images"] == 32
                and counts["generate_toolbar_events"] == 5,
                "Generate workflow did not exercise its complete approval/toolbar workload",
            )
            return {
                "performance": performance_evidence(),
                "scope": "real JLCPCBTools constructor and toolbar handlers, GTK modal controls, native Gerber/Excellon/PNG rendering, SQLite and report/ZIP publication",
                "supplier_boundary": "deterministic catalog and calculator responses; HTTP requests blocked",
                "fixture": "copied six-layer USB sample; existing specification intent seeded before Configure",
                "drc": "optional force-DRC disabled; fixture zones prefilled once before source preservation baseline",
                "enabled_archive": enabled,
                "disabled_archive": disabled,
                "calculator_calls": len(calculator_calls),
                "configured_spacing": configured_spacing[0],
                "generated_spacing": spacing_report,
                "spacing_previews": spacing_previews,
                "accepted_scenarios": [
                    "Apply settings without capture approval",
                    "save incomplete target, reopen and resume editing",
                    "select stackup",
                    "calculate all layers",
                    "visible width comparisons",
                    "terminal fanouts stay quiet while explicit routed spacing defects remain visible",
                    "separate native spacing close-ups have external captions and individual approvals",
                    "spacing targets and measurements preserved across Save and reopen",
                    "generated HTML agrees with saved spacing targets and measured bounds",
                    "consecutive workbook Row references through refresh and reopening",
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
