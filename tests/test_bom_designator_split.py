"""Tests for split_bom_designators – the 2048-char BOM Designator chunker.

JLCPCB rejects BOM rows whose Designator field exceeds 2048 characters.
When a single LCSC group has enough references to overflow that limit,
generate_bom() must emit multiple rows (one per chunk).

These tests exercise split_bom_designators() directly – a pure function –
and also run generate_bom() end-to-end against a fake board that has 500
identical LED footprints sharing one LCSC number, which is the scenario
described in https://github.com/Bouni/kicad-jlcpcb-tools/issues/755.
"""

import csv
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import types
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Bootstrap: load fabrication.py with mocked KiCad / wx dependencies
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).parent.parent

for _mod in ["pcbnew", "wx", "wx.dataview"]:
    sys.modules.setdefault(_mod, MagicMock())

_pkg = types.ModuleType("kicadplugin")
_pkg.__path__ = [str(_ROOT)]
sys.modules["kicadplugin"] = _pkg

_helpers = types.ModuleType("kicadplugin.helpers")
_helpers.get_is_dnp = lambda fp: False  # type: ignore[attr-defined]
sys.modules["kicadplugin.helpers"] = _helpers

_spec = importlib.util.spec_from_file_location(
    "kicadplugin.fabrication", _ROOT / "fabrication.py"
)
assert _spec is not None and _spec.loader is not None
_fab_mod = importlib.util.module_from_spec(_spec)
_fab_mod.__package__ = "kicadplugin"
sys.modules["kicadplugin.fabrication"] = _fab_mod
_spec.loader.exec_module(_fab_mod)  # type: ignore[union-attr]

Fabrication = _fab_mod.Fabrication  # type: ignore[attr-defined]
split_bom_designators = _fab_mod.split_bom_designators  # type: ignore[attr-defined]
_BOM_DESIGNATOR_MAX_LEN = _fab_mod._BOM_DESIGNATOR_MAX_LEN  # type: ignore[attr-defined]

# ---------------------------------------------------------------------------
# Unit tests for split_bom_designators()
# ---------------------------------------------------------------------------


def test_empty_list_returns_empty():
    """Empty input produces empty output."""
    assert split_bom_designators([]) == []


def test_single_ref_returns_one_chunk():
    """Single designator always stays in one chunk."""
    assert split_bom_designators(["R1"]) == [["R1"]]


def test_short_list_stays_in_one_chunk():
    """A list whose joined length is well under the limit stays in one chunk."""
    refs = [f"R{i}" for i in range(1, 11)]
    assert split_bom_designators(refs) == [refs]


def test_all_refs_preserved_across_chunks():
    """Every input designator must appear in exactly one output chunk."""
    refs = [f"LED{i}" for i in range(1, 501)]
    chunks = split_bom_designators(refs)
    flat = [r for chunk in chunks for r in chunk]
    assert flat == refs


def test_no_chunk_exceeds_max_len():
    """No chunk's joined string may exceed the limit."""
    refs = [f"LED{i}" for i in range(1, 501)]
    chunks = split_bom_designators(refs)
    for chunk in chunks:
        assert len(",".join(chunk)) <= _BOM_DESIGNATOR_MAX_LEN


def test_500_leds_requires_multiple_chunks():
    """500 LED designators produce more than one chunk (the issue-755 scenario)."""
    refs = [f"LED{i}" for i in range(1, 501)]
    assert len(",".join(refs)) > _BOM_DESIGNATOR_MAX_LEN, (
        "precondition: 500 LEDs joined should exceed the designator cap"
    )
    chunks = split_bom_designators(refs)
    assert len(chunks) > 1


def test_custom_max_len_respected():
    """Custom max_len parameter is honoured."""
    refs = ["A" * 10] * 5  # each ref is 10 chars; 5 of them joined = 54 chars
    chunks = split_bom_designators(refs, max_len=25)
    for chunk in chunks:
        assert len(",".join(chunk)) <= 25


def test_single_oversized_ref_gets_its_own_chunk():
    """A ref that is itself longer than max_len must still appear (in its own chunk)."""
    long_ref = "X" * 3000
    refs = ["R1", long_ref, "R2"]
    chunks = split_bom_designators(refs, max_len=2048)
    flat = [r for chunk in chunks for r in chunk]
    assert flat == refs
    assert long_ref in flat


def test_chunks_are_contiguous_and_ordered():
    """Order of designators must be preserved across chunk boundaries."""
    refs = [f"C{i:04d}" for i in range(1, 300)]
    chunks = split_bom_designators(refs)
    flat = [r for chunk in chunks for r in chunk]
    assert flat == refs


# ---------------------------------------------------------------------------
# Integration test: generate_bom() with a 500-LED fake board
# ---------------------------------------------------------------------------

N_LEDS = 500
_LED_REFS = [f"LED{i}" for i in range(1, N_LEDS + 1)]


class _FakeBoard:
    """Minimal board stub whose Footprints() returns one mock fp per ref."""

    def __init__(self, refs):
        self._refs = refs

    def Footprints(self):
        """Return mock footprints, one per reference."""
        fps = []
        for ref in self._refs:
            fp = MagicMock()
            fp.GetReference.return_value = ref
            fps.append(fp)
        return fps


class _FakeStore:
    """Minimal store stub that returns a single BOM part group."""

    def __init__(self, refs, value, footprint, lcsc):
        self._refs = refs
        self._value = value
        self._footprint = footprint
        self._lcsc = lcsc

    def read_bom_parts(self):
        """Return one part group with all refs joined."""
        return [
            {
                "refs": ",".join(self._refs),
                "value": self._value,
                "footprint": self._footprint,
                "lcsc": self._lcsc,
            }
        ]


def _make_fake_fab_for_bom(refs, lcsc="C25741", value="WS2812B", footprint="LED_0805"):
    """Build a minimal Fabrication instance whose generate_bom() we can call.

    The fake board has one footprint per ref; the fake store returns a single
    part group with all those refs joined.
    """
    fab = object.__new__(Fabrication)
    fab.logger = MagicMock()
    fab.board = _FakeBoard(refs)

    fake_parent = MagicMock()
    fake_parent.settings.get.return_value = {"lcsc_bom_cpl": True}
    fake_parent.store = _FakeStore(refs, value, footprint, lcsc)
    fab.parent = fake_parent

    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="")
    tmp.close()
    fab._tmppath = tmp.name
    fab.get_bom_csv_path = lambda: fab._tmppath
    return fab


def _read_bom_rows(fab):
    """Run generate_bom() and return the data rows (excluding the header)."""
    fab.generate_bom()
    with open(fab._tmppath, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    os.unlink(fab._tmppath)
    return rows[1:]


def test_generate_bom_500_leds_all_refs_present():
    """All 500 LED refs must appear in the output BOM (possibly across rows)."""
    fab = _make_fake_fab_for_bom(_LED_REFS)
    rows = _read_bom_rows(fab)
    found_refs = []
    for row in rows:
        found_refs.extend(row[1].split(","))
    assert sorted(found_refs) == sorted(_LED_REFS)


def test_generate_bom_500_leds_no_row_exceeds_2048():
    """No Designator field in any output row may exceed 2048 characters."""
    fab = _make_fake_fab_for_bom(_LED_REFS)
    rows = _read_bom_rows(fab)
    for row in rows:
        designator_field = row[1]
        assert len(designator_field) <= _BOM_DESIGNATOR_MAX_LEN, (
            f"Designator field length {len(designator_field)} exceeds {_BOM_DESIGNATOR_MAX_LEN}"
        )


def test_generate_bom_500_leds_quantity_matches_chunk_size():
    """The Quantity column in each row must equal the number of refs in that row."""
    fab = _make_fake_fab_for_bom(_LED_REFS)
    rows = _read_bom_rows(fab)
    for row in rows:
        designators = row[1].split(",")
        quantity = int(row[4])
        assert quantity == len(designators)


def test_generate_bom_500_leds_total_quantity_correct():
    """Sum of Quantity across all rows must equal the total component count."""
    fab = _make_fake_fab_for_bom(_LED_REFS)
    rows = _read_bom_rows(fab)
    total = sum(int(row[4]) for row in rows)
    assert total == N_LEDS


def test_generate_bom_500_leds_multiple_rows_emitted():
    """500 LEDs must produce more than one BOM row (the 2048-char split must fire)."""
    fab = _make_fake_fab_for_bom(_LED_REFS)
    rows = _read_bom_rows(fab)
    assert len(rows) > 1, "Expected BOM to be split into multiple rows"


def test_generate_bom_small_board_stays_single_row():
    """A board with few components (no overflow) must still produce exactly one row."""
    refs = [f"R{i}" for i in range(1, 11)]
    fab = _make_fake_fab_for_bom(refs, lcsc="C25741", value="100k", footprint="R0402")
    rows = _read_bom_rows(fab)
    assert len(rows) == 1
    assert int(rows[0][4]) == 10
