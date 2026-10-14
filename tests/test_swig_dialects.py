"""The SWIG backend speaks one dialect per KiCad major, chosen by the major.

The native test is the dialects' oracle: it finds out what the running
KiCad's footprints hold by probing a fresh one itself, and compares that
with the facts of the dialect chosen for it.
"""

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from .native_kicad_support import add_native_field, native_bindings
from .wx_harness import load_siblings

__all__ = ["native_bindings"]


@pytest.fixture
def swig() -> Iterator[SimpleNamespace]:
    """Load the SWIG backend and its dialects under a scoped name."""
    with load_siblings(
        "_swig_dialects_tests",
        ("kicad_backend", "kicad_backend.swig", "kicad_backend.swig.dialects"),
        {},
    ) as modules:
        yield SimpleNamespace(
            api=modules["kicad_backend"],
            backend=modules["kicad_backend.swig"].SwigBackend,
            dialects=modules["kicad_backend.swig.dialects"],
        )


def _kicad(version: str) -> SimpleNamespace:
    """Return a pcbnew module reporting this major.minor version."""
    return SimpleNamespace(GetMajorMinorVersion=lambda: version, GetBoard=lambda: None)


@pytest.mark.parametrize(
    "version, major",
    [("7.0", 7), ("8.0", 8), ("9.0", 9), ("10.0", 10), ("10.99", 10)],
)
def test_the_dialect_is_chosen_by_the_major(
    swig: SimpleNamespace, version: str, major: int
) -> None:
    """Each supported major gets its own; a nightly gets the major it follows."""
    assert swig.dialects.select(_kicad(version)).major == major


@pytest.mark.parametrize("version", ["6.0", "11.0", "", "nightly"])
def test_a_major_with_no_dialect_is_unsupported(
    swig: SimpleNamespace, version: str
) -> None:
    """The backend refuses it when it is made, naming the supported range."""
    with pytest.raises(swig.api.Unsupported, match="KiCad 7 to 10"):
        swig.dialects.select(_kicad(version))
    with pytest.raises(swig.api.Unsupported, match="KiCad 7 to 10"):
        swig.backend(_kicad(version))


def _probe(pcbnew: Any) -> dict[str, Any]:
    """Find out what this KiCad's footprints hold, from fresh ones."""
    footprint = pcbnew.FOOTPRINT(pcbnew.BOARD())
    field_objects = hasattr(footprint, "GetFields")
    if field_objects:
        builtin = tuple(str(field.GetName()) for field in footprint.GetFields())
    else:
        # Reference and Value are items of their own, beside an empty map.
        assert callable(footprint.Reference) and callable(footprint.Value)
        assert dict(footprint.GetProperties()) == {}
        builtin = ("Reference", "Value")

    holds_dnp = callable(getattr(footprint, "SetDNP", None))
    if holds_dnp:
        footprint.SetDNP(True)
        holds_dnp = bool(footprint.IsDNP())

    twice = pcbnew.FOOTPRINT(pcbnew.BOARD())
    for text in ("first", "second"):
        if field_objects:
            add_native_field(pcbnew, twice, "Twice", text)
        else:
            twice.SetProperty("Twice", text)
    if field_objects:
        held = [str(f.GetText()) for f in twice.GetFields() if f.GetName() == "Twice"]
    else:
        held = [str(twice.GetProperties()["Twice"])]

    return {
        "major": int(str(pcbnew.Version()).split(".", 1)[0]),
        "builtin_fields": builtin,
        "holds_dnp": holds_dnp,
        "keeps_repeated_fields": held == ["first", "second"],
    }


@pytest.mark.native_kicad
def test_the_chosen_dialect_matches_the_running_kicad(
    native_bindings: SimpleNamespace, swig: SimpleNamespace
) -> None:
    """Every fact the dialect states holds for a fresh footprint of this KiCad."""
    found = _probe(native_bindings.pcbnew)
    dialect = swig.dialects.select(native_bindings.pcbnew)

    assert {name: getattr(dialect, name) for name in found} == found
