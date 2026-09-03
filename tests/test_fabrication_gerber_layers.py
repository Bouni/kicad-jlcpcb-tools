"""Regression tests for Gerber layer selection by copper-layer count."""

import importlib.util
from pathlib import Path
import sys
import types
from unittest.mock import MagicMock
import uuid

import pytest

_ROOT = Path(__file__).parent.parent

_TOP_LAYER_NAMES = ("CuTop", "SilkTop", "MaskTop", "PasteTop")
_BOTTOM_LAYER_NAMES = ("CuBottom", "SilkBottom", "MaskBottom", "PasteBottom")

_LAYER_ID_PROFILES = (
    pytest.param(
        {
            "F_Cu": 0,
            "In1_Cu": 1,
            "In2_Cu": 2,
            "B_Cu": 31,
            "B_Paste": 34,
            "F_Paste": 35,
            "B_SilkS": 36,
            "F_SilkS": 37,
            "B_Mask": 38,
            "F_Mask": 39,
            "Edge_Cuts": 44,
        },
        id="kicad-8",
    ),
    pytest.param(
        {
            "F_Cu": 0,
            "F_Mask": 1,
            "B_Cu": 2,
            "B_Mask": 3,
            "In1_Cu": 4,
            "F_SilkS": 5,
            "In2_Cu": 6,
            "B_SilkS": 7,
            "F_Paste": 13,
            "B_Paste": 15,
            "Edge_Cuts": 25,
        },
        id="kicad-9-plus",
    ),
)


@pytest.fixture(params=_LAYER_ID_PROFILES)
def plotted_layers(request, monkeypatch, tmp_path):
    """Load Fabrication with isolated KiCad mocks and return a plot runner."""
    pcbnew = types.ModuleType("pcbnew")
    constants = {
        "PLOT_FORMAT_GERBER": 1,
        "DRILL_MARKS_NO_DRILL_SHAPE": 0,
        **request.param,
    }
    for name, value in constants.items():
        setattr(pcbnew, name, value)

    copper_ids = {
        constants["F_Cu"],
        constants["In1_Cu"],
        constants["In2_Cu"],
        constants["B_Cu"],
    }
    pcbnew.IsCopperLayer = MagicMock(side_effect=copper_ids.__contains__)

    for name in (
        "EXCELLON_WRITER",
        "PCB_PLOT_PARAMS",
        "PCB_VIA",
        "VECTOR2I",
        "ZONE_FILLER",
        "FromMM",
        "Refresh",
        "ToMM",
        "wxPoint",
    ):
        setattr(pcbnew, name, MagicMock(name=name))

    plot_options = MagicMock(name="plot_options")
    plot_controller = MagicMock(name="plot_controller")
    plot_controller.GetPlotOptions.return_value = plot_options
    plot_controller.PlotLayer.return_value = True
    pcbnew.PLOT_CONTROLLER = MagicMock(return_value=plot_controller)
    monkeypatch.setitem(sys.modules, "pcbnew", pcbnew)

    package_name = f"_gerber_layer_test_{uuid.uuid4().hex}"
    package = types.ModuleType(package_name)
    package.__path__ = [str(_ROOT)]
    monkeypatch.setitem(sys.modules, package_name, package)

    footprint_helpers = types.ModuleType(f"{package_name}.footprint_helpers")
    footprint_helpers.get_is_dnp = MagicMock(return_value=False)
    monkeypatch.setitem(
        sys.modules,
        f"{package_name}.footprint_helpers",
        footprint_helpers,
    )

    module_name = f"{package_name}.fabrication"
    spec = importlib.util.spec_from_file_location(module_name, _ROOT / "fabrication.py")
    assert spec is not None and spec.loader is not None
    fabrication_module = importlib.util.module_from_spec(spec)
    fabrication_module.__package__ = package_name
    monkeypatch.setitem(sys.modules, module_name, fabrication_module)
    spec.loader.exec_module(fabrication_module)

    def generate(layer_count, board_layer_count=1):
        board = MagicMock(name="board")
        board.GetCopperLayerCount.return_value = board_layer_count
        board.GetEnabledLayers.return_value.Seq.return_value = []

        fabrication = object.__new__(fabrication_module.Fabrication)
        fabrication.parent = MagicMock(settings={})
        fabrication.board = board
        fabrication.gerberdir = str(tmp_path)
        fabrication.logger = MagicMock(name="logger")

        plot_options.SetSkipPlotNPTH_Pads.reset_mock()
        plot_controller.OpenPlotfile.reset_mock()
        fabrication.generate_geber(layer_count)
        return types.SimpleNamespace(
            names=[
                call.args[0] for call in plot_controller.OpenPlotfile.call_args_list
            ],
            skip_npth=[
                call.args[0]
                for call in plot_options.SetSkipPlotNPTH_Pads.call_args_list
            ],
        )

    return generate


@pytest.mark.parametrize(
    ("layer_count", "expected_names"),
    [
        (1, (*_TOP_LAYER_NAMES, "SilkBottom", "EdgeCuts")),
        (2, (*_TOP_LAYER_NAMES, *_BOTTOM_LAYER_NAMES, "EdgeCuts")),
        (
            4,
            (
                *_TOP_LAYER_NAMES,
                "CuIn1",
                "CuIn2",
                *_BOTTOM_LAYER_NAMES,
                "EdgeCuts",
            ),
        ),
    ],
)
def test_generate_gerber_plots_exact_layers(
    plotted_layers, layer_count, expected_names
):
    """Explicit layer counts produce the exact ordered Gerber plot plan."""
    result = plotted_layers(layer_count)

    assert result.names == list(expected_names)


def test_generate_gerber_uses_single_layer_board_count(plotted_layers):
    """An omitted layer count uses the board's one-layer plot plan."""
    result = plotted_layers(None, board_layer_count=1)

    assert result.names == [
        *_TOP_LAYER_NAMES,
        "SilkBottom",
        "EdgeCuts",
    ]


@pytest.mark.parametrize(
    ("layer_count", "copper_names"),
    [
        (1, ("CuTop",)),
        (2, ("CuTop", "CuBottom")),
        (4, ("CuTop", "CuIn1", "CuIn2", "CuBottom")),
    ],
)
def test_generate_gerber_skips_npth_pads_only_on_copper_layers(
    plotted_layers, layer_count, copper_names
):
    """NPTH pads are skipped only while plotting physical copper layers."""
    result = plotted_layers(layer_count)

    assert len(result.skip_npth) == len(result.names)
    assert result.skip_npth == [name in copper_names for name in result.names]
