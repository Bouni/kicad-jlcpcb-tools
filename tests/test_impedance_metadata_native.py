"""Compare real KiCad fields, source snapshots and freshly rasterized captures.

These tests require an explicitly enabled KiCad 10 desktop lane. Pure tests in
test_impedance_render separately cover capture caching and SQLite approval reopen.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from impedance.board_copy import empty_board
from impedance.matching import analyze
from impedance.model import Config, LayerSettings, Section, Specification
from impedance.pcbnew_adapter import snapshot_board
from impedance.render import SectionRenderer
from tests.native_kicad_support import native_bindings, requires_native_plotting
from tests.native_wx_support import isolated_native

pytestmark = [
    pytest.mark.native_kicad,
    pytest.mark.kicad10_impedance,
    requires_native_plotting,
]

__all__ = ["native_bindings"]


def _point(pcbnew: Any, x: float, y: float) -> Any:
    """Place native fixture geometry using physical millimetres."""
    return pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))


def _fixture(pcbnew: Any, board: Any, path: Path) -> Any:
    """Create one routed signal and nearby footprint with normal native fields."""
    board.SetFileName(str(path))
    net = pcbnew.NETINFO_ITEM(board, "SIGNAL")
    board.Add(net)
    track = pcbnew.PCB_TRACK(board)
    track.SetStart(_point(pcbnew, 10, 10))
    track.SetEnd(_point(pcbnew, 20, 10))
    track.SetWidth(pcbnew.FromMM(0.2))
    track.SetLayer(pcbnew.F_Cu)
    track.SetNet(net)
    board.Add(track)
    footprint = pcbnew.FOOTPRINT(board)
    footprint.SetReference("R1")
    footprint.SetPosition(_point(pcbnew, 15, 10))
    reference = footprint.Reference()
    reference.SetLayer(pcbnew.F_SilkS)
    reference.SetVisible(True)
    reference.SetPosition(_point(pcbnew, 15, 8))
    reference.SetTextSize(_point(pcbnew, 1, 1))
    reference.SetTextThickness(pcbnew.FromMM(0.15))
    footprint.Value().SetVisible(False)
    board.Add(footprint)
    return footprint


def _config() -> Config:
    """Select the real native Default class containing the fixture's signal."""
    return Config(
        True,
        (
            Specification(
                "signal",
                "Signal",
                "50",
                "single_ended",
                "Default",
                (LayerSettings("F.Cu", ("B.Cu",)),),
            ),
        ),
    )


def _record(pcbnew: Any, footprint: Any) -> str:
    """Use the native const item formatter to detect accidental source writes."""
    formatter = pcbnew.PCB_IO_KICAD_SEXPR()
    formatter.Format(footprint)
    return str(formatter.GetStringOutput(True))


def _capture(pcbnew: Any, board: Any, row: Section, path: Path) -> bytes:
    """Use a fresh native renderer so a cache cannot conceal changed pixels."""
    with SectionRenderer(board, pcbnew) as renderer:
        return renderer.render(row, path).read_bytes()


def _bindings(request: pytest.FixtureRequest) -> SimpleNamespace:
    """Resolve fixtures only in the isolated native child, after availability checks."""
    bindings = request.getfixturevalue("native_bindings")
    if int(str(bindings.pcbnew.GetBuildVersion()).split(".", 1)[0]) < 10:
        pytest.skip("Safe impedance capture requires KiCad 10 or newer")
    return bindings


def test_native_hidden_field_edits_preserve_snapshot_and_fresh_pixels(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """Actual SetField additions/edits/clears/deletions have no plotted effect."""
    if isolated_native(request, "KICAD_IMPEDANCE_METADATA_CHILD", native="kicad"):
        return
    pcbnew = _bindings(request).pcbnew
    with empty_board(pcbnew) as board:
        footprint = _fixture(pcbnew, board, tmp_path / "metadata.kicad_pcb")
        original = snapshot_board(board, pcbnew)
        row = analyze(_config(), original).sections[0]
        baseline = _record(pcbnew, footprint)
        image = _capture(pcbnew, board, row, tmp_path / "before.png")
        assert _record(pcbnew, footprint) == baseline
        for index, value in enumerate(("C123", "C456", "", None)):
            if value is None:
                footprint.Delete(footprint.GetField("LCSC"))
            else:
                footprint.SetField("LCSC", value)
                footprint.GetField("LCSC").SetVisible(False)
                assert footprint.GetField("LCSC").IsVisible() is False
            source = _record(pcbnew, footprint)
            assert snapshot_board(board, pcbnew) == original
            assert (
                _capture(pcbnew, board, row, tmp_path / f"after-{index}.png") == image
            )
            assert _record(pcbnew, footprint) == source


@pytest.mark.parametrize("display", ["field", "consumer", "fab_consumer"])
def test_native_shown_fields_and_hidden_field_consumers_invalidate_pixels(
    request: pytest.FixtureRequest, tmp_path: Path, display: str
) -> None:
    """Native visible fields and ${LCSC} text remain approval dependencies."""
    if isolated_native(request, "KICAD_IMPEDANCE_METADATA_CHILD", native="kicad"):
        return
    pcbnew = _bindings(request).pcbnew
    with empty_board(pcbnew) as board:
        footprint = _fixture(pcbnew, board, tmp_path / "metadata.kicad_pcb")
        footprint.SetField("LCSC", "C123")
        field = footprint.GetField("LCSC")
        if display == "field":
            shown = field
            field.SetVisible(True)
        else:
            field.SetVisible(display == "fab_consumer")
            field.SetLayer(pcbnew.F_Fab)
            assert field.IsVisible() is (display == "fab_consumer")
            shown = pcbnew.PCB_TEXT(footprint)
            shown.SetText("${LCSC}")
            footprint.Add(shown)
        shown.SetLayer(pcbnew.F_SilkS)
        shown.SetPosition(_point(pcbnew, 15, 9))
        shown.SetTextSize(_point(pcbnew, 1, 1))
        shown.SetTextThickness(pcbnew.FromMM(0.15))
        assert shown.GetShownText(True) == "C123"
        original = snapshot_board(board, pcbnew)
        row = analyze(_config(), original).sections[0]
        before = _capture(pcbnew, board, row, tmp_path / "before.png")
        footprint.SetField("LCSC", "C456")
        source = _record(pcbnew, footprint)
        assert shown.GetShownText(True) == "C456"
        assert snapshot_board(board, pcbnew) != original
        assert _capture(pcbnew, board, row, tmp_path / "after.png") != before
        assert _record(pcbnew, footprint) == source


@pytest.mark.parametrize(
    "change",
    ["exclude_from_bom", "exclude_from_pos", "description", "keywords", "fab_value"],
)
def test_native_unplotted_metadata_preserves_snapshot_and_fresh_pixels(
    request: pytest.FixtureRequest, tmp_path: Path, change: str
) -> None:
    """Actual assembly metadata and visible F.Fab Value do not change captures."""
    if isolated_native(request, "KICAD_IMPEDANCE_METADATA_CHILD", native="kicad"):
        return
    pcbnew = _bindings(request).pcbnew
    with empty_board(pcbnew) as board:
        footprint = _fixture(pcbnew, board, tmp_path / "metadata.kicad_pcb")
        value = footprint.Value()
        value.SetVisible(True)
        value.SetLayer(pcbnew.F_Fab)
        value.SetPosition(_point(pcbnew, 15, 9))
        value.SetTextSize(_point(pcbnew, 1, 1))
        value.SetTextThickness(pcbnew.FromMM(0.15))
        footprint.SetValue("10k")
        original = snapshot_board(board, pcbnew)
        row = analyze(_config(), original).sections[0]
        before = _capture(pcbnew, board, row, tmp_path / "before.png")
        if change == "exclude_from_bom":
            footprint.SetAttributes(
                footprint.GetAttributes() ^ pcbnew.FP_EXCLUDE_FROM_BOM
            )
        elif change == "exclude_from_pos":
            footprint.SetAttributes(
                footprint.GetAttributes() ^ pcbnew.FP_EXCLUDE_FROM_POS_FILES
            )
        elif change == "description":
            footprint.SetLibDescription('Unplotted "description" (metadata)')
        elif change == "keywords":
            footprint.SetKeywords("Unplotted metadata keywords")
        else:
            footprint.SetValue("20k")
        source = _record(pcbnew, footprint)
        assert snapshot_board(board, pcbnew) == original
        assert _capture(pcbnew, board, row, tmp_path / "after.png") == before
        assert _record(pcbnew, footprint) == source


@pytest.mark.parametrize("location", ["board", "footprint"])
@pytest.mark.parametrize("layer", ["F_Fab", "F_CrtYd", "Dwgs_User"])
def test_native_unplotted_graphics_and_text_follow_plotted_layer_transitions(
    request: pytest.FixtureRequest, tmp_path: Path, location: str, layer: str
) -> None:
    """Real shape/text additions are inert until moved onto a capture layer."""
    if isolated_native(request, "KICAD_IMPEDANCE_METADATA_CHILD", native="kicad"):
        return
    pcbnew = _bindings(request).pcbnew
    with empty_board(pcbnew) as board:
        footprint = _fixture(pcbnew, board, tmp_path / "metadata.kicad_pcb")
        original = snapshot_board(board, pcbnew)
        row = analyze(_config(), original).sections[0]
        before = _capture(pcbnew, board, row, tmp_path / "before.png")
        parent = board if location == "board" else footprint
        shape = pcbnew.PCB_SHAPE(parent)
        shape.SetShape(pcbnew.SHAPE_T_SEGMENT)
        shape.SetStart(_point(pcbnew, 14, 9))
        shape.SetEnd(_point(pcbnew, 16, 11))
        shape.SetWidth(pcbnew.FromMM(0.2))
        shape.SetLayer(getattr(pcbnew, layer))
        parent.Add(shape)
        text = pcbnew.PCB_TEXT(parent)
        text.SetText("Nonplotted context")
        text.SetLayer(getattr(pcbnew, layer))
        text.SetPosition(_point(pcbnew, 15, 9))
        text.SetTextSize(_point(pcbnew, 1, 1))
        text.SetTextThickness(pcbnew.FromMM(0.15))
        parent.Add(text)
        assert snapshot_board(board, pcbnew) == original
        assert _capture(pcbnew, board, row, tmp_path / "nonplotted.png") == before
        shape.SetLayer(pcbnew.F_SilkS)
        text.SetLayer(pcbnew.F_SilkS)
        assert snapshot_board(board, pcbnew) != original
        assert _capture(pcbnew, board, row, tmp_path / "plotted.png") != before
