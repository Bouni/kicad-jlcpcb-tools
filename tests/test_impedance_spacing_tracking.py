"""Keep spacing-image consent separate, exact, and stable through persistence."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from typing import Any

import pytest

from impedance import review_tracking as tracking
from impedance.database import ImpedanceDatabase
from impedance.model import Config, LayerSettings, Specification, ValidationError
from impedance.repository import ImpedanceRepository

VIEWED = "2026-10-24T12:34:56.000001Z"
APPROVED = "2026-10-24T12:35:56.000002Z"
INPUT = "a" * 64
PNG = "b" * 64


def _record(
    spec_id: str = "usb", *, view_id: str = "issue-a-1", approved: bool = False
) -> tracking.SpacingImageReview:
    """Construct explicit diagnostic events without borrowing workbook consent."""
    digest = tracking.spacing_capture_fingerprint(INPUT, PNG)
    return tracking.SpacingImageReview(
        spec_id,
        "F.Cu",
        "issue-a",
        view_id,
        INPUT,
        digest,
        VIEWED,
        digest if approved else "",
        APPROVED if approved else "",
    )


def _repository(tmp_path: Path) -> ImpedanceRepository:
    """Exercise real board-scoped SQLite save/load rather than JSON alone."""
    board_path = tmp_path / "board.kicad_pcb"
    board_path.write_text("(kicad_pcb)\n", encoding="utf-8")
    database = ImpedanceDatabase(tmp_path / "jlcpcb" / "project.db")
    return ImpedanceRepository(database, database.resolve_board(board_path))


def _legacy_payload(version: int, *, incomplete: bool = False) -> dict[str, Any]:
    """Describe historical records independently of the latest tracking serializer."""
    spec = {
        "spec_id": "usb",
        "label": "USB",
        "target_ohms": "" if incomplete else "90",
        "kind": "differential",
        "net_class": "USB",
        "layer_settings": [
            {
                "layer": "F.Cu",
                "reference_layers": ["B.Cu"],
                "spacing_nm": None if incomplete else 200_000,
                "ground_gap_nm": None,
            }
        ],
        "excluded_layers": [],
    }
    if version >= 6:
        spec.update(linked_layer_groups=[], independent_layers=[])
    result: dict[str, Any] = {
        "schema_version": version,
        "enabled": True,
        "specifications": [spec],
        "reviewed_digest": "historical-review",
        "included_section_ids": ["historical-section"],
        "review_tracking": {
            "images": [
                {
                    "spec_id": "usb",
                    "layer": "F.Cu",
                    "section_id": "historical-section",
                    "capture_digest": "c" * 64,
                    "viewed_at_utc": VIEWED,
                }
            ],
            "layers": [
                {
                    "spec_id": "usb",
                    "layer": "F.Cu",
                    "settings_digest": "d" * 64,
                    "approved_at_utc": APPROVED,
                    "captures": [["historical-section", "c" * 64]],
                }
            ],
        },
        "stackup": None,
        "width_results": [],
    }
    if version == 4:
        result.update(section_groups=[], legacy_specifications=[])
    return result


def test_schema7_spacing_images_save_reopen_preserves_exact_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only explicit UI events create times; opening/saving keeps existing consent."""
    record = _record(approved=True)
    history = tracking.ReviewTracking(spacing_images=(record,))
    config = Config(
        enabled=True,
        specifications=(
            Specification(
                "usb",
                "USB",
                "90",
                "differential",
                "USB",
                (LayerSettings("F.Cu", ("B.Cu",), 200_000),),
            ),
        ),
        review_tracking=history,
    )
    repository = _repository(tmp_path)

    def unexpected_clock() -> str:
        raise AssertionError("Persistence must not manufacture review events")

    monkeypatch.setattr(tracking, "utc_now", unexpected_clock)
    assert repository.save(config, 0) == 1
    before = repository.database.load_config(repository.board_id)
    assert before is not None
    assert before["payload"]["schema_version"] == 8
    assert repository.load() == (config, 1)
    assert repository.database.load_config(repository.board_id) == before
    assert repository.save(config, 1) == 2
    assert repository.load() == (config, 2)
    assert tracking.find_spacing_image(history, "usb", "F.Cu", "issue-a-1") == record


def test_spacing_review_requires_its_own_exact_viewing_and_approval() -> None:
    """Input changes, new pixels and ordinary approvals cannot inherit consent."""
    record = _record()
    capture = tracking.spacing_capture_fingerprint(INPUT, PNG)
    assert tracking.spacing_image_current(record, INPUT, capture)
    assert not tracking.spacing_image_current(record, INPUT, capture, approved=True)
    assert not tracking.spacing_image_current(None, INPUT, capture, approved=True)
    approved = replace(record, approved_digest=capture, approved_at_utc=APPROVED)
    assert tracking.spacing_image_current(approved, INPUT, capture, approved=True)
    assert not tracking.spacing_image_current(
        approved, "c" * 64, capture, approved=True
    )
    assert not tracking.spacing_image_current(approved, INPUT, "d" * 64, approved=True)
    newly_viewed = replace(approved, viewed_digest="e" * 64, viewed_at_utc=APPROVED)
    assert not tracking.spacing_image_current(newly_viewed, INPUT, approved=True)
    other_input = tracking.spacing_capture_fingerprint("f" * 64, PNG)
    other_pixels = tracking.spacing_capture_fingerprint(INPUT, "f" * 64)
    assert len({capture, other_input, other_pixels}) == 3


def test_tracking_edits_preserve_separate_spacing_events() -> None:
    """Specification accept/cancel buffers cannot lose or cross-pollinate images."""
    usb = _record(approved=True)
    rf = _record("rf")
    history = tracking.ReviewTracking(spacing_images=(usb, rf))
    image = tracking.ImageView("usb", "F.Cu", "row", "c" * 64, VIEWED)
    layer = tracking.LayerApproval("usb", "F.Cu", "d" * 64, APPROVED)
    assert tracking.put_image(history, image).spacing_images == (usb, rf)
    assert tracking.put_layer(history, layer).spacing_images == (usb, rf)
    assert tracking.for_spec(history, "usb").spacing_images == (usb,)
    assert tracking.retain_specs(history, ("rf",)).spacing_images == (rf,)
    replacement = tracking.put_spacing_image(
        tracking.for_spec(history, "usb"), replace(usb, issue_id="new-issue")
    )
    assert len(replacement.spacing_images) == 1
    merged = tracking.merge_spec(history, replacement, "usb")
    assert set(merged.spacing_images) == {rf, replace(usb, issue_id="new-issue")}
    with pytest.raises(ValidationError, match="other specifications"):
        tracking.merge_spec(history, tracking.for_spec(history, "rf"), "usb")
    assert history.spacing_images == (usb, rf)


def test_new_viewing_retains_prior_approval_as_stale_history() -> None:
    """A changed diagnostic can be viewed without rewriting earlier consent."""
    approved = _record(approved=True)
    new_input = "f" * 64
    new_capture = tracking.spacing_capture_fingerprint(new_input, PNG)
    viewed = replace(
        approved,
        input_digest=new_input,
        viewed_digest=new_capture,
        viewed_at_utc="2026-10-24T12:36:56.000003Z",
    )
    config = Config(review_tracking=tracking.ReviewTracking(spacing_images=(viewed,)))
    restored = Config.from_dict(config.to_dict()).review_tracking.spacing_images[0]
    assert restored == viewed
    assert restored.approved_digest == approved.approved_digest
    assert restored.approved_at_utc == APPROVED
    assert tracking.spacing_image_current(restored, new_input, new_capture)
    assert not tracking.spacing_image_current(
        restored, new_input, new_capture, approved=True
    )


@pytest.mark.parametrize("version", [4, 5, 6])
def test_legacy_load_preserves_history_without_spacing_consent_or_writes(
    tmp_path: Path, version: int
) -> None:
    """No historical manufacturing approval implicitly approves diagnostic images."""
    repository = _repository(tmp_path)
    payload = _legacy_payload(version)
    raw = json.dumps(payload, indent=2) + "\n"
    with repository.database.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO board_feature_config VALUES (?, 'impedance', 1, 7, 1, ?)",
            (repository.board_id, raw),
        )
    before = repository.database.load_config(repository.board_id)
    config, revision = repository.load()
    assert revision == 7
    assert config.review_tracking.spacing_images == ()
    upgraded = config.to_dict()
    assert upgraded["schema_version"] == 8
    assert upgraded["review_tracking"] == {
        **payload["review_tracking"],
        "spacing_images": [],
    }
    assert repository.database.load_config(repository.board_id) == before
    assert payload == _legacy_payload(version)


def test_schema6_incomplete_draft_remains_loadable_after_schema7_upgrade() -> None:
    """A migration must not turn a previously saved unfinished draft into an error."""
    payload = _legacy_payload(6, incomplete=True)
    before = deepcopy(payload)
    config = Config.from_dict(payload)
    assert config.enabled
    assert config.specifications[0].target_ohms == ""
    assert config.specifications[0].layer_settings[0].spacing_nm is None
    assert config.review_tracking.spacing_images == ()
    assert Config.from_dict(config.to_dict()) == config
    assert payload == before


@pytest.mark.parametrize("version", [4, 5])
def test_earlier_schemas_retain_strict_incomplete_validation(version: int) -> None:
    """The existing schema6 draft allowance does not relax retired formats."""
    with pytest.raises(ValidationError):
        Config.from_dict(_legacy_payload(version, incomplete=True))


@pytest.mark.parametrize(
    "changes",
    [
        {"spec_id": " "},
        {"layer": ""},
        {"issue_id": ""},
        {"view_id": ""},
        {"input_digest": "A" * 64},
        {"viewed_digest": "invalid"},
        {"viewed_at_utc": "2026-10-24T12:34:56Z"},
        {"viewed_at_utc": "2026-02-30T12:34:56.000000Z"},
        {"approved_digest": "f" * 64},
        {"approved_at_utc": APPROVED},
        {"approved_digest": "f" * 64, "approved_at_utc": "yesterday"},
    ],
)
def test_malformed_spacing_event_is_rejected(changes: dict[str, Any]) -> None:
    """Persistence enforces identifiers, canonical hashes and complete UTC events."""
    with pytest.raises(ValidationError):
        tracking.validate_tracking(
            tracking.ReviewTracking(spacing_images=(replace(_record(), **changes),))
        )


def test_spacing_records_are_bounded_unique_and_strictly_decoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bound JSON before decoding and reject missing/unknown schema7 fields."""
    record = _record()
    with pytest.raises(ValidationError, match="repeat"):
        tracking.validate_tracking(
            tracking.ReviewTracking(spacing_images=(record, record))
        )
    monkeypatch.setattr(tracking, "MAX_SPACING_IMAGE_RECORDS", 1)
    with pytest.raises(ValidationError, match="at most 1"):
        tracking.validate_tracking(
            tracking.ReviewTracking(
                spacing_images=(record, _record(view_id="issue-a-2"))
            )
        )
    payload = Config(
        review_tracking=tracking.ReviewTracking(spacing_images=(record,))
    ).to_dict()
    assert Config.from_dict(payload).review_tracking.spacing_images == (record,)
    original = deepcopy(payload)
    del payload["review_tracking"]["spacing_images"]
    with pytest.raises(ValidationError, match="tracking"):
        Config.from_dict(payload)
    payload = deepcopy(original)
    payload["review_tracking"]["spacing_images"][0]["approved"] = True
    with pytest.raises(ValidationError, match="spacing image"):
        Config.from_dict(payload)
    payload = deepcopy(original)
    payload["review_tracking"]["spacing_images"] *= 2
    with pytest.raises(ValidationError, match="at most 1"):
        Config.from_dict(payload)


@pytest.mark.parametrize("version", [4, 5, 6])
def test_legacy_schema_cannot_smuggle_spacing_approval(version: int) -> None:
    """Only schema7 defines diagnostic review records; old JSON remains strict."""
    payload = _legacy_payload(version)
    payload["review_tracking"]["spacing_images"] = []
    with pytest.raises(ValidationError, match="tracking"):
        Config.from_dict(payload)
