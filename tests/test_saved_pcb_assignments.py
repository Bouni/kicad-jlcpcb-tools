"""Read actual PCB serialization without native loaders or live-board state."""

from collections.abc import Iterator
import importlib
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Optional

import pytest

from .wx_harness import load_siblings

UUID = "074fd983-e570-4259-9550-b7386752b87f"
OTHER_UUID = "bcc4b608-43f5-4e8f-860d-e0885a242eaa"


@pytest.fixture
def reader() -> Iterator[ModuleType]:
    """Load the pure reader without importing the plugin or native KiCad."""
    with load_siblings(
        "_saved_pcb_reader_tests", ("saved_pcb_assignments",), {}
    ) as modules:
        yield modules["saved_pcb_assignments"]


def board(footprints: str = "", version: str = "20260206") -> str:
    """Include representative saved-board sections unrelated to assignments."""
    return f"""(kicad_pcb (version {version}) (generator "pcbnew")
      (general (thickness 1.6)) (paper "A4")
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
      (setup (pad_to_mask_clearance 0))
      {footprints})"""


def modern(extra: str = "", identity: str = UUID) -> str:
    """Match KiCad 8–10's direct property and UUID encoding."""
    return f'''(footprint "Resistor_SMD:R_0603_1608Metric"
      (layer "F.Cu") (uuid "{identity}") (at 10 20)
      (property "Reference" "R1" (at 0 -1) (layer "F.SilkS")
        (uuid "d8e10f41-c1c3-4d7a-86b6-9e422bf984da")
        (effects (font (size 1 1) (thickness 0.15))))
      (property "Value" "10k" (at 0 1) (layer "F.Fab") (hide yes))
      (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask"))
      {extra})'''


def read(reader: ModuleType, tmp_path: Path, source: str) -> tuple:
    """Assert every successful read preserves the exact source bytes."""
    path = tmp_path / "saved.kicad_pcb"
    original = source.encode("utf-8")
    path.write_bytes(original)
    result = reader.read_saved_pcb_assignments(path)
    assert path.read_bytes() == original
    assert isinstance(result, tuple)
    return result


@pytest.mark.parametrize("version", ["20240108", "20241229", "20260206"])
def test_reads_modern_saved_default_and_ignores_variants(
    reader: ModuleType, tmp_path: Path, version: str
) -> None:
    """Variant fields and flags must never become persisted Default evidence."""
    source = board(
        modern("""(property "JLCPCB Part #" " c123 ")
          (property "Footprint" "Other:WrongItem")
          (attr smd exclude_from_pos_files dnp)
          (variant (name "Production")
            (field (name "JLCPCB Part #") (value "C999"))
            (exclude_from_bom yes) (exclude_from_pos_files no))"""),
        version,
    )
    source = source.replace('(layer "F.Cu")', f'(version {version}) (layer "F.Cu")', 1)
    (part,) = read(reader, tmp_path, source)
    assert (part.component_id, part.reference, part.value, part.footprint) == (
        UUID,
        "R1",
        "10k",
        "R_0603_1608Metric",
    )
    assert (part.bom, part.pos, part.lcsc, part.native_value) == (
        True,
        False,
        "C123",
        "C123",
    )
    assert part.assignment.aliases[0].text == " c123 "


def test_reads_kicad7_tstamp_fp_text_and_legacy_text_variables(
    reader: ModuleType, tmp_path: Path
) -> None:
    """Old text variables are converted by KiCad before GetReference/GetValue."""
    source = board(
        f"""(footprint "R_0603" locked (layer "F.Cu") (tstamp {UUID.upper()})
          (fp_text reference locked "R%R" (at 0 0 unlocked) (layer "F.SilkS")
            hide (effects (font (size 1 1))))
          (fp_text value "%V" (at 0 1) (layer "F.Fab"))
          (fp_text user "ignore" (at 0 2) (layer "F.Fab"))
          (property "LCSC" "C456") (attr through_hole exclude_from_bom))""",
        "20221018",
    )
    (part,) = read(reader, tmp_path, source)
    assert (part.component_id, part.reference, part.value, part.footprint) == (
        UUID,
        "R${REFERENCE}",
        "${VALUE}",
        "R_0603",
    )
    assert (part.bom, part.pos, part.native_value) == (False, True, "C456")


@pytest.mark.parametrize(
    ("fields", "status", "value"),
    [
        ("", "missing", None),
        ('(property "LCSC" "")', "empty", ""),
        ('(property "LCSC" "garbage")', "invalid", None),
        ('(property "LCSC" "C123") (property "JLCPCB" "C456")', "conflict", None),
        ('(property "LCSC" "C123") (property "JLCPCB" "   ")', "valid", None),
        ('(property "LCSC" "") (property "JLCPCB" "C123")', "conflict", None),
    ],
)
def test_preserves_missing_clear_invalid_and_alias_evidence(
    reader: ModuleType, tmp_path: Path, fields: str, status: str, value: Optional[str]
) -> None:
    """Preserve assignment provenance through the shared native resolver."""
    (part,) = read(reader, tmp_path, board(modern(fields)))
    assert part.assignment.status == status
    assert part.native_value == value


@pytest.mark.parametrize(
    ("attrs", "bom", "pos"),
    [
        ("", True, True),
        ("(attr board_only)", True, True),
        ("(attr smd dnp)", True, True),
        ("(attr virtual)", False, False),
        ("(attr through_hole exclude_from_bom)", False, True),
        ("(attr smd exclude_from_pos_files)", True, False),
        ("(attr smd exclude_from_bom exclude_from_pos_files)", False, False),
        ("(attr allow_missing_courtyard allow_soldermask_bridges)", True, True),
    ],
)
def test_exact_bom_pos_flags(
    reader: ModuleType, tmp_path: Path, attrs: str, bom: bool, pos: bool
) -> None:
    """DNP and board-only metadata must not imply BOM or position exclusions."""
    (part,) = read(reader, tmp_path, board(modern(attrs)))
    assert (part.bom, part.pos) == (bom, pos)


def test_decodes_kicad_strings_comments_and_literal_item_placeholders(
    reader: ModuleType, tmp_path: Path
) -> None:
    """Use native string escapes while retaining literal library placeholders."""
    source = board(modern(r'(property "LCSC" "C\x31\0623")'))
    source = source.replace(
        "Resistor_SMD:R_0603_1608Metric", r"Library\x3aμ/R{colon}x{slash} y"
    )
    source = source.replace('"10k"', r'"quoted \"value\" (x) \u1234 \303\251"')
    source = '# comment ( ignored "\n' + source.replace(
        '(property "Reference"', '# comment )\n(property "Reference"'
    )
    (part,) = read(reader, tmp_path, source)
    assert part.footprint == "μ/R{colon}x{slash} y"
    assert part.value == 'quoted "value" (x) \\u1234 é'
    assert part.lcsc == "C123"


def test_explicit_empty_tuple_fields_are_not_invented(
    reader: ModuleType, tmp_path: Path
) -> None:
    """Explicitly saved empty tuple values remain valid, exact evidence."""
    source = modern().replace('"Resistor_SMD:R_0603_1608Metric"', '""')
    source = source.replace('"R1"', '""').replace('"10k"', '""')
    (part,) = read(reader, tmp_path, board(source))
    assert (part.reference, part.value, part.footprint) == ("", "", "")


def test_empty_board_has_an_empty_inventory(reader: ModuleType, tmp_path: Path) -> None:
    """An actual empty saved board has no recoverable footprint owners."""
    assert read(reader, tmp_path, board()) == ()


@pytest.mark.parametrize(
    "source",
    [
        "",
        "(kicad_pcb)",
        "(kicad_sch (version 20260206))",
        board() + board(),
        "\ufeff" + board(),
        "junk " + board(),
        board() + ' "junk"',
        board().replace("(version 20260206)", "(version 20260206) stray"),
        board().replace("(version 20260206)", "(version 20260206) (version 20260206)"),
        board(version="20211014"),
        board(version="20991231"),
        board(version="abc"),
        board(modern())[:-2],
        board(modern()) + ")",
        board(modern()).replace('"10k"', '"bad'),
        board(modern()).replace('"10k"', '"two\nlines"'),
        board(modern()).replace('"10k"', '"null\0value"'),
        board(modern()).replace('"10k"', r'"null\x00value"'),
        board(modern()).replace('"10k"', "unquoted|value"),
        board(modern(r'(property "LCSC" "C123\000")')),
        board(modern().replace("(footprint ", "(module ")),
        board(modern().replace("(footprint ", "(footprints ")),
        board("(unknown_inventory " + modern() + ")"),
        board(modern().replace(f'(uuid "{UUID}")', "")),
        board(modern(identity="component-1")),
        board(modern(identity="12345678")),
        board(modern(identity="00000000-0000-0000-0000-000000000000")),
        board(modern(f"(tstamp {UUID})")),
        board(modern() + modern(identity=UUID.upper())),
        board(modern().replace('(property "Reference" "R1"', '(property "Other" "R1"')),
        board(modern().replace('(property "Value" "10k"', '(property "Other" "10k"')),
        board(modern('(property "Reference" "R2")')),
        board(modern('(fp_text reference "R1" (at 0 0))')),
        board(modern('(property "LCSC" "C123") (property "LCSC" "C456")')),
        board(modern('(property "LCSC")')),
        board(modern('(property "LCSC" "C123" "extra")')),
        board(modern("(attr smd) (attr exclude_from_bom)")),
        board(modern("(attr unknown_future_flag)")),
        board(modern("(attr (exclude_from_bom yes))")),
        board(modern("(attributes exclude_from_bom)")),
        board(modern("(version 20991231)")),
        board(modern("(version 20260206) (version 20260206)")),
        board(modern().replace("Resistor_SMD:R_0603_1608Metric", "Library:R:0603")),
        board(modern().replace("Resistor_SMD:R_0603_1608Metric", r"Library:R\\0603")),
        board(modern().replace("Resistor_SMD:R_0603_1608Metric", r"Library:R\n0603")),
    ],
)
def test_rejects_unusable_inventory_without_partial_results(
    reader: ModuleType, tmp_path: Path, source: str
) -> None:
    """Malformed or unsupported evidence cannot authorize retirement."""
    path = tmp_path / "broken.kicad_pcb"
    original = source.encode("utf-8")
    path.write_bytes(original)
    with pytest.raises(ValueError):
        reader.read_saved_pcb_assignments(path)
    assert path.read_bytes() == original


def test_invalid_second_footprint_rejects_whole_board(
    reader: ModuleType, tmp_path: Path
) -> None:
    """One readable footprint does not make an incomplete inventory safe."""
    path = tmp_path / "partial.kicad_pcb"
    path.write_text(board(modern() + modern(identity="bad")), encoding="utf-8")
    with pytest.raises(ValueError):
        reader.read_saved_pcb_assignments(path)


def test_io_errors_do_not_become_empty_boards(
    reader: ModuleType, tmp_path: Path
) -> None:
    """Unreadable sources retain recovery rather than proving no owners."""
    with pytest.raises(OSError):
        reader.read_saved_pcb_assignments(tmp_path / "missing.kicad_pcb")
    path = tmp_path / "undecodable.kicad_pcb"
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(UnicodeError):
        reader.read_saved_pcb_assignments(path)


def test_returned_inventory_is_detached_from_later_disk_changes(
    reader: ModuleType, tmp_path: Path
) -> None:
    """Each read depends on saved bytes and returns independent immutable values."""
    path = tmp_path / "saved.kicad_pcb"
    path.write_text(board(modern('(property "LCSC" "C123")')), encoding="utf-8")
    original = reader.read_saved_pcb_assignments(path)
    path.write_text(board(modern('(property "LCSC" "C456")')), encoding="utf-8")
    assert original[0].native_value == "C123"
    assert reader.read_saved_pcb_assignments(path)[0].native_value == "C456"


@pytest.mark.parametrize("malformed_attr", [False, True])
def test_unsupported_sibling_inventory_cannot_prove_legacy_row_obsolete(
    reader: ModuleType, tmp_path: Path, malformed_attr: bool
) -> None:
    """Unknown inventory or flags must block real persisted-coverage eligibility."""
    coverage = importlib.import_module(f"{reader.__package__}.legacy_board_coverage")
    current = tmp_path / "current.kicad_pcb"
    current.write_text(board(), encoding="utf-8")
    sibling = tmp_path / "sibling.kicad_pcb"
    source = modern("(attr exclude_from_bom)")
    source = (
        source.replace("(attr ", "(attributes ")
        if malformed_attr
        else source.replace("(footprint ", "(footprints ")
    )
    sibling.write_text(board(source), encoding="utf-8")
    row = SimpleNamespace(
        reference="R1",
        lcsc="C123",
        status="obsolete",
        component_ids=(),
        reason="no exact live match",
        native_value=None,
        identity=("R1", "10k", "R_0603_1608Metric", True, False),
    )
    result = coverage.collect_saved_board_coverage(
        str(current), (), (row,), load_board=reader.read_saved_pcb_assignments
    )
    assert not result.eligible
    assert result.diagnostics
