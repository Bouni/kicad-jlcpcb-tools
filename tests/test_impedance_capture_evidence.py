"""Keep reviewed image evidence without redoing per-image source validation."""

from base64 import b64decode
from dataclasses import replace
import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from .test_impedance_controls import PNG, _configuration, _snapshot, harness
from .test_impedance_output_variant import _variant_controls

__all__ = ["harness"]
OTHER_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1sAAAAASUVORK5CYII="
)


def _session(harness: SimpleNamespace, directory: Path) -> tuple[Any, Any, Any]:
    """Use the real session, immutable sections, and counted native boundaries."""
    state = SimpleNamespace(
        snapshot=_snapshot(harness), reads=0, variant=None, error=None
    )

    def snapshot() -> Any:
        """Read mutable source state so the test detects actual freshness work."""
        state.reads += 1
        if state.error is not None:
            raise state.error
        return state.snapshot

    controls = SimpleNamespace(
        parent=harness.parent,
        _selected_output_variant=lambda: state.variant,
        _snapshot=snapshot,
    )
    captures = harness.integration.CaptureSession(controls, directory)
    section = harness.matching.analyze(
        _configuration(harness), state.snapshot
    ).sections[0]
    return captures, state, section


def test_closing_and_reopening_review_reuses_owned_png_without_native_lifetime(
    harness: SimpleNamespace, tmp_path: Path
) -> None:
    """A same-source reopening must verify bytes, without plotting them again."""
    captures, state, section = _session(harness, tmp_path)
    reviewed = captures.preview(section)
    assert captures.verify_captures(captures.snapshot(), (section,)).current
    renderer = captures.renderer
    controls = captures.controls
    captures.close()
    assert renderer.closed and captures.renderer is None
    reopened = harness.integration.CaptureSession(controls, tmp_path)
    try:
        evidence = reopened.verify_captures(reopened.snapshot(), (section,))
        assert evidence.current and evidence.hashes == {section: reviewed.sha256}
        assert reopened.preview(section).data == reviewed.data
        assert len(harness.renders) == 1
        assert reopened.renderer is None
        assert state.reads >= 3
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "change", ["source", "appearance", "policy", "inputs", "board"]
)
def test_reopened_review_renders_again_when_any_pixel_identity_changes(
    harness: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """Reopening cannot reuse old bytes solely because a section ID survived."""
    captures, state, section = _session(harness, tmp_path)
    controls = captures.controls
    captures.preview(section)
    captures.verify_captures(captures.snapshot(), (section,))
    captures.close()
    if change == "source":
        state.snapshot = replace(state.snapshot, context_digest="new-source")
    elif change == "appearance":
        harness.palette = "new-palette"
    elif change == "policy":
        monkeypatch.setattr(harness.matching, "CAPTURE_POLICY_REVISION", 999)
    elif change == "inputs":
        section = replace(section, width_nm=section.width_nm + 1)
    else:
        controls.board_path = str(tmp_path / "other-board.kicad_pcb")
    reopened = harness.integration.CaptureSession(controls, tmp_path)
    try:
        evidence = reopened.verify_captures(reopened.snapshot(), (section,))
        assert evidence.current and evidence.hashes[section]
        assert len(harness.renders) == 2
    finally:
        reopened.close()


def test_unverified_render_cannot_enter_owned_cache_after_source_change(
    harness: SimpleNamespace, tmp_path: Path
) -> None:
    """Fresh post-render verification is required before sharing any pixels."""
    captures, state, section = _session(harness, tmp_path)
    captures.preview(section)
    assert captures._pending_owned and not captures.owned_captures._images
    state.snapshot = replace(state.snapshot, context_digest="render-changed-source")
    assert not captures.verify_captures(captures.snapshot(), (section,)).current
    assert not captures.owned_captures._images
    assert not captures._pending_owned
    captures.close()


@pytest.mark.parametrize("change", ["close", "source"])
@pytest.mark.parametrize("operation", ["preview", "prepare", "batch"])
def test_renderer_construction_cannot_publish_after_session_invalidation(
    harness: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    operation: str,
) -> None:
    """Native constructor reentry must release its unpublished candidate once."""
    captures, state, section = _session(harness, tmp_path)
    render = importlib.import_module(f"{harness.integration.__package__}.render")
    original = render.SectionRenderer

    def construct(*args: Any, **kwargs: Any) -> Any:
        """Invalidate the actual session before its renderer constructor returns."""
        candidate = original(*args, **kwargs)
        if change == "close":
            captures.close()
        else:
            state.snapshot = replace(state.snapshot, context_digest="reentrant-edit")
            captures.snapshot()
        return candidate

    monkeypatch.setattr(render, "SectionRenderer", construct)
    try:
        if operation == "batch":
            evidence = captures.verify_captures(captures.snapshot(), (section,))
            assert not evidence.current and evidence.hashes == {section: None}
        else:
            with pytest.raises(harness.model.ValidationError, match="changed|closed"):
                if operation == "prepare":
                    captures.prepare(captures.snapshot(), section)
                else:
                    captures.preview(section)
        assert captures.renderer is None
        assert captures.revision is None
        if change == "close":
            assert captures._evidence_revision is None
        assert not harness.renders
        assert not captures.images and not captures._pending_owned
        assert not captures.owned_captures._images
        assert len(harness.renderers) == 1
        assert harness.renderers[0].closed
        assert harness.renderers[0].close_calls == 1
    finally:
        captures.close()


@pytest.mark.parametrize("changed_source", [False, True])
@pytest.mark.parametrize("operation", ["preview", "prepare", "batch"])
@pytest.mark.parametrize(
    "failure",
    [None, "board_access", "constructor", "candidate_close", "interrupt", "exit"],
)
def test_obsolete_renderer_constructor_preserves_a_newer_nested_preview(
    harness: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_source: bool,
    operation: str,
    failure: Optional[str],
) -> None:
    """Discard the outer allocation without closing or reverting newer captures."""
    captures, state, section = _session(harness, tmp_path)
    selected = replace(section, section_id="selected-during-construction")
    render = importlib.import_module(f"{harness.integration.__package__}.render")
    original = render.SectionRenderer
    nested: list[Any] = []

    def newer_preview() -> None:
        """Publish the newer request through the real session and renderer."""
        if changed_source:
            state.snapshot = replace(state.snapshot, context_digest="nested-edit")
        nested.append(captures.preview(selected))

    def construct(*args: Any, **kwargs: Any) -> Any:
        """Complete a real nested render while the older candidate is unpublished."""
        candidate = original(*args, **kwargs)
        if len(harness.renderers) == 1 and failure != "board_access":
            if failure == "candidate_close":
                close = candidate.close

                def failed_close() -> None:
                    """Retain the native owner's state change before cleanup fails."""
                    close()
                    raise RuntimeError("obsolete renderer cleanup failed")

                candidate.close = failed_close
            newer_preview()
            if failure in ("constructor", "interrupt", "exit"):
                candidate.close()
                if failure == "interrupt":
                    raise KeyboardInterrupt("obsolete renderer interrupted")
                if failure == "exit":
                    raise SystemExit(7)
                raise RuntimeError("obsolete renderer constructor failed")
        return candidate

    monkeypatch.setattr(render, "SectionRenderer", construct)
    if failure == "board_access":
        captures.controls.board_path = harness.board_path
        entered = False

        def get_board() -> Any:
            """Fail the obsolete native getter after its callback finishes newer work."""
            nonlocal entered
            if not entered:
                entered = True
                newer_preview()
                raise RuntimeError("obsolete board getter failed")
            return harness.board

        monkeypatch.setattr(harness.pcbnew, "GetBoard", get_board)
    try:
        interruption = {"interrupt": KeyboardInterrupt, "exit": SystemExit}.get(failure)
        if operation == "batch" and interruption is None:
            evidence = captures.verify_captures(captures.snapshot(), (section,))
            assert not evidence.current and evidence.hashes == {section: None}
        else:
            with pytest.raises(
                interruption or harness.model.ValidationError,
                match="changed|closed" if interruption is None else None,
            ) as caught:
                if operation == "prepare":
                    captures.prepare(captures.snapshot(), section)
                elif operation == "batch":
                    captures.verify_captures(captures.snapshot(), (section,))
                else:
                    captures.preview(section)
            if failure == "exit":
                assert caught.value.code == 7
        if failure == "board_access":
            assert len(harness.renderers) == 1
            current = harness.renderers[0]
        else:
            assert len(harness.renderers) == 2
            older, current = harness.renderers
            assert older.closed and older.close_calls == 1
        assert captures.renderer is current and not current.closed
        assert captures.revision[0] == state.snapshot
        assert captures.images == {selected: nested[0]}
        assert len(captures._pending_owned) == 1
        assert captures.preview(selected) is nested[0]
        assert len(harness.renders) == 1
    finally:
        captures.close()
    assert all(renderer.close_calls == 1 for renderer in harness.renderers)


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_renderer_cleanup_cannot_replace_the_original_cancellation(
    harness: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: type[BaseException],
) -> None:
    """Dispose the unpublished candidate while preserving the initiating signal."""
    captures, _state, section = _session(harness, tmp_path)
    render = importlib.import_module(f"{harness.integration.__package__}.render")
    original = render.SectionRenderer
    variant = captures.controls._selected_output_variant

    def cancelled_variant() -> Any:
        captures.controls._selected_output_variant = variant
        raise interruption("original cancellation")

    def construct(*args: Any, **kwargs: Any) -> Any:
        candidate = original(*args, **kwargs)
        close = candidate.close

        def failed_close() -> None:
            close()
            cleanup = (
                SystemExit if interruption is KeyboardInterrupt else KeyboardInterrupt
            )
            raise cleanup("cleanup cancellation")

        candidate.close = failed_close
        captures.controls._selected_output_variant = cancelled_variant
        return candidate

    monkeypatch.setattr(render, "SectionRenderer", construct)
    try:
        with pytest.raises(interruption, match="original cancellation"):
            captures.preview(section)
        assert captures.renderer is None and not harness.renders
        assert harness.renderers[0].closed and harness.renderers[0].close_calls == 1
    finally:
        captures.controls._selected_output_variant = variant
        captures.close()


@pytest.mark.parametrize("identity", [None, "", "unreadable"])
def test_missing_stable_board_identity_keeps_the_native_cold_path_working(
    harness: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity: Any,
) -> None:
    """An optional cache must not fabricate a cwd identity or block capture."""
    captures, _state, section = _session(harness, tmp_path)
    if identity is None:
        board = object()
    elif identity == "unreadable":

        def failed_filename() -> str:
            raise RuntimeError("native identity unavailable")

        board = SimpleNamespace(GetFileName=failed_filename)
    else:
        board = SimpleNamespace(GetFileName=lambda: identity)
    monkeypatch.setattr(harness.parent.pcbnew, "GetBoard", lambda: board)
    captures.preview(section)
    assert captures.verify_captures(captures.snapshot(), (section,)).current
    assert not captures.owned_captures._images
    controls = captures.controls
    captures.close()
    reopened = harness.integration.CaptureSession(controls, tmp_path)
    try:
        assert reopened.verify_captures(reopened.snapshot(), (section,)).current
        assert len(harness.renders) == 2
    finally:
        reopened.close()


def test_reopened_background_preparation_reuses_one_checked_owned_revision(
    harness: SimpleNamespace, tmp_path: Path
) -> None:
    """A warm renderer-free batch cannot rescan the source for every image."""
    captures, state, section = _session(harness, tmp_path)
    sections = tuple(
        replace(section, section_id=f"warm-{index}") for index in range(25)
    )
    for item in sections:
        captures.preview(item)
    captures.verify_captures(captures.snapshot(), sections)
    controls = captures.controls
    captures.close()
    reopened = harness.integration.CaptureSession(controls, tmp_path)
    before = state.reads
    try:
        snapshot = reopened.snapshot()
        for item in sections:
            assert reopened.prepare(snapshot, item).data == PNG
        assert state.reads - before == 2
        assert len(harness.renders) == len(sections)
        assert reopened.renderer is None
    finally:
        reopened.close()


@pytest.mark.parametrize("change", ["source", "appearance"])
def test_warm_preparation_requires_current_source_before_binding_cold_renderer(
    harness: SimpleNamespace, tmp_path: Path, change: str
) -> None:
    """A cache hit across idle turns cannot authorize binding changed geometry."""
    captures, state, section = _session(harness, tmp_path)
    captures.preview(section)
    captures.verify_captures(captures.snapshot(), (section,))
    controls = captures.controls
    captures.close()
    reopened = harness.integration.CaptureSession(controls, tmp_path)
    snapshot = reopened.snapshot()
    missing = replace(section, section_id="uncached-after-idle")
    try:
        reopened.prepare(snapshot, section)
        assert reopened.renderer is None
        if change == "source":
            state.snapshot = replace(snapshot, context_digest="changed-in-idle")
        else:
            harness.palette = "changed-in-idle"
        with pytest.raises(harness.model.ValidationError, match="changed"):
            reopened.prepare(snapshot, missing)
        # Restoring later must not admit pixels from the intermediate source.
        state.snapshot = snapshot
        harness.palette = "original-palette"
        assert len(harness.renders) == 1
        assert not reopened._pending_owned
        restored = reopened.prepare(snapshot, missing)
        evidence = reopened.verify_captures(reopened.snapshot(), (missing,))
        assert evidence.current and evidence.hashes == {missing: restored.sha256}
        key = reopened._owned_key(missing)
        owned = reopened.owned_captures.get(key)
        assert owned is not None and owned.sha256 == restored.sha256
        assert owned.viewport_nm == restored.viewport_nm
        assert len(harness.renders) == 2
    finally:
        reopened.close()


def test_renderer_failure_then_source_change_cannot_bind_old_preparation_revision(
    harness: SimpleNamespace, tmp_path: Path
) -> None:
    """Retrying failed native work must recheck its current live source."""
    captures, state, section = _session(harness, tmp_path)
    snapshot = captures.snapshot()
    harness.render_error = RuntimeError("native render failed")
    with pytest.raises(RuntimeError, match="native render failed"):
        captures.prepare(snapshot, section)
    assert captures.renderer is None and not captures._pending_owned
    harness.render_error = None
    state.snapshot = replace(snapshot, context_digest="changed-after-failure")
    with pytest.raises(harness.model.ValidationError, match="changed"):
        captures.prepare(snapshot, section)
    state.snapshot = snapshot
    assert not captures.owned_captures._images and not captures._pending_owned
    assert len(harness.renders) == 1
    restored = captures.prepare(snapshot, section)
    evidence = captures.verify_captures(captures.snapshot(), (section,))
    assert evidence.current and evidence.hashes == {section: restored.sha256}
    key = captures._owned_key(section)
    owned = captures.owned_captures.get(key)
    assert owned is not None and owned.sha256 == restored.sha256
    assert owned.viewport_nm == restored.viewport_nm
    assert len(harness.renders) == 2
    captures.close()


@pytest.mark.parametrize("count", [1, 25])
def test_known_capture_verification_has_one_source_check_for_any_image_count(
    harness: SimpleNamespace, tmp_path: Path, count: int
) -> None:
    """Saving verified current images must not reread the board or call preview."""
    captures, state, section = _session(harness, tmp_path)
    sections = tuple(replace(section, section_id=f"image-{i}") for i in range(count))
    try:
        hashes = {row: captures.preview(row).sha256 for row in sections}
        before = state.reads, len(harness.renders)
        evidence = captures.verify_captures(captures.snapshot(), sections)
        assert evidence.current
        assert evidence.hashes == hashes
        assert (state.reads - before[0], len(harness.renders) - before[1]) == (1, 0)
    finally:
        captures.close()


@pytest.mark.parametrize("count", [1, 25])
def test_background_capture_preparation_does_not_rescan_each_image(
    harness: SimpleNamespace, tmp_path: Path, count: int
) -> None:
    """One owned revision warms many images, and Save still checks the live source."""
    captures, state, section = _session(harness, tmp_path)
    sections = tuple(replace(section, section_id=f"image-{i}") for i in range(count))
    try:
        snapshot = captures.snapshot()
        hashes = {row: captures.prepare(snapshot, row).sha256 for row in sections}
        assert state.reads == 3
        assert len(harness.renderers) == 1
        assert len(harness.renders) == count
        evidence = captures.verify_captures(captures.snapshot(), sections)
        assert evidence.current
        assert evidence.hashes == hashes
        assert state.reads == 4
        assert len(harness.renders) == count
    finally:
        captures.close()


@pytest.mark.parametrize("change", ["snapshot", "palette"])
def test_historical_restoration_rejects_revision_changed_since_initial_snapshot(
    harness: SimpleNamespace, tmp_path: Path, change: str
) -> None:
    """An opening's checked source cannot silently become proof for a later revision."""
    captures, state, section = _session(harness, tmp_path)
    try:
        captures.snapshot()
        if change == "snapshot":
            state.snapshot = replace(state.snapshot, context_digest="changed")
        else:
            harness.palette = "changed"
        evidence = captures.verify_captures(captures.snapshot(), (section,))
        assert not evidence.current
        assert evidence.hashes == {section: None}
        assert not harness.renders
        assert not harness.renderers
    finally:
        captures.close()


def test_png_lru_eviction_preserves_verified_hashes(
    harness: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evict real cache entries without allocating tens of megabytes in this test."""
    captures, state, section = _session(harness, tmp_path)
    monkeypatch.setattr(harness.integration, "MAX_CAPTURE_CACHE_BYTES", len(PNG) * 2)
    sections = tuple(replace(section, section_id=f"image-{i}") for i in range(5))
    try:
        hashes = {row: captures.preview(row).sha256 for row in sections}
        assert len(captures.images) == 2
        assert sections[0] not in captures.images
        before = state.reads, len(harness.renders)
        evidence = captures.verify_captures(captures.snapshot(), sections)
        assert evidence.current
        assert evidence.hashes == hashes
        assert (state.reads - before[0], len(harness.renders) - before[1]) == (1, 0)
    finally:
        captures.close()


@pytest.mark.parametrize("count", [1, 25])
def test_missing_historical_evidence_checks_only_batch_boundaries(
    harness: SimpleNamespace, tmp_path: Path, count: int
) -> None:
    """Cold restoration checks batch boundaries and the real native binding."""
    captures, state, section = _session(harness, tmp_path)
    sections = tuple(replace(section, section_id=f"image-{i}") for i in range(count))
    try:
        evidence = captures.verify_captures(captures.snapshot(), sections)
        assert evidence.current
        assert all(evidence.hashes.values())
        assert set(evidence.hashes) == set(sections)
        assert state.reads == 3
        assert len(harness.renders) == count
        assert len(harness.renderers) == 1
        repeated = captures.verify_captures(captures.snapshot(), sections)
        assert repeated == evidence
        assert state.reads == 4
        assert len(harness.renders) == count
    finally:
        captures.close()


def test_mixed_evidence_only_renders_missing_sections_and_reads_appearance_at_boundaries(
    harness: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing proof and new historical proof can share one checked batch."""
    captures, state, section = _session(harness, tmp_path)
    other = replace(section, section_id="missing")
    appearances = []
    original = harness.integration.read_theme_context

    def read_appearance(pcbnew: Any) -> Any:
        """Count real appearance-boundary reads without changing their state."""
        appearances.append(True)
        return original(pcbnew)

    try:
        current = captures.preview(section)
        monkeypatch.setattr(harness.integration, "read_theme_context", read_appearance)
        before = state.reads
        evidence = captures.verify_captures(captures.snapshot(), (section, other))
        assert evidence.current
        assert evidence.hashes[section] == current.sha256
        assert all(evidence.hashes.values())
        assert [row for row, _path in harness.renders] == [section, other]
        assert state.reads - before == 2
        assert len(appearances) == 2
        captures.verify_captures(captures.snapshot(), (section, other))
        assert state.reads - before == 3
        assert len(appearances) == 3
    finally:
        captures.close()


def test_changed_source_detected_by_caller_does_not_turn_into_cold_restoration(
    harness: SimpleNamespace, tmp_path: Path
) -> None:
    """Source invalidation must remember that old approval was previously verified."""
    captures, state, section = _session(harness, tmp_path)
    try:
        captures.preview(section)
        state.snapshot = replace(
            state.snapshot, context_digest="changed-after-approval"
        )
        evidence = captures.verify_captures(captures.snapshot(), (section,))
        assert not evidence.current
        assert evidence.hashes == {section: None}
        assert len(harness.renders) == 1
        assert not captures.verify_captures(captures.snapshot(), (section,)).current
        assert len(harness.renders) == 1
    finally:
        captures.close()


def test_same_section_id_with_different_geometry_needs_new_evidence(
    harness: SimpleNamespace, tmp_path: Path
) -> None:
    """A section identifier alone cannot authorize different image contents."""
    captures, _state, section = _session(harness, tmp_path)
    changed = replace(section, width_nm=section.width_nm + 1)
    try:
        original = captures.preview(section)
        evidence = captures.verify_captures(captures.snapshot(), (changed,))
        assert evidence.current
        assert evidence.hashes == {changed: original.sha256}
        assert [row for row, _path in harness.renders] == [section, changed]
    finally:
        captures.close()


@pytest.mark.parametrize("change", ["snapshot", "palette", "variant", "policy"])
def test_changed_revision_rejects_old_evidence_without_rendering(
    harness: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """Save must not turn a revision change into an implicit image regeneration."""
    captures, state, section = _session(harness, tmp_path)
    try:
        captures.preview(section)
        if change == "snapshot":
            state.snapshot = replace(state.snapshot, context_digest="edited")
        elif change == "palette":
            harness.palette = "new-theme"
        elif change == "variant":
            # Identical selected output geometry still belongs to a different variant.
            state.variant = "new-output"
        else:
            monkeypatch.setattr(
                harness.matching,
                "CAPTURE_POLICY_REVISION",
                harness.matching.CAPTURE_POLICY_REVISION + 1,
            )
        # Caller already owns the fresh snapshot. This avoids fabricating an output
        # copy for the cheap, independent variant-provenance assertion above.
        evidence = captures.verify_captures(state.snapshot, (section,))
        assert not evidence.current
        assert evidence.hashes == {section: None}
        assert len(harness.renders) == 1
        assert harness.renderers[0].closed
    finally:
        captures.close()


@pytest.mark.parametrize("change", ["snapshot", "palette", "variant"])
def test_changed_revision_during_batch_cannot_authorize_any_image(
    harness: SimpleNamespace, tmp_path: Path, change: str
) -> None:
    """Images produced while source state changes do not become trusted evidence."""
    captures, state, section = _session(harness, tmp_path)
    if change == "snapshot":
        harness.render_action = lambda: setattr(
            state, "snapshot", replace(state.snapshot, context_digest="edited")
        )
    elif change == "palette":
        harness.render_action = lambda: setattr(harness, "palette", "changed")
    else:
        # The boundary revision compares the output selection independently from
        # otherwise identical geometry. Avoid native variant ownership in this unit.
        harness.render_action = lambda: setattr(state, "variant", "other")
        captures.snapshot = lambda: state.snapshot
    try:
        evidence = captures.verify_captures(captures.snapshot(), (section,))
        assert not evidence.current
        assert evidence.hashes == {section: None}
        assert harness.renderers[0].closed
    finally:
        captures.close()


def test_forced_refresh_replaces_verified_hash(
    harness: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The latest successful native refresh replaces the retained hash for Save."""
    captures, _state, section = _session(harness, tmp_path)
    try:
        original = captures.preview(section)
        renderer = captures.renderer
        original_render = renderer.render

        def changed_pixels(row: Any, destination: Path) -> Path:
            """Write a different bounded native image fixture on explicit refresh."""
            path = original_render(row, destination)
            path.write_bytes(OTHER_PNG)
            return path

        monkeypatch.setattr(renderer, "render", changed_pixels)
        updated = captures.preview(section, refresh=True)
        assert updated.sha256 != original.sha256
        evidence = captures.verify_captures(captures.snapshot(), (section,))
        assert evidence.current
        assert evidence.hashes == {section: updated.sha256}
    finally:
        captures.close()


@pytest.mark.parametrize("phase", ["before", "after_batch"])
def test_unreadable_appearance_releases_renderer_and_downgrades_evidence(
    harness: SimpleNamespace, tmp_path: Path, phase: str
) -> None:
    """An unavailable palette prevents approval without blocking draft persistence."""
    captures, _state, section = _session(harness, tmp_path)
    try:
        captures.preview(section)
        other = replace(section, section_id="new")

        def unreadable() -> None:
            """Make the actual appearance reader fail at the selected boundary."""
            harness.palette_error = OSError("palette unavailable")

        if phase == "before":
            unreadable()
        else:
            harness.render_action = unreadable
        evidence = captures.verify_captures(captures.snapshot(), (section, other))
        assert not evidence.current
        assert evidence.hashes == {section: None, other: None}
        assert all(renderer.closed for renderer in harness.renderers)
        assert not captures.images
    finally:
        captures.close()


def test_unreadable_source_at_final_batch_boundary_propagates(
    harness: SimpleNamespace, tmp_path: Path
) -> None:
    """A source read failure must keep Save open rather than silently saving a draft."""
    captures, state, section = _session(harness, tmp_path)
    harness.render_action = lambda: setattr(
        state, "error", OSError("board unavailable")
    )
    try:
        with pytest.raises(OSError, match="board unavailable"):
            captures.verify_captures(captures.snapshot(), (section,))
        assert all(renderer.closed for renderer in harness.renderers)
        assert not captures.images
    finally:
        captures.close()


def test_failed_refresh_retains_failure_even_when_renderer_cleanup_raises(
    harness: SimpleNamespace, tmp_path: Path
) -> None:
    """Cleanup failure must not authorize a previously captured image on Save retry."""
    captures, _state, section = _session(harness, tmp_path)

    def failed_cleanup() -> None:
        """Model a native cleanup error after recording the renderer as closed."""
        raise RuntimeError("renderer cleanup failed")

    try:
        captures.preview(section)
        harness.render_error = RuntimeError("render failed")
        harness.close_action = failed_cleanup
        with pytest.raises(RuntimeError, match="renderer cleanup failed"):
            captures.preview(section, refresh=True)
        harness.render_error = None
        harness.close_action = None
        before = len(harness.renders)
        evidence = captures.verify_captures(captures.snapshot(), (section,))
        assert evidence.current and evidence.hashes == {section: None}
        assert len(harness.renders) == before
    finally:
        captures.close()


def test_variant_selection_change_invalidates_evidence_after_live_source_check(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Changing a selected output cannot silently restore old capture approval."""
    controls, state = _variant_controls(harness, monkeypatch)
    captures = harness.integration.CaptureSession(controls, tmp_path)
    try:
        snapshot = captures.snapshot()
        section = harness.matching.analyze(_configuration(harness), snapshot).sections[
            0
        ]
        captures.preview(section)
        state.session.output_variant = "B"
        evidence = captures.verify_captures(captures.snapshot(), (section,))
        assert not evidence.current
        assert evidence.hashes == {section: None}
        assert len(harness.renders) == 1
        assert harness.renderers[0].closed
        assert state.owners[0].closed
    finally:
        captures.close()


@pytest.mark.parametrize("phase", ["preview", "batch"])
def test_failed_capture_does_not_silently_restore_old_verified_hash(
    harness: SimpleNamespace, tmp_path: Path, phase: str
) -> None:
    """Failed evidence stays failed until a successful explicit capture replaces it."""
    captures, _state, section = _session(harness, tmp_path)
    try:
        if phase == "preview":
            captures.preview(section)
        harness.render_error = RuntimeError("native render failed")
        if phase == "preview":
            with pytest.raises(RuntimeError, match="native render failed"):
                captures.preview(section, refresh=True)
        else:
            failed = captures.verify_captures(captures.snapshot(), (section,))
            assert failed.current and failed.hashes == {section: None}
        assert harness.renderers[0].closed
        harness.render_error = None
        before = len(harness.renders)
        unchanged = captures.verify_captures(captures.snapshot(), (section,))
        assert unchanged.current and unchanged.hashes == {section: None}
        assert len(harness.renders) == before
        restored = captures.preview(section, refresh=True)
        assert captures.verify_captures(captures.snapshot(), (section,)).hashes == {
            section: restored.sha256
        }
    finally:
        captures.close()


@pytest.mark.parametrize("change", ["none_to_variant", "variant_to_none"])
def test_output_mode_transition_uses_matching_snapshot_and_render_board(
    harness: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    change: str,
) -> None:
    """Never render the live PCB using a detached output variant's snapshot."""
    controls, state = _variant_controls(harness, monkeypatch)
    if change == "none_to_variant":
        harness.parent._variant_controller = None
    captures = harness.integration.CaptureSession(controls, tmp_path)
    try:
        snapshot = captures.snapshot()
        section = harness.matching.analyze(_configuration(harness), snapshot).sections[
            0
        ]
        captures.preview(section)
        if change == "none_to_variant":
            harness.parent._variant_controller = SimpleNamespace(session=state.session)
        else:
            harness.parent._variant_controller = None
        current = captures.snapshot()
        section = harness.matching.analyze(_configuration(harness), current).sections[0]
        captures.preview(section)
        expected = "A" if change == "none_to_variant" else "B"
        assert harness.renderers[-1].board.variant == expected
        assert harness.renderers[0].closed
        assert all(owner.closed for owner in state.owners[:-1])
    finally:
        captures.close()
    assert all(owner.closed for owner in state.owners)
