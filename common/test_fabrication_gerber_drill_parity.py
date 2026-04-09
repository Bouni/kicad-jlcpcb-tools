"""Parity tests for Gerber/Drill generation behavior and settings handling."""

from pathlib import Path

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


class _BaseBoardAdapter:
    @staticmethod
    def get_copper_layer_count() -> int:
        return 2

    @staticmethod
    def get_enabled_layers():
        return [0, 1, 2, 3, 4, 5, 7, 8, 9, 42]

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
        return names[layer_id]

    @staticmethod
    def get_aux_origin():
        return _Point(111, 222)


class _SWIGBoardAdapter(_BaseBoardAdapter):
    pass


class _IPCBoardAdapter(_BaseBoardAdapter):
    pass


class _BaseUtilityAdapter:
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
        return 77

    @staticmethod
    def get_plot_format_gerber() -> int:
        return 1

    @staticmethod
    def get_inner_cu_layer(layer: int) -> int:
        return 100 + layer

    def refill_zones(self, _board) -> None:
        self.refill_calls += 1


class _SWIGUtilityAdapter(_BaseUtilityAdapter):
    pass


class _IPCUtilityAdapter(_BaseUtilityAdapter):
    pass


class _RecorderGerberAdapter:
    def __init__(self):
        self.calls = []

    @staticmethod
    def create_plot_controller(_board):
        return "plot-controller"

    @staticmethod
    def get_plot_options(_plot_controller):
        return "plot-options"

    def set_output_directory(self, _plot_options, directory: str) -> None:
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
        self.calls.append(("set_skip_plot_npth_pads", value))

    def set_layer(self, _plot_controller, layer_id: int) -> None:
        self.calls.append(("set_layer", layer_id))

    def open_plot_file(
        self, _plot_controller, filename: str, extension: str, plot_title: str
    ) -> None:
        self.calls.append(("open_plot_file", filename, extension, plot_title))

    def plot_layer(self, _plot_controller) -> bool:
        self.calls.append(("plot_layer",))
        return True

    def close_plot(self, _plot_controller) -> None:
        self.calls.append(("close_plot",))

    @staticmethod
    def create_excellon_writer(_board):
        return "excellon-writer"

    def set_drill_options(self, _writer, **kwargs) -> None:
        self.calls.append(("set_drill_options", kwargs))

    def set_drill_format(self, _writer, metric: bool) -> None:
        self.calls.append(("set_drill_format", metric))

    def generate_drill_files(self, _writer, output_directory: str) -> None:
        self.calls.append(("generate_drill_files", output_directory))


class _AdapterSet:
    def __init__(self, board, utility, gerber):
        self.board = board
        self.utility = utility
        self.gerber = gerber


def _normalize_calls(calls):
    """Normalize paths in call tuples so parity compares behavior, not temp dirs."""

    def _normalize_value(value):
        if isinstance(value, _Point):
            return (value.x, value.y)
        if isinstance(value, tuple):
            return tuple(_normalize_value(v) for v in value)
        if isinstance(value, list):
            return [_normalize_value(v) for v in value]
        if isinstance(value, dict):
            return {k: _normalize_value(v) for k, v in value.items()}
        return value

    normalized = []
    for call in calls:
        name = call[0]
        if name in {"set_output_directory", "generate_drill_files"}:
            normalized.append((name, "<OUTPUT_DIR>"))
        elif name == "set_drill_options":
            normalized.append((name, _normalize_value(call[1])))
        else:
            normalized.append(call)
    return normalized


def _run_export_flow(tmp_path: Path, adapter_set, settings: dict):
    board_path = tmp_path / "fixture.kicad_pcb"
    board_path.parent.mkdir(parents=True, exist_ok=True)
    board_path.write_text("", encoding="utf-8")

    parent = _FakeParent(settings)
    board = _PathBoard(board_path)

    fab = Fabrication(parent, board, adapter_set=adapter_set)
    fab.fill_zones()
    fab.generate_geber(layer_count=2)
    fab.generate_excellon()

    return {
        "calls": _normalize_calls(adapter_set.gerber.calls),
        "refill_calls": adapter_set.utility.refill_calls,
    }


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
def test_gerber_drill_call_parity_and_settings(
    tmp_path,
    settings,
    expected_vias_on_mask,
    expected_refill_calls,
    expected_values,
    expected_refs,
):
    """Gerber/Drill behavior and settings should match SWIG-like and IPC-like paths."""
    swig_adapter_set = _AdapterSet(
        _SWIGBoardAdapter(),
        _SWIGUtilityAdapter(),
        _RecorderGerberAdapter(),
    )
    ipc_adapter_set = _AdapterSet(
        _IPCBoardAdapter(),
        _IPCUtilityAdapter(),
        _RecorderGerberAdapter(),
    )

    swig_result = _run_export_flow(tmp_path / "swig", swig_adapter_set, settings)
    ipc_result = _run_export_flow(tmp_path / "ipc", ipc_adapter_set, settings)

    assert swig_result["calls"] == ipc_result["calls"]
    assert swig_result["refill_calls"] == ipc_result["refill_calls"]
    assert swig_result["refill_calls"] == expected_refill_calls

    calls = swig_result["calls"]
    assert ("set_plot_component_values", expected_values) in calls
    assert ("set_plot_reference_designators", expected_refs) in calls
    assert ("set_plot_vias_on_mask", expected_vias_on_mask) in calls

    # Drill generation behavior
    assert ("set_drill_format", False) in calls
    drill_option_calls = [c for c in calls if c[0] == "set_drill_options"]
    assert len(drill_option_calls) == 1
    assert "Options" in drill_option_calls[0][1]

    # 2-layer plot plan + one JLC_* layer + close call
    open_calls = [c for c in calls if c[0] == "open_plot_file"]
    assert len(open_calls) == 10
