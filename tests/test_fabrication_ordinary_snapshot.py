"""Ordinary exports use one board-derived mapping for every assembly artifact."""

from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from tests.fabrication_test_support import (
    Point,
    make_footprint,
    modules as fabrication_modules,
)

modules = fabrication_modules


@pytest.fixture
def runtime(modules: SimpleNamespace, tmp_path: Path) -> SimpleNamespace:
    """Construct a real exporter backed by mutable board mapping records."""
    parts = [
        {
            "reference": f"R{index}",
            "value": value,
            "footprint": "Package:Device",
            "lcsc": "C111",
            "exclude_from_bom": 0,
            "exclude_from_pos": 0,
            "is_dnp": False,
            "stock": 20,
        }
        for index, value in enumerate(("10k", "20k"), 1)
    ]
    footprints = [
        make_footprint(part["reference"], 0, 0, Point(10, 20)) for part in parts
    ]
    board = SimpleNamespace(
        GetFileName=lambda: str(tmp_path / "board.kicad_pcb"),
        Footprints=lambda: footprints,
        GetDesignSettings=lambda: SimpleNamespace(GetAuxOrigin=lambda: Point(1, 2)),
    )

    def read_bom_parts(
        captured: Optional[Iterable[dict[str, Any]]] = None,  # noqa: UP045
    ) -> list[dict[str, Any]]:
        """Group the supplied mapping or the current native data independently."""
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for part in parts if captured is None else captured:
            if part["exclude_from_bom"] or part["is_dnp"]:
                continue
            key = (part["value"], part["lcsc"])
            if key in groups:
                groups[key]["refs"] += "," + part["reference"]
            else:
                groups[key] = {**part, "refs": part["reference"]}
        return list(groups.values())

    store = SimpleNamespace(
        read_all=lambda: [dict(part) for part in parts],
        get_part=lambda reference: next(
            (dict(part) for part in parts if part["reference"] == reference), None
        ),
        read_bom_parts=read_bom_parts,
    )
    parent = SimpleNamespace(settings={}, store=store)
    exporter = modules.fabrication.Fabrication(parent, board)
    corrections = (
        modules.data.LcscCorrection("C111", 90, (0, 0)),
        modules.data.LcscCorrection("C222", 180, (0, 0)),
    )
    return SimpleNamespace(
        exporter=exporter,
        parts=parts,
        footprints=footprints,
        parent=parent,
        corrections=corrections,
    )


def test_mapping_change_cannot_mix_bom_with_prepared_cpl(
    runtime: SimpleNamespace,
) -> None:
    """A changed native assignment rejects writes before an old file is opened."""
    exporter = runtime.exporter
    exporter.begin_ordinary_generation(runtime.corrections)
    placements = exporter.prepare_cpl()
    assert [row[5] for row in placements] == [90, 90]
    assert "R1 -> 10k" in exporter.get_part_consistency_warnings()
    previous = Path(exporter.get_bom_csv_path())
    previous.write_text("previous BOM\n")

    runtime.parts[0]["lcsc"] = "C222"

    assert [row[3] for row in exporter.output_snapshot.bom_rows] == ["C111", "C111"]
    with pytest.raises(RuntimeError, match="changed.*generation"):
        exporter.generate_bom()
    assert previous.read_text() == "previous BOM\n"
    with pytest.raises(RuntimeError, match="changed.*generation"):
        exporter.write_cpl(placements)
    assert not Path(exporter.get_cpl_csv_path()).exists()


def test_generation_end_discards_mapping_and_refreshes_correction_choice(
    runtime: SimpleNamespace,
) -> None:
    """Reusing an exporter after cancellation captures the new board assignment."""
    exporter = runtime.exporter
    exporter.begin_ordinary_generation(runtime.corrections)
    runtime.parts[0]["lcsc"] = "C222"
    exporter.end_ordinary_generation()
    assert exporter.output_snapshot is None

    exporter.begin_ordinary_generation(runtime.corrections)

    assert [row[3] for row in exporter.prepare_bom()] == ["C222", "C111"]
    assert [row[5] for row in exporter.prepare_cpl()] == [180, 90]
    assert exporter.get_part_consistency_warnings() == ""
    exporter.end_ordinary_generation()


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("value", "30k"),
        ("exclude_from_bom", 1),
        ("exclude_from_pos", 1),
        ("is_dnp", True),
    ],
)
def test_assembly_state_changes_reject_captured_output(
    runtime: SimpleNamespace, field: str, changed: Any
) -> None:
    """Values and each assembly flag are part of the frozen source contract."""
    exporter = runtime.exporter
    exporter.begin_ordinary_generation(runtime.corrections)
    runtime.parts[0][field] = changed
    with pytest.raises(RuntimeError, match="changed.*generation"):
        exporter.validate_generation()


def test_catalog_refresh_does_not_invalidate_board_mapping(
    runtime: SimpleNamespace,
) -> None:
    """Supplier stock is not native assembly state and cannot invalidate exports."""
    exporter = runtime.exporter
    exporter.begin_ordinary_generation(runtime.corrections)
    runtime.parts[0]["stock"] = 1234
    exporter.validate_generation()
    exporter.generate_bom()
    assert Path(exporter.get_bom_csv_path()).is_file()


def test_board_context_is_validated_before_capture_and_writes(
    runtime: SimpleNamespace,
) -> None:
    """A modeless window cannot export after its editor ownership guard fails."""
    exporter = runtime.exporter
    calls = []

    def current_board() -> Any:
        """Mirror the window's validated current-board accessor."""
        calls.append(True)
        return exporter.board

    runtime.parent._get_current_board = current_board
    exporter.begin_ordinary_generation(runtime.corrections)
    assert calls

    def changed_board() -> None:
        """Reject the replaced live editor board before native reads."""
        raise RuntimeError("Board ownership changed")

    runtime.parent._get_current_board = changed_board
    with pytest.raises(RuntimeError, match="Board ownership changed"):
        exporter.generate_bom()
    assert not Path(exporter.get_bom_csv_path()).exists()


def test_capture_failure_does_not_leave_active_snapshot(
    runtime: SimpleNamespace,
) -> None:
    """A failing source check remains retryable without stale captured rows."""
    exporter = runtime.exporter
    original = runtime.parent.store.read_all
    reads = []

    def read_during_edit() -> list[dict[str, Any]]:
        """Model a native edit between initial capture and source verification."""
        reads.append(True)
        if len(reads) == 2:
            runtime.parts[0]["lcsc"] = "C222"
        return original()

    runtime.parent.store.read_all = read_during_edit
    with pytest.raises(RuntimeError, match="changed.*generation"):
        exporter.begin_ordinary_generation(runtime.corrections)
    assert exporter.output_snapshot is None

    runtime.parent.store.read_all = original
    exporter.begin_ordinary_generation(runtime.corrections)
    assert exporter.prepare_bom()[0][3] == "C222"


def test_duplicate_references_reject_export_even_when_one_is_bom_excluded(
    runtime: SimpleNamespace,
) -> None:
    """A POS-included duplicate cannot borrow another footprint's assignment."""
    runtime.parts[0]["exclude_from_bom"] = 1
    runtime.parts[1]["reference"] = "R1"
    runtime.parts[1]["lcsc"] = "C222"
    runtime.footprints[1] = make_footprint("R1", 0, 0, Point(30, 40))

    with pytest.raises(ValueError, match="duplicate.*references"):
        runtime.exporter.begin_ordinary_generation(runtime.corrections)

    assert runtime.exporter.output_snapshot is None
    assert not Path(runtime.exporter.get_bom_csv_path()).exists()
    assert not Path(runtime.exporter.get_cpl_csv_path()).exists()


@pytest.mark.parametrize("change", ["filename", "geometry"])
def test_filename_or_geometry_changes_invalidate_capture(
    runtime: SimpleNamespace, change: str
) -> None:
    """Output filenames and physical placement must still belong to the capture."""
    exporter = runtime.exporter
    exporter.begin_ordinary_generation(runtime.corrections)
    if change == "filename":
        exporter.board.GetFileName = lambda: "different.kicad_pcb"
    else:
        runtime.footprints[0] = make_footprint("R1", 0, 90, Point(30, 40))

    with pytest.raises(RuntimeError, match="changed.*generation"):
        exporter.validate_generation()
