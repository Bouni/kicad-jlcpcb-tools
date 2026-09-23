"""Authenticate all placed units of a component from real hierarchy instances."""

import importlib
from pathlib import Path
from types import ModuleType

import pytest

from tests.wx_harness import temporary_modules

_ROOT = Path(__file__).parent.parent
_PACKAGE = ModuleType("schematic_components_test_plugin")
_PACKAGE.__path__ = [str(_ROOT)]
with temporary_modules(
    {"schematic_components_test_plugin": _PACKAGE},
    namespaces=("schematic_components_test_plugin",),
):
    SchematicIndex = importlib.import_module(
        "schematic_components_test_plugin.schematic_links"
    ).SchematicIndex


def _library(name: str = "Amplifier:Triple", count: int = 3) -> str:
    """Describe the cache's actual numbered unit definitions."""
    units = " ".join(f'(symbol "Triple_{unit}_1")' for unit in range(1, count + 1))
    return f'(symbol "{name}" {units})'


def _records(*entries: tuple[str, str, int], project: str = "stale-name") -> str:
    """Use sheet-instance paths, which deliberately exclude the symbol UUID."""
    paths = " ".join(
        f'(path "{path}" (reference "{reference}") (unit {unit}))'
        for path, reference, unit in entries
    )
    return f'(instances (project "{project}" {paths}))'


def _symbol(
    uuid: str,
    unit: int,
    reference: str = "U104",
    records: str = "",
    lib_id: str = "Amplifier:Triple",
    lib_name: str = "",
) -> str:
    """Create a placed unit with independently specified base and instance data."""
    binding = f'(lib_name "{lib_name}")' if lib_name else ""
    return (
        f'(symbol (lib_id "{lib_id}") {binding} (uuid "{uuid}") '
        f'(unit {unit}) (in_bom yes) (property "Reference" "{reference}") '
        f'(property "LCSC" "C6961") {records})'
    )


def _sheet(uuid: str, filename: str) -> str:
    """Place a child using a UUID unrelated to its file's root UUID."""
    return f'(sheet (uuid "{uuid}") (property "Sheetfile" "{filename}"))'


def _write(path: Path, uuid: str, *children: str, libraries: str = "") -> Path:
    """Write a complete schematic with a small real symbol cache."""
    path.write_text(
        f'(kicad_sch (version 20250114) (uuid "{uuid}") '
        f"(lib_symbols {libraries or _library()}) {' '.join(children)})",
        encoding="utf-8",
    )
    return path


def test_every_unit_anchor_resolves_the_same_complete_component(tmp_path: Path) -> None:
    """The linked unit is an anchor, not the only symbol eligible for updating."""
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        *(_symbol(f"unit{unit}", unit) for unit in (1, 2, 3)),
    )
    index = SchematicIndex.from_paths([str(root)])
    expected = tuple(f"/root/unit{unit}" for unit in (1, 2, 3))

    for anchor in ("/root/unit1", "/root/unit2", "/unit3"):
        component = index.resolve_component(anchor)
        assert component.resolved
        assert component.issues == ()
        assert component.key == expected
        assert tuple(member.path for member in component.members) == expected
        assert {member.reference for member in component.members} == {"U104"}
        assert {member.unit for member in component.members} == {1, 2, 3}
        assert {member.target.lcsc for member in component.members} == {"C6961"}


def test_exact_instance_reference_and_unit_override_stale_base_fields(
    tmp_path: Path,
) -> None:
    """Current paths authenticate grouping even after copied-project annotation."""
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        *(
            _symbol(
                f"unit{unit}",
                1,
                reference=f"U{900 + unit}",
                records=_records(
                    ("/foreign-root", "U999", 3),
                    ("/root", "U105", unit),
                    project=f"old-project-{unit}",
                ),
            )
            for unit in (1, 2, 3)
        ),
    )
    component = SchematicIndex.from_paths([str(root)]).resolve_component("/unit2")

    assert component.resolved
    assert {member.reference for member in component.members} == {"U105"}
    assert {member.unit for member in component.members} == {1, 2, 3}


@pytest.mark.parametrize(
    "current_record",
    [
        '(path "/root" (reference "U104"))',
        '(path "/root" (unit 1))',
        '(path "/root" (reference "U104") (unit 0))',
        '(path "/root" (reference "U104") (unit 4))',
    ],
    ids=["missing-unit", "missing-reference", "zero-unit", "out-of-range-unit"],
)
def test_current_instance_requires_a_valid_reference_and_unit(
    tmp_path: Path, current_record: str
) -> None:
    """A valid base field cannot repair incomplete or invalid current metadata."""
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _symbol("a", 2),
        _symbol("b", 1, records=f'(instances (project "old" {current_record}))'),
    )
    index = SchematicIndex.from_paths([str(root)])

    for path in ("/root/a", "/root/b"):
        component = index.resolve_component(path)
        assert not component.resolved
        assert component.issues


def test_identical_current_records_in_different_projects_are_unambiguous(
    tmp_path: Path,
) -> None:
    """Stale project names do not make agreeing current-path evidence unsafe."""
    records = (
        '(instances (project "old" (path "/root" (reference "U104") (unit 1))) '
        '(project "renamed" (path "/root" (reference "U104") (unit 1))))'
    )
    root = _write(tmp_path / "root.kicad_sch", "root", _symbol("a", 3, records=records))
    component = SchematicIndex.from_paths([str(root)]).resolve_component("/root/a")

    assert component.resolved
    assert component.members[0].unit == 1


@pytest.mark.parametrize("second", [("U999", 1), ("U104", 2)])
def test_conflicting_current_records_preserve_the_entire_component(
    tmp_path: Path, second: tuple[str, int]
) -> None:
    """Neither reference nor unit disagreements may silently discard a sibling."""
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _symbol("anchor", 2, records=_records(("/root", "U104", 2))),
        _symbol(
            "conflict",
            1,
            records=_records(("/root", "U104", 1), ("/root", *second)),
        ),
    )
    index = SchematicIndex.from_paths([str(root)])

    for path in ("/root/anchor", "/root/conflict"):
        component = index.resolve_component(path)
        assert not component.resolved
        assert component.issues


def test_cross_sheet_units_form_one_component(tmp_path: Path) -> None:
    """Actual hierarchy membership, not physical file equality, groups units."""
    child = _write(
        tmp_path / "child.kicad_sch",
        "unrelated-child-root",
        _symbol("b", 2, records=_records(("/root/placed", "U104", 2))),
        _symbol("c", 3, records=_records(("/root/placed", "U104", 3))),
    )
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _symbol("a", 1, records=_records(("/root", "U104", 1))),
        _sheet("placed", child.name),
    )
    index = SchematicIndex.from_paths([str(root)])
    component = index.resolve_component("/placed/b")

    assert component.resolved
    assert set(component.key) == {"/root/a", "/root/placed/b", "/root/placed/c"}
    assert len({member.target.file_path for member in component.members}) == 2


def test_partial_placed_unit_set_does_not_require_unused_library_units(
    tmp_path: Path,
) -> None:
    """A valid component can place units one and three while leaving two unused."""
    root = _write(tmp_path / "root.kicad_sch", "root", _symbol("a", 1), _symbol("c", 3))
    component = SchematicIndex.from_paths([str(root)]).resolve_component("/root/c")

    assert component.resolved
    assert {member.unit for member in component.members} == {1, 3}


def test_distinct_edited_cache_names_keep_compatible_units_together(
    tmp_path: Path,
) -> None:
    """KiCad permits per-unit edits that create distinct local cache names."""
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _symbol("a", 1, lib_name="Edited_A"),
        _symbol("b", 2, lib_name="Edited_B"),
        libraries=_library("Edited_A") + _library("Edited_B"),
    )
    component = SchematicIndex.from_paths([str(root)]).resolve_component("/root/a")

    assert component.resolved
    assert set(component.key) == {"/root/a", "/root/b"}


@pytest.mark.parametrize(
    "sibling,libraries",
    [
        (_symbol("b", 1), _library()),
        (
            _symbol("b", 2, lib_id="Amplifier:Other"),
            _library() + _library("Amplifier:Other"),
        ),
        (
            _symbol("b", 1, lib_id="Amplifier:Single"),
            _library() + _library("Amplifier:Single", 1),
        ),
        (
            _symbol("b", 2, lib_name="Different_count"),
            _library() + _library("Different_count", 2),
        ),
        (_symbol("b", 4), _library()),
        (_symbol("b", 0), _library()),
        (_symbol("b", 2, lib_name="Absent_cache"), _library()),
    ],
    ids=[
        "duplicate-unit",
        "lib-id",
        "single-unit-collision",
        "unit-count",
        "out-of-range",
        "zero",
        "no-cache",
    ],
)
def test_incompatible_related_units_block_every_anchor(
    tmp_path: Path, sibling: str, libraries: str
) -> None:
    """Discovery must retain a related unit even if its library is incompatible."""
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _symbol("a", 1),
        sibling,
        libraries=libraries,
    )
    index = SchematicIndex.from_paths([str(root)])

    for path in ("/root/a", "/root/b"):
        component = index.resolve_component(path)
        assert not component.resolved
        assert component.issues


@pytest.mark.parametrize("reference", ["U?", "", "U"])
def test_multiunit_component_requires_complete_annotation(
    tmp_path: Path, reference: str
) -> None:
    """UUID anchors cannot authenticate sibling grouping without a reference."""
    root = _write(
        tmp_path / "root.kicad_sch", "root", _symbol("a", 1, reference=reference)
    )
    component = SchematicIndex.from_paths([str(root)]).resolve_component("/root/a")

    assert not component.resolved
    assert component.issues


def test_true_single_unit_needs_no_reference_grouping(tmp_path: Path) -> None:
    """A UUID-linked one-unit symbol remains writable without annotation."""
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _symbol("a", 1, reference="U?"),
        _symbol("b", 1, reference="U?"),
        libraries=_library(count=1),
    )
    index = SchematicIndex.from_paths([str(root)])

    for uuid in ("a", "b"):
        component = index.resolve_component(f"/root/{uuid}")
        assert component.resolved
        assert component.key == (f"/root/{uuid}",)


def test_reused_sheet_has_distinct_components_with_shared_physical_members(
    tmp_path: Path,
) -> None:
    """Consumer consensus can see every occurrence of each physical write target."""
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
    index = SchematicIndex.from_paths([str(root)])
    left = index.resolve_component("/root/left/unit1")
    right = index.resolve_component("/right/unit3")

    assert left.resolved and right.resolved
    assert left.key != right.key
    assert {member.reference for member in left.members} == {"U104"}
    assert {member.reference for member in right.members} == {"U105"}
    assert len(left.members) == len(right.members) == 3
    assert {member.target.key for member in left.members} == {
        member.target.key for member in right.members
    }
    for member in left.members:
        assert member.target.instance_paths == (
            f"/root/left/{member.target.symbol_uuid}",
            f"/root/right/{member.target.symbol_uuid}",
        )


def test_missing_current_record_in_reused_sheet_cannot_use_base_reference(
    tmp_path: Path,
) -> None:
    """A sibling with unknown current annotation must not disappear from a group."""
    child = _write(
        tmp_path / "shared.kicad_sch",
        "child-root",
        _symbol(
            "a",
            1,
            records=_records(("/root/left", "U104", 1), ("/root/right", "U105", 1)),
        ),
        _symbol("b", 2, reference="U105", records=_records(("/root/left", "U104", 2))),
    )
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _sheet("left", child.name),
        _sheet("right", child.name),
    )
    index = SchematicIndex.from_paths([str(root)])

    for path in ("/root/right/a", "/root/right/b"):
        component = index.resolve_component(path)
        assert not component.resolved
        assert component.issues


def test_legacy_base_fields_are_unsafe_for_reused_physical_symbol(
    tmp_path: Path,
) -> None:
    """The no-instance-record fallback is limited to one actual occurrence."""
    child = _write(tmp_path / "shared.kicad_sch", "child", _symbol("a", 1))
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _sheet("left", child.name),
        _sheet("right", child.name),
    )
    index = SchematicIndex.from_paths([str(root)])

    assert not index.resolve_component("/root/left/a").resolved
    assert not index.resolve_component("/root/right/a").resolved


@pytest.mark.parametrize("stored_path", ["/old-root", "/", "/a"])
def test_foreign_or_rootless_stored_instance_paths_do_not_authenticate_current_unit(
    tmp_path: Path, stored_path: str
) -> None:
    """PCB rootless aliases do not apply to rooted saved instance metadata."""
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _symbol("a", 1, records=_records((stored_path, "U104", 1))),
    )
    index = SchematicIndex.from_paths([str(root)])

    assert index.resolve("/a") is not None
    assert not index.resolve_component("/a").resolved


def test_independent_roots_do_not_share_a_component_reference_namespace(
    tmp_path: Path,
) -> None:
    """Selected unrelated projects may reuse component references safely."""
    first = _write(tmp_path / "one.kicad_sch", "one", _symbol("a", 1))
    second = _write(tmp_path / "two.kicad_sch", "two", _symbol("b", 2))
    paths = [str(first), str(second)]

    independent = SchematicIndex.from_paths(paths)
    assert independent.resolve_component("/one/a").key == ("/one/a",)
    assert independent.resolve_component("/two/b").key == ("/two/b",)
    assert independent.resolve_component("/one/a").resolved
    assert independent.resolve_component("/two/b").resolved

    shared = SchematicIndex.from_paths(paths, shared_project=True)
    component = shared.resolve_component("/one/a")
    assert component.resolved
    assert component.key == ("/one/a", "/two/b")
    assert shared.resolve_component("/two/b").key == component.key


def test_duplicate_units_across_authenticated_top_roots_are_rejected(
    tmp_path: Path,
) -> None:
    """Explicit project sharing also shares annotation-conflict validation."""
    roots = [
        _write(tmp_path / f"{name}.kicad_sch", name, _symbol(name, 1))
        for name in ("one", "two")
    ]
    index = SchematicIndex.from_paths(
        [str(path) for path in roots], shared_project=True
    )

    assert not index.resolve_component("/one/one").resolved
    assert not index.resolve_component("/two/two").resolved


def test_missing_or_ambiguous_anchor_returns_an_unresolved_result(
    tmp_path: Path,
) -> None:
    """Callers can preserve/report unmatched PCB links without guessing a group."""
    root = _write(
        tmp_path / "root.kicad_sch", "root", _symbol("same", 1), _symbol("same", 2)
    )
    index = SchematicIndex.from_paths([str(root)])

    for path in ("/root/same", "/root/missing", ""):
        component = index.resolve_component(path)
        assert not component.resolved
        assert component.issues


@pytest.mark.parametrize(
    "cached_anchor", [True, False], ids=["one-unit-cache", "no-cache"]
)
def test_apparent_single_unit_keeps_unknown_related_units_in_candidate_set(
    tmp_path: Path, cached_anchor: bool
) -> None:
    """A local cache cannot prove independence from an unresolved edited sibling."""
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
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _symbol("a", 1, lib_name="Edited_A"),
        _sheet("placed", child.name),
        libraries=_library("Edited_A", 1)
        if cached_anchor
        else _library("Unrelated", 1),
    )
    index = SchematicIndex.from_paths([str(root)])

    component = index.resolve_component("/root/a")

    assert not component.resolved
    assert component.issues


@pytest.mark.parametrize(
    "unit,records",
    [
        (0, ""),
        (1, _records(("/root", "U104", 1), ("/root", "U104", 2))),
    ],
    ids=["explicit-zero", "conflicting-current-units"],
)
def test_single_unit_cache_does_not_override_explicit_invalid_unit_metadata(
    tmp_path: Path, unit: int, records: str
) -> None:
    """UUID independence does not turn invalid or conflicting units into unit one."""
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _symbol("a", unit, records=records),
        libraries=_library(count=1),
    )

    component = SchematicIndex.from_paths([str(root)]).resolve_component("/root/a")

    assert not component.resolved
    assert component.issues


def test_missing_cache_requires_validation_when_another_component_proves_multiunit_family(
    tmp_path: Path,
) -> None:
    """A different annotation does not prove a cacheless unit-one anchor is single."""
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
    index = SchematicIndex.from_paths([str(root)])

    component = index.resolve_component("/root/a")

    assert not component.resolved
    assert component.issues
    assert index.resolve_component("/root/placed/b").resolved


@pytest.mark.parametrize("conflicting_current", [True, False])
def test_current_unit_evidence_keeps_conflicted_sibling_in_single_unit_candidate_set(
    tmp_path: Path, conflicting_current: bool
) -> None:
    """Conflicting current unit two matters; a foreign-path unit two does not."""
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
        libraries=_library(count=1),
    )
    index = SchematicIndex.from_paths([str(root)])

    for uuid in ("a", "b"):
        component = index.resolve_component(f"/root/{uuid}")
        assert component.resolved is not conflicting_current
        if conflicting_current:
            assert component.issues
        else:
            assert component.key == (f"/root/{uuid}",)


def test_known_single_duplicate_refs_remain_independent_of_other_multiunit_component(
    tmp_path: Path,
) -> None:
    """A different annotated package cannot erase positive one-unit cache evidence."""
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _symbol("a", 1, lib_name="Single"),
        _symbol("b", 1, lib_name="Single"),
        _symbol("multi", 2, reference="U105", lib_name="Multi"),
        libraries=_library("Single", 1) + _library("Multi", 2),
    )
    index = SchematicIndex.from_paths([str(root)])

    for uuid in ("a", "b", "multi"):
        component = index.resolve_component(f"/root/{uuid}")
        assert component.resolved
        assert component.key == (f"/root/{uuid}",)


def test_valid_current_unit_does_not_hide_out_of_range_explicit_placed_unit(
    tmp_path: Path,
) -> None:
    """Current metadata cannot make an explicitly invalid placed unit well-formed."""
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _symbol("a", 2, records=_records(("/root", "U104", 1))),
        libraries=_library(count=1),
    )

    component = SchematicIndex.from_paths([str(root)]).resolve_component("/root/a")

    assert not component.resolved
    assert component.issues
