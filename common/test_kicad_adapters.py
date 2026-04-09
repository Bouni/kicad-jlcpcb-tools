"""Tests for adapter-based KiCad abstraction in standalone mode."""

from kicad_api import BoardAPI, FootprintAPI, GerberAPI, UtilityAPI
from standalone_impl import create_adapter_set


def test_standalone_adapter_set_shape():
    """Standalone adapter set exposes the same high-level structure as runtime adapters."""
    adapters = create_adapter_set()

    assert isinstance(adapters.board, BoardAPI)
    assert isinstance(adapters.footprint, FootprintAPI)
    assert isinstance(adapters.gerber, GerberAPI)
    assert isinstance(adapters.utility, UtilityAPI)
    assert adapters.version == (8, 0, 1)


def test_board_adapter_basic_calls():
    """Board adapter methods return expected stub values."""
    adapters = create_adapter_set()

    assert adapters.board.get_board_filename() == "fake_test_board.kicad_pcb"
    assert adapters.board.get_current_selection() == []
    assert adapters.board.get_copper_layer_count() == 2
    assert adapters.board.get_aux_origin().x == 0
    assert adapters.board.get_aux_origin().y == 0

    fps = adapters.board.get_all_footprints()
    assert len(fps) == 1
    assert adapters.footprint.get_reference(fps[0]) == "R1"
    assert len(adapters.board.get_footprints()) == 1


def test_utility_adapter_constants():
    """Utility adapter returns wrapped constants used by UI/business logic."""
    adapters = create_adapter_set()

    layer_constants = adapters.utility.get_layer_constants()
    assert layer_constants["F_Cu"] == 0
    assert layer_constants["B_Cu"] == 1

    pcb_constants = adapters.utility.get_pcb_constants()
    assert "F_SilkS" in pcb_constants
    assert "B_SilkS" in pcb_constants
    assert "PCB_TEXT" in pcb_constants
    assert "PCB_SHAPE" in pcb_constants
    assert "S_RECT" in pcb_constants
