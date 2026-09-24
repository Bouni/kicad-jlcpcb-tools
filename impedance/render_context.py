"""Share the native capture-layer policy without importing plotting machinery."""

from collections.abc import Iterable
import re
from typing import Any, Optional

NONCOPPER_CONTEXT_LAYERS = ("B.SilkS", "F.SilkS", "Edge.Cuts")
CONTEXT_POLICY_VERSION = 1
_COPPER_LAYER = re.compile(r"(?:F|B|In(?:[1-9]|[12]\d|30))\.Cu\Z")
_NONPLOTTED_LAYER = re.compile(
    r"(?:[FB]\.(?:Adhes|Paste|Mask|CrtYd|Fab)|"
    r"(?:Dwgs|Cmts|Eco[12])\.User|User\.(?:[1-9]|[1-3]\d|4[0-5])|"
    r"Margin|Rescue)\Z"
)


def layer_is_context(name: str, copper_names: Optional[Iterable[str]] = None) -> bool:
    """Omit known unplotted layers; retain unfamiliar native layer semantics."""
    if name in NONCOPPER_CONTEXT_LAYERS:
        return True
    if _COPPER_LAYER.fullmatch(name):
        return copper_names is None or name in copper_names
    if name in {"*.Cu", "F&B.Cu"}:
        return True
    return _NONPLOTTED_LAYER.fullmatch(name) is None


def item_is_context(
    item: Any, pcbnew_module: Any, copper_names: Optional[Iterable[str]] = None
) -> bool:
    """Classify typed native text by canonical layer, never a custom board label.

    LSET.Name matches the native serialized layer tokens. LayerName returns
    display strings such as F.Courtyard and User.Drawings; the fallback retains
    unfamiliar names. Missing APIs are not evidence that an item is safe to omit.
    """
    getter = getattr(item, "GetLayer", None)
    canonical = getattr(getattr(pcbnew_module, "LSET", None), "Name", None)
    if not callable(canonical):
        canonical = getattr(pcbnew_module, "LayerName", None)
    if not callable(getter) or not callable(canonical):
        return True
    return layer_is_context(str(canonical(getter())), copper_names)
