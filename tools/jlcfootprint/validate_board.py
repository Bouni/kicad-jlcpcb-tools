"""Validate the resolver against a board and recorded EasyEDA responses.

Usage:
    python3 tools/jlcfootprint/validate_board.py BOARD.kicad_pcb [--fixtures DIR] [--truth truth.csv]
                                      [--flip-y] [--report out.txt] [--pro-fixtures DIR]

Without --truth it prints one verdict per part that has an LCSC field.  With
--truth it compares the CPL rotation the resolver would emit against the
rotation JLC's preview settled on, and exits 1 on any disagreement: this is the
gate.  With --pro-fixtures the parts are read the way EasyEDA's Pro host serves
them: the recorded batch answers ``devices_*.json`` give each part's uuids,
``footprint_<uuid>.json`` its pads and ``symbol_<uuid>.json`` its pins; the
classic fixtures are not consulted.

truth.csv columns: reference,observed_rotation, optionally origin_dx_mm and
origin_dy_mm.  observed_rotation is JLC's final rotation for the part after it
was aligned in the placement preview.  Leave it blank for parts that must NOT
get a derived rotation (wrong picks, checkerboard parts).  The origin columns
pin the package origin the resolver derives in the KiCad footprint frame, a
snapshot of its own output rather than something the preview showed, compared
within ORIGIN_TOLERANCE_MM; blank means no derived origin is expected.  Any other
column, such as a note, is not read.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import re
import sys
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jlcfootprint.boardfile import (  # noqa: E402
    KiCadFootprint,
    footprint_pads,
    parse_kicad_pcb,
)
from jlcfootprint.drawing import drawing_marks  # noqa: E402
from jlcfootprint.easyeda_parse import (  # noqa: E402
    ComponentRecord,
    DeviceHit,
    assemble_record,
    parse_component_response,
    parse_devices_response,
    parse_puuid_response,
    parse_symbol_response,
)
from jlcfootprint.geometry import (  # noqa: E402
    easyeda_pads_to_mm,
    mirror_box,
    mirror_y,
    pad_box_centre,
)
from jlcfootprint.resolver import Verdict, resolve  # noqa: E402
from jlcfootprint.resolving import resolve_record  # noqa: E402

DEFAULT_FIXTURES = ROOT / "tests" / "fixtures" / "jlcfootprint" / "easyeda"
# How far a derived origin may sit from the truth row before the gate fails.
ORIGIN_TOLERANCE_MM = 0.005
# How far an emitted rotation may sit from JLC's: float noise from a placement
# such as 179.9 degrees, and the presses converter's six significant digits.
ANGLE_TOLERANCE_DEG = 0.001


def expected_cpl_rotation(placed: float, is_bottom: bool, correction: int) -> float:
    """Reproduce Fabrication's CPL rotation: bottom parts mirror first, the correction is added after."""
    rotation = placed % 360
    if is_bottom:
        rotation = (180 - rotation) % 360
    return (rotation + correction % 360) % 360


def load_record(fixtures: Path, lcsc: str) -> Optional[ComponentRecord]:
    """Parse the recorded response for one part, or None when no fixture exists."""
    path = fixtures / f"{lcsc}.json"
    if not path.exists():
        return None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise SystemExit(
            f"{path}: not valid JSON ({error}); delete it and re-record"
        ) from error
    return parse_component_response(body, lcsc)


def load_pro_index(pro_dir: Path) -> dict[str, Optional[DeviceHit]]:
    """Return every code a recorded batch answer asked, mapped to its hit or None for a miss."""
    index: dict[str, Optional[DeviceHit]] = {}
    for path in sorted(pro_dir.glob("devices_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            codes = list(data["codes"])
            body = data["body"]
        except (ValueError, KeyError, TypeError) as error:
            raise SystemExit(
                f"{path}: not a recorded batch answer ({error})"
            ) from error
        result = parse_devices_response(body, codes)
        if result.error:
            raise SystemExit(f"{path}: {result.error}")
        for code in codes:
            index[code] = result.hits.get(code)
    return index


def load_pro_record(
    pro_dir: Path, index: dict[str, Optional[DeviceHit]], lcsc: str
) -> Optional[ComponentRecord]:
    """Assemble one part from the Pro recordings with ``assemble_record``, or None when unrecorded.

    A code the batch was asked about but did not know is ``none``.  Otherwise the
    footprint document's own status carries through, ``none`` when it answered
    nothing and ``error`` when it cannot be read, as it does for a fetched part.  A
    missing footprint or symbol file leaves the part unrecorded; a symbol that
    answered nothing leaves it without pins.
    """
    if lcsc not in index:
        return None
    hit = index[lcsc]
    if hit is None:
        return ComponentRecord(lcsc=lcsc, status="none")
    footprint_path = pro_dir / f"footprint_{hit.puuid}.json"
    if not footprint_path.exists():
        return None
    footprint = parse_puuid_response(
        json.loads(footprint_path.read_text(encoding="utf-8")), hit.puuid
    )
    symbol = None
    if hit.symbol_uuid:
        symbol_path = pro_dir / f"symbol_{hit.symbol_uuid}.json"
        if not symbol_path.exists():
            return None
        symbol = parse_symbol_response(
            json.loads(symbol_path.read_text(encoding="utf-8")), hit.symbol_uuid
        )
    return assemble_record(lcsc, hit, footprint, symbol)


def evaluate_footprints(
    footprints: list[KiCadFootprint],
    fixtures: Path,
    flip_y: Optional[bool] = None,
    pro_fixtures: Optional[Path] = None,
) -> list[dict]:
    """Resolve every footprint with an LCSC field; each row keeps the board facts and the verdict.

    ``flip_y`` None uses the plugin's own ``FLIP_EASYEDA_Y`` constant, so the gate
    tests the convention the plugin ships; ``--flip-y`` overrides it for calibration.
    ``pro_fixtures`` reads the parts from the Pro-host recordings instead.
    """
    index = load_pro_index(pro_fixtures) if pro_fixtures is not None else None
    rows: list[dict] = []
    for fp in footprints:
        if not fp.lcsc:
            continue
        row = {
            "reference": fp.reference,
            "lcsc": fp.lcsc,
            "footprint": fp.footprint_name,
            "placed": fp.placed_rotation,
            "bottom": fp.is_bottom,
            "package": "",
            "centre": None,
            "verdict": None,
        }
        if pro_fixtures is not None and index is not None:
            record = load_pro_record(pro_fixtures, index, fp.lcsc)
        else:
            record = load_record(fixtures, fp.lcsc)
        if record is not None:
            pads = footprint_pads(fp)
            courtyard = fp.courtyard
            if fp.is_bottom:
                pads = mirror_y(pads)
                courtyard = None if courtyard is None else mirror_box(courtyard)
            row["package"] = record.package_name
            row["centre"] = pad_box_centre(pads)
            if flip_y is None:
                row["verdict"] = resolve_record(
                    pads, fp.footprint_name, record, kicad_courtyard=courtyard
                )
            else:
                # resolve_record always converts with FLIP_EASYEDA_Y, so the --flip-y
                # calibration path (test_jlcfootprint_validate.py also calls it with
                # flip_y=False) builds the resolver call itself.
                row["verdict"] = resolve(
                    pads,
                    fp.footprint_name,
                    record.status,
                    record.package_name,
                    easyeda_pads_to_mm(record.pads, flip_y=flip_y),
                    record.symbol_pins,
                    marks=drawing_marks(
                        record.symbol_shapes,
                        record.footprint_shapes,
                        record.footprint_origin,
                    ),
                    kicad_courtyard=courtyard,
                )
        rows.append(row)
    return rows


def reference_key(reference: str) -> tuple:
    """Sort key that orders Q2 before Q10: letters, then the number, then any suffix."""
    match = re.match(r"^([A-Za-z_]*)(\d*)(.*)$", reference)
    letters, digits, rest = match.groups()  # the pattern matches every string
    return (letters.upper(), int(digits) if digits else -1, rest)


def evaluate(
    board: Path,
    fixtures: Path,
    flip_y: Optional[bool] = None,
    pro_fixtures: Optional[Path] = None,
) -> list[dict]:
    """Parse the board file and evaluate it, rows in reference order.

    pcbnew writes footprints in the order of their fresh internal ids, so a
    regenerated board lists the same parts in a different order; sorting keeps
    the report and the gate deterministic.
    """
    footprints = sorted(
        parse_kicad_pcb(str(board)), key=lambda fp: reference_key(fp.reference)
    )
    return evaluate_footprints(footprints, fixtures, flip_y, pro_fixtures)


def load_truth(path: Path) -> dict[str, str]:
    """Return ``{reference: observed_rotation text}`` from the truth CSV.

    Header names match case-insensitively, a UTF-8 BOM (what spreadsheets write) is
    tolerated, a missing cell reads as blank, and a missing column or a reference
    named twice stops the run: a second row would replace the first unseen.
    """
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = {name.strip().lower(): name for name in reader.fieldnames or []}
        for wanted in ("reference", "observed_rotation"):
            if wanted not in columns:
                raise SystemExit(
                    f"{path}: no '{wanted}' column (header: {reader.fieldnames})"
                )
        truth: dict[str, str] = {}
        for row in reader:
            reference = (row.get(columns["reference"]) or "").strip()
            if not reference:
                continue
            if reference in truth:
                raise SystemExit(f"{path}: {reference} has more than one row")
            truth[reference] = (row.get(columns["observed_rotation"]) or "").strip()
        return truth


def load_truth_origins(
    path: Path,
) -> Optional[dict[str, Optional[tuple[float, float]]]]:
    """Return ``{reference: (dx, dy)}`` from the truth CSV, or None without the columns.

    The two columns are optional as a pair, so a truth file without them still
    gates the rotations; an empty pair means the part must derive no origin, and a
    half-filled pair, in a row or in the header, is a mistake in the file rather
    than a silent pass.
    """
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = {name.strip().lower(): name for name in reader.fieldnames or []}
        present = [name for name in ("origin_dx_mm", "origin_dy_mm") if name in columns]
        if not present:
            return None
        if len(present) == 1:
            raise SystemExit(
                f"{path}: the origin columns come as a pair, but only {present[0]} is there"
            )
        origins: dict[str, Optional[tuple[float, float]]] = {}
        for row in reader:
            reference = (row.get(columns["reference"]) or "").strip()
            if not reference:
                continue
            values = [
                (row.get(columns[f"origin_d{axis}_mm"]) or "").strip()
                for axis in ("x", "y")
            ]
            if not any(values):
                origins[reference] = None
                continue
            try:
                origin = (float(values[0]), float(values[1]))
            except ValueError as error:
                raise SystemExit(
                    f"{path}: {reference} has a non-numeric origin {values}"
                ) from error
            # float() reads "nan" and "inf", and a NaN compares as within any
            # tolerance, so it would pass every origin check it took part in.
            if not all(math.isfinite(value) for value in origin):
                raise SystemExit(
                    f"{path}: {reference} has a non-finite origin {values}"
                )
            origins[reference] = origin
        return origins


def origin_offset(row: dict) -> Optional[tuple[float, float]]:
    """Return the derived origin's offset from the part's pad-box centre, or None.

    This is how far a CPL position would move from the pad-box centre, which
    ``Fabrication.get_position`` writes (the same pad box in board coordinates),
    to JLC's package origin.
    """
    verdict: Optional[Verdict] = row["verdict"]
    origin = None if verdict is None else verdict.origin
    centre = row.get("centre")
    if origin is None or centre is None:
        return None
    return (origin[0] - centre[0], origin[1] - centre[1])


def compare(
    rows: list[dict],
    truth: dict[str, str],
    origins: Optional[dict[str, Optional[tuple[float, float]]]] = None,
) -> list[tuple[dict, str]]:
    """Return ``(row, reason)`` for every part whose emitted rotation would disagree with JLC.

    A truth reference that is not on the board is reported too, so a typo in
    ``truth.csv`` cannot silently pass the gate.
    """
    failures: list[tuple[dict, str]] = []
    on_board = {row["reference"] for row in rows}
    for reference in sorted(set(truth) - on_board, key=reference_key):
        placeholder = {
            "reference": reference,
            "lcsc": "",
            "footprint": "",
            "placed": 0.0,
            "bottom": False,
            "package": "",
            "centre": None,
            "verdict": None,
        }
        failures.append((placeholder, "not on the board (typo in truth.csv?)"))
    for row in rows:
        if row["reference"] not in truth:
            continue
        observed = truth[row["reference"]]
        verdict: Optional[Verdict] = row["verdict"]
        if origins is not None and row["reference"] in origins:
            reason = _origin_reason(
                origins[row["reference"]],
                None if verdict is None else verdict.origin,
            )
            if reason:
                failures.append((row, reason))
        if verdict is None:
            failures.append((row, "no recorded EasyEDA response"))
            continue
        if observed == "":
            if verdict.rotation is not None:
                failures.append(
                    (row, f"expected no derived rotation, got {verdict.rotation}")
                )
            continue
        if verdict.rotation is None:
            failures.append(
                (row, f"no derived rotation ({verdict.status}); JLC shows {observed}")
            )
            continue
        try:
            observed_angle = float(observed)
        except ValueError:
            observed_angle = math.nan
        if not math.isfinite(observed_angle):
            # float() reads "nan" and "inf", which no tolerance comparison can fail.
            failures.append((row, f"truth value {observed!r} is not a number"))
            continue
        observed_angle %= 360
        expected = expected_cpl_rotation(row["placed"], row["bottom"], verdict.rotation)
        # A part aligned by its axis, or a marked inductor whose token only places
        # JLC's cosmetic pin-1 dot, has no polarity to get wrong, so it is compared
        # modulo 180 degrees.  A part aligned by pad shape is non-polar too, but its
        # pads fit one way round only, so a half turn is a misplacement.
        modulus = 180 if verdict.non_polar and verdict.method != "shape" else 360
        apart = (expected - observed_angle) % modulus
        if min(apart, modulus - apart) > ANGLE_TOLERANCE_DEG:
            failures.append((row, f"would emit {expected:g}, JLC shows {observed}"))
    return failures


def _origin_reason(
    expected: Optional[tuple[float, float]], derived: Optional[tuple[float, float]]
) -> str:
    """Return why a derived origin disagrees with its truth row, or "" when it agrees."""
    if expected is None:
        return (
            ""
            if derived is None
            else f"expected no derived origin, got ({derived[0]:.3f}, {derived[1]:.3f})"
        )
    if derived is None:
        return f"no derived origin; truth says ({expected[0]:.3f}, {expected[1]:.3f})"
    if any(abs(a - b) > ORIGIN_TOLERANCE_MM for a, b in zip(derived, expected)):
        return (
            f"origin ({derived[0]:.3f}, {derived[1]:.3f}) differs from truth "
            f"({expected[0]:.3f}, {expected[1]:.3f}) by more than "
            f"{ORIGIN_TOLERANCE_MM} mm"
        )
    return ""


def format_rows(rows: list[dict]) -> str:
    """Render one line per part."""
    header = (
        f"{'ref':<6} {'lcsc':<10} {'status':<8} {'fit':<17} {'rot':>4} {'name':>4} "
        f"{'orgdx':>6} {'orgdy':>6}  footprint / package / notes"
    )
    lines = [header]
    for row in rows:
        verdict = row["verdict"]
        if verdict is None:
            lines.append(
                f"{row['reference']:<6} {row['lcsc']:<10} {'no-fix':<8} {'':<17} {'':>4} {'':>4} "
                f"{'':>6} {'':>6}  {row['footprint']}"
            )
            continue
        rot = "" if verdict.rotation is None else str(verdict.rotation)
        name = "" if verdict.name_rotation is None else str(verdict.name_rotation)
        offset = origin_offset(row)
        # The origin as the preview sees it: how far it sits from the pad-box centre
        # Fabrication writes to the CPL, two decimals, blank without one.
        # -0.0 normalised to 0.0, so a sign of rounding noise cannot change the file.
        dx, dy = (
            ("", "")
            if offset is None
            else tuple(f"{round(value, 2) + 0.0:.2f}" for value in offset)
        )
        lines.append(
            f"{row['reference']:<6} {row['lcsc']:<10} {verdict.status:<8} {verdict.fit:<17} "
            f"{rot:>4} {name:>4} {dx:>6} {dy:>6}  {row['footprint']} / {row['package']} / {verdict.note_text}"
        )
    # A part with no notes would end its line in "/ "; no line keeps trailing space.
    return "\n".join(line.rstrip() for line in lines)


def main(argv: Optional[list[str]] = None) -> int:
    """Run the validator from the command line."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("board", type=Path)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--truth", type=Path)
    parser.add_argument(
        "--flip-y",
        action="store_true",
        help="flip EasyEDA Y before matching, to check the gate against the other Y convention",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="also write the table (without the truth summary) to this file",
    )
    parser.add_argument(
        "--pro-fixtures",
        type=Path,
        help="read the parts from Pro-host recordings (devices_*, footprint_*, symbol_*) instead",
    )
    args = parser.parse_args(argv)
    rows = evaluate(
        args.board, args.fixtures, True if args.flip_y else None, args.pro_fixtures
    )
    table = format_rows(rows)
    print(table)
    if args.report:
        args.report.write_text(table + "\n", encoding="utf-8")
    if not args.truth:
        return 0
    truth = load_truth(args.truth)
    failures = compare(rows, truth, load_truth_origins(args.truth))
    checked = sum(1 for row in rows if row["reference"] in truth)
    print(f"\n{checked} parts checked against JLC, {len(failures)} disagree")
    for row, reason in failures:
        side = " bottom" if row["bottom"] else ""
        print(
            f"  {row['reference']:<6} {row['lcsc']:<10} placed {row['placed']:g}{side}: {reason}"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
