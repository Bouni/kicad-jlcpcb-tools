"""Exercise complete component writes through the public schematic exporter."""

from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any

import pytest

from tests.test_schematic_components import _library, _records, _sheet, _symbol, _write
from tests.test_schematicexport import SchematicExport, _module


def _row(
    identity: str,
    path: str,
    value: str = "C900",
    excluded: bool = True,
    status: str = "valid",
) -> dict[str, object]:
    """Capture a PCB record with an explicit link and an untrusted display label."""
    return {
        "component_id": identity,
        "schematic_path": path,
        "reference": "PCB annotation differs",
        "lcsc": value,
        "exclude_from_bom": excluded,
        "assignment_status": status,
    }


def _export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    paths: list[Path],
    rows: list[dict[str, object]],
    **options: Any,
) -> Any:
    """Exercise snapshot capture, index construction, decisions, and real writes."""
    parent = SimpleNamespace(
        board_name="board.kicad_pcb", project_path=str(tmp_path), pcbnew=None
    )
    monkeypatch.setattr(_module, "GetBuildVersion", lambda: "10.0")
    monkeypatch.setattr(_module, "is_version7", lambda _: False)
    return SchematicExport(parent).load_schematic(
        [str(path) for path in paths], parts=rows, **options
    )


def _values(path: Path) -> list[str]:
    """Read the fixture's direct assignment values without production parsing."""
    return re.findall(r'\(property "LCSC" "([^"]*)"', path.read_text(encoding="utf-8"))


def _bom(path: Path) -> list[str]:
    """Read the fixture's direct BOM states; its cache has no BOM properties."""
    return re.findall(r"\(in_bom (yes|no)\)", path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("anchor", ["/root/unit1", "/root/unit2", "/unit3"])
def test_one_footprint_updates_every_placed_unit_and_reports_it_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, anchor: str
) -> None:
    """Linking any unit must update all three physical units and both fields."""
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        *(_symbol(f"unit{unit}", unit) for unit in (1, 2, 3)),
    )
    original = root.read_text(encoding="utf-8")

    outcome = _export(tmp_path, monkeypatch, [root], [_row("fp", anchor)])

    assert _values(root) == ["C900", "C900", "C900"]
    assert _bom(root) == ["no", "no", "no"]
    assert outcome.saved == ("fp",)
    assert outcome.bom_saved == ("fp",)
    assert outcome.retirement_eligible
    assert not outcome.diagnostics
    assert root.with_name(root.name + "_old").read_text(encoding="utf-8") == original


def test_only_actually_placed_units_need_a_footprint_contribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unused library units are not missing PCB links."""
    root = _write(tmp_path / "root.kicad_sch", "root", _symbol("a", 1), _symbol("c", 3))

    outcome = _export(tmp_path, monkeypatch, [root], [_row("fp", "/root/c")])

    assert _values(root) == ["C900", "C900"]
    assert _bom(root) == ["no", "no"]
    assert outcome.retirement_eligible


def test_component_assignment_and_bom_reach_units_in_other_sheets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One component is updated across files in its actual project hierarchy."""
    child = _write(
        tmp_path / "child.kicad_sch",
        "old-child-root",
        _symbol("b", 2, records=_records(("/root/placed", "U104", 2))),
        _symbol("c", 3, records=_records(("/root/placed", "U104", 3))),
    )
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _symbol("a", 1, records=_records(("/root", "U104", 1))),
        _sheet("placed", child.name),
    )

    outcome = _export(tmp_path, monkeypatch, [root], [_row("fp", "/placed/b")])

    assert _values(root) == ["C900"]
    assert _values(child) == ["C900", "C900"]
    assert _bom(root) == ["no"]
    assert _bom(child) == ["no", "no"]
    assert outcome.saved == outcome.bom_saved == ("fp",)
    assert outcome.retirement_eligible


def _reused_component(tmp_path: Path) -> tuple[Path, Path]:
    """Place the same three physical units as separately annotated components."""
    child = _write(
        tmp_path / "shared.kicad_sch",
        "child-root",
        *(
            _symbol(
                f"unit{unit}",
                unit,
                records=_records(
                    ("/root/left", "U104", unit),
                    ("/root/right", "U105", unit),
                ),
            )
            for unit in (1, 2, 3)
        ),
    )
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _sheet("left", child.name),
        _sheet("right", child.name),
    )
    return root, child


@pytest.mark.parametrize("second", ["C900", "C901"])
def test_reused_component_assignment_consensus_is_independent_of_bom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, second: str
) -> None:
    """All real occurrences contribute exactly once regardless of linked unit."""
    root, child = _reused_component(tmp_path)
    rows = [
        _row("left", "/root/left/unit1"),
        _row("right", "/right/unit3", second),
    ]

    outcome = _export(tmp_path, monkeypatch, [root], rows)

    assert _values(child) == ["C900" if second == "C900" else "C6961"] * 3
    assert _bom(child) == ["no"] * 3
    assert set(outcome.bom_saved) == {"left", "right"}
    assert len(outcome.bom_saved) == 2
    assert outcome.retirement_eligible is (second == "C900")
    if second == "C900":
        assert set(outcome.saved) == {"left", "right"}
        assert len(outcome.saved) == 2
    else:
        assert not outcome.saved
        assert all(
            any(f"[{fp}]" in item for item in outcome.diagnostics)
            for fp in ("left", "right")
        )


def test_reused_bom_disagreement_does_not_prevent_assignment_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Separate field decisions allow a complete agreed assignment to persist."""
    root, child = _reused_component(tmp_path)

    outcome = _export(
        tmp_path,
        monkeypatch,
        [root],
        [
            _row("left", "/root/left/unit1"),
            _row("right", "/root/right/unit2", excluded=False),
        ],
    )

    assert _values(child) == ["C900"] * 3
    assert _bom(child) == ["yes"] * 3
    assert set(outcome.saved) == {"left", "right"}
    assert not outcome.bom_saved
    assert outcome.diagnostics


def _overlapping_components(tmp_path: Path) -> tuple[Path, Path]:
    """Two components have unique unit ones and share a physical unit two."""
    child = _write(
        tmp_path / "shared.kicad_sch",
        "child-root",
        _symbol(
            "shared",
            2,
            records=_records(("/root/left", "U104", 2), ("/root/right", "U105", 2)),
        ),
    )
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _symbol("exclusive-a", 1, reference="U104"),
        _symbol("exclusive-b", 1, reference="U105"),
        _sheet("left", child.name),
        _sheet("right", child.name),
    )
    return root, child


@pytest.mark.parametrize("anchor", ["/root/exclusive-a", "/root/left/shared"])
def test_missing_reused_peer_preserves_every_member_including_exclusive_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, anchor: str
) -> None:
    """No component may be partly written before discovering a missing peer."""
    root, child = _overlapping_components(tmp_path)
    originals = {path: path.read_text(encoding="utf-8") for path in (root, child)}

    outcome = _export(tmp_path, monkeypatch, [root], [_row("left", anchor)])

    for path, original in originals.items():
        assert path.read_text(encoding="utf-8") == original
    assert not outcome.saved
    assert not outcome.bom_saved
    assert not outcome.retirement_eligible
    assert any("[left]" in item for item in outcome.diagnostics)


def test_assignment_disagreement_preserves_exclusive_units_but_bom_can_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shared-target disagreement cannot leave either component partly assigned."""
    root, child = _overlapping_components(tmp_path)

    outcome = _export(
        tmp_path,
        monkeypatch,
        [root],
        [
            _row("left", "/root/exclusive-a"),
            _row("right", "/root/exclusive-b", value="C901"),
        ],
    )

    assert _values(root) == ["C6961", "C6961"]
    assert _values(child) == ["C6961"]
    assert _bom(root) == ["no", "no"]
    assert _bom(child) == ["no"]
    assert not outcome.saved
    assert set(outcome.bom_saved) == {"left", "right"}
    assert not outcome.retirement_eligible


def test_missing_peer_propagates_through_transitively_overlapping_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A overlaps B, B overlaps absent C: neither A nor B may be partly written."""
    first_shared = _write(
        tmp_path / "first-shared.kicad_sch",
        "first-child",
        _symbol(
            "shared-two",
            2,
            records=_records(
                ("/root/first-a", "U104", 2),
                ("/root/first-b", "U105", 2),
            ),
        ),
    )
    second_shared = _write(
        tmp_path / "second-shared.kicad_sch",
        "second-child",
        _symbol(
            "shared-three",
            3,
            records=_records(
                ("/root/second-b", "U105", 3),
                ("/root/second-c", "U106", 3),
            ),
        ),
    )
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _symbol("exclusive-a", 1, reference="U104"),
        _symbol("exclusive-b", 1, reference="U105"),
        _symbol("exclusive-c", 1, reference="U106"),
        _symbol("safe", 1, reference="U200"),
        _sheet("first-a", first_shared.name),
        _sheet("first-b", first_shared.name),
        _sheet("second-b", second_shared.name),
        _sheet("second-c", second_shared.name),
    )

    outcome = _export(
        tmp_path,
        monkeypatch,
        [root],
        [
            _row("a", "/root/exclusive-a"),
            _row("b", "/root/exclusive-b"),
            _row("safe", "/root/safe", value="C950"),
        ],
    )

    assert _values(root) == ["C6961", "C6961", "C6961", "C950"]
    assert _bom(root) == ["yes", "yes", "yes", "no"]
    for path in (first_shared, second_shared):
        assert _values(path) == ["C6961"]
        assert _bom(path) == ["yes"]
    assert outcome.saved == outcome.bom_saved == ("safe",)
    assert not outcome.retirement_eligible
    assert all(
        any(f"[{fp}]" in item for item in outcome.diagnostics) for fp in ("a", "b")
    )


def test_duplicate_footprints_linking_different_units_do_not_claim_one_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two UUIDs claiming one package are ambiguous even when their values agree."""
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _symbol("a", 1),
        _symbol("b", 2),
        _symbol("safe", 1, reference="U106"),
    )

    outcome = _export(
        tmp_path,
        monkeypatch,
        [root],
        [
            _row("duplicate-a", "/root/a"),
            _row("duplicate-b", "/root/b"),
            _row("safe", "/root/safe", value="C901"),
        ],
    )

    assert _values(root) == ["C6961", "C6961", "C901"]
    assert _bom(root) == ["yes", "yes", "no"]
    assert outcome.saved == outcome.bom_saved == ("safe",)
    assert all(
        any(f"[{fp}]" in item for item in outcome.diagnostics)
        for fp in ("duplicate-a", "duplicate-b")
    )
    assert not outcome.retirement_eligible


@pytest.mark.parametrize("status", ["missing", "invalid", "conflict"])
def test_unsafe_assignment_preserves_every_unit_and_still_saves_bom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """Unsafe provenance affects assignment only; a separate safe package saves."""
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _symbol("a", 1),
        _symbol("b", 2),
        _symbol("safe-a", 1, reference="U106"),
        _symbol("safe-b", 2, reference="U106"),
    )
    rows = [
        _row("unsafe", "/root/b", value="", status=status),
        _row("safe", "/root/safe-a", value="C901"),
    ]

    outcome = _export(tmp_path, monkeypatch, [root], rows)

    assert _values(root) == ["C6961", "C6961", "C901", "C901"]
    assert _bom(root) == ["no"] * 4
    assert outcome.saved == ("safe",)
    assert set(outcome.bom_saved) == {"safe", "unsafe"}
    assert len(outcome.bom_saved) == 2
    if status == "missing":
        assert outcome.preserved == ("unsafe",)
        assert not outcome.diagnostics
    else:
        assert "unsafe" in (*outcome.skipped, *outcome.preserved)
        assert any("[unsafe]" in item for item in outcome.diagnostics)
        assert not outcome.retirement_eligible


def test_missing_pcb_assignment_reports_conflicting_schematic_unit_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Conflict preservation must not recommend copying two competing IDs to PCB."""
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _symbol("a", 1),
        _symbol("b", 2).replace('"LCSC" "C6961"', '"LCSC" "C6962"'),
    )

    outcome = _export(
        tmp_path,
        monkeypatch,
        [root],
        [_row("missing", "/root/a", value="", status="missing")],
    )

    assert _values(root) == ["C6961", "C6962"]
    assert _bom(root) == ["no", "no"]
    assert outcome.preserved == outcome.bom_saved == ("missing",)
    assert not outcome.saved
    assert not outcome.retirement_eligible
    assert any(
        "[missing]" in item and "disagree" in item for item in outcome.diagnostics
    )
    assert not any("Update PCB" in item for item in outcome.advisory)


def test_explicit_clear_reaches_all_existing_aliases_on_every_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clear must not leave an old alias capable of resurrecting the assignment."""
    symbols = [
        _symbol(f"unit{unit}", unit).replace(
            '(property "LCSC" "C6961")',
            '(property "LCSC" "C6961") (property "JLCPCBPartNr" "C6961")',
        )
        for unit in (1, 2, 3)
    ]
    root = _write(tmp_path / "root.kicad_sch", "root", *symbols)

    outcome = _export(
        tmp_path,
        monkeypatch,
        [root],
        [_row("clear", "/root/unit2", value="", status="empty")],
    )

    assert _values(root) == [""] * 3
    assert (
        re.findall(
            r'\(property "JLCPCBPartNr" "([^"]*)"', root.read_text(encoding="utf-8")
        )
        == [""] * 3
    )
    assert _bom(root) == ["no"] * 3
    assert outcome.saved == ("clear",)
    assert outcome.retirement_eligible


@pytest.mark.parametrize("shared_project", [False, True])
def test_multiple_top_roots_share_components_only_when_authenticated_as_one_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shared_project: bool
) -> None:
    """Selecting unrelated roots does not authorize merging equal references."""
    first = _write(tmp_path / "one.kicad_sch", "one", _symbol("a", 1))
    second = _write(tmp_path / "two.kicad_sch", "two", _symbol("b", 2))

    outcome = _export(
        tmp_path,
        monkeypatch,
        [first, second],
        [_row("fp", "/one/a")],
        shared_project=shared_project,
    )

    assert _values(first) == ["C900"]
    assert _bom(first) == ["no"]
    assert _values(second) == ["C900" if shared_project else "C6961"]
    assert _bom(second) == ["no" if shared_project else "yes"]
    assert outcome.saved == outcome.bom_saved == ("fp",)
    assert outcome.retirement_eligible


@pytest.mark.parametrize(
    "cached_anchor", [True, False], ids=["one-unit-cache", "no-cache"]
)
def test_unknown_edited_sibling_prevents_apparent_single_unit_partial_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cached_anchor: bool
) -> None:
    """Local cache unit counts cannot hide a related unit with unknown annotation."""
    child = _write(
        tmp_path / "child.kicad_sch",
        "child-root",
        _symbol(
            "b",
            2,
            records=_records(("/foreign-root", "U999", 2)),
            lib_name="Edited_B",
        ),
        libraries=_library("Edited_B", 2),
    )
    libraries = _library("Device:R", 1)
    if cached_anchor:
        libraries += _library("Edited_A", 1)
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _symbol("a", 1, lib_name="Edited_A"),
        _symbol("safe", 1, reference="R1", lib_id="Device:R"),
        _sheet("placed", child.name),
        libraries=libraries,
    )
    child_original = child.read_text(encoding="utf-8")

    outcome = _export(
        tmp_path,
        monkeypatch,
        [root],
        [_row("uncertain", "/root/a"), _row("safe", "/root/safe", value="C901")],
    )

    assert _values(root) == ["C6961", "C901"]
    assert _bom(root) == ["yes", "no"]
    assert child.read_text(encoding="utf-8") == child_original
    assert outcome.saved == outcome.bom_saved == ("safe",)
    assert not outcome.retirement_eligible
    assert any("[uncertain]" in item for item in outcome.diagnostics)


@pytest.mark.parametrize(
    "unit,records",
    [
        (0, ""),
        (1, _records(("/root", "U104", 1), ("/root", "U104", 2))),
    ],
    ids=["explicit-zero", "conflicting-current-units"],
)
def test_invalid_single_unit_metadata_preserves_both_fields_and_recovery_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unit: int, records: str
) -> None:
    """An apparently single-unit cache cannot authorize malformed placed data."""
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _symbol("a", unit, records=records),
        libraries=_library(count=1),
    )
    original = root.read_text(encoding="utf-8")

    outcome = _export(tmp_path, monkeypatch, [root], [_row("invalid", "/root/a")])

    assert root.read_text(encoding="utf-8") == original
    assert not outcome.saved
    assert not outcome.bom_saved
    assert not outcome.retirement_eligible
    assert any("[invalid]" in item for item in outcome.diagnostics)


def test_cacheless_anchor_in_multiunit_family_preserves_while_other_component_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The known family proves cache validation is needed even for another ref."""
    child = _write(
        tmp_path / "child.kicad_sch",
        "child-root",
        _symbol(
            "b",
            2,
            reference="U105",
            records=_records(("/root/placed", "U105", 2)),
            lib_name="Edited_B",
        ),
        libraries=_library("Edited_B", 2),
    )
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _symbol("a", 1, lib_name="Missing_A"),
        _sheet("placed", child.name),
        libraries=_library("Unrelated", 1),
    )
    original = root.read_text(encoding="utf-8")

    outcome = _export(
        tmp_path,
        monkeypatch,
        [root],
        [_row("uncertain", "/root/a"), _row("safe", "/root/placed/b", value="C901")],
    )

    assert root.read_text(encoding="utf-8") == original
    assert _values(child) == ["C901"]
    assert _bom(child) == ["no"]
    assert outcome.saved == outcome.bom_saved == ("safe",)
    assert not outcome.retirement_eligible
    assert any("[uncertain]" in item for item in outcome.diagnostics)


@pytest.mark.parametrize("provide_roots", [False, True])
def test_project_declared_top_root_ids_authenticate_cross_root_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provide_roots: bool
) -> None:
    """Project sheet UUIDs can differ from the top-level files' own root UUIDs."""
    first = _write(
        tmp_path / "one.kicad_sch",
        "file-one",
        _symbol("a", 1, records=_records(("/project-one", "U104", 1))),
    )
    second = _write(
        tmp_path / "two.kicad_sch",
        "file-two",
        _symbol("b", 2, records=_records(("/project-two", "U104", 2))),
    )
    roots = {str(first): "project-one", str(second): "project-two"}

    outcome = _export(
        tmp_path,
        monkeypatch,
        [first, second],
        [_row("fp", "/project-two/b")],
        shared_project=True,
        root_uuids=roots if provide_roots else None,
    )

    for path in (first, second):
        assert _values(path) == ["C900" if provide_roots else "C6961"]
        assert _bom(path) == ["no" if provide_roots else "yes"]
    assert outcome.saved == outcome.bom_saved == (("fp",) if provide_roots else ())
    assert outcome.retirement_eligible is provide_roots


@pytest.mark.parametrize("conflicting_current", [True, False])
def test_conflicting_current_unit_sibling_blocks_the_other_single_unit_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conflicting_current: bool
) -> None:
    """A conflicted member may not be dropped before authenticating its peer."""
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _symbol("a", 1),
        _symbol(
            "b",
            1,
            records=_records(
                ("/root", "U104", 1),
                ("/root" if conflicting_current else "/foreign", "U104", 2),
            ),
        ),
        _symbol("safe", 1, reference="R1", lib_id="Device:R"),
        libraries=_library(count=1) + _library("Device:R", 1),
    )

    outcome = _export(
        tmp_path,
        monkeypatch,
        [root],
        [
            _row("a", "/root/a"),
            _row("b", "/root/b", value="C902", excluded=False),
            _row("safe", "/root/safe", value="C901"),
        ],
    )

    assert _values(root) == (
        ["C6961", "C6961", "C901"] if conflicting_current else ["C900", "C902", "C901"]
    )
    assert _bom(root) == (
        ["yes", "yes", "no"] if conflicting_current else ["no", "yes", "no"]
    )
    expected = {"safe"} if conflicting_current else {"a", "b", "safe"}
    assert set(outcome.saved) == set(outcome.bom_saved) == expected
    assert outcome.retirement_eligible is not conflicting_current


def test_known_single_duplicates_export_independently_of_other_multiunit_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unrelated multiunit member in the family does not merge true single units."""
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _symbol("a", 1, lib_name="Single"),
        _symbol("b", 1, lib_name="Single"),
        _symbol("multi", 2, reference="U105", lib_name="Multi"),
        libraries=_library("Single", 1) + _library("Multi", 2),
    )

    outcome = _export(
        tmp_path,
        monkeypatch,
        [root],
        [
            _row("a", "/root/a"),
            _row("b", "/root/b", value="C901", excluded=False),
            _row("multi", "/root/multi", value="C902"),
        ],
    )

    assert _values(root) == ["C900", "C901", "C902"]
    assert _bom(root) == ["no", "yes", "no"]
    assert set(outcome.saved) == set(outcome.bom_saved) == {"a", "b", "multi"}
    assert outcome.retirement_eligible
