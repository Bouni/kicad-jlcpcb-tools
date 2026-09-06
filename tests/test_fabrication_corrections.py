"""Keep the parts table and actual placement output on one matching policy."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.test_fabrication_correction_recovery import (
    Point,
    make_fabrication,
    make_footprint,
    read_cpl,
)
from tests.test_mainwindow_correction_recovery import (
    _displayed_corrections,
    _population_window,
    runtime as correction_runtime,
)

runtime = correction_runtime


@pytest.mark.parametrize(
    "records,reference,value,footprint,rotation,offset,source",
    [
        (
            [("SOT-23", 10, (0, 0)), ("SOT-23-3", 20, (1, 2))],
            "U1",
            "Device",
            "SOT-23-3",
            20,
            (1, 2),
            "fpt",
        ),
        (
            [("SOT-23-3", 20, (1, 2)), ("SOT-23", 10, (0, 0))],
            "U1",
            "Device",
            "SOT-23-3",
            20,
            (1, 2),
            "fpt",
        ),
        (
            [("U", 10, (1, 2)), ("U1", 20, (3, 4))],
            "U1",
            "Device",
            "SOT-23-3",
            20,
            (3, 4),
            "ref",
        ),
        (
            [("U", 10, (1, 2)), ("Device", 20, (3, 4))],
            "U1",
            "Device",
            "SOT-23-3",
            10,
            (1, 2),
            "ref",
        ),
        (
            [("Dev", 10, (1, 2)), ("Device", 20, (3, 4))],
            "U1",
            "Device",
            "SOT-23-3",
            20,
            (3, 4),
            "val",
        ),
        (
            [("Dev", 10, (1, 2)), ("SOT-23-3", 20, (3, 4))],
            "U1",
            "Device",
            "SOT-23-3",
            10,
            (1, 2),
            "val",
        ),
        (
            [("U[0-9]", 10, (1, 2)), ("U.", 20, (3, 4))],
            "U1",
            "Device",
            "SOT-23-3",
            20,
            (3, 4),
            "ref",
        ),
        (
            [("SOT-23-3|SOT-23-5", 10, (1, 2)), ("SOT-23-30", 20, (3, 4))],
            "U1",
            "Device",
            "SOT-23-30",
            20,
            (3, 4),
            "fpt",
        ),
        (
            [("SOT-23-3|SOT-23-5", 10, (1, 2))],
            "U1",
            "Device",
            "Package:SOT-23-5-long",
            10,
            (1, 2),
            "fpt",
        ),
        (
            [("U1", 0, (0, 0)), ("Device", 20, (3, 4))],
            "U1",
            "Device",
            "SOT-23-3",
            0,
            (0, 0),
            "ref",
        ),
        (
            [("U1", 0, (1, 2)), ("Device", 20, (3, 4))],
            "U1",
            "Device",
            "SOT-23-3",
            0,
            (1, 2),
            "ref",
        ),
        (
            [("U1", -90, (0, 0)), ("Device", 20, (3, 4))],
            "U1",
            "Device",
            "SOT-23-3",
            -90,
            (0, 0),
            "ref",
        ),
        ([("QFP", 10, (1, 2))], "U1", "Device", "SOT-23-3", 0, (0, 0), None),
        ([], "U1", "Device", "SOT-23-3", 0, (0, 0), None),
    ],
)
def test_parts_table_reports_the_correction_used_by_cpl(
    runtime: SimpleNamespace,
    tmp_path: Path,
    records: list[tuple[str, int, tuple[float, float]]],
    reference: str,
    value: str,
    footprint: str,
    rotation: int,
    offset: tuple[float, float],
    source: Any,
) -> None:
    """Persisted overlap rules produce matching display, rotation and position."""
    runtime.library.apply_corrections(records)
    fabrication = make_fabrication(runtime.modules, runtime.library, tmp_path)
    fp = make_footprint(reference, 0, 0, Point(10, 20))
    fp.GetValue = lambda: value
    fp.GetFPID = lambda: SimpleNamespace(GetLibItemName=lambda: footprint)
    fabrication.board.Footprints.return_value = [fp]
    part = {
        "reference": reference,
        "value": value,
        "footprint": footprint,
        "exclude_from_bom": 0,
        "exclude_from_pos": 0,
        "lcsc": "",
    }
    fabrication.parent.store.get_part = lambda _reference: part
    window = _population_window(runtime)
    window.store.read_all.return_value = [part]
    window.pcbnew = SimpleNamespace(
        GetBoard=lambda: SimpleNamespace(FindFootprintByReference=lambda _reference: fp)
    )

    window.populate_footprint_list()
    fabrication.generate_cpl()

    expected = (
        f"{rotation}°, {float(offset[0])}/{float(offset[1])} ({source})"
        if source is not None
        else "0°, 0.0/0.0"
    )
    assert _displayed_corrections(window) == [expected]
    (row,) = read_cpl(fabrication)
    assert float(row["Rotation"]) == rotation % 360
    assert float(row["Mid X"]) == pytest.approx(9 + offset[0])
    assert float(row["Mid Y"]) == pytest.approx(-18 - offset[1])
