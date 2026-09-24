"""Review hashes preserve stored proof while serializing immutable geometry once."""

from __future__ import annotations

from collections import Counter
import dataclasses
from dataclasses import dataclass, replace
from typing import Any, Optional

import pytest

from impedance import review_tracking as tracking
from impedance.model import (
    BoardSnapshot,
    CopperFeature,
    LayerSettings,
    Section,
    Specification,
    Trace,
)


@dataclass(frozen=True)
class _FutureMetadata:
    """Model a newly added field containing another nested dataclass."""

    label: str
    copper: CopperFeature


@dataclass(frozen=True)
class _FutureTrace(Trace):
    metadata: Optional[_FutureMetadata] = None


@dataclass(frozen=True)
class _FutureSnapshot(BoardSnapshot):
    metadata: tuple[_FutureMetadata, ...] = ()


@dataclass(frozen=True)
class _FutureSection(Section):
    metadata: tuple[_FutureMetadata, ...] = ()


@dataclass(frozen=True)
class _FutureValue:
    value: object


@dataclass(frozen=True)
class _SnapshotWithFutureValue(BoardSnapshot):
    metadata: Optional[_FutureValue] = None


def _geometry(
    *, future: bool = False
) -> tuple[BoardSnapshot, Specification, tuple[Section, ...]]:
    """Include native arc perimeter proof, polygon holes and all board context."""
    outer = (
        (-100_000, -100_000),
        (2_100_000, -100_000),
        (2_100_000, 1_100_000),
        (-100_000, 1_100_000),
    )
    hole = (
        (300_000, 100_000),
        (500_000, 100_000),
        (500_000, 300_000),
        (300_000, 300_000),
    )
    copper = CopperFeature(
        "polygon-pad",
        "SIGNAL_A",
        "F.Cu",
        "pad",
        "polygon",
        (1_000_000, 500_000),
        2_200_000,
        1_200_000,
        22.5,
        0,
        ((outer, hole),),
    )
    metadata = _FutureMetadata("retained future content", copper)
    trace = Trace(
        "arc",
        "F.Cu",
        "SIGNAL_A",
        150_000,
        ((0, 0), (1_000_000, 500_000), (2_000_000, 0)),
        True,
        ((outer,),),
    )
    if future:
        trace = _FutureTrace(**vars(trace), metadata=metadata)
    traces = (
        trace,
        Trace(
            "straight",
            "F.Cu",
            "SIGNAL_B",
            175_000,
            ((0, 2_000_000), (2_000_000, 2_000_000)),
        ),
    )
    snapshot = BoardSnapshot(
        ("F.Cu", "In1.Cu", "B.Cu"),
        traces,
        "native-context",
        ("RF", "Default"),
        (("SIGNAL_A", ("RF", "Default")), ("SIGNAL_B", ("RF",))),
        "native-net-settings",
        "",
        (("SIGNAL_B", "SIGNAL_A"),),
        "",
        (copper,),
        (-1_000_000, -1_000_000, 3_000_000, 3_000_000),
    )
    sections = tuple(
        Section(
            f"row-{index}",
            "rf",
            "F.Cu",
            item.width_nm,
            (item,),
            (-100_000, -100_000, 2_100_000, 2_100_000),
            (item.net,),
        )
        for index, item in enumerate(traces)
    )
    if future:
        snapshot = _FutureSnapshot(**vars(snapshot), metadata=(metadata,))
        sections = tuple(
            _FutureSection(**vars(section), metadata=(metadata,))
            for section in sections
        )
    spec = Specification(
        "rf",
        "RF",
        "50",
        "single_ended",
        "RF",
        (LayerSettings("F.Cu", ("B.Cu", "In1.Cu")),),
    )
    return snapshot, spec, sections


def _fingerprints(
    snapshot: BoardSnapshot, spec: Specification, sections: tuple[Section, ...]
) -> tuple[str, tuple[str, ...], str]:
    """Exercise complete production source, image and approval hash construction."""
    source = tracking.source_fingerprint(snapshot)
    return (
        source,
        tuple(
            tracking.capture_fingerprint(source, spec, section, "a" * 64)
            for section in sections
        ),
        tracking.layer_fingerprint(source, spec, "F.Cu", sections),
    )


# Captured from the pre-optimization implementation. Existing persisted approval
# must retain these exact fingerprints, including every native perimeter field.
_SAVED_FINGERPRINTS = {
    False: (
        "25d1b89fc795a40e3aa79c4959ddbf99a33681753bc81b40f0a4a68701efeb00",
        (
            "36508e9061aa20d230ab05551ddf4ebb752dfed87b4ddfa62f48890df3424e32",
            "58603ddb2a457a6da9be5fb4216324e32bf87ddad16194fe9ddb51e85e8b2b59",
        ),
        "7cd59398f4ce4c5c1198da654f2f95a23178ac9c8a5dfca7ecfa363655314465",
    ),
    True: (
        "31f98c86b3fb70e36cfc81765ffe252856e080e95d2a6df6fa41d5a03af089cc",
        (
            "4d75d1ecab8e9702de3f7db16c1ba3ee594fc8ea78ee940fb9e0786e0085efe7",
            "fd484f195a0a2996e705f41e6814a5f14f71131ae27ad62d1e02215c4c99d221",
        ),
        "ed3521faccdb29714cc79d35370a4cd2b277135a754b1e401d6f701af0ab0b8c",
    ),
}


@pytest.mark.parametrize(
    "future", [False, True], ids=["native-fields", "future-fields"]
)
@pytest.mark.parametrize(
    "reordered", [False, True], ids=["original", "canonical-order"]
)
def test_saved_source_image_and_layer_hashes_remain_exact(
    future: bool, reordered: bool
) -> None:
    """Reordering and shallow serialization cannot invalidate saved review history."""
    snapshot, spec, sections = _geometry(future=future)
    expected = _SAVED_FINGERPRINTS[future]
    if reordered:
        traces = tuple(
            replace(trace, points=tuple(reversed(trace.points)))
            for trace in reversed(snapshot.traces)
        )
        snapshot = replace(
            snapshot,
            traces=traces,
            net_classes=tuple(reversed(snapshot.net_classes)),
            net_class_memberships=tuple(
                (net, tuple(reversed(names)))
                for net, names in reversed(snapshot.net_class_memberships)
            ),
            differential_pairs=tuple(
                tuple(reversed(pair)) for pair in snapshot.differential_pairs
            ),
        )
        by_id = {trace.trace_id: trace for trace in traces}
        sections = tuple(
            replace(
                section,
                traces=tuple(
                    by_id[trace.trace_id] for trace in reversed(section.traces)
                ),
                net_names=tuple(reversed(section.net_names)),
            )
            for section in sections
        )
    assert _fingerprints(snapshot, spec, sections) == expected


@pytest.mark.parametrize("operation", ["source", "image", "layer"])
def test_fingerprinting_visits_each_selected_trace_record_once(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """Normalization must not serialize nested geometry only to discard it."""
    snapshot, spec, sections = _geometry()
    source = tracking.source_fingerprint(snapshot)
    visits: Counter[str] = Counter()
    original = dataclasses.fields

    def fields(record: Any) -> tuple[dataclasses.Field[Any], ...]:
        """Count real dataclass traversal without replacing geometry or the hash."""
        if isinstance(record, Trace):
            visits[record.trace_id] += 1
        return original(record)

    monkeypatch.setattr(dataclasses, "fields", fields)
    monkeypatch.setattr(tracking, "fields", fields, raising=False)
    if operation == "source":
        tracking.source_fingerprint(snapshot)
    elif operation == "image":
        tracking.capture_fingerprint(source, spec, sections[0], "a" * 64)
    else:
        tracking.layer_fingerprint(source, spec, "F.Cu", sections)
    selected = snapshot.traces if operation != "image" else sections[0].traces
    assert visits == Counter({trace.trace_id: 1 for trace in selected})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_future_nested_dataclass_still_rejects_nonfinite_json(value: float) -> None:
    """The dataclass fallback cannot weaken canonical JSON's finite-number rule."""
    snapshot, _spec, _sections = _geometry()
    invalid = _SnapshotWithFutureValue(**vars(snapshot), metadata=_FutureValue(value))
    with pytest.raises(ValueError):
        tracking.source_fingerprint(invalid)


def test_future_nested_dataclass_still_rejects_unsupported_object_leaf() -> None:
    """Unknown objects must fail instead of acquiring an unstable representation."""
    snapshot, _spec, _sections = _geometry()
    invalid = _SnapshotWithFutureValue(
        **vars(snapshot), metadata=_FutureValue(object())
    )
    with pytest.raises(TypeError):
        tracking.source_fingerprint(invalid)
