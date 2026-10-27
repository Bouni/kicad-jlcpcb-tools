"""Challenge immutable PNG ownership separately from all approval records."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from impedance import capture_cache, matching, service
from tests.test_impedance_export_capture_proof import _plan
from tests.test_impedance_report_pipeline import _png as _fixture_png


def _png(width: int = 120, height: int = 60, color: bytes = b"\x40\x80\xc0") -> bytes:
    """Use enough pixels for the real route-frame padding contract."""
    return _fixture_png(width, height, color)


def _key(cache: capture_cache.OwnedCaptureCache) -> capture_cache.CaptureKey:
    """Bind a real source and section to an independently checked appearance."""
    plan = _plan()
    return capture_cache.capture_key(
        cache,
        "/board-a.kicad_pcb",
        plan.snapshot,
        "theme-a",
        None,
        plan.sections[0],
        dimensions=(120, 60),
    )


@pytest.mark.parametrize(
    "change",
    [
        {"board_identity": "/board-b.kicad_pcb"},
        {"source_digest": "0" * 64},
        {"appearance": "theme-b"},
        {"output_variant": "Default"},
        {"capture_policy": matching.CAPTURE_POLICY_REVISION + 1},
        {"spacing_policy": (999, 999)},
        {"dimensions": (400, 210)},
        {"viewport_nm": (0, 0, 1, 1)},
        {"kind": "spacing"},
    ],
)
def test_owned_png_requires_the_entire_current_pixel_identity(
    change: dict[str, Any],
) -> None:
    """An unchanged label or image ID cannot hide a different capture contract."""
    cache = capture_cache.OwnedCaptureCache()
    key = _key(cache)
    image = service.CapturedImage.from_bytes(
        _png(), viewport_nm=_key(cache).viewport_nm
    )
    cache.put(key, image)
    assert cache.get(replace(key, **change)) is None
    assert cache.get(key) == image


@pytest.mark.parametrize(
    "fault", ["bytes", "hash", "dimensions", "viewport", "valid_viewport"]
)
def test_corrupt_owned_png_is_discarded_instead_of_becoming_proof(fault: str) -> None:
    """Rebuild the immutable capture and check its recorded metadata on lookup."""
    cache = capture_cache.OwnedCaptureCache()
    key = _key(cache)
    cache.put(
        key,
        service.CapturedImage.from_bytes(_png(), viewport_nm=_key(cache).viewport_nm),
    )
    owned = cache._images[key]
    changes = {
        "bytes": {"data": _png(color=b"\x90\x20\x40")},
        "hash": {"sha256": "0" * 64},
        "dimensions": {"dimensions": (1, 1)},
        "viewport": {"viewport_nm": (0, 0, -1, 1)},
        "valid_viewport": {"viewport_nm": (0, 0, 1, 1)},
    }
    cache._images[key] = replace(owned, **changes[fault])
    assert cache.get(key) is None
    assert key not in cache._images
    assert not hasattr(cache, "approval")


def test_owned_png_lru_accounts_for_both_original_and_composite() -> None:
    """Eviction and oversized fallback must include independent comparison bytes."""
    original = service.CapturedImage.from_bytes(_png(color=b"\x90\x20\x40"))
    cache = capture_cache.OwnedCaptureCache()
    image = service.CapturedImage.from_bytes(
        _png(), original=original, viewport_nm=_key(cache).viewport_nm
    )
    cache.max_bytes = image.byte_size * 2
    keys = tuple(
        replace(_key(cache), board_identity=f"board-{index}") for index in range(3)
    )
    for key in keys:
        cache.put(key, image)
    assert cache.byte_size == image.byte_size * 2
    assert cache.get(keys[0]) is None
    assert cache.get(keys[-1]) == image
    cache.max_bytes = image.byte_size - 1
    cache.put(keys[-1], image)
    assert cache.get(keys[-1]) is None


def test_owned_png_cache_has_an_independent_entry_bound() -> None:
    """Tiny images cannot retain an unbounded number of historical sources."""
    cache = capture_cache.OwnedCaptureCache(max_entries=2)
    image = service.CapturedImage.from_bytes(
        _png(), viewport_nm=_key(cache).viewport_nm
    )
    keys = tuple(
        replace(_key(cache), board_identity=f"board-{index}") for index in range(3)
    )
    for key in keys:
        cache.put(key, image)
    assert len(cache._images) == 2
    assert cache.get(keys[0]) is None
    cache.discard(keys[-1])
    assert cache.byte_size == image.byte_size


@pytest.mark.parametrize("kind", ["section", "spacing"])
def test_well_formed_wrong_sized_png_cannot_satisfy_requested_dimensions(
    kind: str,
) -> None:
    """Self-consistent owned metadata is insufficient for a different request."""
    cache = capture_cache.OwnedCaptureCache()
    key = replace(_key(cache), kind=kind, dimensions=(800, 420))
    cache.put(key, service.CapturedImage.from_bytes(_png(800, 421)))
    assert cache.get(key) is None


def test_well_formed_trace_png_requires_the_requested_physical_viewport() -> None:
    """A valid PNG with an unrelated physical transform is a cold miss."""
    cache = capture_cache.OwnedCaptureCache()
    key = _key(cache)
    image = service.CapturedImage.from_bytes(_png(), viewport_nm=(0, 0, 1, 1))
    cache.put(key, image)
    assert cache.get(key) is None


@pytest.mark.parametrize(
    "kind,inputs", [("section", None), ("spacing", None), ("unknown", None)]
)
def test_owned_pixels_cannot_satisfy_unrelated_key_inputs(
    kind: str, inputs: Any
) -> None:
    """The role and immutable input object must agree, beyond sealed PNG metadata."""
    cache = capture_cache.OwnedCaptureCache()
    key = replace(_key(cache), kind=kind, inputs=inputs)
    cache.put(
        key,
        service.CapturedImage.from_bytes(_png(), viewport_nm=_key(cache).viewport_nm),
    )
    assert cache.get(key) is None


def test_spacing_original_must_match_detail_dimensions_at_cache_and_consumer(
    tmp_path: Path,
) -> None:
    """An existing Original cannot bypass the physical detail-panel contract."""
    from impedance.spacing_review import spacing_review_entries
    from tests.test_impedance_export_capture_proof import _approved_spacing_plan

    plan = _approved_spacing_plan()
    entry = spacing_review_entries(plan.config, plan.snapshot, plan.analysis.sections)[
        0
    ]
    cache = capture_cache.OwnedCaptureCache()
    key = capture_cache.capture_key(
        cache,
        "/board.kicad_pcb",
        plan.snapshot,
        "theme",
        None,
        entry,
        dimensions=(entry.width_px, entry.height_px),
    )
    width, height = entry.composition.detail_rect[2:]
    original = service.CapturedImage.from_bytes(
        _png(width, height + 1), viewport_nm=entry.viewport_nm
    )
    combined = service.CapturedImage.from_bytes(
        _png(entry.width_px, entry.height_px), original=original
    )
    cache.put(key, combined)
    assert cache.get(key) is None
    with pytest.raises(service.ValidationError, match="unexpected dimensions"):
        service.attach_spacing_original(
            combined, object(), entry, tmp_path / "unused.png"
        )


@pytest.mark.parametrize("identity", ["", "   "])
def test_empty_export_identity_cannot_become_a_cwd_cache_identity(
    tmp_path: Path, identity: str
) -> None:
    """Stable source and appearance callbacks cannot legitimize an empty path."""
    plan = _plan()
    with pytest.raises(service.ValidationError, match="live source and appearance"):
        service.export_reports(
            plan,
            object(),
            object(),
            tmp_path,
            renderer_factory=lambda _board, _pcbnew: None,
            capture_cache=capture_cache.OwnedCaptureCache(),
            capture_identity=(identity, None),
            source_context=lambda: plan.snapshot,
            appearance_context=lambda: "theme",
        )


def test_cache_cannot_silently_enable_reuse_without_live_provenance(
    tmp_path: Path,
) -> None:
    """An export caller must supply source and appearance boundary callbacks."""
    with pytest.raises(service.ValidationError, match="live source and appearance"):
        service.export_reports(
            _plan(),
            object(),
            object(),
            tmp_path,
            renderer_factory=lambda _board, _pcbnew: None,
            capture_cache=capture_cache.OwnedCaptureCache(),
            capture_identity=("/board.kicad_pcb", None),
        )


def test_outline_free_renderer_capture_roundtrips_through_owned_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real renderer's route-derived transform remains reusable without an outline."""
    from copy import deepcopy
    from types import SimpleNamespace

    from impedance import render
    from tests.test_impedance_render import FakeBoard, pcbnew

    plan = _plan()
    assert plan.snapshot.board_bounds is None
    monkeypatch.setattr(
        render,
        "copy_for_render",
        lambda board, _pcbnew: SimpleNamespace(
            board=deepcopy(board), close=lambda: None
        ),
    )
    destination = tmp_path / "section.png"
    destination.write_bytes(_png(800, 420))
    with render.SectionRenderer(FakeBoard(), pcbnew()) as renderer:
        image = service.load_rendered_capture(destination, renderer, plan.sections[0])
    assert image.viewport_nm is not None
    cache = capture_cache.OwnedCaptureCache()
    key = capture_cache.capture_key(
        cache, "/board.kicad_pcb", plan.snapshot, "theme", None, plan.sections[0]
    )
    cache.put(key, image)
    restored = cache.get(key)
    assert restored is not None
    assert restored.viewport_nm == image.viewport_nm
    assert restored.sha256 == image.sha256


def test_source_digest_adopts_equal_snapshot_without_repeated_deep_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One equal replacement keeps its digest and makes subsequent reads constant."""
    snapshot = _plan().snapshot
    equal = replace(snapshot)
    cache = capture_cache.OwnedCaptureCache()
    digest = cache.source_digest(snapshot)
    compared: list[object] = []
    snapshot_type = type(snapshot)
    original_equal = snapshot_type.__eq__

    def compare(left: Any, right: object) -> bool:
        """Count the real structural comparison rather than substitute equality."""
        compared.append(right)
        return original_equal(left, right)

    monkeypatch.setattr(snapshot_type, "__eq__", compare)
    assert cache.source_digest(equal) == digest
    assert len(compared) == 1
    compared.clear()
    for _attempt in range(5):
        assert cache.source_digest(equal) == digest
    assert compared == []
    assert cache._source is equal


def test_source_digest_failure_preserves_the_last_coherent_pair() -> None:
    """A failed canonical derivation cannot bind old bytes to invalid new input."""
    snapshot = _plan().snapshot
    cache = capture_cache.OwnedCaptureCache()
    digest = cache.source_digest(snapshot)
    invalid = replace(snapshot, traces=("not a trace",))
    for _attempt in range(2):
        with pytest.raises(ValueError, match="Every board trace"):
            cache.source_digest(invalid)
        assert cache._source is snapshot
        assert cache._source_digest == digest
    assert cache.source_digest(snapshot) == digest
