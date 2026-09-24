"""Export approved workbook pixels independently of historical image-view events."""

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest

from impedance import html_report, review_tracking, service, workbook
from impedance.model import Config, ValidationError
from tests.test_impedance_export_capture_proof import _plan, _Renderer
from tests.test_impedance_report_pipeline import _html_images, _png


@pytest.mark.parametrize(
    ("has_baseline", "fresh_pixels", "accepted"),
    [
        (True, True, True),
        (True, False, False),
        (False, False, True),
        (False, True, False),
    ],
    ids=(
        "current-workbook-supersedes-historical-view",
        "historical-match-cannot-override-current-workbook",
        "legacy-consent-keeps-historical-pixels",
        "legacy-consent-rejects-unreviewed-pixels",
    ),
)
def test_export_uses_current_workbook_capture_baseline_and_preserves_view_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    has_baseline: bool,
    fresh_pixels: bool,
    accepted: bool,
) -> None:
    """The saved current baseline controls both writers without rewriting audit times."""
    original = _plan(layer_history=False)
    historical = _png()
    fresh = _png(color=b"\x10\x20\x30")
    source = review_tracking.source_fingerprint(original.snapshot)
    specs = {spec.spec_id: spec for spec in original.config.specifications}
    fresh_sha = service.CapturedImage.from_bytes(fresh).sha256
    baseline = tuple(
        (
            section.section_id,
            review_tracking.capture_fingerprint(
                source, specs[section.spec_id], section, fresh_sha
            ),
        )
        for section in original.sections
    )
    assert all(
        record.capture_digest != dict(baseline)[record.section_id]
        for record in original.config.review_tracking.images
    )
    saved = replace(original.config, reviewed_captures=baseline if has_baseline else ())
    config = Config.from_dict(saved.to_dict())
    plan = service.prepare(config, original.snapshot)
    assert plan is not None
    before = config.to_dict()
    history = config.review_tracking
    pixels = fresh if fresh_pixels else historical
    renderer = _Renderer(pixels)
    written: list[Sequence[service.ReportRow]] = []
    write_html = html_report.write_html_report

    def write_workbook(rows: Sequence[service.ReportRow], destination: Path) -> Path:
        """Observe publication while retaining the real vendor workbook writer."""
        written.append(rows)
        return workbook.write_workbook(rows, destination)

    def write_report(
        rows: Sequence[service.ReportRow], destination: Path, **options: Any
    ) -> Path:
        """Observe the second consumer while retaining the real HTML writer."""
        written.append(rows)
        return write_html(rows, destination, **options)

    monkeypatch.setattr(html_report, "write_html_report", write_report)
    outputs = (
        tmp_path / workbook.OUTPUT_FILENAME,
        tmp_path / html_report.OUTPUT_FILENAME,
    )
    for output in outputs:
        output.write_bytes(b"previous report")

    def export() -> service.ExportArtifacts:
        """Run the actual export validation, capture batch and publication path."""
        return service.export_reports(
            plan,
            object(),
            object(),
            tmp_path,
            renderer_factory=lambda _board, _pcbnew: renderer,
            writer=write_workbook,
        )

    if accepted:
        artifacts = export()
        assert len(written) == 2 and written[0] is written[1]
        assert all(row.last_viewed_current == (not fresh_pixels) for row in written[0])
        assert [row.last_viewed_at_utc for row in written[0]] == [
            record.viewed_at_utc for record in history.images
        ]
        assert _html_images(artifacts.html_report) == [pixels] * len(plan.sections)
        with ZipFile(artifacts.workbook) as archive:
            images = [
                name for name in archive.namelist() if name.startswith("xl/media/")
            ]
            assert len(images) == len(plan.sections)
            assert all(archive.read(name) == pixels for name in images)
    else:
        with pytest.raises(ValidationError, match="image changed since review"):
            export()
        assert written == []
        assert all(output.read_bytes() == b"previous report" for output in outputs)
    assert renderer.closed and renderer.sections == list(plan.sections)
    assert config.review_tracking == history == original.config.review_tracking
    assert config.to_dict() == before
