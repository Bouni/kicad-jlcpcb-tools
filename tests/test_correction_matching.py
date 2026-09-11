"""Keep display and fabrication selection on the established CPL policy."""

from dataclasses import FrozenInstanceError
import importlib.util
from pathlib import Path
import re
import sys
from types import ModuleType
from typing import Optional

import pytest

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def data(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the pure production matcher without GUI or database imports."""
    name = "correction_data_matching_tests"
    spec = importlib.util.spec_from_file_location(name, _ROOT / "correction_data.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("patterns", "value", "expected"),
    [
        (("SOT-23", "SOT-23-3"), "SOT-23-3", 1),
        (("SOT-23-3", "SOT-23"), "SOT-23-3", 0),
        (("SOT-23",), "Package:SOT-23", 0),
        (("SOT-23",), "Package:SOT-23-extra", 0),
        (("23", "SOT-23"), "SOT-23", 1),
        (("SOT-23", "23"), "SOT-23", 0),
        ((".+", "SOT-23"), "SOT-23", 0),
        (("SOT", "SOT-23"), "SOT-23-extra", 1),
        (("SOT-23", "SOT"), "SOT-23-extra", 0),
        (("SOT|QFN", "SOT-23"), "SOT-23", 1),
        (("SOT|QFN",), "Package:QFN", 0),
        (("(?i:sot-23)",), "Package:SOT-23", 0),
        (("^SOT-23$",), "Package:SOT-23", None),
        (("SOT-23",), "sot-23", None),
        (("SOT-23",), "QFN-32", None),
        (("SOT-23", "23\\n"), "SOT-23\n", 0),
        (("^SOP-(?!18_)", "^SOP-4_"), "SOP-4_3.8x4.1mm_P2.54mm", 1),
        (("^SOP-(?!18_)", "^SOP-4_"), "SOP-18_7.5x11.6mm_P1.27mm", None),
        ((), "QFN-32", None),
    ],
)
def test_find_correction_ranks_by_how_much_of_the_value_is_consumed(
    data: ModuleType,
    patterns: tuple[str, ...],
    value: str,
    expected: Optional[int],  # noqa: UP045
) -> None:
    """More specific wins: the longest match, then input order for ties."""
    corrections = tuple(
        data.Correction(pattern, index * 90, (index, -index))
        for index, pattern in enumerate(patterns)
    )
    selected = data.find_correction(corrections, value)
    assert selected is (None if expected is None else corrections[expected])


@pytest.mark.parametrize("container", [tuple, list])
@pytest.mark.parametrize(
    ("patterns", "reference", "value", "footprint", "expected", "source"),
    [
        (("SOT-23-3", "VALUE", "R"), "R1", "VALUE", "SOT-23-3", 2, "ref"),
        (("SOT-23-3", "VAL"), "R1", "VALUE", "SOT-23-3", 1, "val"),
        (("SOT-23", "SOT-23-3"), "R1", "VALUE", "SOT-23-3", 1, "fpt"),
        (("R", "R12", "VALUE"), "R12", "VALUE", "SOT-23-3", 1, "ref"),
        (("VAL", "VALUE"), "R12", "VALUE", "SOT-23-3", 1, "val"),
        (("1", "R1", "VALUE"), "R1", "VALUE", "SOT-23-3", 1, "ref"),
        (("R1", "VALUE", "SOT-23"), "R1", "VALUE", "SOT-23-3", 0, "ref"),
        (("SOT-23",), "R1", "VALUE", "Package:SOT-23-extra", 0, "fpt"),
    ],
)
def test_match_correction_resolves_one_winner_and_source(
    data: ModuleType,
    container: type,
    patterns: tuple[str, ...],
    reference: str,
    value: str,
    footprint: str,
    expected: int,
    source: str,
) -> None:
    """Complete each target's passes before trying the next footprint field."""
    corrections = container(
        data.Correction(pattern, index * 90, (index, -index))
        for index, pattern in enumerate(patterns)
    )
    match = data.match_correction(corrections, reference, value, footprint)
    assert match.correction is corrections[expected]
    assert match.source == source


def test_zero_valued_match_still_suppresses_lower_priority_corrections(
    data: ModuleType,
) -> None:
    """Zero is an explicit correction and not the absence of a matching record."""
    zero = data.Correction("R", 0, (0, 0))
    nonzero = data.Correction("VALUE", 90, (1, 2))
    match = data.match_correction((nonzero, zero), "R1", "VALUE", "SOT-23")
    assert match.correction is zero
    assert match.source == "ref"


@pytest.mark.parametrize("patterns", [(), ("QFN", "U1")])
def test_no_match_has_no_correction_or_source(
    data: ModuleType, patterns: tuple[str, ...]
) -> None:
    """Missing corrections remain distinct from an explicit zero correction."""
    corrections = tuple(data.Correction(pattern, 90, (1, 2)) for pattern in patterns)
    assert data.match_correction(corrections, "R1", "VALUE", "SOT-23") is None


def test_match_result_cannot_change_the_selected_value_or_source(
    data: ModuleType,
) -> None:
    """Consumers share an immutable description of the selected correction."""
    correction = data.Correction("R1", 90, (1, 2))
    match = data.match_correction((correction,), "R1", "VALUE", "SOT-23")
    with pytest.raises(FrozenInstanceError):
        match.correction = data.Correction("VALUE", 180, (3, 4))
    with pytest.raises(FrozenInstanceError):
        match.source = "val"


@pytest.mark.parametrize(
    ("reference", "value", "footprint", "expected", "source"),
    [
        ("R1", "value", "Package:REVIEW_0599", 599, "fpt"),
        ("R1", "beforeREVIEW_0599after", "package", 599, "val"),
        ("R1", "value", "package", None, None),
    ],
)
def test_large_correction_set_never_recompiles_during_matching(
    data: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    reference: str,
    value: str,
    footprint: str,
    expected: Optional[int],  # noqa: UP045
    source: Optional[str],  # noqa: UP045
) -> None:
    """Retained expressions survive cache eviction for footprint, value and missed matches."""
    corrections = tuple(
        data.Correction(f"REVIEW_{index:04d}", index, (0, 0)) for index in range(600)
    )
    # CPython renamed the compiler module in 3.11. Count actual compilation,
    # including the path used by re.search(), rather than only re.compile().
    compiler = re._compiler if hasattr(re, "_compiler") else re.sre_compile
    original_compile = compiler.compile
    compile_requests: list[object] = []

    def record_compile(pattern: object, flags: int = 0) -> re.Pattern[str]:
        compile_requests.append(pattern)
        return original_compile(pattern, flags)

    re.purge()
    with monkeypatch.context() as scoped:
        scoped.setattr(compiler, "compile", record_compile)
        for _ in range(2):
            match = data.match_correction(corrections, reference, value, footprint)
            if expected is None:
                assert match is None
            else:
                assert match.correction is corrections[expected]
                assert match.source == source
    assert len(compile_requests) == 0
