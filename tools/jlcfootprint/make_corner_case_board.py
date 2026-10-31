r"""Generate a gate board from a cases CSV (run with KiCad's bundled Python).

    /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 \
        tools/jlcfootprint/make_corner_case_board.py [--cases CSV] [--blank BOARD] --out BOARD

Reads the cases CSV (tests/fixtures/jlcfootprint/boards/corner_case/cases.csv by default), loads each
footprint from KiCad's library, places the parts on a grid at the requested
rotation and side, sets the LCSC field and any pin functions, draws the outline
and writes the board to ``--out``, which has no default, so a run never overwrites the
committed tests/fixtures/jlcfootprint/boards/corner_case/corner_case.kicad_pcb by accident.
The corner-case board is the resolver's gate.  The second gate board, for pairing
by pin function, the drawings' + marks and the body caveat, is
tests/fixtures/jlcfootprint/boards/pin_functions_and_marks/, with its own cases.csv.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
import sys
from typing import Optional

import pcbnew

BOARDS = Path(__file__).resolve().parents[2] / "tests/fixtures/jlcfootprint/boards"
CASES = BOARDS / "corner_case" / "cases.csv"
BLANK = BOARDS / "corner_case" / "blank.kicad_pcb"
LIBRARY_ROOT = Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints")
COLUMNS = 6
PITCH_MM = 12.0
MARGIN_MM = 10.0


def load_cases(cases: Path = CASES) -> list[dict]:
    """Return the non-empty rows of the cases CSV."""
    with cases.open(newline="", encoding="utf-8") as handle:
        return [row for row in csv.DictReader(handle) if row["reference"].strip()]


def place(board, row: dict, index: int) -> None:
    """Load one footprint, add it to the board and configure it from its row."""
    footprint = pcbnew.FootprintLoad(
        str(LIBRARY_ROOT / f"{row['lib']}.pretty"), row["footprint"]
    )
    if footprint is None:
        raise SystemExit(f"footprint not found: {row['lib']}:{row['footprint']}")
    footprint.SetFPID(pcbnew.LIB_ID(row["lib"], row["footprint"]))
    footprint.SetReference(row["reference"])
    footprint.SetValue(row["value"])
    board.Add(footprint)  # before Flip, or pcbnew segfaults
    footprint.SetField("LCSC", row["lcsc"])
    x = MARGIN_MM + (index % COLUMNS) * PITCH_MM
    y = MARGIN_MM + (index // COLUMNS) * PITCH_MM
    footprint.SetPosition(pcbnew.VECTOR2I_MM(x, y))
    if row["layer"].strip().upper() == "B":
        footprint.Flip(footprint.GetPosition(), pcbnew.FLIP_DIRECTION_TOP_BOTTOM)
    footprint.SetOrientationDegrees(float(row["rotation"] or 0))
    functions = dict(
        item.split("=", 1) for item in row["pinfunctions"].split(";") if "=" in item
    )
    for pad in footprint.Pads():
        function = functions.get(str(pad.GetNumber()))
        if function:
            pad.SetPinFunction(function)


def outline(board, count: int) -> None:
    """Draw the Edge.Cuts rectangle around the grid."""
    rows = (count + COLUMNS - 1) // COLUMNS
    width = 2 * MARGIN_MM + (COLUMNS - 1) * PITCH_MM
    height = 2 * MARGIN_MM + (rows - 1) * PITCH_MM
    rect = pcbnew.PCB_SHAPE(board)
    rect.SetShape(pcbnew.SHAPE_T_RECT)
    rect.SetStart(pcbnew.VECTOR2I_MM(0, 0))
    rect.SetEnd(pcbnew.VECTOR2I_MM(width, height))
    rect.SetLayer(pcbnew.Edge_Cuts)
    rect.SetWidth(pcbnew.FromMM(0.1))
    board.Add(rect)


def main(argv: Optional[list[str]] = None) -> int:
    """Generate the board."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--blank", type=Path, default=BLANK)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    board = pcbnew.LoadBoard(str(args.blank))
    if board is None:
        raise SystemExit(f"could not load {args.blank}")
    cases = load_cases(args.cases)
    for index, row in enumerate(cases):
        place(board, row, index)
    outline(board, len(cases))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    pcbnew.SaveBoard(str(args.out), board)
    print(f"wrote {args.out} with {len(cases)} parts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
