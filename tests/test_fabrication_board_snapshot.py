"""Export live board assignments and keep one operation's eligibility consistent."""

from collections.abc import Callable, Iterator
import csv
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from . import test_stock_fabrication_recovery as generation
from .wx_harness import load_siblings, module

PlacementFootprint = generation.GenerationFootprint
mainwindow = generation.mainwindow
make_window = generation.make_window
generation_window = generation.generation_window


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[Callable[..., SimpleNamespace]]:
    """Use real Store and Fabrication constructors with stateful native boundaries."""
    package = "_fabrication_board_snapshot_tests"
    pcbnew = MagicMock()
    pcbnew.FromMM = lambda value: value
    pcbnew.ToMM = lambda value: value
    pcbnew.wxPoint = lambda x, y: SimpleNamespace(x=x, y=y)
    helpers = module(
        f"{package}.helpers",
        natural_sort_collation=lambda a, b: (a > b) - (a < b),
    )
    with load_siblings(
        package,
        ("store", "fabrication"),
        {"pcbnew": pcbnew, f"{package}.helpers": helpers},
    ) as modules:

        def create(
            footprints: list[PlacementFootprint], *, include_unassigned: bool = True
        ) -> SimpleNamespace:
            """Open an exporter against the current board rather than prebuilt rows."""
            board = generation.GenerationBoard(tmp_path / "board.kicad_pcb")
            board.footprints = {fp.reference: fp for fp in footprints}
            parent = SimpleNamespace(
                settings={"gerber": {"lcsc_bom_cpl": include_unassigned}},
                library=SimpleNamespace(get_lcsc_metadata=lambda _codes: {}),
            )
            parent.store = modules["store"].Store(parent, str(tmp_path), board)
            exporter = modules["fabrication"].Fabrication(parent, board)
            return SimpleNamespace(
                board=board,
                store=parent.store,
                exporter=exporter,
                correction=modules["fabrication"].Correction,
            )

        yield create


def read_bom(exporter: Any) -> list[dict[str, str]]:
    """Read the export from disk with the public CSV field names."""
    with Path(exporter.get_bom_csv_path()).open(newline="") as stream:
        return list(csv.DictReader(stream))


def test_direct_exports_follow_edits_deletions_and_dnp(
    runtime: Callable[..., SimpleNamespace],
) -> None:
    """Reusing an exporter must never retain old assignments or board membership."""
    first = PlacementFootprint("R1")
    deleted = PlacementFootprint("R2")
    dnp = PlacementFootprint("R3")
    run = runtime([first, deleted, dnp])
    run.exporter.generate_bom()
    assert read_bom(run.exporter)[0]["Quantity"] == "3"

    first.SetField("LCSC", "C200")
    first.value = "22k"
    del run.board.footprints["R2"]
    dnp.dnp = True

    run.exporter.generate_bom()
    placements = run.exporter.prepare_cpl(())

    assert [tuple(row.values()) for row in read_bom(run.exporter)] == [
        ("22k", "R1", "R_0603", "C200", "1")
    ]
    assert [row[:3] for row in placements] == [("R1", "22k", "R_0603")]
    assert run.exporter.get_part_consistency_warnings() == ""
    run.board.footprints.clear()
    run.exporter.generate_bom()
    assert read_bom(run.exporter) == []
    assert run.exporter.prepare_cpl(()) == ()


@pytest.mark.parametrize("existing_output", [False, True])
def test_board_read_failure_preserves_bom(
    runtime: Callable[..., SimpleNamespace],
    monkeypatch: pytest.MonkeyPatch,
    existing_output: bool,
) -> None:
    """Read current native state successfully before replacing the last BOM."""
    run = runtime([PlacementFootprint("R1")])
    path = Path(run.exporter.get_bom_csv_path())
    if existing_output:
        path.write_text("previous complete bill of materials")
    monkeypatch.setattr(
        run.board,
        "GetFootprints",
        MagicMock(side_effect=RuntimeError("board unavailable")),
    )

    with pytest.raises(RuntimeError, match="board unavailable"):
        run.exporter.generate_bom()

    assert (path.read_text() if path.exists() else None) == (
        "previous complete bill of materials" if existing_output else None
    )


def test_one_snapshot_drives_preflight_cpl_and_bom(
    runtime: Callable[..., SimpleNamespace],
) -> None:
    """UI changes after capture cannot change saved BOM/CPL eligibility mid-operation."""
    first = PlacementFootprint("R1")
    second = PlacementFootprint("R2", value="22k")
    run = runtime([first, second])
    parts = run.store.read_all()

    first.SetField("LCSC", "")
    first.dnp = True
    first.attributes = (1 << 2) | (1 << 3)
    second.value = "10k"
    run.board.footprints["R3"] = PlacementFootprint("R3")

    warning = run.exporter.get_part_consistency_warnings(parts=parts)
    placements = run.exporter.prepare_cpl(
        (run.correction("22k", 90, (0, 0)),), parts=parts
    )
    # Placement geometry has now been captured; writing uses the completed rows.
    del run.board.footprints["R2"]
    run.exporter.write_cpl(placements)
    run.exporter.generate_bom(parts=parts)

    assert "C100:" in warning
    assert "R1 -> 10k" in warning
    assert "R2 -> 22k" in warning
    assert [row[:3] for row in placements] == [
        ("R1", "10k", "R_0603"),
        ("R2", "22k", "R_0603"),
    ]
    assert [row[5] for row in placements] == [0, 90]
    assert [(row["Designator"], row["Comment"]) for row in read_bom(run.exporter)] == [
        ("R1", "10k"),
        ("R2", "22k"),
    ]
    with Path(run.exporter.get_cpl_csv_path()).open(newline="") as stream:
        assert [row["Designator"] for row in csv.DictReader(stream)] == ["R1", "R2"]
    assert run.exporter.get_part_consistency_warnings() == ""


@pytest.mark.parametrize("change", ["remove", "rename", "duplicate"])
@pytest.mark.parametrize("existing_output", [False, True])
def test_missing_or_ambiguous_placement_preserves_cpl(
    runtime: Callable[..., SimpleNamespace], change: str, existing_output: bool
) -> None:
    """Missing snapshot references and ambiguous geometry fail before opening output."""
    second = PlacementFootprint("R2")
    run = runtime([PlacementFootprint("R1"), second])
    parts = run.store.read_all()
    path = Path(run.exporter.get_cpl_csv_path())
    if existing_output:
        path.write_text("previous complete placement file")
    if change == "remove":
        del run.board.footprints["R2"]
    else:
        second.reference = "R20" if change == "rename" else "R1"
    error = "R2 was removed or renamed"
    if change == "duplicate":
        parts = None  # A direct export must reject ambiguous live references too.
        error = "Duplicate footprint reference R1"
    with pytest.raises(ValueError, match=error):
        run.exporter.generate_cpl((), parts=parts)

    assert (path.read_text() if path.exists() else None) == (
        "previous complete placement file" if existing_output else None
    )


@pytest.mark.parametrize(
    "first_lcsc,second_fields,include_unassigned,expected_count",
    [
        ("C100", {}, True, None),
        ("C100", {"value": "22k"}, True, None),
        ("C100", {"lcsc": "C200"}, True, None),
        ("C100", {"bom": True}, True, 1),
        ("C100", {"dnp": True}, True, 1),
        ("C100", {"lcsc": ""}, True, None),
        ("", {"lcsc": ""}, True, None),
        ("C100", {"lcsc": ""}, False, 1),
        ("", {"lcsc": ""}, False, 0),
    ],
)
@pytest.mark.parametrize("existing_output", [False, True])
def test_duplicate_bom_references_follow_export_eligibility(
    runtime: Callable[..., SimpleNamespace],
    first_lcsc: str,
    second_fields: dict[str, Any],
    include_unassigned: bool,
    expected_count: Optional[int],
    existing_output: bool,
) -> None:
    """Only exported BOM references must be unique, even when CPL contains no parts."""
    second = PlacementFootprint("R2", pos=True, **second_fields)
    run = runtime(
        [PlacementFootprint("R1", lcsc=first_lcsc, pos=True), second],
        include_unassigned=include_unassigned,
    )
    second.reference = "R1"
    path = Path(run.exporter.get_bom_csv_path())
    if existing_output:
        path.write_text("previous complete bill of materials")

    assert run.exporter.prepare_cpl(()) == ()
    if expected_count is None:
        with pytest.raises(ValueError, match="Duplicate footprint reference R1"):
            run.exporter.get_part_consistency_warnings()
        with pytest.raises(ValueError, match="Duplicate footprint reference R1"):
            run.exporter.generate_bom()
        assert (path.read_text() if path.exists() else None) == (
            "previous complete bill of materials" if existing_output else None
        )
    else:
        assert run.exporter.get_part_consistency_warnings() == ""
        run.exporter.generate_bom()
        expected = [("R1", first_lcsc, "1")] if expected_count else []
        assert [
            (row["Designator"], row["LCSC"], row["Quantity"])
            for row in read_bom(run.exporter)
        ] == expected


@pytest.mark.parametrize("include_unassigned", [False, True])
def test_bom_and_cpl_keep_independent_exclusions(
    runtime: Callable[..., SimpleNamespace], include_unassigned: bool
) -> None:
    """DNP suppresses both outputs while each exclusion controls only its own file."""
    run = runtime(
        [
            PlacementFootprint("R1"),
            PlacementFootprint("R2", bom=True),
            PlacementFootprint("R3", pos=True),
            PlacementFootprint("R4", dnp=True),
            PlacementFootprint("R5", lcsc=""),
        ],
        include_unassigned=include_unassigned,
    )
    parts = run.store.read_all()
    placements = run.exporter.prepare_cpl((), parts=parts)
    run.exporter.generate_bom(parts=parts)

    unassigned = {"R5"} if include_unassigned else set()
    assert {row[0] for row in placements} == {"R1", "R2"} | unassigned
    assert {
        ref for row in read_bom(run.exporter) for ref in row["Designator"].split(",")
    } == {"R1", "R3"} | unassigned


@pytest.mark.parametrize("second_value", ["10k", "22k"])
def test_pos_excluded_duplicate_bom_refs_block_all_generation_outputs(
    generation_window: Callable[[str], SimpleNamespace], second_value: str
) -> None:
    """Real preflight rejects duplicate BOM references even when CPL has no rows."""
    context = generation_window("healthy")
    window = context.window
    board = window.pcbnew.GetBoard()
    board.footprints["R1"].attributes = 1 << 2
    # The dictionary retains distinct objects even though both now report R1.
    board.footprints["duplicate"] = generation.GenerationFootprint(
        "R1", value=second_value, pos=True
    )
    window.run_generate_hook = MagicMock(return_value=True)
    output_methods = (
        "generate_geber",
        "generate_excellon",
        "zip_gerber_excellon",
        "write_cpl",
        "generate_bom",
    )
    existing = [Path(path) for path in window.fabrication.get_artifact_paths().values()]
    for path in existing:
        path.write_bytes(b"previous complete artifact")
    for name in output_methods:
        setattr(window.fabrication, name, MagicMock())

    window.generate_fabrication_data()

    context.message_box.assert_called_once()
    assert "Duplicate footprint reference R1" in context.message_box.call_args.args[0]
    assert context.placements == [()]
    assert not context.zone_checks
    window.run_drc_before_gerber_export.assert_not_called()
    window.run_generate_hook.assert_not_called()
    for name in output_methods:
        getattr(window.fabrication, name).assert_not_called()
    assert all(path.read_bytes() == b"previous complete artifact" for path in existing)
    assert not Path(window.store.dbfile).exists()
