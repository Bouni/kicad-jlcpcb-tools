"""Captured operations use native identity and stay independent of edit focus."""
# ruff: noqa: D103

from pathlib import Path
from typing import Any

import pytest

from .variant_data_support import _native_session, session_module
from .variant_native_support import KICAD_FP_DNP, KICAD_FP_JUST_ADDED, Footprint, native


def test_assignment_requires_explicit_nonempty_single_variant_batch(
    tmp_path: Path,
) -> None:
    _, session, _, _, _ = _native_session(tmp_path)
    for targets in (
        (),
        tuple(session.snapshot.target("component-1", name) for name in ("A", "B")),
    ):
        with pytest.raises(session_module.VariantSessionError, match="one variant"):
            session.begin_assignment(targets)


@pytest.mark.parametrize("existing_record", [False, True])
def test_partial_native_write_failure_recovers_original_state_and_allows_retry(
    tmp_path: Path,
    existing_record: bool,
) -> None:
    _, session, _, _, board = _native_session(tmp_path)
    if not existing_record:
        board.parts[0].DeleteVariant("A")
        session.refresh()
    original_flags = KICAD_FP_DNP | KICAD_FP_JUST_ADDED | 128
    board.parts[0].SetAttributes(original_flags)
    second = Footprint(board, "component-2", "R2")
    board.parts.append(second)
    captured = session.refresh()
    second.fail_field = "Value"
    edits = (
        native.VariantEdit(captured.target("component-1", ""), (("pop", True),)),
        native.VariantEdit(captured.target("component-1", "A"), (("lcsc", "C999"),)),
        native.VariantEdit(captured.target("component-2", ""), (("value", "22k"),)),
    )
    with pytest.raises(native.NativeVariantError, match="restored"):
        session.apply(edits)
    assert session.reliable
    assert session.snapshot.components == captured.components
    assert second.fields["Value"] == "10k"
    assert board.parts[0].GetAttributes() == original_flags
    assert bool(board.parts[0].GetVariant("A")) is existing_record
    session.apply(edits)
    assert session.snapshot.get("component-1", "A").lcsc == "C999"


def test_failed_rollback_blocks_editing_generation_and_refresh(
    tmp_path: Path,
) -> None:
    _, session, _, adapter, board = _native_session(tmp_path)

    def fail(_variant: Any) -> None:
        raise RuntimeError("setter failed")

    board.parts[0].SetVariant = fail
    with pytest.raises(native.NativeVariantError, match="recovery is incomplete"):
        session.apply(
            (
                native.VariantEdit(
                    session.snapshot.target("component-1", "A"), (("lcsc", "C999"),)
                ),
            )
        )
    assert adapter.unreliable and not session.reliable
    for action in (
        session.begin_generation,
        lambda: session.apply(()),
        session.refresh,
    ):
        with pytest.raises(session_module.VariantSessionError):
            action()


def test_generation_blocks_mutations_and_detects_assembly_changes_not_timestamps(
    tmp_path: Path,
) -> None:
    _, session, _, _, board = _native_session(tmp_path)
    source, name = session.begin_generation()
    assert source is session.snapshot and name == "A"
    for action in (lambda: session.set_output_variant("B"), lambda: session.apply(())):
        with pytest.raises(session_module.VariantSessionError, match="generation"):
            action()
    board.current = "B"
    board.GetTimeStamp = lambda: 12345
    session.validate_generation()
    board.parts[0].GetVariant("B").SetFieldValue("LCSC", "C777")
    with pytest.raises(session_module.VariantSessionError, match="changed"):
        session.validate_generation()
    session.end_generation()
    session.refresh()
    session.set_output_variant("B")
    assert session.begin_generation()[1] == "B"


def test_save_as_invalidates_captured_assignment(
    tmp_path: Path,
) -> None:
    _, session, _, _, board = _native_session(tmp_path)
    context = session.begin_assignment((session.snapshot.target("component-1", "A"),))
    board.filename = tmp_path / "renamed.kicad_pcb"
    with pytest.raises(session_module.VariantSessionError, match="replaced or saved"):
        session.accept_assignment(context)
    assert not session.reliable
    assert session._assignment is None
