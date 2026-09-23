"""Tests for syncing schematic in_bom state from PCB parts."""

from collections.abc import Sequence
import importlib.util
import logging
import os
from pathlib import Path
import re
import types
from typing import Any, Optional

import pytest

from tests.wx_harness import temporary_modules

_ROOT = Path(__file__).parent.parent

# Hand-authored fixture reproducing the mixed-project table shape observed in
# KiCad's RoyalBlue54L-Feather demo. Geometry and identifiers are synthetic.
# https://gitlab.com/kicad/code/kicad/-/blob/9.0.5/demos/royalblue54L_feather/RoyalBlue54L-Feather.kicad_sch
_MIXED_PROJECT_FIXTURE = _ROOT / "tests/fixtures/kicad9_mixed_projects.kicad_sch"

_pcbnew = types.ModuleType("pcbnew")
_pcbnew.GetBuildVersion = lambda: "8.0"  # type: ignore[attr-defined]

_package = types.ModuleType("kicadplugin")
_package.__path__ = [str(_ROOT)]

_core = types.ModuleType("kicadplugin.core")
_core.__path__ = [str(_ROOT / "core")]

_version = types.ModuleType("kicadplugin.core.version")
_version.is_version7 = lambda version: False  # type: ignore[attr-defined]

_spec = importlib.util.spec_from_file_location(
    "kicadplugin.schematicexport", _ROOT / "schematicexport.py"
)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
_module.__package__ = "kicadplugin"
with temporary_modules(
    {
        "pcbnew": _pcbnew,
        "kicadplugin": _package,
        "kicadplugin.core": _core,
        "kicadplugin.core.version": _version,
        "kicadplugin.schematicexport": _module,
    },
    namespaces=("kicadplugin",),
):
    _spec.loader.exec_module(_module)

SchematicExport = _module.SchematicExport
SchematicLockedError = _module.SchematicLockedError


def _part(reference: str, lcsc: str, excluded: bool) -> dict[str, object]:
    """Return one stored PCB part."""
    return {
        "reference": reference,
        "lcsc": lcsc,
        "exclude_from_bom": excluded,
    }


def _instance_block(
    refs: Sequence[str],
    variant: bool = False,
    project_name: str = "board",
    include_foreign: bool = True,
) -> str:
    """Return nested KiCad 7+ instance groups."""
    paths = []
    for index, ref in enumerate(refs):
        if variant and index == 0:
            paths.append(
                f"""        (path "/sheet-{index}"
          (reference "{ref}")
          (unit 1)
          (variant
            (name "alternate")
            (in_bom yes)
          )
        )"""
            )
        else:
            paths.append(
                f'        (path "/sheet-{index}" (reference "{ref}") (unit 1))'
            )
    paths_text = "\n".join(paths)
    foreign = (
        """      (project "foreign"
        (path "/foreign" (reference "RV99") (unit 1))
      )
"""
        if include_foreign
        else ""
    )
    return f"""    (instances
{foreign}      (project "{project_name}"
{paths_text}
      )
    )
"""


def _symbol(
    version: int,
    initial_bom: str,
    refs: Sequence[str],
    variant: bool = False,
    reference: str = "RV2",
    project_name: str = "board",
    include_foreign: bool = True,
    symbol_uuid: str = "symbol-uuid",
    instance_text: Optional[str] = None,  # noqa: UP045
    locally_modified: bool = False,
) -> str:
    """Return one placed symbol using the selected KiCad serialization."""
    if instance_text is None:
        instances = (
            ""
            if len(refs) == 1 and not variant
            else _instance_block(refs, variant, project_name, include_foreign)
        )
    else:
        instances = instance_text
    inline_lib_name = '(lib_name "Device:R_modified") ' if locally_modified else ""
    if version == 7:
        return f"""  (symbol {inline_lib_name}(lib_id "Device:R") (at 0 0 0) (unit 1)
    (in_bom {initial_bom}) (on_board yes) (dnp no)
    (uuid "{symbol_uuid}")
    (property "Reference" "{reference}" (at 0 0 0))
    (property "LCSC" "OLD" (at 0 0 0))
    (pin "1" (uuid "pin-uuid"))
{instances}  )"""
    multiline_lib_name = (
        '    (lib_name "Device:R_modified")\n' if locally_modified else ""
    )
    return f"""  (symbol
{multiline_lib_name}    (lib_id "Device:R")
    (at 0 0 0)
    (unit 1)
    (in_bom {initial_bom})
    (on_board yes)
    (dnp no)
    (uuid "{symbol_uuid}")
    (property "Reference" "{reference}"
      (at 0 0 0)
    )
    (property "LCSC" "OLD"
      (at 0 0 0)
    )
    (pin "1" (uuid "pin-uuid"))
{instances}  )"""


def _schematic(
    version: int,
    initial_bom: str,
    refs: Sequence[str],
    variant: bool = False,
    reference: str = "RV2",
    project_name: str = "board",
    include_foreign: bool = True,
    instance_text: Optional[str] = None,  # noqa: UP045
    locally_modified: bool = False,
) -> str:
    """Return a minimal schematic using the selected KiCad serialization."""
    symbol = _symbol(
        version,
        initial_bom,
        refs,
        variant,
        reference,
        project_name,
        include_foreign,
        instance_text=instance_text,
        locally_modified=locally_modified,
    )
    return f"""(kicad_sch
  (lib_symbols)
{symbol}
)
"""


def _project_api(
    board_project: object, loaded_projects: dict[str, object]
) -> types.SimpleNamespace:
    """Return a minimal pcbnew API with project identity information."""
    board = types.SimpleNamespace(GetProject=lambda: board_project)
    manager = types.SimpleNamespace(
        GetProject=lambda path: loaded_projects.get(Path(path).name)
    )
    return types.SimpleNamespace(
        GetBoard=lambda: board, GetSettingsManager=lambda: manager
    )


def _ambiguous_project_api(
    tmp_path: Path,
    match_count: Optional[int],  # noqa: UP045
) -> types.SimpleNamespace:
    """Return project APIs without exactly one authenticated candidate."""
    if match_count is None:
        (tmp_path / "renamed_board.kicad_pro").write_text("{}", encoding="utf-8")
        return _project_api(None, {"renamed_board.kicad_pro": None})

    board_project = object()
    project_names = (
        ("renamed_board.kicad_pro",)
        if match_count == 0
        else ("renamed_board.kicad_pro", "realproject.kicad_pro")
    )
    loaded_projects = {}
    for project_name in project_names:
        (tmp_path / project_name).write_text("{}", encoding="utf-8")
        loaded_projects[project_name] = object() if match_count == 0 else board_project
    return _project_api(board_project, loaded_projects)


def _load_schematic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: int,
    paths: Sequence[Path],
    parts: list[dict[str, object]],
    board_name: str = "board.kicad_pcb",
    pcbnew: Optional[types.SimpleNamespace] = None,  # noqa: UP045
    approved_locks: Sequence[str] = (),
) -> None:
    """Load selected schematic paths using the requested KiCad version."""
    store = types.SimpleNamespace(read_all=lambda: parts)
    parent = types.SimpleNamespace(
        board_name=board_name,
        project_path=str(tmp_path),
        store=store,
        pcbnew=pcbnew,
    )
    exporter = SchematicExport(parent)
    monkeypatch.setattr(_module, "GetBuildVersion", lambda: str(version))
    monkeypatch.setattr(_module, "is_version7", lambda _: version == 7)
    exporter.load_schematic(
        [str(path) for path in paths], approved_locks=approved_locks
    )


def _run_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: int,
    initial_bom: str,
    refs: Sequence[str],
    parts: list[dict[str, object]],
    variant: bool = False,
    reference: str = "RV2",
    project_name: str = "board",
    include_foreign: bool = True,
    instance_text: Optional[str] = None,  # noqa: UP045
    board_name: str = "board.kicad_pcb",
    pcbnew: Optional[types.SimpleNamespace] = None,  # noqa: UP045
    locally_modified: bool = False,
) -> str:
    """Run the matching exporter and return the rewritten schematic."""
    path = tmp_path / "child.kicad_sch"
    path.write_text(
        _schematic(
            version,
            initial_bom,
            refs,
            variant,
            reference,
            project_name,
            include_foreign,
            instance_text,
            locally_modified,
        ),
        encoding="utf-8",
    )
    _load_schematic(
        tmp_path,
        monkeypatch,
        version,
        [path],
        parts,
        board_name=board_name,
        pcbnew=pcbnew,
    )
    return path.read_text(encoding="utf-8")


CASES = [
    pytest.param("yes", ("RV2",), [_part("RV2", "NEW", True)], "no", id="single"),
    pytest.param(
        "yes",
        ("RV2", "RV6"),
        [_part("RV2", "NEW", True), _part("RV6", "SECONDARY", True)],
        "no",
        id="reused-agree",
    ),
    pytest.param(
        "yes",
        ("RV2", "RV6"),
        [_part("RV2", "NEW", True), _part("RV6", "SECONDARY", False)],
        "yes",
        id="reused-disagree",
    ),
    pytest.param(
        "yes",
        ("RV2", "RV6"),
        [_part("RV2", "NEW", True)],
        "yes",
        id="reused-partial",
    ),
    pytest.param(
        "no",
        ("RV2", "RV6"),
        [_part("RV2", "NEW", False), _part("RV6", "SECONDARY", False)],
        "yes",
        id="reused-included",
    ),
]


@pytest.mark.parametrize(
    "version",
    [7, 8],
    ids=["kicad7", "kicad8+"],
)
@pytest.mark.parametrize(("initial_bom", "refs", "parts", "expected_bom"), CASES)
def test_export_syncs_bom_without_changing_lcsc_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: int,
    initial_bom: str,
    refs: Sequence[str],
    parts: list[dict[str, object]],
    expected_bom: str,
) -> None:
    """BOM sync handles each format while LCSC still follows the top reference."""
    parts = [*parts, _part("RV99", "FOREIGN", False)]
    result = _run_export(tmp_path, monkeypatch, version, initial_bom, refs, parts)

    assert re.findall(r"^\s*\(in_bom\s+(yes|no)\)", result, re.MULTILINE) == [
        expected_bom
    ]
    assert re.findall(r'\(property\s+"LCSC"\s+"([^"]*)"', result) == ["NEW"]
    if version == 7:
        assert f"(in_bom {expected_bom}) (on_board yes)" in result


@pytest.mark.parametrize("version", [7, 8], ids=["kicad7", "kicad8+"])
def test_export_inserts_lcsc_and_replaces_backup_on_repeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    """Repeated exports update one LCSC property and back up the previous state."""
    lcsc_property = (
        '    (property "LCSC" "OLD" (at 0 0 0))\n'
        if version == 7
        else '    (property "LCSC" "OLD"\n      (at 0 0 0)\n    )\n'
    )
    original = _schematic(version, "yes", ("RV2",)).replace(lcsc_property, "")
    path = tmp_path / "board.kicad_sch"
    backup = tmp_path / "board.kicad_sch_old"
    path.write_text(original, encoding="utf-8")
    parts = [_part("RV2", "FIRST", True)]

    _load_schematic(tmp_path, monkeypatch, version, [path], parts)
    first = path.read_text(encoding="utf-8")
    assert backup.read_text(encoding="utf-8") == original
    assert re.findall(r'\(property\s+"LCSC"\s+"([^"]*)"', first) == ["FIRST"]
    assert re.findall(r"^\s*\(in_bom\s+(yes|no)\)", first, re.MULTILINE) == ["no"]

    _load_schematic(
        tmp_path, monkeypatch, version, [path], [_part("RV2", "SECOND", False)]
    )
    second = path.read_text(encoding="utf-8")
    assert backup.read_text(encoding="utf-8") == first
    assert re.findall(r'\(property\s+"LCSC"\s+"([^"]*)"', second) == ["SECOND"]
    assert re.findall(r"^\s*\(in_bom\s+(yes|no)\)", second, re.MULTILINE) == ["yes"]


@pytest.mark.parametrize(
    "version",
    [7, 8],
    ids=["kicad7", "kicad8+"],
)
def test_export_syncs_bom_for_locally_modified_symbol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    """A lib_name before lib_id does not hide a placed symbol from BOM sync."""
    result = _run_export(
        tmp_path,
        monkeypatch,
        version,
        "yes",
        ("RV2",),
        [_part("RV2", "NEW", True)],
        locally_modified=True,
    )

    assert re.findall(r"^\s*\(in_bom\s+(yes|no)\)", result, re.MULTILINE) == ["no"]


@pytest.mark.parametrize("version", [8], ids=["kicad8+"])
def test_export_updates_only_the_base_bom_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    """A later per-variant in_bom override remains untouched."""
    parts = [_part("RV2", "NEW", True), _part("RV99", "FOREIGN", False)]
    result = _run_export(
        tmp_path, monkeypatch, version, "yes", ("RV2",), parts, variant=True
    )

    assert re.findall(r"^\s*\(in_bom\s+(yes|no)\)", result, re.MULTILINE) == [
        "no",
        "yes",
    ]


@pytest.mark.parametrize(
    ("secondary_excluded", "expected_bom"),
    [(True, "no"), (False, "yes")],
    ids=["agree", "disagree"],
)
@pytest.mark.parametrize("version", [8], ids=["kicad8+"])
def test_export_resolves_instances_in_empty_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    secondary_excluded: bool,
    expected_bom: str,
    version: int,
) -> None:
    """An empty project name resolves every instance in its sole group."""
    parts = [
        _part("RV2", "NEW", True),
        _part("RV6", "SECONDARY", secondary_excluded),
    ]
    result = _run_export(
        tmp_path,
        monkeypatch,
        version,
        "yes",
        ("RV2", "RV6"),
        parts,
        project_name="",
        include_foreign=False,
    )

    assert re.findall(r"^\s*\(in_bom\s+(yes|no)\)", result, re.MULTILINE) == [
        expected_bom
    ]


@pytest.mark.parametrize(
    "version",
    [7, 8],
    ids=["kicad7", "kicad8+"],
)
def test_export_ignores_foreign_top_reference_for_bom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    """BOM state follows active instances while LCSC follows the top reference."""
    parts = [
        _part("RV2", "ACTIVE", True),
        _part("RV6", "SECONDARY", True),
        _part("RV99", "TOP", False),
    ]
    result = _run_export(
        tmp_path,
        monkeypatch,
        version,
        "yes",
        ("RV2", "RV6"),
        parts,
        reference="RV99",
    )

    assert re.findall(r"^\s*\(in_bom\s+(yes|no)\)", result, re.MULTILINE) == ["no"]
    assert re.findall(r'\(property\s+"LCSC"\s+"([^"]*)"', result) == ["TOP"]


@pytest.mark.parametrize("version", [7, 8], ids=["kicad7", "kicad8+"])
@pytest.mark.parametrize(
    ("board_project", "foreign_project"),
    [(None, None), (object(), object())],
    ids=["unloaded", "different-project"],
)
@pytest.mark.parametrize(
    "instance_text",
    [
        pytest.param(
            _instance_block(
                ("RV2", "RV6"), project_name="foreign", include_foreign=False
            ),
            id="sole-foreign-project",
        ),
        pytest.param("    (instances\n    )\n", id="empty-instances"),
        pytest.param(
            _instance_block(("RV2", "RV6"), project_name="other", include_foreign=True),
            id="ambiguous-projects",
        ),
    ],
)
def test_export_skips_unresolved_instances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: int,
    board_project: object,
    foreign_project: object,
    instance_text: str,
) -> None:
    """Unresolved instance data must not fall back to the top reference."""
    parts = [
        _part("RV2", "NEW", True),
        _part("RV6", "SECONDARY", True),
        _part("RV99", "FOREIGN", True),
    ]
    (tmp_path / "foreign.kicad_pro").write_text("{}", encoding="utf-8")
    pcbnew = _project_api(board_project, {"foreign.kicad_pro": foreign_project})
    result = _run_export(
        tmp_path,
        monkeypatch,
        version,
        "yes",
        ("RV2", "RV6"),
        parts,
        instance_text=instance_text,
        pcbnew=pcbnew,
    )

    assert re.findall(r"^\s*\(in_bom\s+(yes|no)\)", result, re.MULTILINE) == ["yes"]
    assert re.findall(r'\(property\s+"LCSC"\s+"([^"]*)"', result) == ["NEW"]


def test_export_warns_for_stale_project_instances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A real-world mixed project table reports a skipped symbol."""
    caplog.set_level(logging.WARNING)
    path = tmp_path / "board.kicad_sch"
    path.write_text(_MIXED_PROJECT_FIXTURE.read_text(encoding="utf-8"))
    parts = [
        _part("R1", "STALE", False),
        _part("R2", "CURRENT", False),
    ]

    _load_schematic(tmp_path, monkeypatch, 9, [path], parts)
    result = path.read_text(encoding="utf-8")

    assert re.findall(r"^\s*\(in_bom\s+(yes|no)\)", result, re.MULTILINE) == [
        "no",
        "yes",
    ]
    assert caplog.messages == [
        "Not updating BOM state for R1; no instances resolve for project board"
    ]


@pytest.mark.parametrize("version", [8], ids=["kicad8+"])
def test_export_keeps_symbol_resolution_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    """Instance resolution state does not leak between consecutive symbols."""
    symbols = "\n".join(
        [
            _symbol(version, "yes", ("RV2", "RV6")),
            _symbol(
                version,
                "no",
                ("RV3",),
                reference="RV3",
                symbol_uuid="symbol-uuid-2",
            ),
        ]
    )
    path = tmp_path / "two-symbols.kicad_sch"
    path.write_text(
        f"""(kicad_sch
  (lib_symbols)
{symbols}
)
""",
        encoding="utf-8",
    )
    parts = [
        _part("RV2", "FIRST", True),
        _part("RV6", "SECONDARY", True),
        _part("RV3", "SECOND", False),
        _part("RV99", "FOREIGN", False),
    ]

    _load_schematic(tmp_path, monkeypatch, version, [path], parts)
    result = path.read_text(encoding="utf-8")

    assert re.findall(r"^\s*\(in_bom\s+(yes|no)\)", result, re.MULTILINE) == [
        "no",
        "yes",
    ]


@pytest.mark.parametrize(
    "version",
    [7, 8],
    ids=["kicad7", "kicad8+"],
)
def test_export_uses_loaded_project_for_renamed_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    """A renamed board resolves instances from its loaded KiCad project."""
    owner_project = object()
    foreign_project = object()
    (tmp_path / "realproject.kicad_pro").write_text("{}", encoding="utf-8")
    (tmp_path / "foreign.kicad_pro").write_text("{}", encoding="utf-8")
    pcbnew = _project_api(
        owner_project,
        {
            "realproject.kicad_pro": owner_project,
            "foreign.kicad_pro": foreign_project,
        },
    )
    parts = [
        _part("RV2", "NEW", True),
        _part("RV6", "SECONDARY", True),
        _part("RV99", "FOREIGN", False),
    ]

    result = _run_export(
        tmp_path,
        monkeypatch,
        version,
        "yes",
        ("RV2", "RV6"),
        parts,
        project_name="realproject",
        board_name="renamed_board.kicad_pcb",
        pcbnew=pcbnew,
    )

    assert re.findall(r"^\s*\(in_bom\s+(yes|no)\)", result, re.MULTILINE) == ["no"]
    assert re.findall(r'\(property\s+"LCSC"\s+"([^"]*)"', result) == ["NEW"]


@pytest.mark.parametrize(
    "version",
    [7, 8],
    ids=["kicad7", "kicad8+"],
)
@pytest.mark.parametrize(
    "match_count", [None, 0, 2], ids=["unloaded", "no-match", "multiple-matches"]
)
def test_export_skips_ambiguous_board_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    version: int,
    match_count: Optional[int],  # noqa: UP045
) -> None:
    """A board-stem instance group is unsafe without one authenticated project."""
    caplog.set_level(logging.WARNING)
    result = _run_export(
        tmp_path,
        monkeypatch,
        version,
        "yes",
        ("RV2", "RV6"),
        [_part("RV2", "NEW", True), _part("RV6", "SECONDARY", True)],
        project_name="renamed_board",
        board_name="renamed_board.kicad_pcb",
        pcbnew=_ambiguous_project_api(tmp_path, match_count),
    )

    assert re.findall(r"^\s*\(in_bom\s+(yes|no)\)", result, re.MULTILINE) == ["yes"]
    reason = (
        "open board has no project identity"
        if match_count is None
        else f"expected one matching .kicad_pro file, found {match_count}"
    )
    assert caplog.messages == [
        f"Not updating project-specific BOM states for renamed_board.kicad_pcb; {reason}"
    ]


@pytest.mark.parametrize("version", [7, 8], ids=["kicad7", "kicad8+"])
@pytest.mark.parametrize(
    "match_count", [None, 0, 2], ids=["unloaded", "no-match", "multiple-matches"]
)
def test_export_still_syncs_unscoped_symbol_when_project_is_ambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    version: int,
    match_count: Optional[int],  # noqa: UP045
) -> None:
    """Project authentication is unnecessary for an unscoped symbol."""
    caplog.set_level(logging.WARNING)
    symbols = "\n".join(
        [
            _symbol(
                version,
                "yes",
                ("RV2", "RV6"),
                project_name="renamed_board",
                include_foreign=False,
            ),
            _symbol(
                version,
                "yes",
                ("RV3",),
                reference="RV3",
                symbol_uuid="standalone-uuid",
            ),
        ]
    )
    path = tmp_path / "mixed-scope.kicad_sch"
    path.write_text(f"(kicad_sch\n  (lib_symbols)\n{symbols}\n)\n", encoding="utf-8")
    second_path = tmp_path / "second.kicad_sch"
    second_path.write_text(
        _schematic(
            version,
            "yes",
            ("RV2", "RV6"),
            project_name="renamed_board",
            include_foreign=False,
        ),
        encoding="utf-8",
    )
    parts = [
        _part("RV2", "FIRST", True),
        _part("RV6", "SECONDARY", True),
        _part("RV3", "STANDALONE", True),
    ]

    _load_schematic(
        tmp_path,
        monkeypatch,
        version,
        [path, second_path],
        parts,
        board_name="renamed_board.kicad_pcb",
        pcbnew=_ambiguous_project_api(tmp_path, match_count),
    )
    result = path.read_text(encoding="utf-8")

    assert re.findall(r"^\s*\(in_bom\s+(yes|no)\)", result, re.MULTILINE) == [
        "yes",
        "no",
    ]
    assert re.findall(
        r"^\s*\(in_bom\s+(yes|no)\)",
        second_path.read_text(encoding="utf-8"),
        re.MULTILINE,
    ) == ["yes"]
    reason = (
        "open board has no project identity"
        if match_count is None
        else f"expected one matching .kicad_pro file, found {match_count}"
    )
    assert caplog.messages == [
        f"Not updating project-specific BOM states for renamed_board.kicad_pcb; {reason}"
    ]


def _sheet(version: int, file_name: str) -> str:
    """Return a sheet symbol that uses file_name, in the selected serialization."""
    if version == 7:
        return f'  (sheet (at 100 50) (property "Sheetfile" "{file_name}" (at 100 60 0)))\n'
    return f"""  (sheet
    (at 100 50)
    (property "Sheetfile" "{file_name}"
      (at 100 60 0)
    )
  )
"""


@pytest.mark.parametrize("version", [7, 8], ids=["kicad7", "kicad8+"])
def test_export_writes_nothing_while_the_schematic_is_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    """A KiCad lock stops the export until the caller approves writing past it."""
    path = tmp_path / "board.kicad_sch"
    original = _schematic(version, "yes", ("RV2",))
    path.write_text(original, encoding="utf-8")
    (tmp_path / "~board.kicad_sch.lck").write_text(
        '{"hostname":"mac","username":"alice"}', encoding="utf-8"
    )
    parts = [_part("RV2", "NEW", False)]

    with pytest.raises(SchematicLockedError, match="locked by alice@mac") as raised:
        _load_schematic(tmp_path, monkeypatch, version, [path], parts)
    assert path.read_text(encoding="utf-8") == original
    assert not (tmp_path / "board.kicad_sch_old").exists()

    approved = [locked for locked, _info in raised.value.locks]
    _load_schematic(
        tmp_path, monkeypatch, version, [path], parts, approved_locks=approved
    )
    result = path.read_text(encoding="utf-8")
    assert re.findall(r'\(property\s+"LCSC"\s+"([^"]*)"', result) == ["NEW"]


@pytest.mark.parametrize("version", [7, 8], ids=["kicad7", "kicad8+"])
def test_export_writes_nothing_when_a_sheet_file_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    """A missing sheet stops the export before any sheet is rewritten."""
    root = tmp_path / "board.kicad_sch"
    child = tmp_path / "child.kicad_sch"
    root_text = _schematic(version, "yes", ("RV2",)).replace(
        "\n)\n",
        "\n"
        + _sheet(version, "child.kicad_sch")
        + _sheet(version, "gone.kicad_sch")
        + ")\n",
    )
    child_text = _schematic(version, "yes", ("RV3",), reference="RV3")
    root.write_text(root_text, encoding="utf-8")
    child.write_text(child_text, encoding="utf-8")
    parts = [_part("RV2", "NEW", False), _part("RV3", "CHILD", False)]

    with pytest.raises(
        FileNotFoundError, match="'gone.kicad_sch' used in 'board.kicad_sch'"
    ):
        _load_schematic(tmp_path, monkeypatch, version, [root], parts)

    assert root.read_text(encoding="utf-8") == root_text
    assert child.read_text(encoding="utf-8") == child_text
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "board.kicad_sch",
        "child.kicad_sch",
    ]


@pytest.mark.parametrize("target_exists", [False, True], ids=["missing", "existing"])
@pytest.mark.parametrize("version", [7, 8], ids=["kicad7", "kicad8+"])
def test_export_ignores_a_symbol_field_named_sheetfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int, target_exists: bool
) -> None:
    """A symbol's custom Sheetfile field is neither followed nor fatal."""
    lcsc_property = (
        '    (property "LCSC" "OLD" (at 0 0 0))\n'
        if version == 7
        else '    (property "LCSC" "OLD"\n      (at 0 0 0)\n    )\n'
    )
    custom_field = lcsc_property.replace(
        '"LCSC" "OLD"', '"Sheetfile" "notes.kicad_sch"'
    )
    path = tmp_path / "board.kicad_sch"
    path.write_text(
        _schematic(version, "yes", ("RV2",)).replace(
            lcsc_property, custom_field + lcsc_property
        ),
        encoding="utf-8",
    )
    notes = tmp_path / "notes.kicad_sch"
    notes_text = _schematic(version, "yes", ("RV9",), reference="RV9")
    if target_exists:
        notes.write_text(notes_text, encoding="utf-8")
    parts = [_part("RV2", "NEW", False), _part("RV9", "NINE", False)]

    _load_schematic(tmp_path, monkeypatch, version, [path], parts)

    result = path.read_text(encoding="utf-8")
    assert re.findall(r'\(property\s+"LCSC"\s+"([^"]*)"', result) == ["NEW"]
    assert '(property "Sheetfile" "notes.kicad_sch"' in result
    if target_exists:
        assert notes.read_text(encoding="utf-8") == notes_text
        assert not (tmp_path / "notes.kicad_sch_old").exists()


@pytest.mark.parametrize("version", [7, 8], ids=["kicad7", "kicad8+"])
def test_export_writes_nothing_past_an_unapproved_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    """Approving the lock on one sheet does not approve a lock on another."""
    root = tmp_path / "board.kicad_sch"
    child = tmp_path / "child.kicad_sch"
    root_text = _schematic(version, "yes", ("RV2",)).replace(
        "\n)\n", "\n" + _sheet(version, "child.kicad_sch") + ")\n"
    )
    child_text = _schematic(version, "yes", ("RV3",), reference="RV3")
    root.write_text(root_text, encoding="utf-8")
    child.write_text(child_text, encoding="utf-8")
    for locked in (root, child):
        (tmp_path / f"~{locked.name}.lck").write_text(
            '{"hostname":"mac","username":"alice"}', encoding="utf-8"
        )
    parts = [_part("RV2", "NEW", False), _part("RV3", "CHILD", False)]

    with pytest.raises(SchematicLockedError) as raised:
        _load_schematic(tmp_path, monkeypatch, version, [root], parts)
    locked = [path for path, _info in raised.value.locks]
    assert [Path(path).name for path in locked] == [
        "board.kicad_sch",
        "child.kicad_sch",
    ]

    with pytest.raises(SchematicLockedError, match="'child.kicad_sch' is locked"):
        _load_schematic(
            tmp_path, monkeypatch, version, [root], parts, approved_locks=locked[:1]
        )
    assert root.read_text(encoding="utf-8") == root_text
    assert child.read_text(encoding="utf-8") == child_text

    _load_schematic(
        tmp_path, monkeypatch, version, [root], parts, approved_locks=locked
    )
    assert re.findall(
        r'\(property\s+"LCSC"\s+"([^"]*)"', child.read_text(encoding="utf-8")
    ) == ["CHILD"]


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("version", [7, 8], ids=["kicad7", "kicad8+"])
def test_export_finds_the_lock_beside_a_symlinked_schematic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    """KiCad locks the path it opened, which may be a link to the file it writes."""
    target = tmp_path / "shared" / "real.kicad_sch"
    target.parent.mkdir()
    alias = tmp_path / "board.kicad_sch"
    original = _schematic(version, "yes", ("RV2",))
    target.write_text(original, encoding="utf-8")
    alias.symlink_to(target)
    (tmp_path / "~board.kicad_sch.lck").write_text(
        '{"hostname":"mac","username":"alice"}', encoding="utf-8"
    )
    parts = [_part("RV2", "NEW", False)]

    with pytest.raises(
        SchematicLockedError, match="'board.kicad_sch' is locked by alice@mac"
    ) as raised:
        _load_schematic(tmp_path, monkeypatch, version, [alias], parts)
    assert target.read_text(encoding="utf-8") == original

    approved = [locked for locked, _info in raised.value.locks]
    _load_schematic(
        tmp_path, monkeypatch, version, [alias], parts, approved_locks=approved
    )
    assert alias.is_symlink()
    result = target.read_text(encoding="utf-8")
    assert re.findall(r'\(property\s+"LCSC"\s+"([^"]*)"', result) == ["NEW"]
    backup = target.parent / "real.kicad_sch_old"
    assert backup.read_text(encoding="utf-8") == original
    assert not (tmp_path / "board.kicad_sch_old").exists()


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("version", [7, 8], ids=["kicad7", "kicad8+"])
def test_export_checks_every_name_of_a_shared_schematic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    """A lock beside any name of a schematic stops the export; it is written once."""
    target = tmp_path / "real.kicad_sch"
    alias = tmp_path / "board.kicad_sch"
    original = _schematic(version, "yes", ("RV2",))
    target.write_text(original, encoding="utf-8")
    alias.symlink_to(target)
    (tmp_path / "~board.kicad_sch.lck").write_text(
        '{"hostname":"mac","username":"alice"}', encoding="utf-8"
    )
    parts = [_part("RV2", "NEW", False)]

    with pytest.raises(SchematicLockedError, match="'board.kicad_sch'") as raised:
        _load_schematic(tmp_path, monkeypatch, version, [target, alias], parts)
    assert target.read_text(encoding="utf-8") == original

    approved = [locked for locked, _info in raised.value.locks]
    _load_schematic(
        tmp_path, monkeypatch, version, [target, alias], parts, approved_locks=approved
    )
    result = target.read_text(encoding="utf-8")
    assert re.findall(r'\(property\s+"LCSC"\s+"([^"]*)"', result) == ["NEW"]
    # Written once: the backup holds the original, not an already exported copy.
    backup = tmp_path / "real.kicad_sch_old"
    assert backup.read_text(encoding="utf-8") == original


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("first_beside", ["link", "target"])
@pytest.mark.parametrize("version", [7, 8], ids=["kicad7", "kicad8+"])
def test_export_reports_a_lock_that_appears_beside_the_other_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int, first_beside: str
) -> None:
    """Approving the lock the user saw does not approve one taken since, elsewhere."""
    target = tmp_path / "shared" / "real.kicad_sch"
    target.parent.mkdir()
    alias = tmp_path / "board.kicad_sch"
    original = _schematic(version, "yes", ("RV2",))
    target.write_text(original, encoding="utf-8")
    alias.symlink_to(target)
    first, second = (alias, target) if first_beside == "link" else (target, alias)
    parts = [_part("RV2", "NEW", False)]

    (first.parent / f"~{first.name}.lck").write_text(
        '{"hostname":"mac","username":"alice"}', encoding="utf-8"
    )
    with pytest.raises(SchematicLockedError) as raised:
        _load_schematic(tmp_path, monkeypatch, version, [alias], parts)
    approved = [locked for locked, _info in raised.value.locks]

    (second.parent / f"~{second.name}.lck").write_text(
        '{"hostname":"mac","username":"bob"}', encoding="utf-8"
    )
    with pytest.raises(
        SchematicLockedError, match=f"'{second.name}' is locked by bob"
    ) as raised:
        _load_schematic(
            tmp_path, monkeypatch, version, [alias], parts, approved_locks=approved
        )
    assert target.read_text(encoding="utf-8") == original
    assert not list(tmp_path.rglob("*_old"))

    approved += [locked for locked, _info in raised.value.locks]
    _load_schematic(
        tmp_path, monkeypatch, version, [alias], parts, approved_locks=approved
    )
    result = target.read_text(encoding="utf-8")
    assert re.findall(r'\(property\s+"LCSC"\s+"([^"]*)"', result) == ["NEW"]


def _two_named_child(
    tmp_path: Path, version: int, first: str, second: str
) -> tuple[Path, str]:
    """Write a root using a child sheet under two names; return the child and its text."""
    root = tmp_path / "board.kicad_sch"
    root.write_text(
        _schematic(version, "yes", ("RV2",)).replace(
            "\n)\n",
            "\n" + _sheet(version, first) + _sheet(version, second) + ")\n",
        ),
        encoding="utf-8",
    )
    child = tmp_path / first
    child_text = _schematic(version, "yes", ("RV3",), reference="RV3")
    child.write_text(child_text, encoding="utf-8")
    return child, child_text


def _lcsc_values(path: Path) -> list[str]:
    return re.findall(
        r'\(property\s+"LCSC"\s+"([^"]*)"', path.read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("version", [7, 8], ids=["kicad7", "kicad8+"])
def test_export_writes_every_hard_linked_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    """Replacing one name of a hard-linked sheet leaves the other; both get the export."""
    child, child_text = _two_named_child(
        tmp_path, version, "child.kicad_sch", "twin.kicad_sch"
    )
    twin = tmp_path / "twin.kicad_sch"
    os.link(child, twin)
    parts = [_part("RV2", "NEW", False), _part("RV3", "CHILD", False)]

    _load_schematic(
        tmp_path, monkeypatch, version, [tmp_path / "board.kicad_sch"], parts
    )

    assert _lcsc_values(child) == ["CHILD"]
    assert _lcsc_values(twin) == ["CHILD"]
    for backup in (tmp_path / "child.kicad_sch_old", tmp_path / "twin.kicad_sch_old"):
        assert backup.read_text(encoding="utf-8") == child_text


def _case_insensitive(tmp_path: Path) -> bool:
    (tmp_path / "CaseProbe").touch()
    return (tmp_path / "caseprobe").exists()


@pytest.mark.parametrize("version", [7, 8], ids=["kicad7", "kicad8+"])
def test_export_writes_a_case_aliased_sheet_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    """Two spellings of one directory entry are one write, so the backup is the original."""
    if not _case_insensitive(tmp_path):
        pytest.skip("needs a case-insensitive filesystem")
    child, child_text = _two_named_child(
        tmp_path, version, "child.kicad_sch", "Child.kicad_sch"
    )
    parts = [_part("RV2", "NEW", False), _part("RV3", "CHILD", False)]

    _load_schematic(
        tmp_path, monkeypatch, version, [tmp_path / "board.kicad_sch"], parts
    )

    assert _lcsc_values(child) == ["CHILD"]
    assert (tmp_path / "child.kicad_sch_old").read_text(encoding="utf-8") == child_text


def _directory_aliases(tmp_path: Path, version: int) -> tuple[Path, Path, str]:
    """Reach shared/sub.kicad_sch as a/block/sub and b/block/sub; sub uses ../child."""
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "sub.kicad_sch").write_text(
        _schematic(version, "yes", ("RV3",), reference="RV3").replace(
            "\n)\n", "\n" + _sheet(version, "../child.kicad_sch") + ")\n"
        ),
        encoding="utf-8",
    )
    child_text = _schematic(version, "yes", ("RV4",), reference="RV4")
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "block").symlink_to(shared, target_is_directory=True)
        (tmp_path / name / "child.kicad_sch").write_text(child_text, encoding="utf-8")
    (tmp_path / "board.kicad_sch").write_text(
        _schematic(version, "yes", ("RV2",)).replace(
            "\n)\n",
            "\n"
            + _sheet(version, "a/block/sub.kicad_sch")
            + _sheet(version, "b/block/sub.kicad_sch")
            + ")\n",
        ),
        encoding="utf-8",
    )
    return (
        tmp_path / "a" / "child.kicad_sch",
        tmp_path / "b" / "child.kicad_sch",
        child_text,
    )


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("second_child", ["present", "missing", "locked"])
@pytest.mark.parametrize("version", [7, 8], ids=["kicad7", "kicad8+"])
def test_export_follows_each_directory_alias_to_its_own_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int, second_child: str
) -> None:
    """One sheet under two directory links has two ../child files, each checked."""
    first, second, child_text = _directory_aliases(tmp_path, version)
    parts = [
        _part("RV2", "NEW", False),
        _part("RV3", "SUB", False),
        _part("RV4", "CHILD", False),
    ]
    root = tmp_path / "board.kicad_sch"

    if second_child == "present":
        _load_schematic(tmp_path, monkeypatch, version, [root], parts)
        assert _lcsc_values(tmp_path / "shared" / "sub.kicad_sch") == ["SUB"]
        assert _lcsc_values(first) == ["CHILD"]
        assert _lcsc_values(second) == ["CHILD"]
        return

    if second_child == "missing":
        second.unlink()
        expected: Any = pytest.raises(
            FileNotFoundError, match="Sheet file '../child.kicad_sch'"
        )
    else:
        (second.parent / "~child.kicad_sch.lck").write_text(
            '{"hostname":"mac","username":"alice"}', encoding="utf-8"
        )
        expected = pytest.raises(SchematicLockedError, match="locked by alice@mac")
    with expected:
        _load_schematic(tmp_path, monkeypatch, version, [root], parts)
    assert _lcsc_values(root) == ["OLD"]
    assert _lcsc_values(first) == ["OLD"]
    assert not list(tmp_path.rglob("*_old"))


@pytest.mark.parametrize("version", [7, 8], ids=["kicad7", "kicad8+"])
def test_export_checks_the_lock_of_the_project_schematic_that_is_not_a_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    """KiCad always opens and locks <project>.kicad_sch, even when it is no root."""
    from schematic_safety import resolve_project_schematics

    board = tmp_path / "board.kicad_sch"
    board_text = _schematic(version, "yes", ("RV2",))
    board.write_text(board_text, encoding="utf-8")
    power = tmp_path / "power.kicad_sch"
    power.write_text(
        _schematic(version, "yes", ("RV3",), reference="RV3"), encoding="utf-8"
    )
    (tmp_path / "board.kicad_pro").write_text(
        '{"schematic": {"top_level_sheets": [{"filename": "power.kicad_sch"}]}}',
        encoding="utf-8",
    )
    (tmp_path / "~board.kicad_sch.lck").write_text(
        '{"hostname":"mac","username":"alice"}', encoding="utf-8"
    )
    parts = [_part("RV3", "POWER", False)]
    roots = resolve_project_schematics(str(tmp_path), "board.kicad_pcb")
    assert roots == [str(power)]

    with pytest.raises(
        SchematicLockedError, match="'board.kicad_sch' is locked by alice@mac"
    ) as raised:
        _load_schematic(tmp_path, monkeypatch, version, [power], parts)
    assert _lcsc_values(power) == ["OLD"]

    approved = [locked for locked, _info in raised.value.locks]
    _load_schematic(
        tmp_path, monkeypatch, version, [power], parts, approved_locks=approved
    )
    assert _lcsc_values(power) == ["POWER"]
    assert board.read_text(encoding="utf-8") == board_text
    assert not (tmp_path / "board.kicad_sch_old").exists()


@pytest.mark.parametrize("version", [7, 8], ids=["kicad7", "kicad8+"])
def test_export_writes_nothing_when_a_sheet_is_not_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    """A sheet that cannot be read stops the export before any sheet is written."""
    child, _child_text = _two_named_child(
        tmp_path, version, "child.kicad_sch", "child.kicad_sch"
    )
    root = tmp_path / "board.kicad_sch"
    root_text = root.read_text(encoding="utf-8")
    child.write_bytes(b"(kicad_sch \xb5)\n")
    parts = [_part("RV2", "NEW", False), _part("RV3", "CHILD", False)]

    with pytest.raises(ValueError, match="Sheet file 'child.kicad_sch' is not UTF-8"):
        _load_schematic(tmp_path, monkeypatch, version, [root], parts)
    assert root.read_text(encoding="utf-8") == root_text
    assert not list(tmp_path.rglob("*_old"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
@pytest.mark.parametrize("version", [7, 8], ids=["kicad7", "kicad8+"])
def test_export_writes_nothing_when_a_sheet_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    """A read-only sheet, which KiCad would refuse to save, stops the export first."""
    child, _child_text = _two_named_child(
        tmp_path, version, "child.kicad_sch", "child.kicad_sch"
    )
    root = tmp_path / "board.kicad_sch"
    root_text = root.read_text(encoding="utf-8")
    child.chmod(0o444)
    parts = [_part("RV2", "NEW", False), _part("RV3", "CHILD", False)]

    with pytest.raises(PermissionError, match="'child.kicad_sch' is read-only"):
        _load_schematic(tmp_path, monkeypatch, version, [root], parts)
    assert root.read_text(encoding="utf-8") == root_text
    assert not list(tmp_path.rglob("*_old"))
