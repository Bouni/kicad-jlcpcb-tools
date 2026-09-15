"""Resolve one physical correction per component without variant field writes."""

from types import SimpleNamespace
from typing import Any

import pytest

import correction_data as data
from tests.variant_native_support import Snapshot, State


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
    result = data.resolve_shared_corrections(snapshot, rules)
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


@pytest.mark.parametrize("bad_inventory", ["missing_default", "duplicate_component"])
def test_shared_resolution_rejects_ambiguous_base_inventory(bad_inventory: str) -> None:
    """No arbitrary Default or duplicate UUID may become the shared authority."""
    part = State("uuid-1", "U1", "")
    snapshot = SimpleNamespace(
        variants=[
            SimpleNamespace(name="" if bad_inventory != "missing_default" else "A")
        ],
        for_variant=lambda _name: (part,)
        if bad_inventory == "missing_default"
        else (part, part),
    )
    with pytest.raises(
        ValueError,
        match="Default" if bad_inventory == "missing_default" else "Duplicate",
    ):
        data.resolve_shared_corrections(snapshot, ())
