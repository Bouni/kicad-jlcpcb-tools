"""Tests for SchematicExport's in_bom/exclude_from_bom sync (issue #328).

Keeps the schematic's in_bom flag in sync with the footprint's
exclude-from-BOM attribute.
"""

import importlib.util
from pathlib import Path
import sys
import types
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).parent.parent

# Mock KiCad modules before importing schematicexport
for _mod in ["pcbnew", "wx", "wx.dataview"]:
    sys.modules[_mod] = MagicMock()

_pkg = types.ModuleType("kicadplugin")
_pkg.__path__ = [str(_ROOT)]
sys.modules["kicadplugin"] = _pkg

_version_mod = types.ModuleType("kicadplugin.core.version")
_version_mod.is_version6 = lambda version: False
_version_mod.is_version7 = lambda version: False
_core_pkg = types.ModuleType("kicadplugin.core")
_core_pkg.__path__ = [str(_ROOT / "core")]
sys.modules["kicadplugin.core"] = _core_pkg
sys.modules["kicadplugin.core.version"] = _version_mod

_spec = importlib.util.spec_from_file_location(
    "kicadplugin.schematicexport", _ROOT / "schematicexport.py"
)
assert _spec is not None and _spec.loader is not None
_se_mod = importlib.util.module_from_spec(_spec)
_se_mod.__package__ = "kicadplugin"
sys.modules["kicadplugin.schematicexport"] = _se_mod
_spec.loader.exec_module(_se_mod)  # type: ignore[union-attr]

SchematicExport = _se_mod.SchematicExport  # type: ignore[attr-defined]

# Trimmed real-world fragment (KiCad 10 / v8+ format) of a single placed
# symbol, taken from KiCad's own bundled RoyalBlue54L-NFC-Antenna demo. The
# footprint side of this exact demo has "exclude_from_pos_files
# exclude_from_bom" set, while the schematic symbol still says "(in_bom
# yes)" - the mismatch reported in issue #328.
SCHEMATIC_FRAGMENT = """\
(kicad_sch
\t(version 20250114)
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Connector_Generic:Conn_01x02")
\t\t(at 156.21 111.76 180)
\t\t(unit 1)
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "d5224ac6-3b29-4f27-99e0-c4e878a39680")
\t\t(property "Reference" "J1"
\t\t\t(at 156.21 102.87 0)
\t\t)
\t\t(property "Value" "Conn_01x02"
\t\t\t(at 156.21 105.41 0)
\t\t)
\t\t(property "LCSC" "C123"
\t\t\t(at 156.21 111.76 0)
\t\t)
\t\t(pin "1"
\t\t\t(uuid "61bc920a-4459-4bb7-8012-ae3b9a30bead")
\t\t)
\t\t(pin "2"
\t\t\t(uuid "8f7ce03d-53e2-40cf-8c3f-27c3af97fc39")
\t\t)
\t)
)
"""


# A symbol placed via a reused hierarchical sheet: one symbol block, two
# instance references (RV2, RV6) sharing the single in_bom/LCSC slot.
REUSED_SHEET_FRAGMENT = """\
(kicad_sch
\t(version 20250114)
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 100 0)
\t\t(unit 1)
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "d5224ac6-3b29-4f27-99e0-c4e878a39681")
\t\t(property "Reference" "RV2"
\t\t\t(at 100 95 0)
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 97 0)
\t\t)
\t\t(property "LCSC" "C1"
\t\t\t(at 100 99 0)
\t\t)
\t\t(pin "1"
\t\t\t(uuid "61bc920a-4459-4bb7-8012-ae3b9a30bea1")
\t\t)
\t\t(pin "2"
\t\t\t(uuid "8f7ce03d-53e2-40cf-8c3f-27c3af97fc41")
\t\t)
\t\t(instances
\t\t\t(project "multichannel"
\t\t\t\t(path "/root/sheet1"
\t\t\t\t\t(reference "RV2")
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t\t(path "/root/sheet2"
\t\t\t\t\t(reference "RV6")
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t)
\t\t)
\t)
)
"""


# Same reused symbol, but its (instances ...) block also carries a foreign
# project's paths (e.g. this sheet is shared with a different board). Only
# the active board's own project ("myboard") should be used to resolve refs.
# The top-level Reference property is deliberately left as "RV99" - the
# foreign project's own reference - since that's whichever project last
# saved the shared sheet; it must not leak into the resolved ref set.
FOREIGN_PROJECT_FRAGMENT = """\
(kicad_sch
\t(version 20250114)
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 100 0)
\t\t(unit 1)
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "d5224ac6-3b29-4f27-99e0-c4e878a39682")
\t\t(property "Reference" "RV99"
\t\t\t(at 100 95 0)
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 97 0)
\t\t)
\t\t(property "LCSC" "C1"
\t\t\t(at 100 99 0)
\t\t)
\t\t(pin "1"
\t\t\t(uuid "61bc920a-4459-4bb7-8012-ae3b9a30bea2")
\t\t)
\t\t(pin "2"
\t\t\t(uuid "8f7ce03d-53e2-40cf-8c3f-27c3af97fc42")
\t\t)
\t\t(instances
\t\t\t(project "foreign_lib"
\t\t\t\t(path "/foreign/sheet1"
\t\t\t\t\t(reference "RV2")
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t\t(path "/foreign/sheet2"
\t\t\t\t\t(reference "RV99")
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t)
\t\t\t(project "myboard"
\t\t\t\t(path "/root/sheet1"
\t\t\t\t\t(reference "RV2")
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t\t(path "/root/sheet2"
\t\t\t\t\t(reference "RV6")
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t)
\t\t)
\t)
)
"""

# A reused sheet with no owning project - KiCad writes this as
# (project "" ...) rather than omitting the project block. The empty
# string is a valid project name, not "no project", and must still be
# used to collect instance references.
EMPTY_PROJECT_FRAGMENT = """\
(kicad_sch
\t(version 20250114)
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 100 0)
\t\t(unit 1)
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "d5224ac6-3b29-4f27-99e0-c4e878a39684")
\t\t(property "Reference" "RV2"
\t\t\t(at 100 95 0)
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 97 0)
\t\t)
\t\t(property "LCSC" "C1"
\t\t\t(at 100 99 0)
\t\t)
\t\t(pin "1"
\t\t\t(uuid "61bc920a-4459-4bb7-8012-ae3b9a30bea4")
\t\t)
\t\t(pin "2"
\t\t\t(uuid "8f7ce03d-53e2-40cf-8c3f-27c3af97fc44")
\t\t)
\t\t(instances
\t\t\t(project ""
\t\t\t\t(path "/root/sheet1"
\t\t\t\t\t(reference "RV2")
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t\t(path "/root/sheet2"
\t\t\t\t\t(reference "RV6")
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t)
\t\t)
\t)
)
"""

# A reused sheet whose (instances ...) block has two projects, neither of
# which is the active board's own project - genuinely ambiguous, unlike
# the single-foreign-project or single-project-fallback cases.
AMBIGUOUS_PROJECT_FRAGMENT = """\
(kicad_sch
\t(version 20250114)
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 100 0)
\t\t(unit 1)
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "d5224ac6-3b29-4f27-99e0-c4e878a39685")
\t\t(property "Reference" "RV2"
\t\t\t(at 100 95 0)
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 97 0)
\t\t)
\t\t(property "LCSC" "C1"
\t\t\t(at 100 99 0)
\t\t)
\t\t(pin "1"
\t\t\t(uuid "61bc920a-4459-4bb7-8012-ae3b9a30bea5")
\t\t)
\t\t(pin "2"
\t\t\t(uuid "8f7ce03d-53e2-40cf-8c3f-27c3af97fc45")
\t\t)
\t\t(instances
\t\t\t(project "projectA"
\t\t\t\t(path "/a/sheet1"
\t\t\t\t\t(reference "RVA")
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t)
\t\t\t(project "projectB"
\t\t\t\t(path "/b/sheet1"
\t\t\t\t\t(reference "RVB")
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t)
\t\t)
\t)
)
"""

# KiCad 7 format: same info as v8+ but each property stays on a single
# line instead of splitting the "(at ...)" onto its own line.
V7_REUSED_SHEET_FRAGMENT = """\
(kicad_sch
\t(version 20231120)
\t(lib_symbols
\t)
\t(symbol (lib_id "Device:R") (at 100 100 0) (unit 1)
\t\t(in_bom yes) (on_board yes) (dnp no)
\t\t(uuid "d5224ac6-3b29-4f27-99e0-c4e878a39683")
\t\t(property "Reference" "RV2" (at 100 95 0))
\t\t(property "Value" "10k" (at 100 97 0))
\t\t(property "LCSC" "C1" (at 100 99 0))
\t\t(pin "1" (uuid "61bc920a-4459-4bb7-8012-ae3b9a30bea3"))
\t\t(pin "2" (uuid "8f7ce03d-53e2-40cf-8c3f-27c3af97fc43"))
\t\t(instances
\t\t\t(project "board"
\t\t\t\t(path "/root/sheet1" (reference "RV2") (unit 1))
\t\t\t\t(path "/root/sheet2" (reference "RV6") (unit 1))
\t\t\t)
\t\t)
\t)
)
"""

# KiCad 6 format: reused-sheet references aren't inline on the symbol -
# they live in the project root's (symbol_instances ...) table, keyed by
# the symbol's own uuid.
V6_CHILD_FRAGMENT = """\
(kicad_sch
\t(version 20211123)
\t(lib_symbols
\t)
\t(symbol (lib_id "Device:R") (at 100 100 0) (unit 1)
\t\t(in_bom yes) (on_board yes)
\t\t(uuid sym-rv2)
\t\t(property "Reference" "RV2" (id 0) (at 100 95 0))
\t\t(property "Value" "10k" (id 1) (at 100 97 0))
\t\t(property "LCSC" "C1" (id 2) (at 100 99 0))
\t\t(pin "1" (uuid pin-1))
\t\t(pin "2" (uuid pin-2))
\t)
)
"""

# Same symbol, but its own top-level Reference is stale ("RV99", left
# over from whichever project last saved this shared sheet) rather than
# matching either instance in the root table.
V6_CHILD_STALE_REFERENCE_FRAGMENT = """\
(kicad_sch
\t(version 20211123)
\t(lib_symbols
\t)
\t(symbol (lib_id "Device:R") (at 100 100 0) (unit 1)
\t\t(in_bom yes) (on_board yes)
\t\t(uuid sym-rv2)
\t\t(property "Reference" "RV99" (id 0) (at 100 95 0))
\t\t(property "Value" "10k" (id 1) (at 100 97 0))
\t\t(property "LCSC" "C1" (id 2) (at 100 99 0))
\t\t(pin "1" (uuid pin-1))
\t\t(pin "2" (uuid pin-2))
\t)
)
"""

V6_ROOT_FRAGMENT = """\
(kicad_sch
\t(version 20211123)
\t(symbol_instances
\t\t(path "/sheet1-uuid/sym-rv2"
\t\t\t(reference "RV2") (unit 1)
\t\t)
\t\t(path "/sheet2-uuid/sym-rv2"
\t\t\t(reference "RV6") (unit 1)
\t\t)
\t)
)
"""


class FakeStore:
    """Stand-in for store.Store, returning canned part_info rows."""

    def __init__(self, parts):
        self._parts = parts

    def read_all(self):
        """Return the canned parts, matching Store.read_all()'s signature."""
        return self._parts


def _parent(parts, board_name="board.kicad_pcb", project_path=".", schematic_name=""):
    """Build a fake mainwindow-like parent for SchematicExport."""
    return types.SimpleNamespace(
        store=FakeStore(parts),
        board_name=board_name,
        project_path=project_path,
        schematic_name=schematic_name,
    )


def test_export_sets_in_bom_no_for_excluded_part(tmp_path):
    """Excluded footprint flips the schematic symbol's in_bom to "no".

    So KiCad's own Update PCB from Schematic sync doesn't reset the
    footprint's exclusion afterwards.
    """
    sch_path = tmp_path / "test.kicad_sch"
    sch_path.write_text(SCHEMATIC_FRAGMENT, encoding="utf-8")

    parent = _parent([{"reference": "J1", "lcsc": "C123", "exclude_from_bom": 1}])
    exporter = SchematicExport(parent)
    exporter._update_schematic(str(sch_path))

    result = sch_path.read_text(encoding="utf-8")
    assert "(in_bom no)" in result
    assert "(in_bom yes)" not in result


def test_export_keeps_in_bom_yes_for_included_part(tmp_path):
    """A footprint included in the BOM leaves in_bom untouched."""
    sch_path = tmp_path / "test.kicad_sch"
    sch_path.write_text(SCHEMATIC_FRAGMENT, encoding="utf-8")

    parent = _parent([{"reference": "J1", "lcsc": "C123", "exclude_from_bom": 0}])
    exporter = SchematicExport(parent)
    exporter._update_schematic(str(sch_path))

    result = sch_path.read_text(encoding="utf-8")
    assert "(in_bom yes)" in result
    assert "(in_bom no)" not in result


def test_export_updates_in_bom_when_reused_instances_agree(tmp_path):
    """A reused hierarchical sheet updates in_bom when every instance agrees."""
    sch_path = tmp_path / "test.kicad_sch"
    sch_path.write_text(REUSED_SHEET_FRAGMENT, encoding="utf-8")

    parent = _parent(
        [
            {"reference": "RV2", "lcsc": "C1", "exclude_from_bom": 1},
            {"reference": "RV6", "lcsc": "C1", "exclude_from_bom": 1},
        ],
        board_name="multichannel.kicad_pcb",
    )
    exporter = SchematicExport(parent)
    exporter._update_schematic(str(sch_path))

    result = sch_path.read_text(encoding="utf-8")
    assert "(in_bom no)" in result


def test_export_skips_in_bom_when_reused_instances_disagree(tmp_path):
    """Mixed exclude-from-BOM states across reused instances leave in_bom untouched."""
    sch_path = tmp_path / "test.kicad_sch"
    sch_path.write_text(REUSED_SHEET_FRAGMENT, encoding="utf-8")

    parent = _parent(
        [
            {"reference": "RV2", "lcsc": "C1", "exclude_from_bom": 1},
            {"reference": "RV6", "lcsc": "C1", "exclude_from_bom": 0},
        ],
        board_name="multichannel.kicad_pcb",
    )
    exporter = SchematicExport(parent)
    exporter._update_schematic(str(sch_path))

    result = sch_path.read_text(encoding="utf-8")
    assert "(in_bom yes)" in result
    assert "(in_bom no)" not in result


def test_export_updates_in_bom_when_instances_agree_on_bom_but_not_on_part(tmp_path):
    """BOM state is resolved on its own, independently of the parts fitted.

    Two instances of a reused symbol can carry different LCSC part
    numbers, from a per-instance override, while agreeing on whether
    they belong in the BOM. That agreement is enough to write in_bom.
    """
    sch_path = tmp_path / "test.kicad_sch"
    sch_path.write_text(REUSED_SHEET_FRAGMENT, encoding="utf-8")

    parent = _parent(
        [
            {"reference": "RV2", "lcsc": "C99", "exclude_from_bom": 1},
            {"reference": "RV6", "lcsc": "C2", "exclude_from_bom": 1},
        ],
        board_name="multichannel.kicad_pcb",
    )
    exporter = SchematicExport(parent)
    exporter._update_schematic(str(sch_path))

    result = sch_path.read_text(encoding="utf-8")
    assert "(in_bom no)" in result


def test_export_skips_in_bom_when_reused_instance_has_no_pcb_data(tmp_path):
    """A reused instance missing from the PCB store is treated as unsafe, not ignored.

    Regression test: matching only against the instances that *happen* to be
    in the store (instead of requiring all of them) would silently apply
    RV2's state to the shared symbol even though RV6's state is unknown.
    """
    sch_path = tmp_path / "test.kicad_sch"
    sch_path.write_text(REUSED_SHEET_FRAGMENT, encoding="utf-8")

    parent = _parent(
        [{"reference": "RV2", "lcsc": "C1", "exclude_from_bom": 1}],
        board_name="multichannel.kicad_pcb",
    )
    exporter = SchematicExport(parent)
    exporter._update_schematic(str(sch_path))

    result = sch_path.read_text(encoding="utf-8")
    assert "(in_bom yes)" in result
    assert "(in_bom no)" not in result


def test_export_ignores_foreign_project_instances(tmp_path):
    """Only the active board's own project resolves reused-sheet refs.

    Regression test: without project-name filtering, RV99 (from an
    unrelated "foreign_lib" project sharing this sheet) would be pulled
    into the ref set, and since RV99 has no PCB data, the whole update
    would incorrectly be skipped as "missing data".
    """
    sch_path = tmp_path / "test.kicad_sch"
    sch_path.write_text(FOREIGN_PROJECT_FRAGMENT, encoding="utf-8")

    parent = _parent(
        [
            {"reference": "RV2", "lcsc": "C1", "exclude_from_bom": 1},
            {"reference": "RV6", "lcsc": "C1", "exclude_from_bom": 1},
        ],
        board_name="myboard.kicad_pcb",
    )
    exporter = SchematicExport(parent)
    exporter._update_schematic(str(sch_path))

    result = sch_path.read_text(encoding="utf-8")
    assert "(in_bom no)" in result


def test_export_resolves_instances_with_empty_project_name(tmp_path):
    """A reused sheet with no owning project uses (project "" ...), not a missing block.

    Regression test: "" is a valid project name KiCad actually writes, not
    an absent one. Treating it as falsy would silently drop those instance
    refs and fall back to just the top-level Reference, missing the
    disagreement between RV2 and RV6 entirely.
    """
    sch_path = tmp_path / "test.kicad_sch"
    sch_path.write_text(EMPTY_PROJECT_FRAGMENT, encoding="utf-8")

    parent = _parent(
        [
            {"reference": "RV2", "lcsc": "C1", "exclude_from_bom": 1},
            {"reference": "RV6", "lcsc": "C1", "exclude_from_bom": 0},
        ],
        board_name="myboard.kicad_pcb",
    )
    exporter = SchematicExport(parent)
    exporter._update_schematic(str(sch_path))

    result = sch_path.read_text(encoding="utf-8")
    assert "(in_bom yes)" in result
    assert "(in_bom no)" not in result


def test_export_skips_in_bom_when_project_is_ambiguous(tmp_path):
    """Multiple instance projects, none matching the board, is unresolvable, not standalone.

    Regression test: falling back to the symbol's top-level Reference here
    would treat a genuinely ambiguous reused symbol as if it were a plain
    non-reused one, applying just RVA's state to a symbol that actually
    also has an unrelated RVB instance under a different project.
    """
    sch_path = tmp_path / "test.kicad_sch"
    sch_path.write_text(AMBIGUOUS_PROJECT_FRAGMENT, encoding="utf-8")

    parent = _parent(
        [{"reference": "RV2", "lcsc": "C1", "exclude_from_bom": 1}],
        board_name="myboard.kicad_pcb",
    )
    exporter = SchematicExport(parent)
    exporter._update_schematic(str(sch_path))

    result = sch_path.read_text(encoding="utf-8")
    assert "(in_bom yes)" in result
    assert "(in_bom no)" not in result


def test_export_updates_in_bom_for_kicad7_reused_instances(tmp_path):
    """The v7 code path also resolves reused-sheet instances correctly."""
    sch_path = tmp_path / "test.kicad_sch"
    sch_path.write_text(V7_REUSED_SHEET_FRAGMENT, encoding="utf-8")

    parent = _parent(
        [
            {"reference": "RV2", "lcsc": "C1", "exclude_from_bom": 1},
            {"reference": "RV6", "lcsc": "C1", "exclude_from_bom": 1},
        ],
        board_name="board.kicad_pcb",
    )
    exporter = SchematicExport(parent)
    exporter._update_schematic7(str(sch_path))

    result = sch_path.read_text(encoding="utf-8")
    assert "(in_bom no)" in result


def test_export_skips_in_bom_for_kicad7_when_reused_instances_disagree(tmp_path):
    """The v7 code path also leaves in_bom untouched when instances disagree."""
    sch_path = tmp_path / "test.kicad_sch"
    sch_path.write_text(V7_REUSED_SHEET_FRAGMENT, encoding="utf-8")

    parent = _parent(
        [
            {"reference": "RV2", "lcsc": "C1", "exclude_from_bom": 1},
            {"reference": "RV6", "lcsc": "C1", "exclude_from_bom": 0},
        ],
        board_name="board.kicad_pcb",
    )
    exporter = SchematicExport(parent)
    exporter._update_schematic7(str(sch_path))

    result = sch_path.read_text(encoding="utf-8")
    assert "(in_bom yes)" in result
    assert "(in_bom no)" not in result


def test_export_resolves_reused_instances_for_kicad6(tmp_path):
    """KiCad 6 resolves reused-sheet refs from the project's root symbol_instances table.

    Regression test: v6 doesn't embed (instances ...) inline on the symbol
    like v7/v8, so without reading the root table this would only ever see
    RV2 and never notice RV6 shares the same in_bom slot.
    """
    (tmp_path / "board.kicad_sch").write_text(V6_ROOT_FRAGMENT, encoding="utf-8")
    child_path = tmp_path / "child.kicad_sch"
    child_path.write_text(V6_CHILD_FRAGMENT, encoding="utf-8")

    parent = _parent(
        [
            {"reference": "RV2", "lcsc": "C1", "exclude_from_bom": 1},
            {"reference": "RV6", "lcsc": "C1", "exclude_from_bom": 1},
        ],
        project_path=str(tmp_path),
        schematic_name="board.kicad_sch",
    )
    exporter = SchematicExport(parent)
    v6_instance_refs = exporter._v6_root_instance_refs()
    exporter._update_schematic6(str(child_path), v6_instance_refs)

    result = child_path.read_text(encoding="utf-8")
    assert "(in_bom no)" in result


def test_export_skips_in_bom_when_kicad6_instances_disagree(tmp_path):
    """KiCad 6 reused instances that disagree leave in_bom untouched."""
    (tmp_path / "board.kicad_sch").write_text(V6_ROOT_FRAGMENT, encoding="utf-8")
    child_path = tmp_path / "child.kicad_sch"
    child_path.write_text(V6_CHILD_FRAGMENT, encoding="utf-8")

    parent = _parent(
        [
            {"reference": "RV2", "lcsc": "C1", "exclude_from_bom": 1},
            {"reference": "RV6", "lcsc": "C1", "exclude_from_bom": 0},
        ],
        project_path=str(tmp_path),
        schematic_name="board.kicad_sch",
    )
    exporter = SchematicExport(parent)
    v6_instance_refs = exporter._v6_root_instance_refs()
    exporter._update_schematic6(str(child_path), v6_instance_refs)

    result = child_path.read_text(encoding="utf-8")
    assert "(in_bom yes)" in result
    assert "(in_bom no)" not in result


def test_export_ignores_kicad6_table_outside_the_project_root(tmp_path):
    """Only the project's own root sheet provides the table, not any selected file.

    Regression test: the file dialog lets the user select any sheets, and
    a reused child sheet can carry the symbol_instances table of the
    project it was last saved as the root of. Trusting the first selected
    file holding a table resolves references against a foreign project.
    """
    foreign_root = tmp_path / "foreign_project.kicad_sch"
    foreign_root.write_text(V6_ROOT_FRAGMENT, encoding="utf-8")
    child_path = tmp_path / "child.kicad_sch"
    child_path.write_text(V6_CHILD_FRAGMENT, encoding="utf-8")

    parent = _parent(
        [
            {"reference": "RV2", "lcsc": "C1", "exclude_from_bom": 1},
            {"reference": "RV6", "lcsc": "C1", "exclude_from_bom": 1},
        ],
        board_name="phantom.kicad_pcb",
        project_path=str(tmp_path),
        schematic_name="phantom.kicad_sch",
    )
    exporter = SchematicExport(parent)
    v6_instance_refs = exporter._v6_root_instance_refs()
    exporter._update_schematic6(str(child_path), v6_instance_refs)

    result = child_path.read_text(encoding="utf-8")
    assert "(in_bom yes)" in result
    assert "(in_bom no)" not in result


def test_export_ignores_stale_top_reference_for_kicad6(tmp_path):
    """The root symbol_instances table is authoritative; a stale top-level Reference isn't mixed in.

    Regression test: unioning a stale/foreign top-level Reference (here
    "RV99", left over from another project saving the shared sheet) with
    the authoritative table lookup would add RV99 to the ref set. Since
    RV99 has no PCB data, that would incorrectly block an update that
    RV2 and RV6 (the table's real instances) both agree on.
    """
    (tmp_path / "board.kicad_sch").write_text(V6_ROOT_FRAGMENT, encoding="utf-8")
    child_path = tmp_path / "child.kicad_sch"
    child_path.write_text(V6_CHILD_STALE_REFERENCE_FRAGMENT, encoding="utf-8")

    parent = _parent(
        [
            {"reference": "RV2", "lcsc": "C1", "exclude_from_bom": 1},
            {"reference": "RV6", "lcsc": "C1", "exclude_from_bom": 1},
        ],
        project_path=str(tmp_path),
        schematic_name="board.kicad_sch",
    )
    exporter = SchematicExport(parent)
    v6_instance_refs = exporter._v6_root_instance_refs()
    exporter._update_schematic6(str(child_path), v6_instance_refs)

    result = child_path.read_text(encoding="utf-8")
    assert "(in_bom no)" in result


def test_export_skips_all_kicad6_symbols_when_table_not_found(tmp_path):
    """No BOM/LCSC updates happen for any symbol when the root table can't be located.

    Regression test: the warning logged in this case claims sync will be
    skipped. Falling back to each symbol's own top-level Reference here
    (instead of a real skip) would make that warning false, and would be
    unsafe since a truly reused symbol can't be told apart from a
    standalone one without the table.
    """
    child_path = tmp_path / "child.kicad_sch"
    child_path.write_text(V6_CHILD_FRAGMENT, encoding="utf-8")

    parent = _parent(
        [{"reference": "RV2", "lcsc": "C1", "exclude_from_bom": 1}],
        project_path=str(tmp_path),
        schematic_name="board.kicad_sch",
    )
    exporter = SchematicExport(parent)
    v6_instance_refs = exporter._v6_root_instance_refs()
    exporter._update_schematic6(str(child_path), v6_instance_refs)

    result = child_path.read_text(encoding="utf-8")
    assert "(in_bom yes)" in result
    assert "(in_bom no)" not in result
    assert '(property "LCSC" "C1"' in result


# A reused sheet carrying exactly one project, and it is not the board's.
SOLE_FOREIGN_PROJECT_FRAGMENT = """\
(kicad_sch
\t(version 20250114)
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 100 0)
\t\t(unit 1)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(uuid "d5224ac6-3b29-4f27-99e0-c4e878a39686")
\t\t(property "Reference" "RV2"
\t\t\t(at 100 95 0)
\t\t)
\t\t(property "LCSC" "C1"
\t\t\t(at 100 99 0)
\t\t)
\t\t(instances
\t\t\t(project "foreign_lib"
\t\t\t\t(path "/a/sheet1"
\t\t\t\t\t(reference "RV2")
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t\t(path "/a/sheet2"
\t\t\t\t\t(reference "RV6")
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t)
\t\t)
\t)
)
"""

# An (instances ...) container that holds no project at all.
EMPTY_INSTANCES_FRAGMENT = """\
(kicad_sch
\t(version 20250114)
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 100 0)
\t\t(unit 1)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(uuid "d5224ac6-3b29-4f27-99e0-c4e878a39687")
\t\t(property "Reference" "RV2"
\t\t\t(at 100 95 0)
\t\t)
\t\t(property "LCSC" "C1"
\t\t\t(at 100 99 0)
\t\t)
\t\t(instances
\t\t)
\t)
)
"""

# A standalone symbol followed by a sheet block, the layout KiCad writes
# for every hierarchical root: sheets come after the last symbol, each
# with an (instances ...) container of its own.
TRAILING_SHEET_FRAGMENT = """\
(kicad_sch
\t(version 20250114)
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 100 0)
\t\t(unit 1)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(uuid "d5224ac6-3b29-4f27-99e0-c4e878a39688")
\t\t(property "Reference" "RV2"
\t\t\t(at 100 95 0)
\t\t)
\t\t(property "LCSC" "C1"
\t\t\t(at 100 99 0)
\t\t)
\t)
\t(sheet
\t\t(at 200 200)
\t\t(uuid "5d6c7b8a-0000-4000-8000-000000000001")
\t\t(property "Sheetname" "channel"
\t\t\t(at 200 199 0)
\t\t)
\t\t(instances
\t\t\t(project "myboard"
\t\t\t\t(path "/5d6c7b8a-0000-4000-8000-000000000002"
\t\t\t\t\t(page "2")
\t\t\t\t)
\t\t\t)
\t\t)
\t)
)
"""


def test_export_skips_in_bom_for_a_sole_foreign_project(tmp_path):
    """One instance project that is not the board's own is foreign, not a fallback.

    A shared sheet keeps the instance data of the projects it was used
    in. If those references happen to exist on this PCB too, accepting
    them because they are the only ones present applies another
    project's placement to this board.
    """
    sch_path = tmp_path / "test.kicad_sch"
    sch_path.write_text(SOLE_FOREIGN_PROJECT_FRAGMENT, encoding="utf-8")

    parent = _parent(
        [
            {"reference": "RV2", "lcsc": "C1", "exclude_from_bom": 1},
            {"reference": "RV6", "lcsc": "C1", "exclude_from_bom": 1},
        ],
        board_name="myboard.kicad_pcb",
        project_path=str(tmp_path),
    )
    exporter = SchematicExport(parent)
    exporter._update_schematic(str(sch_path))

    result = sch_path.read_text(encoding="utf-8")
    assert "(in_bom yes)" in result
    assert "(in_bom no)" not in result


def test_export_resolves_project_name_from_the_project_file(tmp_path):
    """The project name comes from the .kicad_pro file, not from the board filename.

    A board saved under a different name still belongs to the project
    next to it. Deriving the name from the board file would make every
    instance group look foreign and skip the update.
    """
    (tmp_path / "realproject.kicad_pro").write_text("{}", encoding="utf-8")
    sch_path = tmp_path / "test.kicad_sch"
    sch_path.write_text(
        SOLE_FOREIGN_PROJECT_FRAGMENT.replace('"foreign_lib"', '"realproject"'),
        encoding="utf-8",
    )

    parent = _parent(
        [
            {"reference": "RV2", "lcsc": "C1", "exclude_from_bom": 1},
            {"reference": "RV6", "lcsc": "C1", "exclude_from_bom": 1},
        ],
        board_name="renamed_board.kicad_pcb",
        project_path=str(tmp_path),
    )
    exporter = SchematicExport(parent)
    exporter._update_schematic(str(sch_path))

    result = sch_path.read_text(encoding="utf-8")
    assert "(in_bom no)" in result


def test_export_skips_in_bom_for_an_empty_instances_block(tmp_path):
    """A present but empty (instances ...) block means resolution failed.

    The symbol carries instance data, so it is not standalone, and none
    of it names a project. Falling back to its own Reference here would
    use the one value that a reused symbol cannot be trusted on.
    """
    sch_path = tmp_path / "test.kicad_sch"
    sch_path.write_text(EMPTY_INSTANCES_FRAGMENT, encoding="utf-8")

    parent = _parent(
        [{"reference": "RV2", "lcsc": "C1", "exclude_from_bom": 1}],
        board_name="myboard.kicad_pcb",
        project_path=str(tmp_path),
    )
    exporter = SchematicExport(parent)
    exporter._update_schematic(str(sch_path))

    result = sch_path.read_text(encoding="utf-8")
    assert "(in_bom yes)" in result
    assert "(in_bom no)" not in result


def test_export_does_not_attribute_a_trailing_sheet_to_the_last_symbol(tmp_path):
    """Parsing of a symbol stops at its own closing paren.

    KiCad writes sheet blocks after the last symbol, and every sheet has
    an (instances ...) container. Collecting until the next symbol pulls
    that container into the last symbol, which then looks like a reused
    symbol whose instances resolve to nothing.
    """
    sch_path = tmp_path / "test.kicad_sch"
    sch_path.write_text(TRAILING_SHEET_FRAGMENT, encoding="utf-8")

    parent = _parent(
        [{"reference": "RV2", "lcsc": "C1", "exclude_from_bom": 1}],
        board_name="myboard.kicad_pcb",
        project_path=str(tmp_path),
    )
    exporter = SchematicExport(parent)
    exporter._update_schematic(str(sch_path))

    result = sch_path.read_text(encoding="utf-8")
    assert "(in_bom no)" in result


def test_load_schematic_dispatches_to_the_kicad6_path(tmp_path):
    """load_schematic reads the root table once and updates every selected sheet."""
    (tmp_path / "board.kicad_sch").write_text(V6_ROOT_FRAGMENT, encoding="utf-8")
    child_path = tmp_path / "child.kicad_sch"
    child_path.write_text(V6_CHILD_FRAGMENT, encoding="utf-8")

    parent = _parent(
        [
            {"reference": "RV2", "lcsc": "C1", "exclude_from_bom": 1},
            {"reference": "RV6", "lcsc": "C1", "exclude_from_bom": 1},
        ],
        project_path=str(tmp_path),
        schematic_name="board.kicad_sch",
    )
    exporter = SchematicExport(parent)
    with patch.object(_se_mod, "is_version6", lambda _version: True):
        exporter.load_schematic([str(child_path)])

    assert "(in_bom no)" in child_path.read_text(encoding="utf-8")


def test_load_schematic_dispatches_to_the_kicad7_path(tmp_path):
    """The v7 branch is reached through the public entry point."""
    sch_path = tmp_path / "test.kicad_sch"
    sch_path.write_text(V7_REUSED_SHEET_FRAGMENT, encoding="utf-8")

    parent = _parent(
        [
            {"reference": "RV2", "lcsc": "C1", "exclude_from_bom": 1},
            {"reference": "RV6", "lcsc": "C1", "exclude_from_bom": 1},
        ],
        project_path=str(tmp_path),
    )
    exporter = SchematicExport(parent)
    with patch.object(_se_mod, "is_version7", lambda _version: True):
        exporter.load_schematic([str(sch_path)])

    assert "(in_bom no)" in sch_path.read_text(encoding="utf-8")


def test_load_schematic_dispatches_to_the_kicad8_path(tmp_path):
    """The v8+ branch is reached through the public entry point."""
    sch_path = tmp_path / "test.kicad_sch"
    sch_path.write_text(SCHEMATIC_FRAGMENT, encoding="utf-8")

    parent = _parent(
        [{"reference": "J1", "lcsc": "C123", "exclude_from_bom": 1}],
        project_path=str(tmp_path),
    )
    exporter = SchematicExport(parent)
    exporter.load_schematic([str(sch_path)])

    assert "(in_bom no)" in sch_path.read_text(encoding="utf-8")


# Two placed symbols, the second one locally modified so KiCad writes
# (lib_name ...) ahead of its (lib_id ...). Real projects are full of
# these: 33 of KiCad 10's own demo schematics contain one.
MODIFIED_LIB_SYMBOL_FRAGMENT = """\
(kicad_sch
\t(version 20250114)
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 100 0)
\t\t(unit 1)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(uuid "d5224ac6-3b29-4f27-99e0-c4e878a39689")
\t\t(property "Reference" "R1"
\t\t\t(at 100 95 0)
\t\t)
\t\t(property "LCSC" "C1"
\t\t\t(at 100 99 0)
\t\t)
\t\t(pin "1"
\t\t\t(uuid "61bc920a-4459-4bb7-8012-ae3b9a30be01")
\t\t)
\t)
\t(symbol
\t\t(lib_name "Device:R_modified")
\t\t(lib_id "Device:R")
\t\t(at 120 100 0)
\t\t(unit 1)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(uuid "d5224ac6-3b29-4f27-99e0-c4e878a3968a")
\t\t(property "Reference" "R2"
\t\t\t(at 120 95 0)
\t\t)
\t\t(property "LCSC" "C2"
\t\t\t(at 120 99 0)
\t\t)
\t\t(pin "1"
\t\t\t(uuid "61bc920a-4459-4bb7-8012-ae3b9a30be02")
\t\t)
\t)
)
"""
