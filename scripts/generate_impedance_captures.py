"""Generate inspectable examples with real KiCad SVG plots and production framing.

Run with the repository's myenv Python. Pass --kicad-cli if it is not on PATH.
Default output includes PNGs via wx.svg; --svg-only avoids GUI initialization.
The resulting native plots exercise KiCad CLI rather than PLOT_CONTROLLER.
"""

import argparse
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Optional

# Keep the developer script executable directly from any current directory.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from impedance.palette import PaletteError, ThemeContext, read_theme_files  # noqa: E402
from impedance.render import _wx_rasterize, annotated_svg  # noqa: E402
from tests.impedance_capture_fixtures import (  # noqa: E402
    BOARD_BOUNDS,
    EXAMPLES,
    CaptureExample,
    board_text,
)


def capture_theme(cli: Path, theme: Optional[str] = None) -> ThemeContext:
    """Resolve the CLI's versioned PCB preferences, using KiCad's path policy.

    KICAD_CONFIG_HOME overrides the KiCad base directory (before the version).
    Export commands inherit the same environment, so native colors and the
    production canvas are read from the same selected palette.
    """
    result = subprocess.run(
        [str(cli), "version", "--format", "plain"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    version = re.match(r"^(\d+\.\d+)\.", result.stdout.strip())
    if result.returncode or version is None:
        raise PaletteError(
            f"Cannot determine KiCad's settings version: {result.stdout} {result.stderr}"
        )
    override = os.environ.get("KICAD_CONFIG_HOME")
    if override:
        base = Path(override)
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Preferences" / "kicad"
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise PaletteError(
                "APPDATA is unavailable; set KICAD_CONFIG_HOME explicitly"
            )
        base = Path(appdata) / "kicad"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = (Path(xdg) if xdg else Path.home() / ".config") / "kicad"
    return read_theme_files(base / version.group(1), theme=theme)


def export_board_layers(
    cli: Path,
    board: Path,
    directory: Path,
    layers: tuple[str, ...],
    theme: Optional[str] = None,
) -> dict[str, str]:
    """Retain native colored layer plots for the production context compositor."""
    directory.mkdir(parents=True, exist_ok=True)
    sources = {}
    for layer in layers:
        plot = directory / f"{layer.replace('.', '_')}.svg"
        command = [
            str(cli),
            "pcb",
            "export",
            "svg",
            "--output",
            str(plot),
            "--layers",
            layer,
            "--mode-single",
            "--page-size-mode",
            "1",
            "--exclude-drawing-sheet",
            "--scale",
            "1",
        ]
        if theme:
            command.extend(("--theme", theme))
        command.append(str(board))
        result = subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=60
        )
        if result.returncode or not plot.is_file():
            raise RuntimeError(
                f"KiCad SVG export failed: {result.stdout}\n{result.stderr}"
            )
        sources[layer] = plot.read_text(encoding="utf-8")
    return sources


def export_example_svg(
    cli: Path,
    example: CaptureExample,
    directory: Path,
    theme: Optional[str] = None,
    *,
    palette: Optional[ThemeContext] = None,
) -> Path:
    """Plot native context then apply the same composition policy as the plugin."""
    from impedance.render import compose_layer_svgs

    palette = palette or capture_theme(cli, theme)
    directory.mkdir(parents=True, exist_ok=True)
    board = directory / f"{example.name}.kicad_pcb"
    plot = directory / f"{example.name}-native.svg"
    board.write_text(board_text(example), encoding="utf-8")
    sources = export_board_layers(
        cli,
        board,
        directory / f"{example.name}-layers",
        ("F.Cu", "B.Cu", "F.SilkS", "Edge.Cuts"),
        palette.name,
    )
    plot.write_text(
        compose_layer_svgs(
            sources,
            "F.Cu",
            background_color=palette.background,
            dimming_factor=palette.dimming_factor,
        ),
        encoding="utf-8",
    )
    return plot


def rasterize_capture(svg: Path, png: Path) -> Path:
    """Retain a native-color baseline to prove the box does not repaint the PCB."""
    source = svg.read_text(encoding="utf-8")
    baseline_source, count = re.subn(
        r'<rect\b[^>]*\bdata-impedance-highlight="box"[^>]*/>', "", source
    )
    if count != 1:
        raise ValueError("Capture must contain exactly one selected-route box")
    baseline_svg = svg.with_suffix(".baseline.svg")
    baseline_svg.write_text(baseline_source, encoding="utf-8")
    baseline_png = png.with_suffix(".baseline.png")
    _wx_rasterize(baseline_svg, baseline_png, 800, 420)
    _wx_rasterize(svg, png, 800, 420)
    return baseline_png


def _write_gallery(directory: Path, metadata: list[dict[str, object]]) -> None:
    """Write a local inspection page next to its self-contained source artifacts."""
    cards = []
    for record in metadata:
        image = record["png"] or record["svg"]
        cards.append(
            f"<section><h2>{html.escape(str(record['title']))}</h2>"
            f'<img width="800" height="420" src="{html.escape(str(image))}" '
            f'alt="{html.escape(str(record["title"]))}">'
            f'<p><a href="{record["board"]}">PCB fixture</a> · '
            f'<a href="{record["native_svg"]}">Composed KiCad layer plots</a> · '
            f'<a href="{record["svg"]}">Annotated SVG</a></p></section>'
        )
    document = (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        "<title>Impedance capture examples</title>"
        "<style>body{font:16px system-ui;margin:2rem;background:#f3f4f6;color:#18212c}"
        "section{background:white;padding:1.2rem;margin:1rem 0;width:800px}"
        "img{border:1px solid #ccd1d8}h1,h2{font-weight:550}h2{font-size:20px}</style>"
        "<h1>Impedance capture examples</h1><p>Real KiCad CLI plots, production "
        "full-color whole-route framing and yellow boxes, production wx.svg PNG rasterizer. "
        "Synthetic PCB fixtures; not a PLOT_CONTROLLER or live-editor smoke test.</p>"
        + "".join(cards)
        + "</html>"
    )
    (directory / "index.html").write_text(document, encoding="utf-8")
    (directory / "manifest.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )


def generate(
    directory: Path, cli: Path, *, rasterize: bool = True, theme: Optional[str] = None
) -> Path:
    """Export all fixtures and retain the exact source boards, plots and images."""
    palette = capture_theme(cli, theme)
    directory.mkdir(parents=True, exist_ok=True)
    app = None
    if rasterize:
        import wx

        app = wx.GetApp() or wx.App(False)
    metadata = []
    for example in EXAMPLES:
        native = export_example_svg(
            cli, example, directory, palette.name, palette=palette
        )
        annotated = directory / f"{example.name}.svg"
        annotated.write_text(
            annotated_svg(
                native.read_text(encoding="utf-8"),
                example.section(),
                board_bounds=BOARD_BOUNDS,
                background_color=palette.background,
            ),
            encoding="utf-8",
        )
        png = directory / f"{example.name}.png"
        baseline = None
        if rasterize:
            baseline = rasterize_capture(annotated, png)
        metadata.append(
            {
                "name": example.name,
                "title": example.title,
                "length_mm_per_net": example.length_mm,
                "board": f"{example.name}.kicad_pcb",
                "native_svg": native.name,
                "svg": annotated.name,
                "png": png.name if rasterize else None,
                "baseline_png": baseline.name if baseline is not None else None,
                "theme": palette.name,
                "background_color": palette.background,
                "inactive_layer_mode": "hidden-copper-dimmed-context",
                "inactive_copper_visibility": "hidden",
                "context_layer_mode": "dimmed",
                "dimming_factor": palette.dimming_factor,
                "zone_opacity": 0.6,
                "theme_source": "explicit override"
                if theme
                else "configured KiCad PCB editor theme",
                "renderer": "KiCad CLI SVG + production annotated_svg + wx.svg"
                if rasterize
                else "KiCad CLI SVG + production annotated_svg",
                "board_bounds_nm": BOARD_BOUNDS,
                "section_bounds_nm": example.section().bounds,
            }
        )
    _write_gallery(directory, metadata)
    # Retain wx.App for the lifetime of every bitmap operation.
    del app
    return directory / "index.html"


def main(argv: Optional[list[str]] = None) -> int:
    """Generate the gallery without needing a system or KiCad Python interpreter."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--kicad-cli", type=Path, default=shutil.which("kicad-cli"))
    parser.add_argument("--svg-only", action="store_true")
    parser.add_argument(
        "--theme", help="Override the configured KiCad PCB editor theme"
    )
    args = parser.parse_args(argv)
    if args.kicad_cli is None:
        parser.error("kicad-cli is not on PATH; supply --kicad-cli /path/to/kicad-cli")
    result = generate(
        args.output.resolve(),
        args.kicad_cli.resolve(),
        rasterize=not args.svg_only,
        theme=args.theme,
    )
    sys.stdout.write(f"{result}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
