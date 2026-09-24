"""Publication rechecks actual output geometry and the pixels' appearance source."""

from dataclasses import replace
import sqlite3
from types import SimpleNamespace

import pytest

from .test_impedance_controls import _controls, harness

__all__ = ["harness"]


def test_identity_check_has_no_storage_ui_side_effect(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Optional cache contention cannot disable otherwise known saved intent."""
    controls = _controls(harness, monkeypatch)
    before = controls.status.label, controls.checkbox.enabled, controls.config

    def locked(*_args: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(harness.database, "ensure_current_board", locked)
    with pytest.raises(harness.integration.ImpedanceStorageError):
        controls.check_board()
    assert (controls.status.label, controls.checkbox.enabled, controls.config) == before
    assert controls._storage_error is None
    with pytest.raises(harness.integration.ImpedanceStorageError):
        controls.reload()
    assert not controls.checkbox.enabled
    assert controls._storage_error is not None


@pytest.mark.parametrize("changed", ["unchanged", "appearance", "output", "unreadable"])
def test_completed_reports_recheck_actual_output_and_appearance(
    harness: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    """Source approval alone cannot authorize a changed report board or theme."""
    controls = _controls(harness, monkeypatch)
    snapshot = harness.current_snapshot
    output = (
        replace(snapshot, context_digest="changed output")
        if changed == "output"
        else snapshot
    )
    plan = SimpleNamespace(snapshot=snapshot)
    artifacts = SimpleNamespace(appearance="approved theme")
    board = object()
    monkeypatch.setattr(
        controls,
        "snapshot_output_board",
        lambda actual: output if actual is board else None,
    )

    def appearance(_module: object) -> str:
        if changed == "unreadable":
            raise ValueError("Cannot read current appearance")
        return "changed theme" if changed == "appearance" else "approved theme"

    monkeypatch.setattr(harness.integration, "read_theme_context", appearance)
    if changed == "unchanged":
        controls.verify_report_captures(plan, artifacts, board)
    else:
        with pytest.raises(ValueError, match="appearance|output|captures"):
            controls.verify_report_captures(plan, artifacts, board)
