"""Keep BOM designators within JLCPCB's row limit without losing components.

The 500-LED export reproduces https://github.com/Bouni/kicad-jlcpcb-tools/issues/755.
"""

from collections.abc import Callable
import csv
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from tests.fabrication_test_support import modules as fabrication_modules

modules = fabrication_modules
BomFactory = Callable[..., Any]


@pytest.mark.parametrize(
    "refs,limit,expected",
    [
        ([], 2048, []),
        (["R1"], 2048, [["R1"]]),
        (["R1", "R2"], 2048, [["R1", "R2"]]),
        (["A" * 10] * 5, 25, [["A" * 10] * 2, ["A" * 10] * 2, ["A" * 10]]),
        (["R1", "X" * 3000, "R2"], 2048, [["R1"], ["X" * 3000], ["R2"]]),
        (["R1", "R2", "R3"], 8, [["R1", "R2", "R3"]]),
        (["R1", "R2", "R3"], 7, [["R1", "R2"], ["R3"]]),
    ],
    ids=("empty", "single", "short", "custom", "oversized", "exact", "separator"),
)
def test_designator_chunks_preserve_order_and_include_separators(
    modules: SimpleNamespace, refs: list[str], limit: int, expected: list[list[str]]
) -> None:
    """Exact chunks cover boundaries, custom limits, and an indivisible reference."""
    assert modules.fabrication.split_bom_designators(refs, max_len=limit) == expected


@pytest.fixture
def bom_factory(modules: SimpleNamespace, tmp_path: Path) -> BomFactory:
    """Construct the real exporter with one catalog group and explicit board refs."""

    def make(
        refs: list[str],
        board_refs: Optional[list[str]] = None,  # noqa: UP045
        **fields: str,
    ) -> Any:
        footprints = [
            SimpleNamespace(GetReference=lambda ref=ref: ref)
            for ref in (refs if board_refs is None else board_refs)
        ]
        board = SimpleNamespace(
            Footprints=lambda: footprints,
            GetFileName=lambda: str(tmp_path / "board.kicad_pcb"),
        )
        group = {
            "refs": ",".join(refs),
            "value": "WS2812B",
            "footprint": "LED_0805",
            "lcsc": "C25741",
            **fields,
        }
        parent = SimpleNamespace(
            settings={"gerber": {"lcsc_bom_cpl": True}},
            store=SimpleNamespace(read_bom_parts=lambda: [group]),
        )
        return modules.fabrication.Fabrication(parent, board)

    return make


def _read_bom_rows(fab: Any) -> list[list[str]]:
    """Run generation and read the actual CSV, including its expected header."""
    fab.generate_bom()
    with Path(fab.get_bom_csv_path()).open(newline="", encoding="utf-8") as stream:
        header, *rows = csv.reader(stream)
    assert header == ["Comment", "Designator", "Footprint", "LCSC", "Quantity"]
    return rows


@pytest.mark.parametrize(
    "refs,board_refs,expected",
    [
        ([], None, []),
        (["R1", "R2"], None, [["100k", "R1,R2", "R0402", "C25741", "2"]]),
        (["R1", "GONE"], ["R1"], [["100k", "R1", "R0402", "C25741", "1"]]),
    ],
    ids=("empty", "single-row", "deleted-reference"),
)
def test_generate_bom_keeps_only_current_board_references(
    bom_factory: BomFactory,
    refs: list[str],
    board_refs: Optional[list[str]],  # noqa: UP045
    expected: list[list[str]],
) -> None:
    """Empty groups emit nothing; small groups omit footprints deleted from KiCad."""
    fab = bom_factory(refs, board_refs, value="100k", footprint="R0402")
    assert _read_bom_rows(fab) == expected


def test_generate_bom_splits_500_leds_without_losing_data(
    bom_factory: BomFactory,
) -> None:
    """One real export checks every chunk's contents, limits, and quantities."""
    refs = [f"LED{i}" for i in range(1, 501)]
    rows = _read_bom_rows(bom_factory(refs))
    assert len(rows) == 2
    assert [ref for row in rows for ref in row[1].split(",")] == refs
    assert sum(int(row[4]) for row in rows) == 500
    for comment, designators, footprint, lcsc, quantity in rows:
        assert len(designators) <= 2048
        assert int(quantity) == len(designators.split(","))
        assert (comment, footprint, lcsc) == ("WS2812B", "LED_0805", "C25741")


def test_bom_read_failure_preserves_previous_output(
    bom_factory: BomFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database error preparing replacement rows cannot truncate the last BOM."""
    fab = bom_factory(["R1"])
    fab.generate_bom()
    destination = Path(fab.get_bom_csv_path())
    previous = destination.read_bytes()

    def fail_read() -> list[dict[str, str]]:
        raise OSError("catalog read failed")

    monkeypatch.setattr(fab.parent.store, "read_bom_parts", fail_read)
    with pytest.raises(OSError, match="catalog read failed"):
        fab.generate_bom()
    assert destination.read_bytes() == previous
