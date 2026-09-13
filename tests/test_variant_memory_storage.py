"""Real session workflows keep KiCad state out of persistent plugin storage."""
# ruff: noqa: D103

from pathlib import Path

import pytest

from .variant_data_support import _native_session
from .variant_native_support import KICAD_FP_DNP, native


@pytest.mark.parametrize("field", ["bom", "pos", "pop", "lcsc"])
@pytest.mark.parametrize("operation", ["edit", "generation"])
def test_external_edits_reject_captured_operations_and_refresh_native_rows(
    tmp_path: Path, field: str, operation: str
) -> None:
    """The public edit/publication boundaries reject stale captured native state."""
    module, session, store, adapter, board = _native_session(tmp_path)
    captured = store.assembly_rows(session.snapshot, "A")[0]
    target = session.snapshot.target("component-1", "A")
    if operation == "generation":
        session.begin_generation()
    variant = board.parts[0].GetVariant("A")
    setters = {
        "bom": lambda: variant.SetExcludedFromBOM(True),
        "pos": lambda: variant.SetExcludedFromPosFiles(True),
        "pop": lambda: variant.SetDNP(True),
        "lcsc": lambda: variant.SetFieldValue("LCSC", "C900"),
    }
    setters[field]()
    external = adapter.snapshot()
    if operation == "edit":
        with pytest.raises(native.StaleVariantTarget, match="source changed"):
            session.apply((native.VariantEdit(target, (("lcsc", "C777"),)),))
    else:
        with pytest.raises(module.VariantSessionError, match="changed during"):
            session.validate_generation()
        session.end_generation()
    assert adapter.snapshot().components == external.components
    session.refresh()
    row = store.assembly_rows(session.snapshot, "A")[0]
    expected = dict(captured)
    row_key = {
        "bom": "exclude_from_bom",
        "pos": "exclude_from_pos",
        "pop": "is_dnp",
        "lcsc": "lcsc",
    }[field]
    expected[row_key] = "C900" if field == "lcsc" else True
    assert row == expected
    assert captured["lcsc"] == "C200" and not captured["is_dnp"]


def test_reopening_reads_unsaved_native_blank_flags_and_assignments(
    tmp_path: Path,
) -> None:
    _, first, store, _, board = _native_session(tmp_path)
    first.set_output_variant("B")
    original = first.snapshot
    on_disk = board.filename.read_bytes()
    board.parts[0].SetField("LCSC", "")
    board.parts[0].SetAttributes(KICAD_FP_DNP | 8)
    variant = board.parts[0].GetVariant("A")
    variant.SetFieldValue("LCSC", "C900")
    variant.SetExcludedFromBOM(True)
    variant.SetDNP(True)
    _, reopened, _, _, same_board = _native_session(tmp_path, board=board)
    assert same_board is board and board.filename.read_bytes() == on_disk
    assert first.snapshot is original
    assert reopened.output_variant == "B"
    for name, lcsc in (("", ""), ("A", "C900")):
        part = reopened.snapshot.get("component-1", name)
        assert part.lcsc == lcsc and not part.bom and not part.pop
    assert not Path(store.dbfile).exists()
