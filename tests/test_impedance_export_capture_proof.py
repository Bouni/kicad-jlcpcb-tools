"""Require reviewed pixels before either report can reach publication."""

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional
from zipfile import ZipFile

import pytest

from impedance import (
    capture_cache,
    html_report,
    matching,
    review_tracking,
    service,
    stackup_model,
    workbook,
)
from impedance.model import Section, ValidationError
from impedance.review_tracking import ImageView, LayerApproval, ReviewTracking
from tests.test_impedance_report_pipeline import _html_images, _png, _spacing_plan
from tests.test_impedance_service import _board, _configuration, _review

_TIME = "2026-10-20T12:13:14.000000Z"


def _approved_spacing_plan(*, distinct_concerns: bool = False) -> service.ExportPlan:
    """Record explicit per-image approval for every physical spacing concern."""
    from impedance.spacing_review import spacing_review_entries

    plan = _spacing_plan(100_000)
    if distinct_concerns:
        snapshot = replace(
            plan.snapshot,
            traces=(
                *plan.snapshot.traces,
                *(
                    replace(
                        trace,
                        trace_id=f"separate-{trace.trace_id}",
                        points=tuple(
                            (x + 30_000_000, y + 2_000_000) for x, y in trace.points
                        ),
                    )
                    for trace in plan.snapshot.traces
                ),
            ),
        )
        analysis = matching.analyze(plan.config, snapshot)
        config = replace(
            plan.config,
            reviewed_digest=analysis.digest,
            included_section_ids=tuple(
                section.section_id for section in analysis.sections
            ),
        )
        plan = service.ExportPlan(config, snapshot, analysis, analysis.sections)
    tracking = plan.config.review_tracking
    for entry in spacing_review_entries(
        plan.config, plan.snapshot, plan.analysis.sections
    ):
        digest = review_tracking.spacing_capture_fingerprint(
            entry.input_digest,
            service.CapturedImage.from_bytes(
                _png(entry.width_px, entry.height_px, b"\x20\x90\x40")
            ).sha256,
        )
        tracking = review_tracking.put_spacing_image(
            tracking,
            review_tracking.SpacingImageReview(
                entry.spec_id,
                entry.layer,
                entry.issue_id,
                entry.view_id,
                entry.input_digest,
                digest,
                _TIME,
                digest,
                _TIME,
            ),
        )
    return replace(plan, config=replace(plan.config, review_tracking=tracking))


@pytest.mark.parametrize(
    "evidence", ["absent", "viewed", "stale", "wrong_issue", "one_pending"]
)
def test_prepare_requires_explicit_current_approval_of_every_spacing_image(
    evidence: str,
) -> None:
    """Global workbook consent and exact viewing do not approve any diagnostic."""
    plan = _approved_spacing_plan(distinct_concerns=True)
    history = plan.config.review_tracking
    assert len(history.spacing_images) == 2
    assert len({record.issue_id for record in history.spacing_images}) == 2
    if evidence == "absent":
        records = ()
    elif evidence == "viewed":
        records = tuple(
            replace(record, approved_digest="", approved_at_utc="")
            for record in history.spacing_images
        )
    elif evidence == "stale":
        records = tuple(
            replace(record, input_digest="0" * 64) for record in history.spacing_images
        )
    elif evidence == "wrong_issue":
        records = tuple(
            replace(record, issue_id="unrelated") for record in history.spacing_images
        )
    else:
        records = history.spacing_images[:-1]
    config = replace(
        plan.config, review_tracking=replace(history, spacing_images=records)
    )
    before = config.to_dict()
    with pytest.raises(ValidationError, match="Spacing.*individual approval"):
        service.prepare(config, plan.snapshot)
    assert config.to_dict() == before
    assert service.prepare(plan.config, plan.snapshot) is not None


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

    def viewport(
        self, section: Section, width_px: int, height_px: int
    ) -> tuple[float, float, float, float]:
        """Preserve the real physical frame on deterministic test PNGs."""
        from impedance.render import section_viewport

        frame = section_viewport(section, width_px, height_px, None)
        return frame.left, frame.top, frame.width, frame.height


class _SpacingRenderer(_Renderer):
    """Keep diagnostic entry rendering separate from manufacturing section calls."""

    def __init__(self, failure: str = "") -> None:
        super().__init__(_png())
        self.failure = failure
        self.spacing: list[Any] = []

    def render_spacing(
        self, entry: Any, destination: Path, width_px: int, height_px: int
    ) -> Path:
        """Return actual image bytes so validation checks the captured pixels."""
        self.spacing.append(entry)
        if self.failure == "render":
            raise RuntimeError("spacing render failed")
        if self.failure == "path":
            return destination.with_name("unexpected.png")
        if self.failure == "dimensions":
            width_px //= 2
        color = b"\xff\x00\x00" if self.failure == "pixels" else b"\x20\x90\x40"
        destination.write_bytes(_png(width_px, height_px, color))
        return destination


class _OriginalSpacingRenderer(_SpacingRenderer):
    """Provide independently rendered board context for the comparison action."""

    def __init__(self, original_failure: str = "") -> None:
        super().__init__()
        self.original_failure = original_failure
        self.originals: list[bytes] = []

    def render_spacing_original(
        self,
        entry: Any,
        destination: Path,
        width_px: Optional[int] = None,
        height_px: Optional[int] = None,
    ) -> Path:
        """Keep original pixels and failures independent of composite rendering."""
        composition = getattr(entry, "composition", None)
        expected = (
            composition.detail_rect[2:]
            if composition is not None
            else (entry.width_px, entry.height_px)
        )
        width_px = expected[0] if width_px is None else width_px
        height_px = expected[1] if height_px is None else height_px
        if self.original_failure == "render":
            raise RuntimeError("original render failed")
        if self.original_failure == "path":
            return destination.with_name("unexpected-original.png")
        if self.original_failure == "dimensions":
            width_px //= 2
        data = (
            b"not a PNG"
            if self.original_failure == "png"
            else _png(width_px, height_px, b"\x90\x20\x40")
        )
        self.originals.append(data)
        destination.write_bytes(data)
        return destination


def test_full_export_keeps_combined_and_original_out_of_excel(
    tmp_path: Path,
) -> None:
    """Real HTML/OOXML writers preserve approval evidence and manufacturing media."""
    from impedance.spacing_review import spacing_review_entries

    plan = _approved_spacing_plan()
    before = plan.config.to_dict()
    entries = spacing_review_entries(plan.config, plan.snapshot, plan.analysis.sections)
    renderer = _OriginalSpacingRenderer()
    result = service.export_reports(
        plan,
        object(),
        object(),
        tmp_path,
        renderer_factory=lambda _board, _pcbnew: renderer,
    )
    manufacturing = _png()
    composite = _png(entries[0].width_px, entries[0].height_px, b"\x20\x90\x40")
    assert renderer.closed and len(renderer.originals) == len(entries)
    assert _html_images(result.html_report) == [
        *(data for original in renderer.originals for data in (composite, original)),
        *([manufacturing] * len(plan.sections)),
    ]
    markup = result.html_report.read_text(encoding="utf-8")
    assert markup.count('class="spacing-review"') == len(entries)
    assert markup.count("Original image") == len(entries)
    assert "Not approved for this capture" not in markup
    baseline = workbook.write_workbook(
        service.report_rows(plan, _images_for_plan(plan)), tmp_path / "baseline.xlsx"
    )
    with ZipFile(baseline) as expected, ZipFile(result.workbook) as actual:
        owned = tuple(
            name
            for name in expected.namelist()
            if name.startswith(("xl/worksheets/", "xl/drawings/", "xl/media/"))
        )
        assert {name: actual.read(name) for name in owned} == {
            name: expected.read(name) for name in owned
        }
        media = tuple(
            actual.read(name)
            for name in actual.namelist()
            if name.startswith("xl/media/")
        )
        assert len(media) == len(plan.sections) and set(media) == {manufacturing}
    assert plan.config.to_dict() == before


def _images_for_plan(plan: service.ExportPlan) -> tuple[service.CapturedImage, ...]:
    """Build unchanged original manufacturing captures for the independent baseline."""
    return tuple(service.CapturedImage.from_bytes(_png()) for _ in plan.sections)


def _owned_options(
    plan: service.ExportPlan, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """Use real immutable ownership plus independently reread live provenance."""
    monkeypatch.setattr(service, "IMAGE_WIDTH", 120)
    monkeypatch.setattr(service, "IMAGE_HEIGHT", 60)
    return {
        "capture_cache": capture_cache.OwnedCaptureCache(),
        "capture_identity": ("/board-a.kicad_pcb", None),
        "appearance_context": lambda: "current-theme",
        "source_context": lambda: plan.snapshot,
    }


def test_repeated_export_reuses_exact_owned_pngs_without_constructing_renderer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeated publication attempt must not replot already verified pixels."""
    plan = _approved_spacing_plan()
    options = _owned_options(plan, monkeypatch)
    cold = _OriginalSpacingRenderer()
    cold.data = _png(120, 60)
    first = service.export_reports(
        plan,
        object(),
        object(),
        tmp_path,
        renderer_factory=lambda _board, _pcbnew: cold,
        **options,
    )
    expected_html = first.html_report.read_bytes()
    expected_images = _html_images(first.html_report)

    def no_renderer(_board: Any, _pcbnew: Any) -> Any:
        raise AssertionError("Warm export unnecessarily created a renderer")

    second = service.export_reports(
        plan,
        object(),
        object(),
        tmp_path,
        renderer_factory=no_renderer,
        **options,
    )
    assert cold.closed and cold.sections == list(plan.sections)
    assert len(cold.originals) == len(cold.spacing) > 0
    assert second.html_report.read_bytes() == expected_html
    assert _html_images(second.html_report) == expected_images


def _warm_spacing_export(
    plan: service.ExportPlan, directory: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[
    dict[str, Any], Any, capture_cache.CaptureKey, service.CapturedImage, dict[str, Any]
]:
    """Prime actual export ownership, preserving independent live boundary callbacks."""
    from impedance.spacing_review import spacing_review_entries

    options = _owned_options(plan, monkeypatch)
    live = {
        "source": plan.snapshot,
        "appearance": "current-theme",
        "source_reads": 0,
        "appearance_reads": 0,
    }

    def read_source() -> Any:
        """Read the current mutable source at every real consumer boundary."""
        live["source_reads"] += 1
        return live["source"]

    def read_appearance() -> object:
        """Read mutable appearance rather than returning a recorded renderer value."""
        live["appearance_reads"] += 1
        return live["appearance"]

    options["source_context"] = read_source
    options["appearance_context"] = read_appearance
    renderer = _OriginalSpacingRenderer()
    renderer.data = _png(120, 60)
    service.export_reports(
        plan,
        object(),
        object(),
        directory,
        renderer_factory=lambda _board, _pcbnew: renderer,
        **options,
    )
    assert renderer.closed
    entries = spacing_review_entries(plan.config, plan.snapshot, plan.analysis.sections)
    assert len(entries) == 1
    entry = entries[0]
    key = capture_cache.capture_key(
        options["capture_cache"],
        options["capture_identity"][0],
        plan.snapshot,
        "current-theme",
        None,
        entry,
        dimensions=(entry.width_px, entry.height_px),
    )
    image = options["capture_cache"].get(key)
    assert image is not None and image.original is not None
    assert live["source_reads"] == live["appearance_reads"] == 2
    live["source_reads"] = live["appearance_reads"] = 0
    return options, entry, key, image, live


@pytest.mark.parametrize(
    "fault",
    [
        "missing_original",
        "corrupt_original",
        "evicted",
        "dimensions",
        "viewport",
        "policy",
    ],
)
@pytest.mark.parametrize("failure", [False, True])
def test_deferred_export_handles_missing_or_invalid_owned_comparisons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str, failure: bool
) -> None:
    """The real consumer rerenders lost proof, or stops both writers on fallback failure."""
    plan = _approved_spacing_plan()
    before = plan.config.to_dict()
    options, entry, key, image, live = _warm_spacing_export(plan, tmp_path, monkeypatch)
    cache = options["capture_cache"]
    expected_html_images = _html_images(tmp_path / html_report.OUTPUT_FILENAME)
    assert image.original is not None
    if fault == "missing_original":
        cache.put(key, service.CapturedImage.from_bytes(image.data))
    elif fault == "corrupt_original":
        owned = cache._images[key]
        assert owned.original is not None
        cache._images[key] = replace(
            owned, original=replace(owned.original, data=b"corrupt PNG")
        )
    elif fault == "evicted":
        cache.max_entries = len(cache._images)
        for other in tuple(cache._images):
            if other != key:
                assert cache.get(other) is not None
        cache.put(replace(key, board_identity="/unrelated.kicad_pcb"), image)
        assert key not in cache._images
    elif fault in {"dimensions", "viewport"}:
        width, height = entry.composition.detail_rect[2:]
        viewport = entry.viewport_nm
        if fault == "dimensions":
            width += 1
        else:
            viewport = (viewport[0] + 1, *viewport[1:])
        original = service.CapturedImage.from_bytes(
            _png(width, height, b"\x90\x20\x40"), viewport_nm=viewport
        )
        cache.put(key, service.CapturedImage.from_bytes(image.data, original=original))
    else:
        cache.discard(key)
        check, render = key.spacing_policy
        cache.put(replace(key, spacing_policy=(check, render + 1)), image)
    renderer = _OriginalSpacingRenderer("render" if failure else "")
    renderer.data = _png(120, 60)
    outputs = (
        tmp_path / workbook.OUTPUT_FILENAME,
        tmp_path / html_report.OUTPUT_FILENAME,
    )
    archive = tmp_path / "previous-fabrication.zip"
    archive.write_bytes(b"previous published archive")
    for output in outputs:
        output.write_bytes(b"previous published report")
    if failure:
        seen, writer = _writers(monkeypatch)
        with pytest.raises(RuntimeError, match="original render failed"):
            service.export_reports(
                plan,
                object(),
                object(),
                tmp_path,
                renderer_factory=lambda _board, _pcbnew: renderer,
                writer=writer,
                **options,
            )
        assert seen == []
        assert all(
            output.read_bytes() == b"previous published report" for output in outputs
        )
    else:
        result = service.export_reports(
            plan,
            object(),
            object(),
            tmp_path,
            renderer_factory=lambda _board, _pcbnew: renderer,
            **options,
        )
        assert _html_images(result.html_report) == expected_html_images
        assert len(renderer.originals) == 1
        with ZipFile(result.workbook) as workbook_zip:
            assert {
                workbook_zip.read(name)
                for name in workbook_zip.namelist()
                if name.startswith("xl/media/")
            } == {_png(120, 60)}
    assert renderer.closed and renderer.sections == []
    assert renderer.spacing == ([] if fault == "missing_original" else [entry])
    assert archive.read_bytes() == b"previous published archive"
    assert not tuple(tmp_path.glob(".impedance-spacing-*"))
    assert plan.config.to_dict() == before
    assert live["source_reads"] == live["appearance_reads"] == (1 if failure else 2)


@pytest.mark.parametrize("bound", ["diagnostic", "combined"])
def test_deferred_original_enforces_total_bytes_before_either_report_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bound: str
) -> None:
    """Original attachment cannot bypass diagnostic or manufacturing-plus-diagnostic limits."""
    from impedance import spacing_review

    plan = _approved_spacing_plan()
    before = plan.config.to_dict()
    options, entry, key, image, live = _warm_spacing_export(plan, tmp_path, monkeypatch)
    assert image.original is not None
    options["capture_cache"].put(key, service.CapturedImage.from_bytes(image.data))
    manufacturing_bytes = len(plan.sections) * len(_png(120, 60))
    if bound == "diagnostic":
        limit = image.byte_size - 1
        assert len(image.data) <= limit
        monkeypatch.setattr(spacing_review, "MAX_SPACING_REVIEW_BYTES", limit)
    else:
        limit = manufacturing_bytes + image.byte_size - 1
        assert manufacturing_bytes + len(image.data) <= limit
        monkeypatch.setattr(service, "MAX_TOTAL_IMAGE_BYTES", limit)
    renderer = _OriginalSpacingRenderer()
    renderer.data = _png(120, 60)
    seen, writer = _writers(monkeypatch)
    outputs = (
        tmp_path / workbook.OUTPUT_FILENAME,
        tmp_path / html_report.OUTPUT_FILENAME,
    )
    archive = tmp_path / "previous-fabrication.zip"
    archive.write_bytes(b"previous archive")
    for output in outputs:
        output.write_bytes(b"previous report")
    with pytest.raises(ValidationError, match="128 MiB"):
        service.export_reports(
            plan,
            object(),
            object(),
            tmp_path,
            renderer_factory=lambda _board, _pcbnew: renderer,
            writer=writer,
            **options,
        )
    assert renderer.closed and renderer.sections == renderer.spacing == []
    assert len(renderer.originals) == 1 and seen == []
    assert all(output.read_bytes() == b"previous report" for output in outputs)
    assert archive.read_bytes() == b"previous archive"
    assert not tuple(tmp_path.glob(".impedance-spacing-*"))
    assert plan.config.to_dict() == before
    assert live["source_reads"] == live["appearance_reads"] == 1


@pytest.mark.parametrize("budget", ["individual", "aggregate"])
def test_deferred_export_remains_valid_when_ownership_budget_skips_or_evicts_pngs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, budget: str
) -> None:
    """Memory limits include Originals and change reuse, never report correctness or consent."""
    plan = _approved_spacing_plan()
    before = plan.config.to_dict()
    options, entry, key, image, live = _warm_spacing_export(plan, tmp_path, monkeypatch)
    expected = _html_images(tmp_path / html_report.OUTPUT_FILENAME)
    cache = options["capture_cache"]
    cache.max_bytes = (
        image.byte_size - 1 if budget == "individual" else cache.byte_size - 1
    )
    cache.put(key, service.CapturedImage.from_bytes(image.data))
    renderer = _OriginalSpacingRenderer()
    renderer.data = _png(120, 60)
    result = service.export_reports(
        plan,
        object(),
        object(),
        tmp_path,
        renderer_factory=lambda _board, _pcbnew: renderer,
        **options,
    )
    assert renderer.closed and len(renderer.originals) == 1
    assert _html_images(result.html_report) == expected
    assert cache.byte_size <= cache.max_bytes
    if budget == "individual":
        assert key not in cache._images
    else:
        assert key in cache._images
        assert len(cache._images) < len(plan.sections) + 1
    assert plan.config.to_dict() == before
    assert live["source_reads"] == live["appearance_reads"] == 2
    assert not tuple(tmp_path.glob(".impedance-spacing-*"))


@pytest.mark.parametrize("failure", ["render", "path", "png", "dimensions"])
def test_deferred_original_failure_preserves_reports_when_combined_png_is_warm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Moving Original work out of review retains all original export safeguards."""
    from impedance.spacing_review import spacing_review_entries

    plan = _approved_spacing_plan()
    options = _owned_options(plan, monkeypatch)
    cache = options["capture_cache"]
    for entry in spacing_review_entries(
        plan.config, plan.snapshot, plan.analysis.sections
    ):
        key = capture_cache.capture_key(
            cache,
            options["capture_identity"][0],
            plan.snapshot,
            "current-theme",
            None,
            entry,
            dimensions=(entry.width_px, entry.height_px),
        )
        cache.put(
            key,
            service.CapturedImage.from_bytes(
                _png(entry.width_px, entry.height_px, b"\x20\x90\x40")
            ),
        )
    renderer = _OriginalSpacingRenderer(failure)
    seen, writer = _writers(monkeypatch)
    outputs = (
        tmp_path / workbook.OUTPUT_FILENAME,
        tmp_path / html_report.OUTPUT_FILENAME,
    )
    for output in outputs:
        output.write_bytes(b"previous report")
    with pytest.raises(RuntimeError if failure == "render" else ValidationError):
        service.export_reports(
            plan,
            object(),
            object(),
            tmp_path,
            renderer_factory=lambda _board, _pcbnew: renderer,
            writer=writer,
            **options,
        )
    assert renderer.closed and renderer.spacing == [] and seen == []
    assert all(output.read_bytes() == b"previous report" for output in outputs)
    assert not tuple(tmp_path.glob(".impedance-spacing-*"))


@pytest.mark.parametrize("change", ["source", "appearance"])
def test_deferred_original_revision_change_invalidates_all_export_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """The last deferred pixel render remains inside fresh provenance boundaries."""
    plan = _approved_spacing_plan()
    options = _owned_options(plan, monkeypatch)
    live = {"source": plan.snapshot, "appearance": "current-theme"}
    options["source_context"] = lambda: live["source"]
    options["appearance_context"] = lambda: live["appearance"]
    renderer = _OriginalSpacingRenderer()
    render_original = renderer.render_spacing_original

    def changed_original(*args: Any, **kwargs: Any) -> Path:
        result = render_original(*args, **kwargs)
        live[change] = (
            replace(plan.snapshot, context_digest="changed")
            if change == "source"
            else "changed-theme"
        )
        return result

    renderer.render_spacing_original = changed_original
    seen, writer = _writers(monkeypatch)
    with pytest.raises(ValidationError, match="changed"):
        service.export_reports(
            plan,
            object(),
            object(),
            tmp_path,
            renderer_factory=lambda _board, _pcbnew: renderer,
            writer=writer,
            **options,
        )
    assert renderer.closed and seen == []
    assert not options["capture_cache"]._images


@pytest.mark.parametrize("failure", ["render", "path", "png", "dimensions"])
def test_original_export_failure_preserves_both_previous_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Partial capture evidence never reaches either publication consumer."""
    plan = _approved_spacing_plan()
    before = plan.config.to_dict()
    renderer = _OriginalSpacingRenderer(failure)
    seen, writer = _writers(monkeypatch)
    outputs = (
        tmp_path / workbook.OUTPUT_FILENAME,
        tmp_path / html_report.OUTPUT_FILENAME,
    )
    for output in outputs:
        output.write_bytes(b"previous report")
    with pytest.raises(RuntimeError if failure == "render" else ValidationError):
        service.export_reports(
            plan,
            object(),
            object(),
            tmp_path,
            renderer_factory=lambda _board, _pcbnew: renderer,
            writer=writer,
        )
    assert renderer.closed and seen == []
    assert all(output.read_bytes() == b"previous report" for output in outputs)
    assert not tuple(tmp_path.glob(".impedance-spacing-*"))
    assert plan.config.to_dict() == before


@pytest.mark.parametrize("failure", ["pixels", "dimensions", "path", "render", "api"])
def test_spacing_capture_failures_stop_both_writers_and_keep_previous_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Saved approval cannot authorize changed diagnostics or a missing renderer."""
    plan = _approved_spacing_plan()
    before = plan.config.to_dict()
    renderer = _Renderer(_png()) if failure == "api" else _SpacingRenderer(failure)
    seen, writer = _writers(monkeypatch)
    outputs = (
        tmp_path / workbook.OUTPUT_FILENAME,
        tmp_path / html_report.OUTPUT_FILENAME,
    )
    for output in outputs:
        output.write_bytes(b"previous report")
    error = RuntimeError if failure == "render" else ValidationError
    with pytest.raises(error):
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
    assert not tuple(tmp_path.glob(".impedance-spacing-*"))
    assert plan.config.to_dict() == before


def test_export_keeps_spacing_images_out_of_canonical_workbook_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both writers share manufacturing rows; only HTML receives extra close-ups."""
    from impedance.spacing_review import SpacingReviewImage, spacing_review_entries

    plan = _approved_spacing_plan()
    expected_entries = spacing_review_entries(
        plan.config, plan.snapshot, plan.analysis.sections
    )
    renderer = _SpacingRenderer()
    workbook_rows: list[Any] = []
    html_options: dict[str, Any] = {}
    before = plan.config.to_dict()

    def write_xlsx(rows: Any, destination: Path) -> Path:
        """Check that the workbook receives its unchanged immutable row contract."""
        workbook_rows.append(rows)
        destination.write_bytes(b"workbook")
        return destination

    def write_html(rows: Any, destination: Path, **options: Any) -> Path:
        """Keep diagnostics in their dedicated companion-only argument."""
        assert rows is workbook_rows[0]
        html_options.update(options)
        destination.write_bytes(b"html")
        return destination

    monkeypatch.setattr(html_report, "write_html_report", write_html)
    service.export_reports(
        plan,
        object(),
        object(),
        tmp_path,
        renderer_factory=lambda _board, _pcbnew: renderer,
        writer=write_xlsx,
    )
    assert renderer.closed and renderer.sections == list(plan.sections)
    assert renderer.spacing == list(expected_entries)
    assert len(workbook_rows[0]) == len(plan.sections)
    assert all(isinstance(row, service.ReportRow) for row in workbook_rows[0])
    assert all(row.image.data == _png() for row in workbook_rows[0])
    diagnostics = html_options["spacing_reviews"]
    assert all(isinstance(image, SpacingReviewImage) for image in diagnostics)
    assert tuple(image.entry for image in diagnostics) == expected_entries
    assert all(image.capture.viewport_nm is None for image in diagnostics)
    assert html_options["review_tracking"] == plan.config.review_tracking
    assert plan.config.to_dict() == before
    assert not tuple(tmp_path.glob(".impedance-spacing-*"))


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
