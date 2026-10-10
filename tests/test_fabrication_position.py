"""Center CPL positions on soldered pads, not on holes or paste-only apertures."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.fabrication_test_support import Point, modules as fabrication_modules
from tests.wx_harness import load_siblings

modules = fabrication_modules

PTH, SMD, NPTH = 0, 1, 3


@dataclass
class Box:
    """Mirror BOX2I's in-place Merge and its center."""

    left: float
    top: float
    right: float
    bottom: float

    def Merge(self, other: Box) -> Box:
        """Grow to cover another box."""
        self.left = min(self.left, other.left)
        self.top = min(self.top, other.top)
        self.right = max(self.right, other.right)
        self.bottom = max(self.bottom, other.bottom)
        return self

    def GetCenter(self) -> Point:
        """Return the middle of the merged area."""
        return Point((self.left + self.right) / 2, (self.top + self.bottom) / 2)


def make_pad(attribute: int, on_copper: bool, x: float, y: float) -> SimpleNamespace:
    """Expose the pad data the position calculation reads, as a 1 by 1 pad."""
    return SimpleNamespace(
        GetAttribute=lambda: attribute,
        IsOnCopperLayer=lambda: on_copper,
        GetBoundingBox=lambda: Box(x - 0.5, y - 0.5, x + 0.5, y + 0.5),
    )


def make_footprint(pads: list[SimpleNamespace]) -> SimpleNamespace:
    """Place the footprint origin away from every pad."""
    return SimpleNamespace(
        GetReference=lambda: "J1",
        Pads=lambda: pads,
        GetPosition=lambda: Point(100, 100),
    )


def make_fabrication(modules: SimpleNamespace, tmp_path: Path) -> Any:
    """Create a real generator whose output folder is temporary."""
    board = SimpleNamespace(GetFileName=lambda: str(tmp_path / "board.kicad_pcb"))
    return modules.fabrication.Fabrication(SimpleNamespace(settings={}), board)


SOLDERED = [make_pad(PTH, True, 2, 4), make_pad(SMD, True, 6, 4)]


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param(make_pad(NPTH, True, 4, -20), id="npth-hole"),
        pytest.param(make_pad(SMD, False, 30, 4), id="paste-only"),
    ],
)
def test_position_ignores_pads_that_are_not_soldered(
    modules: SimpleNamespace, tmp_path: Path, extra: SimpleNamespace
) -> None:
    """A hole or paste aperture outside the copper must not move Mid X/Y."""
    fabrication = make_fabrication(modules, tmp_path)

    position = fabrication.get_position(make_footprint([*SOLDERED, extra]))

    assert position == Point(4, 4)


def test_position_falls_back_without_soldered_pads(
    modules: SimpleNamespace, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A mounting hole keeps its own position, quietly: every geometry check asks."""
    fabrication = make_fabrication(modules, tmp_path)
    footprint = make_footprint(
        [make_pad(NPTH, True, 4, -20), make_pad(SMD, False, 30, 4)]
    )

    with caplog.at_level(logging.DEBUG):
        assert fabrication.get_position(footprint) == Point(100, 100)
    assert not caplog.records


@pytest.mark.native_kicad
def test_native_position_ignores_npth_and_paste_only_pads(tmp_path: Path) -> None:
    """Real KiCad pads: an NPTH hole with a copper ring and a paste-only pad."""
    pcbnew = pytest.importorskip("pcbnew", reason="KiCad pcbnew is unavailable")
    with load_siblings("_fabrication_position_native_test", ("fabrication",), {}) as (
        loaded
    ):
        board = pcbnew.BOARD()
        board.SetFileName(str(tmp_path / "board.kicad_pcb"))
        footprint = pcbnew.FOOTPRINT(board)
        footprint.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(50), pcbnew.FromMM(60)))
        board.Add(footprint)
        paste = pcbnew.LSET()
        paste.AddLayer(pcbnew.F_Paste)
        for attribute, layers, x, y in (
            (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD.PTHMask(), 0, 0),
            (pcbnew.PAD_ATTRIB_SMD, pcbnew.PAD.SMDMask(), 4, 2),
            (pcbnew.PAD_ATTRIB_NPTH, pcbnew.PAD.PTHMask(), 2, -10),
            (pcbnew.PAD_ATTRIB_SMD, paste, 12, 1),
        ):
            pad = pcbnew.PAD(footprint)
            pad.SetAttribute(attribute)
            pad.SetLayerSet(layers)
            pad.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(1.5), pcbnew.FromMM(1.5)))
            if attribute != pcbnew.PAD_ATTRIB_SMD:
                pad.SetDrillSize(pcbnew.VECTOR2I(pcbnew.FromMM(1), pcbnew.FromMM(1)))
            pad.SetFPRelativePosition(
                pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))
            )
            footprint.Add(pad)
        fabrication = loaded["fabrication"].Fabrication(
            SimpleNamespace(settings={}), board
        )

        position = fabrication.get_position(footprint)

    assert (pcbnew.ToMM(position.x), pcbnew.ToMM(position.y)) == (52, 61)
