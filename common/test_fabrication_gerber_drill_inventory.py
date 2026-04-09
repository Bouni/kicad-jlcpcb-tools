"""Comprehensive Gerber/Drill inventory and parity tests."""

from pathlib import Path
from zipfile import ZipFile

import pytest

from fabrication import Fabrication


class _Point:
    def __init__(self, x: int, y: int):
        self.x = x
        self.y = y

    def __iter__(self):
        yield self.x
        yield self.y


class _PathBoard:
    def __init__(self, board_path: Path):
        self._path = board_path

    def GetFileName(self) -> str:
        return str(self._path)


class _FakeParent:
    def __init__(self, gerber_settings):
        self.settings = {"gerber": dict(gerber_settings)}


class _BoardAdapter:
    def __init__(self, copper_layers: int, enabled_layers: list[int], aux_origin: _Point):
        self._copper_layers = copper_layers
        self._enabled_layers = enabled_layers
        self._aux_origin = aux_origin

    def get_copper_layer_count(self) -> int:
        return self._copper_layers

    def get_enabled_layers(self):
        return list(self._enabled_layers)

    @staticmethod
    def get_layer_name(layer_id: int) -> str:
        names = {
            0: "F_Cu",
            1: "B_Cu",
            2: "F_SilkS",
            3: "B_SilkS",
            4: "F_Mask",
            5: "B_Mask",
            7: "F_Paste",
            8: "B_Paste",
            9: "Edge_Cuts",
            42: "JLC_Assy",
        }
        return names.get(layer_id, str(layer_id))

    def get_aux_origin(self):
        return self._aux_origin


class _UtilityAdapter:
    def __init__(self):
        self.refill_calls = 0

    @staticmethod
    def get_layer_constants():
        return {
            "F_Cu": 0,
            "B_Cu": 1,
            "F_SilkS": 2,
            "B_SilkS": 3,
            "F_Mask": 4,
            "B_Mask": 5,
            "F_Paste": 7,
            "B_Paste": 8,
            "Edge_Cuts": 9,
        }

    @staticmethod
    def get_no_drill_shape() -> int:
        return 99

    @staticmethod
    def get_plot_format_gerber() -> int:
        return 1

    @staticmethod
    def get_inner_cu_layer(layer: int) -> int:
        # Distinct IDs (> B_Cu) so NPTH skipping logic can be verified clearly.
        return 100 + layer

    def refill_zones(self, _board) -> None:
        self.refill_calls += 1


class _FileWritingGerberBase:
    def __init__(self):
        self.output_directory = None
        self._current_filename = None
        self.calls = []
        self.skip_npth_flags = []
        self.drill_options = None

    @staticmethod
    def create_plot_controller(_board):
        return object()

    @staticmethod
    def get_plot_options(_plot_controller):
        return object()

    def set_output_directory(self, _plot_options, directory: str) -> None:
        self.output_directory = directory
        self.calls.append(("set_output_directory", directory))

    def set_format(self, _plot_options, format_id: int) -> None:
        self.calls.append(("set_format", format_id))

    def set_plot_component_values(self, _plot_options, value: bool) -> None:
        self.calls.append(("set_plot_component_values", value))

    def set_plot_reference_designators(self, _plot_options, value: bool) -> None:
        self.calls.append(("set_plot_reference_designators", value))

    def set_sketch_pads_on_mask_layers(self, _plot_options, value: bool) -> None:
        self.calls.append(("set_sketch_pads_on_mask_layers", value))

    def set_use_protel_extensions(self, _plot_options, value: bool) -> None:
        self.calls.append(("set_use_protel_extensions", value))

    def set_create_job_file(self, _plot_options, value: bool) -> None:
        self.calls.append(("set_create_job_file", value))

    def set_mask_color(self, _plot_options, value: bool) -> None:
        self.calls.append(("set_mask_color", value))

    def set_use_auxiliary_origin(self, _plot_options, value: bool) -> None:
        self.calls.append(("set_use_auxiliary_origin", value))

    def set_plot_vias_on_mask(self, _plot_options, value: bool) -> None:
        self.calls.append(("set_plot_vias_on_mask", value))

    def set_use_x2_format(self, _plot_options, value: bool) -> None:
        self.calls.append(("set_use_x2_format", value))

    def set_include_netlist_attributes(self, _plot_options, value: bool) -> None:
        self.calls.append(("set_include_netlist_attributes", value))

    def set_disable_macros(self, _plot_options, value: bool) -> None:
        self.calls.append(("set_disable_macros", value))

    def set_drill_marks(self, _plot_options, mark_type: int) -> None:
        self.calls.append(("set_drill_marks", mark_type))

    def set_plot_frame_ref(self, _plot_options, value: bool) -> None:
        self.calls.append(("set_plot_frame_ref", value))

    def set_skip_plot_npth_pads(self, _plot_options, value: bool) -> None:
        self.skip_npth_flags.append(value)
        self.calls.append(("set_skip_plot_npth_pads", value))

    def set_layer(self, _plot_controller, layer_id: int) -> None:
        self.calls.append(("set_layer", layer_id))

    def open_plot_file(
        self, _plot_controller, filename: str, extension: str, plot_title: str
    ) -> None:
        self._current_filename = filename
        self.calls.append(("open_plot_file", filename, extension, plot_title))

    def plot_layer(self, _plot_controller) -> bool:
        assert self.output_directory is not None
        assert self._current_filename is not None
        Path(self.output_directory).mkdir(parents=True, exist_ok=True)
        (Path(self.output_directory) / f"{self._current_filename}.gbr").write_text(
            f"G04 {self._current_filename}*\n",
            encoding="utf-8",
        )
        return True

    def close_plot(self, _plot_controller) -> None:
        self.calls.append(("close_plot",))

    @staticmethod
    def create_excellon_writer(_board):
        return object()

    def set_drill_options(self, _writer, **kwargs) -> None:
        self.drill_options = kwargs
        self.calls.append(("set_drill_options", kwargs))

    def set_drill_format(self, _writer, metric: bool) -> None:
        self.calls.append(("set_drill_format", metric))

    def generate_drill_files(self, _writer, output_directory: str) -> None:
        Path(output_directory).mkdir(parents=True, exist_ok=True)
        (Path(output_directory) / "drill.drl").write_text("M48\n", encoding="utf-8")
        self.calls.append(("generate_drill_files", output_directory))


class _SWIGFileGerber(_FileWritingGerberBase):
    pass


class _IPCFileGerber(_FileWritingGerberBase):
    pass


class _AdapterSet:
    def __init__(self, board, utility, gerber):
        self.board = board
        self.utility = utility
        self.gerber = gerber


def _expected_gerber_files(layer_count: int):
    if layer_count == 1:
        base = {
            "CuTop.gbr",
            "SilkTop.gbr",
            "MaskTop.gbr",
            "PasteTop.gbr",
            "EdgeCuts.gbr",
            "PasteBottom.gbr",
        }
    elif layer_count == 2:
        base = {
            "CuTop.gbr",
            "SilkTop.gbr",
            "MaskTop.gbr",
            "PasteTop.gbr",
            "CuBottom.gbr",
            "SilkBottom.gbr",
            "MaskBottom.gbr",
            "EdgeCuts.gbr",
            "PasteBottom.gbr",
        }
    else:
        base = {
            "CuTop.gbr",
            "SilkTop.gbr",
            "MaskTop.gbr",
            "PasteTop.gbr",
            "CuIn1.gbr",
            "CuIn2.gbr",
            "CuBottom.gbr",
            "SilkBottom.gbr",
            "MaskBottom.gbr",
            "EdgeCuts.gbr",
            "PasteBottom.gbr",
        }
    base.add("JLC_ASSY.gbr")
    return base


def _expected_skip_counts(layer_count: int):
    total = len(_expected_gerber_files(layer_count))
    true_count = 1 if layer_count == 1 else 2
    false_count = total - true_count
    return true_count, false_count


def _run_flow(tmp_path: Path, backend: str, layer_count: int, settings: dict):
    board_path = tmp_path / backend / "fixture.kicad_pcb"
    board_path.parent.mkdir(parents=True, exist_ok=True)
    board_path.write_text("", encoding="utf-8")

    parent = _FakeParent(settings)
    board = _PathBoard(board_path)

    enabled_layers = [0, 1, 2, 3, 4, 5, 7, 8, 9, 42]
    board_adapter = _BoardAdapter(layer_count, enabled_layers, aux_origin=_Point(7, 9))
    utility_adapter = _UtilityAdapter()
    gerber_adapter = _SWIGFileGerber() if backend == "swig" else _IPCFileGerber()
    adapter_set = _AdapterSet(board_adapter, utility_adapter, gerber_adapter)

    fab = Fabrication(parent, board, adapter_set=adapter_set)

    # Ensure old files are purged by generate_geber.
    stale = Path(fab.gerberdir) / "stale_file.gbr"
    stale.write_text("old", encoding="utf-8")

    fab.fill_zones()
    fab.generate_geber(layer_count=layer_count)
    fab.generate_excellon()
    fab.zip_gerber_excellon()

    gerber_files = {
        p.name
        for p in Path(fab.gerberdir).iterdir()
        if p.is_file() and p.suffix in {".gbr", ".drl"}
    }
    zip_path = Path(fab.outputdir) / f"GERBER-{board_path.stem}.zip"
    with ZipFile(zip_path, "r") as zf:
        zipped = set(zf.namelist())

    return {
        "fab": fab,
        "gerber": gerber_adapter,
        "utility": utility_adapter,
        "gerber_files": gerber_files,
        "zipped": zipped,
        "stale_exists": stale.exists(),
    }


@pytest.mark.parametrize("layer_count", [1, 2, 4])
@pytest.mark.parametrize(
    "settings, expected_vias_on_mask, expected_refill_calls, expected_values, expected_refs",
    [
        (
            {
                "fill_zones": True,
                "tented_vias": True,
                "plot_values": True,
                "plot_references": True,
            },
            False,
            1,
            True,
            True,
        ),
        (
            {
                "fill_zones": False,
                "tented_vias": False,
                "plot_values": False,
                "plot_references": False,
            },
            True,
            0,
            False,
            False,
        ),
    ],
)
def test_gerber_drill_inventory_and_backend_parity(
    tmp_path,
    layer_count,
    settings,
    expected_vias_on_mask,
    expected_refill_calls,
    expected_values,
    expected_refs,
):
    """Gerber/Drill outputs and settings behavior should match SWIG-like and IPC-like paths."""
    swig = _run_flow(tmp_path, "swig", layer_count, settings)
    ipc = _run_flow(tmp_path, "ipc", layer_count, settings)

    expected = _expected_gerber_files(layer_count)
    expected_with_drill = set(expected)
    expected_with_drill.add("drill.drl")

    # File inventory parity on disk.
    assert swig["gerber_files"] == expected_with_drill
    assert ipc["gerber_files"] == expected_with_drill
    assert swig["gerber_files"] == ipc["gerber_files"]

    # Stale files should be removed by generation pass.
    assert swig["stale_exists"] is False
    assert ipc["stale_exists"] is False

    # ZIP inventory parity and inclusion of generated outputs.
    assert swig["zipped"] == expected_with_drill
    assert ipc["zipped"] == expected_with_drill

    # Settings behavior parity.
    swig_calls = swig["gerber"].calls
    ipc_calls = ipc["gerber"].calls
    assert ("set_plot_component_values", expected_values) in swig_calls
    assert ("set_plot_reference_designators", expected_refs) in swig_calls
    assert ("set_plot_vias_on_mask", expected_vias_on_mask) in swig_calls
    assert ("set_plot_component_values", expected_values) in ipc_calls
    assert ("set_plot_reference_designators", expected_refs) in ipc_calls
    assert ("set_plot_vias_on_mask", expected_vias_on_mask) in ipc_calls
    assert swig["utility"].refill_calls == expected_refill_calls
    assert ipc["utility"].refill_calls == expected_refill_calls

    # NPTH pad skipping behavior parity and expected counts by layer plan.
    swig_skip = swig["gerber"].skip_npth_flags
    ipc_skip = ipc["gerber"].skip_npth_flags
    assert swig_skip == ipc_skip
    true_count, false_count = _expected_skip_counts(layer_count)
    assert swig_skip.count(True) == true_count
    assert swig_skip.count(False) == false_count

    # Drill options include aux origin from board adapter (same behavior both paths).
    swig_options = swig["gerber"].drill_options["Options"]
    ipc_options = ipc["gerber"].drill_options["Options"]
    assert (swig_options[2].x, swig_options[2].y) == (7, 9)
    assert (ipc_options[2].x, ipc_options[2].y) == (7, 9)
