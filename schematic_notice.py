"""Read saved schematic assignments through KiCad's generic XML netlist export."""

from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import NamedTuple, Optional
import xml.etree.ElementTree as ET

NOTICE_TITLE = "Schematic LCSC assignments found"
NOTICE_MESSAGE = (
    "KiCad can copy schematic assembly settings onto the PCB. "
    "kicad-jlcpcb-tools stores its manufacturing choices independently: "
    "PCB values initialize new parts only.\n\n"
    "Review the imported assignments, then remove obsolete LCSC fields using "
    "Schematic Editor and save there. The plugin cannot safely modify the live "
    "schematic on supported KiCad versions.\n\n"
    "Existing plugin choices remain protected even if you retain those fields. "
    "This check reads the saved schematic; unsaved editor changes are not included."
)


class NoticeResult(NamedTuple):
    """Keep detection failures distinct from a schematic with no assignments."""

    found: Optional[bool]
    detail: str = ""


def _find_cli(pcbnew_path: str, kicad_version: str) -> Optional[str]:
    """Prefer the running installation and require its KiCad major/minor version."""
    expected = re.search(r"\d+\.\d+", kicad_version)
    if expected is None:
        return None
    name = "kicad-cli.exe" if sys.platform == "win32" else "kicad-cli"
    candidates = []
    for anchor in (pcbnew_path, sys.executable):
        if anchor:
            for parent in Path(anchor).resolve().parents:
                candidates.extend(
                    parent / folder / name for folder in ("", "bin", "MacOS")
                )
    on_path = shutil.which(name)
    if on_path:
        candidates.append(Path(on_path))
    for candidate in dict.fromkeys(candidates):
        if not candidate.is_file():
            continue
        try:
            result = subprocess.run(
                [str(candidate), "version"],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            continue
        actual = re.search(r"\d+\.\d+", result.stdout)
        if actual and actual.group() == expected.group():
            return str(candidate)
    return None


def scan_schematic_lcsc(
    board_path: str, pcbnew_path: str = "", kicad_version: str = ""
) -> NoticeResult:
    """Inspect a known project root; call from a worker after committing imports.

    SWIG does not expose the project's filename. Require the board's same-name
    project and schematic rather than guessing another root in its directory.
    KiCad handles hierarchy, excluded components and field serialization.
    """
    board = Path(board_path)
    if not board_path or not board.is_file():
        return NoticeResult(
            None, "The PCB must be saved before checking its schematic."
        )
    schematic = board.with_suffix(".kicad_sch")
    if not schematic.is_file() or not any(
        board.with_suffix(suffix).is_file() for suffix in (".kicad_pro", ".pro")
    ):
        return NoticeResult(None, "Cannot identify the saved project schematic.")
    cli = _find_cli(pcbnew_path, kicad_version)
    if cli is None:
        return NoticeResult(None, "No matching KiCad CLI is available.")
    try:
        with tempfile.TemporaryDirectory(prefix="kicad-jlcpcb-notice-") as directory:
            output = Path(directory) / "netlist.xml"
            subprocess.run(
                [
                    cli,
                    "sch",
                    "export",
                    "netlist",
                    "--format",
                    "kicadxml",
                    "--output",
                    str(output),
                    str(schematic),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            # This XML was generated locally by the matching KiCad executable.
            root = ET.parse(output).getroot()  # noqa: S314
            if root.tag != "export" or root.find("components") is None:
                return NoticeResult(None, "KiCad returned an unrecognized netlist.")
            found = any(
                re.match(r"lcsc|jlc", field.get("name", ""), re.IGNORECASE)
                and re.fullmatch(r"C\d+", field.text or "")
                for field in root.findall("./components/comp/fields/field")
            )
            return NoticeResult(found)
    except (OSError, subprocess.SubprocessError, ET.ParseError) as error:
        return NoticeResult(None, f"Schematic LCSC check unavailable: {error}")
