"""Turn a placement-preview log of space-bar presses into the gate's truth.csv.

Usage:
    python3 tools/jlcfootprint/truth_from_presses.py PRESSES.csv CPL.csv --out TRUTH.csv

JLC's placement preview rotates a part 90 degrees counter-clockwise per press of the
space bar and does not display the resulting angle, so the natural thing to write
down is the number of degrees pressed per part.  The angle JLC settled on is the
uploaded CPL rotation plus the presses, modulo 360.  PRESSES.csv rows are
``reference,degrees_pressed[,note]``; lines starting with ``#`` are comments; a row
with a blank second cell means "must not derive" (wrong picks, checkerboards).
The output is ``reference,observed_rotation,note``, one row per PRESSES.csv row in
the same order, which is what ``validate_board.py --truth`` reads.  ``--out`` has no
default, so a run never overwrites a committed truth file by accident.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
import sys
from typing import Optional


def read_cpl(path: Path) -> dict[str, float]:
    """Return ``{designator: uploaded rotation}`` from the plugin's CPL."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = {name.strip().lower(): name for name in reader.fieldnames or []}
        ref = columns.get("designator") or columns.get("reference")
        rot = columns.get("rotation")
        if not ref or not rot:
            raise SystemExit(
                f"{path}: expected Designator and Rotation columns, got {reader.fieldnames}"
            )
        return {row[ref].strip(): float(row[rot]) for row in reader if row[ref].strip()}


def read_presses(path: Path) -> list[tuple[str, str, str]]:
    """Return ``(reference, degrees text, note)`` rows, skipping comments and blanks."""
    rows: list[tuple[str, str, str]] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for cells in csv.reader(handle):
            if not cells or not cells[0].strip() or cells[0].lstrip().startswith("#"):
                continue
            reference = cells[0].strip()
            if reference.lower() == "reference":
                continue
            degrees = cells[1].strip() if len(cells) > 1 else ""
            note = cells[2].strip() if len(cells) > 2 else ""
            rows.append((reference, degrees, note))
    return rows


def main(argv: Optional[list[str]] = None) -> int:
    """Write truth.csv from the presses log and the uploaded CPL."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("presses", type=Path)
    parser.add_argument("cpl", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    uploaded = read_cpl(args.cpl)
    rows = read_presses(args.presses)
    missing = [reference for reference, _, _ in rows if reference not in uploaded]
    if missing:
        raise SystemExit(f"not in the CPL: {' '.join(missing)}")
    with args.out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["reference", "observed_rotation", "note"])
        for reference, degrees, note in rows:
            if degrees == "":
                writer.writerow([reference, "", note])
                continue
            try:
                pressed = float(degrees)
            except ValueError:
                raise SystemExit(
                    f"{reference}: presses {degrees!r} is not a number"
                ) from None
            observed = (uploaded[reference] + pressed) % 360
            writer.writerow([reference, f"{observed:g}", note])
            print(
                f"{reference:<6} uploaded {uploaded[reference]:>5g} + pressed {pressed:>4g} = {observed:g}"
            )
    print(f"wrote {args.out} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
