"""Keep the parts table and actual placement output on one matching policy."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

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


@pytest.mark.parametrize(
    ("records", "part_rules", "lcsc", "rotation", "offset", "source"),
    [
        pytest.param(
            [("SOT-23-3", 180, (1, 1))],
            [("C12345", 90, (0.5, -0.5))],
            "C12345",
            90,
            (0.5, -0.5),
            "lcsc",
            id="part-rule-beats-its-footprint-family",
        ),
        pytest.param(
            [("SOT-23-3", 180, (1, 1))],
            [("C12345", 0, (0, 0))],
            "C12345",
            0,
            (0, 0),
            "lcsc",
            id="zero-part-rule-switches-the-family-off",
        ),
        pytest.param(
            [("SOT-23-3", 180, (1, 1))],
            [("C12345", 0, (0, 0))],
            "C99999",
            180,
            (1, 1),
            "fpt",
            id="sibling-on-the-same-footprint-keeps-the-family",
        ),
        pytest.param(
            [("SOT-23-3", 180, (1, 1))],
            [("C12345", 0, (0, 0))],
            "",
            180,
            (1, 1),
            "fpt",
            id="unassigned-part-falls-through",
        ),
        pytest.param(
            [("U1", 45, (0, 0)), ("Device", 135, (0, 0))],
            [("C12345", 90, (0, 0))],
            "C12345",
            90,
            (0, 0),
            "lcsc",
            id="part-rule-beats-reference-and-value",
        ),
        pytest.param(
            [],
            [("C12345", 90, (0, 0))],
            "c12345",
            90,
            (0, 0),
            "lcsc",
            id="lower-case-store-value-still-matches",
        ),
        pytest.param(
            [],
            [("C12345", 90, (0, 0))],
            "C1234",
            0,
            (0, 0),
            None,
            id="exact-not-prefix",
        ),
    ],
)
def test_part_rules_share_the_display_and_cpl_policy(
    runtime: SimpleNamespace,
    tmp_path: Path,
    records: list[tuple[str, int, tuple[float, float]]],
    part_rules: list[tuple[str, int, tuple[float, float]]],
    lcsc: str,
    rotation: int,
    offset: tuple[float, float],
    source: Any,
) -> None:
    """A rule for the exact part is what the table shows and the CPL applies.

    Two parts on one footprint name are the case no pattern can express: the
    overridden part gets its own rotation while its sibling keeps the family's.
    """
    runtime.library.apply_corrections(records)
    for key, degrees, part_offset in part_rules:
        runtime.library.insert_lcsc_correction_data(key, degrees, part_offset)
    fabrication = make_fabrication(runtime.modules, runtime.library, tmp_path)
    fp = make_footprint("U1", 0, 0, Point(10, 20))
    fp.GetFPID = lambda: SimpleNamespace(GetLibItemName=lambda: "SOT-23-3")
    fabrication.board.Footprints.return_value = [fp]
    part = {
        "reference": "U1",
        "value": "Device",
        "footprint": "SOT-23-3",
        "exclude_from_bom": 0,
        "exclude_from_pos": 0,
        "lcsc": lcsc,
    }
    fabrication.parent.store.get_part = lambda _reference: part
    window = _population_window(runtime)
    # The parts list looks up stock and type for an assigned number; there is
    # no parts database here, and that lookup is not what is under test.
    window.library.get_part_details = lambda _lcsc: {}
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


def test_cpl_resolves_the_stored_part_number_not_the_footprint_field(
    runtime: SimpleNamespace, tmp_path: Path
) -> None:
    """The BOM orders the store's number, so the CPL must rotate for that one.

    "Paste LCSC" and "Find LCSC from Mappings" write the store without touching
    the footprint field, so a stale field would rotate one part while the BOM
    ordered another -- the silent wrong rotation part rules exist to prevent.
    """
    runtime.library.insert_lcsc_correction_data("C111", 180, (0, 0))
    runtime.library.insert_lcsc_correction_data("C222", 90, (0, 0))
    fabrication = make_fabrication(runtime.modules, runtime.library, tmp_path)
    fp = make_footprint("U1", 0, 0, Point(10, 20))
    fp.GetFields = MagicMock(
        return_value=[SimpleNamespace(GetName=lambda: "LCSC", GetText=lambda: "C111")]
    )
    fp.GetProperties = MagicMock(return_value={"LCSC": "C111"})
    fabrication.board.Footprints.return_value = [fp]
    part = {
        "reference": "U1",
        "value": "Device",
        "footprint": "Package:Device",
        "exclude_from_pos": 0,
        "lcsc": "C222",
    }
    fabrication.parent.store.get_part = lambda _reference: part

    fabrication.generate_cpl()

    (row,) = read_cpl(fabrication)
    assert float(row["Rotation"]) == 90
    fp.GetFields.assert_not_called()
    fp.GetProperties.assert_not_called()


def test_public_wrappers_take_the_part_number(
    runtime: SimpleNamespace, tmp_path: Path
) -> None:
    """fix_rotation and fix_position resolve the part only when given its number."""
    runtime.library.insert_lcsc_correction_data("C12345", 90, (1, 0))
    fabrication = make_fabrication(runtime.modules, runtime.library, tmp_path)
    fabrication.corrections = runtime.library.get_all_correction_data()
    fp = make_footprint("U1", 0, 0, Point(10, 20))

    assert fabrication.fix_rotation(fp, "C12345") == 90
    assert fabrication.fix_rotation(fp) == 0
    assert fabrication.fix_position(fp, Point(10, 20), "C12345") == Point(11, 20)
    assert fabrication.fix_position(fp, Point(10, 20)) == Point(10, 20)
