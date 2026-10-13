"""Give each contract test a board in every backend under test.

The fake backend runs everywhere.
"""

from collections.abc import Callable, Iterator, Sequence
from types import SimpleNamespace

import pytest

from ..wx_harness import load_siblings
from .support import BUILDERS, Board, Part


@pytest.fixture(scope="module")
def backend_modules() -> Iterator[SimpleNamespace]:
    """Load the backend package once per module, under a scoped name."""
    with load_siblings(
        "_backend_contract_tests",
        ("kicad_backend", "kicad_backend.fake"),
        {},
    ) as modules:
        yield SimpleNamespace(
            api=modules["kicad_backend"], fake=modules["kicad_backend.fake"]
        )


@pytest.fixture(params=["fake"])
def board(
    request: pytest.FixtureRequest, backend_modules: SimpleNamespace
) -> Callable[[Sequence[Part]], Board]:
    """Return a factory: parts in, a board in the backend under test out."""
    builder = BUILDERS[request.param]
    return lambda parts: builder(backend_modules, parts, request)
