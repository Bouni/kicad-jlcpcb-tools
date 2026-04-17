"""Tests for the two-pass correction matching logic in Fabrication._find_correction."""

import importlib.util
import sys
import types
from unittest.mock import MagicMock

# Mock KiCad modules before importing fabrication
for _mod in ["pcbnew", "wx", "wx.dataview"]:
    sys.modules[_mod] = MagicMock()

# fabrication.py uses relative imports, so give it a fake parent package
_pkg = types.ModuleType("kicadplugin")
_pkg.__path__ = ["."]
sys.modules["kicadplugin"] = _pkg

_helpers = types.ModuleType("kicadplugin.helpers")
_helpers.get_is_dnp = lambda fp: False  # type: ignore[attr-defined]
sys.modules["kicadplugin.helpers"] = _helpers

_spec = importlib.util.spec_from_file_location(
    "kicadplugin.fabrication", "fabrication.py"
)
assert _spec is not None and _spec.loader is not None
_fab_mod = importlib.util.module_from_spec(_spec)
_fab_mod.__package__ = "kicadplugin"
sys.modules["kicadplugin.fabrication"] = _fab_mod
_spec.loader.exec_module(_fab_mod)  # type: ignore[union-attr]

Fabrication = _fab_mod.Fabrication  # type: ignore[attr-defined]


def make_fab(corrections):
    """Create a bare Fabrication instance with the given corrections list."""
    fab = object.__new__(Fabrication)
    fab.corrections = corrections
    return fab


# ---------------------------------------------------------------------------
# Anchored-first conflict resolution
# ---------------------------------------------------------------------------


class TestFindCorrectionConflictResolution:
    """_find_correction prefers exact-suffix (anchored) matches over substring matches."""

    def test_specific_pattern_wins_over_prefix(self):
        """SOT-23-3 correction wins over the shorter SOT-23 pattern."""
        fab = make_fab([
            ("SOT-23", 10, (0.0, 0.0)),
            ("SOT-23-3", 20, (0.0, 0.0)),
        ])
        rotation, _ = fab._find_correction("SOT-23-3")
        assert rotation == 20

    def test_shorter_pattern_still_matches_its_own_value(self):
        """SOT-23 correction is used when the value is exactly SOT-23."""
        fab = make_fab([
            ("SOT-23", 10, (0.0, 0.0)),
            ("SOT-23-3", 20, (0.0, 0.0)),
        ])
        rotation, _ = fab._find_correction("SOT-23")
        assert rotation == 10

    def test_order_in_list_does_not_matter(self):
        """Anchored match wins regardless of which pattern is listed first."""
        fab = make_fab([
            ("SOT-23-3", 20, (0.0, 0.0)),
            ("SOT-23", 10, (0.0, 0.0)),
        ])
        rotation, _ = fab._find_correction("SOT-23-3")
        assert rotation == 20

    def test_three_way_conflict_most_specific_wins(self):
        """Most specific (longest exact-suffix) pattern wins in a three-way conflict."""
        fab = make_fab([
            ("SOT", 5, (0.0, 0.0)),
            ("SOT-23", 10, (0.0, 0.0)),
            ("SOT-23-3", 20, (0.0, 0.0)),
        ])
        rotation, _ = fab._find_correction("SOT-23-3")
        assert rotation == 20


# ---------------------------------------------------------------------------
# Unanchored fallback
# ---------------------------------------------------------------------------


class TestFindCorrectionUnanchoredFallback:
    """When no anchored match exists, the first substring match is used."""

    def test_substring_match_used_when_no_conflict(self):
        """A substring pattern matches when it is the only candidate."""
        fab = make_fab([("SOT-23", 10, (0.0, 0.0))])
        rotation, _ = fab._find_correction("Package_TO_SOT_SMD:SOT-23")
        assert rotation == 10

    def test_no_match_returns_none(self):
        """Returns None when no pattern matches the value."""
        fab = make_fab([("SOT-23", 10, (0.0, 0.0))])
        assert fab._find_correction("QFP-100") is None

    def test_empty_corrections_returns_none(self):
        """Returns None when the corrections list is empty."""
        fab = make_fab([])
        assert fab._find_correction("SOT-23-3") is None


# ---------------------------------------------------------------------------
# Alternation patterns
# ---------------------------------------------------------------------------


class TestFindCorrectionAlternation:
    """Alternation patterns (|) are wrapped so $ anchors all branches."""

    def test_alternation_matches_first_branch(self):
        """An alternation pattern matches the first branch correctly."""
        fab = make_fab([("SOT-23-3|SOT-23-5", 20, (0.0, 0.0))])
        rotation, _ = fab._find_correction("SOT-23-3")
        assert rotation == 20

    def test_alternation_matches_second_branch(self):
        """An alternation pattern matches the second branch correctly."""
        fab = make_fab([("SOT-23-3|SOT-23-5", 20, (0.0, 0.0))])
        rotation, _ = fab._find_correction("SOT-23-5")
        assert rotation == 20

    def test_alternation_anchored_does_not_match_extended_value(self):
        """SOT-23-3|SOT-23-5 does not match SOT-23-30 via the first branch."""
        fab = make_fab([
            ("SOT-23-3|SOT-23-5", 20, (0.0, 0.0)),
            ("SOT-23-30", 30, (0.0, 0.0)),
        ])
        rotation, _ = fab._find_correction("SOT-23-30")
        assert rotation == 30

    def test_alternation_falls_back_to_unanchored_when_needed(self):
        """Alternation pattern still matches as substring in the fallback pass."""
        fab = make_fab([("SOT-23-3|SOT-23-5", 20, (0.0, 0.0))])
        rotation, _ = fab._find_correction("Package_TO_SOT_SMD:SOT-23-3")
        assert rotation == 20


# ---------------------------------------------------------------------------
# Patterns that already carry anchors
# ---------------------------------------------------------------------------


class TestFindCorrectionExistingAnchors:
    """Patterns that already end with $ are wrapped as (?:pattern)$ harmlessly."""

    def test_pre_anchored_pattern_matches_correctly(self):
        """A pattern ending in $ still matches correctly when wrapped."""
        fab = make_fab([("SOT-23$", 10, (0.0, 0.0))])
        rotation, _ = fab._find_correction("SOT-23")
        assert rotation == 10

    def test_pre_anchored_pattern_does_not_match_longer_value(self):
        """A pre-anchored SOT-23$ pattern does not match SOT-23-3."""
        fab = make_fab([("SOT-23$", 10, (0.0, 0.0))])
        assert fab._find_correction("SOT-23-3") is None

    def test_pre_anchored_and_unanchored_coexist(self):
        """Pre-anchored SOT-23$ and unanchored SOT-23-3 resolve correctly."""
        fab = make_fab([
            ("SOT-23$", 10, (0.0, 0.0)),
            ("SOT-23-3", 20, (0.0, 0.0)),
        ])
        assert fab._find_correction("SOT-23")[0] == 10
        assert fab._find_correction("SOT-23-3")[0] == 20


# ---------------------------------------------------------------------------
# Offset (position correction) passthrough
# ---------------------------------------------------------------------------


class TestFindCorrectionOffset:
    """_find_correction returns the offset tuple as well as rotation."""

    def test_offset_returned_with_rotation(self):
        """Both rotation and offset are returned in the result tuple."""
        fab = make_fab([("SOT-23-3", 45, (1.5, -0.5))])
        rotation, offset = fab._find_correction("SOT-23-3")
        assert rotation == 45
        assert offset == (1.5, -0.5)
