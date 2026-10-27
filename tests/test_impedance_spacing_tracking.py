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


@pytest.mark.parametrize("legacy_revision", (5, 6))
def test_concern_render_policy_retains_old_approval_as_history_after_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy_revision: int
) -> None:
    """Render 5/6→7 requires fresh consent while exact spacing and trace events stay saved."""
    from impedance import spacing_review
    from impedance.spacing_checks import SPACING_CHECK_POLICY_REVISION
    from tests.test_impedance_report_pipeline import _spacing_plan

    plan = _spacing_plan(100_000)
    with monkeypatch.context() as old_policy:
        old_policy.setattr(
            spacing_review, "SPACING_RENDER_POLICY_REVISION", legacy_revision
        )
        previous = spacing_review.spacing_review_entries(
            plan.config, plan.snapshot, plan.analysis.sections
        )
    assert previous
    records = tuple(
        tracking.SpacingImageReview(
            entry.spec_id,
            entry.layer,
            entry.issue_id,
            entry.view_id,
            entry.input_digest,
            tracking.spacing_capture_fingerprint(entry.input_digest, PNG),
            VIEWED,
            tracking.spacing_capture_fingerprint(entry.input_digest, PNG),
            APPROVED,
        )
        for entry in previous
    )
    source = tracking.source_fingerprint(plan.snapshot)
    specification = plan.config.specifications[0]
    trace_images = tuple(
        tracking.ImageView(
            section.spec_id,
            section.layer,
            section.section_id,
            tracking.capture_fingerprint(source, specification, section, PNG),
            VIEWED,
        )
        for section in plan.analysis.sections
    )
    trace_layers = tuple(
        tracking.LayerApproval(
            specification.spec_id,
            layer,
            tracking.layer_fingerprint(
                source,
                specification,
                layer,
                plan.analysis.sections,
                stackup=plan.config.stackup,
                width_results=plan.config.width_results,
            ),
            APPROVED,
            tuple(
                (image.section_id, image.capture_digest)
                for image in trace_images
                if image.layer == layer
            ),
        )
        for layer in sorted({section.layer for section in plan.analysis.sections})
    )
    config = replace(
        plan.config,
        review_tracking=tracking.ReviewTracking(trace_images, trace_layers, records),
    )
    repository = _repository(tmp_path)
    repository.save(config, 0)

    def unexpected_clock() -> str:
        raise AssertionError("Opening a revised image cannot manufacture consent")

    monkeypatch.setattr(tracking, "utc_now", unexpected_clock)
    assert SPACING_CHECK_POLICY_REVISION == 4
    reopened, revision = _repository(tmp_path).load()
    current = spacing_review.spacing_review_entries(
        reopened, plan.snapshot, plan.analysis.sections
    )
    pending = spacing_review.pending_spacing_reviews(
        reopened, plan.snapshot, plan.analysis.sections
    )
    assert revision == 1 and current
    assert pending == current
    assert spacing_review.SPACING_RENDER_POLICY_REVISION == 7
    assert set(current).isdisjoint(previous)
    assert reopened.review_tracking.spacing_images == records
    assert reopened.review_tracking.images == trace_images
    assert reopened.review_tracking.layers == trace_layers
    assert tracking.specification_layer_review_current(
        reopened.review_tracking,
        source,
        specification,
        plan.analysis.sections,
        stackup=plan.config.stackup,
        width_results=plan.config.width_results,
        capture_digests={
            image.section_id: image.capture_digest for image in trace_images
        },
    )
    assert {entry.issue_id for entry in current} == {
        entry.issue_id for entry in previous
    }
    assert all(
        entry.view_id not in {old.view_id for old in previous} for entry in current
    )
    assert repository.load() == (reopened, revision)


def test_seven_historical_crops_reopen_as_two_unapproved_continuous_concerns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Old five-plus-two crop approvals cannot approve replacement concern images."""
    from impedance import spacing_review
    from impedance.matching import analyze
    from impedance.model import Trace
    from tests.test_impedance_report_pipeline import _spacing_plan

    plan = _spacing_plan(331_300)
    pairs = (("PONG_D+", "PONG_D-"), ("PONG_IN_D+", "PONG_IN_D-"))
    traces = tuple(
        Trace(
            f"pair-{index}-{side}",
            "F.Cu",
            net,
            150_000,
            (
                (0, index * 5_000_000 + side * (gap + 150_000)),
                (length, index * 5_000_000 + side * (gap + 150_000)),
            ),
        )
        for index, (pair, gap, length) in enumerate(
            ((pairs[0], 331_300, 24_000_000), (pairs[1], 206_300, 10_000_000))
        )
        for side, net in enumerate(pair)
    )
    snapshot = replace(
        plan.snapshot,
        traces=traces,
        differential_pairs=pairs,
        net_class_memberships=tuple((net, ("USB",)) for pair in pairs for net in pair),
    )
    spec = replace(
        plan.config.specifications[0],
        layer_settings=(LayerSettings("F.Cu", ("B.Cu",), 202_300),),
    )
    # Historical capture identities are deliberately independent of the new
    # selector: changing the selector must not rewrite saved consent to fit it.
    records = tuple(
        replace(
            _record(view_id=f"policy-5-concern-{concern}-crop-{crop}", approved=True),
            issue_id=f"policy-5-concern-{concern}",
        )
        for concern, count in enumerate((5, 2))
        for crop in range(count)
    )
    config = replace(
        plan.config,
        specifications=(spec,),
        review_tracking=tracking.ReviewTracking(spacing_images=records),
    )
    sections = analyze(config, snapshot).sections
    repository = _repository(tmp_path)
    repository.save(config, 0)

    def unexpected_clock() -> str:
        raise AssertionError("Regenerating or reopening concerns cannot grant consent")

    monkeypatch.setattr(tracking, "utc_now", unexpected_clock)
    reopened, revision = repository.load()
    entries = spacing_review.spacing_review_entries(reopened, snapshot, sections)
    assert len(entries) == 2
    assert {entry.pair for entry in entries} == {tuple(sorted(pair)) for pair in pairs}
    assert (
        spacing_review.pending_spacing_reviews(reopened, snapshot, sections) == entries
    )
    assert reopened.review_tracking.spacing_images == records
    assert all(
        not tracking.spacing_image_current(
            tracking.find_spacing_image(
                reopened.review_tracking, entry.spec_id, entry.layer, entry.view_id
            ),
            entry.input_digest,
            approved=True,
        )
        for entry in entries
    )
    repository.save(reopened, revision)
    again, again_revision = _repository(tmp_path).load()
    assert again_revision == revision + 1
    assert again.review_tracking.spacing_images == records
    assert spacing_review.pending_spacing_reviews(again, snapshot, sections) == entries

    first = entries[0]
    current_capture = tracking.spacing_capture_fingerprint(first.input_digest, PNG)
    current_view = tracking.SpacingImageReview(
        first.spec_id,
        first.layer,
        first.issue_id,
        first.view_id,
        first.input_digest,
        current_capture,
        "2026-10-26T12:34:56.000001Z",
    )
    viewed = replace(
        again,
        review_tracking=tracking.put_spacing_image(again.review_tracking, current_view),
    )
    assert spacing_review.pending_spacing_reviews(viewed, snapshot, sections) == entries
    current_approval = replace(
        current_view,
        approved_digest=current_capture,
        approved_at_utc="2026-10-26T12:35:56.000002Z",
    )
    partly_approved = replace(
        viewed,
        review_tracking=tracking.put_spacing_image(
            viewed.review_tracking, current_approval
        ),
    )
    assert (
        spacing_review.pending_spacing_reviews(partly_approved, snapshot, sections)
        == entries[1:]
    )
    repository.save(partly_approved, again_revision)
    final, final_revision = _repository(tmp_path).load()
    assert final_revision == again_revision + 1
    assert len(final.review_tracking.spacing_images) == 8
    assert all(record in final.review_tracking.spacing_images for record in records)
    assert current_approval in final.review_tracking.spacing_images
    assert final.review_tracking == partly_approved.review_tracking
    assert (
        spacing_review.pending_spacing_reviews(final, snapshot, sections) == entries[1:]
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
