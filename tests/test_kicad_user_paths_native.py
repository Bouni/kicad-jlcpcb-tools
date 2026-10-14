"""Drive the plugin's user paths on a board the running KiCad builds itself.

The native KiCad lanes run these on KiCad 7, 8, 9 and 10. Each lane builds and
saves the board through its own KiCad's API, so each path reads and writes that
KiCad's file format. The checks are structural; the golden tests compare bytes.
"""

from collections.abc import Iterator
import csv
from dataclasses import dataclass
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from types import ModuleType, SimpleNamespace
from typing import Any, Optional
import zipfile

import pytest

from .native_kicad_support import (
    native_bindings,
    native_field,
    native_fields,
    place_native_pad,
    set_native_field,
)
from .native_window_support import window_ui
from .native_wx_support import isolated_native
from .wx_harness import ROOT, load_siblings, temporary_modules

__all__ = ["native_bindings", "window_ui"]

# The board's top-left corner on KiCad's canvas, and its size, in millimetres.
LEFT, TOP, WIDTH, HEIGHT = 100.0, 100.0, 40.0, 30.0


@dataclass(frozen=True)
class Pad:
    """One pad, relative to its footprint's origin, in millimetres."""

    number: str
    kind: str  # "smd", "tht" or "npth"
    x: float
    y: float
    width: float
    height: float
    drill: float = 0.0
    net: str = ""


@dataclass(frozen=True)
class Part:
    """One placed footprint and its assembly data."""

    reference: str
    value: str
    footprint: str
    x: float
    y: float
    rotation: float = 0.0
    lcsc: str = ""
    alias: str = "LCSC"
    back: bool = False
    excluded: bool = False  # from both the BOM and the CPL


def _chip(pitch: float, width: float, height: float) -> tuple[Pad, ...]:
    """Return a two-terminal chip's pads, either side of its origin."""
    return (
        Pad("1", "smd", -pitch / 2, 0, width, height),
        Pad("2", "smd", pitch / 2, 0, width, height),
    )


FOOTPRINTS = {
    "R_0603_1608Metric": _chip(1.65, 0.8, 0.95),
    "C_0603_1608Metric": _chip(1.65, 0.8, 0.95),
    "LED_0603_1608Metric": _chip(1.575, 0.875, 0.95),
    # A header's origin is its first pin, so its pad centre sits below it.
    "PinHeader_1x02_P2.54mm_Vertical": (
        Pad("1", "tht", 0, 0, 1.7, 1.7, 1.0),
        Pad("2", "tht", 0, 2.54, 1.7, 1.7, 1.0, "GND"),
    ),
    "MountingHole_2.2mm_M2": (Pad("", "npth", 0, 0, 2.2, 2.2, 2.2),),
}

PARTS = (
    Part("R1", "10k", "R_0603_1608Metric", 8, 8, lcsc="C25804"),
    Part("R2", "10k", "R_0603_1608Metric", 16, 8, 90, lcsc="C25804"),
    Part("C1", "100n", "C_0603_1608Metric", 8, 20, lcsc="C14663", back=True),
    Part("D1", "LED", "LED_0603_1608Metric", 16, 20),
    Part(
        "J1",
        "Conn_01x02",
        "PinHeader_1x02_P2.54mm_Vertical",
        26,
        8,
        lcsc="C2337",
        alias="JLCPCB Part #",
    ),
    Part("H1", "MountingHole", "MountingHole_2.2mm_M2", 34, 22, excluded=True),
)


def _point(pcbnew: ModuleType, x: float, y: float) -> Any:
    """Return a canvas position for millimetres from the board's top-left corner."""
    return pcbnew.VECTOR2I(pcbnew.FromMM(LEFT + x), pcbnew.FromMM(TOP + y))


def _offset(pcbnew: ModuleType, x: float, y: float) -> Any:
    """Return a vector in millimetres."""
    return pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))


def _net(pcbnew: ModuleType, board: Any, name: str) -> Any:
    """Return the board's net of that name, adding it the first time."""
    net = board.FindNet(name)
    if net is None:
        net = pcbnew.NETINFO_ITEM(board, name)
        board.Add(net)
    return net


def _add_pad(pcbnew: ModuleType, board: Any, footprint: Any, pad: Pad) -> None:
    """Add one pad at its position relative to the footprint."""
    item = pcbnew.PAD(footprint)
    item.SetNumber(pad.number)
    item.SetSize(_offset(pcbnew, pad.width, pad.height))
    if pad.kind == "smd":
        item.SetShape(pcbnew.PAD_SHAPE_RECT)
        item.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        item.SetLayerSet(pcbnew.PAD.SMDMask())
    else:
        item.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        plated = pad.kind == "tht"
        item.SetAttribute(pcbnew.PAD_ATTRIB_PTH if plated else pcbnew.PAD_ATTRIB_NPTH)
        item.SetLayerSet(
            pcbnew.PAD.PTHMask() if plated else pcbnew.PAD.UnplatedHoleMask()
        )
        item.SetDrillSize(_offset(pcbnew, pad.drill, pad.drill))
    place_native_pad(item, _offset(pcbnew, pad.x, pad.y))
    if pad.net:
        item.SetNet(_net(pcbnew, board, pad.net))
    footprint.Add(item)


def _add_part(pcbnew: ModuleType, board: Any, part: Part) -> None:
    """Place one footprint with its pads, fields and assembly flags."""
    footprint = pcbnew.FOOTPRINT(board)
    footprint.SetFPID(pcbnew.LIB_ID("UserPaths", part.footprint))
    footprint.SetReference(part.reference)
    footprint.SetValue(part.value)
    pads = FOOTPRINTS[part.footprint]
    if any(pad.kind == "tht" for pad in pads):
        attributes = pcbnew.FP_THROUGH_HOLE
    elif any(pad.kind == "smd" for pad in pads):
        attributes = pcbnew.FP_SMD
    else:
        attributes = 0
    if part.excluded:
        attributes |= pcbnew.FP_EXCLUDE_FROM_BOM | pcbnew.FP_EXCLUDE_FROM_POS_FILES
    footprint.SetAttributes(attributes)
    if part.lcsc:
        set_native_field(footprint, part.alias, part.lcsc)
        if hasattr(footprint, "GetFields"):
            native_field(footprint, part.alias).SetVisible(False)
    for pad in pads:
        _add_pad(pcbnew, board, footprint, pad)
    board.Add(footprint)
    footprint.SetPosition(_point(pcbnew, part.x, part.y))
    footprint.SetOrientationDegrees(part.rotation)
    if part.back:
        # KiCad 9 replaced Flip's left/right flag with a direction.
        direction = getattr(pcbnew, "FLIP_DIRECTION_LEFT_RIGHT", True)
        footprint.Flip(footprint.GetPosition(), direction)


def build_board(pcbnew: ModuleType, directory: Path, name: str = "user_paths") -> Path:
    """Build and save a two-layer board through the running KiCad's own API.

    It holds SMD parts on both sides, one turned; a through-hole header and an
    unplated mounting hole; two parts sharing an LCSC number, one unassigned
    part, one excluded from the BOM and CPL; and an unfilled ground pour.
    """
    board = pcbnew.BOARD()
    path = directory / f"{name}.kicad_pcb"
    board.SetFileName(str(path))
    outline = pcbnew.PCB_SHAPE(board)
    outline.SetShape(pcbnew.SHAPE_T_RECT)
    outline.SetStart(_point(pcbnew, 0, 0))
    outline.SetEnd(_point(pcbnew, WIDTH, HEIGHT))
    outline.SetLayer(pcbnew.Edge_Cuts)
    outline.SetWidth(pcbnew.FromMM(0.1))
    board.Add(outline)
    # The CPL and plots measure from the bottom-left corner.
    board.GetDesignSettings().SetAuxOrigin(_point(pcbnew, 0, HEIGHT))
    for part in PARTS:
        _add_part(pcbnew, board, part)
    zone = pcbnew.ZONE(board)
    zone.SetLayer(pcbnew.B_Cu)
    zone.SetNet(_net(pcbnew, board, "GND"))
    corners = zone.Outline()
    corners.NewOutline()
    for x, y in ((0.5, 0.5), (WIDTH - 0.5, 0.5), (WIDTH - 0.5, HEIGHT - 0.5)):
        corner = _point(pcbnew, x, y)
        corners.Append(corner.x, corner.y)
    corner = _point(pcbnew, 0.5, HEIGHT - 0.5)
    corners.Append(corner.x, corner.y)
    board.Add(zone)
    assert pcbnew.SaveBoard(str(path), board), "KiCad could not save the board"
    return path


@pytest.fixture
def user_board(native_bindings: SimpleNamespace, tmp_path: Path) -> Path:
    """Save the user-path board in its own project folder."""
    project = tmp_path / "project"
    project.mkdir()
    return build_board(native_bindings.pcbnew, project)


@pytest.fixture
def plugin(native_bindings: SimpleNamespace) -> Iterator[SimpleNamespace]:
    """Load the plugin's board, assignment and fabrication modules on real KiCad."""
    with load_siblings(
        "_user_paths_native_test",
        (
            "board_part_edits",
            "board_parts",
            "fabrication",
            "kicad_backend.swig.dialects",
            "kicad_backend.swig.read",
            "footprint_helpers",
            "schematic_discovery",
            "schematic_safety",
            "schematicexport",
            "store",
        ),
        {},
    ) as modules:
        yield SimpleNamespace(
            pcbnew=native_bindings.pcbnew,
            **{name.replace(".", "_"): module for name, module in modules.items()},
        )


@pytest.fixture
def native_window_board(native_bindings: SimpleNamespace, user_board: Path) -> Any:
    """Open the user-path board for the plugin window in place of its doubles."""
    return native_bindings.pcbnew.LoadBoard(str(user_board))


@pytest.mark.native_kicad
def test_the_plugin_loads_on_this_kicad(
    native_bindings: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dialect speaks this KiCad, the package registers its action, the UI imports."""
    pcbnew, wx = native_bindings.pcbnew, native_bindings.wx
    with load_siblings(
        "_user_paths_load_test",
        (
            "kicad_backend.swig.dialects",
            "mainwindow",
            "partselector",
            "schematicexport",
            "variant.matrix_view",
        ),
        {},
    ) as modules:
        major = str(pcbnew.GetBuildVersion()).split(".", 1)[0]
        assert modules["kicad_backend.swig.dialects"].select(pcbnew).major == int(major)
        assert modules["mainwindow"].kicad_pcbnew is pcbnew
        assert issubclass(modules["mainwindow"].JLCPCBTools, wx.Frame)
        assert issubclass(modules["partselector"].PartSelectorDialog, wx.Dialog)
        assert callable(modules["schematicexport"].SchematicExport)
        assert issubclass(
            modules["variant.matrix_view"].VariantMatrixView, wx.grid.Grid
        )

    registered: list[Any] = []
    # KiCad's own list of action plugins is not readable from Python.
    monkeypatch.setattr(
        pcbnew.PYTHON_ACTION_PLUGINS, "register_action", registered.append
    )
    monkeypatch.setattr(sys, "path", list(sys.path))
    name = "_user_paths_package_test"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    with temporary_modules({name: package}, namespaces=(name,)):
        spec.loader.exec_module(package)
        assert [type(action) for action in registered] == [package.JLCPCBPlugin]
    action = registered[0]
    assert isinstance(action, pcbnew.ActionPlugin)
    assert action.GetName() == "JLCPCB Tools"
    assert action.GetShowToolbarButton() is True
    assert Path(action.GetIconFileName(False)).is_file()
    assert action.GetPluginPath().endswith("plugin.py/JLCPCBPlugin")


@pytest.mark.native_kicad
def test_the_plugin_window_lists_the_board_and_leaves_it_unchanged(
    native_window_board: Any, window_ui: Any, tmp_path: Path
) -> None:
    """The plugin action opens the main window on the board KiCad has open."""
    ui = window_ui
    path = Path(ui.board.GetFileName())
    saved = path.read_bytes()

    def open_board(stage: str) -> bytes:
        """Write the board KiCad has open, as a save from the editor would."""
        copy = tmp_path / stage / path.name
        copy.parent.mkdir()
        assert ui.pcbnew.SaveBoard(str(copy), native_window_board, True)
        return copy.read_bytes()

    # KiCad saves the board it has open, so that must not change either.
    # KiCad 8 and 10 normalise a loaded board on save, so compare two saves.
    opened = open_board("opened")

    def check(ui: Any) -> None:
        frame = ui.dialog
        assert ui.plugin.pcbnew.GetBoard() is native_window_board
        assert ui.controller is None and not frame._project_storage_unavailable
        columns = frame.partlist_data_model.columns
        rows = {
            row[columns["REF_COL"]]: (
                row[columns["VALUE_COL"]],
                row[columns["LCSC_COL"]],
            )
            for row in frame.partlist_data_model.data
        }
        assert rows == {part.reference: (part.value, part.lcsc) for part in PARTS}

    ui.run(check)
    assert path.read_bytes() == saved
    assert open_board("closed") == opened


@pytest.mark.native_kicad
def test_assignments_save_in_the_board_and_leave_other_fields_alone(
    plugin: SimpleNamespace, user_board: Path, tmp_path: Path
) -> None:
    """Assign, update an alias, clear and exclude; a reload reads every change."""
    pcbnew = plugin.pcbnew
    board = pcbnew.LoadBoard(str(user_board))
    footprints = {
        part.reference: board.FindFootprintByReference(part.reference) for part in PARTS
    }
    before = {reference: native_fields(fp) for reference, fp in footprints.items()}
    plugin.board_part_edits.apply_board_part_edits(
        [
            (footprints["D1"], {"lcsc": "C2286"}),
            (footprints["J1"], {"lcsc": "C49661"}),
            (footprints["R2"], {"lcsc": ""}),
            (footprints["C1"], {"exclude_from_bom": True, "exclude_from_pos": True}),
        ]
    )
    saved = tmp_path / "assigned.kicad_pcb"
    assert pcbnew.SaveBoard(str(saved), board)
    reloaded = pcbnew.LoadBoard(str(saved))
    after = {
        part.reference: reloaded.FindFootprintByReference(part.reference)
        for part in PARTS
    }
    # Read the assignments back the way the parts list does.
    snapshot = plugin.kicad_backend_swig_read.read_snapshot(
        reloaded, plugin.kicad_backend_swig_dialects.select(pcbnew)
    )
    components = {
        part.reference: plugin.board_parts.find_component(snapshot, part.reference)
        for part in PARTS
    }
    assert {
        reference: plugin.board_parts.default_assignment(component)[1]
        for reference, component in components.items()
    } == {
        "R1": "C25804",
        "R2": "",
        "C1": "C14663",
        "D1": "C2286",
        "J1": "C49661",
        "H1": "",
    }
    flags = {
        reference: (
            component.default.exclude_from_bom,
            component.default.exclude_from_pos,
        )
        for reference, component in components.items()
    }
    assert {reference for reference, both in flags.items() if all(both)} == {"C1", "H1"}
    assert not any(
        any(both) for reference, both in flags.items() if reference not in {"C1", "H1"}
    )
    expected = {
        **before,
        "D1": {**before["D1"], "LCSC": "C2286"},
        "J1": {**before["J1"], "JLCPCB Part #": "C49661"},
        "R2": {**before["R2"], "LCSC": ""},
    }
    assert {reference: native_fields(fp) for reference, fp in after.items()} == expected


@pytest.mark.native_kicad
def test_generate_writes_the_fabrication_files(
    plugin: SimpleNamespace, user_board: Path
) -> None:
    """The main window's ordinary generation steps, in its order, on this board."""
    board = plugin.pcbnew.LoadBoard(str(user_board))
    defaults = json.loads((ROOT / "default_settings.json").read_text("utf-8"))
    parent = SimpleNamespace(settings={"gerber": defaults["gerber"]})
    # The window reads the editor's board through its backend. This board is
    # loaded rather than open in an editor, so it is read directly.
    dialect = plugin.kicad_backend_swig_dialects.select(plugin.pcbnew)
    parent._read_board = parent._held_board = (
        lambda: plugin.kicad_backend_swig_read.read_snapshot(board, dialect)
    )
    parent.store = plugin.store.Store(parent, str(user_board.parent))
    fabrication = plugin.fabrication.Fabrication(parent, board)
    output = fabrication.begin_ordinary_generation(())
    try:
        assert fabrication.get_part_consistency_warnings() == {}
        assert fabrication.fill_zones() == []
        fabrication.validate_generation()
        fabrication.generate_geber(None)
        fabrication.generate_excellon()
        archive = fabrication.zip_gerber_excellon()
        fabrication.write_cpl(output.cpl_rows)
        fabrication.generate_bom()
        fabrication.validate_generation()
    finally:
        fabrication.end_ordinary_generation()

    stem = user_board.stem
    with zipfile.ZipFile(archive) as gerbers:
        names = set(gerbers.namelist())
    assert names == {
        *(
            f"{stem}-{layer}.gbr"
            for layer in (
                "CuTop",
                "SilkTop",
                "MaskTop",
                "PasteTop",
                "CuBottom",
                "SilkBottom",
                "MaskBottom",
                "PasteBottom",
                "EdgeCuts",
            )
        ),
        f"{stem}-PTH.drl",
        f"{stem}-NPTH.drl",
        f"{stem}-PTH-drl_map.pdf",
        f"{stem}-NPTH-drl_map.pdf",
    }

    with open(fabrication.get_cpl_csv_path(), encoding="utf-8", newline="") as file:
        header, *placements = list(csv.reader(file))
    assert header == [
        "Designator",
        "Val",
        "Package",
        "Mid X",
        "Mid Y",
        "Rotation",
        "Layer",
    ]
    # Pad centres measured from the bottom-left corner: the header's pads sit
    # below its first pin, and the bottom-side part keeps its symmetric centre.
    expected = {
        "C1": ("100n", "C_0603_1608Metric", 8, 20, "0.0", "bottom"),
        "D1": ("LED", "LED_0603_1608Metric", 16, 20, "0.0", "top"),
        "J1": ("Conn_01x02", "PinHeader_1x02_P2.54mm_Vertical", 26, 9.27, "0.0", "top"),
        "R1": ("10k", "R_0603_1608Metric", 8, 8, "0.0", "top"),
        "R2": ("10k", "R_0603_1608Metric", 16, 8, "90.0", "top"),
    }
    assert [row[0] for row in placements] == list(expected)
    for reference, value, package, x, y, rotation, side in placements:
        value_, package_, x_, y_, rotation_, side_ = expected[reference]
        assert (value, package, rotation, side) == (value_, package_, rotation_, side_)
        assert abs(float(x) - x_) <= 0.001, (reference, x)
        assert abs(float(y) - (HEIGHT - y_)) <= 0.001, (reference, y)

    with open(fabrication.get_bom_csv_path(), encoding="utf-8", newline="") as file:
        bom = list(csv.reader(file))
    assert bom == [
        ["Comment", "Designator", "Footprint", "LCSC", "Quantity"],
        ["100n", "C1", "C_0603_1608Metric", "C14663", "1"],
        ["10k", "R1,R2", "R_0603_1608Metric", "C25804", "2"],
        ["Conn_01x02", "J1", "PinHeader_1x02_P2.54mm_Vertical", "C2337", "1"],
        ["LED", "D1", "LED_0603_1608Metric", "", "1"],
    ]


def _violating_copy(pcbnew: ModuleType, source: Path) -> Path:
    """Copy the board with two tracks of different nets closer than its clearance."""
    board = pcbnew.LoadBoard(str(source))
    # The default net class keeps 0.2 mm between copper; these edges are 0.15 mm apart.
    for net, y in (("A", 26.0), ("B", 26.35)):
        track = pcbnew.PCB_TRACK(board)
        track.SetStart(_point(pcbnew, 4, y))
        track.SetEnd(_point(pcbnew, 12, y))
        track.SetWidth(pcbnew.FromMM(0.2))
        track.SetLayer(pcbnew.F_Cu)
        track.SetNet(_net(pcbnew, board, net))
        board.Add(track)
    path = source.with_name("violating.kicad_pcb")
    assert pcbnew.SaveBoard(str(path), board), "KiCad could not save the copy"
    return path


def _open_for_drc(pcbnew: ModuleType, path: Path) -> Any:
    """Open a board as the PCB editor holds it when the plugin runs its DRC.

    KiCad 7 and 8 never initialise BOARD::m_DRCMaxClearance, and their DRC
    widens each clearance query by the larger of it and the real clearance.
    A stale value overflows those queries: KiCad 7 then misses clearance
    errors, and KiCad 8 asserts, which outside KiCad can stop the process.
    The DRC clears the value when it moves the board's timestamp on, but
    only once a board cache is filled, as drawing the board in the editor
    fills them; a board fresh from LoadBoard has none. KiCad 9 initialises
    the value.
    """
    board = pcbnew.LoadBoard(str(path))
    for zone in board.Zones():
        zone.GetBoundingBox()
    board.IncrementTimeStamp()
    return board


@pytest.mark.native_kicad
@pytest.mark.kicad_standalone_drc
def test_forced_drc_counts_the_errors_in_this_kicads_report(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report is written and parsed: no errors on the board, one on a copy.

    It runs in a fresh process without a wx application. KiCad 8's standalone
    DRC can assert on a worker thread, and wxPython's assert handler would then
    wait for the interpreter lock that the DRC call holds.
    """
    if isolated_native(request, "KICAD_JLCPCB_DRC_CHILD", native="kicad"):
        return
    pcbnew = pytest.importorskip("pcbnew", reason="KiCad pcbnew is unavailable")
    project = tmp_path / "project"
    project.mkdir()
    board = build_board(pcbnew, project)
    with load_siblings("_user_paths_drc_test", ("kicad_drc",), {}) as modules:
        counter = modules["kicad_drc"].DRCViolationCounter(
            pcbnew_module=pcbnew, working_dir=str(tmp_path)
        )
        for path, errors in ((board, 0), (_violating_copy(pcbnew, board), 1)):
            # The plugin's DRC runs on the board the editor has open.
            opened = _open_for_drc(pcbnew, path)
            assert opened.GetFileName() == str(path)
            monkeypatch.setattr(pcbnew, "GetBoard", lambda opened=opened: opened)
            count, messages = counter.get_error_violations(str(path))
            assert count == errors
            codes = [text.partition(":")[0] for text in messages]
            assert codes == ["[clearance]"] * errors


FIXTURES = Path(__file__).parent / "fixtures" / "user_paths"
# Every fixture project uses these UUIDs; see the fixtures' README.
ROOT_SHEET = "6d2a9e3f-7c1b-4e85-9a3d-2f4b6c8e0a11"
SYMBOLS = {
    "R1": "1b3c5d7e-9f1a-4b2c-8d3e-5f7a9b1c3d21",
    "R2": "2c4d6e8f-a0b1-4c3d-9e4f-6a8b0c2d4e32",
}


def _parse_sexpr(text: str) -> list[Any]:
    """Read one S-expression document into nested lists of unquoted atoms."""
    stack: list[list[Any]] = [[]]
    for token in re.findall(r'\(|\)|"(?:\\.|[^"\\])*"|[^\s()"]+', text):
        if token == "(":
            stack.append([])
        elif token == ")":
            closed = stack.pop()
            stack[-1].append(closed)
        else:
            stack[-1].append(token[1:-1] if token.startswith('"') else token)
    assert len(stack) == 1 and len(stack[0]) == 1, "Unbalanced S-expression"
    return stack[0][0]


def _kicad_cli(major: str) -> str:
    """Find kicad-cli and check that it belongs to the KiCad running the test.

    A newer kicad-cli reads older schematics too, so one from another KiCad
    on PATH would not prove that this KiCad reads its own format.
    """
    cli = shutil.which("kicad-cli")
    if cli is None:
        pytest.skip("kicad-cli is not on PATH")
    version = subprocess.run(
        [cli, "version"], capture_output=True, text=True, timeout=120, check=False
    ).stdout.strip()
    found = re.match(r"\s*(\d+)\.", version)
    assert found and found.group(1) == major, (
        f"{cli} reports KiCad {version!r}, but the running KiCad is {major}"
    )
    return cli


def _netlist_lcsc(cli: str, schematic: Path) -> dict[str, Optional[str]]:
    """Export a netlist with kicad-cli and read each part's LCSC field."""
    netlist = schematic.with_suffix(".net")
    result = subprocess.run(
        [
            cli,
            "sch",
            "export",
            "netlist",
            "--format",
            "kicadsexpr",
            "--output",
            str(netlist),
            str(schematic),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    document = _parse_sexpr(netlist.read_text(encoding="utf-8"))
    components = next(item for item in document if item[0] == "components")
    parts = {}
    for component in components[1:]:
        reference = next(item[1] for item in component if item[0] == "ref")
        properties = {
            item[1][1]: item[2][1] for item in component if item[0] == "property"
        }
        parts[reference] = properties.get("LCSC")
    return parts


def _without_new_field(text: str, value: str) -> str:
    """Remove the property the export adds, with the line break placed before it."""
    start = text.index(f'(property "LCSC" "{value}"')
    depth = 0
    for end in range(start, len(text)):
        depth += {"(": 1, ")": -1}.get(text[end], 0)
        if depth == 0:
            break
    line_start = len(text[:start].rstrip(" \t")) - 1
    assert text[line_start] == "\n"
    return text[:line_start] + text[end + 1 :]


@pytest.mark.native_kicad
def test_assignments_export_to_this_kicads_own_schematic(
    plugin: SimpleNamespace, tmp_path: Path
) -> None:
    """Board assignments reach both symbols; everything else in the file stays."""
    pcbnew = plugin.pcbnew
    found = re.match(r"\s*(\d+)\.", str(pcbnew.GetBuildVersion()))
    assert found, pcbnew.GetBuildVersion()
    fixture = FIXTURES / f"kicad{found.group(1)}"
    assert fixture.is_dir(), f"No schematic fixture for KiCad {found.group(1)}"
    cli = _kicad_cli(found.group(1))
    project = tmp_path / "user_paths"
    project.mkdir()
    board_path = build_board(pcbnew, project)
    for source in fixture.iterdir():
        shutil.copyfile(source, project / source.name)
    schematic = project / "user_paths.kicad_sch"
    original = schematic.read_text(encoding="utf-8")
    assert _netlist_lcsc(cli, schematic) == {"R1": "C17414", "R2": None}

    settings = json.loads((project / "user_paths.kicad_pro").read_text("utf-8"))
    # KiCad 10 projects list their root sheets, and KiCad 10's Update PCB puts
    # the root sheet in each footprint's path; earlier versions leave it out.
    rooted = bool(settings.get("schematic", {}).get("top_level_sheets"))
    board = pcbnew.LoadBoard(str(board_path))
    for reference, symbol in SYMBOLS.items():
        link = f"/{ROOT_SHEET}/{symbol}" if rooted else f"/{symbol}"
        board.FindFootprintByReference(reference).SetPath(pcbnew.KIID_PATH(link))
    plugin.footprint_helpers.set_lcsc_value(
        board.FindFootprintByReference("R2"), "C25744"
    )
    host = SimpleNamespace(
        GetBoard=lambda: board, GetSettingsManager=pcbnew.GetSettingsManager
    )
    parent = SimpleNamespace(
        board_name=board_path.name,
        project_path=str(project),
        pcbnew=host,
        # The window reads the live board through its backend.
        _read_board=lambda: plugin.kicad_backend_swig_read.read_snapshot(
            board, plugin.kicad_backend_swig_dialects.select(pcbnew)
        ),
    )

    # The main window's export_to_schematic, without its dialogs.
    project_name = plugin.schematic_safety.authenticated_project_name(
        host, str(project)
    )
    assert project_name == "user_paths"
    discovery = plugin.schematic_discovery.discover_project_schematics(
        str(project), board_path.name, project_name
    )
    assert discovery.status == "present"
    assert discovery.paths == (str(schematic),)
    context: dict[str, Any] = {"shared_project": True}
    if discovery.root_uuids:
        context["root_uuids"] = dict(discovery.root_uuids)
    outcome = plugin.schematicexport.SchematicExport(parent).load_schematic(
        discovery.paths, **context
    )

    linked = {
        str(board.FindFootprintByReference(reference).m_Uuid.AsString())
        for reference in SYMBOLS
    }
    assert set(outcome.saved) == linked and not outcome.diagnostics
    assert Path(f"{schematic}_old").read_text(encoding="utf-8") == original
    exported = schematic.read_text(encoding="utf-8")
    assert exported.count('"C25804"') == 1
    restored = _without_new_field(exported, "C25744").replace('"C25804"', '"C17414"')
    assert restored == original
    before_new_field = exported[: exported.index('(property "LCSC" "C25744"')]
    reference = before_new_field.rindex('(property "Reference" ')
    assert before_new_field[reference:].startswith('(property "Reference" "R2"')
    assert _netlist_lcsc(cli, schematic) == {"R1": "C25804", "R2": "C25744"}
