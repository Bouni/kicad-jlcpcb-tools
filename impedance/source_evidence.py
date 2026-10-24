"""Fresh, read-only source evidence and owner-scoped immutable snapshot reuse.

A reader never caches freshness. Every read acquires complete evidence again;
only the expensive immutable snapshot materialization can be reused. Native
objects belong to the caller and are never retained by evidence or the reader.
"""

from collections.abc import Callable
from dataclasses import dataclass, field, fields
import marshal
import math
from pathlib import Path
import struct
from typing import Any, Optional

from . import pcbnew_adapter as adapter
from .model import BoardSnapshot, CopperFeature, Trace
from .net_metadata import netclass_context, pairing_context
from .project_context import ProjectContextError, project_context_records
from .render_context import CONTEXT_POLICY_VERSION
from .text_context import TextContextError, text_context_records

_EVIDENCE_VERSION = 1
_MAX_EVIDENCE_BYTES = 32 * 1024 * 1024
_SNAPSHOT_FIELDS = (
    "layers",
    "traces",
    "context_digest",
    "net_classes",
    "net_class_memberships",
    "net_class_context_digest",
    "net_class_error",
    "differential_pairs",
    "differential_pair_error",
    "copper_features",
    "board_bounds",
)
_TRACE_FIELDS = (
    "trace_id",
    "layer",
    "net",
    "width_nm",
    "points",
    "is_native_arc",
    "copper_polygons",
    "native_arc_midpoint",
    "native_arc_center",
    "native_arc_radius_nm",
)
_FEATURE_FIELDS = (
    "feature_id",
    "net",
    "layer",
    "kind",
    "shape",
    "center",
    "size_x_nm",
    "size_y_nm",
    "rotation_deg",
    "corner_radius_nm",
    "polygons",
    "footprint_id",
    "pad_number",
)


class SourceEvidenceUnavailable(Exception):
    """Use the original reader; never mistake admission failure for absent copper.

    This deliberately does not inherit RuntimeError: native geometry readers
    tolerate RuntimeError by omitting unavailable optional geometry. A failed
    evidence admission must instead disable reuse for the entire source read.
    """


class SourceSnapshotChanged(adapter.BoardSnapshotError):
    """The source or its owner changed while a read was in progress."""


@dataclass(frozen=True)
class SourceEvidence:
    """An exact, bounded primitive byte stream, never native borrowed state."""

    payload: bytes

    def __post_init__(self) -> None:
        """Reject mutable, opaque and oversized evidence payloads."""
        if type(self.payload) is not bytes:
            raise TypeError("Source evidence must contain immutable bytes.")
        if len(self.payload) > _MAX_EVIDENCE_BYTES:
            raise SourceEvidenceUnavailable("source evidence exceeds its memory limit")


@dataclass
class SourceSnapshotStatistics:
    """Count actual work, including cold, forced, failed and fallback reads."""

    evidence_reads: int = 0
    builds: int = 0
    hits: int = 0
    misses: int = 0
    fallbacks: int = 0
    invalidations: int = 0
    failures: int = 0
    superseded: int = 0
    forced_reads: int = 0
    postchecks: int = 0
    fallback_reasons: dict[str, int] = field(default_factory=dict)


def _primitive(value: object) -> bool:
    """Do not retain opaque objects, native wrappers, mutable containers or NaNs."""
    return type(value) in (str, bytes, int, bool, type(None)) or (
        type(value) is tuple and all(_primitive(part) for part in value)
    )


class SourceSnapshotReader:
    """Keep one successful snapshot within a caller-owned native lifetime."""

    def __init__(self) -> None:
        self.statistics = SourceSnapshotStatistics()
        self._generation = 0
        self._closed = False
        self._owner_key: object = None
        self._evidence: Optional[SourceEvidence] = None
        self._snapshot: Optional[BoardSnapshot] = None

    def _discard(self) -> None:
        """Discard a slot without superseding the currently executing read."""
        self._owner_key = None
        self._evidence = None
        self._snapshot = None
        self.statistics.invalidations += 1

    def invalidate(self) -> None:
        """Supersede pending reads and discard the previous successful pair."""
        self._generation += 1
        self._discard()

    def close(self) -> None:
        """Release all owned immutable state before the native owner closes."""
        self._closed = True
        self.invalidate()

    def _check(self, generation: int) -> None:
        """Reject stale callbacks before invoking another native operation."""
        if self._generation != generation:
            self.statistics.superseded += 1
            raise SourceSnapshotChanged(
                "The source snapshot owner changed during reading."
            )
        if self._closed:
            raise SourceSnapshotChanged(
                "The source snapshot owner changed during reading."
            )

    def lifetime_guard(self) -> Callable[[], None]:
        """Guard native operation boundaries inside an acquisition closure.

        Capture this before acquiring source evidence and check it before any
        subsequent native getters. It proves the owner's lifetime only; callers
        must still obtain fresh complete source evidence on every read.
        """
        generation = self._generation
        self._check(generation)

        def check() -> None:
            """Reject closure continuation after close or nested replacement."""
            self._check(generation)

        return check

    def _acquire(self, acquire: Callable[[], SourceEvidence]) -> SourceEvidence:
        self.statistics.evidence_reads += 1
        evidence = acquire()
        if type(evidence) is not SourceEvidence:
            raise SourceEvidenceUnavailable("source evidence has an unsupported schema")
        return evidence

    def _build(self, build: Callable[[], BoardSnapshot]) -> BoardSnapshot:
        self.statistics.builds += 1
        return build()

    def _fallback(
        self, build: Callable[[], BoardSnapshot], generation: int, reason: str
    ) -> BoardSnapshot:
        self._check(generation)
        self._discard()
        self.statistics.fallbacks += 1
        reasons = self.statistics.fallback_reasons
        reason = reason[:256]
        if reason not in reasons and len(reasons) >= 32:
            reason = "other unsupported capabilities"
        reasons[reason] = reasons.get(reason, 0) + 1
        snapshot = self._build(build)
        self._check(generation)
        return snapshot

    def read(
        self,
        acquire: Callable[[], SourceEvidence],
        build: Callable[[], BoardSnapshot],
        *,
        owner_key: object,
        force: bool = False,
    ) -> BoardSnapshot:
        """Acquire fresh proof, construct on misses, and verify before publication.

        Capability failures use a fresh uncached build. Actual source read errors
        propagate. A source change during construction never returns its obsolete
        candidate. Nested reads supersede outer reads, preserving the inner slot.
        """
        if force:
            self.statistics.forced_reads += 1
        if self._closed:
            self.statistics.failures += 1
            raise SourceSnapshotChanged("The source snapshot owner is closed.")
        self._generation += 1
        generation = self._generation
        try:
            if not _primitive(owner_key):
                return self._fallback(
                    build, generation, "unsupported snapshot owner key"
                )
            if owner_key != self._owner_key:
                self._discard()
            try:
                before = self._acquire(acquire)
            except SourceEvidenceUnavailable as error:
                return self._fallback(build, generation, str(error))
            self._check(generation)
            if (
                not force
                and self._snapshot is not None
                and self._evidence == before
                and self._owner_key == owner_key
            ):
                self.statistics.hits += 1
                return self._snapshot
            self.statistics.misses += 1
            snapshot = self._build(build)
            self._check(generation)
            try:
                self.statistics.postchecks += 1
                after = self._acquire(acquire)
            except SourceEvidenceUnavailable as error:
                # The first candidate has no complete post-build proof. Discard
                # it and preserve the original uncached acquisition semantics.
                return self._fallback(build, generation, str(error))
            self._check(generation)
            if before != after:
                self._discard()
                raise SourceSnapshotChanged(
                    "The source changed during snapshot construction."
                )
            self._owner_key, self._evidence, self._snapshot = owner_key, after, snapshot
            return snapshot
        except BaseException:
            self.statistics.failures += 1
            if self._generation == generation:
                self._discard()
            raise


class _EvidenceWriter:
    """Encode typed exact values without ambiguous separators or decimal floats."""

    def __init__(self) -> None:
        self.data = bytearray()

    def add(self, value: Any) -> None:
        """Encode audited primitive values in C with exact binary64 floats.

        Marshal format 2 has no reference-sharing encoding, so object identity
        and string interning cannot change proof bytes. This is an in-process
        encoder only: evidence is never persisted or unmarshalled. Every caller
        projects native state to explicit primitives before reaching this seam.
        """
        if type(value) not in (str, bytes, int, bool, float, tuple, list, type(None)):
            raise SourceEvidenceUnavailable("unsupported source evidence value")
        try:
            encoded = marshal.dumps(value, 2)
        except (TypeError, ValueError) as error:
            raise SourceEvidenceUnavailable(
                "unsupported source evidence value"
            ) from error
        if len(self.data) + len(encoded) + 8 > _MAX_EVIDENCE_BYTES:
            raise SourceEvidenceUnavailable("source evidence exceeds its memory limit")
        self.data.extend(struct.pack(">Q", len(encoded)))
        self.data.extend(encoded)


def _feature_evidence(
    writer: _EvidenceWriter,
    board: Any,
    pcbnew_module: Any,
    layers: dict[str, int],
    tracks: tuple[Any, ...],
) -> None:
    """Read precise effective copper inputs, including runtime flash decisions.

    These are the same authoritative getters used by the uncached adapter. Raw
    serialization cannot stand in for layer-dependent positions, sizes, shapes,
    native binary64 orientation, runtime connectivity or effective perimeters.
    Getter failures that make a feature absent are recorded as outcomes; failures
    outside the adapter's documented omission paths still propagate.
    """
    for item in tracks:
        if not adapter._is_via(item, pcbnew_module):
            continue
        center: Optional[tuple[int, int]] = None
        layer_records: list[tuple[Any, ...]] = []
        for layer_id in layers.values():
            if not adapter._item_on_layer(item, layer_id):
                layer_records.append((False,))
                continue
            try:
                width = int(item.GetWidth(layer_id))
                if center is None:
                    center = adapter._point(item.GetPosition())
                layer_records.append((True, width, center))
            except adapter._FLASH_READ_ERRORS:
                layer_records.append((True, "omitted-via"))
        writer.add(
            (
                "via",
                adapter._feature_uuid(item),
                str(item.GetNetname()) if hasattr(item, "GetNetname") else "",
                tuple(layer_records),
            )
        )
    for footprint in board.GetFootprints():
        writer.add(("footprint", adapter._footprint_id(footprint)))
        for pad in footprint.Pads() if hasattr(footprint, "Pads") else ():
            number_getter = getattr(pad, "GetNumber", None)
            pad_rotation: Optional[float] = None
            layer_records = []
            for layer_id in layers.values():
                if not adapter._item_on_layer(pad, layer_id):
                    layer_records.append((False,))
                    continue
                try:
                    size = pad.GetSize(layer_id)
                    size_x, size_y = int(size.x), int(size.y)
                    pad_center = adapter._point(pad.ShapePos(layer_id))
                    shape = adapter._pad_shape_name(
                        pad.GetShape(layer_id), pcbnew_module
                    )
                    rotation = (
                        adapter._pad_rotation_deg(pad)
                        if pad_rotation is None
                        else pad_rotation
                    )
                    if math.isfinite(rotation):
                        pad_rotation = rotation
                    radius = (
                        int(pad.GetRoundRectCornerRadius(layer_id))
                        if shape == "roundrect"
                        else 0
                    )
                    polygons = (
                        adapter._copper_polygons(pad, layer_id, pcbnew_module)
                        if shape == "polygon"
                        else ()
                    )
                    layer_records.append(
                        (
                            True,
                            size_x,
                            size_y,
                            pad_center,
                            shape,
                            rotation,
                            radius,
                            polygons,
                        )
                    )
                except adapter._FLASH_READ_ERRORS:
                    layer_records.append((True, "omitted-pad"))
            writer.add(
                (
                    "pad",
                    adapter._feature_uuid(pad),
                    str(pad.GetNetname()) if hasattr(pad, "GetNetname") else "",
                    str(number_getter()) if callable(number_getter) else "",
                    tuple(layer_records),
                )
            )


def acquire_source_evidence(
    board: Any,
    pcbnew_module: Any,
    *,
    netclass_source: Any = None,
) -> SourceEvidence:
    """Freshly read every dependency of the current uncached snapshot schema.

    This correctness-first primitive prototype deliberately reads effective
    copper, shown text, project bytes and zone state each time. It does not use
    timestamps, listeners, file mtimes or a previously calculated digest as proof
    that a native board is unchanged. No full formatter touches the live board.
    """
    for model, expected in (
        (BoardSnapshot, _SNAPSHOT_FIELDS),
        (Trace, _TRACE_FIELDS),
        (CopperFeature, _FEATURE_FIELDS),
    ):
        if tuple(item.name for item in fields(model)) != expected:
            raise SourceEvidenceUnavailable("unsupported immutable snapshot schema")
    required = ("GetTracks", "GetFootprints", "GetDrawings", "GetEnabledLayers")
    if not all(callable(getattr(board, name, None)) for name in required):
        raise SourceEvidenceUnavailable(
            "source board lacks complete evidence capabilities"
        )
    factory = getattr(pcbnew_module, "PCB_IO_KICAD_SEXPR", None)
    if not callable(factory):
        raise SourceEvidenceUnavailable("native item serialization is unavailable")
    formatter = factory()
    if not all(
        callable(getattr(formatter, name, None))
        for name in ("Format", "GetStringOutput")
    ):
        raise SourceEvidenceUnavailable("native item serialization is unavailable")
    writer = _EvidenceWriter()
    layers = adapter.copper_layers(board, pcbnew_module)
    tracks = tuple(board.GetTracks())
    writer.add(
        (
            _EVIDENCE_VERSION,
            CONTEXT_POLICY_VERSION,
            tuple(layers.items()),
            adapter._board_bounds(board),
        )
    )
    writer.add(adapter._board_settings_records(board, pcbnew_module))
    try:
        writer.add(
            text_context_records(board, pcbnew_module, copper_names=tuple(layers))
        )
    except TextContextError as error:
        raise adapter.BoardSnapshotError(str(error)) from error
    filename_getter = getattr(board, "GetFileName", None)
    filename = str(filename_getter()) if callable(filename_getter) else ""
    writer.add(filename)
    if filename:
        try:
            writer.add(project_context_records(Path(filename)))
        except ProjectContextError as error:
            raise adapter.BoardSnapshotError(str(error)) from error
    routed_nets: set[str] = set()
    for item in tracks:
        if (
            not adapter._is_via(item, pcbnew_module)
            and int(item.GetLayer()) in layers.values()
        ):
            routed_nets.add(str(item.GetNetname()))
    classes = netclass_context(
        board if netclass_source is None else netclass_source, routed_nets
    )
    pairs = pairing_context(board, routed_nets)
    writer.add(
        (
            classes.classes,
            classes.memberships,
            classes.digest,
            classes.error,
            pairs.pairs,
            pairs.error,
        )
    )
    collections = [("GetTracks", tracks)]
    arc_features: Optional[tuple[CopperFeature, ...]] = None
    names_by_id = {value: name for name, value in layers.items()}
    for name in ("GetFootprints", "GetDrawings", "Zones", "Groups", "Generators"):
        getter = getattr(board, name, None)
        collections.append((name, tuple(getter()) if callable(getter) else ()))
    for name, items in collections:
        writer.add((name, len(items)))
        for item in items:
            is_trace = name == "GetTracks" and not adapter._is_via(item, pcbnew_module)
            if not is_trace:
                # The uncached context digest formats vias and non-track items.
                # Trace values below are copied through their exact native
                # getters; their raw serialization adds no snapshot dependency.
                formatter.Format(item)
                writer.add(str(formatter.GetStringOutput(True)))
            if is_trace:
                # Track raw integer values and IDs are exact even on older
                # serializers. Optional native arc metadata is binary64.
                is_native_arc = adapter._is_arc(item, pcbnew_module)
                layer_id = int(item.GetLayer())
                endpoints = (
                    adapter._point(item.GetStart()),
                    adapter._point(item.GetEnd()),
                )
                net = str(item.GetNetname())
                writer.add(
                    (
                        adapter._feature_uuid(item),
                        layer_id,
                        int(item.GetWidth()),
                        *endpoints,
                        net,
                        is_native_arc,
                    )
                )
                if is_native_arc:
                    midpoint = getattr(item, "GetMid", None)
                    if not callable(midpoint):
                        raise adapter.BoardSnapshotError(
                            "KiCad cannot read the native arc midpoint."
                        )
                    writer.add(adapter._point(midpoint()))
                    try:
                        center = getattr(item, "GetPosition", None)
                        radius = getattr(item, "GetRadius", None)
                        writer.add(
                            (
                                adapter._point(center()) if callable(center) else None,
                                float(radius()) if callable(radius) else None,
                            )
                        )
                    except adapter._FLASH_READ_ERRORS:
                        writer.add("omitted-arc-metadata")
                    # Match the reference snapshot's exact relevance predicate.
                    # Remote arc contours are not a snapshot dependency; fresh
                    # effective landing getters still determine eligibility.
                    layer = names_by_id.get(layer_id)
                    if layer is None:
                        writer.add(())
                        continue
                    if arc_features is None:
                        arc_features = adapter._copper_features(
                            board, pcbnew_module, layers, tracks=tracks
                        )
                    if not adapter._arc_needs_copper_polygons(
                        layer, net, endpoints, arc_features
                    ):
                        writer.add(())
                        continue
                    try:
                        writer.add(
                            adapter._copper_polygons(
                                item, layer_id, pcbnew_module, outside=True
                            )
                        )
                    except adapter._FLASH_READ_ERRORS:
                        writer.add("omitted-arc-perimeter")
            zones = (
                (item,)
                if name == "Zones"
                else tuple(item.Zones())
                if name == "GetFootprints" and hasattr(item, "Zones")
                else ()
            )
            for zone in zones:
                if name != "Zones":
                    formatter.Format(zone)
                    writer.add(str(formatter.GetStringOutput(True)))
                readiness = []
                for method in ("IsFilled", "NeedRefill"):
                    getter = getattr(zone, method, None)
                    readiness.append(
                        (method, bool(getter()) if callable(getter) else None)
                    )
                writer.add(tuple(readiness))
    _feature_evidence(writer, board, pcbnew_module, layers, tracks)
    return SourceEvidence(bytes(writer.data))
