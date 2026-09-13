"""Production snapshot builders shared by pure matrix and native-grid tests."""

from dataclasses import replace
import importlib
from typing import Any, Optional

import pytest

from .variant_native_support import Snapshot, State, assignment, native

__all__ = ["Snapshot", "State", "assignment"]

matrix = importlib.import_module("_variant_test.variant.matrix_model")


def changed(
    source: native.BoardVariantSnapshot,
    *,
    component: Optional[str] = None,
    variants: Optional[tuple[str, ...]] = None,
    **values: Any,
) -> native.BoardVariantSnapshot:
    """Change explicit fixture identities, retaining the real snapshot index."""
    return replace(
        source,
        components=tuple(
            replace(part, **values)
            if (component is None or part.component_id == component)
            and (variants is None or part.variant_name in variants)
            else part
            for part in source.components
        ),
    )


def catalog(
    source: native.BoardVariantSnapshot,
    values: dict[str, dict[str, Any]],
    *,
    component: Optional[str] = None,
) -> dict[tuple[str, str], matrix.CatalogMetadata]:
    """Bind catalog inputs to real assignments unless a case overrides that tag."""
    return {
        (part.component_id, part.variant_name): matrix.CatalogMetadata(
            **{"lcsc": part.lcsc, "status": "complete", **values[part.variant_name]}
        )
        for part in source.components
        if part.variant_name in values
        and (component is None or part.component_id == component)
    }


def coordinates(
    model: matrix.MatrixModel,
    variant: Optional[str],
    field: str,
    component: str = "id1",
) -> tuple[int, int]:
    """Locate a displayed test cell by identity, never by assumed row order."""
    row = model.row_for_component(component)
    assert row is not None
    return row, model.column_for(variant, field)


@pytest.fixture
def snapshot() -> native.BoardVariantSnapshot:
    """Cover equal, A=B≠Default, and same-part/different-population components."""
    states = []
    for component_id, reference in (("id10", "R10"), ("id2", "R2"), ("id1", "R1")):
        for variant in ("", "A", "B"):
            state = State(component_id, reference, variant)
            if reference == "R1" and variant:
                state = replace(
                    state,
                    value="4.7k",
                    lcsc="C456",
                )
            if reference == "R2" and variant == "B":
                state = replace(state, pop=False)
            if reference == "R10" and variant == "A":
                state = replace(state, assignment=assignment(inherited=True))
            states.append(state)
    return Snapshot(tuple(states))


@pytest.fixture
def matching_snapshot() -> native.BoardVariantSnapshot:
    """Three equal variants on each of three naturally ordered references."""
    return Snapshot(
        tuple(
            State(part, ref, name)
            for part, ref in (("id1", "R1"), ("id2", "R2"), ("id10", "R10"))
            for name in ("", "A", "B")
        )
    )
