"""Reuse only pure serialized-record work while rereading live context each time."""

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from impedance import pcbnew_adapter as adapter
from impedance.model import BoardSnapshot
from impedance.review_tracking import source_fingerprint
from tests.test_impedance_context_fidelity import ContextZone
from tests.test_impedance_pcbnew_copper_features import LayerPad, LayerVia
from tests.test_impedance_text_context import NativeText


class _ContextBoard:
    """Expose mutable native values and count reads at the actual digest boundary."""

    def __init__(self, collection: str, serialized: str) -> None:
        self.item = SimpleNamespace(serialized=serialized)
        self.collection, self.collections = collection, []
        self.formats = 0
        self.outputs = 0
        self.thickness = 1_600_000
        self.settings_reads = 0
        self.text = NativeText("fresh-context-text", "${VERSION}")
        self.text.serialized = '(gr_text "${VERSION}" (layer "F.SilkS"))'
        self.shown_reads = 0
        shown = self.text.GetShownText

        def shown_text(allow_extra: bool) -> str:
            self.shown_reads += 1
            return shown(allow_extra)

        self.text.GetShownText = shown_text
        board = self

        class Formatter:
            def Format(self, item: Any) -> None:
                board.formats += 1
                self.output = item.serialized

            def GetStringOutput(self, clear: bool) -> str:
                assert clear
                board.outputs += 1
                return self.output

        self.module = SimpleNamespace(PCB_IO_KICAD_SEXPR=Formatter)

    def _items(self, collection: str) -> tuple[Any, ...]:
        """Keep collection enumeration independent of pure-record memoization."""
        self.collections.append(collection)
        items = (self.item,) if collection == self.collection else ()
        return items + ((self.text,) if collection == "GetDrawings" else ())

    def GetFootprints(self) -> tuple[Any, ...]:
        return self._items("GetFootprints")

    def GetDrawings(self) -> tuple[Any, ...]:
        return self._items("GetDrawings")

    def Groups(self) -> tuple[Any, ...]:
        return self._items("Groups")

    def Zones(self) -> tuple[Any, ...]:
        return self._items("Zones")

    def GetDesignSettings(self) -> Any:
        self.settings_reads += 1
        return SimpleNamespace(GetBoardThickness=lambda: self.thickness)

    def digest(self, layers: tuple[str, ...] = ("F.Cu", "B.Cu")) -> str:
        return adapter._context_digest(self, self.module, (), layers)


@pytest.fixture(autouse=True)
def isolated_normalization_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Do not let unrelated records or preceding tests supply a warm result."""
    cache_type = getattr(adapter, "_ContextRecordCache", None)
    if cache_type is not None:
        monkeypatch.setattr(adapter, "_CONTEXT_RECORD_CACHE", cache_type())
    yield


@pytest.mark.parametrize(
    ("collection", "record", "normalizer"),
    [
        (
            "GetFootprints",
            '(footprint "native" (pad "1" smd rect (size 1 2) (layers "F.Cu")))',
            "_footprint_context_record",
        ),
        (
            "GetDrawings",
            '(gr_line (start 0 0) (end 1 1) (layer "F.SilkS"))',
            "_drawing_context_record",
        ),
        ("Groups", '(group "native" (members "a"))', "_without_fill_cache"),
    ],
)
def test_repeated_fresh_context_reads_normalize_identical_records_once(
    monkeypatch: pytest.MonkeyPatch,
    collection: str,
    record: str,
    normalizer: str,
) -> None:
    """Every boundary rereads native bytes; unchanged bytes need only one parse."""
    board = _ContextBoard(collection, record)
    normalize = getattr(adapter, normalizer)
    reads: list[str] = []

    def observed(serialized: str, *args: Any) -> Any:
        if serialized == record:
            reads.append(serialized)
        return normalize(serialized, *args)

    monkeypatch.setattr(adapter, normalizer, observed)
    first = board.digest()
    first_counts = board.formats, board.outputs, board.settings_reads, board.shown_reads
    assert board.digest() == first
    assert (board.formats, board.outputs, board.settings_reads, board.shown_reads) == (
        first_counts[0] * 2,
        first_counts[1] * 2,
        first_counts[2] * 2,
        first_counts[3] * 2,
    )
    assert reads == [record], "The exact same pure native record was parsed again"


def test_warm_record_cache_cannot_hide_live_or_external_context_changes(
    tmp_path: Path,
) -> None:
    """Effective text, settings, source bytes, layers and project files stay live."""
    board = _ContextBoard(
        "GetFootprints",
        '(footprint "native" (fp_line (start 0 0) (end 1 1) (layer "In1.Cu")))',
    )
    board_path = tmp_path / "board.kicad_pcb"
    board.GetFileName = lambda: str(board_path)
    current = board.digest()
    assert board.digest() == current
    board.text.context["VERSION"] = "revision C"
    changed = board.digest()
    assert changed != current
    current = changed
    board.thickness += 1
    changed = board.digest()
    assert changed != current
    current = changed
    board.item.serialized = board.item.serialized.replace("end 1 1", "end 2 1")
    # Inactive copper graphics are intentionally absent from the digest.
    assert board.digest() == current
    changed = board.digest(("F.Cu", "In1.Cu", "B.Cu"))
    assert changed != current
    current = changed
    (tmp_path / "project.kicad_pro").write_text('{"text_variables":{"REV":"D"}}')
    assert board.digest(("F.Cu", "In1.Cu", "B.Cu")) != current


@pytest.mark.parametrize(
    ("collection", "record"),
    [
        (
            "GetFootprints",
            '(footprint "quoted (hide yes)" (property "hidden" "C123" (hide yes)) '
            '(property "visible" "µ RF" (layer "F.SilkS")) '
            '(attr smd exclude_from_bom) (extension (descr "keep")) '
            '(fp_line (start 0 0) (end 1 2) (layer "In1.Cu")))',
        ),
        ("GetFootprints", '(future_primitive "quoted \\"text" (detail "keep"))'),
        ("GetDrawings", '(gr_line (start 0 0) (end 1 2) (layer "In1.Cu"))'),
        ("GetDrawings", '(gr_line (start 0 0) (end 1 2) (layer "F.Fab"))'),
        ("GetDrawings", '(future_primitive (layer "F.Fab") (detail "keep"))'),
        ("Groups", '(group "µ RF" (members "a" "b"))'),
        (
            "Zones",
            "(zone (fill yes (thermal_gap 0.3)) (filled_polygon (pts (xy 1 2))))",
        ),
    ],
)
def test_warm_and_cold_context_digests_are_byte_identical(
    monkeypatch: pytest.MonkeyPatch, collection: str, record: str
) -> None:
    """Use unchanged cold helpers as the exact source-fingerprint oracle."""
    board = _ContextBoard(collection, record)
    warm = adapter._ContextRecordCache()
    cold = adapter._ContextRecordCache(max_entries=0)
    states = [
        (record, ("F.Cu", "B.Cu")),
        (record, ("F.Cu", "In1.Cu", "B.Cu")),
        (record.replace("1 2", "3 4"), ("F.Cu", "In1.Cu", "B.Cu")),
        (record.replace("µ", "Δ"), ("F.Cu", "B.Cu")),
        (record, ("F.Cu", "B.Cu")),
    ]
    for serialized, names in states:
        board.item.serialized = serialized
        monkeypatch.setattr(adapter, "_CONTEXT_RECORD_CACHE", cold)
        expected = board.digest(names)
        monkeypatch.setattr(adapter, "_CONTEXT_RECORD_CACHE", warm)
        assert board.digest(names) == expected
        assert board.digest(names) == expected
    assert not cold.entries


@pytest.mark.parametrize(
    "boundary",
    [
        "GetFootprints",
        "GetDrawings",
        "GetDesignSettings",
        "Format",
        "GetStringOutput",
        "GetShownText",
    ],
)
def test_warm_pure_records_never_mask_fresh_native_getter_failure(
    monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    """A warm result cannot certify a source whose new native read fails."""
    board = _ContextBoard("GetFootprints", '(footprint "native" (at 1 2))')
    board.digest()

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("new native read failed")

    target = (
        board.module.PCB_IO_KICAD_SEXPR
        if boundary in {"Format", "GetStringOutput"}
        else board.text
        if boundary == "GetShownText"
        else board
    )
    monkeypatch.setattr(target, boundary, fail)
    with pytest.raises((adapter.BoardSnapshotError, RuntimeError), match="native read"):
        board.digest()


def test_missing_native_serializer_is_still_rejected_after_a_warm_read() -> None:
    """A retained pure result cannot replace an unavailable native serializer."""
    board = _ContextBoard("GetFootprints", '(footprint "native" (at 1 2))')
    board.digest()
    del board.module.PCB_IO_KICAD_SEXPR
    with pytest.raises(adapter.BoardSnapshotError, match="fingerprint PCB context"):
        board.digest()


@pytest.mark.parametrize(
    ("collection", "record"),
    [
        ("GetFootprints", '(footprint "incomplete"'),
        ("GetFootprints", '(footprint "quote)'),
        ("GetDrawings", "(gr_line (start 1 2)"),
        ("Groups", "(group (filled_polygon (pts (xy 1 2))"),
    ],
)
def test_failed_record_normalization_is_never_retained(
    monkeypatch: pytest.MonkeyPatch, collection: str, record: str
) -> None:
    """Malformed inputs remain cold and fail with the unchanged actionable error."""
    board = _ContextBoard(collection, record)
    normalize = adapter._record_tokens
    calls = []

    def observed(serialized: str, *args: Any) -> list[str]:
        calls.append(serialized)
        return normalize(serialized, *args)

    monkeypatch.setattr(adapter, "_record_tokens", observed)
    messages = []
    for _ in range(2):
        with pytest.raises(adapter.BoardSnapshotError) as error:
            board.digest()
        messages.append(str(error.value))
    assert messages[0] == messages[1]
    if collection != "Groups":
        assert calls.count(record) == 2
    assert all(key[-1] != record for key in adapter._CONTEXT_RECORD_CACHE.entries)


def test_omitted_drawing_is_a_successful_cached_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An audited unplotted record may legitimately normalize to None."""
    record = '(gr_line (start 0 0) (end 1 1) (layer "F.Fab"))'
    normalizer = adapter._drawing_context_record
    calls = []

    def observed(serialized: str, names: Any) -> Any:
        calls.append(serialized)
        return normalizer(serialized, names)

    monkeypatch.setattr(adapter, "_drawing_context_record", observed)
    for _ in range(2):
        assert (
            adapter._normalized_context_record(record, "GetDrawings", ["F.Cu"]) is None
        )
    assert calls == [record]


def test_copper_iterables_are_copied_and_policy_and_kind_bind_cache_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mutable layer list or a policy change cannot reuse different semantics."""
    record = '(footprint "native" (fp_line (start 0 0) (end 1 1) (layer "In1.Cu")))'
    layers = ["F.Cu"]
    first = adapter._normalized_context_record(record, "GetFootprints", layers)
    assert (
        adapter._normalized_context_record(record, "GetFootprints", iter(layers))
        == first
    )
    assert len(adapter._CONTEXT_RECORD_CACHE.entries) == 1
    layers.append("In1.Cu")
    second = adapter._normalized_context_record(record, "GetFootprints", layers)
    assert second != first
    assert adapter._normalized_context_record(record, "Groups", layers) != second
    monkeypatch.setattr(adapter, "CONTEXT_POLICY_VERSION", 2)
    assert adapter._normalized_context_record(record, "GetFootprints", layers) == second
    assert len(adapter._CONTEXT_RECORD_CACHE.entries) == 4


def test_entry_limit_evicts_the_least_recent_pure_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Touching an entry updates eviction order without counting its bytes twice."""
    cache = adapter._ContextRecordCache(max_entries=2)
    monkeypatch.setattr(adapter, "_CONTEXT_RECORD_CACHE", cache)
    records = ['(group "a")', '(group "b")', '(group "c")']
    for index in (0, 1, 0, 2):
        adapter._normalized_context_record(records[index], "Groups", None)
    assert [key[-1] for key in cache.entries] == [records[0], records[2]]
    before = cache.payload_bytes
    adapter._normalized_context_record(records[0], "Groups", None)
    assert cache.payload_bytes == before
    adapter._normalized_context_record(records[1], "Groups", None)
    assert [key[-1] for key in cache.entries] == [records[0], records[1]]


def test_byte_budget_counts_unicode_keys_layers_and_immutable_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bound retained encoded payload rather than just Python character counts."""
    cache = adapter._ContextRecordCache(max_bytes=75)
    monkeypatch.setattr(adapter, "_CONTEXT_RECORD_CACHE", cache)
    for name in ("µ", "Δ", "Ω"):
        adapter._normalized_context_record(f'(group "{name}")', "Groups", ["F.Cu"])
        assert cache.payload_bytes <= 75
    assert len(cache.entries) == 2
    expected = sum(
        len(value.encode("utf-8"))
        for key, (result, _) in cache.entries.items()
        for value in (key[1], key[3], *(key[2] or ()), result)
    )
    assert cache.payload_bytes == expected


@pytest.mark.parametrize("reason", ["input", "result", "unknown-kind", "subclass"])
def test_oversized_or_ambiguous_records_use_the_unchanged_cold_path(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    """Retention limits cannot replace or weaken the actual normalizer."""
    record = '(group "native")'
    collection = "Groups"
    cache = adapter._ContextRecordCache(max_entry_bytes=30)
    if reason == "input":
        record = '(group "' + "x" * 31 + '")'
    elif reason == "unknown-kind":
        collection = "FutureNativeCollection"
    elif reason == "subclass":

        class NativeTextSubclass(str):
            pass

        record = NativeTextSubclass(record)
    monkeypatch.setattr(adapter, "_CONTEXT_RECORD_CACHE", cache)
    normalize = adapter._without_fill_cache
    calls = []

    def observed(serialized: str) -> str:
        calls.append(serialized)
        result = normalize(serialized)
        return result * 5 if reason == "result" else result

    monkeypatch.setattr(adapter, "_without_fill_cache", observed)
    expected = observed(record)
    calls.clear()
    for _ in range(2):
        assert adapter._normalized_context_record(record, collection, None) == expected
    assert calls == [record, record]
    assert not cache.entries


def test_warm_context_still_reads_zone_fill_bytes_and_native_readiness() -> None:
    """Generated zone pixels and per-zone flags remain fresh beyond the memo."""
    record = "(zone (filled_polygon (pts (xy 1 2))))"
    board = _ContextBoard("Zones", record)
    reads: list[str] = []
    readiness = {"IsFilled": True, "NeedRefill": False}

    def native_flag(name: str) -> bool:
        reads.append(name)
        return readiness[name]

    board.item.IsFilled = lambda: native_flag("IsFilled")
    board.item.NeedRefill = lambda: native_flag("NeedRefill")
    first = board.digest()
    assert board.digest() == first
    assert reads == ["IsFilled", "NeedRefill"] * 2
    readiness["NeedRefill"] = True
    second = board.digest()
    assert second != first
    board.item.serialized = record.replace("1 2", "3 4")
    assert board.digest() != second
    assert reads == ["IsFilled", "NeedRefill"] * 4
    assert all(key[-1] != record for key in adapter._CONTEXT_RECORD_CACHE.entries)


def test_unchanged_warm_footprint_still_reads_owned_zone_pixels_and_bound_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A footprint parse hit cannot replace its separately serialized zone state."""
    record = '(footprint "local ground" (at 1 2))'
    board = _ContextBoard("GetFootprints", record)
    first, second = ContextZone(), ContextZone()
    second.point = "9 8"
    first.needs_refill = True
    zones = [first, second]
    board.item.Zones = lambda: tuple(zones)
    normalize = adapter._footprint_context_record
    footprint_parses = []

    def observed(serialized: str, names: Any) -> str:
        footprint_parses.append(serialized)
        return normalize(serialized, names)

    monkeypatch.setattr(adapter, "_footprint_context_record", observed)
    original = board.digest()
    assert board.digest() == original
    first.needs_refill, second.needs_refill = False, True
    assert board.digest() != original
    first.needs_refill, second.needs_refill = True, False
    zones.reverse()
    assert board.digest() == original
    first.point = "3 4"
    assert board.digest() != original
    first.point = "1 2"
    assert board.digest() == original
    assert board.item.serialized == record
    assert footprint_parses == [record]


@pytest.mark.parametrize("changed", ["size", "shape", "position"])
def test_full_snapshot_reads_changed_pad_geometry_despite_identical_context_text(
    changed: str,
) -> None:
    """Native effective copper remains fresh even when normalized records hit."""
    board = _ContextBoard("GetFootprints", '(footprint "native" (at 1 2))')
    pad = LayerPad()
    board.item.Pads = lambda: (pad,)
    board.GetTracks = lambda: ()
    board.GetEnabledLayers = lambda: SimpleNamespace(CuStack=lambda: (0, 31))
    board.module.F_Cu, board.module.B_Cu = 0, 31
    board.module.PAD_SHAPE_CIRCLE = 0
    board.module.PAD_SHAPE_OVAL = 1
    board.module.PAD_SHAPE_RECT = 2
    original = adapter.snapshot_board(board, board.module)
    assert adapter.snapshot_board(board, board.module) == original
    pad.reads.clear()
    if changed == "size":
        pad.sizes[0] = (1_200_000, 900_000)
    elif changed == "shape":
        pad.shapes[0] = 1
    else:
        pad.positions[0] = (600_000, 100_000)
    current = adapter.snapshot_board(board, board.module)
    assert current.context_digest == original.context_digest
    assert current.copper_features != original.copper_features
    assert source_fingerprint(current) != source_fingerprint(original)
    assert set(pad.reads) == {
        (method, layer) for method in ("size", "shape", "position") for layer in (0, 31)
    }


@pytest.mark.parametrize("serializer", ["PCB_IO_KICAD_SEXPR", "PCB_IO"])
@pytest.mark.parametrize("collection", ["Generators", "via"])
def test_generator_and_via_records_remain_fresh_with_modern_or_legacy_serializer(
    monkeypatch: pytest.MonkeyPatch, serializer: str, collection: str
) -> None:
    """KiCad7's formatter fallback keeps both cold and warm source reads exact."""
    record = '(group "generator" (members "a"))'
    board = _ContextBoard("unused", record)
    vias: tuple[Any, ...] = ()
    if collection == "Generators":
        board.Generators = lambda: (board.item,)
    else:
        via = LayerVia()
        via.serialized = '(via (at 1 2) (size 0.6) (layers "F.Cu" "B.Cu"))'
        board.item = via
        record = via.serialized
        vias = (via,)
    if serializer == "PCB_IO":
        board.module.PCB_IO = board.module.PCB_IO_KICAD_SEXPR
        del board.module.PCB_IO_KICAD_SEXPR
    normalize = adapter._without_fill_cache
    parsed = []

    def observed(serialized: str) -> str:
        if serialized == record:
            parsed.append(serialized)
        return normalize(serialized)

    monkeypatch.setattr(adapter, "_without_fill_cache", observed)

    def digest() -> str:
        return adapter._context_digest(board, board.module, vias, ("F.Cu", "B.Cu"))

    original = digest()
    first_reads = board.formats, board.outputs
    assert digest() == original
    assert (board.formats, board.outputs) == tuple(value * 2 for value in first_reads)
    assert parsed == [record]
    board.item.serialized = record.replace("1 2", "3 4").replace('"a"', '"a" "b"')
    assert digest() != original


@pytest.mark.parametrize("collection", ["Zones", "Groups", "Generators"])
def test_warm_context_cannot_hide_optional_collection_enumeration_failure(
    monkeypatch: pytest.MonkeyPatch, collection: str
) -> None:
    """An optional native API that exists but fails cannot authorize stale context."""
    board = _ContextBoard("unused", '(group "native")')
    monkeypatch.setattr(board, collection, lambda: (board.item,), raising=False)
    board.digest()

    def fail() -> Any:
        raise RuntimeError("native collection is no longer readable")

    monkeypatch.setattr(board, collection, fail)
    with pytest.raises(RuntimeError, match="no longer readable"):
        board.digest()


def test_disappearing_optional_generator_is_not_restored_from_a_warm_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-longer-enumerated item contributes no record, regardless of retained text."""
    board = _ContextBoard("unused", '(group "generator" (members "a"))')
    without_generator = board.digest()
    monkeypatch.setattr(board, "Generators", lambda: (board.item,), raising=False)
    with_generator = board.digest()
    assert with_generator != without_generator
    assert board.digest() == with_generator
    monkeypatch.delattr(board, "Generators")
    assert board.digest() == without_generator


def test_default_normalization_cache_keeps_the_agreed_resource_bounds() -> None:
    """Entry and byte caps independently bound this process-wide pure memo."""
    cache = adapter._ContextRecordCache()
    assert cache.max_entries == 4096
    assert cache.max_bytes == 16 * 1024 * 1024
    assert cache.max_entry_bytes == 1024 * 1024


def test_large_native_context_scan_reuses_each_parse_and_rereads_every_native_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A complete 1,741-item scan must fit without sequential LRU thrashing."""
    board = _ContextBoard("unused", "")
    items = tuple(
        SimpleNamespace(
            serialized=(
                f"(gr_line (start {index} 0) (end {index} 1) "
                '(layer "F.SilkS") (width 0.1))'
            )
        )
        for index in range(1741)
    )
    enumerations = []

    def drawings() -> tuple[Any, ...]:
        enumerations.append("GetDrawings")
        return items

    board.GetDrawings = drawings
    normalize = adapter._record_tokens
    parsed = []

    def observed(serialized: str, expected_root: str = "") -> list[str]:
        parsed.append(serialized)
        return normalize(serialized, expected_root)

    monkeypatch.setattr(adapter, "_record_tokens", observed)
    original = board.digest()
    assert len(parsed) == 1741
    assert board.digest() == original
    assert board.formats == board.outputs == 1741 * 2
    assert enumerations == ["GetDrawings"] * 4
    assert len(parsed) == 1741, "A fresh full-board scan evicted its reusable records"
    items[-1].serialized = items[-1].serialized.replace("width 0.1", "width 0.2")
    assert board.digest() != original
    assert board.formats == board.outputs == 1741 * 3
    assert len(parsed) == 1742
    assert adapter._CONTEXT_RECORD_CACHE.payload_bytes <= 16 * 1024 * 1024


class _SnapshotPad(LayerPad):
    """Count native item-wide reads separately from physical-layer geometry."""

    def __init__(self) -> None:
        super().__init__()
        self.uuid = "native-pad"
        self.uuid_reads = 0
        self.rotation_reads = 0

    @property
    def m_Uuid(self) -> Any:
        return SimpleNamespace(AsString=self._uuid)

    def _uuid(self) -> str:
        self.uuid_reads += 1
        return self.uuid

    def GetOrientationDegrees(self) -> float:
        self.rotation_reads += 1
        return super().GetOrientationDegrees()


class _SnapshotVia(LayerVia):
    """Expose one physical position and independently read physical-layer sizes."""

    def __init__(self) -> None:
        self.uuid = "native-via"
        self.uuid_reads = 0
        self.position_reads = 0
        self.width_reads: list[int] = []
        self.position = (0, 0)
        self.serialized = '(via (at 0 0) (size 1) (layers "F.Cu" "B.Cu"))'

    @property
    def m_Uuid(self) -> Any:
        return SimpleNamespace(AsString=self._uuid)

    def _uuid(self) -> str:
        self.uuid_reads += 1
        return self.uuid

    def GetPosition(self) -> Any:
        self.position_reads += 1
        return SimpleNamespace(x=self.position[0], y=self.position[1])

    def GetWidth(self, layer_id: int = 0) -> int:
        self.width_reads.append(layer_id)
        return super().GetWidth(layer_id)


def _geometry_snapshot_fixture() -> tuple[_ContextBoard, _SnapshotPad, _SnapshotVia]:
    """Use existing layer-aware geometry through the complete snapshot boundary."""
    board = _ContextBoard("GetFootprints", '(footprint "native" (at 1 2))')
    pad, via = _SnapshotPad(), _SnapshotVia()
    board.item.Pads = lambda: (pad,)
    board.track_reads = 0

    def tracks() -> tuple[Any, ...]:
        board.track_reads += 1
        return (via,)

    board.GetTracks = tracks
    board.GetEnabledLayers = lambda: SimpleNamespace(CuStack=lambda: (0, 31))
    board.module.F_Cu, board.module.B_Cu = 0, 31
    board.module.PAD_SHAPE_CIRCLE = 0
    board.module.PAD_SHAPE_OVAL = 1
    board.module.PAD_SHAPE_RECT = 2
    return board, pad, via


@pytest.mark.parametrize("boundary", ["tracks", "pad-rotation", "via-position", "uuid"])
def test_full_snapshot_reads_item_wide_native_values_once_per_boundary(
    boundary: str,
) -> None:
    """Repeated layer flashes must not duplicate successful item-wide native reads."""
    board, pad, via = _geometry_snapshot_fixture()
    snapshot = adapter.snapshot_board(board, board.module)
    assert len(snapshot.copper_features) == 4
    counts = {
        "tracks": board.track_reads,
        "pad-rotation": pad.rotation_reads,
        "via-position": via.position_reads,
        "uuid": (pad.uuid_reads, via.uuid_reads),
    }
    assert counts[boundary] == ((1, 1) if boundary == "uuid" else 1)
    assert via.width_reads == [0, 31]
    assert set(pad.reads) == {
        (method, layer) for method in ("size", "shape", "position") for layer in (0, 31)
    }


def test_repeated_snapshots_reread_item_wide_native_values_and_preserve_exact_output() -> (
    None
):
    """Only one synchronous walk shares values; subsequent reads observe all edits."""
    board, pad, via = _geometry_snapshot_fixture()
    original: BoardSnapshot = adapter.snapshot_board(board, board.module)
    assert adapter.snapshot_board(board, board.module) == original
    pad.uuid = "edited-pad"
    pad.rotation = 45.0
    via.uuid = "edited-via"
    via.position = (100_000, 200_000)
    current = adapter.snapshot_board(board, board.module)
    assert current.context_digest == original.context_digest
    assert source_fingerprint(current) != source_fingerprint(original)
    assert {feature.feature_id for feature in current.copper_features} == {
        f"{kind}:{layer}"
        for kind in ("edited-pad", "edited-via")
        for layer in ("F.Cu", "B.Cu")
    }
    assert all(
        feature.rotation_deg == 45.0
        for feature in current.copper_features
        if feature.kind == "pad"
    )
    assert all(
        feature.center == (100_000, 200_000)
        for feature in current.copper_features
        if feature.kind == "via"
    )
    assert board.track_reads == pad.rotation_reads == via.position_reads == 3
    assert pad.uuid_reads == via.uuid_reads == 3


@pytest.mark.parametrize("getter", ["pad-rotation", "via-position"])
def test_failed_item_wide_getter_is_retried_on_later_layers_and_next_snapshot(
    getter: str,
) -> None:
    """Only successful precise native values may be reused inside a layer walk."""
    board, pad, via = _geometry_snapshot_fixture()
    owner, name = (
        (pad, "GetOrientationDegrees")
        if getter == "pad-rotation"
        else (via, "GetPosition")
    )
    original = getattr(owner, name)
    calls = []

    def transient() -> Any:
        calls.append(getter)
        if len(calls) == 1:
            raise RuntimeError("one native read failed")
        return original()

    setattr(owner, name, transient)
    first = adapter.snapshot_board(board, board.module)
    kind = "pad" if getter == "pad-rotation" else "via"
    assert [
        feature.layer for feature in first.copper_features if feature.kind == kind
    ] == ["B.Cu"]
    assert len(calls) == 2
    second = adapter.snapshot_board(board, board.module)
    assert {
        feature.layer for feature in second.copper_features if feature.kind == kind
    } == {
        "F.Cu",
        "B.Cu",
    }
    assert len(calls) == 3


@pytest.mark.parametrize(
    ("native_uuid", "legacy_fingerprint"),
    [
        (True, "e4c08dc94137bf7724be30ae8ac58d1fde7b4a69e66027ded1e3412147f5a95f"),
        (False, "50ee2834ee30097c0ddd334136829990dde435f8c123adc834a9524d6b4ae7b0"),
    ],
)
def test_snapshot_output_matches_frozen_preoptimization_geometry_and_ids(
    monkeypatch: pytest.MonkeyPatch, native_uuid: bool, legacy_fingerprint: str
) -> None:
    """Freeze the complete source fingerprint before deduplicating native getters."""
    if not native_uuid:
        monkeypatch.setattr(_SnapshotPad, "m_Uuid", None)
        monkeypatch.setattr(_SnapshotVia, "m_Uuid", None)
    board, _pad, _via = _geometry_snapshot_fixture()
    assert (
        source_fingerprint(adapter.snapshot_board(board, board.module))
        == legacy_fingerprint
    )


def test_nonfinite_rotation_is_retried_instead_of_reused_on_another_layer() -> None:
    """A returned NaN is not successful native geometry evidence."""
    board, pad, _via = _geometry_snapshot_fixture()
    rotations = iter((float("nan"), 30.0))
    pad.GetOrientationDegrees = lambda: next(rotations)
    snapshot = adapter.snapshot_board(board, board.module)
    flashes = [feature for feature in snapshot.copper_features if feature.kind == "pad"]
    assert len(flashes) == 1
    assert (flashes[0].layer, flashes[0].rotation_deg) == ("B.Cu", 30.0)


@pytest.mark.parametrize("kind", ["pad", "via"])
def test_new_native_uuid_read_failure_still_propagates_after_a_successful_snapshot(
    kind: str,
) -> None:
    """Native UUID errors retain their existing fail-closed snapshot behavior."""
    board, pad, via = _geometry_snapshot_fixture()
    adapter.snapshot_board(board, board.module)

    def fail() -> str:
        raise RuntimeError("native UUID is no longer readable")

    item = pad if kind == "pad" else via
    item._uuid = fail
    with pytest.raises(RuntimeError, match="UUID is no longer readable"):
        adapter.snapshot_board(board, board.module)
