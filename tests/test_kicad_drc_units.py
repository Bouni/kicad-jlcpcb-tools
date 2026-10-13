"""The forced DRC passes millimetres under whichever name the bindings use."""

import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "standalone_kicad_drc", _ROOT / "kicad_drc.py"
)
assert _spec is not None and _spec.loader is not None
_kicad_drc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_kicad_drc)


def _bindings(board_filename: str, **units: Any) -> SimpleNamespace:
    """Stand in for pcbnew with an open board, a recording DRC and these units."""
    board = SimpleNamespace(GetFileName=lambda: board_filename)
    calls = []

    def write_drc_report(*args: Any) -> bool:
        calls.append(args)
        Path(args[1]).write_text("** Found 0 DRC violations **\n", encoding="utf-8")
        return True

    return SimpleNamespace(
        GetBoard=lambda: board,
        WriteDRCReport=write_drc_report,
        board=board,
        calls=calls,
        **units,
    )


@pytest.mark.parametrize(
    "units_name",
    [
        pytest.param("EDA_UNITS_MILLIMETRES", id="kicad-7-to-9.0.0"),
        pytest.param("EDA_UNITS_MM", id="kicad-9.0.1-and-later"),
    ],
)
def test_run_drc_passes_the_millimetre_unit_the_bindings_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, units_name: str
) -> None:
    """Each KiCad gets its own millimetre value, and the log names it."""
    millimetres = object()
    board_filename = str(tmp_path / "board.kicad_pcb")
    report = str(tmp_path / "drc.rpt")
    pcbnew = _bindings(board_filename, **{units_name: millimetres})
    caplog.set_level(logging.INFO, logger=_kicad_drc.logger.name)

    assert _kicad_drc.run_drc(pcbnew, board_filename, report) is True

    assert pcbnew.calls == [(pcbnew.board, report, millimetres, False)]
    assert f"units={units_name}," in caplog.text


def test_drc_without_a_millimetre_unit_fails_before_running(tmp_path: Path) -> None:
    """Bindings with neither name give a clear error, not an AttributeError."""
    board_filename = str(tmp_path / "board.kicad_pcb")
    pcbnew = _bindings(board_filename)
    counter = _kicad_drc.DRCViolationCounter(
        pcbnew_module=pcbnew, working_dir=str(tmp_path)
    )

    with pytest.raises(
        RuntimeError, match="neither EDA_UNITS_MM nor EDA_UNITS_MILLIMETRES"
    ):
        counter.get_error_violations(board_filename)

    assert pcbnew.calls == []
    assert list(tmp_path.glob("*.rpt")) == []
