"""Tests for syncing schematic in_bom state from PCB parts."""

import importlib.util
from pathlib import Path
import re
import sys
import types

import pytest

_ROOT = Path(__file__).parent.parent

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
_version.is_version6 = lambda version: False  # type: ignore[attr-defined]
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


def _part(reference, lcsc, excluded):
    """Return one stored PCB part."""
    return {
        "reference": reference,
        "lcsc": lcsc,
        "exclude_from_bom": excluded,
    }


def _instance_block(refs, variant=False, project_name="board", include_foreign=True):
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
    version,
    initial_bom,
    refs,
    variant=False,
    reference="RV2",
    project_name="board",
    include_foreign=True,
    symbol_uuid="symbol-uuid",
    instance_text=None,
    locally_modified=False,
):
    """Return one placed symbol using the selected KiCad serialization."""
    if instance_text is None:
        instances = (
            ""
            if version == 6 or (len(refs) == 1 and not variant)
            else _instance_block(refs, variant, project_name, include_foreign)
        )
    else:
        instances = instance_text
    inline_lib_name = '(lib_name "Device:R_modified") ' if locally_modified else ""
    if version == 6:
        return f"""  (symbol {inline_lib_name}(lib_id "Device:R") (at 0 0 0) (unit 1)
    (in_bom {initial_bom}) (on_board yes)
    (uuid "{symbol_uuid}")
    (property "Reference" "{reference}" (id 0) (at 0 0 0))
    (property "LCSC" "OLD" (id 1) (at 0 0 0))
    (pin "1" (uuid "pin-uuid"))
  )"""
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
    version,
    initial_bom,
    refs,
    variant=False,
    reference="RV2",
    project_name="board",
    include_foreign=True,
    instance_text=None,
    locally_modified=False,
):
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


def _v6_root(refs):
    """Return a KiCad 6 root mapping reused child symbols by UUID."""
    paths = "\n".join(
        f'    (path "/sheet-{index}/symbol-uuid"\n'
        f'      (reference "{ref}") (unit 1)\n'
        "    )"
        for index, ref in enumerate(refs)
    )
    return f"""(kicad_sch
  (symbol_instances
{paths}
    (path "/other/other-uuid"
      (reference "RV99") (unit 1)
    )
  )
)
"""


def _project_api(board_project, loaded_projects):
    """Return a minimal pcbnew API with project identity information."""
    board = types.SimpleNamespace(GetProject=lambda: board_project)
    manager = types.SimpleNamespace(
        GetProject=lambda path: loaded_projects.get(Path(path).name)
    )
    return types.SimpleNamespace(
        GetBoard=lambda: board, GetSettingsManager=lambda: manager
    )


def _load_schematic(
    tmp_path,
    monkeypatch,
    version,
    paths,
    parts,
    board_name="board.kicad_pcb",
    pcbnew=None,
):
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
    monkeypatch.setattr(_module, "is_version6", lambda _: version == 6)
    monkeypatch.setattr(_module, "is_version7", lambda _: version == 7)
    exporter.load_schematic([str(path) for path in paths])


def _run_export(
    tmp_path,
    monkeypatch,
    version,
    initial_bom,
    refs,
    parts,
    variant=False,
    reference="RV2",
    project_name="board",
    include_foreign=True,
    instance_text=None,
    board_name="board.kicad_pcb",
    pcbnew=None,
    locally_modified=False,
):
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
    if version == 6:
        (tmp_path / f"{project_name}.kicad_sch").write_text(
            _v6_root(refs), encoding="utf-8"
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
    [6, 7, 8, 9, 10],
    ids=["kicad6", "kicad7", "kicad8", "kicad9", "kicad10"],
)
@pytest.mark.parametrize(("initial_bom", "refs", "parts", "expected_bom"), CASES)
def test_export_syncs_bom_without_changing_lcsc_resolution(
    tmp_path, monkeypatch, version, initial_bom, refs, parts, expected_bom
):
    """BOM sync handles each format while LCSC still follows the top reference."""
    parts = [*parts, _part("RV99", "FOREIGN", False)]
    result = _run_export(tmp_path, monkeypatch, version, initial_bom, refs, parts)

    assert re.findall(r"^\s*\(in_bom\s+(yes|no)\)", result, re.MULTILINE) == [
        expected_bom
    ]
    assert re.findall(r'\(property\s+"LCSC"\s+"([^"]*)"', result) == ["NEW"]
    if version in {6, 7}:
        assert f"(in_bom {expected_bom}) (on_board yes)" in result


@pytest.mark.parametrize(
    "version",
    [6, 7, 8, 9, 10],
    ids=["kicad6", "kicad7", "kicad8", "kicad9", "kicad10"],
)
def test_export_syncs_bom_for_locally_modified_symbol(tmp_path, monkeypatch, version):
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


@pytest.mark.parametrize("version", [8, 9, 10], ids=["kicad8", "kicad9", "kicad10"])
def test_export_updates_only_the_base_bom_token(tmp_path, monkeypatch, version):
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
@pytest.mark.parametrize("version", [8, 9, 10], ids=["kicad8", "kicad9", "kicad10"])
def test_export_resolves_instances_in_empty_project(
    tmp_path, monkeypatch, secondary_excluded, expected_bom, version
):
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
    [6, 7, 8, 9, 10],
    ids=["kicad6", "kicad7", "kicad8", "kicad9", "kicad10"],
)
def test_export_ignores_foreign_top_reference_for_bom(tmp_path, monkeypatch, version):
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


def test_kicad6_ignores_selected_instance_tables(tmp_path, monkeypatch):
    """A selected file is not assumed to be the active project's root."""
    child = tmp_path / "child.kicad_sch"
    child.write_text(_schematic(6, "yes", ("RV2", "RV6")), encoding="utf-8")
    selected_root = tmp_path / "alternate-root.kicad_sch"
    selected_root.write_text(_v6_root(("RV2", "RV6")), encoding="utf-8")
    (tmp_path / "alternate-root.kicad_pro").write_text("{}", encoding="utf-8")
    parts = [_part("RV2", "NEW", True), _part("RV6", "SECONDARY", True)]
    pcbnew = _project_api(None, {"alternate-root.kicad_pro": None})

    assert not (tmp_path / "board.kicad_sch").exists()
    _load_schematic(
        tmp_path, monkeypatch, 6, [child, selected_root], parts, pcbnew=pcbnew
    )
    result = child.read_text(encoding="utf-8")

    assert re.findall(r"^\s*\(in_bom\s+(yes|no)\)", result, re.MULTILINE) == ["yes"]
    assert re.findall(r'\(property\s+"LCSC"\s+"([^"]*)"', result) == ["NEW"]


@pytest.mark.parametrize(
    "version", [7, 8, 9, 10], ids=["kicad7", "kicad8", "kicad9", "kicad10"]
)
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
    tmp_path, monkeypatch, version, board_project, foreign_project, instance_text
):
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


@pytest.mark.parametrize("version", [8, 9, 10], ids=["kicad8", "kicad9", "kicad10"])
def test_export_keeps_symbol_resolution_independent(tmp_path, monkeypatch, version):
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
    [6, 7, 8, 9, 10],
    ids=["kicad6", "kicad7", "kicad8", "kicad9", "kicad10"],
)
def test_export_uses_loaded_project_for_renamed_board(tmp_path, monkeypatch, version):
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
