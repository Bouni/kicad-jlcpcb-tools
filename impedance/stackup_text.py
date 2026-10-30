"""Complete, clipboard-friendly text for an immutable stackup definition."""

from .stackup_model import Stackup, copper_layer_names

_MISSING = "—"
_PRICE_LABELS = {"additional": "Additional", "none": "Normal", "unknown": _MISSING}


def _cell(value: str) -> str:
    """Retain full values while keeping one physical layer per tab-separated row."""
    if not value:
        return _MISSING
    return (
        value.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def stackup_definition_text(stackup: Stackup, *, saved: bool = False) -> str:
    """Describe exactly this saved or catalog snapshot, without rounding values."""
    copper_names = iter(copper_layer_names(stackup))
    preferred = (
        "Unknown" if not stackup.calculator_id else "Yes" if stackup.preferred else "No"
    )
    lines = [
        f"JLCPCB stackup: {_cell(stackup.name)}",
        f"Snapshot: {'Saved board selection' if saved else 'Catalog selection'}",
        f"Stackup ID: {_cell(stackup.stackup_id)}",
        f"Copper layers: {stackup.layer_count}",
        f"Total thickness (mm): {_cell(stackup.thickness_mm)}",
        f"Outer copper (oz): {_cell(stackup.outer_copper_oz)}",
        f"Inner copper (oz): {_cell(stackup.inner_copper_oz)}",
        f"Preferred: {preferred}",
        f"Price category: {_PRICE_LABELS[stackup.charge_status]}",
        f"Calculator ID: {_cell(stackup.calculator_id)}",
        f"Source: {_cell(stackup.source_url)}",
        f"Retrieved at (UTC): {_cell(stackup.retrieved_at_utc)}",
        "",
        "Construction (top to bottom):",
    ]
    if not stackup.layers:
        lines.append("Construction data unavailable.")
    else:
        lines.append(
            "Position\tKiCad layer\tType\tVendor layer\tMaterial\tThickness (mm)\tDk"
        )
        for index, layer in enumerate(stackup.layers, start=1):
            lines.append(
                "\t".join(
                    (
                        str(index),
                        next(copper_names) if layer.kind == "copper" else _MISSING,
                        _cell(layer.kind),
                        _cell(layer.name),
                        _cell(layer.material),
                        _cell(layer.thickness_mm),
                        _cell(layer.dielectric_constant),
                    )
                )
            )
    return "\n".join(lines) + "\n"
