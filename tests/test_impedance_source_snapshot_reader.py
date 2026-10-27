"""A fresh source proof may skip materialization, never freshness checks."""

from typing import Any

import pytest

from impedance.model import BoardSnapshot
from impedance.source_evidence import (
    SourceEvidence,
    SourceEvidenceUnavailable,
    SourceSnapshotChanged,
    SourceSnapshotReader,
)


def test_unchanged_reads_acquire_fresh_evidence_and_build_once() -> None:
    """Check fresh proof at every boundary while reusing one materialization."""
    reader = SourceSnapshotReader()
    calls = {"evidence": 0, "build": 0}

    def acquire() -> SourceEvidence:
        calls["evidence"] += 1
        return SourceEvidence(b"complete")

    def build() -> BoardSnapshot:
        calls["build"] += 1
        return BoardSnapshot(("F.Cu",), ())

    first = reader.read(acquire, build, owner_key=("board", 1))
    for _ in range(10):
        assert reader.read(acquire, build, owner_key=("board", 1)) is first
    assert calls == {"evidence": 12, "build": 1}
    assert reader.statistics.evidence_reads == 12
    assert reader.statistics.builds == 1
    assert reader.statistics.hits == 10


@pytest.mark.parametrize("change", ["evidence", "owner", "force"])
def test_changed_evidence_owner_or_force_rebuilds(change: str) -> None:
    """Rebuild when either source ownership, proof or a forced barrier changes."""
    reader = SourceSnapshotReader()
    evidence = [b"before"]
    build = lambda: BoardSnapshot(("F.Cu",), ())
    acquire = lambda: SourceEvidence(evidence[0])
    first = reader.read(acquire, build, owner_key=(1,))
    if change == "evidence":
        evidence[0] = b"after"
    result = reader.read(
        acquire,
        build,
        owner_key=(2,) if change == "owner" else (1,),
        force=change == "force",
    )
    assert result == first
    assert result is not first
    assert reader.statistics.builds == 2


def test_mutation_during_build_is_not_published_or_returned() -> None:
    """Discard obsolete materialization when its source changes during reading."""
    reader = SourceSnapshotReader()
    evidence = [b"before"]

    def build() -> BoardSnapshot:
        evidence[0] = b"after"
        return BoardSnapshot(("F.Cu",), ())

    with pytest.raises(SourceSnapshotChanged, match="changed"):
        reader.read(lambda: SourceEvidence(evidence[0]), build, owner_key=(1,))
    assert reader.statistics.invalidations >= 1
    assert (
        reader.read(
            lambda: SourceEvidence(evidence[0]),
            lambda: BoardSnapshot((), ()),
            owner_key=(1,),
        ).layers
        == ()
    )


@pytest.mark.parametrize("phase", ["acquire", "build", "postcheck"])
def test_errors_invalidate_previous_success(phase: str) -> None:
    """Propagate real errors and prevent reuse after failed reads."""
    reader = SourceSnapshotReader()
    old = reader.read(
        lambda: SourceEvidence(b"one"),
        lambda: BoardSnapshot(("F.Cu",), ()),
        owner_key=(1,),
    )
    reads = 0

    def acquire() -> SourceEvidence:
        nonlocal reads
        reads += 1
        if phase == "acquire" or (phase == "postcheck" and reads == 2):
            raise RuntimeError("native read failed")
        return SourceEvidence(b"two")

    def build() -> BoardSnapshot:
        if phase == "build":
            raise RuntimeError("native read failed")
        return BoardSnapshot(("F.Cu",), ())

    with pytest.raises(RuntimeError, match="native read failed"):
        reader.read(acquire, build, owner_key=(1,))
    fresh = reader.read(
        lambda: SourceEvidence(b"one"),
        lambda: BoardSnapshot(("F.Cu",), ()),
        owner_key=(1,),
    )
    assert fresh == old and fresh is not old


def test_unsupported_evidence_uses_original_builder_every_time() -> None:
    """Fall back without retaining unsupported evidence as a currentness proof."""
    reader = SourceSnapshotReader()

    def unsupported() -> SourceEvidence:
        raise SourceEvidenceUnavailable("unsupported binding")

    first = reader.read(unsupported, lambda: BoardSnapshot((), ()), owner_key=(1,))
    second = reader.read(unsupported, lambda: BoardSnapshot((), ()), owner_key=(1,))
    assert first is not second
    assert reader.statistics.fallbacks == 2
    assert reader.statistics.fallback_reasons == {"unsupported binding": 2}


def test_unsupported_postcheck_discards_candidate_and_uses_fresh_builder() -> None:
    """Discard the unproved candidate before taking the original fallback path."""
    reader = SourceSnapshotReader()
    calls = 0
    builds = 0

    def acquire() -> SourceEvidence:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise SourceEvidenceUnavailable("postcheck unavailable")
        return SourceEvidence(b"source")

    def build() -> BoardSnapshot:
        nonlocal builds
        builds += 1
        return BoardSnapshot((str(builds),), ())

    assert reader.read(acquire, build, owner_key=(1,)).layers == ("2",)
    assert reader.statistics.builds == 2
    assert reader.statistics.fallbacks == 1


@pytest.mark.parametrize("phase", ["acquire", "build", "postcheck"])
def test_closed_owner_aborts_before_any_further_native_operation(phase: str) -> None:
    """Stop at every operation boundary when the native owner closes."""
    reader = SourceSnapshotReader()
    calls: list[str] = []

    def acquire() -> SourceEvidence:
        calls.append("acquire")
        if phase == "acquire" or (phase == "postcheck" and len(calls) == 3):
            reader.close()
        return SourceEvidence(b"source")

    def build() -> BoardSnapshot:
        calls.append("build")
        if phase == "build":
            reader.close()
        return BoardSnapshot((), ())

    with pytest.raises(SourceSnapshotChanged):
        reader.read(acquire, build, owner_key=(1,))
    assert (
        calls
        == {
            "acquire": ["acquire"],
            "build": ["acquire", "build"],
            "postcheck": ["acquire", "build", "acquire"],
        }[phase]
    )
    with pytest.raises(SourceSnapshotChanged):
        reader.read(acquire, build, owner_key=(1,))


def test_nested_read_supersedes_outer_without_publishing_older_revision() -> None:
    """Preserve the newer nested result and reject obsolete outer work."""
    reader = SourceSnapshotReader()
    nested: list[BoardSnapshot] = []

    def acquire_outer() -> SourceEvidence:
        nested.append(
            reader.read(
                lambda: SourceEvidence(b"new"),
                lambda: BoardSnapshot(("new",), ()),
                owner_key=(1,),
            )
        )
        return SourceEvidence(b"old")

    with pytest.raises(SourceSnapshotChanged):
        reader.read(
            acquire_outer, lambda: pytest.fail("obsolete build"), owner_key=(1,)
        )
    assert (
        reader.read(
            lambda: SourceEvidence(b"new"),
            lambda: pytest.fail("nested state lost"),
            owner_key=(1,),
        )
        is nested[0]
    )


def test_nonprimitive_owner_key_is_uncached_and_not_retained() -> None:
    """Avoid retaining native wrappers or mutable keys inside the reader."""
    reader = SourceSnapshotReader()
    native_owner = object()
    first = reader.read(
        lambda: SourceEvidence(b"source"),
        lambda: BoardSnapshot((), ()),
        owner_key=native_owner,
    )
    second = reader.read(
        lambda: SourceEvidence(b"source"),
        lambda: BoardSnapshot((), ()),
        owner_key=native_owner,
    )
    assert first is not second
    assert reader.statistics.fallbacks == 2


@pytest.mark.parametrize("payload", [[], {}, bytearray(b"x"), "x"])
def test_evidence_requires_exact_immutable_bytes(payload: Any) -> None:
    """Reject mutable or opaque evidence payloads."""
    with pytest.raises(TypeError):
        SourceEvidence(payload)


def test_statistics_distinguish_forced_postchecked_failed_and_superseded_reads() -> (
    None
):
    """Report real work and failure modes without counting capability fallback as failure."""
    reader = SourceSnapshotReader()
    acquire = lambda: SourceEvidence(b"source")
    build = lambda: BoardSnapshot((), ())
    reader.read(acquire, build, owner_key=(1,))
    reader.read(acquire, build, owner_key=(1,), force=True)
    assert reader.statistics.forced_reads == 1
    assert reader.statistics.postchecks == 2
    assert reader.statistics.failures == 0
    assert reader.statistics.superseded == 0

    def unsupported() -> SourceEvidence:
        raise SourceEvidenceUnavailable("unsupported binding")

    reader.read(unsupported, build, owner_key=(1,))
    assert reader.statistics.fallbacks == 1
    assert reader.statistics.failures == 0

    def nested() -> SourceEvidence:
        reader.read(acquire, build, owner_key=(1,))
        return SourceEvidence(b"obsolete")

    with pytest.raises(SourceSnapshotChanged):
        reader.read(nested, build, owner_key=(1,))
    assert reader.statistics.superseded == 1
    assert reader.statistics.failures == 1
    assert reader.statistics.postchecks == 3
    reader.close()
    with pytest.raises(SourceSnapshotChanged):
        reader.read(acquire, build, owner_key=(1,), force=True)
    assert reader.statistics.forced_reads == 2
    assert reader.statistics.failures == 2
    assert reader.statistics.superseded == 1
    assert reader.statistics.postchecks == 3


@pytest.mark.parametrize("change", ["close", "invalidate", "nested_read"])
def test_lifetime_guard_rejects_owner_changes_before_additional_native_reads(
    change: str,
) -> None:
    """Guard acquisition closures before post-proof native metadata inspection."""
    reader = SourceSnapshotReader()
    calls: list[str] = []
    nested: list[BoardSnapshot] = []

    def acquire() -> SourceEvidence:
        guard = reader.lifetime_guard()
        calls.append("acquire")
        if change == "close":
            reader.close()
        elif change == "invalidate":
            reader.invalidate()
        else:
            nested.append(
                reader.read(
                    lambda: SourceEvidence(b"new"),
                    lambda: BoardSnapshot(("new",), ()),
                    owner_key=(1,),
                )
            )
        guard()
        calls.append("native metadata")
        return SourceEvidence(b"obsolete")

    with pytest.raises(SourceSnapshotChanged):
        reader.read(acquire, lambda: pytest.fail("obsolete build"), owner_key=(1,))
    assert calls == ["acquire"]
    assert reader.statistics.superseded == 1
    assert reader.statistics.failures == 1
    if nested:
        assert (
            reader.read(
                lambda: SourceEvidence(b"new"),
                lambda: pytest.fail("nested slot lost"),
                owner_key=(1,),
            )
            is nested[0]
        )


def test_lifetime_guard_rejects_an_already_closed_owner() -> None:
    """Prevent acquisition closures from starting native reads after owner close."""
    reader = SourceSnapshotReader()
    reader.close()
    with pytest.raises(SourceSnapshotChanged):
        reader.lifetime_guard()
