"""Preserve native field spelling and unrelated bytes through UUID-linked saves."""

from pathlib import Path
from typing import Any

import pytest

from .test_schematicexport import _load_schematic, _module, _schematic


def _row(value: str, *, excluded: bool = False) -> dict[str, object]:
    """Supply native Default provenance and an explicit fixture identity link."""
    return {
        "component_id": "footprint-R1",
        "schematic_path": "/root-R1/symbol-R1",
        "reference": "R1",
        "lcsc": value,
        "assignment_status": "valid" if value else "empty",
        "fields": {"LCSC": value},
        "exclude_from_bom": excluded,
    }


def _save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: int,
    original: str,
    expected: str,
    value: str = "C200",
    *,
    excluded: bool = False,
) -> Any:
    """Exercise parsing, decisions and atomic writing, then check exact file bytes."""
    path = tmp_path / "board.kicad_sch"
    path.write_bytes(original.encode("utf-8"))
    outcome = _load_schematic(
        tmp_path,
        monkeypatch,
        version,
        [path],
        [_row(value, excluded=excluded)],
    )
    assert outcome.saved == ("footprint-R1",)
    assert not outcome.diagnostics
    assert path.read_bytes() == expected.encode("utf-8")
    assert path.with_name(path.name + "_old").read_bytes() == original.encode("utf-8")
    target = _module.SchematicIndex.from_paths([str(path)]).resolve(
        "/root-R1/symbol-R1"
    )
    assert target is not None and target.lcsc == value
    assert target.in_bom is (not excluded)
    return target


@pytest.mark.parametrize("version", [7, 8], ids=["kicad7", "kicad8+"])
@pytest.mark.parametrize("value", ["C200", ""], ids=["assignment", "clear"])
@pytest.mark.parametrize(
    "encoded_name,decoded_name",
    [
        ("JLCPCB\tPart Number", "JLCPCB\tPart Number"),
        (r"\x4cCSC", "LCSC"),
        (r"\114CSC", "LCSC"),
    ],
    ids=["literal-tab", "hex-escape", "octal-escape"],
)
def test_native_alias_names_are_reused_without_normalizing_their_spelling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: int,
    value: str,
    encoded_name: str,
    decoded_name: str,
) -> None:
    """A native escaped alias updates in place for assignments and proven clears."""
    original = _schematic(version, "yes", ("R1",), reference="R1").replace(
        '"LCSC" "OLD"', f'"{encoded_name}" "C100"'
    )
    expected = original.replace(
        f'"{encoded_name}" "C100"', f'"{encoded_name}" "{value}"'
    )

    target = _save(tmp_path, monkeypatch, version, original, expected, value)

    assert target.fields[decoded_name] == value
    assert sum(alias.name == decoded_name for alias in target.assignment.aliases) == 1
    path = tmp_path / "board.kicad_sch"
    _load_schematic(tmp_path, monkeypatch, version, [path], [_row(value)])
    assert path.read_bytes() == expected.encode("utf-8")
    assert path.with_name(path.name + "_old").read_bytes() == expected.encode("utf-8")


@pytest.mark.parametrize("version", [7, 8], ids=["kicad7", "kicad8+"])
def test_native_escaped_metadata_names_and_values_keep_their_original_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    """Byte escapes in metadata cannot abort export or become assignment aliases."""
    metadata = (
        '    (property "Manufacturer\\x20Notes" "C100")\n'
        '    (property "JLCPCB\\040Rotation" "90")\n'
        '    (property "Notes\\x20\\xc3\\xa9" "hex-encoded UTF-8")\n'
        '    (property "Notes\\040\\303\\251 Details" "octal-encoded UTF-8")\n'
        '    (property "Notes\tInternal" "keep # literal (parentheses)")\n'
    )
    original = _schematic(version, "yes", ("R1",), reference="R1").replace(
        '    (property "LCSC"', metadata + '    (property "LCSC"', 1
    )
    expected = original.replace('"LCSC" "OLD"', '"LCSC" "C200"')

    target = _save(tmp_path, monkeypatch, version, original, expected)

    assert dict(target.fields) == {
        "Reference": "R1",
        "Manufacturer Notes": "C100",
        "JLCPCB Rotation": "90",
        "Notes é": "hex-encoded UTF-8",
        "Notes é Details": "octal-encoded UTF-8",
        "Notes\tInternal": "keep # literal (parentheses)",
        "LCSC": "C200",
    }


@pytest.mark.parametrize("version", [7, 8], ids=["kicad7", "kicad8+"])
def test_comments_between_native_property_arguments_do_not_hide_existing_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    """Comment quotes and parentheses cannot split the alias from its value."""
    original = _schematic(version, "yes", ("R1",), reference="R1").replace(
        '"LCSC" "OLD"',
        '\n      # before the field name: "unclosed (\n'
        '      "JLCPCB Part Number"\n'
        '      # preserve "C100" here, with unmatched )))\n'
        '      "C100"',
    )
    expected = original.replace('      "C100"', '      "C200"')

    target = _save(tmp_path, monkeypatch, version, original, expected)

    assert target.fields["JLCPCB Part Number"] == "C200"
    assert "LCSC" not in target.fields


@pytest.mark.parametrize("version", [7, 8], ids=["kicad7", "kicad8+"])
@pytest.mark.parametrize("separator", ["\u2028", "\x85", "\v"])
def test_unicode_separator_stays_inside_comment_while_real_bom_flag_updates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: int,
    separator: str,
) -> None:
    """Only physical line endings terminate native comments containing BOM text."""
    comment = f'    # retain {separator}(in_bom yes) and an "unclosed quote (((\n'
    original = _schematic(version, "yes", ("R1",), reference="R1").replace(
        "    (in_bom yes)", comment + "    (in_bom yes)", 1
    )
    expected = original.replace('"LCSC" "OLD"', '"LCSC" "C200"').replace(
        "    (in_bom yes)", "    (in_bom no)", 1
    )

    _save(tmp_path, monkeypatch, version, original, expected, excluded=True)


@pytest.mark.parametrize("version", [7, 8], ids=["kicad7", "kicad8+"])
def test_escaped_placed_alias_does_not_change_library_or_named_variant_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    """The same name and value in nested records cannot join a base-field update."""
    library = r"""  (lib_symbols
    (symbol "Device:R"
      (property "\x4cCSC" "C100")
      (symbol "R_1_1")
    )
  )"""
    instances = r"""    (instances
      (project "board"
        (path "/root-R1"
          (reference "R1")
          (unit 1)
          (variant (name "Alternate")
            (in_bom no)
            (field (name "\x4cCSC") (value "C100"))
          )
          (variant (name "Cleared")
            (field (name "\x4cCSC") (value ""))
          )
        )
      )
    )
"""
    original = (
        _schematic(version, "yes", ("R1",), reference="R1", instance_text=instances)
        .replace("  (lib_symbols)", library)
        .replace('"LCSC" "OLD"', r'"\x4cCSC" "C100"')
    )
    expected = original.replace(
        "\n" + r'    (property "\x4cCSC" "C100"',
        "\n" + r'    (property "\x4cCSC" "C200"',
    ).replace("    (in_bom yes)", "    (in_bom no)", 1)

    target = _save(tmp_path, monkeypatch, version, original, expected, excluded=True)

    assert target.fields["LCSC"] == "C200"
    assert library in expected and instances in expected


@pytest.mark.parametrize("version", [7, 8], ids=["kicad7", "kicad8+"])
@pytest.mark.parametrize(
    "damage", ["missing-close", "extra-close", "unterminated-quote"]
)
def test_malformed_schematic_preserves_original_and_existing_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: int,
    damage: str,
) -> None:
    """A malformed document fails preparation before replacing files or backups."""
    original = _schematic(version, "yes", ("R1",), reference="R1")
    if damage == "missing-close":
        original = original[:-2]
    elif damage == "extra-close":
        original += ")\n"
    else:
        original = original[:-2] + '  (text "unterminated)\n)\n'
    path = tmp_path / "board.kicad_sch"
    backup = path.with_name(path.name + "_old")
    original_bytes = original.encode("utf-8")
    previous_backup = b"previous schematic backup\r\n\x00\xff"
    path.write_bytes(original_bytes)
    backup.write_bytes(previous_backup)

    with pytest.raises(ValueError):
        _load_schematic(
            tmp_path,
            monkeypatch,
            version,
            [path],
            [_row("C200", excluded=True)],
        )

    assert path.read_bytes() == original_bytes
    assert backup.read_bytes() == previous_backup
    assert set(tmp_path.iterdir()) == {path, backup}
