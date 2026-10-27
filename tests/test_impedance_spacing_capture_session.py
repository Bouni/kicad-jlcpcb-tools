"""Keep native diagnostic capture work out of manufacturing image evidence."""

from dataclasses import replace
import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from .test_impedance_capture_evidence import OTHER_PNG, _session
from .test_impedance_controls import PNG, harness
from .test_impedance_report_pipeline import _png

__all__ = ["harness"]


def _entry(api: SimpleNamespace, section: Any) -> Any:
    """Use a real immutable entry with small PNGs at the capture-cache boundary."""
    module = importlib.import_module(api.integration.__package__ + ".spacing_review")
    detail_width, detail_height = module.DETAIL_RECT[2:]
    return module.SpacingReviewEntry(
        spec_id=section.spec_id,
        layer=section.layer,
        issue_id="concern-a",
        view_id="concern-a-view-1",
        parent_section_ids=(section.section_id,),
        pair=("P", "N"),
        issue_code="too_wide",
        caption="Spacing concern",
        bounds=section.bounds,
        viewport_nm=(0, 0, detail_width * 1000, detail_height * 1000),
        input_digest="a" * 64,
        source_traces=section.traces,
        mate_traces=(),
        width_px=1,
        height_px=1,
    )


def _install_spacing_renderer(
    api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> list[Any]:
    """Write actual bounded PNG files through the separate native render API."""
    module = importlib.import_module(api.integration.__package__ + ".render")
    calls = []

    def render_spacing(
        renderer: Any, entry: Any, destination: Path, **dimensions: int
    ) -> Path:
        assert not renderer.closed
        assert dimensions == {"width_px": entry.width_px, "height_px": entry.height_px}
        calls.append(entry)
        destination.write_bytes(OTHER_PNG)
        return destination

    monkeypatch.setattr(
        module.SectionRenderer, "render_spacing", render_spacing, raising=False
    )
    return calls


def _install_original_renderer(
    api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, *, failure: str = ""
) -> list[Any]:
    """Expose an independently captured, unannotated comparison image."""
    module = importlib.import_module(api.integration.__package__ + ".render")
    calls: list[Any] = []

    def render_original(
        renderer: Any, entry: Any, destination: Path, **dimensions: int
    ) -> Path:
        """Keep original pixels distinct from the approved composite."""
        assert not renderer.closed
        assert not dimensions or dimensions == {
            "width_px": entry.width_px,
            "height_px": entry.height_px,
        }
        calls.append(entry)
        if failure == "render":
            raise RuntimeError("original render failed")
        if failure == "path":
            return destination.with_name("unexpected-original.png")
        destination.write_bytes(
            b"invalid PNG" if failure == "png" else _original_png(entry)
        )
        return destination

    monkeypatch.setattr(
        module.SectionRenderer,
        "render_spacing_original",
        render_original,
        raising=False,
    )
    return calls


def _original_png(entry: Any) -> bytes:
    """Capture the original at its detail-panel resolution, independently of PNG size."""
    width, height = entry.composition.detail_rect[2:]
    return _png(width, height, b"\x90\x20\x40")


def test_spacing_images_never_enter_manufacturing_cache_or_evidence(
    harness: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Separate diagnostic work cannot change the exact spreadsheet capture set."""
    captures, state, section = _session(harness, tmp_path)
    calls = _install_spacing_renderer(harness, monkeypatch)
    entry = _entry(harness, section)
    try:
        original = captures.preview(section)
        diagnostic = captures.preview(entry)
        assert original.data == PNG
        assert diagnostic.data == OTHER_PNG
        assert tuple(captures.images) == (section,)
        assert tuple(captures.spacing_images) == (entry,)
        assert diagnostic.viewport_nm is None
        evidence = captures.verify_captures(captures.snapshot(), (section,))
        assert evidence.current and evidence.hashes == {section: original.sha256}
        assert calls == [entry]
        assert captures.preview(entry) is diagnostic
        assert calls == [entry]
        assert len(harness.renders) == 1
        assert state.reads > 0
    finally:
        captures.close()


@pytest.mark.parametrize("operation", ["preview_spacing", "verify_spacing_captures"])
def test_superseded_spacing_constructor_preserves_the_newer_capture(
    harness: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """Nested diagnostic work keeps its renderer, hashes and pending PNG ownership."""
    captures, state, section = _session(harness, tmp_path)
    calls = _install_spacing_renderer(harness, monkeypatch)
    entry = _entry(harness, section)
    selected = replace(entry, issue_id="newer-concern", view_id="newer-spacing-view")
    render = importlib.import_module(f"{harness.integration.__package__}.render")
    original = render.SectionRenderer
    nested: list[Any] = []

    def construct(*args: Any, **kwargs: Any) -> Any:
        """Publish a newer diagnostic while the older allocation remains local."""
        candidate = original(*args, **kwargs)
        if len(harness.renderers) == 1:
            nested.append(captures.preview_spacing(selected))
        return candidate

    monkeypatch.setattr(render, "SectionRenderer", construct)
    try:
        if operation == "preview_spacing":
            with pytest.raises(harness.model.ValidationError, match="changed|closed"):
                captures.preview_spacing(entry)
        else:
            failed = captures.verify_spacing_captures(state.snapshot, (entry,))
            assert not failed.current and failed.hashes == {entry: None}
        older, current = harness.renderers
        assert older.closed and older.close_calls == 1
        assert captures.renderer is current and not current.closed
        assert captures.revision[0] == state.snapshot
        assert captures.spacing_images == {selected: nested[0]}
        assert captures._spacing_hashes[selected] == nested[0].sha256
        assert captures.spacing_image_bytes == nested[0].byte_size
        assert len(captures._pending_owned) == 1
        key, pending = next(iter(captures._pending_owned.items()))
        assert key.kind == "spacing" and key.inputs == selected
        assert pending is nested[0] and not captures.owned_captures._images
        assert not captures.images and not captures._hashes
        assert captures.preview_spacing(selected) is nested[0]
        assert calls == [selected]
        evidence = captures.verify_spacing_captures(state.snapshot, (selected,))
        assert evidence.current and evidence.hashes == {selected: nested[0].sha256}
        assert not captures._pending_owned and len(captures.owned_captures._images) == 1
    finally:
        captures.close()
    assert all(renderer.close_calls == 1 for renderer in harness.renderers)


def test_spacing_review_defers_distinct_original_until_html_export(
    harness: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native review owns only approved pixels; HTML comparisons are deferred."""
    captures, state, section = _session(harness, tmp_path)
    composites = _install_spacing_renderer(harness, monkeypatch)
    originals = _install_original_renderer(harness, monkeypatch)
    entry = _entry(harness, section)
    try:
        image = captures.preview(entry)
        assert image.viewport_nm is None
        assert image.original is None
        assert image.data == OTHER_PNG
        assert captures.spacing_image_bytes == len(OTHER_PNG)
        assert captures.preview(entry) is image
        assert composites == [entry] and originals == []
        evidence = captures.verify_spacing_captures(state.snapshot, (entry,))
        assert evidence.current and evidence.hashes == {entry: image.sha256}
        assert not captures.images and not captures._hashes
    finally:
        captures.close()


def test_spacing_review_reopening_reuses_combined_pixels_without_approving_them(
    harness: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Saved consent is still separate from frame-owned current PNG evidence."""
    captures, state, section = _session(harness, tmp_path)
    composites = _install_spacing_renderer(harness, monkeypatch)
    originals = _install_original_renderer(harness, monkeypatch)
    entry = _entry(harness, section)
    first = captures.preview(entry)
    assert captures.verify_spacing_captures(state.snapshot, (entry,)).current
    controls = captures.controls
    captures.close()
    reopened = harness.integration.CaptureSession(controls, tmp_path)
    try:
        evidence = reopened.verify_spacing_captures(reopened.snapshot(), (entry,))
        assert evidence.current and evidence.hashes == {entry: first.sha256}
        assert reopened.preview(entry).data == first.data
        assert composites == [entry] and originals == []
        assert reopened.renderer is None
        assert not hasattr(controls._owned_capture_cache, "review_tracking")
    finally:
        reopened.close()


def test_spacing_review_lru_contains_only_requested_composites(
    harness: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deferred Original comparisons cannot consume the native review PNG budget."""
    captures, _state, section = _session(harness, tmp_path)
    _install_spacing_renderer(harness, monkeypatch)
    _install_original_renderer(harness, monkeypatch)
    entry = _entry(harness, section)
    owned_bytes = len(OTHER_PNG)
    monkeypatch.setattr(
        harness.integration, "MAX_SPACING_CAPTURE_CACHE_BYTES", owned_bytes * 2
    )
    entries = tuple(replace(entry, view_id=f"combined-{index}") for index in range(5))
    try:
        manufacturing = captures.preview(section)
        for item in entries:
            captures.preview(item)
        assert tuple(captures.spacing_images) == entries[-2:]
        assert captures.spacing_image_bytes == owned_bytes * 2
        assert captures.images == {section: manufacturing}
        # Refresh must subtract old pixels before retaining their replacement.
        captures.preview(entries[-1], refresh=True)
        assert tuple(captures.spacing_images) == entries[-2:]
        assert captures.spacing_image_bytes == owned_bytes * 2
    finally:
        captures.close()


@pytest.mark.parametrize("failure", ["render", "path", "png"])
def test_unrequested_original_failure_does_not_delay_native_review(
    harness: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Original failures belong to the HTML operation that requests those pixels."""
    captures, state, section = _session(harness, tmp_path)
    _install_spacing_renderer(harness, monkeypatch)
    _install_original_renderer(harness, monkeypatch, failure=failure)
    entry = _entry(harness, section)
    try:
        image = captures.preview(entry)
        assert image.original is None
        assert captures.spacing_images[entry] is image
        assert captures.spacing_image_bytes == len(OTHER_PNG)
        evidence = captures.verify_spacing_captures(state.snapshot, (entry,))
        assert evidence.current and evidence.hashes == {entry: image.sha256}
    finally:
        captures.close()


def test_changed_input_cannot_reuse_same_view_identity_pixels(
    harness: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A display label or stable view ID cannot authorize changed issue inputs."""
    captures, _state, section = _session(harness, tmp_path)
    calls = _install_spacing_renderer(harness, monkeypatch)
    entry = _entry(harness, section)
    changed = replace(entry, input_digest="b" * 64)
    try:
        captures.preview(entry)
        captures.preview(changed)
        assert calls == [entry, changed]
        assert len(captures.spacing_images) == 2
        assert not captures.images
    finally:
        captures.close()


def test_spacing_preparation_is_cached_without_manufacturing_evidence(
    harness: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Speculative preparation owns pixels and never manufactures review records."""
    captures, state, section = _session(harness, tmp_path)
    calls = _install_spacing_renderer(harness, monkeypatch)
    entry = _entry(harness, section)
    try:
        snapshot = captures.snapshot()
        first = captures.prepare(snapshot, entry)
        second = captures.prepare(snapshot, entry)
        assert first is second and calls == [entry]
        assert state.reads == 3  # Includes the first actual native binding.
        assert not captures.images and not captures._hashes
        assert captures.spacing_images[entry] is first
    finally:
        captures.close()


def test_spacing_lru_does_not_evict_manufacturing_capture(
    harness: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Diagnostic memory has its own bound and cannot evict workbook PNGs."""
    captures, _state, section = _session(harness, tmp_path)
    _install_spacing_renderer(harness, monkeypatch)
    monkeypatch.setattr(
        harness.integration, "MAX_SPACING_CAPTURE_CACHE_BYTES", len(OTHER_PNG) * 2
    )
    entry = _entry(harness, section)
    try:
        original = captures.preview(section)
        entries = tuple(replace(entry, view_id=f"view-{i}") for i in range(5))
        for item in entries:
            captures.preview(item)
        assert len(captures.spacing_images) == 2
        assert captures.images == {section: original}
        assert captures.spacing_image_bytes == len(OTHER_PNG) * 2
    finally:
        captures.close()


@pytest.mark.parametrize("change", ["source", "appearance"])
def test_revision_change_releases_both_capture_collections(
    harness: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """An old diagnostic cannot survive an independent source/theme revision."""
    captures, state, section = _session(harness, tmp_path)
    calls = _install_spacing_renderer(harness, monkeypatch)
    entry = _entry(harness, section)
    try:
        captures.preview(section)
        captures.preview(entry)
        previous = captures.renderer
        if change == "source":
            state.snapshot = replace(state.snapshot, context_digest="changed")
        else:
            harness.palette = "changed"
        captures.preview(entry)
        assert previous.closed
        assert calls == [entry, entry]
        assert not captures.images and not captures._hashes
        assert tuple(captures.spacing_images) == (entry,)
    finally:
        captures.close()


@pytest.mark.parametrize("count", [1, 25])
@pytest.mark.parametrize("warm", [False, True])
def test_spacing_batch_checks_boundaries_and_any_actual_native_binding(
    harness: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    warm: bool,
) -> None:
    """The Save path performs constant source work for any diagnostic image count."""
    captures, state, section = _session(harness, tmp_path)
    calls = _install_spacing_renderer(harness, monkeypatch)
    entry = _entry(harness, section)
    entries = tuple(replace(entry, view_id=f"view-{i}") for i in range(count))
    appearances = []
    read_appearance = harness.integration.read_theme_context

    def appearance(pcbnew: Any) -> Any:
        appearances.append(True)
        return read_appearance(pcbnew)

    monkeypatch.setattr(harness.integration, "read_theme_context", appearance)
    try:
        if warm:
            snapshot = captures.snapshot()
            for item in entries:
                captures.prepare(snapshot, item)
        before = state.reads, len(calls), len(appearances)
        evidence = captures.verify_spacing_captures(state.snapshot, entries)
        assert evidence.current
        expected_sha = harness.integration.CapturedImage.from_bytes(OTHER_PNG).sha256
        assert evidence.hashes == dict.fromkeys(entries, expected_sha)
        expected_checks = 2 if warm else 3
        assert state.reads - before[0] == expected_checks
        assert len(appearances) - before[2] == expected_checks
        assert len(calls) - before[1] == (0 if warm else count)
        assert not captures.images and not captures._hashes
        again = captures.verify_spacing_captures(state.snapshot, entries)
        assert again == evidence
        assert state.reads - before[0] == expected_checks + 2
        assert len(appearances) - before[2] == expected_checks + 2
        assert len(calls) - before[1] == (0 if warm else count)
    finally:
        captures.close()


def test_spacing_batch_keeps_actual_hashes_after_png_eviction_without_review_events(
    harness: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same-session hash evidence survives bounded bytes, without workbook consent."""
    captures, state, section = _session(harness, tmp_path)
    calls = _install_spacing_renderer(harness, monkeypatch)
    entry = _entry(harness, section)
    entries = tuple(replace(entry, view_id=f"view-{i}") for i in range(5))
    monkeypatch.setattr(
        harness.integration, "MAX_SPACING_CAPTURE_CACHE_BYTES", len(OTHER_PNG) * 2
    )
    tracking = importlib.import_module(
        harness.integration.__package__ + ".review_tracking"
    )

    def no_timestamp() -> str:
        pytest.fail("Capture verification cannot record review events")

    monkeypatch.setattr(tracking, "utc_now", no_timestamp)
    try:
        original = captures.preview(section)
        hashes = {item: captures.preview(item).sha256 for item in entries}
        assert len(captures.spacing_images) == 2
        assert entries[0] not in captures.spacing_images
        before = state.reads, len(calls), dict(captures._hashes)
        evidence = captures.verify_spacing_captures(state.snapshot, entries)
        assert evidence.current and evidence.hashes == hashes
        assert state.reads - before[0] == 2 and len(calls) == before[1]
        assert captures._hashes == before[2] == {section: original.sha256}
        assert captures.images == {section: original}
        assert captures.spacing_image_bytes == len(OTHER_PNG) * 2
    finally:
        captures.close()


def test_spacing_batch_only_renders_missing_entries_and_uses_each_owned_png_sha(
    harness: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new entry sharing a view ID cannot inherit different inputs' pixels."""
    captures, state, section = _session(harness, tmp_path)
    calls = _install_spacing_renderer(harness, monkeypatch)
    renderer_module = importlib.import_module(
        harness.integration.__package__ + ".render"
    )
    render = renderer_module.SectionRenderer.render_spacing
    entry = _entry(harness, section)
    changed = replace(entry, input_digest="b" * 64)
    missing = replace(entry, view_id="missing")
    changed_png = _png(1, 1, b"\x90\x20\x40")

    def distinct_png(
        renderer: Any, item: Any, destination: Path, **kwargs: int
    ) -> Path:
        result = render(renderer, item, destination, **kwargs)
        if item == changed:
            destination.write_bytes(changed_png)
        return result

    monkeypatch.setattr(renderer_module.SectionRenderer, "render_spacing", distinct_png)
    try:
        original = captures.preview(entry)
        before = state.reads
        evidence = captures.verify_spacing_captures(
            state.snapshot, (entry, changed, missing)
        )
        png_sha = harness.integration.CapturedImage.from_bytes(changed_png).sha256
        assert evidence.current
        assert evidence.hashes == {
            entry: original.sha256,
            changed: png_sha,
            missing: original.sha256,
        }
        assert calls == [entry, changed, missing]
        assert state.reads - before == 2
        assert not captures._hashes
    finally:
        captures.close()


@pytest.mark.parametrize(
    "change", ["source", "palette", "variant", "source_read", "palette_read"]
)
@pytest.mark.parametrize("warm", [False, True])
def test_spacing_batch_invalidates_all_evidence_when_live_revision_changes(
    harness: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    warm: bool,
) -> None:
    """A changed boundary invalidates both ledgers, including already cached PNGs."""
    captures, state, section = _session(harness, tmp_path)
    calls = _install_spacing_renderer(harness, monkeypatch)
    renderer_module = importlib.import_module(
        harness.integration.__package__ + ".render"
    )
    render = renderer_module.SectionRenderer.render_spacing
    entry = _entry(harness, section)
    other = replace(entry, view_id="other")

    def mutate() -> None:
        if change == "source":
            state.snapshot = replace(
                state.snapshot, context_digest="changed-during-batch"
            )
        elif change == "palette":
            harness.palette = "changed-during-batch"
        elif change == "variant":
            state.variant = "changed-output"
        elif change == "source_read":
            state.error = OSError("source unavailable")
        else:
            harness.palette_error = OSError("palette unavailable")

    def mutate_during_render(
        renderer: Any, item: Any, destination: Path, **kwargs: int
    ) -> Path:
        result = render(renderer, item, destination, **kwargs)
        mutate()
        return result

    try:
        expected = captures.snapshot()
        captures.preview(section)
        captures.preview(entry)
        if warm:
            captures.preview(other)
            snapshot = captures.controls._snapshot

            def mutate_after_first_source_read() -> Any:
                result = snapshot()
                mutate()
                return result

            captures.controls._snapshot = mutate_after_first_source_read
        else:
            monkeypatch.setattr(
                renderer_module.SectionRenderer, "render_spacing", mutate_during_render
            )
        evidence = captures.verify_spacing_captures(expected, (entry, other))
        assert not evidence.current and evidence.hashes == {entry: None, other: None}
        assert not captures._hashes and not captures._spacing_hashes
        assert not captures.images and not captures.spacing_images
        assert captures.image_bytes == captures.spacing_image_bytes == 0
        assert all(renderer.closed for renderer in harness.renderers)
        assert calls == [entry, other]
    finally:
        captures.close()


@pytest.mark.parametrize("phase", ["batch", "refresh"])
def test_spacing_batch_failure_stays_failed_until_successful_explicit_capture(
    harness: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    """Missing/failed proof cannot silently revive an old digest after cache release."""
    captures, state, section = _session(harness, tmp_path)
    calls = _install_spacing_renderer(harness, monkeypatch)
    module = importlib.import_module(harness.integration.__package__ + ".render")
    render = module.SectionRenderer.render_spacing
    entry = _entry(harness, section)

    def fail(renderer: Any, item: Any, destination: Path, **kwargs: int) -> Path:
        raise RuntimeError("native spacing render failed")

    try:
        if phase == "refresh":
            captures.preview(entry)
        monkeypatch.setattr(module.SectionRenderer, "render_spacing", fail)
        if phase == "refresh":
            with pytest.raises(RuntimeError, match="native spacing render failed"):
                captures.preview(entry, refresh=True)
        else:
            first = captures.verify_spacing_captures(state.snapshot, (entry,))
            assert first.current and first.hashes == {entry: None}
        monkeypatch.setattr(module.SectionRenderer, "render_spacing", render)
        before = len(calls)
        failed = captures.verify_spacing_captures(state.snapshot, (entry,))
        assert failed.current and failed.hashes == {entry: None}
        assert len(calls) == before
        actual = captures.prepare(state.snapshot, entry)
        restored = captures.verify_spacing_captures(state.snapshot, (entry,))
        assert restored.current and restored.hashes == {entry: actual.sha256}
        assert len(calls) == before + 1
    finally:
        captures.close()


def test_new_capture_session_renders_diagnostics_instead_of_trusting_old_history(
    harness: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same entry keys and stale files cannot seed a reopened session's ledger."""
    captures, state, section = _session(harness, tmp_path)
    calls = _install_spacing_renderer(harness, monkeypatch)
    entry = _entry(harness, section)
    captures.preview(entry)
    captures.close()
    reopened, reopened_state, reopened_section = _session(harness, tmp_path)
    reopened_entry = _entry(harness, reopened_section)
    assert reopened_entry == entry
    try:
        assert not reopened._spacing_hashes
        evidence = reopened.verify_spacing_captures(reopened_state.snapshot, (entry,))
        assert evidence.current and all(evidence.hashes.values())
        assert calls == [entry, entry]
        assert not reopened._hashes
    finally:
        reopened.close()


def test_spacing_batch_rejects_wrong_dimensions_without_poisoning_other_evidence(
    harness: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only actual matching captures may enter the diagnostic evidence ledger."""
    captures, state, section = _session(harness, tmp_path)
    calls = _install_spacing_renderer(harness, monkeypatch)
    entry = _entry(harness, section)
    wrong = replace(entry, view_id="wrong-size", width_px=2)
    try:
        evidence = captures.verify_spacing_captures(state.snapshot, (wrong, entry))
        expected = harness.integration.CapturedImage.from_bytes(OTHER_PNG).sha256
        assert evidence.current and evidence.hashes == {wrong: None, entry: expected}
        assert calls == [wrong, entry]
        repeated = captures.verify_spacing_captures(state.snapshot, (wrong, entry))
        assert repeated == evidence and calls == [wrong, entry]
        assert not captures._hashes
    finally:
        captures.close()


def test_spacing_batch_variant_reselection_releases_real_output_owner(
    harness: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Identical output copper cannot reuse consent after selecting another variant."""
    from .test_impedance_controls import _configuration
    from .test_impedance_output_variant import _variant_controls

    controls, state = _variant_controls(harness, monkeypatch)
    captures = harness.integration.CaptureSession(controls, tmp_path)
    _install_spacing_renderer(harness, monkeypatch)
    try:
        snapshot = captures.snapshot()
        section = harness.matching.analyze(_configuration(harness), snapshot).sections[
            0
        ]
        entry = _entry(harness, section)
        captures.preview(entry)
        state.session.output_variant = "B"
        evidence = captures.verify_spacing_captures(snapshot, (entry,))
        assert not evidence.current and evidence.hashes == {entry: None}
        assert not captures._hashes and not captures._spacing_hashes
        assert not captures.spacing_images
        assert harness.renderers[0].closed
        assert state.owners[0].closed
    finally:
        captures.close()
    assert all(owner.closed for owner in state.owners)


@pytest.mark.parametrize("policy_name", ["check", "render"])
@pytest.mark.parametrize("phase", ["before", "during"])
def test_diagnostic_policy_change_invalidates_batch_without_changing_workbook_proof(
    harness: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy_name: str,
    phase: str,
) -> None:
    """Diagnostic policy provenance cannot borrow the manufacturing capture policy."""
    captures, state, section = _session(harness, tmp_path)
    calls = _install_spacing_renderer(harness, monkeypatch)
    renderer_module = importlib.import_module(
        harness.integration.__package__ + ".render"
    )
    render = renderer_module.SectionRenderer.render_spacing
    owner = importlib.import_module(
        harness.integration.__package__
        + (".spacing_checks" if policy_name == "check" else ".spacing_review")
    )
    name = (
        "SPACING_CHECK_POLICY_REVISION"
        if policy_name == "check"
        else "SPACING_RENDER_POLICY_REVISION"
    )
    old_policy = getattr(owner, name)
    entry = _entry(harness, section)
    missing = replace(entry, view_id="missing")

    def change_policy() -> None:
        monkeypatch.setattr(owner, name, old_policy + 1)

    def mutate_during_render(
        renderer: Any, item: Any, destination: Path, **kwargs: int
    ) -> Path:
        result = render(renderer, item, destination, **kwargs)
        change_policy()
        return result

    try:
        original = captures.preview(section)
        captures.preview(entry)
        if phase == "before":
            captures.preview(missing)
            change_policy()
        else:
            monkeypatch.setattr(
                renderer_module.SectionRenderer, "render_spacing", mutate_during_render
            )
        evidence = captures.verify_spacing_captures(state.snapshot, (entry, missing))
        assert not evidence.current and evidence.hashes == {entry: None, missing: None}
        assert not captures._spacing_hashes and not captures.spacing_images
        assert captures._hashes == {section: original.sha256}
        assert captures.images == {section: original}
        before = len(calls)
        again = captures.verify_spacing_captures(state.snapshot, (entry, missing))
        assert again == evidence and len(calls) == before
        monkeypatch.setattr(renderer_module.SectionRenderer, "render_spacing", render)
        actual = captures.prepare(state.snapshot, entry)
        restored = captures.verify_spacing_captures(state.snapshot, (entry,))
        assert restored.current and restored.hashes == {entry: actual.sha256}
        assert len(calls) == before + 1
        assert captures._hashes == {section: original.sha256}
    finally:
        captures.close()


@pytest.mark.parametrize("method", ["prepare", "preview"])
def test_cached_diagnostic_cannot_reuse_pixels_after_policy_change(
    harness: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    """Explicit capture under new policy replaces old cached PNG evidence."""
    captures, state, section = _session(harness, tmp_path)
    calls = _install_spacing_renderer(harness, monkeypatch)
    module = importlib.import_module(
        harness.integration.__package__ + ".spacing_review"
    )
    entry = _entry(harness, section)
    try:
        if method == "prepare":
            first = captures.prepare(state.snapshot, entry)
            assert captures.preview(entry) is first
        else:
            first = captures.preview(entry)
        assert calls == [entry]
        monkeypatch.setattr(
            module,
            "SPACING_RENDER_POLICY_REVISION",
            module.SPACING_RENDER_POLICY_REVISION + 1,
        )
        actual = (
            captures.prepare(state.snapshot, entry)
            if method == "prepare"
            else captures.preview(entry)
        )
        assert actual is not first and calls == [entry, entry]
        evidence = captures.verify_spacing_captures(state.snapshot, (entry,))
        assert evidence.current and evidence.hashes == {entry: actual.sha256}
    finally:
        captures.close()


def test_diagnostic_hash_ledger_is_bounded_and_prunes_old_configuration_entries(
    harness: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Many edited configurations cannot grow evidence beyond one review set."""
    captures, state, section = _session(harness, tmp_path)
    calls = _install_spacing_renderer(harness, monkeypatch)
    module = importlib.import_module(
        harness.integration.__package__ + ".spacing_review"
    )
    monkeypatch.setattr(module, "MAX_SPACING_REVIEW_IMAGES", 2)
    monkeypatch.setattr(
        harness.integration, "MAX_SPACING_CAPTURE_CACHE_BYTES", len(OTHER_PNG) * 2
    )
    entry = _entry(harness, section)
    entries = tuple(replace(entry, view_id=f"view-{i}") for i in range(5))
    try:
        original = captures.preview(section)
        for item in entries:
            captures.prepare(state.snapshot, item)
        assert len(captures._spacing_hashes) == 2
        assert tuple(captures._spacing_hashes) == entries[-2:]
        evidence = captures.verify_spacing_captures(
            state.snapshot, (entries[0], entries[-1])
        )
        assert evidence.current and all(evidence.hashes.values())
        assert set(captures._spacing_hashes) == {entries[0], entries[-1]}
        assert calls == [*entries, entries[0]]
        before = len(calls)
        rejected = captures.verify_spacing_captures(state.snapshot, entries)
        assert not rejected.current and rejected.hashes == dict.fromkeys(entries)
        assert len(calls) == before
        assert not captures._spacing_hashes and not captures.spacing_images
        assert captures._hashes == {section: original.sha256}
    finally:
        captures.close()
