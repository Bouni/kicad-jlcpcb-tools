"""Separate saving board intent from permission to export an impedance report."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from impedance.database import ConfigConflictError, ImpedanceDatabase
from impedance.matching import analyze, validate_review
from impedance.model import (
    Analysis,
    BoardSnapshot,
    Config,
    LayerSettings,
    Specification,
    Trace,
    ValidationError,
)
from impedance.repository import ImpedanceRepository
from impedance.review_tracking import ImageView, LayerApproval, ReviewTracking
from impedance.service import prepare
from impedance.stackup_model import Stackup


def _stackup() -> Stackup:
    """Use a valid immutable selection without depending on the live catalog."""
    return Stackup("draft-stackup", "Selected stackup", 2, "1.6", "1", "")


def _snapshot() -> BoardSnapshot:
    """Expose one complete single-ended class route on a two-layer board."""
    return BoardSnapshot(
        layers=("F.Cu", "B.Cu"),
        traces=(Trace("rf-track", "F.Cu", "RF1", 180_000, ((0, 0), (100_000_000, 0))),),
        context_digest="board-visual-state",
        net_classes=("RF",),
        net_class_memberships=(("RF1", ("RF",)),),
        net_class_context_digest="rf-class-settings",
    )


def _specification() -> Specification:
    """Represent a completed layer editor without authorizing report export."""
    return Specification(
        spec_id="rf",
        label="RF 50 ohm",
        target_ohms="50",
        kind="single_ended",
        net_class="RF",
        layer_settings=(LayerSettings("F.Cu", ("B.Cu",)),),
    )


def _repository(tmp_path: Path) -> tuple[ImpedanceDatabase, ImpedanceRepository]:
    """Use actual SQLite persistence and an actual saved board identity."""
    board_path = tmp_path / "board.kicad_pcb"
    board_path.write_text("(kicad_pcb)\n", encoding="utf-8")
    database = ImpedanceDatabase(tmp_path / "jlcpcb" / "project.db")
    board_id = database.resolve_board(board_path)
    return database, ImpedanceRepository(database, board_id)


def _history() -> ReviewTracking:
    """Retain explicit historical events that cannot authorize changed intent."""
    timestamp = "2026-09-07T12:34:56.000000Z"
    return ReviewTracking(
        images=(ImageView("rf", "F.Cu", "old-section", "a" * 64, timestamp),),
        layers=(
            LayerApproval(
                "rf", "F.Cu", "b" * 64, timestamp, (("old-section", "a" * 64),)
            ),
        ),
    )


@pytest.mark.parametrize("with_stackup", [False, True])
def test_enabled_empty_draft_roundtrips_real_database_without_approval(
    tmp_path: Path, with_stackup: bool
) -> None:
    """Save and reopen an enabled draft without inventing specs or disabling it."""
    database, repository = _repository(tmp_path)
    draft = Config(enabled=True, stackup=_stackup() if with_stackup else None)

    assert repository.save(draft, 0) == 1

    reopened = ImpedanceDatabase(database.path)
    restored, revision = ImpedanceRepository(reopened, repository.board_id).load()
    assert (restored, revision) == (draft, 1)
    assert restored.enabled is True
    assert restored.reviewed_digest == ""
    assert restored.included_section_ids == ()
    assert restored.review_tracking == ReviewTracking()
    record = reopened.load_config(repository.board_id)
    assert record is not None
    assert record["version"] == 1
    assert record["payload"]["schema_version"] == 5
    assert record["enabled"] is True
    with pytest.raises(ValidationError, match="requires at least one specification"):
        prepare(restored, _snapshot())


@pytest.mark.parametrize("with_stackup", [False, True])
def test_no_specification_cannot_authorize_an_enabled_report(
    with_stackup: bool,
) -> None:
    """An old or fabricated review marker cannot make an empty report valid."""
    config = Config(
        enabled=True,
        stackup=_stackup() if with_stackup else None,
        reviewed_digest="matching-old-marker",
        included_section_ids=("old-section",),
    )
    analysis = Analysis((), "matching-old-marker")

    with pytest.raises(ValidationError, match="requires at least one specification"):
        validate_review(config, analysis)


def test_completed_unreviewed_draft_preserves_history_but_blocks_export(
    tmp_path: Path,
) -> None:
    """Saving completed specifications must not turn old timestamps into approval."""
    database, repository = _repository(tmp_path)
    draft = Config(
        enabled=True,
        specifications=(_specification(),),
        stackup=_stackup(),
        review_tracking=_history(),
    )
    assert repository.save(draft, 0) == 1

    restored, _ = ImpedanceRepository(
        ImpedanceDatabase(database.path), repository.board_id
    ).load()
    assert restored == draft
    assert restored.review_tracking == _history()
    assert restored.reviewed_digest == ""
    assert restored.included_section_ids == ()
    with pytest.raises(ValidationError, match="scan and review"):
        prepare(restored, _snapshot())


def test_unchanged_approved_record_retains_existing_export_authorization(
    tmp_path: Path,
) -> None:
    """Persisting unchanged settings neither invalidates nor replaces prior approval."""
    database, repository = _repository(tmp_path)
    config = Config(
        enabled=True,
        specifications=(_specification(),),
        stackup=_stackup(),
        review_tracking=_history(),
    )
    snapshot = _snapshot()
    analysis = analyze(config, snapshot)
    approved = replace(
        config,
        reviewed_digest=analysis.digest,
        included_section_ids=tuple(section.section_id for section in analysis.sections),
    )
    assert repository.save(approved, 0) == 1
    before = database.load_config(repository.board_id)

    reopened = ImpedanceRepository(
        ImpedanceDatabase(database.path), repository.board_id
    )
    restored, revision = reopened.load()
    assert (restored, revision) == (approved, 1)
    assert database.load_config(repository.board_id) == before
    validate_review(restored, analyze(restored, snapshot))
    assert reopened.save(restored, revision) == 2
    assert reopened.load() == (approved, 2)


@pytest.mark.parametrize("schema_version", [1, 2, 3, 4])
def test_old_disabled_empty_records_never_rewrite_without_explicit_action(
    tmp_path: Path, schema_version: int
) -> None:
    """V4 drafts upgrade only in memory; retired schemas need an explicit reset."""
    database, repository = _repository(tmp_path)
    payload: dict[str, Any] = {
        "schema_version": schema_version,
        "enabled": False,
        "specifications": [],
        "reviewed_digest": "",
        "included_section_ids": [],
        "section_groups": [],
    }
    if schema_version >= 3:
        payload["review_tracking"] = {"images": [], "layers": []}
    if schema_version == 4:
        payload.update(stackup=None, width_results=[], legacy_specifications=[])
    database.save_config(repository.board_id, payload, False, 0)
    before = database.load_config(repository.board_id)

    if schema_version < 4:
        with pytest.raises(ValidationError, match="Reset settings"):
            repository.load()
    else:
        assert repository.load() == (Config(), 1)
        assert prepare(repository.load()[0], _snapshot()) is None
    assert database.load_config(repository.board_id) == before


def test_draft_save_conflict_preserves_newer_intent_and_other_board(
    tmp_path: Path,
) -> None:
    """Permitting drafts must not bypass revision checks or board isolation."""
    database, repository = _repository(tmp_path)
    other_path = tmp_path / "other.kicad_pcb"
    other_path.write_text("(kicad_pcb)\n", encoding="utf-8")
    other = ImpedanceRepository(database, database.resolve_board(other_path))
    assert other.save(Config(), 0) == 1
    other_before = database.load_config(other.board_id)
    draft = Config(enabled=True, stackup=_stackup())
    assert repository.save(draft, 0) == 1
    stale, stale_revision = repository.load()
    newer = replace(draft, specifications=(_specification(),))
    assert repository.save(newer, stale_revision) == 2

    with pytest.raises(ConfigConflictError):
        repository.save(stale, stale_revision)

    assert repository.load() == (newer, 2)
    assert database.load_config(other.board_id) == other_before
