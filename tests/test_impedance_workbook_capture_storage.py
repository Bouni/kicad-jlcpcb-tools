"""Persist workbook pixel consent independently of historical image viewing."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from typing import Any

import pytest

from impedance.database import ImpedanceDatabase
from impedance.model import (
    Config,
    LayerSettings,
    Specification,
    ValidationError,
    validate_storage_config,
)
from impedance.repository import ImpedanceRepository
from impedance.review_tracking import (
    ImageView,
    LayerApproval,
    ReviewTracking,
    SpacingImageReview,
)


def _approved_config() -> Config:
    """Keep past display/approval hashes distinct from fresh workbook consent."""
    timestamp = "2026-10-29T12:34:56.000000Z"
    return Config(
        enabled=True,
        specifications=(
            Specification(
                "rf",
                "RF 50 ohm",
                "50",
                "single_ended",
                "RF",
                (LayerSettings("F.Cu", ("B.Cu",)),),
            ),
        ),
        reviewed_digest="a" * 64,
        included_section_ids=("row-a", "row-b"),
        review_tracking=ReviewTracking(
            images=(ImageView("rf", "F.Cu", "row-b", "b" * 64, timestamp),),
            layers=(
                LayerApproval(
                    "rf", "F.Cu", "c" * 64, timestamp, (("row-b", "b" * 64),)
                ),
            ),
        ),
        reviewed_captures=(("row-a", "d" * 64), ("row-b", "e" * 64)),
    )


def _legacy_payload(version: int) -> dict[str, Any]:
    """Describe each supported earlier schema without new workbook evidence."""
    payload = _approved_config().to_dict()
    payload["schema_version"] = version
    payload.pop("reviewed_captures")
    if version < 7:
        payload["review_tracking"].pop("spacing_images")
    if version < 6:
        for spec in payload["specifications"]:
            spec.pop("linked_layer_groups")
            spec.pop("independent_layers")
    if version == 4:
        payload.update(section_groups=[], legacy_specifications=[])
    return payload


def test_workbook_capture_consent_roundtrips_without_restamping_history() -> None:
    """Saved current workbook pixels must not rewrite previous viewing events."""
    config = _approved_config()
    payload = config.to_dict()
    assert payload["schema_version"] == 8
    assert payload["reviewed_captures"] == [
        ["row-a", "d" * 64],
        ["row-b", "e" * 64],
    ]
    assert Config.from_dict(json.loads(json.dumps(payload))) == config
    assert config.review_tracking.images[0].capture_digest == "b" * 64


def test_workbook_capture_consent_survives_actual_database_reopening(
    tmp_path: Path,
) -> None:
    """SQLite storage must keep pixel consent separate from historical dates."""
    board = tmp_path / "board.kicad_pcb"
    board.write_text("(kicad_pcb)\n", encoding="utf-8")
    database = ImpedanceDatabase(tmp_path / "project.db")
    board_id = database.resolve_board(board)
    config = _approved_config()
    assert ImpedanceRepository(database, board_id).save(config, 0) == 1
    reopened = ImpedanceRepository(ImpedanceDatabase(database.path), board_id)
    restored, revision = reopened.load()
    assert (restored, revision) == (config, 1)
    assert restored.review_tracking == config.review_tracking


@pytest.mark.parametrize("version", [4, 5, 6, 7])
def test_old_schemas_do_not_invent_workbook_pixel_evidence(version: int) -> None:
    """Legacy approval and image history cannot imply a new independent baseline."""
    payload = _legacy_payload(version)
    unchanged = deepcopy(payload)
    config = Config.from_dict(payload)
    assert config.reviewed_captures == ()
    assert config.review_tracking == _approved_config().review_tracking
    assert config.reviewed_digest == ("" if version == 4 else "a" * 64)
    assert payload == unchanged
    assert config.to_dict()["schema_version"] == 8


def test_current_schema_requires_explicit_workbook_capture_field() -> None:
    """An omitted current-schema field is malformed rather than legacy consent."""
    payload = _approved_config().to_dict()
    payload.pop("reviewed_captures")
    with pytest.raises(ValidationError, match="missing or unknown fields"):
        Config.from_dict(payload)


def test_schema_seven_spacing_approval_remains_unchanged() -> None:
    """Adding workbook evidence must not alter existing diagnostic consent."""
    timestamp = "2026-10-29T12:34:56.000000Z"
    config = _approved_config()
    config = replace(
        config,
        review_tracking=replace(
            config.review_tracking,
            spacing_images=(
                SpacingImageReview(
                    "rf",
                    "F.Cu",
                    "issue-a",
                    "view-a",
                    "f" * 64,
                    "a" * 64,
                    timestamp,
                    "a" * 64,
                    timestamp,
                ),
            ),
        ),
    )
    payload = config.to_dict()
    payload["schema_version"] = 7
    payload.pop("reviewed_captures")
    restored = Config.from_dict(payload)
    assert restored.reviewed_captures == ()
    assert restored.review_tracking == config.review_tracking


def test_empty_workbook_evidence_is_valid_for_drafts_and_legacy_approval() -> None:
    """Empty evidence retains configurations that predate pixel baselines."""
    for config in (Config(), replace(_approved_config(), reviewed_captures=())):
        assert Config.from_dict(config.to_dict()) == config


@pytest.mark.parametrize(
    "captures",
    [
        [("row-a", "d" * 64)],
        (["row-a", "d" * 64],),
        (("row-a",),),
        (("row-a", "d" * 64, "extra"),),
        (("row-a", "d" * 64), ("row-a", "e" * 64)),
        (("", "d" * 64),),
        ((" ", "d" * 64),),
        ((12, "d" * 64),),
        (("row-c", "d" * 64),),
        (("row-a", "D" * 64),),
        (("row-a", "g" * 64),),
        (("row-a", "d" * 63),),
        (("row-a", "d" * 65),),
        (("row-a", "d" * 64 + "\n"),),
        (("row-a", 12),),
    ],
)
def test_storage_rejects_invalid_or_mutable_capture_baselines(
    captures: Any,
) -> None:
    """A valid baseline is immutable, unambiguous, canonical and inclusion-bound."""
    with pytest.raises(ValidationError):
        validate_storage_config(replace(_approved_config(), reviewed_captures=captures))


def test_workbook_pixels_cannot_be_persisted_without_workbook_approval() -> None:
    """Invalidating consent must clear its pixel authority along with its digest."""
    with pytest.raises(ValidationError, match="reviewed digest"):
        validate_storage_config(replace(_approved_config(), reviewed_digest=""))


@pytest.mark.parametrize(
    "captures",
    [
        None,
        {},
        "row-a",
        ["row-a"],
        [["row-a"]],
        [["row-a", "d" * 64, "extra"]],
        [["row-a", "d" * 64], ["row-a", "e" * 64]],
        [["row-c", "d" * 64]],
        [["row-a", "D" * 64]],
    ],
)
def test_decoder_rejects_malformed_capture_payloads(captures: Any) -> None:
    """Malformed JSON must produce the shared validation error, never coercion."""
    payload = _approved_config().to_dict()
    payload["reviewed_captures"] = captures
    with pytest.raises(ValidationError):
        Config.from_dict(payload)


def test_workbook_capture_collection_has_a_fixed_storage_limit() -> None:
    """Do not accept unbounded baseline data from either immutable or JSON input."""
    captures = tuple((f"row-{index}", "d" * 64) for index in range(10_001))
    config = replace(
        _approved_config(),
        included_section_ids=tuple(section_id for section_id, _digest in captures),
        reviewed_captures=captures,
    )
    with pytest.raises(ValidationError, match="bounded"):
        validate_storage_config(config)
    payload = _approved_config().to_dict()
    payload["included_section_ids"] = list(config.included_section_ids)
    payload["reviewed_captures"] = [list(pair) for pair in captures]
    with pytest.raises(ValidationError, match="bounded"):
        Config.from_dict(payload)


def test_workbook_capture_limit_accepts_the_maximum_valid_baseline() -> None:
    """The storage ceiling is inclusive and independent of image audit counts."""
    captures = tuple((f"row-{index}", "d" * 64) for index in range(10_000))
    config = replace(
        _approved_config(),
        included_section_ids=tuple(section_id for section_id, _digest in captures),
        reviewed_captures=captures,
    )
    assert Config.from_dict(config.to_dict()) == config
