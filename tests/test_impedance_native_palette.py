"""Model KiCad's unloaded plot palettes separately from managed selected colors."""

import json
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any

import pytest

from impedance import render
from impedance.model import Section, Trace


@pytest.fixture
def selected_theme(monkeypatch: pytest.MonkeyPatch) -> str:
    """Isolate theme-ID selection from native palette initialization tests."""
    monkeypatch.setattr(
        render,
        "read_theme_context",
        lambda module: render.ThemeContext("selected-custom", "#123456"),
        raising=False,
    )
    return "selected-custom"


@pytest.mark.parametrize("source_kind", ["dummy", "different-custom"])
def test_selected_manager_palette_replaces_nonnull_plot_palette(
    source_kind: str, selected_theme: str
) -> None:
    """A plot-options pointer cannot identify the selected PCB Editor palette."""
    default, source, detached = object(), object(), object()
    requests = []

    def get_colors(name: str) -> object:
        """Return the manager-owned, already-loaded palette."""
        requests.append(name)
        return default

    module = SimpleNamespace(
        GetSettingsManager=lambda: SimpleNamespace(GetColorSettings=get_colors)
    )
    assert source_kind in {"dummy", "different-custom"}
    result = render._native_color_settings(
        module,
        SimpleNamespace(ColorSettings=lambda: detached),
        SimpleNamespace(ColorSettings=lambda: source),
    )
    assert result is default
    assert requests == [selected_theme]


@pytest.mark.parametrize("manager", [None, SimpleNamespace()])
def test_direct_loaded_selected_palette_is_supported(
    manager: Any, selected_theme: str
) -> None:
    """Use the public equivalent helper when manager lookup is unavailable."""
    default = object()
    requested = []

    def get_colors(name: str) -> object:
        """Return the loaded built-in theme without consulting plot defaults."""
        requested.append(name)
        return default

    module = SimpleNamespace(
        GetSettingsManager=lambda: manager, GetColorSettings=get_colors
    )
    assert render._native_color_settings(module, SimpleNamespace()) is default
    assert requested == [selected_theme]


@pytest.mark.parametrize("failure", ["missing", "null", "error"])
def test_unavailable_selected_palette_fails_instead_of_plotting_monochrome(
    failure: str,
    selected_theme: str,
) -> None:
    """A dummy palette cannot turn backend failure into a successful black image."""

    def get_colors(name: str) -> None:
        """Expose each real native lookup failure."""
        assert name == selected_theme
        if failure == "error":
            raise RuntimeError("native palette unavailable")

    module = (
        SimpleNamespace()
        if failure == "missing"
        else SimpleNamespace(GetColorSettings=get_colors)
    )
    with pytest.raises(render.RenderError, match="palette|color"):
        render._native_color_settings(
            module, SimpleNamespace(ColorSettings=lambda: object())
        )


@pytest.mark.parametrize("can_set_palette", [True, False])
def test_native_controller_receives_selected_palette_before_plot_is_opened(
    monkeypatch: pytest.MonkeyPatch, can_set_palette: bool, selected_theme: str
) -> None:
    """Exercise the real renderer constructor and state-dependent plotting order."""
    default, dummy, custom = object(), object(), object()
    original_options = SimpleNamespace(ColorSettings=lambda: custom)
    board = SimpleNamespace(GetPlotOptions=lambda: original_options)
    detached = object()
    controllers = []

    class Options:
        """Start with KiCad's non-null unloaded dummy palette."""

        def __init__(self) -> None:
            self.palette = dummy
            self.values: dict[str, Any] = {}

        def ColorSettings(self) -> object:
            """Return a pointer even before usable colors have been loaded."""
            return self.palette

        def __getattr__(self, name: str) -> Any:
            """Apply setter state so the later plot observes the actual configuration."""
            if name == "SetColorSettings" and not can_set_palette:
                raise AttributeError(name)
            if not name.startswith("Set"):
                raise AttributeError(name)

            def setter(value: Any) -> None:
                """Retain the native controller-local option value."""
                if name == "SetColorSettings":
                    self.palette = value
                self.values[name] = value

            return setter

    class Controller:
        """Resolve color when the plot opens, just as native render settings do."""

        def __init__(self, plot_board: Any) -> None:
            assert plot_board is detached
            self.options = Options()
            self.opened = False
            self.closed = False
            self.color_mode = False
            controllers.append(self)

        def GetPlotOptions(self) -> Options:
            return self.options

        def SetLayer(self, layer: int) -> None:
            assert layer == 0

        def OpenPlotfile(self, suffix: str, file_format: int, description: str) -> bool:
            assert suffix and description and file_format == 4
            self.opened = True
            self.color = "#c83434" if self.options.palette is default else "#000000"
            self.path = Path(self.options.values["SetOutputDirectory"]) / "layer.svg"
            return True

        def SetColorMode(self, enabled: bool) -> None:
            self.color_mode = enabled

        def PlotLayer(self) -> bool:
            assert self.color_mode
            assert not self.options.values["SetBlackAndWhite"]
            self.path.write_text(
                f'<svg><path stroke="{self.color}"/></svg>', encoding="utf-8"
            )
            return True

        def GetPlotFileName(self) -> str:
            return str(self.path)

        def ClosePlot(self) -> None:
            self.closed = True

    module = SimpleNamespace(
        PLOT_CONTROLLER=Controller,
        PLOT_FORMAT_SVG=4,
        PLOT_TEXT_MODE_STROKE=17,
        GetSettingsManager=lambda: SimpleNamespace(
            GetColorSettings=lambda name: default if name == selected_theme else None
        ),
    )
    monkeypatch.setattr(render, "copper_layers", lambda board, module: {"F.Cu": 0})
    monkeypatch.setattr(
        render,
        "copy_for_render",
        lambda board, module: SimpleNamespace(board=detached, close=lambda: None),
    )
    renderer = render.SectionRenderer(board, module)
    if can_set_palette:
        assert "#c83434" in renderer._plot_layer("F.Cu")
        assert controllers[0].closed
        assert not controllers[0].path.exists()
    else:
        with pytest.raises(render.RenderError, match="SetColorSettings|color"):
            renderer._plot_layer("F.Cu")
        assert not controllers[0].opened
    assert original_options.ColorSettings() is custom


@pytest.mark.parametrize(
    "theme", ["_builtin_default", "_builtin_classic", "user", "my-custom-theme"]
)
def test_reads_configured_editor_theme_without_writing_settings(
    tmp_path: Path, theme: str
) -> None:
    """Read PCB Editor appearance, not a separate saved SVG-export theme."""
    source = tmp_path / "pcbnew.json"
    content = json.dumps(
        {
            "appearance": {"color_theme": theme},
            "export_svg": {"color_theme": "wrong-theme"},
        }
    )
    source.write_text(content, encoding="utf-8")
    if not theme.startswith("_builtin_"):
        (tmp_path / "colors").mkdir()
        (tmp_path / "colors" / (theme + ".json")).write_text("{}", encoding="utf-8")
    module = SimpleNamespace(
        SETTINGS_MANAGER=SimpleNamespace(GetUserSettingsPath=lambda: str(tmp_path))
    )
    assert render._configured_color_theme(module) == theme
    assert source.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("content", [{}, {"appearance": {}}])
def test_missing_theme_key_uses_kicad_declared_default(
    tmp_path: Path, content: dict[str, Any]
) -> None:
    """A missing preference has KiCad's default; an absent whole file is not equivalent."""
    (tmp_path / "pcbnew.json").write_text(json.dumps(content), encoding="utf-8")
    module = SimpleNamespace(
        SETTINGS_MANAGER=SimpleNamespace(GetUserSettingsPath=lambda: str(tmp_path))
    )
    assert render._configured_color_theme(module) == "_builtin_default"


@pytest.mark.parametrize(
    "content",
    [
        None,
        "{",
        "[]",
        '{"appearance": null}',
        '{"appearance":{"color_theme":null}}',
        '{"appearance":{"color_theme":""}}',
    ],
)
def test_unreadable_editor_theme_is_actionable_not_silent_default(
    tmp_path: Path, content: Any
) -> None:
    """Do not misrepresent the user's choice when it cannot be read reliably."""
    if content is not None:
        (tmp_path / "pcbnew.json").write_text(content, encoding="utf-8")
    module = SimpleNamespace(
        SETTINGS_MANAGER=SimpleNamespace(GetUserSettingsPath=lambda: str(tmp_path))
    )
    with pytest.raises(render.RenderError, match="Preferences|settings"):
        render._configured_color_theme(module)


def test_fresh_previews_follow_theme_switches_and_live_palette_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise persisted theme IDs through real renderer construction and native lookup."""
    preferences = tmp_path / "pcbnew.json"
    colors = tmp_path / "colors"
    colors.mkdir()
    (colors / "light.json").write_text(
        '{"board":{"background":"#ffffff"}}', encoding="utf-8"
    )
    (colors / "dark.json").write_text(
        '{"board":{"background":"#001023"}}', encoding="utf-8"
    )
    palettes = {
        "light": SimpleNamespace(stroke="#101010"),
        "dark": SimpleNamespace(stroke="#c83434"),
    }
    requests = []
    released = []
    board, detached = object(), object()

    def get_palette(name: str) -> Any:
        """Return a cached native palette, not a new object derived from its file."""
        requests.append(name)
        return palettes[name]

    class Options:
        """Stateful independent controller options with a non-null dummy palette."""

        def __init__(self) -> None:
            self.SetColorSettings = self._set_palette
            self.palette = SimpleNamespace(stroke="#000000")
            self.values: dict[str, Any] = {}

        def _set_palette(self, value: Any) -> None:
            self.palette = value

        def __getattr__(self, name: str) -> Any:
            if not name.startswith("Set"):
                raise AttributeError(name)

            def setter(value: Any) -> None:
                self.values[name] = value

            return setter

    class Controller:
        """Consume the selected palette at open, then emit native-colored SVG."""

        def __init__(self, source: Any) -> None:
            assert source is detached
            self.options = Options()
            self.color = False

        def GetPlotOptions(self) -> Options:
            return self.options

        def SetLayer(self, layer: int) -> None:
            assert layer == 0

        def OpenPlotfile(self, suffix: str, file_format: int, description: str) -> bool:
            assert suffix and file_format == 4 and description
            self.stroke = self.options.palette.stroke
            self.path = Path(self.options.values["SetOutputDirectory"]) / "native.svg"
            return True

        def SetColorMode(self, enabled: bool) -> None:
            self.color = enabled

        def PlotLayer(self) -> bool:
            assert self.color and self.options.values["SetBlackAndWhite"] is False
            self.path.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" width="240mm" height="120mm" '
                f'viewBox="0 0 24000 12000"><path fill="none" stroke="{self.stroke}" '
                'stroke-width="20" d="M10000,4000 L11000,4000"/></svg>',
                encoding="utf-8",
            )
            return True

        def GetPlotFileName(self) -> str:
            return str(self.path)

        def ClosePlot(self) -> None:
            pass

    module = SimpleNamespace(
        SETTINGS_MANAGER=SimpleNamespace(GetUserSettingsPath=lambda: str(tmp_path)),
        GetSettingsManager=lambda: SimpleNamespace(GetColorSettings=get_palette),
        PLOT_CONTROLLER=Controller,
        PLOT_FORMAT_SVG=4,
        PLOT_TEXT_MODE_STROKE=17,
    )
    monkeypatch.setattr(render, "copper_layers", lambda board, module: {"F.Cu": 0})
    monkeypatch.setattr(
        render,
        "copy_for_render",
        lambda board, module: SimpleNamespace(
            board=detached, close=lambda: released.append(True)
        ),
    )
    trace = Trace(
        "trace",
        "F.Cu",
        "SIGNAL",
        200_000,
        ((100_000_000, 40_000_000), (110_000_000, 40_000_000)),
    )
    section = Section(
        "section",
        "spec",
        "F.Cu",
        200_000,
        (trace,),
        (99_900_000, 39_900_000, 110_100_000, 40_100_000),
        ("SIGNAL",),
    )

    def preview(theme: str, stroke: str, background: str) -> None:
        """Commit a selection as Preferences does, then run a complete new capture."""
        preferences.write_text(
            json.dumps({"appearance": {"color_theme": theme}}), encoding="utf-8"
        )
        before = preferences.read_bytes()
        with render.SectionRenderer(board, module) as renderer:
            output = renderer.svg(section)
        assert f'fill="{background}"' in output
        assert re.search(rf'(?:stroke="|stroke:){re.escape(stroke)}[;\"]', output)
        assert 'data-impedance-highlight="box"' in output
        assert preferences.read_bytes() == before

    preview("light", "#101010", "#ffffff")
    preview("dark", "#c83434", "#001023")
    # The selected native object, not reloaded JSON, supplies live foreground edits.
    palettes["dark"].stroke = "#12abcd"
    preview("dark", "#12abcd", "#001023")
    assert requests == ["light", "dark", "dark"]
    assert released == [True, True, True]
