"""Ordinary absent assignments do not masquerade as failed schematic saves."""

from pathlib import Path
from typing import Optional

import pytest

from .test_schematicexport import _load_schematic, _part, _schematic


def _missing(reference: str = "R1") -> dict[str, object]:
    return {
        **_part(reference, "", False),
        "assignment_status": "missing",
        "fields": {},
        "component_id": "fp-one",
        "schematic_path": "/root-R1/symbol-R1",
    }


@pytest.mark.parametrize("schematic_value", [None, ""])
def test_absent_board_assignment_with_no_schematic_value_is_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schematic_value: Optional[str]
) -> None:
    """Routine missing fields preserve bytes without manufacturing a problem."""
    path = tmp_path / "board.kicad_sch"
    text = _schematic(8, "yes", ("R1",), reference="R1")
    if schematic_value is None:
        text = text.replace('    (property "LCSC" "OLD"\n      (at 0 0 0)\n    )\n', "")
    else:
        text = text.replace('"LCSC" "OLD"', '"LCSC" ""')
    path.write_text(text, encoding="utf-8")

    outcome = _load_schematic(tmp_path, monkeypatch, 8, [path], [_missing()])

    assert path.read_text(encoding="utf-8") == text
    assert outcome.preserved == ("fp-one",)
    assert outcome.retirement_eligible
    assert not outcome.diagnostics
    assert not outcome.advisory
    assert outcome.information


def test_missing_board_field_with_schematic_value_advises_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Update PCB advice preserves the assignment and permits safe retirement."""
    path = tmp_path / "board.kicad_sch"
    text = _schematic(8, "yes", ("R1",), reference="R1").replace(
        '"LCSC" "OLD"', '"LCSC" "C900"'
    )
    path.write_text(text, encoding="utf-8")

    outcome = _load_schematic(tmp_path, monkeypatch, 8, [path], [_missing()])

    assert path.read_text(encoding="utf-8") == text
    assert outcome.retirement_eligible and not outcome.diagnostics
    assert len(outcome.advisory) == 1
    assert "Update PCB" in outcome.advisory[0]


@pytest.mark.parametrize("link", ["", "/"])
def test_pcb_only_footprint_is_routine_and_does_not_block_linked_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, link: str
) -> None:
    """Unlinked mounting hardware does not interrupt independent linked saves."""
    path = tmp_path / "board.kicad_sch"
    path.write_text(_schematic(8, "yes", ("R1",), reference="R1"), encoding="utf-8")
    rows = [
        {
            **_part("R1", "C100", False),
            "component_id": "linked",
            "schematic_path": "/root-R1/symbol-R1",
        },
        {**_missing("REF**"), "schematic_path": link},
    ]

    outcome = _load_schematic(tmp_path, monkeypatch, 8, [path], rows)

    assert outcome.retirement_eligible and not outcome.diagnostics
    assert outcome.saved == ("linked",)
    assert outcome.preserved == ("fp-one",)
    assert outcome.information and not outcome.advisory


def test_nonempty_stale_link_remains_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken former association remains different from a PCB-only footprint."""
    path = tmp_path / "board.kicad_sch"
    path.write_text(_schematic(8, "yes", ("R1",), reference="R1"), encoding="utf-8")
    row = {**_missing(), "schematic_path": "/root-R1/deleted-symbol"}

    outcome = _load_schematic(tmp_path, monkeypatch, 8, [path], [row])

    assert not outcome.retirement_eligible
    assert outcome.unresolved == ("fp-one",)
    assert outcome.diagnostics


def test_missing_board_field_does_not_hide_unsafe_schematic_assignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unsafe existing schematic aliases remain actionable despite PCB absence."""
    path = tmp_path / "board.kicad_sch"
    original = _schematic(8, "yes", ("R1",), reference="R1")
    path.write_text(original, encoding="utf-8")

    outcome = _load_schematic(tmp_path, monkeypatch, 8, [path], [_missing()])

    assert not outcome.retirement_eligible and outcome.diagnostics
    assert path.read_text(encoding="utf-8") == original
