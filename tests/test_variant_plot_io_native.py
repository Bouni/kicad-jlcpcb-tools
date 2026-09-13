"""Check temporary board save/load and ownership in disposable KiCad processes."""

from collections.abc import Callable
import gc
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import weakref

import pytest

from tests.native_kicad_support import _native_board, native_bindings, native_runtime
from tests.native_wx_support import isolated_native

pytestmark = [
    pytest.mark.native_kicad,
    pytest.mark.skipif(
        os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
        reason="native KiCad plotting requires an explicitly enabled desktop session",
    ),
]

__all__ = ["native_bindings", "native_runtime"]


def _properties(board: Any) -> dict[str, str]:
    """Compare text values independently of KiCad's native wxString key hashes."""
    return {str(key): str(value) for key, value in dict(board.GetProperties()).items()}


@pytest.mark.parametrize(
    "case",
    [
        "save-directory",
        "save-readonly",
        "missing-load",
        "corrupt-load",
        "lifetime",
    ],
)
def test_native_temporary_board_save_load_and_ownership(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """I/O failures reach Python cleanup; successful clones have one releasing owner."""
    if os.environ.get("KICAD_PLOT_IO_CHILD") != "1":
        pcbnew = pytest.importorskip("pcbnew", reason="KiCad pcbnew is unavailable")
        if not all(
            hasattr(pcbnew.BOARD, name)
            for name in ("AddVariant", "GetVariantNamesForUI")
        ):
            pytest.skip("KiCad native variant APIs are unavailable")
    if isolated_native(request, "KICAD_PLOT_IO_CHILD", native="kicad"):
        return

    runtime = request.getfixturevalue("native_runtime")
    board, session = runtime.board, runtime.session
    board.SetCurrentVariant("B")
    project_id = int(board.GetProject())
    properties = _properties(board)
    source = Path(board.GetFileName())
    saved = source.read_bytes()
    project_path = source.with_suffix(".kicad_pro")
    saved_project = project_path.read_bytes()
    text = next(item for item in board.Drawings() if hasattr(item, "GetText"))
    text.SetText("Unsaved text: ${EXPORT_TEST}")
    exporter = runtime.fabrication.Fabrication(SimpleNamespace(settings={}), board)
    session.set_output_variant("A")
    for _ in range(20 if case == "lifetime" else 1):
        snapshot, name = session.begin_generation()
        exporter.begin_generation(snapshot, name, (), session.validate_generation)
        staging = Path(exporter._generation.directory.name)
        try:
            if case == "lifetime":
                clone = exporter._get_plot_board()
                assert int(clone.this) != int(board.this)
                release = clone._jlcpcb_release
                assert release.alive, "The detached plot board must have an owner"
                exporter._own_board_copy(clone)
                assert clone._jlcpcb_release is release
                assert str(clone.GetCurrentVariant()) == "A"
                assert _properties(clone)["EXPORT_TEST"] == "PROJECT-TOKEN"
                assert any(
                    str(item.GetText()) == "Unsaved text: ${EXPORT_TEST}"
                    for item in clone.Drawings()
                    if hasattr(item, "GetText")
                )
                reference = weakref.ref(clone)
                del clone
            else:
                if case == "save-directory":
                    (staging / "plot-source.kicad_pcb").mkdir()
                elif case == "save-readonly":
                    staging.chmod(0o500)
                else:
                    serialize = exporter._serialize_board

                    def damage_source(
                        destination: Path, serialize: Callable[[Path], str] = serialize
                    ) -> str:
                        digest = serialize(destination)
                        if case == "missing-load":
                            destination.unlink()
                        else:
                            destination.write_text(
                                "(kicad_pcb (invalid", encoding="utf-8"
                            )
                        return digest

                    monkeypatch.setattr(exporter, "_serialize_board", damage_source)
                with pytest.raises((OSError, RuntimeError, ValueError)):
                    exporter._get_plot_board()
        finally:
            if case == "save-readonly":
                staging.chmod(0o700)
            exporter.abort_generation()
            session.end_generation()
        gc.collect()
        if case == "lifetime":
            assert reference() is None, "Generation cleanup retained its plot board"
            assert not release.alive, "Generation cleanup did not release its board"
        assert not staging.exists()
        assert str(board.GetCurrentVariant()) == "B"
        assert int(board.GetProject()) == project_id
        assert _properties(board) == properties
        assert source.read_bytes() == saved
        assert project_path.read_bytes() == saved_project
        session.refresh()
    if case != "lifetime":
        monkeypatch.undo()
        snapshot, name = session.begin_generation()
        exporter.begin_generation(snapshot, name, (), session.validate_generation)
        try:
            clone = exporter._get_plot_board()
            assert str(clone.GetCurrentVariant()) == "A"
            assert _properties(clone)["EXPORT_TEST"] == "PROJECT-TOKEN"
        finally:
            exporter.abort_generation()
            session.end_generation()
        assert str(board.GetCurrentVariant()) == "B"
        assert source.read_bytes() == saved
        assert project_path.read_bytes() == saved_project


@pytest.mark.parametrize("start", ["cold", "warm", "reopened"])
def test_native_project_variables_remain_separate(
    request: pytest.FixtureRequest, tmp_path: Path, start: str
) -> None:
    """Actual project files supply distinct values through save/reopen and copies."""
    if isolated_native(request, "KICAD_PROJECT_COPY_CHILD", native="kicad"):
        return
    bindings = request.getfixturevalue("native_bindings")
    pcbnew = bindings.pcbnew
    if start == "warm":
        default = pcbnew.BOARD()
        assert default is not None
    runtime = request.getfixturevalue("native_runtime")
    second_path = tmp_path / "second"
    second_path.mkdir()
    second = _native_board(pcbnew, second_path, "SECOND-TOKEN")
    boards = [runtime.board, second]
    assert int(boards[0].GetProject()) != int(boards[1].GetProject())
    for board, token in zip(boards, ("PROJECT-TOKEN", "SECOND-TOKEN")):
        filename = Path(board.GetFileName())
        project = filename.with_suffix(".kicad_pro")
        assert json.loads(project.read_text())["text_variables"]["EXPORT_TEST"] == token
        board.SynchronizeProperties()
        assert _properties(board)["EXPORT_TEST"] == token
        assert pcbnew.SaveBoard(str(filename), board)
        if start == "reopened":
            board = pcbnew.LoadBoard(str(filename))
            runtime.fabrication.Fabrication._own_board_copy(board)
            board.SynchronizeProperties()
            assert _properties(board)["EXPORT_TEST"] == token
        board.SetCurrentVariant("B")
        saved, saved_project = filename.read_bytes(), project.read_bytes()
        identity = int(board.GetProject())
        adapter = type(runtime.session.adapter)(board, f"project-{token}")
        exporter = runtime.fabrication.Fabrication(SimpleNamespace(settings={}), board)
        exporter.begin_generation(adapter.snapshot(), "A", (), lambda: None)
        try:
            clone = exporter._get_plot_board()
            assert int(clone.GetProject()) == identity
            assert _properties(clone)["EXPORT_TEST"] == token
            assert str(clone.GetCurrentVariant()) == "A"
            del clone
        finally:
            exporter.abort_generation()
        gc.collect()
        assert int(board.GetProject()) == identity
        assert _properties(board)["EXPORT_TEST"] == token
        assert str(board.GetCurrentVariant()) == "B"
        assert filename.read_bytes() == saved
        assert project.read_bytes() == saved_project


@pytest.mark.parametrize("copper_layers", [2, 6])
@pytest.mark.parametrize("fonts", ["pending", "retained"])
def test_native_copy_preserves_fonts_layers_and_groups(
    request: pytest.FixtureRequest, tmp_path: Path, copper_layers: int, fonts: str
) -> None:
    """Formatting preserves font data, custom layers, through pads, and nested groups."""
    if isolated_native(request, "KICAD_FORMAT_COPY_CHILD", native="kicad"):
        return
    runtime = request.getfixturevalue("native_runtime")
    pcbnew, fabrication = runtime.pcbnew, runtime.fabrication.Fabrication
    content = (
        f'(kicad_pcb (version {pcbnew.SEXPR_BOARD_FILE_VERSION}) (generator "pcbnew")'
        '(gr_text "Embedded font" (at 10 10) (layer "F.SilkS")'
        '(effects (font (face "DejaVu Sans") (size 1 1))))(embedded_fonts yes))'
    ).encode()
    source = tmp_path / "fonts.kicad_pcb"
    source.write_bytes(content)
    board = fabrication._load_board_copy(source, hashlib.sha256(content).hexdigest())
    if fonts == "retained":
        formatter = pcbnew.STRING_FORMATTER()
        writer = pcbnew.PCB_IO_KICAD_SEXPR()
        writer.FormatBoardToFormatter(formatter, board)
        content = (
            formatter.GetString()
            .replace("(embedded_fonts yes)", "(embedded_fonts no)")
            .encode("utf-8")
        )
        assert b"(embedded_files" in content, "The fixture must contain a real font"
        source.write_bytes(content)
        board = fabrication._load_board_copy(
            source, hashlib.sha256(content).hexdigest()
        )
    board.SetFileName(str(source))
    board.SetCopperLayerCount(copper_layers)
    board.SetLayerName(pcbnew.F_Cu, "Custom front")
    board.SetLayerName(pcbnew.F_Fab, "Custom fab")
    footprint = pcbnew.FOOTPRINT(board)
    footprint.SetReference("J1")
    pad = pcbnew.PAD(footprint)
    pad.SetNumber("1")
    pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
    pad.SetLayerSet(pcbnew.LSET.AllCuMask())
    footprint.Add(pad)
    board.Add(footprint)
    inner, outer = pcbnew.PCB_GROUP(board), pcbnew.PCB_GROUP(board)
    inner.SetName("inner")
    outer.SetName("outer")
    inner.AddItem(footprint)
    outer.AddItem(inner)
    board.Add(inner)
    board.Add(outer)
    exporter = fabrication(SimpleNamespace(settings={}), board)
    before = exporter._board_content()
    assert (b"(embedded_files" in before) is (fonts == "retained")
    saved = source.read_bytes()
    blocked = tmp_path / "not-a-file"
    blocked.mkdir()
    with pytest.raises(IsADirectoryError):
        exporter._serialize_board(blocked)
    assert exporter._board_content() == before
    target = tmp_path / "copy.kicad_pcb"
    digest = exporter._serialize_board(target)
    clone = exporter._load_board_copy(target, digest)
    assert clone.GetCopperLayerCount() == copper_layers
    assert str(clone.GetLayerName(pcbnew.F_Cu)) == "Custom front"
    assert str(clone.GetLayerName(pcbnew.F_Fab)) == "Custom fab"
    cloned_pad = next(iter(clone.FindFootprintByReference("J1").Pads()))
    assert tuple(cloned_pad.GetLayerSet().Seq()) == tuple(pad.GetLayerSet().Seq())
    assert clone.GroupsSanityCheck() == ""
    assert {str(group.GetName()) for group in clone.Groups()} == {"inner", "outer"}
    assert {
        str(group.GetName()): {
            str(member.m_Uuid.AsString()) for member in group.GetItems()
        }
        for group in clone.Groups()
    } == {
        "inner": {str(footprint.m_Uuid.AsString())},
        "outer": {str(inner.m_Uuid.AsString())},
    }
    copied = fabrication(SimpleNamespace(settings={}), clone)._board_content()
    assert copied == before
    assert exporter._board_content() == before
    assert source.read_bytes() == saved
