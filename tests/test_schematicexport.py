"""Tests for syncing schematic in_bom state from PCB parts."""

from collections.abc import Sequence
import importlib.util
from pathlib import Path
import re
import sys
import types
from typing import Optional

import pytest

_ROOT = Path(__file__).parent.parent

# Hand-authored fixture reproducing the mixed-project table shape observed in
# KiCad's RoyalBlue54L-Feather demo. Geometry and identifiers are synthetic.
# https://gitlab.com/kicad/code/kicad/-/blob/9.0.5/demos/royalblue54L_feather/RoyalBlue54L-Feather.kicad_sch
_MIXED_PROJECT_FIXTURE = _ROOT / "tests/fixtures/kicad9_mixed_projects.kicad_sch"

_pcbnew = types.ModuleType("pcbnew")
_pcbnew.GetBuildVersion = lambda: "8.0"  # type: ignore[attr-defined]
sys.modules["pcbnew"] = _pcbnew

_package = types.ModuleType("kicadplugin")
_package.__path__ = [str(_ROOT)]
sys.modules["kicadplugin"] = _package

_core = types.ModuleType("kicadplugin.core")
_core.__path__ = [str(_ROOT / "core")]
sys.modules["kicadplugin.core"] = _core

_version = types.ModuleType("kicadplugin.core.version")
_version.is_version7 = lambda version: False  # type: ignore[attr-defined]
sys.modules["kicadplugin.core.version"] = _version

_spec = importlib.util.spec_from_file_location(
    "kicadplugin.schematicexport", _ROOT / "schematicexport.py"
)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
_module.__package__ = "kicadplugin"
sys.modules["kicadplugin.schematicexport"] = _module
_spec.loader.exec_module(_module)

SchematicExport = _module.SchematicExport


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
    exporter.load_schematic([str(path) for path in paths])


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
