"""Regression tests for Gerber layer selection by copper-layer count."""

from collections.abc import Callable, Iterator
from pathlib import Path
import types
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from tests.wx_harness import load_siblings, module

_TOP_LAYER_NAMES = ("CuTop", "SilkTop", "MaskTop", "PasteTop")
_BOTTOM_LAYER_NAMES = ("CuBottom", "SilkBottom", "MaskBottom", "PasteBottom")

_LAYER_ID_PROFILES = {
    "kicad-8": {
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
    "kicad-9-plus": {
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
}


@pytest.fixture
def plotted_layers(
    request: pytest.FixtureRequest, tmp_path: Path
) -> Iterator[Callable[..., types.SimpleNamespace]]:
    """Load Fabrication with isolated KiCad mocks and return a plot runner."""
    pcbnew = types.ModuleType("pcbnew")
    constants = {
        "PLOT_FORMAT_GERBER": 1,
        "DRILL_MARKS_NO_DRILL_SHAPE": 0,
        **_LAYER_ID_PROFILES[getattr(request, "param", "kicad-9-plus")],
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
    pcbnew.PLOT_CONTROLLER = MagicMock(return_value=plot_controller)

    def generate(
        layer_count: Optional[int],  # noqa: UP045
        failure: str = "",
        board_name: str = "board.kicad_pcb",
        previous_plot: Optional[dict[str, Any]] = None,  # noqa: UP045
    ) -> types.SimpleNamespace:
        board = MagicMock(name="board")
        board.GetFileName.return_value = str(tmp_path / board_name)
        Path(board.GetFileName()).write_text("(kicad_pcb)\n", encoding="utf-8")
        board.GetCopperLayerCount.return_value = 1
        board.GetEnabledLayers.return_value.Seq.return_value = []

        fabrication = loaded["fabrication"].Fabrication(MagicMock(settings={}), board)
        if previous_plot is not None:
            path = Path(fabrication.gerberdir) / "previous.gbr"
            path.write_bytes(b"previous copper")
            previous_plot.update(path=path, controller=plot_controller)
        if failure == "drill":
            pcbnew.EXCELLON_WRITER.return_value.CreateDrillandMapFilesSet.return_value = False
            fabrication.generate_excellon()
            return types.SimpleNamespace()

        plot_controller.OpenPlotfile.return_value = failure != "open"
        plot_controller.PlotLayer.return_value = failure != "plot"
        plot_controller.PlotLayer.side_effect = (
            OSError("plot output denied") if failure == "exception" else None
        )
        plot_controller.GetPlotOptions.side_effect = (
            OSError("plot configuration unavailable") if failure == "config" else None
        )
        try:
            fabrication.generate_geber(layer_count)
        finally:
            plot_controller.ClosePlot.assert_called_once()
        return types.SimpleNamespace(
            names=[
                call.args[0] for call in plot_controller.OpenPlotfile.call_args_list
            ],
            skip_npth=[
                call.args[0]
                for call in plot_options.SetSkipPlotNPTH_Pads.call_args_list
            ],
        )

    package = "_gerber_layer_tests"
    with load_siblings(
        package,
        ("fabrication",),
        {
            "pcbnew": pcbnew,
            f"{package}.footprint_helpers": module(
                f"{package}.footprint_helpers", get_is_dnp=lambda _footprint: False
            ),
        },
    ) as loaded:
        yield generate


@pytest.mark.parametrize("plotted_layers", _LAYER_ID_PROFILES, indirect=True)
@pytest.mark.parametrize(
    ("layer_count", "expected_names"),
    [
        (None, (*_TOP_LAYER_NAMES, "SilkBottom", "EdgeCuts")),
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
def test_generate_gerber_plots_exact_layers_and_skips_npth_only_on_copper(
    plotted_layers: Callable[..., types.SimpleNamespace],
    layer_count: Optional[int],  # noqa: UP045
    expected_names: tuple[str, ...],
) -> None:
    """Explicit counts and the board fallback preserve layer order and pad policy."""
    result = plotted_layers(layer_count)

    assert result.names == list(expected_names)
    assert result.skip_npth == [name.startswith("Cu") for name in expected_names]


@pytest.mark.parametrize(
    "failure,error,message",
    [
        ("open", RuntimeError, "Could not open plot file"),
        ("plot", RuntimeError, "Error plotting"),
        ("exception", OSError, "plot output denied"),
        ("config", OSError, "plot configuration unavailable"),
    ],
)
def test_plot_failure_propagates_and_closes_controller(
    plotted_layers: Callable[..., types.SimpleNamespace],
    failure: str,
    error: type[Exception],
    message: str,
) -> None:
    """A failed layer reports failure and releases native plotting resources."""
    with pytest.raises(error, match=message):
        plotted_layers(2, failure=failure)


def test_long_native_plot_name_fails_before_removing_previous_plots(
    plotted_layers: Callable[..., types.SimpleNamespace],
) -> None:
    """A legal board basename may leave insufficient room for native layer suffixes."""
    previous: dict[str, Any] = {}
    with pytest.raises(
        ValueError, match="Shorten the board filename or custom layer name"
    ):
        plotted_layers(2, board_name="b" * 245 + ".kicad_pcb", previous_plot=previous)
    assert previous["path"].read_bytes() == b"previous copper"
    previous["controller"].OpenPlotfile.assert_not_called()


def test_drill_writer_failure_is_reported(
    plotted_layers: Callable[..., types.SimpleNamespace],
) -> None:
    """A partial drill/map set must not be reported as a successful generation."""
    with pytest.raises(
        RuntimeError, match="Could not generate complete drill and map files"
    ):
        plotted_layers(2, failure="drill")
