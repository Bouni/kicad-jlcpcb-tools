"""Read KiCad's selected PCB appearance without modifying settings.

Native layer plots supply capture foreground colors. The canvas background,
inactive-layer dimming, and schematic stackup copper colors are read from disk
because KiCad's SWIG settings are not inspectable.
This follows the persisted selection after Preferences OK, not an inaccessible
unsaved selection. Never reload or save KiCad settings to obtain a palette.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, NoReturn, Optional

# KiCad 10.0.6 common/settings/builtin_color_themes.h. A partial custom theme
# inherits the default map, not the classic map or another custom theme.
_BUILTIN_BACKGROUNDS = {"_builtin_default": "#001023", "_builtin_classic": "#000000"}
_DEFAULT_THEME = "_builtin_default"
_GUIDANCE = "Open Preferences, choose the PCB Editor color theme, and click OK."
CopperColor = tuple[int, int, int, int]
_COPPER_KEYS = (
    ("F.Cu", "f"),
    *((f"In{number}.Cu", f"in{number}") for number in range(1, 31)),
    ("B.Cu", "b"),
)

# KiCad common/settings/builtin_color_themes.h, s_defaultTheme copper map.
# Partial custom themes also inherit this map, not the classic map.
_DEFAULT_RGB = (
    (200, 52, 52),
    (127, 200, 127),
    (206, 125, 44),
    (79, 203, 203),
    (219, 98, 139),
    (167, 165, 198),
    (40, 204, 217),
    (232, 178, 167),
    (242, 237, 161),
    (141, 203, 129),
    (237, 124, 51),
    (91, 195, 235),
    (247, 111, 142),
    (167, 165, 198),
    (40, 204, 217),
    (232, 178, 167),
    (242, 237, 161),
    (237, 124, 51),
    (91, 195, 235),
    (247, 111, 142),
    (167, 165, 198),
    (40, 204, 217),
    (232, 178, 167),
    (242, 237, 161),
    (237, 124, 51),
    (91, 195, 235),
    (247, 111, 142),
    (167, 165, 198),
    (40, 204, 217),
    (232, 178, 167),
    (242, 237, 161),
    (77, 127, 196),
)

# Same header's s_classicTheme, expanded through common/gal/color4d.cpp's
# colorRefs(). Its source StructColors fields are B, G, R; these tuples are RGB.
_CLASSIC_RGB = (
    (132, 0, 0),
    (194, 194, 0),
    (194, 0, 194),
    (194, 0, 0),
    (0, 132, 132),
    (0, 132, 0),
    (0, 0, 132),
    (132, 132, 132),
    (132, 0, 132),
    (194, 194, 194),
    (132, 0, 132),
    (132, 0, 0),
    (132, 132, 0),
    (194, 194, 194),
    (0, 0, 132),
    (0, 132, 0),
    (132, 0, 0),
    (194, 194, 0),
    (194, 0, 194),
    (194, 0, 0),
    (0, 132, 132),
    (0, 132, 0),
    (0, 0, 132),
    (132, 132, 132),
    (132, 0, 132),
    (194, 194, 194),
    (132, 0, 132),
    (132, 0, 0),
    (132, 132, 0),
    (194, 194, 194),
    (0, 0, 132),
    (0, 132, 0),
)


class PaletteError(RuntimeError):
    """Report an unavailable or ambiguous native color theme actionably."""


@dataclass(frozen=True)
class ThemeContext:
    """Hashable persisted palette and native inactive-layer dimming context."""

    name: str
    background: str
    dimming_factor: float = 0.8
    palette_digest: str = ""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous JSON rather than selecting one duplicate setting."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> NoReturn:
    """Reject non-standard JSON numbers such as NaN."""
    raise ValueError(f"Invalid JSON constant: {value}")


def _read_object(
    path: Path, description: str, *, missing_ok: bool = False
) -> dict[str, Any]:
    """Read exactly one settings file, accepting no invalid container schema."""
    try:
        document = json.loads(
            path.read_bytes().decode("utf-8-sig"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
        if not isinstance(document, dict):
            raise TypeError("The root must be a JSON object")
        return document
    except (OSError, ValueError, TypeError, RecursionError) as error:
        if missing_ok and isinstance(error, FileNotFoundError):
            return {}
        raise PaletteError(
            f"Cannot read KiCad {description} at {path}: {error}. {_GUIDANCE}"
        ) from error


def _dimming_factor(config_dir: Path) -> float:
    """Read KiCad's common DIMMED strength, using its unsaved-file default."""
    document = _read_object(
        config_dir / "kicad_common.json", "common appearance settings", missing_ok=True
    )
    appearance = document.get("appearance", {})
    if not isinstance(appearance, dict):
        raise PaletteError(f"Invalid KiCad common appearance settings. {_GUIDANCE}")
    factor = appearance.get("hicontrast_dimming_factor", 0.8)
    if (
        isinstance(factor, bool)
        or not isinstance(factor, (int, float))
        or not 0 <= factor <= 1
        or not math.isfinite(factor)
    ):
        raise PaletteError(
            "Invalid KiCad inactive-layer dimming factor. Choose a dimming level "
            f"in Preferences and click OK. {_GUIDANCE}"
        )
    return float(factor)


def _theme_name(value: Any) -> str:
    """Require an actual persisted ID, not a guessed display name or empty value."""
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise PaletteError(f"Invalid KiCad color theme identifier. {_GUIDANCE}")
    return value


def _rgba(value: Any, message: str) -> tuple[int, int, int, Decimal]:
    """Decode bounded native RGB/RGBA or hex, retaining alpha until validation."""
    if not isinstance(value, str) or len(value) > 128:
        raise PaletteError(message)
    color = value.strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?", color):
        red, green, blue = (int(color[start : start + 2], 16) for start in (1, 3, 5))
        alpha = Decimal(int(color[7:9], 16)) / 255 if len(color) == 9 else Decimal(1)
        return red, green, blue, alpha
    match = re.fullmatch(
        r"(rgb|rgba)\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})"
        r"(?:\s*,\s*(\d+(?:\.\d*)?|\.\d+))?\s*\)",
        color,
        flags=re.ASCII,
    )
    if match is None:
        raise PaletteError(message)
    kind, red_text, green_text, blue_text, alpha_text = match.groups()
    red, green, blue = (int(channel) for channel in (red_text, green_text, blue_text))
    if any(channel > 255 for channel in (red, green, blue)):
        raise PaletteError(message)
    if kind == "rgb" and alpha_text is None:
        return red, green, blue, Decimal(1)
    if kind != "rgba" or alpha_text is None:
        raise PaletteError(message)
    alpha = Decimal(alpha_text)
    if not 0 <= alpha <= 1:
        raise PaletteError(message)
    return red, green, blue, alpha


def _opaque_background(value: Any) -> str:
    """Require exact opacity before any alpha rounding for the workbook canvas."""
    message = (
        "Cannot read the KiCad PCB background color. Choose an opaque background "
        f"in Preferences and save the theme. {_GUIDANCE}"
    )
    red, green, blue, alpha = _rgba(value, message)
    if alpha != 1:
        raise PaletteError(message)
    return f"#{red:02x}{green:02x}{blue:02x}"


def _theme_path(name: str, color_dir: Path) -> Path:
    """Use native user basename IDs or absolute system/PCM theme file IDs."""
    path = Path(name)
    if path.is_absolute():
        if path.suffix != ".json":
            raise PaletteError(f"Invalid KiCad theme file: {name}. {_GUIDANCE}")
        return path
    # Native GetName() removes exactly one extension: theme.json.json has the ID
    # theme.json. System/PCM themes use their complete absolute path instead.
    if path.name != name or "\\" in name or name in (".", ".."):
        raise PaletteError(f"Invalid KiCad theme identifier: {name}. {_GUIDANCE}")
    return color_dir / (name + ".json")


def _selected_theme(config_dir: Path) -> str:
    """Read the persisted editor choice without checking unrelated appearance."""
    document = _read_object(config_dir / "pcbnew.json", "Preferences")
    appearance = document.get("appearance", {})
    if not isinstance(appearance, dict):
        raise PaletteError(f"Invalid KiCad appearance settings. {_GUIDANCE}")
    return _theme_name(appearance.get("color_theme", _DEFAULT_THEME))


def _custom_theme(name: str, color_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read shared theme structure; each consumer validates only its own colors."""
    path = _theme_path(name, color_dir)
    document = _read_object(path, "color theme")
    board = document.get("board", {})
    if not isinstance(board, dict):
        raise PaletteError(
            f"Invalid KiCad color theme board settings in {path}. {_GUIDANCE}"
        )
    return document, board


def read_theme_files(
    config_dir: Path,
    *,
    theme: Optional[str] = None,
    color_dir: Optional[Path] = None,
) -> ThemeContext:
    """Read a selected palette for native previews or CLI capture tools.

    ``config_dir`` contains pcbnew.json; ``color_dir`` defaults to its colors child.
    An explicit native theme ID bypasses pcbnew.json for CLI --theme overrides.
    Missing custom files fail: the editor could retain a now-deleted custom palette
    in memory, so assuming its native fallback background could mismatch the plot.
    No contents are cached; a later preview observes an accepted theme switch or
    changed dimming strength. A custom-palette digest also catches in-place edits.
    """
    config_dir = Path(config_dir)
    name = _selected_theme(config_dir) if theme is None else _theme_name(theme)
    dimming = _dimming_factor(config_dir)
    if name in _BUILTIN_BACKGROUNDS:
        return ThemeContext(name, _BUILTIN_BACKGROUNDS[name], dimming)
    document, board = _custom_theme(
        name, Path(color_dir) if color_dir is not None else config_dir / "colors"
    )
    background = board.get("background", _BUILTIN_BACKGROUNDS[_DEFAULT_THEME])
    digest = hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ThemeContext(name, _opaque_background(background), dimming, digest)


def _settings_directory(pcbnew_module: Any) -> Path:
    """Locate native settings without calling the directory-creating color API."""
    try:
        manager_type = getattr(pcbnew_module, "SETTINGS_MANAGER", None)
        getter = getattr(manager_type, "GetUserSettingsPath", None)
        directory = getter() if callable(getter) else None
        if not isinstance(directory, str) or not directory.strip():
            raise ValueError("The native user settings directory is unavailable")
        config_dir = Path(directory)
        if not config_dir.is_absolute():
            raise ValueError("The native user settings directory is not absolute")
    except Exception as error:
        raise PaletteError(
            f"Cannot locate KiCad settings: {error}. {_GUIDANCE}"
        ) from error
    return config_dir


def read_theme_context(pcbnew_module: Any) -> ThemeContext:
    """Use KiCad's own configuration directory without triggering any writes."""
    return read_theme_files(_settings_directory(pcbnew_module))


def read_stackup_copper_colors(pcbnew_module: Any) -> dict[str, CopperColor]:
    """Read saved copper colors independently of capture background and dimming.

    Missing custom colors inherit KiCad's default map. Reads remain uncached and
    read-only, so accepted theme switches and edits appear when the picker opens.
    """
    config_dir = _settings_directory(pcbnew_module)
    name = _selected_theme(config_dir)
    builtin = _CLASSIC_RGB if name == "_builtin_classic" else _DEFAULT_RGB
    colors: dict[str, CopperColor] = {
        layer_name: (red, green, blue, 255)
        for (layer_name, _), (red, green, blue) in zip(_COPPER_KEYS, builtin)
    }
    if name in _BUILTIN_BACKGROUNDS:
        return colors
    _, board = _custom_theme(name, config_dir / "colors")
    copper = board.get("copper", {})
    if not isinstance(copper, dict):
        raise PaletteError(f"Invalid KiCad copper color settings. {_GUIDANCE}")
    for layer_name, setting_name in _COPPER_KEYS:
        if setting_name in copper:
            red, green, blue, alpha = _rgba(
                copper[setting_name],
                f"Cannot read the KiCad {layer_name} color. {_GUIDANCE}",
            )
            alpha_byte = int((alpha * 255).quantize(Decimal(1), rounding=ROUND_HALF_UP))
            colors[layer_name] = red, green, blue, alpha_byte
    return colors
