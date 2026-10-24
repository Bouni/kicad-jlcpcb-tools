"""Exercise advisory spacing diagnostics through immutable rows and real writers."""

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
import re
from typing import Any, Literal
from zipfile import ZipFile

import pytest

from impedance import html_report, service, spacing_checks, workbook
from impedance._vendor.openpyxl import load_workbook
from impedance.matching import analyze
from impedance.model import Trace
from impedance.spacing_checks import (
    NmBounds,
    SpacingCheck,
    SpacingIssue,
    SpacingLocation,
)

from .test_impedance_report_pipeline import _html_images, _png, _row, _spacing_plan


def _images(plan: service.ExportPlan) -> tuple[service.CapturedImage, ...]:
    """Produce immutable captures for every row without native renderer dependency."""
    return tuple(service.CapturedImage.from_bytes(_png()) for _ in plan.sections)


def test_html_keeps_manufacturing_capture_free_of_spacing_annotations(
    tmp_path: Path,
) -> None:
    """A routed defect never places badges or boxes over the workbook capture."""
    plan = _spacing_plan(100_000)
    image = service.CapturedImage.from_bytes(
        _png(880, 120),
        viewport_nm=(-500_000, -500_000, 11_000_000, 1_500_000),
    )
    before = image.data, image.sha256
    rows = service.report_rows(plan, (image,))
    output = html_report.write_html_report(rows, tmp_path / "marked.html")
    markup = output.read_text(encoding="utf-8")
    assert '<svg class="spacing-overlay"' not in markup
    assert 'class="spacing-marker"' not in markup
    assert '<ol class="spacing-issues">' not in markup
    assert (image.data, image.sha256) == before
    assert _html_images(output) == [image.data]


def _located_row() -> service.ReportRow:
    """Supply two distinct confirmed spans through the immutable report contract."""
    issues = []
    for index, (code, gap) in enumerate(
        (("too_tight", 100_000), ("too_wide", 250_000))
    ):
        left, right = (2 + index * 8) * 1_000_000, (3 + index * 8) * 1_000_000
        source = ((left, 1_000_000), (right, 1_000_000))
        mate = ((left, 1_400_000), (right, 1_400_000))
        issues.append(
            SpacingIssue(
                code,
                ("source", "mate"),
                source[0],
                NmBounds(gap, gap),
                "Confirmed routed interval.",
                SpacingLocation(
                    "source",
                    ("mate",),
                    (0.0, 1.0),
                    source,
                    mate,
                    (left, 1_000_000, right, 1_400_000),
                ),
            )
        )
    check = SpacingCheck(
        150_000,
        1_000,
        "deviation",
        "complete",
        NmBounds(100_000, 100_000),
        NmBounds(250_000, 250_000),
        20_000_000,
        0,
        tuple(issues),
        (),
    )
    image = service.CapturedImage.from_bytes(
        _png(800, 400), viewport_nm=(0, 0, 20_000_000, 10_000_000)
    )
    return _row(image=image, spacing_check=check)


def _diagnostic_image() -> Any:
    """Keep diagnostic capture ownership distinct from manufacturing rows."""
    from impedance.spacing_review import SpacingReviewEntry, SpacingReviewImage

    entry = SpacingReviewEntry(
        spec_id="usb",
        layer="F.Cu",
        issue_id="issue-1",
        view_id="view-1",
        parent_section_ids=("section-1",),
        pair=("USB_D+", "USB_D-"),
        issue_code="too_tight",
        caption="<Too tight> 0.1 mm; target 0.15 mm & inspect",
        bounds=(1_000_000, 1_000_000, 2_000_000, 1_300_000),
        viewport_nm=(500_000.0, 500_000.0, 2_000_000.0, 1_000_000.0),
        input_digest="a" * 64,
        source_traces=("source",),
        mate_traces=("mate",),
    )
    return SpacingReviewImage(
        entry,
        service.CapturedImage.from_bytes(
            _png(1200, 800, b"\x20\x90\x40"), viewport_nm=entry.viewport_nm
        ),
    )


def test_html_separate_spacing_cards_keep_caption_outside_exact_capture(
    tmp_path: Path,
) -> None:
    """Close-ups have their own cards and never annotate manufacturing copper."""
    from impedance.review_tracking import (
        ReviewTracking,
        SpacingImageReview,
        spacing_capture_fingerprint,
    )

    row = _located_row()
    diagnostic = _diagnostic_image()
    entry, capture = diagnostic.entry, diagnostic.capture
    digest = spacing_capture_fingerprint(entry.input_digest, capture.sha256)
    tracking = ReviewTracking(
        spacing_images=(
            SpacingImageReview(
                entry.spec_id,
                entry.layer,
                entry.issue_id,
                entry.view_id,
                entry.input_digest,
                digest,
                "2026-10-24T12:00:00.000000Z",
                digest,
                "2026-10-24T12:01:00.000000Z",
            ),
        )
    )
    output = html_report.write_html_report(
        (row,),
        tmp_path / "separate.html",
        spacing_reviews=(diagnostic,),
        review_tracking=tracking,
    )
    markup = output.read_text(encoding="utf-8")
    assert markup.count('class="spacing-review"') == 1
    assert "&lt;Too tight&gt; 0.1 mm; target 0.15 mm &amp; inspect" in markup
    assert "Approved" in markup and "2026-10-24 12:01:00 UTC" in markup
    assert "separate from the workbook" in markup
    assert '<svg class="spacing-overlay"' not in markup
    assert _html_images(output) == [row.image.data, capture.data]


@pytest.mark.parametrize("evidence", ["absent", "viewed", "old_pixels"])
def test_html_does_not_claim_unproven_spacing_approval(
    tmp_path: Path, evidence: str
) -> None:
    """A timestamp alone cannot imply approval of the close-up in the report."""
    from impedance.review_tracking import ReviewTracking, SpacingImageReview

    diagnostic = _diagnostic_image()
    entry = diagnostic.entry
    records = ()
    if evidence != "absent":
        records = (
            SpacingImageReview(
                entry.spec_id,
                entry.layer,
                entry.issue_id,
                entry.view_id,
                entry.input_digest,
                "b" * 64,
                "2026-10-24T12:00:00.000000Z",
                "b" * 64 if evidence == "old_pixels" else "",
                "2026-10-24T12:01:00.000000Z" if evidence == "old_pixels" else "",
            ),
        )
    markup = html_report.write_html_report(
        (_row(),),
        tmp_path / "unapproved.html",
        spacing_reviews=(diagnostic,),
        review_tracking=ReviewTracking(spacing_images=records),
    ).read_text(encoding="utf-8")
    assert "Not approved for this capture" in markup


def test_spacing_evidence_never_changes_xlsx_rows_drawings_or_png_bytes(
    tmp_path: Path,
) -> None:
    """Exact OOXML and media equality catches diagnostic contamination."""
    row = _located_row()
    plain = replace(row, spacing_check=None)
    baseline = workbook.write_workbook((plain,), tmp_path / "plain.xlsx")
    diagnostic = workbook.write_workbook((row,), tmp_path / "diagnostic.xlsx")
    with ZipFile(baseline) as before, ZipFile(diagnostic) as after:
        owned = tuple(
            name
            for name in before.namelist()
            if name.startswith(("xl/worksheets/", "xl/drawings/", "xl/media/"))
        )
        assert owned
        assert tuple(name for name in after.namelist() if name in owned) == owned
        assert {name: before.read(name) for name in owned} == {
            name: after.read(name) for name in owned
        }
        assert sum(name.startswith("xl/media/") for name in after.namelist()) == 1
        assert (
            next(after.read(name) for name in owned if name.startswith("xl/media/"))
            == row.image.data
        )
        for name in after.namelist():
            if name.endswith(".xml"):
                xml = after.read(name)
                assert (
                    b"Too tight" not in xml and b"Confirmed routed interval" not in xml
                )


@pytest.mark.parametrize("limit", ["count", "diagnostic_bytes", "combined_bytes"])
def test_html_diagnostic_resource_limits_preserve_previous_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    """Extra review captures cannot evade bounds or replace a previous report."""
    from impedance import spacing_review
    from impedance.model import ValidationError

    diagnostic = _diagnostic_image()
    row = _row()
    if limit == "count":
        monkeypatch.setattr(spacing_review, "MAX_SPACING_REVIEW_IMAGES", 0)
    elif limit == "diagnostic_bytes":
        monkeypatch.setattr(
            spacing_review, "MAX_SPACING_REVIEW_BYTES", len(diagnostic.capture.data) - 1
        )
    else:
        monkeypatch.setattr(
            service,
            "MAX_TOTAL_IMAGE_BYTES",
            len(row.image.data) + len(diagnostic.capture.data) - 1,
        )
    destination = tmp_path / "previous.html"
    destination.write_bytes(b"previous report")
    with pytest.raises(ValidationError, match="Spacing|combined"):
        html_report.write_html_report(
            (row,), destination, spacing_reviews=(diagnostic,)
        )
    assert destination.read_bytes() == b"previous report"
    assert tuple(tmp_path.iterdir()) == (destination,)


def test_html_never_invents_diagnostic_images_from_report_rows(tmp_path: Path) -> None:
    """Even located issues cannot turn a manufacturing capture into a close-up."""
    row = _located_row()
    output = html_report.write_html_report((row, row), tmp_path / "two-rows.html")
    markup = output.read_text(encoding="utf-8")
    assert 'class="spacing-review"' not in markup
    assert '<svg class="spacing-overlay"' not in markup
    assert "Numbered outlines" not in markup
    assert _html_images(output) == [row.image.data, row.image.data]


def test_html_keeps_issue_descriptions_without_inventing_missing_viewport(
    tmp_path: Path,
) -> None:
    """Manufacturing images remain original even without a physical transform."""
    row = _located_row()
    row = replace(row, image=service.CapturedImage.from_bytes(row.image.data))
    markup = html_report.write_html_report(
        (row,), tmp_path / "no-viewport.html"
    ).read_text(encoding="utf-8")
    assert '<svg class="spacing-overlay"' not in markup
    assert '<ol class="spacing-issues">' not in markup
    assert "select an issue below" not in markup


def test_html_does_not_box_unlocalized_or_unverified_spacing_evidence(
    tmp_path: Path,
) -> None:
    """Only confirmed physical spans, rather than extrema or reasons, get markers."""
    row = _located_row()
    assert row.spacing_check is not None
    check = replace(
        row.spacing_check,
        verdict="undetermined",
        coverage="partial",
        issues=(
            replace(row.spacing_check.issues[0], location=None),
            replace(row.spacing_check.issues[1], code="unverified"),
        ),
    )
    markup = html_report.write_html_report(
        (replace(row, spacing_check=check),), tmp_path / "no-false-box.html"
    ).read_text(encoding="utf-8")
    assert '<svg class="spacing-overlay"' not in markup
    assert '<ol class="spacing-issues">' not in markup


def _with_snapshot(
    plan: service.ExportPlan, traces: Sequence[Trace]
) -> service.ExportPlan:
    """Reanalyze changed geometry through the normal full-selection report path."""
    snapshot = replace(plan.snapshot, traces=tuple(traces))
    analysis = analyze(plan.config, snapshot)
    config = replace(
        plan.config,
        reviewed_digest=analysis.digest,
        included_section_ids=tuple(section.section_id for section in analysis.sections),
    )
    return service.ExportPlan(config, snapshot, analysis, analysis.sections)


@pytest.mark.parametrize("actual_gap_nm", [100_000, 150_000, 250_000])
def test_spacing_diagnostics_preserve_declared_vendor_spacing(
    tmp_path: Path, actual_gap_nm: int
) -> None:
    """A measured departure never overwrites the numeric fabrication gap cell."""
    plan = _spacing_plan(actual_gap_nm)
    before = plan.config.to_dict()
    rows = service.report_rows(plan, _images(plan))
    result = workbook.write_workbook(rows, tmp_path / "spacing.xlsx")
    document = load_workbook(result)
    try:
        assert document.active["F2"].value == pytest.approx(5.9055)
    finally:
        document.close()
    assert rows[0].spacing_nm == 150_000
    assert rows[0].spacing_check is not None
    assert rows[0].spacing_check.verdict == (
        "match" if actual_gap_nm == 150_000 else "deviation"
    )
    assert plan.config.to_dict() == before


def test_spacing_assessment_is_shared_once_across_report_rows_and_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Batch assessment uses the exact export snapshot once, never either writer."""
    plan = _spacing_plan(150_000)
    plan = _with_snapshot(
        plan,
        (
            *plan.snapshot.traces,
            Trace(
                "plus-wide",
                "F.Cu",
                "USB_D+",
                175_000,
                ((10_000_000, 0), (11_000_000, 0)),
            ),
            Trace(
                "minus-wide",
                "F.Cu",
                "USB_D-",
                175_000,
                ((10_000_000, 300_000), (11_000_000, 300_000)),
            ),
        ),
    )
    assert len(plan.sections) == 2
    original = spacing_checks.spacing_checks_for_sections
    calls: list[tuple[Any, Any, Any]] = []

    def assess(config: Any, snapshot: Any, sections: Any, **kwargs: Any) -> Any:
        """Record batch inputs while retaining the actual geometry implementation."""
        calls.append((config, snapshot, sections))
        return original(config, snapshot, sections, **kwargs)

    monkeypatch.setattr(spacing_checks, "spacing_checks_for_sections", assess)
    rows = service.report_rows(plan, _images(plan))
    assert len(calls) == 1
    assert calls[0][0] is plan.config
    assert calls[0][1] is plan.snapshot
    assert calls[0][2] is plan.sections
    assert all(row.spacing_check is not None for row in rows)
    html_report.write_html_report(rows, tmp_path / "spacing.html")
    workbook.write_workbook(rows, tmp_path / "spacing.xlsx")
    assert len(calls) == 1


def test_preflight_checks_required_spacing_images_from_complete_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publication consent includes diagnostic entries outside selected XLSX rows."""
    from impedance import spacing_review

    plan = _spacing_plan(150_000)
    original = spacing_review.spacing_review_entries
    seen = []

    def entries(config: Any, snapshot: Any, sections: Any, **kwargs: Any) -> Any:
        """Retain actual derivation and inspect the complete review scope."""
        seen.append(tuple(sections))
        return original(config, snapshot, sections, **kwargs)

    monkeypatch.setattr(spacing_review, "spacing_review_entries", entries)
    assert service.prepare(plan.config, plan.snapshot) is not None
    service.validate_current(plan, plan.config, plan.snapshot)
    assert seen and all(sections == plan.analysis.sections for sections in seen)


def test_report_keeps_definite_violation_with_unverified_native_arc(
    tmp_path: Path,
) -> None:
    """Unresolved geometry remains explicit without concealing a proven tight gap."""
    plan = _spacing_plan(100_000)
    plan = _with_snapshot(
        plan,
        (
            *plan.snapshot.traces,
            Trace(
                "unsupported-arc",
                "F.Cu",
                "USB_D+",
                150_000,
                ((20_000_000, 0), (20_500_000, 500_000), (21_000_000, 0)),
                is_native_arc=True,
            ),
        ),
    )
    rows = service.report_rows(plan, _images(plan))
    assert rows[0].spacing_check is not None
    assert rows[0].spacing_check.verdict == "deviation"
    assert rows[0].spacing_check.coverage == "partial"
    markup = html_report.write_html_report(rows, tmp_path / "partial.html").read_text(
        encoding="utf-8"
    )
    assert "Deviation" in markup
    assert "partial" in markup.casefold()
    assert "unverified" in markup.casefold()


def test_html_escapes_spacing_limitations_and_keeps_partial_verdict(
    tmp_path: Path,
) -> None:
    """Diagnostic text is untrusted display data even when geometry is derived."""
    check = SpacingCheck(
        declared_spacing_nm=150_000,
        comparison_band_nm=1_000,
        verdict="deviation",
        coverage="partial",
        minimum_gap_nm=NmBounds(100_000, 100_000),
        maximum_gap_nm=NmBounds(150_000, 150_000),
        assessed_length_nm=20_000_000,
        unverified_length_nm=1_000_000,
        issues=(),
        reasons=('<script>alert("spacing")</script> & unresolved',),
    )
    markup = html_report.write_html_report(
        (_row(spacing_check=check),), tmp_path / "escaped.html"
    ).read_text(encoding="utf-8")
    assert "<script>" not in markup
    assert "&lt;script&gt;" in markup
    assert "&amp; unresolved" in markup
    assert "Deviation" in markup
    assert "partial" in markup.casefold()
    assert "0.1" in markup
    assert "0.15" in markup


@pytest.mark.parametrize("coverage", ["partial", "none"])
def test_matching_assessed_subset_is_reported_as_unverified(
    tmp_path: Path, coverage: Literal["partial", "none"]
) -> None:
    """Rendered matching dimensions cannot overstate incomplete route coverage."""
    check = SpacingCheck(
        declared_spacing_nm=150_000,
        comparison_band_nm=1_000,
        verdict="undetermined",
        coverage=coverage,
        minimum_gap_nm=NmBounds(150_000, 150_000) if coverage == "partial" else None,
        maximum_gap_nm=NmBounds(150_000, 150_000) if coverage == "partial" else None,
        assessed_length_nm=20_000_000 if coverage == "partial" else 0,
        unverified_length_nm=1_000_000,
        issues=(),
        reasons=("Unresolved arc",),
    )
    markup = html_report.write_html_report(
        (_row(spacing_check=check),), tmp_path / "unverified.html"
    ).read_text(encoding="utf-8")
    status = re.search(r"Pair spacing</dt><dd[^>]*>([^<]*)", markup)
    assert status is not None
    assert "Unverified" in status.group(1)
    assert "match" not in status.group(1).casefold()


def test_spacing_reports_work_offline_without_stackup_or_width_results(
    tmp_path: Path,
) -> None:
    """Missing calculator prerequisites do not hide known geometric departures."""
    plan = _spacing_plan(100_000)
    plan = replace(plan, config=replace(plan.config, stackup=None, width_results=()))
    plan = _with_snapshot(plan, plan.snapshot.traces)
    rows = service.report_rows(plan, _images(plan))
    assert rows[0].width_check is not None
    assert rows[0].width_check.status != "matches_nominal"
    assert rows[0].spacing_check is not None
    assert rows[0].spacing_check.verdict == "deviation"
    markup = html_report.write_html_report(rows, tmp_path / "offline.html").read_text(
        encoding="utf-8"
    )
    assert "Deviation" in markup


def test_saved_calculator_assumptions_remain_historical_evidence(
    tmp_path: Path,
) -> None:
    """New diagnostic copy never rewrites assumptions retained in saved results."""
    plan = _spacing_plan(100_000)
    historical = "Declared spacing is not verified by this saved width calculation."
    config = replace(
        plan.config,
        width_results=(
            replace(plan.config.width_results[0], assumptions=(historical,)),
        ),
    )
    plan = replace(plan, config=config)
    before = config.to_dict()
    rows = service.report_rows(plan, _images(plan))
    markup = html_report.write_html_report(
        rows, tmp_path / "historical.html"
    ).read_text(encoding="utf-8")
    assert historical in markup
    assert "Deviation" in markup
    assert config.to_dict() == before


def test_coplanar_pair_checks_spacing_and_preserves_both_vendor_gaps(
    tmp_path: Path,
) -> None:
    """The pair diagnostic cannot replace either declared coplanar dimension."""
    plan = _spacing_plan(100_000)
    spec = plan.config.specifications[0]
    spec = replace(
        spec,
        kind="differential_coplanar",
        layer_settings=(replace(spec.layer_settings[0], ground_gap_nm=254_000),),
    )
    plan = replace(plan, config=replace(plan.config, specifications=(spec,)))
    plan = _with_snapshot(plan, plan.snapshot.traces)
    rows = service.report_rows(plan, _images(plan))
    assert rows[0].spacing_check is not None
    assert rows[0].spacing_check.verdict == "deviation"
    result = workbook.write_workbook(rows, tmp_path / "coplanar.xlsx")
    document = load_workbook(result)
    try:
        assert document.active["F2"].value == "Pair: 5.9055\nGround: 10"
    finally:
        document.close()
    markup = html_report.write_html_report(rows, tmp_path / "coplanar.html").read_text(
        encoding="utf-8"
    )
    assert "Declared pair spacing" in markup
    assert "Coplanar ground gap" in markup
    assert "Deviation" in markup


def test_single_ended_report_has_no_differential_spacing_diagnostic(
    tmp_path: Path,
) -> None:
    """Inapplicable checks do not turn single-ended rows into unresolved pairs."""
    row = _row(kind="single_ended", spacing_nm=None, ground_gap_nm=None)
    markup = html_report.write_html_report((row,), tmp_path / "single.html").read_text(
        encoding="utf-8"
    )
    assert "Pair spacing" not in markup
    assert "Declared pair spacing" not in markup
