"""Open real plugin dialogs for a combined example board or individual RF case.

This is a manual native-wx inspection harness, not a drawing of the dialogs.
Edits remain in memory and never write plugin settings or modify the PCB. A
native SVG context plot is optional; when supplied, previews use the production
annotation and PNG rasterizer with the selected section's actual geometry.
Cached context colors require their originating palette's --background-color.
"""

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from typing import Optional

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from impedance.model import (  # noqa: E402
    BoardSnapshot,
    Bounds,
    Config,
    Section,
    Specification,
)
from tests.rf_impedance_combined import COMBINED_BOARDS, CombinedBoard  # noqa: E402
from tests.rf_impedance_fixtures import (  # noqa: E402
    BOARD_BOUNDS,
    COPPER_LAYERS,
    RF_CASES,
    RFCase,
)


@dataclass(frozen=True)
class PreviewOptions:
    """Validated in-memory inspection settings, independent of any GUI runtime."""

    case: Optional[RFCase]
    specification: Optional[int]
    new: bool
    contexts: dict[str, Path]
    background_color: Optional[str]
    board: Optional[CombinedBoard] = None

    @property
    def board_bounds(self) -> Bounds:
        """Return the selected fixture's physical outline for context framing."""
        if self.board is not None:
            return self.board.board_bounds
        if self.case is not None:
            return self.case.board_bounds
        raise ValueError("Choose a combined board or an individual RF case")


def validated_background(value: object) -> str:
    """Require a literal opaque RGB color, never CSS expressions or a guessed theme."""
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError("--background-color must be an opaque #rrggbb color")
    return value.lower()


def parse_preview_args(argv: Optional[list[str]] = None) -> PreviewOptions:
    """Validate all command-line input without importing or constructing native UI."""
    parser = argparse.ArgumentParser(description=__doc__)
    fixture = parser.add_mutually_exclusive_group()
    fixture.add_argument(
        "--board",
        choices=[board.name for board in COMBINED_BOARDS],
        help="Combined example board (default: usb-differential-90-ohm)",
    )
    fixture.add_argument(
        "--case",
        choices=[case.name for case in RF_CASES],
        help="Open an individual RF case instead of a combined board",
    )
    parser.add_argument(
        "--specification",
        type=int,
        help="Edit this 1-based net-class specification",
    )
    parser.add_argument("--new", action="store_true", help="Open a new specification")
    parser.add_argument(
        "--context",
        action="append",
        default=[],
        metavar="LAYER=SVG",
        help="Repeat for each layer's native composed context SVG",
    )
    parser.add_argument(
        "--background-color",
        metavar="#RRGGBB",
        help="Originating palette background; required with cached --context plots",
    )
    args = parser.parse_args(argv)
    case = (
        next(case for case in RF_CASES if case.name == args.case)
        if args.case is not None
        else None
    )
    board = (
        next(
            board
            for board in COMBINED_BOARDS
            if board.name == (args.board or "usb-differential-90-ohm")
        )
        if case is None
        else None
    )
    if args.new and args.specification is not None:
        parser.error("Choose either --new or --specification")
    if board is not None:
        count = len(board.specifications())
    elif case is not None:
        count = len(case.specifications())
    else:
        parser.error("Choose a combined board or an individual RF case")
    if args.specification is not None and not 1 <= args.specification <= count:
        parser.error(
            "The specification number must identify an available specification"
        )
    if args.context and args.background_color is None:
        parser.error(
            "Cached --context plots require their originating --background-color"
        )
    if args.background_color is not None and not args.context:
        parser.error("--background-color requires a cached --context plot")
    background = None
    if args.background_color is not None:
        try:
            background = validated_background(args.background_color)
        except ValueError as error:
            parser.error(str(error))
    contexts = {}
    for assignment in args.context:
        layer, separator, filename = assignment.partition("=")
        if not separator or layer not in COPPER_LAYERS:
            parser.error(
                "--context must be a copper layer and SVG path, e.g. F.Cu=/path/plot.svg"
            )
        if layer in contexts:
            parser.error(f"Only one --context may be supplied for {layer}")
        source = Path(filename).resolve()
        if not source.is_file():
            parser.error(f"The context SVG does not exist: {source}")
        contexts[layer] = source
    return PreviewOptions(
        case, args.specification, args.new, contexts, background, board
    )


def preview_inputs(
    options: PreviewOptions,
) -> tuple[BoardSnapshot, tuple[Specification, ...]]:
    """Use the selected board or circuit's declared net-class intent."""
    if options.board is not None:
        return options.board.snapshot(), options.board.specifications()
    case = options.case
    if case is None:
        raise ValueError("Choose a combined board or an individual RF case")
    return case.snapshot(), case.specifications()


def cached_preview_svg(
    section: Section,
    contexts: dict[str, Path],
    background_color: Optional[str],
    board_bounds: Bounds = BOARD_BOUNDS,
) -> str:
    """Frame a cached native plot using its explicitly supplied original background."""
    from impedance.render import annotated_svg

    if section.layer not in contexts:
        raise ValueError(
            f"Supply --context {section.layer}=/path/to/native-context.svg "
            "and its --background-color to enable this layer's preview."
        )
    return annotated_svg(
        contexts[section.layer].read_text(encoding="utf-8"),
        section,
        board_bounds=board_bounds,
        background_color=validated_background(background_color),
    )


def main(argv: Optional[list[str]] = None) -> int:
    """Launch native controls in myenv without requiring a KiCad Python binding."""
    options = parse_preview_args(argv)

    import wx

    from impedance.dialog import ImpedanceDialog, SpecificationDialog
    from impedance.render import _wx_rasterize
    from impedance.service import CapturedImage

    app = wx.App(False)
    app.SetAppName("Impedance dialog preview")
    snapshot, specifications = preview_inputs(options)
    with TemporaryDirectory(prefix="jlcpcb-dialog-preview-") as directory:

        def preview(section: Section, *, refresh: bool = False) -> CapturedImage:
            """Render the same automatically paired row used by workbook export."""
            svg = Path(directory) / "preview.svg"
            png = Path(directory) / "preview.png"
            svg.write_text(
                cached_preview_svg(
                    section,
                    options.contexts,
                    options.background_color,
                    options.board_bounds,
                ),
                encoding="utf-8",
            )
            _wx_rasterize(svg, png, 800, 420)
            return CapturedImage.load(png)

        if options.new or options.specification is not None:
            original = (
                None if options.new else specifications[options.specification - 1]
            )
            dialog = SpecificationDialog(None, snapshot, original, preview=preview)
        else:
            dialog = ImpedanceDialog(
                None,
                Config(enabled=True, specifications=specifications),
                snapshot,
                preview,
                lambda: snapshot,
            )
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()
    del app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
