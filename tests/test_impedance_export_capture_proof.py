"""Require reviewed pixels before either report can reach publication."""

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

import pytest

from impedance import (
    html_report,
    matching,
    review_tracking,
    service,
    stackup_model,
    workbook,
)
from impedance.model import Section, ValidationError
from impedance.review_tracking import ImageView, LayerApproval, ReviewTracking
from tests.test_impedance_report_pipeline import _png
from tests.test_impedance_service import _board, _configuration, _review

_TIME = "2026-10-20T12:13:14.000000Z"


def _plan(
    *, image_history: bool = True, layer_history: bool = True
) -> service.ExportPlan:
    """Approve real matched geometry with deterministic previously viewed pixels."""
    snapshot = _board()
    config = _review(_configuration(), snapshot)
    analysis = matching.analyze(config, snapshot)
    spec = config.specifications[0]
    source = review_tracking.source_fingerprint(snapshot)
    image_sha = service.CapturedImage.from_bytes(_png()).sha256
    captures = tuple(
        (
            section.section_id,
            review_tracking.capture_fingerprint(source, spec, section, image_sha),
        )
        for section in analysis.sections
    )
    history = ReviewTracking(
        tuple(
            ImageView(spec.spec_id, section.layer, section.section_id, digest, _TIME)
            for section, (_, digest) in zip(analysis.sections, captures)
        )
        if image_history
        else (),
        tuple(
            LayerApproval(
                spec.spec_id,
                layer,
                review_tracking.layer_fingerprint(
                    source, spec, layer, analysis.sections
                ),
                _TIME,
                tuple(
                    capture
                    for section, capture in zip(analysis.sections, captures)
                    if section.layer == layer
                ),
            )
            for layer in {section.layer for section in analysis.sections}
        )
        if layer_history
        else (),
    )
    result = service.prepare(replace(config, review_tracking=history), snapshot)
    assert result is not None
    return result


class _Renderer:
    """Record one capture per row and actual renderer cleanup."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.sections: list[Section] = []
        self.closed = False

    def render(
        self, section: Section, destination: Path, width_px: int, height_px: int
    ) -> Path:
        """Create deterministic owned pixels for the requested real section."""
        self.sections.append(section)
        destination.write_bytes(self.data)
        return destination

    def close(self) -> None:
        """Expose release on success and every validation failure."""
        self.closed = True


def _writers(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[Sequence[service.ReportRow]], service.WorkbookWriter]:
    """Observe both publication consumers while keeping their output contract."""
    seen: list[Sequence[service.ReportRow]] = []

    def write(
        rows: Sequence[service.ReportRow], destination: Path, **kwargs: Any
    ) -> Path:
        seen.append(rows)
        destination.write_bytes(b"completed report")
        return destination

    monkeypatch.setattr(html_report, "write_html_report", write)
    return seen, write


@pytest.mark.parametrize("evidence", ["image", "layer", "both", "mixed"])
def test_changed_known_capture_stops_both_report_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, evidence: str
) -> None:
    """Current global approval cannot authorize changed previously recorded pixels."""
    plan = _plan(image_history=evidence != "layer", layer_history=evidence == "both")
    if evidence == "layer":
        plan = _plan(image_history=False)
    elif evidence == "mixed":
        history = plan.config.review_tracking
        plan = replace(
            plan,
            config=replace(
                plan.config,
                review_tracking=replace(history, images=history.images[:1]),
            ),
        )
    before = plan.config.to_dict()
    renderer = _Renderer(_png(color=b"\xff\x00\x00"))
    seen, writer = _writers(monkeypatch)
    outputs = (
        tmp_path / workbook.OUTPUT_FILENAME,
        tmp_path / html_report.OUTPUT_FILENAME,
    )
    for output in outputs:
        output.write_bytes(b"previous report")
    with pytest.raises(ValidationError, match="Configure"):
        service.export_reports(
            plan,
            object(),
            object(),
            tmp_path,
            renderer_factory=lambda _board, _pcbnew: renderer,
            writer=writer,
        )
    assert renderer.closed
    assert seen == []
    assert all(output.read_bytes() == b"previous report" for output in outputs)
    assert plan.config.to_dict() == before


@pytest.mark.parametrize("history", ["current", "legacy", "layer_only", "old_layer"])
def test_current_captures_and_legacy_history_share_owned_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, history: str
) -> None:
    """Historical layer settings cannot veto current image evidence or legacy consent."""
    plan = _plan(image_history=history != "layer_only")
    if history == "legacy":
        plan = replace(
            plan, config=replace(plan.config, review_tracking=ReviewTracking())
        )
    elif history == "old_layer":
        tracking = plan.config.review_tracking
        plan = replace(
            plan,
            config=replace(
                plan.config,
                review_tracking=replace(
                    tracking,
                    layers=tuple(
                        replace(layer, settings_digest="0" * 64, captures=())
                        for layer in tracking.layers
                    ),
                ),
            ),
        )
    renderer = _Renderer(_png())
    seen, writer = _writers(monkeypatch)
    before = plan.config.to_dict()
    service.export_reports(
        plan,
        object(),
        object(),
        tmp_path,
        renderer_factory=lambda _board, _pcbnew: renderer,
        writer=writer,
    )
    assert renderer.closed and renderer.sections == list(plan.sections)
    assert len(seen) == 2 and seen[0] is seen[1]
    assert plan.config.to_dict() == before
    if history == "old_layer":
        assert not any(row.layer_approval_current for row in seen[0])
        assert all(row.last_viewed_current for row in seen[0])


@pytest.mark.parametrize("changed", [False, True])
def test_appearance_is_bound_to_rendered_capture_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: bool
) -> None:
    """Theme mutation during capture must stop both writers even without history."""
    plan = _plan(image_history=False, layer_history=False)
    renderer = _Renderer(_png())
    seen, writer = _writers(monkeypatch)
    appearances = iter(("theme-a", "theme-b" if changed else "theme-a"))

    def appearance() -> object:
        return next(appearances)

    def export() -> service.ExportArtifacts:
        return service.export_reports(
            plan,
            object(),
            object(),
            tmp_path,
            renderer_factory=lambda _board, _pcbnew: renderer,
            writer=writer,
            appearance_context=appearance,
        )

    if changed:
        with pytest.raises(ValidationError, match="Configure"):
            export()
        assert seen == []
    else:
        assert export().appearance == "theme-a"
        assert len(seen) == 2
    assert renderer.closed


@pytest.mark.parametrize("policy", ["capture", "covered_stub", "calculation"])
def test_layer_approval_fingerprint_binds_all_review_policies(
    monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    """Old layer approval cannot restore after classification/calculation changes."""
    plan = _plan()
    spec = plan.config.specifications[0]
    source = review_tracking.source_fingerprint(plan.snapshot)
    old = review_tracking.layer_fingerprint(
        source, spec, "F.Cu", plan.analysis.sections
    )
    owner, name = {
        "capture": (matching, "CAPTURE_POLICY_REVISION"),
        "covered_stub": (matching, "COVERED_STUB_POLICY_REVISION"),
        "calculation": (stackup_model, "CALCULATION_REVISION"),
    }[policy]
    monkeypatch.setattr(owner, name, getattr(owner, name) + 1)
    assert (
        review_tracking.layer_fingerprint(source, spec, "F.Cu", plan.analysis.sections)
        != old
    )


def test_expected_capture_uses_latest_image_before_layer_history() -> None:
    """The shared selector treats image viewing as authoritative pixel evidence."""
    plan = _plan()
    spec = plan.config.specifications[0]
    section = plan.sections[0]
    image = next(
        item
        for item in plan.config.review_tracking.images
        if item.section_id == section.section_id
    )
    history = review_tracking.put_image(
        plan.config.review_tracking, replace(image, capture_digest="1" * 64)
    )
    assert (
        review_tracking.expected_capture_digest(
            history,
            review_tracking.source_fingerprint(plan.snapshot),
            spec,
            section,
            plan.analysis.sections,
        )
        == "1" * 64
    )


@pytest.mark.parametrize("current", [False, True])
def test_expected_capture_only_falls_back_to_current_settings_layer(
    current: bool,
) -> None:
    """Absent image events do not revive a layer capture from different intent."""
    plan = _plan(image_history=False)
    spec = plan.config.specifications[0]
    section = plan.sections[0]
    history = plan.config.review_tracking
    if not current:
        history = replace(
            history,
            layers=tuple(
                replace(item, settings_digest="0" * 64) for item in history.layers
            ),
        )
    expected: Optional[str] = None
    if current:
        layer = next(item for item in history.layers if item.layer == section.layer)
        expected = dict(layer.captures)[section.section_id]
    assert (
        review_tracking.expected_capture_digest(
            history,
            review_tracking.source_fingerprint(plan.snapshot),
            spec,
            section,
            plan.analysis.sections,
        )
        == expected
    )


def test_latest_viewed_pixels_override_older_layer_capture_at_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A newly approved workbook can retain older per-layer audit history."""
    plan = _plan()
    fresh_image = service.CapturedImage.from_bytes(_png(color=b"\x10\x20\x30"))
    source = review_tracking.source_fingerprint(plan.snapshot)
    spec = plan.config.specifications[0]
    history = replace(
        plan.config.review_tracking,
        images=tuple(
            ImageView(
                spec.spec_id,
                section.layer,
                section.section_id,
                review_tracking.capture_fingerprint(
                    source, spec, section, fresh_image.sha256
                ),
                _TIME,
            )
            for section in plan.sections
        ),
    )
    plan = replace(plan, config=replace(plan.config, review_tracking=history))
    renderer = _Renderer(fresh_image.data)
    seen, writer = _writers(monkeypatch)
    service.export_reports(
        plan,
        object(),
        object(),
        tmp_path,
        renderer_factory=lambda _board, _pcbnew: renderer,
        writer=writer,
    )
    assert len(seen) == 2
    assert all(row.last_viewed_current for row in seen[0])
    assert not any(row.layer_approval_current for row in seen[0])


def test_omitted_approved_sibling_keeps_current_layer_label_during_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Current workbook consent controls inclusion; layer audit remains complete."""
    plan = _plan()
    front = tuple(section for section in plan.sections if section.layer == "F.Cu")
    assert len(front) == 2
    sections = tuple(section for section in plan.sections if section != front[1])
    config = replace(
        plan.config,
        included_section_ids=tuple(section.section_id for section in sections),
    )
    plan = service.prepare(config, plan.snapshot)
    assert plan is not None
    renderer = _Renderer(_png())
    seen, writer = _writers(monkeypatch)
    service.export_reports(
        plan,
        object(),
        object(),
        tmp_path,
        renderer_factory=lambda _board, _pcbnew: renderer,
        writer=writer,
    )
    assert len(seen) == 2 and len(seen[0]) == 2
    assert all(row.last_viewed_current for row in seen[0])
    assert next(
        row for row in seen[0] if row.signal_layer == "F.Cu"
    ).layer_approval_current
    assert next(
        row for row in seen[0] if row.signal_layer == "B.Cu"
    ).layer_approval_current


@pytest.mark.parametrize("stage", ["before", "after"])
def test_unreadable_appearance_prevents_writers_and_releases_renderer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """A missing appearance token is a failed verification, never legacy proof."""
    plan = _plan()
    renderer = _Renderer(_png())
    seen, writer = _writers(monkeypatch)
    calls = 0

    def appearance() -> object:
        nonlocal calls
        calls += 1
        if stage == "before" or calls == 2:
            raise RuntimeError("appearance unavailable")
        return "theme-a"

    with pytest.raises(RuntimeError, match="appearance unavailable"):
        service.export_reports(
            plan,
            object(),
            object(),
            tmp_path,
            renderer_factory=lambda _board, _pcbnew: renderer,
            writer=writer,
            appearance_context=appearance,
        )
    assert seen == []
    assert renderer.closed is (stage == "after")
    assert bool(renderer.sections) is (stage == "after")
