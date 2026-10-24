"""Deterministic physical panels and readable summaries for spacing evidence."""

from dataclasses import dataclass
import math
from typing import TYPE_CHECKING, Optional

from .model import ValidationError
from .spacing_checks import NmBounds, SpacingCrossSection

if TYPE_CHECKING:
    from .spacing_review import SpacingReviewEntry

PixelRect = tuple[int, int, int, int]
ViewportNm = tuple[float, float, float, float]
DETAIL_RECT: PixelRect = (16, 70, 1120, 600)
OVERVIEW_RECT: PixelRect = (1160, 70, 424, 280)
METRICS_RECT: PixelRect = (1160, 374, 424, 296)
REGION_RECT: PixelRect = (16, 700, 1568, 76)
SCHEMATIC_RECT: PixelRect = (16, 800, 1568, 184)
WIDE_DETAIL_RECT: PixelRect = (16, 70, 1568, 460)
MINIMUM_FIT_SCALE = 0.35
MINIMUM_LABEL_PIXELS = 42


@dataclass(frozen=True)
class SpacingComposition:
    """One immutable composition; approval must inspect every required region.

    Rectangles use composite pixel coordinates. ``minimum_scale`` is displayed
    pixels per captured pixel: labels of at least 42px remain at least 14.7px.
    The detail transform is ``entry.viewport_nm`` mapped into ``detail_rect``.
    """

    detail_rect: PixelRect
    overview_rect: PixelRect
    metrics_rect: PixelRect
    schematic_rect: PixelRect
    overview_viewport_nm: ViewportNm
    measurement: Optional[SpacingCrossSection] = None
    actual_gap_nm: Optional[float] = None
    region_gap_nm: Optional[NmBounds] = None
    minimum_scale: float = MINIMUM_FIT_SCALE
    target_spacing_nm: Optional[int] = None
    region_rect: PixelRect = REGION_RECT
    layout_name: str = "standard"
    metric_columns: bool = False
    overview_label_top: int = 8

    @property
    def required_regions(self) -> tuple[PixelRect, ...]:
        """Require all evidence, including an unavailable percentage readout."""
        return (
            self.detail_rect,
            self.overview_rect,
            self.metrics_rect,
            self.region_rect,
            self.schematic_rect,
        )

    @property
    def fingerprint_payload(self) -> tuple[object, ...]:
        """Bind every layout choice and displayed value to saved approval inputs."""
        section = self.measurement
        witness = (
            tuple(
                sorted(
                    (
                        (
                            tuple(round(value, 3) for value in section.source_point),
                            section.source_width_nm,
                        ),
                        (
                            tuple(round(value, 3) for value in section.mate_point),
                            section.mate_width_nm,
                        ),
                    )
                )
            )
            if section is not None
            else None
        )
        gap = self.region_gap_nm
        return (
            self.required_regions,
            self.overview_viewport_nm,
            self.minimum_scale,
            MINIMUM_LABEL_PIXELS,
            self.layout_name,
            self.metric_columns,
            self.overview_label_top,
            witness,
            round(self.actual_gap_nm, 3) if self.actual_gap_nm is not None else None,
            (gap.lower_nm, gap.upper_nm) if gap is not None else None,
            self.target_spacing_nm,
        )


def _overview(entry: "SpacingReviewEntry", rectangle: PixelRect) -> ViewportNm:
    """Fit the complete concern and the exact detail rectangle without stretching."""
    left, top, width, height = entry.viewport_nm
    bounds = entry.bounds
    right, bottom = max(bounds[2], left + width), max(bounds[3], top + height)
    left, top = min(bounds[0], left), min(bounds[1], top)
    width, height = right - left, bottom - top
    padding = max(width, height) * 0.08
    width, height = width + 2 * padding, height + 2 * padding
    ratio = rectangle[2] / rectangle[3]
    if width / height < ratio:
        width = height * ratio
    else:
        height = width / ratio
    return (left + right - width) / 2, (top + bottom - height) / 2, width, height


def spacing_composition(entry: "SpacingReviewEntry") -> SpacingComposition:
    """Show the selected worst native witness, never the midpoint of a bound."""
    from .spacing_overlays import spacing_guide_paths

    if entry.composition_layout == "standard":
        detail, overview, metrics, region = (
            DETAIL_RECT,
            OVERVIEW_RECT,
            METRICS_RECT,
            REGION_RECT,
        )
    elif entry.composition_layout == "wide":
        detail, overview, metrics, region = (
            WIDE_DETAIL_RECT,
            (16, 580, 424, 130),
            (464, 550, 1120, 160),
            (16, 730, 1568, 60),
        )
    else:
        raise ValidationError("The spacing image has an unknown composition layout.")
    left, top, width, height = entry.viewport_nm
    if (
        width <= 0
        or height <= 0
        or not math.isclose(width / height, detail[2] / detail[3], rel_tol=1e-9)
    ):
        raise ValidationError("The spacing image viewport does not match its layout.")

    def fully_visible(section: SpacingCrossSection) -> bool:
        """Keep both complete copper widths inside the physical detail."""
        return all(
            left + copper_width / 2 <= point[0] <= left + width - copper_width / 2
            and top + copper_width / 2 <= point[1] <= top + height - copper_width / 2
            for point, copper_width in (
                (section.source_point, section.source_width_nm),
                (section.mate_point, section.mate_width_nm),
            )
        )

    section = entry.representative
    if section is not None:
        if not fully_visible(section):
            raise ValidationError(
                "The selected spacing measurement does not fit its physical detail."
            )
    else:
        # Retain support for regional-only records and diagnostic callers that
        # have no explicit witness. Newly derived concerns always supply their
        # authoritative worst cross-section and must never silently substitute
        # a more convenient, less severe point near the viewport centre.
        center = left + width / 2, top + height / 2
        sections = tuple(
            guide.section
            for guide in spacing_guide_paths(entry)
            if guide.kind == "measured-gap"
            and guide.section is not None
            and fully_visible(guide.section)
        )
        section = min(
            sections,
            key=lambda value: (
                math.dist(
                    tuple(
                        (a + b) / 2
                        for a, b in zip(value.source_point, value.mate_point)
                    ),
                    center,
                ),
                tuple(sorted((value.source_point, value.mate_point))),
                tuple(sorted((value.source_trace_id, value.mate_trace_id))),
            ),
            default=None,
        )
    actual = (
        math.dist(section.source_point, section.mate_point)
        - (section.source_width_nm + section.mate_width_nm) / 2
        if section is not None
        else None
    )
    return SpacingComposition(
        detail,
        overview,
        metrics,
        SCHEMATIC_RECT,
        _overview(entry, overview),
        section,
        actual,
        entry.region_gap_nm,
        target_spacing_nm=entry.target_spacing_nm,
        region_rect=region,
        layout_name=entry.composition_layout,
        metric_columns=entry.composition_layout == "wide",
        overview_label_top=534 if entry.composition_layout == "wide" else 8,
    )


def spacing_label(
    label: str,
    x: float,
    y: float,
    *,
    size: float = MINIMUM_LABEL_PIXELS,
    color: str = "#FFFFFF",
) -> str:
    """Describe a label rendered by native antialiased fonts in the final PNG.

    SVG keeps normal text for browser inspection. The native compositor removes
    these descriptors before NanoSVG rasterization and paints them using wx.Font.
    """
    from xml.etree import ElementTree as ET

    node = ET.Element(
        "text",
        {
            "data-spacing-label": label,
            "data-spacing-text-top": str(y),
            "x": str(x),
            "y": str(y + size),
            "fill": color,
            "font-family": "sans-serif",
            "font-size": str(size),
            "font-weight": "bold" if size >= 48 else "normal",
        },
    )
    node.text = label
    return ET.tostring(node, encoding="unicode")
