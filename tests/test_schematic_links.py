"""Resolve PCB links through actual placed sheets, without annotation guesses."""

import importlib
import os
from pathlib import Path
from types import ModuleType

import pytest

from tests.wx_harness import temporary_modules

_ROOT = Path(__file__).parent.parent
_PACKAGE = ModuleType("schematic_links_test_plugin")
_PACKAGE.__path__ = [str(_ROOT)]
with temporary_modules(
    {"schematic_links_test_plugin": _PACKAGE},
    namespaces=("schematic_links_test_plugin",),
):
    SchematicIndex = importlib.import_module(
        "schematic_links_test_plugin.schematic_links"
    ).SchematicIndex


def _symbol(uuid: str = "symbol", reference: str = "R1", extra: str = "") -> str:
    """Describe one real placed symbol, with deliberately untrusted references."""
    return (
        f'(symbol (lib_id "Device:R") (uuid "{uuid}") (in_bom yes) '
        f'(property "Reference" "{reference}") {extra})'
    )


def _sheet(uuid: str, filename: str) -> str:
    """Describe a placed sheet whose UUID need not equal the child's root UUID."""
    return f'(sheet (uuid "{uuid}") (property "Sheetfile" "{filename}"))'


def _write(path: Path, uuid: str, *children: str) -> Path:
    """Write an otherwise minimal structurally complete schematic."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'(kicad_sch (version 20250114) (uuid "{uuid}") {" ".join(children)})',
        encoding="utf-8",
    )
    return path


def test_copied_child_uses_placed_sheet_uuid_and_ignores_stale_instances(
    tmp_path: Path,
) -> None:
    """Copied child metadata cannot redirect a PCB link or hide its real target."""
    child = _write(
        tmp_path / "child.kicad_sch",
        "copied-root",
        _symbol(
            reference="R44",
            extra='(property "LCSC" " C200 ") '
            '(instances (project "old-project" '
            '(path "/old-root/old-sheet" (reference "R99") (unit 1))))',
        ),
    )
    root = _write(
        tmp_path / "new.kicad_sch", "real-root", _sheet("placed-sheet", child.name)
    )

    index = SchematicIndex.from_paths([str(root)])
    target = index.resolve("/real-root/placed-sheet/symbol")

    assert target is not None
    assert target.file_path == str(child.resolve())
    assert target.symbol_uuid == "symbol"
    assert target.reference == "R44"
    assert target.assignment.status == "valid"
    assert target.lcsc == "C200"
    assert target.in_bom is True
    assert target.instance_paths == ("/real-root/placed-sheet/symbol",)
    assert index.resolve("/real-root/copied-root/symbol") is None
    assert index.resolve("/old-root/old-sheet/symbol") is None
    assert index.texts[str(child.resolve())] == child.read_text(encoding="utf-8")


def test_native_rootless_links_resolve_to_the_actual_rooted_instance(
    tmp_path: Path,
) -> None:
    """KiCad 7–9 links omit the root sheet UUID; their target still has one identity."""
    # Real root/symbol IDs and rootless PCB path from KiCad's shipped
    # Edgeberry_Cartridge/Edgeberry_cartridge_template (generator_version 9.0).
    # The nested child below is synthetic coverage of the same path grammar.
    root_uuid = "e70b6168-f98e-4322-bc55-500948ef7b77"
    root_symbol_uuid = "3f3e20f8-4c6e-49f1-99fe-f7c68e867109"
    sheet_uuid = "340cedf7-9a5b-4eea-af50-bdb9882c4b65"
    symbol_uuid = "1fed9967-85bb-47d4-a6af-95ff4db57f5c"
    child = _write(
        tmp_path / "child.kicad_sch", "copied-child-root", _symbol(symbol_uuid)
    )
    root = _write(
        tmp_path / "root.kicad_sch",
        root_uuid,
        _symbol(root_symbol_uuid),
        _sheet(sheet_uuid, child.name),
    )

    index = SchematicIndex.from_paths([str(root)])

    assert index.resolve(f"/{root_symbol_uuid}") is not None
    assert (
        index.canonical_link(f"/{root_symbol_uuid}")
        == f"/{root_uuid}/{root_symbol_uuid}"
    )
    assert index.resolve(f"/{root_symbol_uuid}") is index.resolve(
        f"/{root_uuid}/{root_symbol_uuid}"
    )
    assert (
        index.canonical_link(f"/{sheet_uuid}/{symbol_uuid}")
        == f"/{root_uuid}/{sheet_uuid}/{symbol_uuid}"
    )
    assert index.resolve(f"/{sheet_uuid}/{symbol_uuid}") is index.resolve(
        f"/{root_uuid}/{sheet_uuid}/{symbol_uuid}"
    )
    assert index.resolve(f"/{sheet_uuid}/{symbol_uuid}").instance_paths == (
        f"/{root_uuid}/{sheet_uuid}/{symbol_uuid}",
    )


def test_rootless_links_ambiguous_across_roots_do_not_block_rooted_links(
    tmp_path: Path,
) -> None:
    """Two copied projects need fully rooted links; neither rootless guess is safe."""
    roots = [
        _write(tmp_path / f"{name}.kicad_sch", name, _symbol())
        for name in ("one", "two")
    ]

    index = SchematicIndex.from_paths([str(root) for root in roots])

    assert index.resolve("/symbol") is None
    assert index.canonical_link("/symbol") is None
    assert index.resolve("/one/symbol") is not None
    assert index.resolve("/two/symbol") is not None
    assert index.issues == ()


def test_rootless_and_rooted_path_spellings_cannot_mask_each_other(
    tmp_path: Path,
) -> None:
    """A literal path matching two full instance identities must fail closed."""
    child = _write(tmp_path / "child.kicad_sch", "child", _symbol())
    nested = _write(tmp_path / "nested.kicad_sch", "other", _sheet("root", child.name))
    root = _write(tmp_path / "root.kicad_sch", "root", _symbol())

    index = SchematicIndex.from_paths([str(nested), str(root)])

    assert index.resolve("/root/symbol") is None
    assert index.canonical_link("/root/symbol") is None
    assert index.resolve("/other/root/symbol") is not None
    assert index.issues == ()


def test_mixed_rootless_and_rooted_reused_sheet_paths_share_one_consensus(
    tmp_path: Path,
) -> None:
    """A board partially updated in KiCad 10 still supplies each shared instance once."""
    child = _write(tmp_path / "child.kicad_sch", "child", _symbol())
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _sheet("left", child.name),
        _sheet("right", child.name),
    )

    index = SchematicIndex.from_paths([str(root)])
    target = index.resolve("/left/symbol")
    canonical = {
        index.canonical_link(path) for path in ("/left/symbol", "/root/right/symbol")
    }

    assert target is index.resolve("/root/right/symbol")
    assert canonical == set(target.instance_paths)


def test_reused_file_retains_every_physical_instance(tmp_path: Path) -> None:
    """Grouping shared writes requires every actual placed sheet instance."""
    child = _write(tmp_path / "shared.kicad_sch", "child", _symbol())
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _sheet("left", child.name),
        _sheet("right", child.name),
    )

    index = SchematicIndex.from_paths([str(root)])
    target = index.resolve("/root/left/symbol")

    assert target is index.resolve("/root/right/symbol")
    assert target is not None
    assert target.instance_paths == ("/root/left/symbol", "/root/right/symbol")
    assert index.targets[(str(child.resolve()), "symbol")] is target
    assert index.encountered_paths == (str(root), str(child))


def test_case_aliases_share_consensus_on_case_insensitive_filesystem(
    tmp_path: Path,
) -> None:
    """Different spellings of one directory entry are one physical write target."""
    child = _write(tmp_path / "child.kicad_sch", "child", _symbol())
    alias = child.with_name("Child.kicad_sch")
    if not alias.exists() or not os.path.samefile(child, alias):
        pytest.skip("requires a case-insensitive filesystem")
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _sheet("first", alias.name),
        _sheet("second", child.name),
    )

    index = SchematicIndex.from_paths([str(root)])
    first = index.resolve("/root/first/symbol")

    assert first is index.resolve("/root/second/symbol")
    assert first.instance_paths == ("/root/first/symbol", "/root/second/symbol")
    assert first.file_path == str(child)
    assert index.file_paths[str(alias)] == str(child)
    assert index.file_paths[str(child)] == str(child)
    assert index.encountered_paths == (str(root), str(alias), str(child))


def test_unicode_spelling_aliases_share_consensus_when_filesystem_normalizes(
    tmp_path: Path,
) -> None:
    """NFC/NFD spellings of one macOS entry cannot bypass shared-write agreement."""
    child = _write(tmp_path / "caf\u00e9.kicad_sch", "child", _symbol())
    alias = tmp_path / "cafe\u0301.kicad_sch"
    if not alias.exists() or not os.path.samefile(child, alias):
        pytest.skip("requires a Unicode-normalizing filesystem")
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _sheet("first", child.name),
        _sheet("second", alias.name),
    )

    index = SchematicIndex.from_paths([str(root)])

    assert index.resolve("/root/first/symbol") is index.resolve("/root/second/symbol")
    assert index.resolve("/root/first/symbol").instance_paths == (
        "/root/first/symbol",
        "/root/second/symbol",
    )
    assert index.file_paths[str(child)] == index.file_paths[str(alias)]


def test_distinct_hardlink_names_remain_separate_write_targets(tmp_path: Path) -> None:
    """Atomic replacement of one hardlink does not replace its other directory entry."""
    child = _write(tmp_path / "child.kicad_sch", "child", _symbol())
    linked = tmp_path / "linked.kicad_sch"
    os.link(child, linked)
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _sheet("first", child.name),
        _sheet("second", linked.name),
    )

    index = SchematicIndex.from_paths([str(root)])

    assert index.resolve("/root/first/symbol") is not index.resolve(
        "/root/second/symbol"
    )
    assert index.resolve("/root/first/symbol").file_path == str(child)
    assert index.resolve("/root/second/symbol").file_path == str(linked)


def test_case_sensitive_hardlink_names_remain_distinct(tmp_path: Path) -> None:
    """Exact directory entries Foo/foo are different writes even with equal inodes."""
    child = _write(tmp_path / "child.kicad_sch", "child", _symbol())
    linked = child.with_name("Child.kicad_sch")
    if linked.exists():
        pytest.skip("requires a case-sensitive filesystem")
    os.link(child, linked)
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _sheet("first", child.name),
        _sheet("second", linked.name),
    )

    index = SchematicIndex.from_paths([str(root)])

    assert index.resolve("/root/first/symbol") is not index.resolve(
        "/root/second/symbol"
    )
    assert index.file_paths[str(child)] == str(child)
    assert index.file_paths[str(linked)] == str(linked)


def test_nested_relative_sheets_and_multiple_roots(tmp_path: Path) -> None:
    """Root UUIDs separate identical symbol UUIDs and references across projects."""
    leaf = _write(tmp_path / "parts/leaf.kicad_sch", "leaf", _symbol())
    branch = _write(
        tmp_path / "sheets/branch.kicad_sch",
        "ignored-branch-root",
        _sheet("nested", "../parts/leaf.kicad_sch"),
    )
    first = _write(
        tmp_path / "first.kicad_sch",
        "first",
        _sheet("branch", "sheets/branch.kicad_sch"),
    )
    second = _write(tmp_path / "second.kicad_sch", "second", _symbol())

    index = SchematicIndex.from_paths([str(first), str(second)])

    assert index.resolve("/first/branch/nested/symbol").file_path == str(leaf)
    assert index.resolve("/second/symbol").file_path == str(second)
    assert index.encountered_paths == (str(first), str(branch), str(leaf), str(second))


def test_selected_descendant_does_not_create_an_extra_root_instance(
    tmp_path: Path,
) -> None:
    """Selecting every project file still describes just its real hierarchy."""
    child = _write(tmp_path / "child.kicad_sch", "unused-child-root", _symbol())
    root = _write(tmp_path / "root.kicad_sch", "root", _sheet("child", child.name))

    index = SchematicIndex.from_paths([str(child), str(root)])

    assert index.resolve("/root/child/symbol").instance_paths == ("/root/child/symbol",)
    assert index.resolve("/unused-child-root/symbol") is None
    assert (
        SchematicIndex.from_paths([str(child)]).resolve("/unused-child-root/symbol")
        is not None
    )


@pytest.mark.parametrize(
    "unit,body,multi_unit", [(1, 1, False), (1, 2, False), (2, 1, True)]
)
def test_library_unit_definitions_identify_unsafe_multiunit_targets(
    tmp_path: Path, unit: int, body: int, multi_unit: bool
) -> None:
    """Even linked unit one requires care when its library has multiple units."""
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        f'(lib_symbols (symbol "Device:R" (symbol "R_1_1") (symbol "R_{unit}_{body}")))',
        _symbol(extra="(unit 1)"),
    )

    assert (
        SchematicIndex.from_paths([str(root)]).resolve("/root/symbol").multi_unit
        is multi_unit
    )


@pytest.mark.parametrize("modified_is_multi", [False, True])
@pytest.mark.parametrize("with_lib_id", [False, True])
def test_locally_modified_symbol_uses_its_cached_library_name(
    tmp_path: Path,
    modified_is_multi: bool,
    with_lib_id: bool,
) -> None:
    """KiCad's lib_name selects the cached definition independently of lib_id."""
    modified_unit = 2 if modified_is_multi else 1
    original_unit = 1 if modified_is_multi else 2
    placed = _symbol(extra='(unit 1) (lib_name "Device:R_modified")')
    if not with_lib_id:
        placed = placed.replace('(lib_id "Device:R")', "")
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        "(lib_symbols "
        f'(symbol "Device:R" (symbol "R_1_1") (symbol "R_{original_unit}_1")) '
        f'(symbol "Device:R_modified" (symbol "R_modified_1_1") '
        f'(symbol "R_modified_{modified_unit}_1")))',
        placed,
    )
    target = SchematicIndex.from_paths([str(root)]).resolve("/root/symbol")
    assert target.multi_unit is modified_is_multi


@pytest.mark.parametrize(
    "binding",
    [
        '(lib_name "")',
        '(lib_name "Device:R_modified" "Device:R")',
        '(lib_name "Device:R_modified") (lib_name "Device:R")',
        '(lib_id "Device:R_modified")',
    ],
)
def test_ambiguous_library_binding_cannot_hide_multiunit_definition(
    tmp_path: Path,
    binding: str,
) -> None:
    """Malformed preferred names never silently fall through to another library."""
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        '(lib_symbols (symbol "Device:R_modified" '
        '(symbol "R_modified_1_1") (symbol "R_modified_2_1")))',
        _symbol(extra=f"(unit 1) {binding}"),
    )
    with pytest.raises(ValueError, match="library"):
        SchematicIndex.from_paths([str(root)])


@pytest.mark.parametrize(
    "extra,status,lcsc",
    [
        ("", "missing", ""),
        ('(property "LCSC" "")', "empty", ""),
        ('(property "LCSC" "bad")', "invalid", ""),
        ('(property "LCSC" "C100") (property "JLCPCB" "C200")', "conflict", ""),
        ('(property "JLCPCB Part Number" "C200")', "valid", "C200"),
    ],
)
def test_assignment_provenance_uses_only_direct_base_fields(
    tmp_path: Path, extra: str, status: str, lcsc: str
) -> None:
    """Variant and library fields cannot manufacture a base assignment."""
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        '(lib_symbols (symbol "Device:R" (property "LCSC" "C999")))',
        _symbol(
            extra=extra + ' (variants (variant "alternate" (property "LCSC" "C300")))'
        ),
    )
    target = SchematicIndex.from_paths([str(root)]).resolve("/root/symbol")

    assert target.assignment.status == status
    assert target.lcsc == lcsc
    with pytest.raises(TypeError):
        target.fields["LCSC"] = "C400"


@pytest.mark.parametrize("duplicate", ["symbol", "sheet", "root"])
def test_duplicate_uuid_paths_fail_closed(tmp_path: Path, duplicate: str) -> None:
    """Neither references nor the first parsed entry resolves duplicate identity."""
    if duplicate == "symbol":
        root = _write(
            tmp_path / "root.kicad_sch", "root", _symbol(), _symbol(reference="R2")
        )
        roots, link = [root], "/root/symbol"
    elif duplicate == "sheet":
        _write(tmp_path / "one.kicad_sch", "one", _symbol())
        _write(tmp_path / "two.kicad_sch", "two", _symbol())
        root = _write(
            tmp_path / "root.kicad_sch",
            "root",
            _sheet("same", "one.kicad_sch"),
            _sheet("same", "two.kicad_sch"),
        )
        roots, link = [root], "/root/same/symbol"
    else:
        roots = [
            _write(tmp_path / f"{name}.kicad_sch", "root", _symbol())
            for name in ("one", "two")
        ]
        link = "/root/symbol"

    index = SchematicIndex.from_paths([str(root) for root in roots])

    assert index.resolve(link) is None
    assert index.issues


def test_duplicate_sheet_uuid_blocks_distinct_child_symbols(tmp_path: Path) -> None:
    """An ambiguous sheet step cannot become safe from differing leaf UUIDs."""
    _write(tmp_path / "one.kicad_sch", "one", _symbol("one"))
    _write(tmp_path / "two.kicad_sch", "two", _symbol("two"))
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _sheet("same", "one.kicad_sch"),
        _sheet("same", "two.kicad_sch"),
    )

    index = SchematicIndex.from_paths([str(root)])

    assert index.resolve("/root/same/one") is None
    assert index.resolve("/root/same/two") is None
    assert index.issues


def test_duplicate_references_and_separate_units_keep_distinct_uuid_targets(
    tmp_path: Path,
) -> None:
    """A native link to any unit resolves that placed unit without reference merging."""
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _symbol("unit-a", "U1", "(unit 1)"),
        _symbol("unit-b", "U1", "(unit 2)"),
    )

    index = SchematicIndex.from_paths([str(root)])

    assert index.resolve("/root/unit-a").symbol_uuid == "unit-a"
    assert index.resolve("/root/unit-b").symbol_uuid == "unit-b"
    assert index.resolve("/root/unit-a").multi_unit is True
    assert index.resolve("/root/unit-b").multi_unit is True
    assert len(index.targets) == 2


def test_three_placed_units_resolve_as_one_component_from_any_anchor(
    tmp_path: Path,
) -> None:
    """One footprint's UUID anchor identifies every placed unit of its package."""
    library = (
        '(lib_symbols (symbol "Device:R" '
        '(symbol "R_1_1") (symbol "R_2_1") (symbol "R_3_1")))'
    )
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        library,
        *(_symbol(f"unit-{unit}", "U104", f"(unit {unit})") for unit in (1, 2, 3)),
    )
    index = SchematicIndex.from_paths([str(root)])
    for unit in (1, 2, 3):
        component = index.resolve_component(f"/root/unit-{unit}")
        assert component.resolved
        assert component.key == tuple(f"/root/unit-{number}" for number in (1, 2, 3))
        assert {member.unit for member in component.members} == {1, 2, 3}


@pytest.mark.parametrize("broken", ["missing", "syntax", "cycle", "root-uuid"])
def test_incomplete_hierarchy_cannot_be_used_for_safe_export(
    tmp_path: Path, broken: str
) -> None:
    """A hierarchy must be complete before any target is declared safe to update."""
    root = _write(tmp_path / "root.kicad_sch", "root", _symbol())
    if broken == "missing":
        _write(root, "root", _sheet("child", "absent.kicad_sch"))
    elif broken == "syntax":
        root.write_text('(kicad_sch (uuid "root")', encoding="utf-8")
    elif broken == "cycle":
        _write(root, "root", _sheet("child", root.name))
    else:
        root.write_text(
            '(kicad_sch (symbol (lib_id "Device:R") (uuid "symbol")))', encoding="utf-8"
        )

    with pytest.raises((FileNotFoundError, ValueError)):
        SchematicIndex.from_paths([str(root)])


def test_symlink_paths_are_retained_for_locks_and_resolve_relative_children(
    tmp_path: Path,
) -> None:
    """One physical parent opened through another name uses that name's directory."""
    real = _write(
        tmp_path / "real/root.kicad_sch", "root", _sheet("child", "child.kicad_sch")
    )
    child = _write(tmp_path / "alias/child.kicad_sch", "child", _symbol())
    alias = tmp_path / "alias/root.kicad_sch"
    alias.symlink_to(real)

    index = SchematicIndex.from_paths([str(alias)])

    assert index.resolve("/root/child/symbol").file_path == str(child)
    assert index.encountered_paths == (str(alias), str(child))
    assert str(real) in index.texts


def test_same_root_file_opened_beside_different_children_is_ambiguous(
    tmp_path: Path,
) -> None:
    """Root aliases with different relative sheet contexts cannot authenticate links."""
    root = _write(
        tmp_path / "one/root.kicad_sch", "root", _sheet("child", "child.kicad_sch")
    )
    _write(tmp_path / "one/child.kicad_sch", "one-child", _symbol("first"))
    _write(tmp_path / "two/child.kicad_sch", "two-child", _symbol("second"))
    alias = tmp_path / "two/root.kicad_sch"
    alias.symlink_to(root)

    index = SchematicIndex.from_paths([str(root), str(alias)])

    assert index.resolve("/root/child/first") is None
    assert index.resolve("/root/child/second") is None
    assert index.issues


def test_missing_child_error_identifies_the_referring_sheet(tmp_path: Path) -> None:
    """Preserve the existing actionable hierarchy error and underlying cause."""
    root = _write(
        tmp_path / "root.kicad_sch", "root", _sheet("child", "gone.kicad_sch")
    )

    with pytest.raises(
        FileNotFoundError, match="'gone.kicad_sch' used in 'root.kicad_sch'"
    ) as caught:
        SchematicIndex.from_paths([str(root)])
    assert isinstance(caught.value.__cause__, FileNotFoundError)


def test_invalid_utf8_identifies_the_bad_sheet(tmp_path: Path) -> None:
    """Unreadable child data must fail with a filename before any exporter writes."""
    root = _write(tmp_path / "root.kicad_sch", "root", _sheet("child", "bad.kicad_sch"))
    (tmp_path / "bad.kicad_sch").write_bytes(b"(kicad_sch \xb5)")

    with pytest.raises(ValueError, match="Sheet file 'bad.kicad_sch' is not UTF-8"):
        SchematicIndex.from_paths([str(root)])


@pytest.mark.parametrize(
    "link", ["", "symbol", "/", "/root", "/root//symbol", "/root/symbol/"]
)
def test_unlinked_or_malformed_path_never_falls_back_to_reference(
    tmp_path: Path, link: str
) -> None:
    """Only a complete native UUID path can identify a schematic target."""
    root = _write(tmp_path / "root.kicad_sch", "root", _symbol())
    assert SchematicIndex.from_paths([str(root)]).resolve(link) is None


def test_declared_project_root_uuids_override_file_ids_for_component_paths(
    tmp_path: Path,
) -> None:
    """KiCad 10 project roots can authenticate paths with IDs unlike file UUIDs."""
    roots = []
    for unit in (1, 2):
        path = tmp_path / f"root{unit}.kicad_sch"
        path.write_text(
            f'(kicad_sch (uuid "file-{unit}") (lib_symbols '
            '(symbol "Device:Dual" (symbol "Dual_1_1") (symbol "Dual_2_1"))) '
            f'(symbol (lib_id "Device:Dual") (uuid "unit-{unit}") (unit {unit}) '
            '(property "Reference" "OLD1") (instances (project "stale" '
            f'(path "/project-{unit}" (reference "U1") (unit {unit}))))))',
            encoding="utf-8",
        )
        roots.append(path)
    index = SchematicIndex.from_paths(
        [str(path) for path in roots],
        shared_project=True,
        root_uuids={str(path): f"project-{unit}" for unit, path in enumerate(roots, 1)},
    )

    component = index.resolve_component("/project-1/unit-1")
    assert component.resolved
    assert component.key == ("/project-1/unit-1", "/project-2/unit-2")
    assert index.resolve_component("/unit-2").key == component.key
    assert index.resolve("/file-1/unit-1") is None


def test_conflicting_declared_root_ids_for_one_physical_file_fail_before_export(
    tmp_path: Path,
) -> None:
    """A path alias cannot declare a second independently editable project root."""
    root = tmp_path / "root.kicad_sch"
    root.write_text('(kicad_sch (uuid "file-root"))', encoding="utf-8")
    alias = tmp_path / "alias.kicad_sch"
    alias.symlink_to(root)

    with pytest.raises(ValueError, match="Conflicting.*root"):
        SchematicIndex.from_paths(
            [str(root), str(alias)],
            root_uuids={str(root): "one", str(alias): "two"},
        )


def test_authenticated_top_level_root_is_not_hidden_by_its_child_occurrence(
    tmp_path: Path,
) -> None:
    """A project-declared root can also be used as a child in another hierarchy."""
    root = _write(
        tmp_path / "root.kicad_sch",
        "root",
        _sheet("placed", "child.kicad_sch"),
    )
    child = _write(tmp_path / "child.kicad_sch", "child-root", _symbol())
    index = SchematicIndex.from_paths(
        [str(root), str(child)],
        shared_project=True,
    )

    target = index.resolve("/child-root/symbol")
    assert target is not None
    assert target.instance_paths == ("/child-root/symbol", "/root/placed/symbol")
    assert index.resolve("/root/placed/symbol") == target
