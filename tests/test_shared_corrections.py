"""Resolve output-part corrections without changing shared rules or native fields."""

from collections.abc import Iterator
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from tests.variant_native_support import Snapshot, State
from tests.wx_harness import load_siblings


@pytest.fixture
def data() -> Iterator[ModuleType]:
    """Import the production matcher with its relative LCSC helper isolated."""
    with load_siblings(
        "shared_correction_tests", ("correction_data",), {}
    ) as loaded:
        yield loaded["correction_data"]


@pytest.mark.parametrize("variant_name", ["", "A"])
@pytest.mark.parametrize(
    "patterns,expected,source",
    [
        (("^SOT-23$", "^Base device$", "^U1$"), 2, "ref"),
        (("^SOT-23$", "^Base device$"), 1, "val"),
        (("^Variant device$", "^Base device$"), 1, "val"),
        (("^SOT-23$",), 0, "fpt"),
        (("^Variant device$",), None, None),
        ((), None, None),
    ],
)
def test_shared_resolution_preserves_physical_precedence(
    data: ModuleType,
    variant_name: str,
    patterns: tuple[str, ...],
    expected: Any,
    source: Any,
) -> None:
    """Default Value retains compatibility; overridden variant Values are ignored."""
    rules = tuple(
        data.Correction(
            pattern,
            0 if pattern == "^U1$" else 90,
            (0, 0) if pattern == "^U1$" else (1, 2),
        )
        for pattern in patterns
    )
    before = tuple(rule.db_row() for rule in rules)
    snapshot = Snapshot(
        tuple(
            State("uuid-1", "U1", name, value=value, footprint="Package:SOT-23")
            for name, value in (("", "Base device"), ("A", "Variant device"))
        )
    )
    result = data.resolve_shared_corrections(snapshot, rules, variant_name)
    assert tuple(rule.db_row() for rule in rules) == before
    assert set(result) == {"uuid-1"}
    match = result["uuid-1"]
    if expected is None:
        assert match is None
    else:
        assert match.correction is rules[expected]
        assert match.source == source
        if source == "ref":
            assert match.correction.rotation == 0


@pytest.mark.parametrize(
    "variant_name,variant_lcsc,expected,source",
    [
        ("", "C222", 0, "lcsc"),
        ("A", "C222", 1, "lcsc"),
        ("A", "C333", 2, "val"),
        ("A", "", 2, "val"),
    ],
)
def test_output_assignment_selects_exact_part_before_default_patterns(
    data: ModuleType,
    variant_name: str,
    variant_lcsc: str,
    expected: int,
    source: str,
) -> None:
    """An absent part rule never reuses Default's exact part or named Value."""
    rules = (
        data.LcscCorrection("C111", 90, (1, 2)),
        data.LcscCorrection("C222", 0, (0, 0)),
        data.Correction("^Base device$", 45, (-2, -3)),
        data.Correction("^Variant device$", 180, (3, 4)),
        data.Correction("^SOT-23$", 270, (5, 6)),
    )
    snapshot = Snapshot(
        (
            State(
                "uuid-1",
                "U1",
                "",
                value="Base device",
                lcsc="C111",
                footprint="Package:SOT-23",
            ),
            State(
                "uuid-1",
                "U1",
                "A",
                value="Variant device",
                lcsc=variant_lcsc,
                footprint="Package:SOT-23",
            ),
        )
    )
    before_components = snapshot.components
    before_rules = tuple(rule.db_row() for rule in rules)
    match = data.resolve_shared_corrections(snapshot, rules, variant_name)["uuid-1"]
    assert match.correction is rules[expected]
    assert match.source == source
    assert snapshot.components == before_components
    assert tuple(rule.db_row() for rule in rules) == before_rules


def test_shared_resolution_rejects_unavailable_output_variant(data: ModuleType) -> None:
    """A removed output cannot silently receive Default's exact-part correction."""
    snapshot = Snapshot((State("uuid-1", "U1", "", lcsc="C111"),))
    with pytest.raises(ValueError, match="Output variant is unavailable"):
        data.resolve_shared_corrections(
            snapshot, (data.LcscCorrection("C111", 90, (1, 2)),), "Removed"
        )


@pytest.mark.parametrize("bad_inventory", ["missing_default", "duplicate_component"])
def test_shared_resolution_rejects_ambiguous_base_inventory(
    data: ModuleType, bad_inventory: str
) -> None:
    """No arbitrary Default or duplicate UUID may become the shared authority."""
    part = State("uuid-1", "U1", "")
    output = State("uuid-1", "U1", "A")
    parts = {"": (part, part), "A": (output,)}
    snapshot = SimpleNamespace(
        variants=[
            SimpleNamespace(name=name)
            for name in (("A",) if bad_inventory == "missing_default" else ("", "A"))
        ],
        for_variant=parts.__getitem__,
    )
    with pytest.raises(
        ValueError,
        match="Default" if bad_inventory == "missing_default" else "Duplicate",
    ):
        data.resolve_shared_corrections(snapshot, (), "A")


@pytest.mark.parametrize("bad_inventory", ["missing_component", "duplicate_component"])
def test_shared_resolution_rejects_ambiguous_output_inventory(
    data: ModuleType, bad_inventory: str
) -> None:
    """Every Default identity must have exactly one effective output assignment."""
    base = State("uuid-1", "U1", "", lcsc="C111")
    output = State("uuid-1", "U1", "A", lcsc="C222")
    parts = {
        "": (base,),
        "A": () if bad_inventory == "missing_component" else (output, output),
    }
    snapshot = SimpleNamespace(
        variants=[SimpleNamespace(name=name) for name in ("", "A")],
        for_variant=parts.__getitem__,
    )
    with pytest.raises(
        ValueError,
        match="unavailable" if bad_inventory == "missing_component" else "Duplicate",
    ):
        data.resolve_shared_corrections(snapshot, (), "A")
