"""The DRC counter returns KiCad's error violations with their messages."""

from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from .wx_harness import load_siblings

REPORT = """** Drc report for board.kicad_pcb **
** Created on 2026-10-10T09:00:00+0000 **

** Found 3 DRC violations **
[clearance]: Clearance violation (netclass 'Default' clearance 0.2000 mm; actual 0.1500 mm)
    Rule: netclass 'Default'; error
    @(10.0000 mm, 20.0000 mm): Track [GND] on F.Cu, length 5.0000 mm
    @(10.1500 mm, 20.0000 mm): Pad 1 [+3V3] of C3 on F.Cu
[silk_overlap]: Silkscreen overlap
    Rule: board setup constraints silk; warning
    @(12.0000 mm, 21.0000 mm): Reference field of R4 on F.Silkscreen
[courtyards_overlap]: Courtyards overlap
    Local override; error
    @(30.0000 mm, 40.0000 mm): Footprint U2
    @(31.0000 mm, 40.0000 mm): Footprint C9

** Found 0 unconnected pads **

** End of Report **
"""


@pytest.fixture
def kicad_drc() -> Iterator[ModuleType]:
    """Load the real DRC helpers as a plugin sibling."""
    with load_siblings("kicad_drc_tests", ("kicad_drc",), {}) as loaded:
        yield loaded["kicad_drc"]


def _pcbnew(report: str, written: list[Path], *, succeeds: bool = True) -> Any:
    """Write the given report text wherever KiCad is asked to write one."""
    board = SimpleNamespace(GetFileName=lambda: "board.kicad_pcb")

    def write_report(_board: Any, path: str, _units: int, _all: bool) -> bool:
        written.append(Path(path))
        Path(path).write_text(report, encoding="utf-8")
        return succeeds

    return SimpleNamespace(
        GetBoard=lambda: board, EDA_UNITS_MM=1, WriteDRCReport=write_report
    )


def test_error_violations_return_count_and_messages(
    kicad_drc: ModuleType, tmp_path: Path
) -> None:
    """Only error-severity blocks count, each with its code and message."""
    written: list[Path] = []
    counter = kicad_drc.DRCViolationCounter(_pcbnew(REPORT, written), str(tmp_path))

    assert counter.get_error_violations("board.kicad_pcb") == (
        2,
        [
            "[clearance]: Clearance violation (netclass 'Default' clearance "
            "0.2000 mm; actual 0.1500 mm)",
            "[courtyards_overlap]: Courtyards overlap",
        ],
    )
    assert written and not any(path.exists() for path in written)


def test_clean_report_returns_no_violations(
    kicad_drc: ModuleType, tmp_path: Path
) -> None:
    """A report with no violations returns a zero count and no messages."""
    report = "** Found 0 DRC violations **\n\n** End of Report **\n"
    counter = kicad_drc.DRCViolationCounter(_pcbnew(report, []), str(tmp_path))

    assert counter.get_error_violations("board.kicad_pcb") == (0, [])


def test_report_without_severities_counts_every_violation(
    kicad_drc: ModuleType, tmp_path: Path
) -> None:
    """Without per-item severity the total is returned, with no messages."""
    report = "** Found 4 DRC violations **\n\n** End of Report **\n"
    counter = kicad_drc.DRCViolationCounter(_pcbnew(report, []), str(tmp_path))

    assert counter.get_error_violations("board.kicad_pcb") == (4, [])


def test_failed_report_raises_and_removes_the_report(
    kicad_drc: ModuleType, tmp_path: Path
) -> None:
    """A KiCad failure raises instead of returning a count."""
    written: list[Path] = []
    pcbnew = _pcbnew(REPORT, written, succeeds=False)
    counter = kicad_drc.DRCViolationCounter(pcbnew, str(tmp_path))

    with pytest.raises(RuntimeError, match="WriteDRCReport returned failure"):
        counter.get_error_violations("board.kicad_pcb")
    assert written and not any(path.exists() for path in written)
