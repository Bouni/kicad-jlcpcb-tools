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
import sys
from types import ModuleType, SimpleNamespace
from typing import Any
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
            "kicad_backend.swig.read",
            "footprint_helpers",
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
    """The version check passes, the package registers its action, the UI imports."""
    pcbnew, wx = native_bindings.pcbnew, native_bindings.wx
    with load_siblings(
        "_user_paths_load_test",
        (
            "core.version",
            "mainwindow",
            "partselector",
            "schematicexport",
            "variant.matrix_view",
        ),
        {},
    ) as modules:
        assert modules["core.version"].is_supported_version(pcbnew.GetBuildVersion())
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
    snapshot = plugin.kicad_backend_swig_read.read_snapshot(reloaded)
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
    parent._read_board = parent._held_board = (
        lambda: plugin.kicad_backend_swig_read.read_snapshot(board)
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
