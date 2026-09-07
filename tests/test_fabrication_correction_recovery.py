"""Protect real CPL output from invalid storage and stale correction snapshots."""

from collections.abc import Iterator
from contextlib import closing
import csv
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.correction_test_support import (
    fresh_library,
    make_library,
    raw_rows,
    seed_raw,
)
from tests.wx_harness import load_correction_modules, module


@dataclass
class Point:
    """Represent KiCad coordinates without rounding away offset assertions."""

    x: float
    y: float

    def __sub__(self, other):
        """Subtract the board auxiliary origin."""
        return Point(self.x - other.x, self.y - other.y)


@pytest.fixture
def modules() -> Iterator[SimpleNamespace]:
    """Keep real storage and placement modules registered with one set of doubles."""
    package = "fabrication_correction_recovery_tests"
    pcbnew = MagicMock()
    pcbnew.FromMM = lambda value: value
    pcbnew.ToMM = lambda value: value
    pcbnew.wxPoint = Point
    pcbnew.VECTOR2I = Point
    with load_correction_modules(
        package=package,
        pcbnew=pcbnew,
        names=("fabrication",),
        replacements={
            f"{package}.footprint_helpers": module(
                f"{package}.footprint_helpers", get_is_dnp=lambda _footprint: False
            )
        },
    ) as loaded:
        yield loaded


@pytest.fixture
def library(modules, tmp_path):
    """Create actual SQLite correction storage away from user databases."""
    return make_library(modules.library, tmp_path)


def make_footprint(
    reference: str, layer: int, rotation: float, position: Point
) -> SimpleNamespace:
    """Expose the board data used by the production placement calculations."""
    return SimpleNamespace(
        GetReference=lambda: reference,
        GetValue=lambda: "Device",
        GetLayer=lambda: layer,
        GetOrientation=lambda: SimpleNamespace(AsDegrees=lambda: float(rotation)),
        GetFPID=lambda: SimpleNamespace(GetLibItemName=lambda: "Package:Device"),
        Pads=lambda: [],
        GetPosition=lambda: position,
    )


def make_fabrication(modules, library, tmp_path):
    """Create a real generator for one top and one bottom footprint."""
    footprints = [
        make_footprint("U1", 0, 0, Point(10, 20)),
        make_footprint("U2", 31, 90, Point(30, 40)),
    ]
    board = SimpleNamespace(
        GetFileName=lambda: str(tmp_path / "board.kicad_pcb"),
        GetDesignSettings=MagicMock(
            return_value=SimpleNamespace(GetAuxOrigin=lambda: Point(1, 2))
        ),
        Footprints=MagicMock(return_value=footprints),
    )
    parts = {
        reference: {
            "reference": reference,
            "value": "Device",
            "footprint": "Package:Device",
            "exclude_from_pos": 0,
            "lcsc": "C123",
        }
        for reference in ("U1", "U2")
    }
    parent = SimpleNamespace(
        library=library,
        settings={},
        store=SimpleNamespace(get_part=parts.get),
    )
    return modules.fabrication.Fabrication(parent, board)


def read_cpl(fabrication):
    """Read the actual generated CSV through a fresh file handle."""
    with Path(fabrication.get_cpl_csv_path()).open(newline="") as stream:
        return list(csv.DictReader(stream))


@pytest.mark.parametrize(
    ("records", "expected"),
    [
        (
            (("Device", 90, (1, 2)),),
            ((10, -20, 90), (27, -37, 180)),
        ),
        (
            (("Device", 90, (1, 2)), ("U", 0, (0, 0))),
            ((9, -18, 0), (29, -38, 90)),
        ),
        (
            (("unused", 90, (1, 2)),),
            ((9, -18, 0), (29, -38, 90)),
        ),
    ],
)
def test_cpl_resolves_each_footprint_once_for_both_transforms(
    modules: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    records: tuple[tuple[str, int, tuple[int, int]], ...],
    expected: tuple[tuple[int, int, int], ...],
) -> None:
    """One selected rule, including zero and no match, drives both placement fields."""
    fabrication = make_fabrication(modules, SimpleNamespace(), tmp_path)
    matcher = MagicMock(wraps=fabrication._correction_for_footprint)
    monkeypatch.setattr(fabrication, "_correction_for_footprint", matcher)

    fabrication.generate_cpl(tuple(modules.data.Correction(*row) for row in records))

    assert [
        tuple(float(row[field]) for field in ("Mid X", "Mid Y", "Rotation"))
        for row in read_cpl(fabrication)
    ] == list(expected)
    assert [call.args[0].GetReference() for call in matcher.call_args_list] == [
        "U1",
        "U2",
    ]


def test_cpl_skipped_footprints_do_not_resolve_corrections(
    modules: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DNP, missing parts, position exclusions and the LCSC filter skip matching."""
    fabrication = make_fabrication(modules, SimpleNamespace(), tmp_path)
    fabrication.board.Footprints.return_value = [
        make_footprint(f"U{index}", 0, 0, Point(10, 20)) for index in range(1, 7)
    ]
    parts = {
        f"U{index}": {
            "reference": f"U{index}",
            "value": "Device",
            "footprint": "Package:Device",
            "exclude_from_pos": int(index == 4),
            "lcsc": "" if index == 5 else "C123",
        }
        for index in (1, 2, 4, 5, 6)
    }
    fabrication.parent.store.get_part = parts.get
    fabrication.parent.settings = {"gerber": {"lcsc_bom_cpl": False}}
    monkeypatch.setattr(
        modules.fabrication, "get_is_dnp", lambda fp: fp.GetReference() == "U2"
    )
    matcher = MagicMock(wraps=fabrication._correction_for_footprint)
    monkeypatch.setattr(fabrication, "_correction_for_footprint", matcher)

    fabrication.generate_cpl(())

    assert [row["Designator"] for row in read_cpl(fabrication)] == ["U1", "U6"]
    assert [call.args[0].GetReference() for call in matcher.call_args_list] == [
        "U1",
        "U6",
    ]


def test_cpl_resolves_new_rules_on_each_export(
    modules: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reusing the exporter cannot retain a previous match or previous no-match."""
    fabrication = make_fabrication(modules, SimpleNamespace(), tmp_path)
    matcher = MagicMock(wraps=fabrication._correction_for_footprint)
    monkeypatch.setattr(fabrication, "_correction_for_footprint", matcher)
    snapshots = (
        (),
        (modules.data.Correction("Device", 90, (1, 2)),),
        (modules.data.Correction("Device", 180, (-1, -2)),),
        (),
    )
    expected = (
        (9, -18, 0),
        (10, -20, 90),
        (8, -16, 180),
        (9, -18, 0),
    )

    for corrections, placement in zip(snapshots, expected):
        matcher.reset_mock()
        fabrication.generate_cpl(corrections)
        row = read_cpl(fabrication)[0]
        assert (
            tuple(float(row[field]) for field in ("Mid X", "Mid Y", "Rotation"))
            == placement
        )
        assert matcher.call_count == 2


@pytest.mark.parametrize("existing_output", [False, True])
@pytest.mark.parametrize(
    "offset,x,origin,layer,angle",
    [
        ((sys.float_info.max, 0), 0, 0, 0, 0),
        ((1e12, 0), 0, 0, 0, 0),
        ((1e-6, 0), 2**31 - 1, 0, 0, 0),
        ((-1e-6, 0), -(2**31), 0, 0, 0),
        ((0, 0), 2**31 - 1, -1, 0, 0),
        ((-1e-6, 0), 2**31 - 1, 0, 31, 180),
        ((2000, 2000), 0, 0, 0, 45),
    ],
)
def test_coordinate_overflow_preserves_complete_cpl(
    modules: SimpleNamespace,
    library: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_output: bool,
    offset: tuple[float, float],
    x: int,
    origin: int,
    layer: int,
    angle: int,
) -> None:
    """A late placement error cannot truncate old output or create a partial file."""
    library.save_correction_data("U2", 0, offset)
    fabrication = make_fabrication(modules, library, tmp_path)
    fabrication.board.Footprints.return_value[1] = make_footprint(
        "U2", layer, angle, Point(x, 0)
    )
    fabrication.board.GetDesignSettings.return_value.GetAuxOrigin = lambda: Point(
        origin, 0
    )
    # KiCad's Python FromMM helper scales then converts to int; wxPoint's C++
    # coordinates are signed 32-bit. The ordinary Point double does not enforce
    # that range, so production must check before passing values to native code.
    monkeypatch.setattr(modules.fabrication, "FromMM", lambda value: int(value * 1e6))
    destination = Path(fabrication.get_cpl_csv_path())
    if existing_output:
        destination.write_bytes(b"previous complete CPL")

    with pytest.raises(ValueError, match="U2.*correction 'U2'"):
        fabrication.generate_cpl()

    assert (destination.read_bytes() if destination.exists() else None) == (
        b"previous complete CPL" if existing_output else None
    )


@pytest.mark.parametrize("coordinate", [-(2**31), 2**31 - 1])
@pytest.mark.parametrize("origin", [0, 100])
def test_cpl_accepts_coordinate_boundary_and_unused_large_offset(
    modules: SimpleNamespace,
    library: Any,
    tmp_path: Path,
    coordinate: int,
    origin: int,
) -> None:
    """Only final placements must fit, including after an origin-canceling offset."""
    library.save_correction_data("unmatched", 0, (sys.float_info.max, 0))
    library.save_correction_data("U1", 0, (origin, -origin))
    fabrication = make_fabrication(modules, library, tmp_path)
    fabrication.board.GetDesignSettings.return_value.GetAuxOrigin = lambda: Point(
        origin, -origin
    )
    fabrication.board.Footprints.return_value = [
        make_footprint("U1", 0, 0, Point(coordinate, coordinate))
    ]

    fabrication.generate_cpl()

    row = read_cpl(fabrication)[0]
    assert float(row["Mid X"]) == coordinate
    assert float(row["Mid Y"]) == -coordinate


@pytest.mark.parametrize("legacy_angle", [False, True])
@pytest.mark.parametrize("layer", [0, 31])
@pytest.mark.parametrize("selection", ["applied", "zero", "unmatched"])
def test_public_correction_wrappers_preserve_top_bottom_and_legacy_angles(
    modules: SimpleNamespace,
    tmp_path: Path,
    legacy_angle: bool,
    layer: int,
    selection: str,
) -> None:
    """Independent wrapper calls retain both orientation APIs and zero semantics."""
    fabrication = make_fabrication(modules, SimpleNamespace(), tmp_path)
    footprint = make_footprint("U1", layer, 30, Point(10, 20))
    if legacy_angle:
        footprint.GetOrientation = lambda: 300
    fabrication.corrections = (
        modules.data.Correction(
            "unused" if selection == "unmatched" else "Device",
            0 if selection == "zero" else 90,
            (0, 0) if selection == "zero" else (1, 2),
        ),
    )
    position = Point(10, 20)

    rotation = fabrication.fix_rotation(footprint)
    corrected = fabrication.fix_position(footprint, position)

    if selection == "applied":
        assert rotation == (120 if layer == 0 else 240)
        expected = (
            (11.866025403784, 21.232050807569)
            if layer == 0
            else (9.866025403784, 17.767949192431)
        )
        assert (corrected.x, corrected.y) == pytest.approx(expected)
    else:
        assert rotation == (30 if layer == 0 else 150)
        assert corrected is position


@pytest.mark.parametrize(
    "row,field",
    [
        (("unused", "47u", 0, 0), "rotation"),
        (("unused", None, 0, 0), "rotation"),
        (("unused", 90.5, 0, 0), "rotation"),
        (("unused", 90, "invalid", 0), "offset_x"),
        (("unused", 90, 0, "invalid"), "offset_y"),
        (("unused", 90, float("inf"), 0), "offset_x"),
        (("unused", 90, 0, None), "offset_y"),
        (("[", 90, 0, 0), "pattern"),
        ((None, 90, 0, 0), "pattern"),
        ((b"unused", 90, 0, 0), "pattern"),
    ],
)
@pytest.mark.parametrize("existing_output", [False, True])
def test_invalid_storage_preserves_output_before_board_work(
    modules: SimpleNamespace,
    library: Any,
    tmp_path: Path,
    row: tuple[object, ...],
    field: str,
    existing_output: bool,
) -> None:
    """Even unmatched invalid rows block output without truncation or stale reuse."""
    seed_raw(library, [("Device", 90, 1, 2), row])
    before = raw_rows(library)
    fabrication = make_fabrication(modules, fresh_library(library), tmp_path)
    fabrication.corrections = (modules.data.Correction("Device", 90, (1, 2)),)
    destination = Path(fabrication.get_cpl_csv_path())
    if existing_output:
        destination.write_bytes(b"previous CPL\r\nuntouched\x00")

    with pytest.raises(ValueError) as error:
        fabrication.generate_cpl()

    assert "Open Corrections Manager" in str(error.value)
    assert field not in str(error.value)
    assert library.correctionsdb_file in str(error.value)
    assert raw_rows(library) == before
    fabrication.board.GetDesignSettings.assert_not_called()
    fabrication.board.Footprints.assert_not_called()
    if existing_output:
        assert destination.read_bytes() == b"previous CPL\r\nuntouched\x00"
    else:
        assert not destination.exists()


@pytest.mark.parametrize("failure", ["missing", "directory", "corrupt", "schema"])
def test_storage_failure_preserves_existing_cpl(
    modules: SimpleNamespace, library: Any, tmp_path: Path, failure: str
) -> None:
    """Unreadable correction storage never becomes an implicit empty correction set."""
    database = Path(library.correctionsdb_file)
    if failure == "schema":
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("DROP TABLE correction")
    else:
        database.unlink()
        if failure == "directory":
            database.mkdir()
        elif failure == "corrupt":
            database.write_bytes(b"this is not a SQLite database")
    fabrication = make_fabrication(modules, fresh_library(library), tmp_path)
    destination = Path(fabrication.get_cpl_csv_path())
    destination.write_bytes(b"saved CPL")

    with pytest.raises(ValueError, match="database"):
        fabrication.generate_cpl()

    assert destination.read_bytes() == b"saved CPL"
    fabrication.board.GetDesignSettings.assert_not_called()
    if failure == "missing":
        assert not database.exists()


def test_conflicting_duplicate_corrections_preserve_output(
    modules: SimpleNamespace, library: Any, tmp_path: Path
) -> None:
    """Ambiguous persisted patterns cannot silently select one fabrication result."""
    seed_raw(library, [("Device", 90, 0, 0), ("Device", 180, 0, 0)])
    fabrication = make_fabrication(modules, fresh_library(library), tmp_path)
    destination = Path(fabrication.get_cpl_csv_path())
    destination.write_bytes(b"previous")

    with pytest.raises(ValueError, match="unresolved"):
        fabrication.generate_cpl()

    assert destination.read_bytes() == b"previous"


def test_repaired_database_reopens_and_generates_correct_top_bottom_cpl(
    modules: SimpleNamespace, library: Any, tmp_path: Path
) -> None:
    """Repair restores actual rotation, mirrored offsets, and origin subtraction."""
    seed_raw(library, [("Device", "47u", 1, 2)])
    fabrication = make_fabrication(modules, fresh_library(library), tmp_path)
    with pytest.raises(ValueError, match="unresolved"):
        fabrication.generate_cpl()
    bad_row = library.read_correction_data().rows[0]
    library.save_correction_data("Device", 90, (1, 2), rowid=bad_row.rowid)
    fabrication.parent.library = fresh_library(library)

    fabrication.generate_cpl()

    rows = read_cpl(fabrication)
    assert [row["Designator"] for row in rows] == ["U1", "U2"]
    assert [row["Layer"] for row in rows] == ["top", "bottom"]
    assert [row["Package"] for row in rows] == ["Package:Device"] * 2
    assert [row["Val"] for row in rows] == ["Device"] * 2
    assert [float(row["Rotation"]) for row in rows] == [90, 180]
    assert [float(row["Mid X"]) for row in rows] == pytest.approx([10, 27])
    assert [float(row["Mid Y"]) for row in rows] == pytest.approx([-20, -37])


def test_preflight_snapshot_survives_storage_change_and_direct_call_rereads(
    modules: SimpleNamespace, library: Any, tmp_path: Path
) -> None:
    """One operation retains its snapshot while later direct calls validate anew."""
    seed_raw(library, [("Device", 90, 1, 2)])
    snapshot = fresh_library(library).read_correction_data().corrections
    fabrication = make_fabrication(modules, library, tmp_path)
    with closing(sqlite3.connect(library.correctionsdb_file)) as connection, connection:
        connection.execute("UPDATE correction SET rotation='47u'")

    fabrication.generate_cpl(corrections=snapshot)

    destination = Path(fabrication.get_cpl_csv_path())
    before = destination.read_bytes()
    assert [float(row["Rotation"]) for row in read_cpl(fabrication)] == [90, 180]
    with pytest.raises(ValueError, match="unresolved"):
        fabrication.generate_cpl()
    assert destination.read_bytes() == before


def test_direct_calls_refresh_valid_corrections_between_generations(
    modules, library, tmp_path
):
    """A populated matcher cache cannot hide a committed correction change."""
    library.save_correction_data("Device", 90, (0, 0))
    fabrication = make_fabrication(modules, library, tmp_path)
    fabrication.generate_cpl()
    assert [float(row["Rotation"]) for row in read_cpl(fabrication)] == [90, 180]
    fresh_library(library).save_correction_data("Device", -90, (0, 0), replace=True)

    fabrication.generate_cpl()

    assert [float(row["Rotation"]) for row in read_cpl(fabrication)] == [270, 0]


@pytest.mark.parametrize(
    "invalid",
    [
        ("Device", 90, (0, 0)),
        SimpleNamespace(pattern="Device", rotation="47u", offset=(0, 0)),
        None,
        [],
    ],
)
def test_supplied_snapshot_requires_immutable_corrections_before_output(
    modules: SimpleNamespace, library: Any, tmp_path: Path, invalid: object
) -> None:
    """Only immutable collections of validated values can enter the public boundary."""
    fabrication = make_fabrication(modules, library, tmp_path)
    destination = Path(fabrication.get_cpl_csv_path())
    destination.write_bytes(b"previous")
    valid = modules.data.Correction("valid-first", 90, (0, 0))
    supplied = [valid] if isinstance(invalid, list) else (valid, invalid)

    with pytest.raises(TypeError, match="Correction"):
        fabrication.generate_cpl(corrections=supplied)

    assert destination.read_bytes() == b"previous"
    fabrication.board.GetDesignSettings.assert_not_called()


def test_explicit_empty_snapshot_does_not_fall_back_to_storage(
    modules: SimpleNamespace, library: Any, tmp_path: Path
) -> None:
    """An intentionally empty preflight snapshot remains valid after new bad writes."""
    snapshot = library.read_correction_data().corrections
    seed_raw(library, [("Device", "47u", 0, 0)])
    fabrication = make_fabrication(modules, library, tmp_path)
    fabrication.corrections = (modules.data.Correction("Device", 90, (0, 0)),)

    fabrication.generate_cpl(corrections=snapshot)

    rows = read_cpl(fabrication)
    assert [float(row["Rotation"]) for row in rows] == [0, 90]
    assert [float(row["Mid X"]) for row in rows] == [9, 29]
    assert [float(row["Mid Y"]) for row in rows] == [-18, -38]
    assert fabrication.corrections == ()


@pytest.mark.parametrize("correction", [2**53 + 1, -(2**53 + 1), 2**63 - 1, -(2**63)])
@pytest.mark.parametrize("orientation", [0.0, 12.5])
def test_cpl_preserves_exact_large_correction_with_native_float_angle(
    modules: SimpleNamespace,
    library: Any,
    tmp_path: Path,
    correction: int,
    orientation: float,
) -> None:
    """Whole-degree corrections retain their modulo when KiCad returns a float."""
    library.save_correction_data("Device", correction, (0, 0))
    fabrication = make_fabrication(modules, fresh_library(library), tmp_path)
    fabrication.board.Footprints.return_value = [
        make_footprint("U1", 0, orientation, Point(10, 20)),
        make_footprint("U2", 31, orientation, Point(30, 40)),
    ]

    fabrication.generate_cpl()

    assert [float(row["Rotation"]) for row in read_cpl(fabrication)] == [
        (orientation + correction % 360) % 360,
        ((180 - orientation) % 360 + correction % 360) % 360,
    ]
    assert raw_rows(library)[0][2] == correction
