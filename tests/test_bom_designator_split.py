"""BOM chunking and assembly choices through real Store and CSV generation."""

import csv
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.test_fabrication_correction_recovery import (
    Point,
    make_fabrication,
    make_footprint,
    modules as fabrication_modules,
    read_cpl,
)

modules = fabrication_modules


@pytest.mark.parametrize(
    "references,limit",
    [([], 25), (["R1"], 25), (["A" * 10] * 5, 25), (["R1", "X" * 3000, "R2"], 2048)],
)
def test_designator_chunks_preserve_order_and_limits(
    modules: SimpleNamespace, references: list[str], limit: int
) -> None:
    """Preserve every reference; an oversized individual reference stays intact."""
    chunks = modules.fabrication.split_bom_designators(references, limit)
    assert [reference for chunk in chunks for reference in chunk] == references
    assert all(len(",".join(chunk)) <= limit or len(chunk) == 1 for chunk in chunks)


def with_store(modules: SimpleNamespace, tmp_path: Path, count: int = 2) -> Any:
    """Use persisted assignments and live geometry in the real exporter."""
    fabrication = make_fabrication(modules, SimpleNamespace(), tmp_path)
    fabrication.board.Footprints.return_value = [
        make_footprint(f"R{index}", 0, 0, Point(10, 20))
        for index in range(1, count + 1)
    ]
    fabrication.parent.store = modules.store.Store(
        fabrication.parent, str(tmp_path), fabrication.board
    )
    fabrication.parent.store.update_parts(
        {fp.GetReference(): {"lcsc": "C123"} for fp in fabrication.board.Footprints()}
    )
    return fabrication


def read_bom(fabrication: Any) -> list[dict[str, str]]:
    """Read generated BOM rows using a fresh handle."""
    with Path(fabrication.get_bom_csv_path()).open(newline="") as stream:
        return list(csv.DictReader(stream))


@pytest.mark.parametrize("count", [10, 500])
def test_generated_bom_preserves_quantity_and_splits_large_groups(
    modules: SimpleNamespace, tmp_path: Path, count: int
) -> None:
    """Issue #755: retain all parts while respecting the 2048-character row limit."""
    fabrication = with_store(modules, tmp_path, count)
    fabrication.generate_bom()
    rows = read_bom(fabrication)
    references = [
        reference for row in rows for reference in row["Designator"].split(",")
    ]
    assert sorted(references) == sorted(f"R{index}" for index in range(1, count + 1))
    assert sum(int(row["Quantity"]) for row in rows) == count
    for row in rows:
        assert int(row["Quantity"]) == len(row["Designator"].split(","))
        assert len(row["Designator"]) <= modules.fabrication._BOM_DESIGNATOR_MAX_LEN
    assert len(rows) == (1 if count == 10 else 2)


@pytest.mark.parametrize(
    "failure", ["duplicate", "removed", "renamed", "replaced", "added"]
)
@pytest.mark.parametrize("output", ["bom", "cpl"])
def test_changed_board_preserves_previous_output(
    modules: SimpleNamespace, tmp_path: Path, failure: str, output: str
) -> None:
    """A captured assignment must not silently move to a different footprint."""
    fabrication = with_store(modules, tmp_path)
    parts = fabrication.parent.store.read_all()
    footprints = fabrication.board.Footprints.return_value
    if failure == "duplicate":
        footprints.append(make_footprint("R1", 0, 0, Point(0, 0)))
    elif failure == "removed":
        footprints.pop()
    elif failure == "renamed":
        footprints[-1].GetReference = lambda: "R3"
    elif failure == "added":
        footprints.append(make_footprint("R3", 0, 0, Point(0, 0)))
    else:
        footprints[-1].m_Uuid = SimpleNamespace(AsString=lambda: "replacement")
    destination = Path(getattr(fabrication, f"get_{output}_csv_path")())
    destination.write_text("previous complete output", encoding="utf-8")

    with pytest.raises(ValueError, match="Duplicate|changed"):
        if output == "bom":
            fabrication.generate_bom(parts)
        else:
            fabrication.generate_cpl((), parts)

    assert destination.read_text(encoding="utf-8") == "previous complete output"


def test_non_component_footprints_do_not_block_exports(
    modules: SimpleNamespace, tmp_path: Path
) -> None:
    """Footprints without references are outside the Store's assembly domain."""
    fabrication = with_store(modules, tmp_path)
    fabrication.board.Footprints.return_value.extend(
        make_footprint("", 0, 0, Point(0, 0)) for _ in range(2)
    )
    fabrication.generate_cpl(())
    fabrication.generate_bom()
    assert len(read_cpl(fabrication)) == 2
    assert read_bom(fabrication)[0]["Quantity"] == "2"


@pytest.mark.parametrize("include_unassigned", [False, True])
def test_one_snapshot_drives_bom_cpl_and_consistency_with_live_geometry(
    modules: SimpleNamespace, tmp_path: Path, include_unassigned: bool
) -> None:
    """Native changes and later DB edits cannot split one fabrication operation."""
    fabrication = with_store(modules, tmp_path, 4)
    fabrication.parent.settings = {"gerber": {"lcsc_bom_cpl": include_unassigned}}
    store = fabrication.parent.store
    footprints = fabrication.board.Footprints()
    footprints[1].GetValue = lambda: "Other"
    assert "C123" in fabrication.get_part_consistency_warnings()
    store.update_parts(
        {
            "R2": {"exclude_from_bom": 1, "exclude_from_pos": 1},
            "R3": {"is_dnp": 1},
            "R4": {"lcsc": ""},
        }
    )
    parts = store.read_all()
    store.update_parts({"R1": {"lcsc": "C999", "is_dnp": 1}})
    for footprint in footprints:
        footprint.GetFields = lambda: [
            SimpleNamespace(GetName=lambda: "LCSC", GetText=lambda: "C555")
        ]
        footprint.GetAttributes = lambda: 12
        footprint.IsDNP = lambda: True
    footprints[0].GetPosition = lambda: Point(42, 64)

    assert fabrication.get_part_consistency_warnings(parts) == ""
    fabrication.generate_bom(parts)
    fabrication.generate_cpl((), parts)

    assert [(row["Designator"], row["LCSC"]) for row in read_bom(fabrication)] == (
        [("R1", "C123")] + ([("R4", "")] if include_unassigned else [])
    )
    assert [
        (row["Designator"], row["Mid X"], row["Mid Y"]) for row in read_cpl(fabrication)
    ] == (
        [("R1", "41.000000", "-62.000000")]
        + ([("R4", "9.000000", "-18.000000")] if include_unassigned else [])
    )
