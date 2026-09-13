"""Native raster and DC evidence for comparison colors, artwork and shared borders."""

from dataclasses import replace
from types import ModuleType
from typing import Any, Optional
from unittest.mock import patch

import pytest

from .native_wx_support import pump, wait_until
from .variant_matrix_native_test_support import matrix, modules, native_marks
from .variant_matrix_render_test_support import (
    block_image,
    contrast,
    observe_cell_paints,
    paint_cell,
    pixel,
    repaired_boundary,
    resize,
    sample_model,
)
from .variant_model_test_support import assignment

__all__ = ["matrix", "modules"]
pytestmark = native_marks


def theme(h: Any, dark: bool) -> tuple[int, int, int]:
    """Choose explicit native colors so raster expectations do not depend on OS theme."""
    h.view.SetDefaultCellBackgroundColour(
        h.wx.Colour(*((0, 0, 0) if dark else (255, 255, 255)))
    )
    h.view.SetDefaultCellTextColour(
        h.wx.Colour(*((240, 240, 240) if dark else (0, 0, 0)))
    )
    h.view.SetGridLineColour(h.wx.Colour(80, 80, 80))
    return (255, 210, 110) if dark else (145, 85, 0)


@pytest.mark.parametrize("oversized", [False, True])
def test_drag_preview_source_keeps_visible_native_geometry_and_selection(
    matrix: Any, modules: tuple[ModuleType, ModuleType], oversized: bool
) -> None:
    """An opaque source retains native geometry; the preview surface applies opacity."""
    model = sample_model(modules[1], rows=30, variants=("", "A", "B", "C", "D"))

    def check(h: Any) -> None:
        view, wx = h.view, h.wx
        theme(h, True)
        first, last = model.column_for("A", "value"), model.column_for("A", "price")
        view.select_components(("component-2",), "A")
        view.SetScrollRate(1, 1)
        pump(wx)
        if oversized:
            resize(h, first, 100_000)
            pump(wx)
            assert view.GetColSize(first) == 100_000
        row_top, row_height = view.CellToRect(1, first).y, view.GetRowSize(1)
        view.Scroll(
            view.GetColLeft(first)
            + view.GetColSize(first) // 2
            - view.GetColLeft(view.GetNumberFrozenCols()),
            row_top + row_height // 2,
        )
        pump(wx)
        header, body = view.GetGridColLabelWindow(), view.GetGridWindow()
        origin = view._header_logical_x(0)
        offset = view.GetGridWindowOffset(body)
        _, top = view.CalcGridWindowUnscrolledPosition(*offset, body)
        left = max(view.GetColLeft(first), origin)
        right = min(view.GetColRight(last), origin + header.GetClientSize().width)
        assert view.GetColLeft(first) < left < right
        assert row_top < top < row_top + row_height
        point = wx.Point(left - origin + 8, view._group_header_height // 2)
        h.mouse(header, wx.wxEVT_LEFT_DOWN, point)
        drag = view._header_drag
        assert drag is not None and drag.variant == "A"
        renderer = h.view_module.MatrixCellRenderer
        original_draw, drawn = renderer.Draw, []

        def draw(
            instance: Any,
            grid: Any,
            attr: Any,
            dc: Any,
            rect: Any,
            row: int,
            col: int,
            selected: bool,
        ) -> None:
            drawn.append((row, col, tuple(rect), dc.GetUserScale(), selected))
            original_draw(instance, grid, attr, dc, rect, row, col, selected)

        with (
            patch.object(renderer, "Draw", draw),
            patch.object(
                view, "_draw_header_labels", wraps=view._draw_header_labels
            ) as labels,
        ):
            bitmap, hotspot = view._drag_preview_bitmap(drag)
        view._cancel_header_drag()
        height = view.GetColLabelSize() + min(
            body.GetClientSize().height,
            view.CellToRect(29, first).y + view.GetRowSize(29) - top,
        )
        assert bitmap.GetLogicalSize() == wx.Size(right - left, height)
        assert bitmap.GetScaleFactor() == view.GetContentScaleFactor()
        assert hotspot == wx.Point(point.x - (left - origin), point.y)
        image = bitmap.ConvertToImage()
        assert not image.HasAlpha() or set(image.GetAlpha()) == {255}
        assert labels.call_count == 1
        assert min(row for row, *_ in drawn) == 1
        assert max(row for row, *_ in drawn) < 29
        for row, col, rect, user_scale, selected in drawn:
            assert first <= col <= last and rect == tuple(view.CellToRect(row, col))
            assert user_scale == (1, 1) and selected is (row == 1)
        scale = bitmap.GetScaleFactor()
        colors = []
        for row in (1, 2):
            expected = paint_cell(h, row, first, width=40).background
            y = (
                view.GetColLabelSize()
                + view.CellToRect(row, first).y
                + view.GetRowSize(row)
                - top
                - 4
            )
            actual = pixel(image, int(4 * scale), int(y * scale))
            assert all(abs(a - b) <= 2 for a, b in zip(actual, expected))
            colors.append(actual)
        assert colors[0] != colors[1]
        if not oversized:
            col = model.column_for("A", "bom")
            flag = paint_cell(h, 2, col)
            x, y = max(
                (
                    (x, y)
                    for y in range(flag.image.GetHeight())
                    for x in range(flag.image.GetWidth())
                ),
                key=lambda p: pixel(flag.image, *p)[1]
                - max(pixel(flag.image, *p)[::2]),
            )
            expected = pixel(flag.image, x, y)
            assert expected[1] > max(expected[::2]) + 40
            rect = view.CellToRect(2, col)
            actual = pixel(
                image,
                int((rect.x - left + x - flag.rect.x + 0.5) * scale),
                int(
                    (view.GetColLabelSize() + rect.y - top + y - flag.rect.y + 0.5)
                    * scale
                ),
            )
            # Retina drawing interpolates icon-edge pixels differently from
            # the one-pixel reference raster; green ink must stay in place.
            assert actual[1] > max(actual[::2]) + 40
        assert view.selected_component_ids() == ("component-2",)

    matrix(check, model=model, size=(760, 500))


@pytest.mark.parametrize(
    "background,foreground,stripe,variant",
    [
        ((0, 0, 0), (240, 240, 240), (38, 38, 38), None),
        ((30, 30, 30), (240, 240, 240), (64, 64, 64), ""),
        ((255, 255, 255), (0, 0, 0), (230, 230, 230), "A"),
        ((0, 0, 0), (240, 240, 240), (38, 38, 38), "B"),
        ((255, 255, 255), (0, 0, 0), (230, 230, 230), None),
    ],
)
def test_stripes_and_selection_guide_the_whole_component_without_erasing_differences(
    matrix: Any,
    modules: tuple[ModuleType, ModuleType],
    background: tuple[int, ...],
    foreground: tuple[int, ...],
    stripe: tuple[int, ...],
    variant: Optional[str],
) -> None:
    model = sample_model(
        modules[1], changes={(row, "B"): {"pop": False} for row in range(3)}
    )

    def check(h: Any) -> None:
        view = h.view
        view.SetDefaultCellBackgroundColour(h.wx.Colour(*background))
        view.SetDefaultCellTextColour(h.wx.Colour(*foreground))
        before = {
            (row, col): paint_cell(h, row, col)
            for row in (0, 1)
            for col in range(view.GetNumberCols())
        }
        assert (
            before[0, 0].background == background and before[1, 0].background == stripe
        )
        for (row, col), result in before.items():
            if (model.columns[col].variant, model.columns[col].key) == ("B", "pop"):
                assert (
                    result.background[0] > result.background[1] > result.background[2]
                )
                assert max(
                    abs(a - b)
                    for a, b in zip(result.background, before[row, 0].background)
                ) >= (40 if sum(background) < 384 else 20)
            else:
                assert result.background == before[row, 0].background
        h.click(0, model.column_for(variant, "ref" if variant is None else "lcsc"))
        for (row, col), previous in before.items():
            if model.columns[col].variant is not None:
                assert view.is_variant_selected(row, col) is (
                    row == 0 and model.columns[col].variant == variant
                )
            current = paint_cell(h, row, col)
            assert (current.background != previous.background) is (row == 0)
            assert [(op["args"], op["color"]) for op in current.of("DrawText")] == [
                (op["args"], op["color"]) for op in previous.of("DrawText")
            ]
        h.click(1, 0)
        assert (
            paint_cell(h, 0, 0).background == background
            and paint_cell(h, 1, 0).background != stripe
        )
        view.restore_state(h.view_module.ViewState())
        assert paint_cell(h, 1, 0).background == stripe

    matrix(check, model=model)


@pytest.mark.parametrize(
    "status,dark",
    [("pending", True), ("missing", False), ("error", True), ("error", False)],
)
def test_unconfirmed_catalog_data_stays_neutral_except_the_error_cue(
    matrix: Any, modules: tuple[ModuleType, ModuleType], status: str, dark: bool
) -> None:
    model = sample_model(
        modules[1],
        catalog={
            (row, v): {
                "standard": True if v != "B" else None,
                "status": "complete" if v != "B" else status,
            }
            for row in range(3)
            for v in ("", "A", "B")
        },
    )

    def check(h: Any) -> None:
        accent = theme(h, dark)
        result = paint_cell(h, 0, model.column_for("B", "standard"))
        assert not any(op["pen"] == accent for op in result.of("DrawLine"))
        if status == "error":
            r, g, b = result.background
            assert r > g and abs(g - b) <= 3
        else:
            assert result.background == paint_cell(h, 0, 0).background
        assert [
            item["args"][0]
            for item in paint_cell(h, 0, model.column_for("B", "type")).of("DrawText")
        ] == ["-"]

    matrix(check, model=model)


@pytest.mark.parametrize(
    "field,dark,selected",
    [
        ("bom", False, False),
        ("pos", False, True),
        ("pop", True, False),
        ("pop", True, True),
    ],
)
def test_red_exclusion_cross_contrasts_with_actual_amber_and_selection_pixels(
    matrix: Any,
    modules: tuple[ModuleType, ModuleType],
    field: str,
    dark: bool,
    selected: bool,
) -> None:
    model = sample_model(modules[1], changes={(0, "B"): {field: False}})

    def check(h: Any) -> None:
        theme(h, dark)
        col = model.column_for("B", field)
        if selected:
            h.click(0, col)
        painted = paint_cell(h, 0, col)
        strokes = [
            op
            for op in painted.of("DrawLine")
            if op["args"][0] != op["args"][2] and op["args"][1] != op["args"][3]
        ]
        assert (
            len(strokes) == 2
            and not painted.of("DrawBitmap")
            and not painted.of("DrawText")
        )
        colors = {
            pixel(painted.image, x, y)
            for x in range(painted.image.GetWidth())
            for y in range(painted.image.GetHeight())
        }
        for stroke in strokes:
            r, g, b = stroke["pen"]
            assert (
                stroke["weight"] >= 2
                and r > g
                and r > b
                and contrast(stroke["pen"], painted.background) >= 3
            )
            assert stroke["pen"] in colors
        assert len(painted.of("DrawPolygon")) == 1
        assert len(paint_cell(h, 0, model.column_for("A", field)).of("DrawBitmap")) == 1

    matrix(check, model=model)


@pytest.mark.parametrize(
    "mask,dark,lines",
    [(mask, True, True) for mask in [(), (0,), (1,), (0, 1), (2,), (0, 2)]]
    + [((0, 1), False, True), ((0, 1), True, False)],
)
def test_native_outlines_share_one_pixel_for_every_border_mask(
    matrix: Any,
    modules: tuple[ModuleType, ModuleType],
    mask: tuple[int, ...],
    dark: bool,
    lines: bool,
) -> None:
    model = sample_model(
        modules[1], changes={(row, "A"): {"pop": False} for row in mask}
    )

    def check(h: Any) -> None:
        view, wx = h.view, h.wx
        accent = theme(h, dark)
        view.EnableGridLines(lines)
        for row, extra in enumerate((0, 7, 3)):
            view.SetRowSize(row, view.GetDefaultRowSize() + extra)
        first, last = model.column_for("A", "value"), model.column_for("A", "price")
        image = block_image(h, first, last)
        centers = [
            sum(view.GetColSize(col) for col in range(first, index))
            + view.GetColSize(index) // 2
            for index in (first, first + 1, last)
        ]
        bottoms = [
            sum(view.GetRowSize(index) for index in range(row + 1)) for row in range(3)
        ]
        for row, bottom in enumerate(bottoms):
            midpoint = bottom - view.GetRowSize(row) // 2
            for edge in (
                range(1, 6),
                range(image.GetWidth() - 6, image.GetWidth() - 1),
            ):
                assert any(pixel(image, x, midpoint) == accent for x in edge) is (
                    row in mask
                )
            for x in centers:
                assert (pixel(image, x, bottom - 1) == accent) is (
                    row in mask or row + 1 in mask
                )
                for y in (bottom - 2, bottom):
                    if y < image.GetHeight():
                        assert pixel(image, x, y) != accent, (
                            "A join occupies exactly one physical pixel"
                        )
        assert (pixel(image, centers[0], 0) == accent) is (0 in mask)
        if lines or 0 in mask or 1 in mask:
            for owner in (0, 1):
                repaired = repaired_boundary(
                    h, image, first, last, owner, bottoms[0] - 1
                )
                assert [pixel(repaired, x, bottoms[0] - 1) for x in centers] == [
                    pixel(image, x, bottoms[0] - 1) for x in centers
                ]
        # The middle of a clipped variant has no invented vertical block edge.
        middle = paint_cell(h, 0, model.column_for("A", "params"), width=20)
        assert not any(
            op["pen"] == accent and op["args"][0] == op["args"][2]
            for op in middle.of("DrawLine")
        )
        with observe_cell_paints(view, h.view_module.MatrixCellRenderer) as observed:
            for row, col in ((0, first), (1, last)):
                view.MakeCellVisible(row, col)
                pump(wx)
                window = view.GetGridWindow()
                rect = view.CellToRect(row, col)
                bottom = rect.y + view.GetRowSize(row) - 1
                x, y = view.CalcGridWindowScrolledPosition(rect.x, bottom, window)
                offset = view.GetGridWindowOffset(window)
                observed.clear()
                window.RefreshRect(
                    wx.Rect(x - offset.x, y - offset.y, view.GetColSize(col) - 1, 1),
                    eraseBackground=False,
                )
                window.Update()
                wait_until(
                    wx,
                    lambda target=(row, col): any(
                        item[:2] == target for item in observed
                    ),
                )
                clips = [item[2] for item in observed if item[:2] == (row, col)]
                assert any(
                    not clip[0] or clip[2] <= bottom < clip[2] + clip[4]
                    for clip in clips
                )
                assert view.GetRowGridLinePen(row).GetStyle() == wx.PENSTYLE_TRANSPARENT
        assert bytes(block_image(h, first, last).GetData()) == bytes(image.GetData())
        matching = block_image(
            h, model.column_for("B", "value"), model.column_for("B", "price")
        )
        assert not any(
            pixel(matching, x, bottoms[0] - 1) == accent
            for x in range(matching.GetWidth())
        )
        for col in (0, model.column_for("", "value"), model.column_for("B", "value")):
            ordinary = paint_cell(h, 0, col)
            assert (
                pixel(
                    ordinary.image,
                    ordinary.rect.x + ordinary.rect.width // 2,
                    ordinary.rect.y + view.GetRowSize(0) - 1,
                )
                == (80, 80, 80)
            ) is lines

    matrix(check, model=model, size=(650, 400))


@pytest.mark.parametrize(
    "native_selection", [False, True], ids=["reference-focus", "variant-rows"]
)
@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("different_rows", [(1,), (0, 1)], ids=["single", "adjacent"])
def test_difference_outline_overlays_selection_at_the_existing_shared_boundary(
    matrix: Any,
    modules: tuple[ModuleType, ModuleType],
    native_selection: bool,
    dark: bool,
    different_rows: tuple[int, ...],
) -> None:
    model = sample_model(
        modules[1], changes={(row, "A"): {"pop": False} for row in different_rows}
    )

    def check(h: Any) -> None:
        view, wx = h.view, h.wx
        accent = theme(h, dark)
        first, last = model.column_for("A", "value"), model.column_for("A", "price")
        if native_selection:
            h.click(0, first)
            h.click(1, first, shift=True)
        else:
            h.click(1, 0)
        assert all(
            view.is_variant_selected(row, col) is native_selection
            for row in (0, 1)
            for col in range(first, last + 1)
        )
        image = block_image(h, first, last)
        centers = [
            sum(view.GetColSize(col) for col in range(first, index))
            + view.GetColSize(index) // 2
            for index in (first, first + 1, last)
        ]
        boundary = view.GetRowSize(0) - 1
        rail = tuple(wx.SystemSettings.GetColour(wx.SYS_COLOUR_HIGHLIGHT))[:3]
        assert {
            op["weight"]
            for op in paint_cell(h, 1, first).of("DrawLine")
            if op["pen"] == rail
        } == {view.FromDIP(2 if native_selection else 1)}
        for row in different_rows:
            # Grid.Render can suppress native selection for its print raster.
            # Draw cells with their actual selection to check the overlapping ink.
            for col in (first, first + 1, last):
                painted = paint_cell(h, row, col)
                x = painted.rect.x + painted.rect.width // 2
                for y in (
                    painted.rect.y - int(row > 0),
                    painted.rect.y + view.GetRowSize(row) - 1,
                ):
                    assert pixel(painted.image, x, y) == accent, (
                        "Selection must not cover the existing amber boundary"
                    )
                    for adjacent in (y - 1, y + 1):
                        assert pixel(painted.image, x, adjacent) != accent, (
                            "The selected amber boundary is one pixel wide without an inset"
                        )
            top = sum(view.GetRowSize(index) for index in range(row)) - int(row > 0)
            bottom = sum(view.GetRowSize(index) for index in range(row + 1)) - 1
            for x in centers:
                for y in (top, bottom):
                    assert pixel(image, x, y) == accent, (
                        "Selection must not cover the existing amber boundary"
                    )
                    for adjacent in (y - 1, y + 1):
                        if 0 <= adjacent < image.GetHeight():
                            assert pixel(image, x, adjacent) != accent, (
                                "The amber boundary stays one pixel wide without an inset gap"
                            )
            inset = max(1, view.FromDIP(2))
            for x in (inset, image.GetWidth() - 2 - inset):
                assert pixel(image, x, (top + bottom) // 2) == accent
        for owner in (0, 1):
            repaired = repaired_boundary(h, image, first, last, owner, boundary)
            assert all(pixel(repaired, x, boundary) == accent for x in centers), (
                "Either selected neighbor must repaint the same amber boundary"
            )
        for variant in (None, "", "B"):
            col = model.column_for(variant, "ref" if variant is None else "value")
            painted = paint_cell(h, 1, col)
            x = painted.rect.x + painted.rect.width // 2
            assert any(op["pen"] == rail for op in painted.of("DrawLine"))
            assert pixel(painted.image, x, painted.rect.y - 1) not in (
                accent,
                painted.background,
            ), "The selection boundary remains visible outside differing blocks"

    matrix(check, model=model)


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_selected_edit_and_revert_keep_neighbor_difference_and_selection(
    matrix: Any, modules: tuple[ModuleType, ModuleType], dark: bool
) -> None:
    model = sample_model(modules[1], changes={(0, "A"): {"pop": False}})

    def check(h: Any) -> None:
        view, wx = h.view, h.wx
        accent = theme(h, dark)
        first, last = model.column_for("A", "value"), model.column_for("A", "price")
        pop = model.column_for("A", "pop")

        def edit(target: Any, value: bool) -> None:
            changed = replace(
                view.model.snapshot,
                components=tuple(
                    replace(state, pop=value)
                    if (state.component_id, state.variant_name)
                    == (target.component_id, target.variant)
                    else state
                    for state in view.model.snapshot.components
                ),
            )
            view.set_model(h.model_module.MatrixModel(changed))

        view._on_edit = edit
        h.click(1, first)
        selected_background = paint_cell(h, 1, pop).background
        before = paint_cell(h, 1, first)
        selection_boundary = pixel(
            before.image,
            before.rect.x + before.rect.width // 2,
            before.rect.y + view.GetRowSize(1) - 1,
        )
        matching_col = model.column_for("B", "value")
        matching_before = bytes(paint_cell(h, 1, matching_col).image.GetData())
        for value in (False, True):
            h.click(1, pop)
            pump(wx)
            assert view.model.get_value(1, pop) is value
            assert view.model.get_value(0, pop) is False
            assert all(
                view.is_variant_selected(1, col) for col in range(first, last + 1)
            )
            assert view.model.cell_style(1, pop).variant_different is (not value)
            painted = paint_cell(h, 1, first)
            x = painted.rect.x + painted.rect.width // 2
            top = painted.rect.y - 1
            bottom = painted.rect.y + view.GetRowSize(1) - 1
            assert pixel(painted.image, x, top) == accent, (
                "The unchanged neighbor keeps its amber shared boundary"
            )
            assert pixel(painted.image, x, bottom) == (
                selection_boundary if value else accent
            ), "Editing shows amber over selection; reverting restores blue in place"
            assert (paint_cell(h, 1, pop).background == selected_background) is value
            inset = max(1, view.FromDIP(2))
            for col in (first, last):
                painted = paint_cell(h, 1, col)
                edge = (
                    painted.rect.x + inset
                    if col == first
                    else painted.rect.GetRight() - inset
                )
                assert (pixel(painted.image, edge, (top + bottom) // 2) == accent) is (
                    not value
                )
            assert (
                bytes(paint_cell(h, 1, matching_col).image.GetData()) == matching_before
            )

    matrix(check, model=model)


@pytest.mark.parametrize(
    "dark,scale,selected",
    [(False, 1, False), (False, 2, True), (True, 1, True), (True, 2, False)],
)
def test_inheritance_artwork_scales_reserves_space_and_stays_clipped(
    matrix: Any,
    modules: tuple[ModuleType, ModuleType],
    dark: bool,
    scale: int,
    selected: bool,
) -> None:
    model = sample_model(
        modules[1], changes={(0, "A"): {"assignment": assignment(inherited=True)}}
    )

    def check(h: Any) -> None:
        theme(h, dark)
        view = h.view
        view.SetDefaultCellFont(view.GetDefaultCellFont().Scaled(scale))
        pump(h.wx)
        if selected:
            h.click(0, model.column_for("A", "lcsc"))
        painted = [
            paint_cell(h, 0, model.column_for(v, "lcsc"), width=250 * scale)
            for v in ("", "A", "B")
        ]
        positions = [result.of("DrawText")[0]["args"][1] for result in painted]
        assert len(set(positions)) == 1 and [
            len(result.of("DrawPolygon")) for result in painted
        ] == [0, 1, 0]
        polygon = painted[1].of("DrawPolygon")[0]
        points = polygon["args"][0]
        top = min(point.y for point in points)
        top_x = [point.x for point in points if point.y == top]
        assert max(top_x) - min(top_x) >= 2 * scale
        assert (
            positions[1] - max(point.x for point in points)
            >= h.view.GetTextExtent("M").width / 2
        )
        assert max(polygon["fill"]) - min(polygon["fill"]) <= 3
        assert min(polygon["fill"]) >= 190 if dark else max(polygon["fill"]) <= 90
        view.ClearSelection()
        view._publish_target(None)
        for width in (6, 20, 60):
            result = paint_cell(h, 0, model.column_for("A", "lcsc"), width=width)
            for y in range(result.image.GetHeight()):
                for x in range(result.image.GetWidth()):
                    separator = (
                        y == result.rect.y + view.GetRowSize(0) - 1
                        and result.rect.x <= x <= result.rect.GetRight() + 1
                    )
                    if not result.rect.Contains(x, y) and not separator:
                        assert pixel(result.image, x, y) == (255, 0, 255)
        view.set_model(sample_model(h.model_module))
        assert not paint_cell(h, 0, model.column_for("A", "lcsc")).of("DrawPolygon")
        assert (
            paint_cell(h, 0, model.column_for("A", "lcsc")).of("DrawText")[0]["args"][1]
            == positions[1]
        )

    matrix(check, model=model)


@pytest.mark.parametrize(
    "status",
    ["missing", "invalid"],
)
def test_unresolved_inherited_assignment_keeps_badge_without_confirmed_outline(
    matrix: Any, modules: tuple[ModuleType, ModuleType], status: str
) -> None:
    model = sample_model(
        modules[1],
        changes={
            (0, variant): {
                "lcsc": "",
                "assignment": assignment(status=status, inherited=True),
            }
            for variant in (("", "A", "B") if status == "missing" else ("A",))
        },
    )

    def check(h: Any) -> None:
        col = model.column_for("A", "lcsc")
        result = paint_cell(h, 0, col)
        assert len(result.of("DrawPolygon")) == 1
        if status in {"empty", "missing"}:
            assert not any(item["args"][0] for item in result.of("DrawText"))
        else:
            assert [item["args"][0] for item in result.of("DrawText")] == ["", "?"]
        accent = theme(h, True)
        for index in range(h.view.GetNumberCols()):
            assert not any(
                op["pen"] == accent for op in paint_cell(h, 0, index).of("DrawLine")
            )

    matrix(check, model=model)


@pytest.mark.parametrize(
    "field,values,expected",
    [
        ("type", ("Basic", "Extended", "Preferred", ""), ("B", "E", "P", "-")),
        ("standard", (False, True, None), ("—", "✓", "—")),
        ("stock", (21, 20, None, None), (True, False, False, False)),
        ("price", ("0", "1.01", "2", "101", None), ("↓", "↑", "↑", "↑", "?")),
        ("price", ("1", "1.01", "2", "101", None), ("↓", "↑", "↑", "↑", "?")),
        ("price", ("1", "1", "1", "1", None), ("-", "-", "-", "-", "?")),
    ],
)
@pytest.mark.parametrize("dark", [False, True])
def test_informational_artwork_is_compact_readable_and_preserves_selection(
    matrix: Any,
    modules: tuple[ModuleType, ModuleType],
    field: str,
    values: tuple[Any, ...],
    expected: tuple[Any, ...],
    dark: bool,
) -> None:
    """Real status glyphs/artwork remain distinct without extra text or lost row rails."""
    variants = ("", "A", "B", "C", "D")[: len(values)]
    model = sample_model(
        modules[1],
        rows=2,
        variants=variants,
        catalog={
            (row, variant): {
                field: value,
                "status": "error" if field == "stock" and index == 3 else "complete",
            }
            for row in range(2)
            for index, (variant, value) in enumerate(zip(variants, values))
        },
    )

    def check(h: Any) -> None:
        theme(h, dark)
        h.click(0, model.column_for("", field))
        colors = []
        rail = tuple(h.wx.SystemSettings.GetColour(h.wx.SYS_COLOUR_HIGHLIGHT))[:3]
        for variant, glyph in zip(variants, expected):
            col = model.column_for(variant, field)
            assert h.view.IsInSelection(0, col)
            assert h.view.is_variant_selected(0, col) is (variant == "")
            painted = paint_cell(h, 0, col)
            rails = [op for op in painted.of("DrawLine") if op["pen"] == rail]
            assert len(rails) == 2
            assert {op["weight"] for op in rails} == {
                h.view.FromDIP(2 if variant == "" else 1)
            }
            if field == "stock":
                assert not painted.of("DrawText")
                pixels = {
                    pixel(painted.image, x, y)
                    for x in range(painted.rect.x, painted.rect.GetRight())
                    for y in range(painted.rect.y, painted.rect.GetBottom())
                }
                assert bool(painted.of("DrawBitmap")) is glyph
                if glyph:
                    assert any(g > r + 20 and g > b for r, g, b in pixels)
                else:
                    assert any(r > 200 and g > 150 and b < 80 for r, g, b in pixels)
                    assert any(r < 80 and g < 80 and b < 80 for r, g, b in pixels)
                    assert len(painted.of("DrawCircle")) == 1
            else:
                text = painted.of("DrawText")
                assert [op["args"][0] for op in text] == [glyph]
                colors.append(text[0]["color"])
                if field == "price":
                    assert not painted.of("DrawBitmap") and not painted.of(
                        "DrawPolygon"
                    )
                    neutral = paint_cell(h, 0, model.column_for(variant, "value"))
                    assert painted.background == neutral.background
        if field == "price":
            assert len(set(colors[-1])) == 1
            if expected[0] == "-":
                assert all(len(set(color)) == 1 for color in colors)
            else:
                assert colors[0][1] > colors[0][0]
                reds = [r - g for r, g, _ in colors[1:-1]]
                assert 0 < reds[0] < reds[1] < reds[2]

    matrix(check, model=model)
