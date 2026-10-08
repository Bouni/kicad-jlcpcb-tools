"""Check the IPC pcbnew facade used by KiCad 10.99+ against SWIG semantics."""

from collections.abc import Iterator
import importlib
import importlib.util
from pathlib import Path
import sys
import time
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from tests.wx_harness import ROOT, module, package_stubs, temporary_modules

for _dependency in ("google.protobuf", "pynng", "zstandard"):
    pytest.importorskip(_dependency)


@pytest.fixture
def ipc(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    """Load the facade with the bundled kipy, outside any package."""
    monkeypatch.syspath_prepend(str(ROOT / "lib"))
    spec = importlib.util.spec_from_file_location(
        "_ipc_pcbnew_test", ROOT / "ipc_pcbnew.py"
    )
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    yield loaded


def _kipy_footprint(reference: str = "R1", *, front: bool = True) -> Any:
    from kipy.board_types import FootprintInstance  # noqa: PLC0415
    from kipy.proto.board.board_types_pb2 import BoardLayer  # noqa: PLC0415

    footprint = FootprintInstance()
    footprint.reference_field.text.value = reference
    footprint.value_field.name = "Value"
    footprint.value_field.text.value = "10k"
    footprint.layer = BoardLayer.BL_F_Cu if front else BoardLayer.BL_B_Cu
    return footprint


class _FakeKipyBoard:
    """Record commit traffic instead of talking to KiCad."""

    def __init__(self, footprints: tuple[Any, ...]) -> None:
        self.footprints = list(footprints)
        self.calls: list[tuple[str, Any]] = []

    def get_footprints(self) -> list[Any]:
        return self.footprints

    def begin_commit(self) -> str:
        self.calls.append(("begin", None))
        return "commit"

    def update_items(self, items: list[Any]) -> list[Any]:
        self.calls.append(("update", list(items)))
        return items

    def push_commit(self, commit: str, message: str) -> None:
        self.calls.append(("push", message))

    def drop_commit(self, commit: str) -> None:
        self.calls.append(("drop", None))


def _board(ipc: ModuleType, *footprints: Any) -> Any:
    """Build a BOARD around fake kipy state without a KiCad connection."""
    board = ipc.BOARD.__new__(ipc.BOARD)
    board.session = SimpleNamespace(call=lambda function, *a, **k: function(*a, **k))
    board.kipy = _FakeKipyBoard(footprints)
    board._footprints = [ipc.FOOTPRINT(board, item) for item in footprints]
    board._loaded_at = time.monotonic()
    board._pad_boxes = None
    board._commit = None
    board._dirty = {}
    board._commits = 0
    return board


def test_layer_ids_follow_swig_numbering(ipc: ModuleType) -> None:
    """Fabrication compares GetLayer() with 0 and plots In<n>_Cu by name."""
    assert (ipc.F_Cu, ipc.B_Cu, ipc.In1_Cu, ipc.In30_Cu) == (0, 2, 4, 62)
    assert (ipc.F_SilkS, ipc.B_SilkS, ipc.Edge_Cuts, ipc.F_Fab) == (5, 7, 25, 35)
    assert (ipc.User_1, ipc.User_45) == (39, 127)
    assert ipc.IsCopperLayer(ipc.In12_Cu) and not ipc.IsCopperLayer(ipc.F_Mask)
    for value in ipc._LAYER_IDS.values():
        assert ipc._from_kipy_layer(ipc._to_kipy_layer(value)) == value


def test_units_and_points_match_pcbnew(ipc: ModuleType) -> None:
    """Conversions truncate like pcbnew.FromMM and round like wxPoint."""
    assert ipc.ToMM(1_500_000) == 1.5
    assert ipc.FromMM(0.0000019) == 1
    assert (ipc.wxPoint(1.5, -1.5).x, ipc.wxPoint(1.5, -1.5).y) == (2, -2)
    box = ipc.BOX2I(0, 0, 3, 3).Merge(ipc.BOX2I(1, 1, 4, 4))
    assert box.GetCenter() == ipc.VECTOR2I(2, 2)


def test_plot_suffix_sanitized_like_kicad(ipc: ModuleType) -> None:
    """BuildPlotFileName replaces illegal characters, '%' and '.' with '_'."""
    assert ipc._sanitize_suffix(" JLC_FAB.User ") == "JLC_FAB_User"
    assert ipc._sanitize_suffix('a/b\\c:d*e?f"g<h>i|j%k') == "a_b_c_d_e_f_g_h_i_j_k"


def test_attribute_bits_round_trip(ipc: ModuleType) -> None:
    """Exclusion, board-only and DNP bits map onto the API attributes."""
    from kipy.proto.board.board_types_pb2 import FootprintMountingStyle  # noqa: PLC0415

    kipy = _kipy_footprint()
    kipy.attributes.mounting_style = FootprintMountingStyle.FMS_THROUGH_HOLE
    board = _board(ipc, kipy)
    footprint = board.GetFootprints()[0]
    assert footprint.GetAttributes() == ipc.FP_THROUGH_HOLE
    with board.commit("test"):
        footprint.SetAttributes(
            ipc.FP_THROUGH_HOLE
            | ipc.FP_EXCLUDE_FROM_BOM
            | ipc.FP_EXCLUDE_FROM_POS_FILES
        )
    assert kipy.attributes.exclude_from_bill_of_materials
    assert kipy.attributes.exclude_from_position_files
    assert kipy.attributes.mounting_style == FootprintMountingStyle.FMS_THROUGH_HOLE
    assert footprint.GetAttributes() == 1 | 4 | 8
    assert not footprint.IsDNP()


def test_fields_are_added_changed_and_removed(ipc: ModuleType) -> None:
    """User fields follow pcbnew's SetField/GetFieldByName/Remove behaviour."""
    board = _board(ipc, _kipy_footprint())
    footprint = board.GetFootprints()[0]
    names = [field.GetName() for field in footprint.GetFields()]
    assert names == ["Reference", "Value", "Datasheet", "Description"]
    with board.commit("test"):
        footprint.SetField("LCSC", "C25804")
        footprint.GetFieldByName("LCSC").SetVisible(False)
        footprint.SetField("Value", "4k7")
    lcsc = footprint.GetFieldByName("LCSC")
    assert (lcsc.GetText(), lcsc.IsVisible()) == ("C25804", False)
    assert footprint.GetValue() == "4k7"
    with board.commit("test"):
        footprint.Remove(lcsc)
        with pytest.raises(ValueError):
            footprint.Remove(footprint.GetField("Value"))
    assert footprint.GetField("LCSC") is None


def test_variant_overrides_fall_back_to_base(ipc: ModuleType) -> None:
    """Effective variant values come from the override or the base footprint."""
    board = _board(ipc, _kipy_footprint())
    footprint = board.GetFootprints()[0]
    assert footprint.GetVariant("Lite") is None
    assert footprint.GetFieldValueForVariant("Lite", "Value") == "10k"
    variant = ipc.FOOTPRINT_VARIANT("Lite")
    variant.SetDNP(True)
    variant.SetFieldValue("LCSC", "C1")
    with board.commit("test"):
        footprint.SetVariant(variant)
    copy = footprint.GetVariant("Lite")
    assert copy.GetDNP() and copy.GetFields() == {"LCSC": "C1"}
    assert footprint.GetDNPForVariant("Lite") and not footprint.IsDNP()
    assert footprint.GetFieldValueForVariant("Lite", "LCSC") == "C1"
    assert not footprint.GetExcludedFromBOMForVariant("Lite")
    with board.commit("test"):
        footprint.DeleteVariant("Lite")
    assert footprint.GetVariant("Lite") is None


def test_edits_require_a_commit(ipc: ModuleType) -> None:
    """Edits outside a commit would never reach KiCad, so they are rejected."""
    board = _board(ipc, _kipy_footprint())
    with pytest.raises(RuntimeError, match="commit"):
        board.GetFootprints()[0].SetField("LCSC", "C1")


def test_commit_sends_changed_footprints_once(ipc: ModuleType) -> None:
    """One commit updates each changed footprint, or drops an empty commit."""
    first, second = _kipy_footprint("R1"), _kipy_footprint("R2")
    board = _board(ipc, first, second)
    with board.commit("JLCPCB Tools"):
        footprint = board.FindFootprintByReference("R1")
        footprint.SetField("LCSC", "C1")
        footprint.SetAttributes(ipc.FP_EXCLUDE_FROM_BOM)
    assert board.kipy.calls == [
        ("begin", None),
        ("update", [first]),
        ("push", "JLCPCB Tools"),
    ]
    board = _board(ipc, first)
    with board.commit("noop"):
        pass
    assert board.kipy.calls == [("begin", None), ("drop", None)]


def test_failed_commit_is_dropped(ipc: ModuleType) -> None:
    """KiCad discards every change of a failed action."""
    board = _board(ipc, _kipy_footprint())
    with pytest.raises(KeyError), board.commit("test"):
        board.GetFootprints()[0].SetField("LCSC", "C1")
        raise KeyError("boom")
    assert board.kipy.calls == [("begin", None), ("drop", None)]
    assert board._dirty == {}


class _PlotBoard:
    """The board surface used by PLOT_CONTROLLER and EXCELLON_WRITER."""

    def __init__(self, ipc: ModuleType) -> None:
        self.session = SimpleNamespace()
        self._ipc = ipc

    def GetFileName(self) -> str:  # noqa: N802
        return "/project/board.kicad_pcb"

    def GetProject(self) -> None:  # noqa: N802
        return None

    def GetLayerName(self, layer: int) -> str:  # noqa: N802
        return {0: "top.layer", 25: "Edge.Cuts", 39: "JLC_FAB"}[layer]

    def GetDesignSettings(self) -> Any:  # noqa: N802
        return SimpleNamespace(GetAuxOrigin=lambda: self._ipc.VECTOR2I(10, 20))

    def serialize(self) -> bytes:
        return b"(kicad_pcb)"


def test_plots_run_once_and_keep_pcbnew_names(
    ipc: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Queued layers plot in one kicad-cli run and move to pcbnew filenames."""
    runs: list[list[str]] = []

    def run_cli(_session: Any, arguments: list[str]) -> None:
        runs.append(arguments)
        output = Path(arguments[arguments.index("--output") + 1])
        output.mkdir()
        for name in ("top_layer", "Edge_Cuts", "JLC_FAB"):
            (output / f"board-{name}.gbr").write_text(name)

    monkeypatch.setattr(ipc, "_run_cli", run_cli)
    controller = ipc.PLOT_CONTROLLER(_PlotBoard(ipc))
    options = controller.GetPlotOptions()
    options.SetOutputDirectory(str(tmp_path))
    options.SetPlotValue(False)
    options.SetSubtractMaskFromSilk(True)
    options.SetUseAuxOrigin(True)
    for layer, suffix in ((0, "CuTop"), (25, "EdgeCuts"), (39, "JLC_FAB")):
        controller.SetLayer(layer)
        assert controller.OpenPlotfile(suffix, ipc.PLOT_FORMAT_GERBER, suffix)
        assert controller.PlotLayer()
    assert runs == []
    controller.ClosePlot()
    assert len(runs) == 1
    arguments = runs[0]
    assert arguments[arguments.index("--layers") + 1] == "F.Cu,Edge.Cuts,User.1"
    assert {
        "--exclude-value",
        "--subtract-soldermask",
        "--use-drill-file-origin",
    } <= set(arguments)
    assert "--variant" not in arguments
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "board-CuTop.gbr",
        "board-EdgeCuts.gbr",
        "board-JLC_FAB.gbr",
    ]
    assert (tmp_path / "board-CuTop.gbr").read_text() == "top_layer"


def test_drill_origin_follows_offset(
    ipc: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The aux-origin offset selects plot origin; other offsets are refused."""
    runs: list[list[str]] = []
    monkeypatch.setattr(ipc, "_run_cli", lambda _s, arguments: runs.append(arguments))
    writer = ipc.EXCELLON_WRITER(_PlotBoard(ipc))
    writer.SetOptions(False, False, ipc.VECTOR2I(10, 20), False)
    writer.SetFormat(True)
    assert writer.CreateDrillandMapFilesSet(str(tmp_path), True, True)
    arguments = runs[0]
    assert arguments[arguments.index("--drill-origin") + 1] == "plot"
    assert {"--excellon-separate-th", "--generate-map"} <= set(arguments)
    writer.SetOptions(False, False, ipc.VECTOR2I(1, 1), False)
    with pytest.raises(ValueError):
        writer.CreateDrillandMapFilesSet(str(tmp_path), True, True)


def test_runtime_selects_backend_without_importing_swig(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The IPC process must not load a stale SWIG pcbnew from site-packages."""
    monkeypatch.syspath_prepend(str(ROOT / "lib"))
    package = "_kicad_runtime_test"
    swig = module("pcbnew", marker="swig")
    with temporary_modules(
        {**package_stubs(package), "pcbnew": swig}, namespaces=[package]
    ):
        runtime = importlib.import_module(f"{package}.kicad_runtime")
        monkeypatch.delenv(runtime.BACKEND_ENV, raising=False)
        assert runtime.import_pcbnew() is swig
        monkeypatch.setenv(runtime.BACKEND_ENV, runtime.IPC_BACKEND)
        assert runtime.import_pcbnew() is sys.modules[f"{package}.ipc_pcbnew"]


def test_ipc_process_skips_action_plugin_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importing the package for IPC never imports the SWIG action plugin."""
    package = "_ipc_package_test"
    monkeypatch.setenv("KICAD_JLCPCB_TOOLS_BACKEND", "ipc")
    spec = importlib.util.spec_from_file_location(
        package, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    with temporary_modules({package: loaded}, namespaces=[package]):
        spec.loader.exec_module(loaded)
        assert f"{package}.plugin" not in sys.modules
