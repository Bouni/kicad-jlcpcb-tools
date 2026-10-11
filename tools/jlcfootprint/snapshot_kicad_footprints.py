"""Record KiCad library footprints' pads as test fixtures (dev-time only).

Usage:
    python3 tools/jlcfootprint/snapshot_kicad_footprints.py Package_TO_SOT_SMD:SOT-23 Diode_SMD:D_SMA [...]
    python3 tools/jlcfootprint/snapshot_kicad_footprints.py --all

Each ``Library:Name`` is read from the KiCad footprint libraries installed on this
machine with the package's own parser (``jlcfootprint.boardfile``: the pads and
the courtyard box) and written to
``tests/fixtures/jlcfootprint/kicad/Library__Name.json``, so the library-backed
tests run where KiCad is not installed.  ``--all`` re-records every existing
snapshot, which is how a KiCad library update is picked up.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.jlcfootprint.kicad_library import (  # noqa: E402
    KICAD_FOOTPRINTS,
    KICAD_SNAPSHOTS,
    installed_library_footprint,
    snapshot_path,
)

VERSION_RE = re.compile(r"\(version\s+(\d+)\)")


def library_version(library: str, name: str) -> str:
    """Return the ``(version YYYYMMDD)`` token from the footprint file, or ``""``."""
    text = (KICAD_FOOTPRINTS / f"{library}.pretty" / f"{name}.kicad_mod").read_text(
        encoding="utf-8"
    )
    match = VERSION_RE.search(text)
    return match.group(1) if match else ""


def record(library: str, name: str) -> Path:
    """Write one snapshot and return its path."""
    pads, courtyard = installed_library_footprint(library, name)
    payload = {
        "library": library,
        "name": name,
        "version": library_version(library, name),
        "pads": [pad._asdict() for pad in pads],
        "courtyard": None if courtyard is None else list(courtyard),
    }
    path = snapshot_path(library, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    return path


def existing() -> list[str]:
    """Return ``Library:Name`` for every snapshot already recorded."""
    return [
        f"{data['library']}:{data['name']}"
        for data in (
            json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(KICAD_SNAPSHOTS.glob("*.json"))
        )
    ]


def main(argv: Optional[list[str]] = None) -> int:
    """Run the recorder from the command line."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("footprints", nargs="*", help="Library:Name pairs")
    parser.add_argument(
        "--all", action="store_true", help="re-record every existing snapshot"
    )
    args = parser.parse_args(argv)
    if not KICAD_FOOTPRINTS.is_dir():
        print(f"no KiCad footprint libraries at {KICAD_FOOTPRINTS}")
        return 1
    wanted = list(args.footprints) + (existing() if args.all else [])
    if not wanted:
        parser.error("give Library:Name pairs or --all")
    for item in dict.fromkeys(wanted):
        library, _, name = item.partition(":")
        if not name:
            parser.error(f"expected Library:Name, got {item!r}")
        path = record(library, name)
        print(f"{item}: {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
