"""Share compatible native bindings and one owned wx application per test."""

from collections.abc import Iterator
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from .wx_harness import load_siblings


@pytest.fixture
def native_bindings() -> Iterator[SimpleNamespace]:
    """Reuse one SWIG module per process and release only our application."""
    # Scoped test doubles restore this import; keep one set of native wrappers
    # instead of registering another set against the same extension module.
    pcbnew = pytest.importorskip("pcbnew", reason="KiCad pcbnew is unavailable")
    if not all(
        hasattr(pcbnew.BOARD, name) for name in ("AddVariant", "GetVariantNamesForUI")
    ):
        pytest.skip("KiCad native variant APIs are unavailable")
    wx = pytest.importorskip("wx", reason="KiCad plotting requires wxPython")
    owns_app = wx.GetApp() is None
    app = wx.App(False) if owns_app else wx.GetApp()
    try:
        yield SimpleNamespace(pcbnew=pcbnew, wx=wx, app=app)
    finally:
        if owns_app:
            app.Destroy()


def _native_position(pcbnew: ModuleType, x: float, y: float) -> Any:
    """Create a native point from millimetres."""
    return pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))


def _native_board(
    pcbnew: ModuleType, directory: Path, project_token: str = "PROJECT-TOKEN"
) -> Any:
    """Create base, DNP, and substituted assemblies with visible fabrication text."""
    (directory / "source.kicad_pro").write_text(
        json.dumps(
            {
                "meta": {"version": 1},
                "text_variables": {"EXPORT_TEST": project_token},
            }
        ),
        encoding="utf-8",
    )
    filename = str(directory / "source.kicad_pcb")
    board = pcbnew.NewBoard(filename)
    assert board is not None, "KiCad could not create the native fixture board"
    # KiCad 10.0.6 NewBoard saves the file without setting BOARD::m_fileName.
    # Raw PCB_IO_MGR.Save does not safely translate an empty-path I/O error.
    board.SetFileName(filename)
    board.AddVariant("A")
    board.AddVariant("B")
    board.SetVariantDescription("A", "DNP assembly")
    board.SetLayerName(pcbnew.F_Fab, "JLC_Fab")
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference("R1")
    fp.SetValue("BASEVALUE")
    fp.SetFPID(pcbnew.LIB_ID("Test", "SOT-23"))
    fp.SetField("LCSC", "C123")
    fp.SetPosition(_native_position(pcbnew, 30, 30))
    for index, field in enumerate((fp.Reference(), fp.Value(), fp.GetField("LCSC"))):
        field.SetLayer(pcbnew.F_Fab)
        field.SetVisible(True)
        field.SetPosition(_native_position(pcbnew, 30, 25 + index * 2))
        field.SetTextSize(_native_position(pcbnew, 1, 1))
        field.SetTextThickness(pcbnew.FromMM(0.15))
    shape = pcbnew.PCB_SHAPE(fp)
    shape.SetShape(pcbnew.SHAPE_T_RECT)
    shape.SetStart(_native_position(pcbnew, 27, 27))
    shape.SetEnd(_native_position(pcbnew, 33, 33))
    shape.SetLayer(pcbnew.F_Fab)
    shape.SetWidth(pcbnew.FromMM(0.1))
    fp.Add(shape)
    board.Add(fp)
    variant = fp.AddVariant("A")
    variant.SetFieldValue("Value", "VARIANTVALUE")
    variant.SetFieldValue("LCSC", "C999")
    variant.SetDNP(True)
    variant = fp.AddVariant("B")
    variant.SetFieldValue("Value", "ROTATED")
    variant.SetFieldValue("LCSC", "C777")
    text = pcbnew.PCB_TEXT(board)
    text.SetText("${VARIANT}; ${VARIANT_DESC}; ${EXPORT_TEST}")
    text.SetPosition(_native_position(pcbnew, 30, 40))
    text.SetLayer(pcbnew.F_Fab)
    text.SetTextSize(_native_position(pcbnew, 1, 1))
    text.SetTextThickness(pcbnew.FromMM(0.15))
    board.Add(text)
    return board


@pytest.fixture
def native_runtime(
    native_bindings: SimpleNamespace, tmp_path: Path
) -> Iterator[SimpleNamespace]:
    """Load production export objects around a disposable native variant project."""
    with load_siblings(
        "_variant_fabrication_native_test",
        (
            "fabrication",
            "variant.native",
            "variant.store",
            "variant.session",
            "correction_data",
        ),
        {},
    ) as modules:
        pcbnew = native_bindings.pcbnew
        board = _native_board(pcbnew, tmp_path)
        source = tmp_path / "source.kicad_pcb"
        assert str(board.GetFileName()) == str(source), (
            "The native fixture board must retain its explicit saved filename"
        )
        pcbnew.PCB_IO_MGR.Save(pcbnew.PCB_IO_MGR.KICAD_SEXP, str(source), board)
        parent = SimpleNamespace(settings={}, save_settings=lambda: None)
        cache = modules["variant.store"].VariantStore(parent, str(tmp_path), board)
        adapter = modules["variant.native"].VariantNativeAdapter(board, cache.board_id)
        session = modules["variant.session"].VariantSession(
            adapter, cache, get_board=lambda: board
        )
        yield SimpleNamespace(
            pcbnew=pcbnew,
            board=board,
            session=session,
            fabrication=modules["fabrication"],
            correction_data=modules["correction_data"],
        )
