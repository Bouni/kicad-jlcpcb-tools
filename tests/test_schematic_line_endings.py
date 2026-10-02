"""Keep schematic bytes intact through real export and atomic backup workflows."""

from collections.abc import Iterator
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import IO, Any
import zipfile

import pytest

from tests.wx_harness import load_siblings, module


@pytest.fixture
def modules() -> Iterator[dict[str, ModuleType]]:
    """Load the real parser, renderer and writer without starting KiCad."""
    with load_siblings(
        "schematic_line_endings_test_plugin",
        ("schematic_fields", "schematic_safety", "schematic_links", "schematicexport"),
        {"pcbnew": module("pcbnew", GetBuildVersion=lambda: "8.0")},
    ) as loaded:
        yield loaded


@pytest.fixture
def windows_text_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Emulate Windows default text translation, honoring explicit newline args."""
    original_fdopen = os.fdopen

    def fdopen(fd: int, mode: str = "r", *args: Any, **kwargs: Any) -> IO[Any]:
        if "b" not in mode and kwargs.get("newline") is None:
            kwargs["newline"] = "\r\n"
        return original_fdopen(fd, mode, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", fdopen)


def _document(endings: tuple[str, ...], *, bom: bool, final: bool) -> str:
    """Build a schematic with Unicode and aliases whose surrounding bytes matter."""
    lines = [
        "(kicad_sch",
        '\t(uuid "root")',
        "\t(symbol",
        '\t\t(lib_id "Device:R")',
        '\t\t(uuid "symbol")',
        '\t\t(property "Reference" "R1" (at 1 2 0))',
        '\t\t(property "Value" "résistance Ω")',
        '\t\t(property "LCSC" "C100")',
        '\t\t(property "JLCPCB" "C100")',
        "\t\t(in_bom yes)",
        "\t)",
        ")",
    ]
    return ("\ufeff" if bom else "") + "".join(
        line
        + (endings[index % len(endings)] if final or index < len(lines) - 1 else "")
        for index, line in enumerate(lines)
    )


def _exporter(modules: dict[str, ModuleType], directory: Path) -> Any:
    """Construct the real exporter with just its project identity supplied."""
    return modules["schematicexport"].SchematicExport(
        SimpleNamespace(project_path=str(directory), board_name="board.kicad_pcb")
    )


def _row(value: str) -> dict[str, object]:
    """Supply a validated native Default row targeting the fixture symbol."""
    return {
        "component_id": "footprint",
        "schematic_path": "/root/symbol",
        "reference": "R1",
        "lcsc": value,
        "assignment_status": "valid" if value else "empty",
        "exclude_from_bom": True,
    }


@pytest.mark.usefixtures("windows_text_writes")
@pytest.mark.parametrize(
    ("endings", "bom", "final"),
    [
        pytest.param(("\n",), False, True, id="lf"),
        pytest.param(("\r\n",), True, False, id="crlf-bom-no-final-newline"),
        pytest.param(("\r",), False, True, id="cr"),
        pytest.param(
            ("\n", "\r\n", "\r"), True, False, id="mixed-bom-no-final-newline"
        ),
    ],
)
def test_export_replacements_clears_and_repeated_saves_preserve_bytes(
    tmp_path: Path,
    modules: dict[str, ModuleType],
    endings: tuple[str, ...],
    bom: bool,
    final: bool,
) -> None:
    """Reopening and saving changes only requested tokens, including both aliases."""
    path = tmp_path / "board.kicad_sch"
    original = _document(endings, bom=bom, final=final).encode("utf-8")
    path.write_bytes(original)
    previous = original
    for value in ("C200", "C200", "", ""):
        expected = original.replace(b'"C100"', f'"{value}"'.encode()).replace(
            b"(in_bom yes)", b"(in_bom no)"
        )
        _exporter(modules, tmp_path).load_schematic([str(path)], parts=[_row(value)])
        assert path.with_name(path.name + "_old").read_bytes() == previous
        assert path.read_bytes() == expected
        index = modules["schematic_links"].SchematicIndex.from_paths([str(path)])
        assert index.resolve("/root/symbol").lcsc == value
        previous = expected


@pytest.mark.parametrize("version7", [True, False], ids=["kicad7", "kicad8"])
@pytest.mark.parametrize(
    "endings",
    [("\n",), ("\r\n",), ("\r",), ("\n", "\r\n", "\r")],
    ids=["lf", "crlf", "cr", "mixed"],
)
def test_inserted_fields_follow_local_endings_and_tab_indentation(
    modules: dict[str, ModuleType], endings: tuple[str, ...], version7: bool
) -> None:
    """Each insertion follows its anchor; existing mixed endings stay untouched."""
    original = _document(endings, bom=True, final=False)
    original = (
        original.replace('\t\t(property "LCSC" "C100")', "")
        .replace('\t\t(property "JLCPCB" "C100")', "")
        .replace("\t\t(in_bom yes)", "")
    )
    # The lib_id begins after source line 2, Reference after source line 4.
    bom_ending = endings[2 % len(endings)]
    field_ending = endings[4 % len(endings)]
    if version7:
        inserted = field_ending.join(
            [
                "",
                '\t\t(property "LCSC" "C200" (at 1 2 0)',
                "\t\t  (effects (font (size 1.27 1.27)) hide)",
                "\t\t)",
            ]
        )
    else:
        inserted = field_ending.join(
            [
                "",
                '\t\t(property "LCSC" "C200"',
                "\t\t  (at 1 2 0)",
                "\t\t  (effects (font (size 1.27 1.27)) (hide yes))",
                "\t\t)",
            ]
        )
    reference = '(property "Reference" "R1" (at 1 2 0))'
    expected = original.replace(reference, reference + inserted).replace(
        '(lib_id "Device:R")', '(lib_id "Device:R")' + bom_ending + "\t\t(in_bom no)"
    )

    rendered = modules["schematic_fields"].update_symbol_fields(
        original, {"symbol": "C200"}, {"symbol": True}, version7=version7
    )

    assert rendered.encode("utf-8") == expected.encode("utf-8")
    assert (
        modules["schematic_fields"].update_symbol_fields(
            rendered, {"symbol": "C200"}, {"symbol": True}, version7=version7
        )
        == rendered
    )


@pytest.mark.parametrize("following", ["", "\r", "\r\n"])
def test_first_line_insertions_use_following_ending_or_lf_fallback(
    modules: dict[str, ModuleType], following: str
) -> None:
    """Minified anchors use four spaces and the next terminator, or LF if absent."""
    reference = '(property "Reference" "R1")'
    original = (
        '(kicad_sch (uuid "root") (symbol (lib_id "Device:R") '
        f'(uuid "symbol") {reference}{following}))'
    )
    ending = following or "\n"
    inserted = ending.join(
        [
            "",
            '    (property "LCSC" "C200"',
            "      (at 0 0 0)",
            "      (effects (font (size 1.27 1.27)) (hide yes))",
            "    )",
        ]
    )
    expected = original.replace(reference, reference + inserted).replace(
        '(lib_id "Device:R")', '(lib_id "Device:R")' + ending + "    (in_bom no)"
    )
    assert (
        modules["schematic_fields"].update_symbol_fields(
            original, {"symbol": "C200"}, {"symbol": True}, version7=False
        )
        == expected
    )


@pytest.mark.parametrize(
    ("endings", "comment_ending"),
    [
        (("\r",), "\r"),
        (("\r\n",), "\r\n"),
        (("\n",), "\n"),
        (("\n", "\r", "\r\n"), "\r\n"),
    ],
    ids=["cr", "crlf", "lf", "mixed"],
)
def test_comments_after_each_line_boundary_do_not_become_forms(
    tmp_path: Path,
    modules: dict[str, ModuleType],
    endings: tuple[str, ...],
    comment_ending: str,
) -> None:
    """Unbalanced comment punctuation is ignored by the index and field renderer."""
    original = _document(endings, bom=False, final=True)
    original = original.replace(
        '\t\t(property "Reference"',
        '\t\t# ignored unmatched ( " and ) )'
        + comment_ending
        + '\t\t(property "Reference"',
    )
    path = tmp_path / "board.kicad_sch"
    path.write_bytes(original.encode("utf-8"))

    _exporter(modules, tmp_path).load_schematic([str(path)], parts=[_row("C200")])

    expected = original.replace('"C100"', '"C200"').replace(
        "(in_bom yes)", "(in_bom no)"
    )
    assert path.read_bytes() == expected.encode("utf-8")


@pytest.mark.usefixtures("windows_text_writes")
def test_zip_backup_and_empty_writer_preserve_original_bytes(
    tmp_path: Path, modules: dict[str, ModuleType]
) -> None:
    """Binary archive and _old backups retain BOM, Unicode and mixed endings."""
    path = tmp_path / "board.kicad_sch"
    original = _document(("\r\n", "\r", "\n"), bom=True, final=False).encode("utf-8")
    path.write_bytes(original)
    safety = modules["schematic_safety"]
    archive = safety.backup_schematics(str(tmp_path), "board.kicad_pcb", "before.zip")
    with zipfile.ZipFile(archive) as backup:
        assert backup.read("board.kicad_sch") == original

    safety.atomic_write_schematic(str(path), "")

    assert path.read_bytes() == b""
    assert path.with_name(path.name + "_old").read_bytes() == original


def test_empty_schematic_fails_before_overwriting_files(
    tmp_path: Path, modules: dict[str, ModuleType]
) -> None:
    """An empty input is invalid even though the low-level writer accepts empty text."""
    path = tmp_path / "board.kicad_sch"
    path.write_bytes(b"")
    backup = path.with_name(path.name + "_old")
    backup.write_bytes(b"previous backup\r\n")

    with pytest.raises(ValueError, match="complete kicad_sch"):
        _exporter(modules, tmp_path).load_schematic([str(path)], parts=[_row("C200")])

    assert path.read_bytes() == b""
    assert backup.read_bytes() == b"previous backup\r\n"
    assert set(tmp_path.iterdir()) == {path, backup}
