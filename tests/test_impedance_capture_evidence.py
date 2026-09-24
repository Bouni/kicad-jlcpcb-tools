"""Keep reviewed image evidence without redoing per-image source validation."""

from base64 import b64decode
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
    """Cold restoration checks source twice total, including the caller's read."""
    captures, state, section = _session(harness, tmp_path)
    sections = tuple(replace(section, section_id=f"image-{i}") for i in range(count))
    try:
        evidence = captures.verify_captures(captures.snapshot(), sections)
        assert evidence.current
        assert all(evidence.hashes.values())
        assert set(evidence.hashes) == set(sections)
        assert state.reads == 2
        assert len(harness.renders) == count
        assert len(harness.renderers) == 1
        repeated = captures.verify_captures(captures.snapshot(), sections)
        assert repeated == evidence
        assert state.reads == 3
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
