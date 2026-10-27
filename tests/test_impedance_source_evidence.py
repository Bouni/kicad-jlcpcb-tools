"""Evidence must preserve precise effective data omitted by native formatting."""

from dataclasses import replace
from math import nextafter
from types import SimpleNamespace
from typing import Any

import pytest

from impedance import source_evidence as source
from impedance.net_metadata import NetClassContext, PairingContext
from impedance.pcbnew_adapter import BoardSnapshotError, snapshot_board


def _fixture(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, dict[str, Any]]:
    """Keep formatting fixed so exact getters, not rounded text, prove changes."""
    state: dict[str, Any] = {
        "rotation": 0.1,
        "flashed": True,
        "size": (500_000, 700_000),
        "center": (1_000_000, 2_000_000),
        "shape": 1,
        "number": "1",
        "polygons": ((((0, 0), (100, 0), (100, 100)),),),
    }

    def point(value: tuple[int, int]) -> Any:
        return SimpleNamespace(x=value[0], y=value[1])

    pad = SimpleNamespace(
        m_Uuid=SimpleNamespace(AsString=lambda: "pad-id"),
        GetNetname=lambda: "USB_P",
        GetNumber=lambda: state["number"],
        FlashLayer=lambda layer: state["flashed"] and layer == 0,
        GetSize=lambda layer: point(state["size"]),
        ShapePos=lambda layer: point(state["center"]),
        GetShape=lambda layer: state["shape"],
        GetOrientationDegrees=lambda: state["rotation"],
    )
    footprint = SimpleNamespace(
        m_Uuid=SimpleNamespace(AsString=lambda: "footprint-id"),
        GetClass=lambda: "FOOTPRINT",
        Pads=lambda: (pad,),
        GetFields=lambda: (),
        GraphicalItems=lambda: (),
    )
    board = SimpleNamespace(
        GetTracks=lambda: (),
        GetFootprints=lambda: (footprint,),
        GetDrawings=lambda: (),
        GetEnabledLayers=lambda: SimpleNamespace(CuStack=lambda: (0, 31)),
    )

    class Formatter:
        """Represent native decimal formatting that hides exact getter changes."""

        def Format(self, item: Any) -> None:
            """Keep the record constant while effective native getter values change."""

        def GetStringOutput(self, clear: bool) -> str:
            """Return fixed text for the independently mutable native fixture."""
            return '(footprint "fixture" (pad "1" smd rect (at 1 2 0.1)))'

    module = SimpleNamespace(
        PCB_IO_KICAD_SEXPR=Formatter,
        F_Cu=0,
        B_Cu=31,
        PAD_SHAPE_RECT=1,
    )
    monkeypatch.setattr(source, "netclass_context", lambda *args: NetClassContext())
    monkeypatch.setattr(source, "pairing_context", lambda *args: PairingContext())
    monkeypatch.setattr(
        source.adapter, "_copper_polygons", lambda *args, **kwargs: state["polygons"]
    )
    return board, module, state


@pytest.mark.parametrize(
    "change", ["rotation", "flashed", "size", "center", "shape", "number", "polygons"]
)
def test_effective_copper_changes_are_not_hidden_by_identical_serialized_records(
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """Every native copper value affecting the snapshot remains a dependency."""
    board, module, state = _fixture(monkeypatch)
    if change == "polygons":
        state["shape"] = 99
    before = source.acquire_source_evidence(board, module)
    snapshot_before = snapshot_board(board, module)
    state[change] = {
        "rotation": nextafter(0.1, 1.0),
        "flashed": False,
        "size": (500_001, 700_000),
        "center": (1_000_001, 2_000_000),
        "shape": 99,
        "number": "2",
        "polygons": ((((0, 0), (101, 0), (100, 100)),),),
    }[change]
    assert snapshot_board(board, module) != snapshot_before
    assert source.acquire_source_evidence(board, module) != before


def test_missing_serialization_capability_falls_back_without_native_reads() -> None:
    """Unsupported bindings use the original builder instead of a partial key."""
    with pytest.raises(source.SourceEvidenceUnavailable):
        source.acquire_source_evidence(object(), object())


def test_new_snapshot_schema_disables_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
    """A future field cannot silently evade the audited evidence inventory."""
    board, module, _ = _fixture(monkeypatch)
    monkeypatch.setattr(
        source, "_SNAPSHOT_FIELDS", source._SNAPSHOT_FIELDS + ("future_field",)
    )
    with pytest.raises(source.SourceEvidenceUnavailable, match="schema"):
        source.acquire_source_evidence(board, module)


def test_evidence_memory_bound_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Oversized source records fall back rather than retaining unbounded state."""
    board, module, _ = _fixture(monkeypatch)
    monkeypatch.setattr(source, "_MAX_EVIDENCE_BYTES", 64)
    with pytest.raises(source.SourceEvidenceUnavailable, match="memory"):
        source.acquire_source_evidence(board, module)


def test_native_read_error_after_warming_is_not_an_unchanged_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An actual read failure propagates and invalidates the previous success."""
    board, module, _ = _fixture(monkeypatch)
    reader = source.SourceSnapshotReader()
    acquire = lambda: source.acquire_source_evidence(board, module)
    build = lambda: snapshot_board(board, module)
    first = reader.read(acquire, build, owner_key=(1,))

    def fail() -> Any:
        raise BoardSnapshotError("native collection failed")

    monkeypatch.setattr(board, "GetTracks", fail)
    with pytest.raises(BoardSnapshotError, match="native collection failed"):
        reader.read(acquire, build, owner_key=(1,))
    monkeypatch.setattr(board, "GetTracks", lambda: ())
    assert reader.read(acquire, build, owner_key=(1,)) is not first


def test_error_metadata_is_part_of_complete_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery from metadata errors cannot reuse an old diagnostic snapshot."""
    board, module, _ = _fixture(monkeypatch)
    before = source.acquire_source_evidence(board, module)
    monkeypatch.setattr(
        source, "netclass_context", lambda *args: NetClassContext(error="read failed")
    )
    failed = source.acquire_source_evidence(board, module)
    assert failed != before
    monkeypatch.setattr(
        source,
        "netclass_context",
        lambda *args: replace(NetClassContext(), digest="changed"),
    )
    assert source.acquire_source_evidence(board, module) not in (before, failed)


@pytest.mark.parametrize(
    "left,right",
    [
        (True, 1),
        (1, 1.0),
        ("x", b"x"),
        ((1,), [1]),
        (0.0, -0.0),
        (0.1, nextafter(0.1, 1.0)),
        (None, "None"),
    ],
)
def test_primitive_encoding_preserves_types_and_binary64_bits(
    left: Any, right: Any
) -> None:
    """Distinguish exact primitive values without lossy decimal formatting."""
    first, second = source._EvidenceWriter(), source._EvidenceWriter()
    first.add(left)
    second.add(right)
    assert first.data != second.data


def test_primitive_encoding_is_independent_of_identity_and_string_interning() -> None:
    """Equal source values produce equal bytes across distinct allocations."""
    first, second = source._EvidenceWriter(), source._EvidenceWriter()
    text = "a long enough string to avoid automatic short-string interning"
    shared = (text, text, (text,))
    independent = (
        text.encode().decode(),
        text.encode().decode(),
        (text.encode().decode(),),
    )
    assert shared == independent
    assert shared[0] is shared[1]
    assert independent[0] is not independent[1]
    first.add(shared)
    second.add(independent)
    assert first.data == second.data


def test_oversized_effective_polygon_cannot_be_recorded_as_absent_copper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Propagate admission failures instead of swallowing them as native omissions."""
    board, module, state = _fixture(monkeypatch)
    state["shape"] = 99
    state["polygons"] = ((tuple((index, index * index) for index in range(1000)),),)
    monkeypatch.setattr(source, "_MAX_EVIDENCE_BYTES", 1024)
    with pytest.raises(source.SourceEvidenceUnavailable, match="memory"):
        source._feature_evidence(
            source._EvidenceWriter(), board, module, {"F.Cu": 0}, ()
        )


def test_exact_arc_radius_is_not_hidden_by_identical_points_or_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bind optional native arc radius bits independently from sampled geometry."""
    board, module, state = _fixture(monkeypatch)
    state["arc_radius"] = 1000.0
    arc = SimpleNamespace(
        GetClass=lambda: "PCB_ARC",
        GetLayer=lambda: 0,
        GetWidth=lambda: 100,
        GetStart=lambda: SimpleNamespace(x=0, y=0),
        GetMid=lambda: SimpleNamespace(x=0, y=1000),
        GetEnd=lambda: SimpleNamespace(x=1000, y=1000),
        GetPosition=lambda: SimpleNamespace(x=500, y=500),
        GetRadius=lambda: state["arc_radius"],
        GetNetname=lambda: "USB_P",
        m_Uuid=SimpleNamespace(AsString=lambda: "native-arc"),
    )
    monkeypatch.setattr(board, "GetTracks", lambda: (arc,))
    before = source.acquire_source_evidence(board, module)
    original = snapshot_board(board, module)
    state["arc_radius"] = nextafter(state["arc_radius"], float("inf"))
    changed = snapshot_board(board, module)
    assert changed.traces[0].points == original.traces[0].points
    assert (
        changed.traces[0].native_arc_radius_nm
        != original.traces[0].native_arc_radius_nm
    )
    assert source.acquire_source_evidence(board, module) != before


def _arc_landing_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, Any, dict[str, Any]]:
    """Keep definitions fixed while effective landing and arc copper can change."""
    board, module, state = _fixture(monkeypatch)
    state.update(
        layer=0,
        net="USB_P",
        arc_calls=[],
        arc_error=None,
        arc_polygons=((((-5, -5), (105, -5), (105, 55), (-5, 55)),),),
    )
    pad = next(iter(next(iter(board.GetFootprints())).Pads()))
    monkeypatch.setattr(pad, "GetNetname", lambda: state["net"], raising=False)
    arc = SimpleNamespace(
        GetClass=lambda: "PCB_ARC",
        GetLayer=lambda: state["layer"],
        GetWidth=lambda: 10,
        GetStart=lambda: SimpleNamespace(x=0, y=0),
        GetMid=lambda: SimpleNamespace(x=50, y=50),
        GetEnd=lambda: SimpleNamespace(x=100, y=0),
        GetPosition=lambda: SimpleNamespace(x=50, y=0),
        GetRadius=lambda: 50.0,
        GetNetname=lambda: "USB_P",
        m_Uuid=SimpleNamespace(AsString=lambda: "effective-arc"),
    )
    monkeypatch.setattr(board, "GetTracks", lambda: (arc,))

    def polygons(item: Any, *args: Any, **kwargs: Any) -> Any:
        """Expose the effective native perimeter independently of item formatting."""
        if item is arc:
            state["arc_calls"].append((args, kwargs))
            if state["arc_error"] is not None:
                raise state["arc_error"]
            return state["arc_polygons"]
        return state["polygons"]

    monkeypatch.setattr(source.adapter, "_copper_polygons", polygons)
    return board, module, state


def test_remote_arc_perimeter_is_outside_the_snapshot_dependency_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Avoid dense native contour copies that cannot affect the reference snapshot."""
    board, module, state = _arc_landing_fixture(monkeypatch)
    expected = snapshot_board(board, module)
    before = source.acquire_source_evidence(board, module)
    state["arc_polygons"] = ((((0, -5), (106, -5), (106, 55), (0, 55)),),)
    assert snapshot_board(board, module) == expected
    assert source.acquire_source_evidence(board, module) == before
    assert state["arc_calls"] == []


@pytest.mark.parametrize("change", ["center", "size", "flashed", "net", "layer"])
def test_raw_landing_changes_make_arc_perimeters_fresh_dependencies(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """Reevaluate native landing eligibility on every proof despite fixed formatting."""
    board, module, state = _arc_landing_fixture(monkeypatch)
    state["center"] = (50, 0)
    initial, updated = {
        "center": ((1_000_000, 2_000_000), (50, 0)),
        "size": ((1, 1), (500_000, 700_000)),
        "flashed": (False, True),
        "net": ("OTHER", "USB_P"),
        "layer": (31, 0),
    }[change]
    state[change] = initial
    acquire = lambda: source.acquire_source_evidence(board, module)
    build = lambda: snapshot_board(board, module)
    reader = source.SourceSnapshotReader()
    before = reader.read(acquire, build, owner_key=(1,))
    assert not before.traces[0].copper_polygons
    assert state["arc_calls"] == []
    state[change] = updated
    expected = build()
    assert expected.traces[0].copper_polygons
    assert expected != before
    assert reader.read(acquire, build, owner_key=(1,)) == expected
    assert reader.statistics.builds == 2
    reader.close()


def test_relevant_arc_override_and_perimeter_read_recovery_remain_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contour-only changes and temporary native omissions cannot reuse old copper."""
    board, module, state = _arc_landing_fixture(monkeypatch)
    state["center"] = (50, 0)
    acquire = lambda: source.acquire_source_evidence(board, module)
    build = lambda: snapshot_board(board, module)
    reader = source.SourceSnapshotReader()
    original = reader.read(acquire, build, owner_key=(1,))
    state["arc_polygons"] = ((((0, -5), (106, -5), (106, 55), (0, 55)),),)
    changed = build()
    assert changed != original
    assert reader.read(acquire, build, owner_key=(1,)) == changed
    state["arc_error"] = RuntimeError("native perimeter unavailable")
    omitted = build()
    assert not omitted.traces[0].copper_polygons
    assert reader.read(acquire, build, owner_key=(1,)) == omitted
    state["arc_error"] = None
    assert reader.read(acquire, build, owner_key=(1,)) == changed
    assert reader.statistics.builds == 4
    reader.close()


@pytest.mark.parametrize("failure", [OSError, source.SourceEvidenceUnavailable])
def test_relevant_arc_fatal_read_error_is_not_an_absent_perimeter(
    monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    """Keep unhandled native failures and evidence admission errors fail-closed."""
    board, module, state = _arc_landing_fixture(monkeypatch)
    state["center"] = (50, 0)
    state["arc_error"] = failure("perimeter failed")
    with pytest.raises(failure, match="perimeter failed"):
        source.acquire_source_evidence(board, module)


def test_via_center_is_shared_only_within_one_fresh_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read every layer's flash and width while copying one item-wide center."""
    board, module, state = _fixture(monkeypatch)
    calls: dict[str, Any] = {"position": 0, "flash": [], "width": []}

    def position() -> Any:
        calls["position"] += 1
        return SimpleNamespace(x=10, y=20)

    def flash(layer: int) -> bool:
        calls["flash"].append(layer)
        return True

    def width(layer: int) -> int:
        calls["width"].append(layer)
        return 100_000 + layer

    via = SimpleNamespace(
        GetClass=lambda: "PCB_VIA",
        GetNetname=lambda: "USB_P",
        GetPosition=position,
        FlashLayer=flash,
        GetWidth=width,
        m_Uuid=SimpleNamespace(AsString=lambda: "via"),
    )
    monkeypatch.setattr(board, "GetTracks", lambda: (via,))
    monkeypatch.setattr(board, "GetFootprints", lambda: ())
    assert source.acquire_source_evidence(
        board, module
    ) == source.acquire_source_evidence(board, module)
    assert calls["position"] == 2
    assert calls["flash"] == [0, 31, 0, 31]
    assert calls["width"] == [0, 31, 0, 31]


def test_pad_rotation_is_shared_only_within_one_fresh_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refresh pad orientation once per proof while still reading every layer."""
    board, module, state = _fixture(monkeypatch)
    calls = {"rotation": 0, "flash": 0, "size": 0}
    pad = next(iter(next(iter(board.GetFootprints())).Pads()))

    def rotation() -> float:
        calls["rotation"] += 1
        return state["rotation"]

    def flash(layer: int) -> bool:
        calls["flash"] += 1
        return True

    def size(layer: int) -> Any:
        calls["size"] += 1
        return SimpleNamespace(x=500_000, y=700_000)

    monkeypatch.setattr(pad, "GetOrientationDegrees", rotation)
    monkeypatch.setattr(pad, "FlashLayer", flash)
    monkeypatch.setattr(pad, "GetSize", size)
    assert source.acquire_source_evidence(
        board, module
    ) == source.acquire_source_evidence(board, module)
    assert calls == {"rotation": 2, "flash": 4, "size": 4}


@pytest.mark.parametrize("arc", [False, True])
def test_trace_proof_uses_exact_getters_without_redundant_native_formatting(
    monkeypatch: pytest.MonkeyPatch,
    arc: bool,
) -> None:
    """Bind every trace dependency while formatting only native render context."""
    board, module, state = _fixture(monkeypatch)
    state["width"] = 100_000
    state["end_x"] = 1_000_000
    track = SimpleNamespace(
        GetClass=lambda: "PCB_ARC" if arc else "PCB_TRACK",
        GetLayer=lambda: 0,
        GetWidth=lambda: state["width"],
        GetStart=lambda: SimpleNamespace(x=0, y=0),
        GetEnd=lambda: SimpleNamespace(x=state["end_x"], y=1_000_000),
        GetPosition=lambda: SimpleNamespace(x=500_000, y=500_000),
        GetRadius=lambda: 500_000.0,
        GetNetname=lambda: "USB_P",
        m_Uuid=SimpleNamespace(AsString=lambda: "native-track"),
    )
    if arc:
        track.GetMid = lambda: SimpleNamespace(x=0, y=1_000_000)
    monkeypatch.setattr(board, "GetTracks", lambda: (track,))

    def format_context_only(self: Any, item: Any) -> None:
        if item is track:
            raise AssertionError(
                "Non-via tracks are not serialized by the snapshot reference"
            )

    monkeypatch.setattr(module.PCB_IO_KICAD_SEXPR, "Format", format_context_only)
    before = source.acquire_source_evidence(board, module)
    original = snapshot_board(board, module)
    state["width"] += 1
    state["end_x"] += 1
    assert snapshot_board(board, module) != original
    assert source.acquire_source_evidence(board, module) != before
