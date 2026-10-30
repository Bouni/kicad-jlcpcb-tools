"""Bind spacing approvals to policy and exact geometry without rewriting history."""

from dataclasses import replace
from pathlib import Path

import pytest

from impedance import matching, model, review_tracking as tracking

from .test_impedance_incomplete_storage import _legacy_payload, _repository, _snapshot
from .test_impedance_pair_matching import _approved_capture
from .test_impedance_tracking_serialization import _fingerprints, _geometry


@pytest.mark.parametrize("future", [False, True])
def test_exact_arc_metadata_preserves_saved_image_fingerprints(future: bool) -> None:
    """New measurement metadata must not change historical pixel evidence."""
    snapshot, spec, sections = _geometry(future=future)
    before = _fingerprints(snapshot, spec, sections)
    traces = tuple(
        replace(
            trace,
            native_arc_midpoint=(1_000_000, 500_000),
            native_arc_center=(1_000_000, -750_000),
            native_arc_radius_nm=1_250_000.0,
        )
        if trace.is_native_arc
        else trace
        for trace in snapshot.traces
    )
    by_id = {trace.trace_id: trace for trace in traces}
    changed_sections = tuple(
        replace(section, traces=tuple(by_id[t.trace_id] for t in section.traces))
        for section in sections
    )
    assert (
        _fingerprints(replace(snapshot, traces=traces), spec, changed_sections)
        == before
    )


@pytest.mark.parametrize("paired", [False, True])
def test_spacing_policy_changes_only_differential_approval(
    monkeypatch: pytest.MonkeyPatch, paired: bool
) -> None:
    """Check policy currentness independently of pixels or storage upgrades."""
    from impedance import spacing_checks

    config, snapshot = _approved_capture(paired)
    analysis = matching.analyze(config, snapshot)
    spec = config.specifications[0]
    source = tracking.source_fingerprint(snapshot)
    layer_before = tracking.layer_fingerprint(source, spec, "F.Cu", analysis.sections)
    image_before = tracking.capture_fingerprint(
        source, spec, analysis.sections[0], "a" * 64
    )
    monkeypatch.setattr(
        spacing_checks,
        "SPACING_CHECK_POLICY_REVISION",
        spacing_checks.SPACING_CHECK_POLICY_REVISION + 1,
    )
    changed = matching.analyze(config, snapshot)
    assert changed.sections == analysis.sections
    assert (changed.digest != analysis.digest) is paired
    assert (
        tracking.layer_fingerprint(source, spec, "F.Cu", changed.sections)
        != layer_before
    ) is paired
    assert (
        tracking.capture_fingerprint(source, spec, changed.sections[0], "a" * 64)
        == image_before
    )
    assert model.Config.from_dict(config.to_dict()) == config
    if paired:
        with pytest.raises(model.ValidationError, match="changed|policy"):
            matching.validate_review(config, changed)
    else:
        matching.validate_review(config, changed)


def test_exact_arc_metadata_is_bound_across_all_layer_rows() -> None:
    """A mate in another width row participates even if that row is unchecked."""
    snapshot, spec, sections = _geometry()
    spec = replace(
        spec,
        kind="differential",
        target_ohms="90",
        layer_settings=(model.LayerSettings("F.Cu", ("B.Cu",), 150_000),),
    )
    source = tracking.source_fingerprint(snapshot)
    before = tracking.layer_fingerprint(source, spec, "F.Cu", sections)
    arc = replace(snapshot.traces[0], native_arc_midpoint=(1_000_000, 500_000))
    changed = (replace(sections[0], traces=(arc,)), sections[1])
    assert tracking.layer_fingerprint(source, spec, "F.Cu", changed) != before
    assert tracking.capture_fingerprint(
        source, spec, sections[0], "a" * 64
    ) == tracking.capture_fingerprint(source, spec, changed[0], "a" * 64)


@pytest.mark.parametrize("version", [4, 5, 6])
def test_spacing_is_derived_after_real_storage_reopen(
    tmp_path: Path, version: int
) -> None:
    """Loading old/current intent must neither persist gaps nor invent events."""
    from impedance.spacing_checks import spacing_checks_for_sections

    repository = _repository(tmp_path)
    payload = _legacy_payload(version)
    if version == 6:
        payload = model.Config.from_dict(_legacy_payload(5)).to_dict()
        payload["schema_version"] = 6
        del payload["reviewed_captures"]
        del payload["review_tracking"]["spacing_images"]
    repository.database.save_config(repository.board_id, payload, True, 0)
    record = repository.database.load_config(repository.board_id)
    config, _revision = repository.load()
    snapshot = _snapshot()
    analysis = matching.analyze(config, snapshot)
    checks = spacing_checks_for_sections(config, snapshot, analysis.sections)
    assert checks and all(check.verdict == "match" for check in checks.values())
    assert repository.database.load_config(repository.board_id) == record
    assert config.review_tracking == tracking.ReviewTracking()
    assert "spacing_check" not in config.to_dict()


@pytest.mark.parametrize("paired", [False, True])
def test_pad_contact_changes_differential_analysis_without_changing_rows(
    paired: bool,
) -> None:
    """Pad-derived launch boundaries are review inputs even with identical traces."""
    config, snapshot = _approved_capture(paired)
    before = matching.analyze(config, snapshot)
    trace = snapshot.traces[0]
    pad = model.CopperFeature(
        "terminal-contact",
        trace.net,
        trace.layer,
        "pad",
        "circle",
        trace.points[0],
        400_000,
        400_000,
    )
    changed = replace(snapshot, copper_features=(*snapshot.copper_features, pad))
    after = matching.analyze(config, changed)
    assert after.sections == before.sections
    assert (after.digest != before.digest) is paired


def test_covered_stub_has_no_verdict_and_does_not_create_a_routing_issue() -> None:
    """Copper wholly inside a pad cannot manufacture a routed spacing warning."""
    from impedance.spacing_checks import (
        measure_pair_spacing,
        spacing_checks_for_sections,
    )

    config = model.Config.from_dict(_legacy_payload(5))
    snapshot = _snapshot()
    stub = model.Trace(
        "covered-launch",
        "F.Cu",
        "USB_P",
        200_000,
        ((-2_000_000, 0), (-1_900_000, 0)),
    )
    snapshot = replace(
        snapshot,
        traces=(*snapshot.traces, stub),
        copper_features=(
            model.CopperFeature(
                "launch-pad",
                "USB_P",
                "F.Cu",
                "pad",
                "circle",
                (-1_950_000, 0),
                600_000,
                600_000,
            ),
        ),
    )
    analysis = matching.analyze(config, snapshot)
    covered = next(section for section in analysis.sections if section.exclusion_reason)
    checks = spacing_checks_for_sections(config, snapshot, analysis.sections)
    assert covered.section_id not in checks
    front = next(
        section
        for section in analysis.sections
        if section.layer == "F.Cu" and not section.exclusion_reason
    )
    assert checks[front.section_id].verdict == "match"
    assert checks[front.section_id].coverage == "complete"
    assert checks[front.section_id].issues == ()
    measurement = measure_pair_spacing(snapshot, ("USB_P", "USB_N"), "F.Cu")
    launch = next(
        trace for trace in measurement.traces if trace.trace_id == stub.trace_id
    )
    assert launch.transition_length_nm == 100_000
    assert launch.length_nm == 0
