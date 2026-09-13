"""Independent native sizing workflows: columns, headers, preferences and refresh."""

from types import ModuleType
from typing import Any

import pytest

from .native_wx_support import pump
from .variant_matrix_native_test_support import matrix, modules, native_marks
from .variant_matrix_render_test_support import (
    column_widths,
    outlier_model,
    paint_cell,
    paint_header,
    resize,
    sample_model,
)
from .variant_model_test_support import changed

__all__ = ["matrix", "modules"]
pytestmark = native_marks
STATUS = {"type", "standard", "stock", "price"}


@pytest.mark.parametrize(
    "populated,prepared",
    [(False, True), (True, True), (True, False)],
    ids=["empty", "prepared", "pending"],
)
def test_native_body_and_both_header_tiers_fit_through_font_roundtrip(
    matrix: Any, modules: tuple[ModuleType, ModuleType], populated: bool, prepared: bool
) -> None:
    model = sample_model(
        modules[1],
        rows=3 if populated else 0,
        catalog={
            (row, variant): {
                "type": kind,
                "params": "10kΩ 0603",
                "standard": variant == "B",
                "stock": 1000,
                "price": 0.1,
            }
            for row in range(3)
            for variant, kind in (("", "Basic"), ("A", "Extended"), ("B", "Preferred"))
        },
    )
    if not prepared:
        model = modules[1].MatrixModel(model.snapshot, enrichment=model._metadata)

    def check(h: Any) -> None:
        view, wx = h.view, h.wx
        body, label = wx.Font(view.GetDefaultCellFont()), wx.Font(view.GetLabelFont())
        original, original_icon = None, view.flag_bitmap(True).GetWidth()
        for scale in (1, 2, 1):
            view.SetDefaultCellFont(body.Scaled(scale))
            view.SetLabelFont(label.Scaled(scale))
            pump(wx)
            widths = column_widths(view)
            if original is None:
                original = widths
            elif scale == 1:
                assert widths == original
            else:
                assert all(after > before for after, before in zip(widths, original))
            assert (view.flag_bitmap(True).GetWidth() > original_icon) is (scale == 2)
            assert (
                len({widths[i] for i, c in enumerate(model.columns) if c.key in STATUS})
                == 1
            )
            assert (
                widths[model.column_for("A", "stock")]
                < widths[model.column_for("A", "bom")]
            )
            for _ in range(2):
                view._auto_widths = None
                view._apply_column_widths()
                assert column_widths(view) == widths
            header = paint_header(view, wx)
            assert sum(item.rotation != 0 for item in header.text) == 3 + 7 * len(
                model.variants
            )
            assert {item.text for item in header.text if not item.rotation} >= {
                "Default",
                "A",
                "B",
                "Ref",
                "Footprint",
            }
            for item in header.text:
                assert wx.Rect(*item.rect).Contains(wx.Rect(*item.bounds)), item.text
                assert item.weight == label.GetWeight()
                if item.rotation:
                    assert item.rotation == 90
                    assert wx.Rect(*item.clip).Contains(wx.Rect(*item.bounds)), (
                        item.text
                    )
                    factor = {
                        "Side": 0.8,
                        "Type": 0.765,
                        "Std": 0.765,
                        "Stock": 0.765,
                        "Price": 0.765,
                    }.get(item.text, 1)
                    assert (
                        item.size
                        == view.GetLabelFont().Scaled(factor).GetFractionalPointSize()
                    )
            for col, column in enumerate(model.columns):
                assert view.GetTable().GetColLabelValue(col) == (
                    column.label
                    if column.variant is None
                    else f"{model.variant_label(column.variant)} · {column.label}"
                )
                if not populated:
                    continue
                painted = paint_cell(h, 0, col)
                texts = painted.of("DrawText")
                if column.key not in {"bom", "pos", "pop", "stock"}:
                    assert texts, column.key
                for item in texts:
                    text, x, y = item["args"]
                    w, height = item["extent"]
                    assert painted.rect.Contains(wx.Rect(x, y, w, height)), (
                        column.key,
                        text,
                    )
                    if column.key in STATUS:
                        assert (
                            abs(x + w / 2 - (painted.rect.x + painted.rect.width / 2))
                            <= 1
                        )
                    if column.key in {"pcb_angle", "correction"}:
                        assert item["size"] == pytest.approx(
                            body.GetFractionalPointSize() * scale * 0.8, abs=0.5
                        )
                for item in painted.of("DrawBitmap"):
                    bitmap, x, y, *_ = item["args"]
                    assert painted.rect.Contains(
                        wx.Rect(x, y, bitmap.GetWidth(), bitmap.GetHeight())
                    )
                if column.key in {
                    "ref",
                    "value",
                    "params",
                    "lcsc",
                    "correction",
                    "side",
                }:
                    ink = [
                        item.bounds[2]
                        for item in header.text
                        if item.text == column.label
                    ]
                    # Count overlapping text only once, without treating the
                    # whitespace before a right-aligned status marker as ink.
                    body_ink = set()
                    for item in texts:
                        x = item["args"][1]
                        body_ink.update(range(x, x + item["extent"][0]))
                    if column.key == "lcsc":
                        body_ink.update(
                            range(
                                painted.rect.x, min(item["args"][1] for item in texts)
                            )
                        )
                    ink.append(len(body_ink))
                    assert widths[col] - max(ink) <= view.FromDIP(24), column.key
            assert view.capture_preferences()["widths"] == {}
        if not populated:
            col = model.column_for("A", "type")
            resize(h, col, view.FromDIP(130))
            assert view.capture_preferences()["widths"] == {"information": 130}
            resize(h, col)
            assert column_widths(view) == original
            assert view.capture_preferences()["widths"] == {}

    matrix(check, model=model)


def test_autosizing_failure_restores_labels_and_native_batch_then_recovers(
    matrix: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def check(h: Any) -> None:
        view = h.view
        labels = [
            view.GetTable().GetColLabelValue(i) for i in range(view.GetNumberCols())
        ]
        widths = column_widths(view)

        def fail(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("renderer failed")

        view._auto_widths = None
        with monkeypatch.context() as patch:
            patch.setattr(view, "AutoSizeColumns", fail)
            with pytest.raises(RuntimeError, match="renderer failed"):
                view._apply_column_widths()
        assert not view._resizing_columns and view._auto_widths is None
        assert view.GetBatchCount() == 0
        assert [
            view.GetTable().GetColLabelValue(i) for i in range(view.GetNumberCols())
        ] == labels
        view._apply_column_widths()
        assert column_widths(view) == widths

    matrix(check)


@pytest.mark.parametrize(
    "field,wide",
    [
        ("ref", False),
        ("ref", True),
        ("footprint", False),
        ("footprint", True),
        ("value", True),
        ("params", True),
        ("lcsc", False),
        ("stock", False),
    ],
)
def test_manual_width_scope_survives_reopen_and_resets_to_content(
    matrix: Any, modules: tuple[ModuleType, ModuleType], field: str, wide: bool
) -> None:
    model = outlier_model(modules[1])

    def check(h: Any) -> None:
        view = h.view
        col = model.column_for(None if field in {"ref", "footprint"} else "A", field)
        before = column_widths(view)
        chosen = max(before[col] + 15, view.FromDIP(1200 if wide else 76))
        if wide:
            dc = h.wx.ClientDC(view)
            dc.SetFont(view.GetDefaultCellFont())
            chosen = max(
                chosen,
                dc.GetTextExtent(model.get_display(4, col)).width + view.FromDIP(30),
            )
            del dc
        resize(h, col, chosen)
        saved = view.capture_preferences()
        targets = (
            {i for i, column in enumerate(model.columns) if column.key in STATUS}
            if field in STATUS
            else {col}
        )
        assert saved["widths"] == (
            {"information": view.ToDIP(chosen)}
            if field in STATUS
            else {next(iter(saved["widths"])): view.ToDIP(chosen)}
        )
        applied = column_widths(view)
        assert all(applied[index] == chosen for index in targets)
        assert applied[5:] == [
            chosen if index in targets else value
            for index, value in enumerate(before[5:], 5)
        ]
        if not wide or col >= 5:
            assert applied[:5] == [
                chosen if index in targets else value
                for index, value in enumerate(before[:5])
            ]
        h.reopen(model=model, preferences=saved)
        # A wide fixed column may temporarily compress its fixed neighbors to
        # reserve scrolling space. Reopening must preserve the applied geometry.
        assert column_widths(h.view) == applied
        if wide and field in {"footprint", "value", "params"}:
            painted = paint_cell(h, 4, col).of("DrawText")[0]
            assert painted["args"][0] == model.get_display(4, col)
            assert (
                painted["size"] == h.view.GetDefaultCellFont().GetFractionalPointSize()
            )
        resize(h, col)
        assert column_widths(h.view) == before
        assert h.view.capture_preferences()["widths"] == {}

    matrix(check, model=model, size=(1600, 650))


def test_supplier_content_refresh_changes_only_its_variant_column_width(
    matrix: Any, modules: tuple[ModuleType, ModuleType]
) -> None:
    model = sample_model(modules[1])

    def check(h: Any) -> None:
        view = h.view
        col, other = model.column_for("A", "lcsc"), model.column_for("B", "lcsc")
        old, unchanged = view.GetColSize(col), view.GetColSize(other)
        refreshed = h.model_module.MatrixModel(
            changed(
                changed(model.snapshot, variants=("A",), lcsc="C12345678901234567890"),
                side="bottom",
            )
        )
        view.set_model(refreshed)
        assert view.GetColSize(col) > old and view.GetColSize(other) == unchanged
        assert "C12345678901234567890" in [
            item["args"][0] for item in paint_cell(h, 0, col).of("DrawText")
        ]
        side = paint_cell(h, 0, model.column_for(None, "side")).of("DrawText")[0]
        assert side["args"][0] == "B" and side["color"] == (77, 127, 196)
        view.set_model(model)
        assert (view.GetColSize(col), view.GetColSize(other)) == (old, unchanged)

    matrix(check, model=model)
