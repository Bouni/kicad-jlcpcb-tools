"""Check complete source evidence against the uncached native snapshot oracle."""

import os
from pathlib import Path
from typing import Any

import pytest

from impedance.board_copy import NativeBoardOwner, empty_board
from impedance.pcbnew_adapter import snapshot_board
from impedance.source_evidence import SourceSnapshotReader, acquire_source_evidence
from tests.native_kicad_support import native_bindings, requires_native_plotting
from tests.native_wx_support import isolated_native

pytestmark = [
    pytest.mark.native_kicad,
    pytest.mark.kicad10_impedance,
    requires_native_plotting,
]
__all__ = ["native_bindings"]


def _fixture(pcbnew: Any, board: Any, path: Path) -> tuple[Any, Any, Any]:
    """Construct actual native objects with geometry, text and landing copper."""
    board.SetFileName(str(path))
    net = pcbnew.NETINFO_ITEM(board, "USB_P")
    board.Add(net)
    negative = pcbnew.NETINFO_ITEM(board, "USB_N")
    board.Add(negative)
    track = pcbnew.PCB_TRACK(board)
    track.SetStart(pcbnew.VECTOR2I(1_000_000, 1_000_000))
    track.SetEnd(pcbnew.VECTOR2I(5_000_000, 1_000_000))
    track.SetWidth(200_000)
    track.SetLayer(pcbnew.F_Cu)
    track.SetNet(net)
    board.Add(track)
    footprint = pcbnew.FOOTPRINT(board)
    footprint.SetReference("J1")
    footprint.Value().SetVisible(False)
    pad = pcbnew.PAD(footprint)
    pad.SetNumber("1")
    pad.SetShape(pcbnew.PAD_SHAPE_RECT)
    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    pad.SetSize(pcbnew.VECTOR2I(500_000, 700_000))
    layers = pcbnew.LSET()
    layers.AddLayer(pcbnew.F_Cu)
    pad.SetLayerSet(layers)
    pad.SetPosition(pcbnew.VECTOR2I(1_000_000, 1_000_000))
    pad.SetNet(net)
    footprint.Add(pad)
    board.Add(footprint)
    text = pcbnew.PCB_TEXT(board)
    text.SetText("${TITLE}")
    text.SetLayer(pcbnew.F_SilkS)
    text.SetPosition(pcbnew.VECTOR2I(3_000_000, 2_000_000))
    board.Add(text)
    return track, pad, text


def _record(board: Any, pcbnew: Any) -> tuple[str, ...]:
    """Independently inspect native definitions before/after proof acquisition."""
    formatter = pcbnew.PCB_IO_KICAD_SEXPR()
    records = []
    for method in ("GetTracks", "GetFootprints", "GetDrawings", "Zones"):
        for item in getattr(board, method)():
            formatter.Format(item)
            records.append(str(formatter.GetStringOutput(True)))
    return tuple(records)


@pytest.mark.parametrize(
    "change",
    [
        "width",
        "start",
        "end",
        "pad_size",
        "pad_shape",
        "pad_rotation",
        "pad_number",
        "text",
        "title",
        "project_bytes",
        "rule_bytes",
        "add",
        "remove",
    ],
)
def test_native_changed_source_cannot_reuse_the_previous_snapshot(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    change: str,
) -> None:
    """Raw setters and adjacent same-mtime contents remain fresh dependencies."""
    if isolated_native(request, "KICAD_SOURCE_EVIDENCE_CHILD", native="kicad"):
        return
    pcbnew = request.getfixturevalue("native_bindings").pcbnew
    with empty_board(pcbnew) as board:
        path = tmp_path / "evidence.kicad_pcb"
        track, pad, text = _fixture(pcbnew, board, path)
        project = path.with_suffix(".kicad_pro")
        project.write_text('{"text_variables":{"TOKEN":"before"}}', encoding="utf-8")
        rules = path.with_suffix(".kicad_dru")
        rules.write_text(
            '(version 1)\n(rule "before" (constraint clearance (min 0.1)))',
            encoding="utf-8",
        )
        original = snapshot_board(board, pcbnew)
        before = acquire_source_evidence(board, pcbnew)
        reader = SourceSnapshotReader()
        acquire = lambda: acquire_source_evidence(board, pcbnew)
        build = lambda: snapshot_board(board, pcbnew)
        assert reader.read(acquire, build, owner_key=(1,)) == original
        assert reader.read(acquire, build, owner_key=(1,)) is reader.read(
            acquire, build, owner_key=(1,)
        )
        if change == "width":
            track.SetWidth(210_000)
        elif change == "start":
            track.SetStart(pcbnew.VECTOR2I(1_000_001, 1_000_000))
        elif change == "end":
            track.SetEnd(pcbnew.VECTOR2I(5_000_001, 1_000_000))
        elif change == "pad_size":
            pad.SetSize(pcbnew.VECTOR2I(501_000, 700_000))
        elif change == "pad_shape":
            pad.SetShape(pcbnew.PAD_SHAPE_OVAL)
        elif change == "pad_rotation":
            pad.SetOrientationDegrees(0.1234567890123)
        elif change == "pad_number":
            pad.SetNumber("2")
        elif change == "text":
            text.SetText("new text")
        elif change == "title":
            board.GetTitleBlock().SetTitle("changed title")
        elif change in ("project_bytes", "rule_bytes"):
            target = project if change == "project_bytes" else rules
            stat = target.stat()
            target.write_bytes(target.read_bytes().replace(b"before", b"AFTER!"))
            os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            assert target.stat().st_mtime_ns == stat.st_mtime_ns
        elif change == "add":
            other = pcbnew.PCB_TRACK(board)
            other.SetStart(pcbnew.VECTOR2I(2_000_000, 3_000_000))
            other.SetEnd(pcbnew.VECTOR2I(4_000_000, 3_000_000))
            other.SetWidth(100_000)
            other.SetLayer(pcbnew.F_Cu)
            other.SetNet(track.GetNet())
            board.Add(other)
        else:
            board.Remove(track)
        native_before = _record(board, pcbnew)
        evidence = acquire_source_evidence(board, pcbnew)
        assert evidence != before
        assert _record(board, pcbnew) == native_before
        expected = snapshot_board(board, pcbnew)
        actual = reader.read(acquire, build, owner_key=(1,))
        assert expected != original
        assert actual == expected
        assert reader.statistics.builds == 2
        assert _record(board, pcbnew) == native_before
        reader.close()


def test_native_evidence_acquisition_preserves_live_state(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    """Repeated reads preserve geometry, settings, files and owner associations."""
    if isolated_native(request, "KICAD_SOURCE_EVIDENCE_CHILD", native="kicad"):
        return
    pcbnew = request.getfixturevalue("native_bindings").pcbnew
    with empty_board(pcbnew) as board:
        path = tmp_path / "evidence.kicad_pcb"
        _fixture(pcbnew, board, path)
        path.write_bytes(b"independent file bytes must remain unchanged")
        definitions = _record(board, pcbnew)
        settings = board.GetDesignSettings()
        thickness = settings.GetBoardThickness()
        project = board.GetProject()
        reference = snapshot_board(board, pcbnew)
        first = acquire_source_evidence(board, pcbnew)
        for _ in range(5):
            assert acquire_source_evidence(board, pcbnew) == first
        assert snapshot_board(board, pcbnew) == reference
        assert _record(board, pcbnew) == definitions
        assert board.GetDesignSettings().GetBoardThickness() == thickness
        assert board.GetProject() == project
        assert path.read_bytes() == b"independent file bytes must remain unchanged"


@pytest.mark.parametrize("location", ["board", "footprint"])
@pytest.mark.parametrize("change", ["readiness", "filled_polygons"])
def test_native_zone_runtime_state_is_freshly_bound_to_its_zone(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    location: str,
    change: str,
) -> None:
    """Observe filled polygons and NeedRefill even when definitions stay fixed."""
    if isolated_native(request, "KICAD_SOURCE_EVIDENCE_CHILD", native="kicad"):
        return
    pcbnew = request.getfixturevalue("native_bindings").pcbnew
    with empty_board(pcbnew) as board:
        _fixture(pcbnew, board, tmp_path / "zone-evidence.kicad_pcb")
        parent = board if location == "board" else next(iter(board.GetFootprints()))
        zone = pcbnew.ZONE(parent)
        zone.SetLayer(pcbnew.F_Cu)
        outline = zone.Outline()
        outline.NewOutline()
        for point in ((0, 0), (8_000_000, 0), (8_000_000, 8_000_000), (0, 8_000_000)):
            outline.Append(*point)
        filled = pcbnew.SHAPE_POLY_SET()
        filled.NewOutline()
        for point in (
            (100_000, 100_000),
            (7_000_000, 100_000),
            (7_000_000, 7_000_000),
            (100_000, 7_000_000),
        ):
            filled.Append(*point)
        zone.SetFilledPolysList(pcbnew.F_Cu, filled)
        zone.SetIsFilled(True)
        zone.SetNeedRefill(False)
        parent.Add(zone)
        before = acquire_source_evidence(board, pcbnew)
        original = snapshot_board(board, pcbnew)
        if change == "readiness":
            zone.SetNeedRefill(True)
        else:
            modified = pcbnew.SHAPE_POLY_SET()
            modified.NewOutline()
            for point in (
                (200_000, 100_000),
                (7_000_000, 100_000),
                (7_000_000, 7_000_000),
                (200_000, 7_000_000),
            ):
                modified.Append(*point)
            zone.SetFilledPolysList(pcbnew.F_Cu, modified)
        assert acquire_source_evidence(board, pcbnew) != before
        assert snapshot_board(board, pcbnew) != original


@pytest.mark.parametrize(
    "change",
    [
        "arc_midpoint",
        "arc_width",
        "arc_landing",
        "custom_pad",
        "pad_offset",
        "pad_rotation_precision",
    ],
)
def test_native_effective_perimeters_and_exact_angles_match_the_reference(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    change: str,
) -> None:
    """Preserve native arc strokes, custom perimeters and binary64 pad angles."""
    if isolated_native(request, "KICAD_SOURCE_EVIDENCE_CHILD", native="kicad"):
        return
    from math import nextafter
    import struct

    pcbnew = request.getfixturevalue("native_bindings").pcbnew
    with empty_board(pcbnew) as board:
        track, pad, _ = _fixture(
            pcbnew, board, tmp_path / "effective-evidence.kicad_pcb"
        )
        arc = pcbnew.PCB_ARC(board)
        arc.SetLayer(pcbnew.F_Cu)
        arc.SetWidth(10_000)
        arc.SetNet(track.GetNet())
        arc.SetStart(pcbnew.VECTOR2I(950_000, 1_000_000))
        arc.SetMid(pcbnew.VECTOR2I(975_000, 1_020_000))
        arc.SetEnd(pcbnew.VECTOR2I(1_000_000, 1_050_000))
        board.Add(arc)
        pad.SetOrientationDegrees(0.1234567890123)
        if change == "arc_landing":
            pad.SetPosition(pcbnew.VECTOR2I(20_000_000, 20_000_000))
        acquire = lambda: acquire_source_evidence(board, pcbnew)
        build = lambda: snapshot_board(board, pcbnew)
        reader = SourceSnapshotReader()
        original = reader.read(acquire, build, owner_key=(1,))
        before = acquire()
        if change == "arc_midpoint":
            assert next(
                trace for trace in original.traces if trace.is_native_arc
            ).copper_polygons
            arc.SetMid(pcbnew.VECTOR2I(975_001, 1_020_000))
        elif change == "arc_width":
            arc.SetWidth(10_001)
        elif change == "arc_landing":
            assert not next(
                trace for trace in original.traces if trace.is_native_arc
            ).copper_polygons
            pad.SetPosition(pcbnew.VECTOR2I(1_000_000, 1_000_000))
        elif change == "custom_pad":
            pad.SetShape(pcbnew.PAD_SHAPE_TRAPEZOID)
            pad.SetDelta(pcbnew.VECTOR2I(20_000, 0))
        elif change == "pad_offset":
            pad.SetOffset(pcbnew.VECTOR2I(10_000, 0))
        else:
            previous = float(pad.GetOrientationDegrees())
            pad.SetOrientationDegrees(nextafter(previous, float("inf")))
            assert struct.pack(">d", pad.GetOrientationDegrees()) != struct.pack(
                ">d", previous
            )
        expected = build()
        assert expected != original
        assert acquire() != before
        result = reader.read(acquire, build, owner_key=(1,))
        assert result == expected
        assert reader.statistics.builds == 2
        if change in ("arc_width", "arc_landing"):
            assert next(
                trace for trace in result.traces if trace.is_native_arc
            ).copper_polygons
        if change == "custom_pad":
            assert result.copper_features[0].polygons
        reader.close()


def test_native_runtime_flash_override_invalidates_fresh_copper_evidence(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    """Observe fresh connectivity-derived flash decisions and their native geometry."""
    if isolated_native(request, "KICAD_SOURCE_EVIDENCE_CHILD", native="kicad"):
        return
    pcbnew = request.getfixturevalue("native_bindings").pcbnew
    with empty_board(pcbnew) as board:
        _, pad, _ = _fixture(pcbnew, board, tmp_path / "flash-evidence.kicad_pcb")
        pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
        pad.SetUnconnectedLayerMode(pcbnew.UNCONNECTED_LAYER_MODE_REMOVE_ALL)
        pad.SetPosition(pcbnew.VECTOR2I(20_000_000, 20_000_000))
        board.BuildConnectivity()
        assert pad.FlashLayer(pcbnew.F_Cu) is False
        before = acquire_source_evidence(board, pcbnew)
        original = snapshot_board(board, pcbnew)
        pad.SetZoneLayerOverride(pcbnew.F_Cu, pcbnew.ZLO_FORCE_FLASHED)
        assert pad.FlashLayer(pcbnew.F_Cu) is True
        # KiCad 10 also serializes zone_layer_connections. Fresh FlashLayer
        # remains the authoritative effective copper decision in the adapter.
        assert acquire_source_evidence(board, pcbnew) != before
        assert snapshot_board(board, pcbnew) != original


@pytest.mark.parametrize("change", ["definition", "assignment"])
def test_native_detached_output_uses_fresh_live_netclass_metadata(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    change: str,
) -> None:
    """Observe live classes independently from a detached geometry owner's data."""
    if isolated_native(request, "KICAD_SOURCE_EVIDENCE_CHILD", native="kicad"):
        return
    pcbnew = request.getfixturevalue("native_bindings").pcbnew
    with empty_board(pcbnew) as live, empty_board(pcbnew) as detached:
        _fixture(pcbnew, live, tmp_path / "live.kicad_pcb")
        _fixture(pcbnew, detached, tmp_path / "detached.kicad_pcb")
        settings = live.GetDesignSettings().m_NetSettings
        configured = pcbnew.NETCLASS("Controlled")
        configured.SetDiffPairGap(150_000)
        settings.SetNetclass("Controlled", configured)
        if change == "definition":
            settings.SetNetclassPatternAssignment("USB_*", "Controlled")
        reader = SourceSnapshotReader()
        acquire = lambda: acquire_source_evidence(
            detached, pcbnew, netclass_source=live
        )
        build = lambda: snapshot_board(detached, pcbnew, netclass_source=live)
        before = acquire()
        first = reader.read(acquire, build, owner_key=(1, 2))
        detached_before = snapshot_board(detached, pcbnew)
        assert not first.net_class_error
        if change == "definition":
            configured.SetDiffPairGap(200_000)
        else:
            settings.SetNetclassPatternAssignment("USB_*", "Controlled")
        assert acquire() != before
        expected = build()
        assert expected != first
        assert reader.read(acquire, build, owner_key=(1, 2)) == expected
        assert snapshot_board(detached, pcbnew) == detached_before
        reader.close()


def test_native_pairing_changes_are_part_of_fresh_evidence(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    """Removing an unrouted coupled net invalidates pairing and class metadata."""
    if isolated_native(request, "KICAD_SOURCE_EVIDENCE_CHILD", native="kicad"):
        return
    pcbnew = request.getfixturevalue("native_bindings").pcbnew
    with empty_board(pcbnew) as board:
        _fixture(pcbnew, board, tmp_path / "pairing.kicad_pcb")
        original = snapshot_board(board, pcbnew)
        assert original.differential_pairs == (("USB_N", "USB_P"),)
        before = acquire_source_evidence(board, pcbnew)
        negative = next(
            net for net in board.GetNetsByName().values() if net.GetNetname() == "USB_N"
        )
        board.Remove(negative)
        expected = snapshot_board(board, pcbnew)
        assert expected.differential_pairs != original.differential_pairs
        assert acquire_source_evidence(board, pcbnew) != before


@pytest.mark.parametrize("change", ["layers", "thickness"])
def test_native_enabled_layers_and_board_settings_are_fresh(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    change: str,
) -> None:
    """Include stack order and rendering settings in source currentness proof."""
    if isolated_native(request, "KICAD_SOURCE_EVIDENCE_CHILD", native="kicad"):
        return
    pcbnew = request.getfixturevalue("native_bindings").pcbnew
    with empty_board(pcbnew) as board:
        _fixture(pcbnew, board, tmp_path / "settings.kicad_pcb")
        original = snapshot_board(board, pcbnew)
        before = acquire_source_evidence(board, pcbnew)
        if change == "layers":
            board.SetCopperLayerCount(4)
        else:
            settings = board.GetDesignSettings()
            settings.SetBoardThickness(settings.GetBoardThickness() + 1_000)
        assert snapshot_board(board, pcbnew) != original
        assert acquire_source_evidence(board, pcbnew) != before


def test_native_edit_restore_and_board_reload_preserve_snapshot_values(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    """Release a native allocation and require fresh proof after same-path reload."""
    if isolated_native(request, "KICAD_SOURCE_EVIDENCE_CHILD", native="kicad"):
        return
    pcbnew = request.getfixturevalue("native_bindings").pcbnew
    path = tmp_path / "restore.kicad_pcb"
    owner = empty_board(pcbnew)
    reader = SourceSnapshotReader()
    reopened = None
    board = owner.board
    track = None

    def owner_key() -> tuple[Any, ...]:
        """Use actual native allocation, persistent identity and filename values."""
        return int(board.this), str(board.m_Uuid.AsString()), str(board.GetFileName())

    acquire = lambda: acquire_source_evidence(board, pcbnew)
    build = lambda: snapshot_board(board, pcbnew)
    try:
        track = _fixture(pcbnew, board, path)[0]
        # Native save writes used nets only. Route the negative side as well so
        # this fixture tests the same electrical content after native reload.
        negative = pcbnew.PCB_TRACK(board)
        negative.SetStart(pcbnew.VECTOR2I(1_000_000, 1_500_000))
        negative.SetEnd(pcbnew.VECTOR2I(5_000_000, 1_500_000))
        negative.SetWidth(200_000)
        negative.SetLayer(pcbnew.F_Cu)
        negative.SetNet(
            next(
                net
                for net in board.GetNetsByName().values()
                if net.GetNetname() == "USB_N"
            )
        )
        board.Add(negative)
        negative = None
        original = reader.read(acquire, build, owner_key=owner_key())
        track.SetWidth(210_000)
        changed = reader.read(acquire, build, owner_key=owner_key())
        assert changed != original
        track.SetWidth(200_000)
        restored = reader.read(acquire, build, owner_key=owner_key())
        assert restored == original and restored is not original
        pcbnew.PCB_IO_KICAD_SEXPR().SaveBoard(str(path), board)
        persisted = build()
        reader.close()
        track = board = None
        owner.close()
        assert owner.closed
        loaded = pcbnew.PCB_IO_KICAD_SEXPR().LoadBoard(str(path), None)
        owner = NativeBoardOwner(loaded, pcbnew.BOARD.__swig_destroy__)
        board = owner.board
        loaded = None
        reopened = SourceSnapshotReader()
        reference = build()
        fresh = reopened.read(acquire, build, owner_key=owner_key())
        assert fresh == reference == persisted
        assert fresh is not restored
        assert reopened.statistics.builds == 1
        assert reopened.statistics.hits == 0
        assert reopened.read(acquire, build, owner_key=owner_key()) is fresh
        assert reopened.statistics.hits == 1
    finally:
        reader.close()
        if reopened is not None:
            reopened.close()
        track = board = None
        owner.close()
