"""Keep exported schematic assignments consistent with native board aliases."""

from collections.abc import Iterator, Sequence
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any

import pytest

from .schematic_export_test_support import snapshot_from_parts
from .wx_harness import load_siblings, module


@pytest.fixture(params=[7, 8], ids=["kicad7", "kicad8+"])
def source(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[SimpleNamespace]:
    """Use the real exporter and resolver with only pcbnew supplied by a double."""
    version = request.param
    store = SimpleNamespace(read_all=lambda: [], variant_name="")
    parent = SimpleNamespace(
        store=store, board_name="board.kicad_pcb", project_path=str(tmp_path)
    )
    with load_siblings(
        "_schematic_alias_tests",
        ("schematicexport", "part_assignments"),
        {"pcbnew": module("pcbnew", GetBuildVersion=lambda: f"{version}.0.0")},
    ) as loaded:
        yield SimpleNamespace(
            version=version,
            exporter=loaded["schematicexport"].SchematicExport(parent),
            assignments=loaded["part_assignments"],
            store=store,
            path=tmp_path / "board.kicad_sch",
            snapshot=lambda parts: snapshot_from_parts(
                loaded["schematicexport"].capture_board, parts
            ),
        )


def _property(version: int, name: str, text: str) -> str:
    """Serialize one field without altering its spelling or value."""
    if version == 7:
        return f'    (property "{name}" "{text}" (at 0 1 0))\n'
    return f'    (property "{name}" "{text}"\n      (at 0 1 0)\n    )\n'


def _symbol(
    version: int,
    reference: str,
    fields: Sequence[tuple[str, str]],
    *,
    aliases_first: bool = False,
    nested: str = "",
) -> str:
    """Return a placed symbol with independently controlled property ordering."""
    ordered = list(fields)
    ordered.insert(len(ordered) if aliases_first else 0, ("Reference", reference))
    properties = "".join(_property(version, name, text) for name, text in ordered)
    start = (
        '  (symbol (lib_id "Device:R")\n'
        if version == 7
        else '  (symbol\n    (lib_id "Device:R")\n'
    )
    return (
        start
        + "    (in_bom yes)\n"
        + properties
        + '    (pin "1" (uuid "pin-id"))\n'
        + nested
        + "  )\n"
    )


def _document(symbols: str, library: str = "  (lib_symbols)\n") -> str:
    """Enclose placed symbols in a minimal schematic document."""
    return "(kicad_sch\n" + library + symbols + ")\n"


def _part(reference: str, lcsc: str) -> dict[str, Any]:
    """Capture the Default mapping and unrelated BOM state used by export."""
    return {
        "reference": reference,
        "lcsc": lcsc,
        "exclude_from_bom": False,
        "variant_name": "",
    }


def _export(
    source: SimpleNamespace,
    fields: Sequence[tuple[str, str]],
    *,
    lcsc: str = "C200",
    aliases_first: bool = False,
) -> str:
    """Export a real file through the complete Default snapshot entry point."""
    source.path.write_text(
        _document(_symbol(source.version, "R1", fields, aliases_first=aliases_first)),
        encoding="utf-8",
    )
    source.exporter.load_schematic(
        [str(source.path)], snapshot=source.snapshot([_part("R1", lcsc)])
    )
    return source.path.read_text(encoding="utf-8")


def _fields(text: str) -> list[tuple[str, str]]:
    """Read exported property names and values while retaining duplicate names."""
    return re.findall(r'\(property\s+"([^"]*)"\s+"([^"]*)"', text)


@pytest.mark.parametrize(
    "alias",
    ["JLCPCB Part Number", "lcsc part #", "JLC_PN", "jlcpcb-part-no"],
)
@pytest.mark.parametrize(
    "aliases_first", [False, True], ids=["after-ref", "before-ref"]
)
def test_export_reuses_board_assignment_alias(
    source: SimpleNamespace, alias: str, aliases_first: bool
) -> None:
    """Replacing a board part updates the existing name without adding a conflict."""
    written = _export(source, [(alias, "C100")], aliases_first=aliases_first)
    fields = _fields(written)

    assert (alias, "C200") in fields
    assert _property(source.version, alias, "C200") in written
    assert [
        name for name, _ in fields if source.assignments.is_assignment_alias(name)
    ] == [alias]
    assignment, lcsc = source.assignments.resolve_assignment(dict(fields), {}, "")
    assert (assignment.status, lcsc) == ("valid", "C200")


@pytest.mark.parametrize("lcsc", ["C200", ""], ids=["assign", "clear"])
def test_export_updates_every_existing_alias_and_preserves_metadata(
    source: SimpleNamespace, lcsc: str
) -> None:
    """Blank, invalid, and conflicting aliases all follow the same board assignment."""
    aliases = ["LCSC", "JLCPCB Part Number", "JLC_PN", "lcsc part #"]
    metadata = [("JLCPCB Rotation", "90"), ("JLCPCB Customer ID", "C777")]
    fields = list(zip(aliases, ["C100", "", "invalid", "C300"])) + metadata

    written = _export(source, fields, lcsc=lcsc)

    assert _fields(written) == [
        ("Reference", "R1"),
        *[(name, lcsc) for name in aliases],
        *metadata,
    ]
    assignment, resolved = source.assignments.resolve_assignment(
        dict(_fields(written)), {}, ""
    )
    assert resolved == lcsc
    assert assignment.status == ("valid" if lcsc else "empty")


@pytest.mark.parametrize("original", ["", "invalid"], ids=["blank", "invalid"])
def test_existing_canonical_alias_is_reused_once(
    source: SimpleNamespace, original: str
) -> None:
    """An empty alias is still a field, including when the board clears it."""
    first = _export(source, [("LCSC", original)])
    assert _fields(first) == [("Reference", "R1"), ("LCSC", "C200")]

    source.exporter.load_schematic(
        [str(source.path)], snapshot=source.snapshot([_part("R1", "")])
    )
    cleared = source.path.read_text(encoding="utf-8")
    assert _fields(cleared) == [("Reference", "R1"), ("LCSC", "")]


@pytest.mark.parametrize("lcsc", ["C200", ""], ids=["assign", "unassigned"])
def test_missing_alias_only_inserts_a_nonempty_assignment(
    source: SimpleNamespace, lcsc: str
) -> None:
    """Canonical insertion is hidden, preserves metadata, and remains idempotent."""
    first = _export(source, [("JLCPCB Rotation", "90")], lcsc=lcsc)
    expected = [("Reference", "R1"), ("JLCPCB Rotation", "90")]
    if lcsc:
        expected.append(("LCSC", lcsc))
        assert "hide" in first
    assert sorted(_fields(first)) == sorted(expected)

    source.exporter.load_schematic(
        [str(source.path)], snapshot=source.snapshot([_part("R1", lcsc)])
    )
    assert source.path.read_text(encoding="utf-8") == first


def test_missing_source_does_not_clear_or_reuse_the_previous_symbol_assignment(
    source: SimpleNamespace,
) -> None:
    """The lack of a source row differs from an explicit empty assignment."""
    first = _symbol(source.version, "R1", [("JLCPCB Part Number", "C100")])
    unmatched = _symbol(source.version, "R2", [("JLCPCB Part Number", "C300")])
    source.path.write_text(_document(first + unmatched), encoding="utf-8")

    source.exporter.load_schematic(
        [str(source.path)], snapshot=source.snapshot([_part("R1", "")])
    )

    written = source.path.read_text(encoding="utf-8")
    assert unmatched in written
    assert _fields(written) == [
        ("Reference", "R1"),
        ("JLCPCB Part Number", ""),
        ("Reference", "R2"),
        ("JLCPCB Part Number", "C300"),
    ]


def test_export_preserves_library_and_nested_native_variant_fields(
    source: SimpleNamespace,
) -> None:
    """Only placed base properties change, even with matching nested alias names."""
    library = """  (lib_symbols
    (symbol "Device:R"
      (property "JLCPCB Part Number" "C800" (at 0 1 0))
    )
  )
"""
    nested = """    (instances
      (project "board"
        (path "/root"
          (reference "R1")
          (unit 1)
          (variant (name "Alternative")
            (field (name "JLCPCB Part Number") (value "C900"))
          )
          (variant (name "Cleared")
            (field (name "LCSC") (value ""))
          )
        )
      )
    )
"""
    source.path.write_text(
        _document(
            _symbol(
                source.version, "R1", [("JLCPCB Part Number", "C100")], nested=nested
            ),
            library,
        ),
        encoding="utf-8",
    )
    source.store.variant_name = "Alternative"

    source.exporter.load_schematic(
        [str(source.path)], snapshot=source.snapshot([_part("R1", "C200")])
    )

    written = source.path.read_text(encoding="utf-8")
    assert library in written
    assert nested in written
    assert _fields(written) == [
        ("JLCPCB Part Number", "C800"),
        ("Reference", "R1"),
        ("JLCPCB Part Number", "C200"),
    ]


def test_selected_child_schematic_uses_the_same_alias_policy(
    source: SimpleNamespace, tmp_path: Path
) -> None:
    """A child supplied by schematic discovery receives the same base mapping."""
    child = tmp_path / "hierarchy" / "child.kicad_sch"
    child.parent.mkdir()
    child.write_text(
        _document(_symbol(source.version, "R1", [("JLCPCB Part Number", "C100")])),
        encoding="utf-8",
    )
    source.path.write_text("parent remains untouched\n", encoding="utf-8")

    source.exporter.load_schematic(
        [str(child)], snapshot=source.snapshot([_part("R1", "C200")])
    )

    assert _fields(child.read_text(encoding="utf-8")) == [
        ("Reference", "R1"),
        ("JLCPCB Part Number", "C200"),
    ]
    assert source.path.read_text(encoding="utf-8") == "parent remains untouched\n"


def test_quoted_parentheses_do_not_change_property_scope(
    source: SimpleNamespace,
) -> None:
    """Quoted punctuation in unrelated metadata cannot terminate a symbol scan."""
    note = 'keep \\"quoted\\" parentheses (and) backslash \\\\ unchanged'
    metadata = _property(source.version, "Notes", note)
    written = _export(
        source,
        [("Notes", note), ("JLCPCB Part Number", "C100")],
        aliases_first=True,
    )

    assert metadata in written
    assert _property(source.version, "JLCPCB Part Number", "C200") in written
    assert '(property "LCSC"' not in written


def test_pinless_symbol_does_not_share_an_assignment_with_its_neighbor(
    source: SimpleNamespace,
) -> None:
    """A symbol's own boundary determines its fields even when it has no pins."""
    first = _symbol(source.version, "R1", []).replace(
        '    (pin "1" (uuid "pin-id"))\n', ""
    )
    unmatched = _symbol(source.version, "R2", [("JLCPCB Part Number", "C300")])
    source.path.write_text(_document(first + unmatched), encoding="utf-8")

    source.exporter.load_schematic(
        [str(source.path)], snapshot=source.snapshot([_part("R1", "C200")])
    )

    written = source.path.read_text(encoding="utf-8")
    assert unmatched in written
    assert _fields(written) == [
        ("Reference", "R1"),
        ("LCSC", "C200"),
        ("Reference", "R2"),
        ("JLCPCB Part Number", "C300"),
    ]


@pytest.mark.parametrize(
    "damage", ["missing-close", "extra-close", "unterminated-quote"]
)
def test_malformed_schematic_preserves_original_and_existing_backup(
    source: SimpleNamespace, damage: str
) -> None:
    """Parsing failures must occur before either original or backup is replaced."""
    original = _document(
        _symbol(source.version, "R1", [("JLCPCB Part Number", "C100")])
    )
    if damage == "missing-close":
        original = original[:-2]
    elif damage == "extra-close":
        original += ")\n"
    else:
        original = original[:-2] + '  (text "unterminated)\n)\n'
    source.path.write_text(original, encoding="utf-8")
    backup = source.path.with_name(source.path.name + "_old")
    backup.write_bytes(b"previous export backup\n")

    with pytest.raises(ValueError):
        source.exporter.load_schematic(
            [str(source.path)], snapshot=source.snapshot([_part("R1", "C200")])
        )

    assert source.path.read_text(encoding="utf-8") == original
    assert backup.read_bytes() == b"previous export backup\n"


@pytest.mark.parametrize("existing", [False, True], ids=["new-field", "existing-alias"])
def test_invalid_source_text_preserves_existing_schematic_assignments(
    source: SimpleNamespace, existing: bool
) -> None:
    """Invalid native values cannot be exported as new assignments or clears."""
    fields = [("JLCPCB Part Number", "C100")] if existing else []
    value = ' c200 "quoted" \\ path\n(part) '

    first = _export(source, fields, lcsc=value)

    assert _fields(first) == [("Reference", "R1"), *fields]
    source.exporter.load_schematic(
        [str(source.path)], snapshot=source.snapshot([_part("R1", value)])
    )
    assert source.path.read_text(encoding="utf-8") == first


@pytest.mark.parametrize(
    "alias",
    ["JLCPCB\tPart Number", r"\x4cCSC", r"\114CSC"],
    ids=["literal-tab", "hex-escape", "octal-escape"],
)
def test_native_quoted_alias_names_are_recognized_without_rewriting_the_name(
    source: SimpleNamespace, alias: str
) -> None:
    """KiCad permits raw tabs and C-style byte escapes, unlike JSON strings.

    See https://dev-docs.kicad.org/en/components/sexpr/ for native quoting rules.
    """
    written = _export(source, [(alias, "C100")])

    assert _property(source.version, alias, "C200") in written
    assert _fields(written) == [("Reference", "R1"), (alias, "C200")]


def test_native_escaped_metadata_names_are_preserved(source: SimpleNamespace) -> None:
    """Legal escaped non-assignment names cannot abort or be changed by export."""
    metadata = [
        (r"Manufacturer\x20Notes", "C777"),
        (r"JLCPCB\040Rotation", "90"),
        (r"Notes\x20\xc3\xa9", "hex-encoded UTF-8"),
        (r"Notes\040\303\251", "octal-encoded UTF-8"),
        ("Notes\tInternal", "retained"),
    ]

    written = _export(source, [*metadata, ("JLCPCB Part Number", "C100")])

    for name, value in metadata:
        assert _property(source.version, name, value) in written
    assert _fields(written) == [
        ("Reference", "R1"),
        *metadata,
        ("JLCPCB Part Number", "C200"),
    ]


def test_native_comments_do_not_change_symbol_or_quote_boundaries(
    source: SimpleNamespace,
) -> None:
    """A hash as the first nonblank character makes its whole line a comment."""
    comment = ' # ignored closing parentheses ))) and an "unterminated quote\n'
    symbol = _symbol(
        source.version,
        "R1",
        [("Notes", "keep # literal (parentheses)"), ("JLCPCB Part Number", "C100")],
    ).replace("    (in_bom yes)\n", "    (in_bom yes)\n" + comment)
    original = _document(
        symbol, library="# ignored opening parentheses (((\n  (lib_symbols)\n"
    )
    source.path.write_text(original, encoding="utf-8")

    source.exporter.load_schematic(
        [str(source.path)], snapshot=source.snapshot([_part("R1", "C200")])
    )

    written = source.path.read_text(encoding="utf-8")
    assert comment in written
    assert "# ignored opening parentheses (((\n" in written
    assert _fields(written) == [
        ("Reference", "R1"),
        ("Notes", "keep # literal (parentheses)"),
        ("JLCPCB Part Number", "C200"),
    ]
