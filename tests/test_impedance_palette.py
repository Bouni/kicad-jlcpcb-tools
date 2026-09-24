"""Resolve the actual selected theme and its background without changing KiCad."""

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from impedance.palette import (
    PaletteError,
    ThemeContext,
    read_stackup_copper_colors,
    read_theme_context,
    read_theme_files,
)


def _write(path: Path, value: Any) -> None:
    """Create a JSON fixture inside pytest's temporary directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _select(directory: Path, name: str) -> None:
    """Model Preferences OK persisting a different active theme."""
    _write(directory / "pcbnew.json", {"appearance": {"color_theme": name}})


def _assert_theme(actual: ThemeContext, name: str, background: str) -> None:
    """Check visible palette values plus detached custom-palette identity."""
    assert (actual.name, actual.background, actual.dimming_factor) == (
        name,
        background,
        0.8,
    )
    if name.startswith("_builtin_"):
        assert actual.palette_digest == ""
    else:
        assert len(actual.palette_digest) == 64
        assert all(
            character in "0123456789abcdef" for character in actual.palette_digest
        )


@pytest.mark.parametrize(
    ("name", "background"),
    [("_builtin_default", "#001023"), ("_builtin_classic", "#000000")],
)
def test_builtin_backgrounds_match_kicad_10_source(
    tmp_path: Path, name: str, background: str
) -> None:
    """Default is navy, classic is black, and neither needs a colors directory."""
    _select(tmp_path, name)
    assert read_theme_files(tmp_path) == ThemeContext(name, background)
    assert not (tmp_path / "colors").exists()


@pytest.mark.parametrize("document", [{}, {"appearance": {}}])
def test_absent_theme_key_uses_native_builtin_default(
    tmp_path: Path, document: dict[str, Any]
) -> None:
    """An absent key has a native default unlike an unreadable whole file."""
    _write(tmp_path / "pcbnew.json", document)
    assert read_theme_files(tmp_path) == ThemeContext("_builtin_default", "#001023")


@pytest.mark.parametrize(
    ("color", "expected"),
    [
        ("rgb(255, 255, 255)", "#ffffff"),
        ("rgb(12, 24, 36)", "#0c1824"),
        ("rgba(240, 241, 242, 1.000)", "#f0f1f2"),
        (" #AbCDEF ", "#abcdef"),
        ("#AABBCCFF", "#aabbcc"),
    ],
)
def test_custom_light_and_dark_backgrounds_preserve_exact_rgb(
    tmp_path: Path, color: str, expected: str
) -> None:
    """Keep selected RGB channels rather than imposing the plugin's dark canvas."""
    _select(tmp_path, "custom")
    _write(tmp_path / "colors/custom.json", {"board": {"background": color}})
    _assert_theme(read_theme_files(tmp_path), "custom", expected)


@pytest.mark.parametrize("document", [{}, {"board": {}}])
def test_missing_background_inherits_default_not_another_theme(
    tmp_path: Path, document: dict[str, Any]
) -> None:
    """Partial themes inherit the native default color map."""
    _select(tmp_path, "partial")
    _write(tmp_path / "colors/partial.json", document)
    assert read_theme_files(tmp_path).background == "#001023"


def test_native_reader_rereads_selection_and_colors_without_mutating_settings(
    tmp_path: Path,
) -> None:
    """A preview after theme A→B must read B, without Save/Load/native mkdir calls."""

    def forbidden() -> None:
        raise AssertionError("Palette inspection must not mutate native settings")

    module = SimpleNamespace(
        SETTINGS_MANAGER=SimpleNamespace(
            GetUserSettingsPath=lambda: str(tmp_path),
            GetColorSettingsPath=forbidden,
        ),
        GetSettingsManager=forbidden,
    )
    _select(tmp_path, "light")
    _write(tmp_path / "colors/light.json", {"board": {"background": "#ffffff"}})
    _write(tmp_path / "colors/dark.json", {"board": {"background": "#123456"}})
    assert read_theme_context(module).background == "#ffffff"
    _select(tmp_path, "dark")
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    _assert_theme(read_theme_context(module), "dark", "#123456")
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == before
    _write(tmp_path / "colors/dark.json", {"board": {"background": "#234567"}})
    assert read_theme_context(module).background == "#234567"


def test_absolute_system_or_pcm_theme_is_read_at_its_registered_path(
    tmp_path: Path,
) -> None:
    """Installed theme IDs already contain the full path and JSON extension."""
    theme = tmp_path / "installed themes" / "Light.json"
    _write(theme, {"board": {"background": "rgb(250, 251, 252)"}})
    _select(tmp_path, str(theme))
    _assert_theme(read_theme_files(tmp_path), str(theme), "#fafbfc")
    assert not (tmp_path / "colors").exists()


def test_user_theme_id_can_itself_end_with_json(tmp_path: Path) -> None:
    """Native GetName strips one extension; theme.json.json has ID theme.json."""
    _select(tmp_path, "theme.json")
    _write(tmp_path / "colors/theme.json.json", {"board": {"background": "#ffffff"}})
    _assert_theme(read_theme_files(tmp_path), "theme.json", "#ffffff")


def test_explicit_gallery_theme_override_does_not_require_editor_preferences(
    tmp_path: Path,
) -> None:
    """A deliberately selected CLI palette does not need a persisted editor ID."""
    colors = tmp_path / "explicit colors"
    _write(colors / "print.json", {"board": {"background": "#ffffff"}})
    _assert_theme(
        read_theme_files(tmp_path, theme="print", color_dir=colors), "print", "#ffffff"
    )
    assert not (tmp_path / "pcbnew.json").exists()
    assert read_theme_files(tmp_path, theme="_builtin_classic").background == "#000000"


@pytest.mark.parametrize("name", ["missing", "/missing/theme.json"])
def test_missing_custom_theme_fails_instead_of_guessing_cached_palette_background(
    tmp_path: Path, name: str
) -> None:
    """A deleted custom theme could still be cached by the live native editor."""
    _select(tmp_path, name)
    with pytest.raises(PaletteError, match="theme"):
        read_theme_files(tmp_path)


def test_missing_whole_preferences_file_is_actionable(tmp_path: Path) -> None:
    """A failed or deleted preference save cannot prove the live theme identity."""
    with pytest.raises(PaletteError, match="Preferences"):
        read_theme_files(tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "document",
    [
        [],
        None,
        {"appearance": []},
        {"appearance": None},
        {"appearance": {"color_theme": 42}},
        {"appearance": {"color_theme": ""}},
        {"appearance": {"color_theme": " "}},
    ],
)
def test_invalid_preferences_schema_is_not_silently_defaulted(
    tmp_path: Path, document: Any
) -> None:
    """Invalid values must not disguise a custom palette as the default."""
    _write(tmp_path / "pcbnew.json", document)
    with pytest.raises(PaletteError, match="Preferences"):
        read_theme_files(tmp_path)


@pytest.mark.parametrize(
    "contents",
    ["{", '{"appearance": {}, "appearance": {}}', '{"invalid": NaN}', "\udcff"],
)
def test_corrupt_preferences_are_rejected(tmp_path: Path, contents: str) -> None:
    """Ambiguous or invalid JSON produces a useful Preferences recovery hint."""
    (tmp_path / "pcbnew.json").write_bytes(
        contents.encode("utf-8", errors="surrogatepass")
    )
    with pytest.raises(PaletteError, match="Preferences"):
        read_theme_files(tmp_path)


@pytest.mark.parametrize(
    "background",
    [
        None,
        42,
        True,
        [],
        {},
        "",
        "url(file:///secret)",
        "#12345g",
        "#ffffff00",
        "rgba(255, 255, 255, 0.5)",
        "rgba(255, 255, 255, 0.999)",
        "rgba(255, 255, 255, 0.999999999999999999999999999999)",
        "rgb(256, 0, 0)",
        "rgb(-1, 0, 0)",
        "rgba(0, 0, 0, NaN)",
        "rgba(0, 0, 0, 1.1)",
        "rgb(1, 2, 3);fill:red",
    ],
)
def test_invalid_or_translucent_background_fails_clearly(
    tmp_path: Path, background: Any
) -> None:
    """Never invent an alpha backdrop or interpolate unsafe CSS into an SVG."""
    _select(tmp_path, "broken")
    _write(tmp_path / "colors/broken.json", {"board": {"background": background}})
    with pytest.raises(PaletteError, match="background"):
        read_theme_files(tmp_path)


@pytest.mark.parametrize("document", [[], None, {"board": []}, {"board": None}])
def test_invalid_theme_schema_is_rejected(tmp_path: Path, document: Any) -> None:
    """A present invalid board object must not inherit a valid default."""
    _select(tmp_path, "broken")
    _write(tmp_path / "colors/broken.json", document)
    with pytest.raises(PaletteError, match="theme"):
        read_theme_files(tmp_path)


@pytest.mark.parametrize(
    "contents",
    [
        b"{",
        b'{"board":{"background":"#ffffff","background":"#000000"}}',
        b'{"board":{"background":NaN}}',
        b"\xff",
    ],
)
def test_corrupt_selected_theme_is_rejected(tmp_path: Path, contents: bytes) -> None:
    """Theme JSON needs the same corruption and ambiguity checks as preferences."""
    _select(tmp_path, "broken")
    colors = tmp_path / "colors"
    colors.mkdir()
    (colors / "broken.json").write_bytes(contents)
    with pytest.raises(PaletteError, match="color theme"):
        read_theme_files(tmp_path)


@pytest.mark.parametrize("name", ["../escape", "nested/theme", "bad\x00name"])
def test_relative_ids_must_match_kicad_extensionless_basename_contract(
    tmp_path: Path, name: str
) -> None:
    """Do not interpret malformed IDs as paths outside the native theme lookup."""
    with pytest.raises(PaletteError, match="theme"):
        read_theme_files(tmp_path, theme=name)


def test_unreadable_selected_theme_is_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An access failure preserves the cause instead of choosing default colors."""
    _select(tmp_path, "custom")
    theme = tmp_path / "colors/custom.json"
    _write(theme, {"board": {"background": "#ffffff"}})
    original = Path.read_bytes

    def denied(path: Path) -> bytes:
        if path == theme:
            raise PermissionError("access denied")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(PaletteError, match="access denied"):
        read_theme_files(tmp_path)


@pytest.mark.parametrize(
    "module", [None, SimpleNamespace(), SimpleNamespace(SETTINGS_MANAGER=None)]
)
def test_native_path_api_missing_is_actionable(module: Any) -> None:
    """An unsupported binding cannot discover the user's native settings path."""
    with pytest.raises(PaletteError, match="settings"):
        read_theme_context(module)


@pytest.mark.parametrize("directory", [None, "", "relative/settings", 42])
def test_invalid_native_settings_path_is_actionable(directory: Any) -> None:
    """Do not read a guessed current-directory path when native lookup fails."""
    module = SimpleNamespace(
        SETTINGS_MANAGER=SimpleNamespace(GetUserSettingsPath=lambda: directory)
    )
    with pytest.raises(PaletteError, match="settings"):
        read_theme_context(module)


def test_native_settings_lookup_exception_is_actionable() -> None:
    """Wrap platform errors at the native boundary with recovery instructions."""

    def broken() -> str:
        raise RuntimeError("native unavailable")

    module = SimpleNamespace(
        SETTINGS_MANAGER=SimpleNamespace(GetUserSettingsPath=broken)
    )
    with pytest.raises(PaletteError, match="native unavailable"):
        read_theme_context(module)


def test_theme_context_is_immutable() -> None:
    """A capture's palette identity and background travel as one stable value."""
    context = ThemeContext("_builtin_default", "#001023")
    with pytest.raises(FrozenInstanceError):
        context.background = "#ffffff"


def test_custom_copper_edit_changes_capture_identity_without_a_background_change(
    tmp_path: Path,
) -> None:
    """A same-name theme edit cannot reuse a capture painted with previous copper colors."""
    _select(tmp_path, "custom")
    path = tmp_path / "colors/custom.json"
    _write(path, {"board": {"background": "#ffffff", "copper": {"f": "#123456"}}})
    original = read_theme_files(tmp_path)
    _write(path, {"board": {"copper": {"f": "#123456"}, "background": "#ffffff"}})
    assert read_theme_files(tmp_path) == original, (
        "JSON field ordering is not a visual change"
    )
    _write(path, {"board": {"background": "#ffffff", "copper": {"f": "#abcdef"}}})
    changed = read_theme_files(tmp_path)
    assert (changed.name, changed.background) == (original.name, original.background)
    assert changed.palette_digest != original.palette_digest
    assert changed != original


@pytest.mark.parametrize(
    ("color", "expected"),
    [
        ("rgb(12, 24, 36)", (12, 24, 36, 255)),
        ("rgba(12, 24, 36, 0.5)", (12, 24, 36, 128)),
        ("rgba(12, 24, 36, 0.999)", (12, 24, 36, 255)),
        ("#0c182400", (12, 24, 36, 0)),
        ("#0c18247f", (12, 24, 36, 127)),
        ("#0c1824fe", (12, 24, 36, 254)),
    ],
)
def test_copper_colors_preserve_alpha_without_reading_capture_settings(
    tmp_path: Path, color: str, expected: tuple[int, int, int, int]
) -> None:
    """Valid copper remains usable with an invalid canvas or dimming preference."""
    module = SimpleNamespace(
        SETTINGS_MANAGER=SimpleNamespace(GetUserSettingsPath=lambda: str(tmp_path))
    )
    _select(tmp_path, "custom")
    _write(
        tmp_path / "kicad_common.json",
        {"appearance": {"hicontrast_dimming_factor": "invalid"}},
    )
    _write(
        tmp_path / "colors/custom.json",
        {"board": {"background": "invalid", "copper": {"f": color}}},
    )
    colors = read_stackup_copper_colors(module)
    assert colors["F.Cu"] == expected
    assert colors["B.Cu"] == (77, 127, 196, 255)
    assert len(colors) == 32
    with pytest.raises(PaletteError, match="dimming"):
        read_theme_context(module)
    _write(tmp_path / "kicad_common.json", {})
    with pytest.raises(PaletteError, match="background"):
        read_theme_context(module)


def test_copper_reader_observes_saved_switches_and_edits_without_writes(
    tmp_path: Path,
) -> None:
    """Builtins and partial custom themes follow the latest saved editor choice."""

    def forbidden() -> None:
        raise AssertionError("Copper inspection must not mutate native settings")

    module = SimpleNamespace(
        SETTINGS_MANAGER=SimpleNamespace(
            GetUserSettingsPath=lambda: str(tmp_path), GetColorSettingsPath=forbidden
        ),
        GetSettingsManager=forbidden,
    )
    _select(tmp_path, "_builtin_classic")
    assert read_stackup_copper_colors(module)["F.Cu"] == (132, 0, 0, 255)
    _select(tmp_path, "_builtin_default")
    assert read_stackup_copper_colors(module)["F.Cu"] == (200, 52, 52, 255)
    assert not (tmp_path / "colors").exists()
    theme = tmp_path / "installed themes" / "Custom.json"
    _select(tmp_path, str(theme))
    _write(theme, {"board": {"copper": {"f": "#123456"}}})
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert read_stackup_copper_colors(module)["F.Cu"] == (18, 52, 86, 255)
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == before
    _write(theme, {"board": {"copper": {"f": "#abcdef"}}})
    assert read_stackup_copper_colors(module)["F.Cu"] == (171, 205, 239, 255)


@pytest.mark.parametrize(
    "copper", [None, [], {"f": "rgb(256, 0, 0)"}, {"f": "rgba(1, 2, 3, 1.1)"}]
)
def test_invalid_custom_copper_is_not_silently_replaced(
    tmp_path: Path, copper: Any
) -> None:
    """Only absent colors inherit defaults; malformed colors remain actionable."""
    module = SimpleNamespace(
        SETTINGS_MANAGER=SimpleNamespace(GetUserSettingsPath=lambda: str(tmp_path))
    )
    _select(tmp_path, "custom")
    _write(tmp_path / "colors/custom.json", {"board": {"copper": copper}})
    with pytest.raises(PaletteError, match="color"):
        read_stackup_copper_colors(module)


def test_missing_selected_copper_theme_does_not_fall_back(tmp_path: Path) -> None:
    """A deleted saved theme must not be mistaken for the builtin palette."""
    module = SimpleNamespace(
        SETTINGS_MANAGER=SimpleNamespace(GetUserSettingsPath=lambda: str(tmp_path))
    )
    _select(tmp_path, "missing")
    with pytest.raises(PaletteError, match="theme"):
        read_stackup_copper_colors(module)
