"""Pure text policy and real-grid compaction, measurement, and font behavior."""

import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from typing import Any, Optional
from unittest.mock import patch

import pytest

from .native_wx_support import pump
from .variant_matrix_native_test_support import matrix, modules
from .variant_matrix_render_test_support import (
    column_widths,
    outlier_model,
    paint_cell,
    sample_model,
)
from .variant_model_test_support import changed

__all__ = ["matrix", "modules"]


@pytest.fixture
def layout(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "variant_text_layout_tests",
        Path(__file__).resolve().parents[1] / "variant/text_layout.py",
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "samples,widths,expected",
    [
        (("aaaa",) * 4 + ("outlier",), None, 4),
        (("aaaa", "aaaaa", "aaaaaa", "aaaaaaa", "aaaaaaaa", "a" * 20), None, 8),
        (("", "a", "abc"), None, 3),
        ((), None, 0),
        (
            ("iiii", "iiiiiiii", "MMMM", "MMMMM", "WWWWWW"),
            {"iiii": 4, "iiiiiiii": 8, "MMMM": 40, "MMMMM": 50, "WWWWWW": 60},
            50,
        ),
        (
            ("iiii",) * 4 + ("outlier", "WWW"),
            {"iiii": 4, "outlier": 100, "WWW": 60},
            60,
        ),
        (("   M",) * 4 + ("outlier",), {"   M": 10, "outlier": 100}, 10),
    ],
)
def test_percentile_counts_rows_and_keeps_short_wide_values(
    layout: ModuleType,
    samples: tuple[str, ...],
    widths: Optional[dict[str, int]],
    expected: int,
) -> None:
    assert (
        layout.percentile_text_width(
            samples, len if widths is None else widths.__getitem__
        )
        == expected
    )


@pytest.mark.parametrize(
    "text,width,display,scale,start,prefix",
    [
        ("abcdefghij", 100, "abcdefghij", 1, 0, 0),
        ("abcdefghij", 90, "abcdefghij", 0.9, 0, 0),
        ("abcdefghijklmnopqrst", 60, "…opqrst", 0.8, 14, 1),
        ("abcdef", 0, "", 0.8, 6, 0),
        ("abcdef", 3, "", 0.8, 6, 0),
        ("abcdef", 7, "", 0.8, 6, 0),
        ("  Logic & Signal\t0603  ", 500, "  Logic & Signal\t0603  ", 1, 0, 0),
    ],
)
def test_suffix_fitting_preserves_exact_text_and_maximizes_readable_size(
    layout: ModuleType,
    text: str,
    width: int,
    display: str,
    scale: float,
    start: int,
    prefix: int,
) -> None:
    def measure(value: str, factor: float) -> int:
        return round(len(value) * 10 * factor)

    fit = layout.fit_text_suffix(text, width, measure)
    assert (fit.display_text, fit.source_start, fit.prefix_length) == (
        display,
        start,
        prefix,
    )
    assert scale <= fit.scale < scale + 0.01
    assert measure(fit.display_text, fit.scale) <= width
    if prefix:
        assert measure("…" + text[start - 1 :], fit.scale) > width
    assert layout.fit_text_suffix(text, 1000, measure) == layout.FittedText(text, 1)


@pytest.mark.parametrize(
    "width,expected", [(60, ((1, 3), (5, 7))), (200, ((1, 4), (12, 16), (18, 20)))]
)
def test_parameter_highlights_map_only_visible_original_characters(
    layout: ModuleType, width: int, expected: tuple[tuple[int, int], ...]
) -> None:
    fit = layout.fit_text_suffix(
        "abcdefghijklmnopqrst", width, lambda text, scale: round(len(text) * 10 * scale)
    )
    assert layout.visible_highlight_spans(((1, 4), (12, 16), (18, 20)), fit) == expected


@pytest.mark.parametrize("field", ["footprint", "value", "params"])
@pytest.mark.native_wx
@pytest.mark.usefixtures("matrix")
def test_real_compaction_preserves_source_and_hidden_row_statistics(
    matrix: Any, modules: tuple[ModuleType, ModuleType], field: str
) -> None:
    model = outlier_model(modules[1])
    if field == "value":
        model = modules[1].MatrixModel(
            changed(
                model.snapshot,
                component="component-5",
                variants=("A",),
                value="rare_value_" * 12 + "_different",
            ),
            enrichment=model._metadata,
            show_footprint_library=True,
        )

    def check(h: Any) -> None:
        view, wx = h.view, h.wx
        col = model.column_for(None if field == "footprint" else "A", field)
        font = view.GetDefaultCellFont()
        dc = wx.ClientDC(view)
        dc.SetFont(font)
        full = model.get_display(4, col)
        original = view.GetColSize(col)
        all_widths = column_widths(view)
        if field != "footprint":
            assert original > view.GetColSize(model.column_for("B", field))
        assert original < dc.GetTextExtent(full).width / 2
        painted = paint_cell(h, 4, col)
        text = painted.of("DrawText")[0]
        if field == "value":
            marker = painted.of("DrawText")[-1]
            assert (
                marker["args"][0] == "≠"
                and marker["size"] == font.GetFractionalPointSize()
            )
        assert text["args"][0].startswith("…") and full.endswith(text["args"][0][1:])
        assert (
            font.GetFractionalPointSize() * 0.8 - 0.01
            <= text["size"]
            < font.GetFractionalPointSize()
        )
        assert dict(model.copy_cell(4, col).rows[0].values)[field] == full
        assert full in model.cell_details(4, col)
        assert (
            paint_cell(h, 0, col).of("DrawText")[0]["size"]
            == font.GetFractionalPointSize()
        )
        model.set_filter(require_bom=True)
        model.sort_by(col, descending=True)
        view.set_model(model)
        assert len(model.rows) == 1 and view.GetColSize(col) == original
        assert column_widths(view) == all_widths
        assert paint_cell(h, 0, col).of("DrawText")[0]["args"] == text["args"]
        hidden = h.model_module.MatrixModel(
            changed(model.snapshot, pop=False),
            enrichment=model._metadata,
            show_footprint_library=True,
        )
        hidden.set_filter(require_pop=True)
        assert not hidden.rows
        view.set_model(hidden)
        assert view.GetColSize(col) == original

    matrix(check, model=model)


@pytest.mark.native_wx
def test_native_text_cache_and_font_dpi_invalidation(
    matrix: Any, modules: tuple[ModuleType, ModuleType]
) -> None:
    def check(h: Any) -> None:
        view, wx = h.view, h.wx
        font = wx.Font(view.GetDefaultCellFont())
        font.SetFractionalPointSize(10.5)
        font.MakeBold()
        dc = wx.ClientDC(view)
        measure = dc.GetTextExtent
        seen = []

        def measured(text: str) -> Any:
            seen.append(text)
            return measure(text)

        with patch.object(dc, "GetTextExtent", measured):
            fit = view._fit_text(dc, font, "W" * 30, 60)
            count = len(seen)
            assert view._fit_text(dc, font, "W" * 30, 60) == fit and len(seen) == count
            assert view._fit_text(dc, font, "W" * 30, 1000).display_text == "W" * 30
        small = view._scaled_text_font(font, 0.8)
        assert small.GetFractionalPointSize() == pytest.approx(8.4, abs=0.5)
        assert (
            small.GetWeight() == font.GetWeight()
            and font.GetFractionalPointSize() == 10.5
        )
        assert view._text_width_cache
        view._on_dpi_changed(wx.CommandEvent())
        assert not view._text_width_cache
        pump(wx)
        view.SetDefaultCellBackgroundColour(wx.Colour(20, 20, 20))
        view.SetDefaultCellTextColour(wx.Colour(240, 240, 240))
        boundary = model.column_for("A", "price")
        before = view.GetColGridLinePen(boundary).GetColour()
        view.SetGridLineColour(wx.Colour(255, 0, 255))
        view.GetEventHandler().ProcessEvent(wx.SysColourChangedEvent())
        assert view.GetDefaultCellBackgroundColour() == wx.SystemSettings.GetColour(
            wx.SYS_COLOUR_LISTBOX
        )
        assert view.GetDefaultCellTextColour() == wx.SystemSettings.GetColour(
            wx.SYS_COLOUR_LISTBOXTEXT
        )
        assert view.GetGridLineColour() != wx.Colour(255, 0, 255)
        assert view.GetColGridLinePen(boundary).GetColour() != before

    model = outlier_model(modules[1])
    matrix(check, model=model)


@pytest.mark.parametrize("narrow", [False, True])
@pytest.mark.native_wx
def test_parameter_colors_keep_one_native_glyph_layout_and_exclude_ellipsis(
    matrix: Any, modules: tuple[ModuleType, ModuleType], narrow: bool
) -> None:
    source = "irrelevant AVAV prefix " * 10 + "0603 10kΩ"
    model = sample_model(
        modules[1],
        catalog={
            (row, variant): {"params": source}
            for row in range(3)
            for variant in ("", "A", "B")
        },
    )

    def check(h: Any) -> None:
        col = model.column_for("A", "params")
        painted = paint_cell(h, 0, col, width=100 if narrow else 2000)
        baseline, *colored = painted.of("DrawText")
        assert len(colored) == 2
        assert all(
            item["args"] == baseline["args"] and item["size"] == baseline["size"]
            for item in colored
        )
        text, x, y = baseline["args"]
        assert text.startswith("…") is narrow
        dc = h.wx.ClientDC(h.view)
        dc.SetFont(h.view.GetDefaultCellFont().Scaled(0.8 if narrow else 1))
        extents = dc.GetPartialTextExtents(text)
        for item, term in zip(colored, ("0603", "10kΩ")):
            left, right = text.index(term), text.index(term) + len(term)
            clip = item["clip"][-4:]
            assert clip[0] == x + (extents[left - 1] if left else 0)
            assert clip[2] == extents[right - 1] - (extents[left - 1] if left else 0)
            assert clip[0] >= x + (extents[0] if narrow else 0)
            assert clip[0] + clip[2] <= x + baseline["extent"][0]

    matrix(check, model=model)


@pytest.mark.parametrize("field,text", [("value", "4.7k"), ("lcsc", "C456")])
@pytest.mark.parametrize("different", [False, True])
@pytest.mark.native_wx
def test_visible_assignment_text_and_difference_marker_are_measured_without_match_letters(
    matrix: Any,
    modules: tuple[ModuleType, ModuleType],
    field: str,
    text: str,
    different: bool,
) -> None:
    model = sample_model(
        modules[1], changes={(0, "A"): {field: text}} if different else {}
    )
    text = model.get_display(0, model.column_for("A", field))

    def check(h: Any) -> None:
        col = model.column_for("A", field)
        painted = paint_cell(h, 0, col, width=300)
        assert [item["args"][0] for item in painted.of("DrawText")] == [text] + (
            ["≠"] if different else []
        )
        dc = h.wx.ClientDC(h.view)
        attr = h.view_module.gridlib.GridCellAttr()
        attr.SetFont(h.view.GetCellFont(0, col))
        best = h.view_module.MatrixCellRenderer().GetBestSize(h.view, attr, dc, 0, col)
        dc.SetFont(attr.GetFont())
        assert best.width >= dc.GetTextExtent(text).width + (
            dc.GetTextExtent("≠").width if different else 0
        )

    matrix(check, model=model)
