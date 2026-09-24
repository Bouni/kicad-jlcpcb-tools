"""Verify physical-scale strokes through the production native SVG rasterizer."""

from pathlib import Path
from typing import Any

import pytest

from .native_wx_support import run_native
from .test_copper_zone_preview import SVG, preview  # noqa: F401 -- pytest fixture.

pytestmark = pytest.mark.native_wx


@pytest.mark.parametrize("zoom,label", [(False, "20 mm"), (True, "1 mm")])
def test_physical_scale_label_survives_native_rasterization(
    preview: Any,  # noqa: F811 -- pytest fixture.
    tmp_path: Path,
    zoom: bool,
    label: str,
) -> None:
    """Every scale-label glyph must produce visible pixels below the geometry."""
    records = __import__(preview.__package__ + ".copper_zones", fromlist=["*"])
    selected = records.EmptyCopperZone(
        "small-teardrop",
        "VEXT",
        0,
        "F.Cu",
        (((12_000_000, 8_000_000), (12_995_000, 8_000_000), (12_000_000, 8_300_000)),),
    )
    svg = tmp_path / "scale.svg"
    png = tmp_path / "scale.png"
    svg.write_text(
        preview.zone_preview_svg(
            SVG,
            selected,
            board_bounds=(0, 0, 100_000_000, 60_000_000),
            zoom=zoom,
        ),
        encoding="utf-8",
    )

    def exercise(_frame: Any, wx: Any) -> None:
        preview._wx_rasterize(svg, png, 1000, 600)
        image = wx.Image(str(png), wx.BITMAP_TYPE_PNG)
        assert image.IsOk()
        assert (image.GetWidth(), image.GetHeight()) == (1000, 600)
        for index, character in enumerate(label):
            if character == " ":
                continue
            left = 16 + index * 10
            assert any(
                min(image.GetRed(x, y), image.GetGreen(x, y), image.GetBlue(x, y)) > 200
                for x in range(left, left + 9)
                for y in range(574, 589)
            ), f"Scale glyph {index} ({character}) did not rasterize"

    run_native(exercise)
