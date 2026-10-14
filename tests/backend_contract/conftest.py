"""Give each contract test a board in every backend under test.

The fake backend runs everywhere; the SWIG backend runs in the native KiCad
lanes, on boards built in real KiCad.
"""

from collections.abc import Callable, Iterator, Sequence
from types import SimpleNamespace

import pytest

from ..native_kicad_support import native_bindings
from ..wx_harness import load_siblings
from .support import Board, Part, fake_board
from .swig_board import swig_board

__all__ = ["native_bindings"]

BUILDERS: dict[str, Callable[..., Board]] = {"fake": fake_board, "swig": swig_board}
NATIVE_KICAD = [
    pytest.mark.native_kicad,
    # A string condition is evaluated when the test runs, after a required
    # native lane has opted in; this file can be imported before that.
    pytest.mark.skipif(
        'os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1"',
        reason="Native KiCad tests require an explicit opt-in",
    ),
]


@pytest.fixture(scope="module")
def backend_modules() -> Iterator[SimpleNamespace]:
    """Load the backend package once per module, under a scoped name."""
    with load_siblings(
        "_backend_contract_tests",
        (
            "kicad_backend",
            "kicad_backend.fake",
            "kicad_backend.swig",
            "kicad_backend.swig.dialects",
        ),
        {},
    ) as modules:
        yield SimpleNamespace(
            api=modules["kicad_backend"],
            fake=modules["kicad_backend.fake"],
            swig=modules["kicad_backend.swig"],
            dialects=modules["kicad_backend.swig.dialects"],
        )


@pytest.fixture(params=["fake", pytest.param("swig", marks=NATIVE_KICAD)])
def board(
    request: pytest.FixtureRequest, backend_modules: SimpleNamespace
) -> Callable[[Sequence[Part]], Board]:
    """Return a factory: parts in, a board in the backend under test out."""
    builder = BUILDERS[request.param]
    return lambda parts: builder(backend_modules, parts, request)
