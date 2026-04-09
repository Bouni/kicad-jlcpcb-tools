"""IPC-backed adapter implementations.

These adapters satisfy the existing local adapter contracts while driving
KiCad directly through the official ``kipy`` API.  All board/footprint
serialisation and business logic lives here; ``ipc_client.py`` handles only
connection management.
"""

from functools import wraps
import re
from typing import Any, Optional

from kipy.errors import ApiError as KiPyApiError, ConnectionError as KiPyConnectionError
from kipy.proto.board import board_commands_pb2

from ipc_client import KiCadIPCClient, KiCadIPCError
from kicad_api import (
    EXCLUDE_FROM_BOM,
    EXCLUDE_FROM_POS,
    BoardAPI,
    FootprintAPI,
    UtilityAPI,
)


class IPCPoint:
    """Simple point object compatible with x/y access and tuple unpacking."""

    def __init__(self, x: float, y: float):
        self.x = x
        self.y = y

    def __iter__(self):
        """Yield x/y coordinates for tuple-style unpacking."""
        yield self.x
        yield self.y


# ---------------------------------------------------------------------------
# Kipy-error wrapper
# ---------------------------------------------------------------------------

def _wrap_kipy(fn):
    """Convert kipy transport errors into `KiCadIPCError`."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except KiCadIPCError:
            raise
        except (KiPyApiError, KiPyConnectionError, OSError) as exc:
            raise KiCadIPCError(str(exc)) from exc
    return wrapper


# ---------------------------------------------------------------------------
# Footprint serialisation helpers
# ---------------------------------------------------------------------------

def _item_id(item: Any) -> str:
    """Return a stable string identifier for a kipy board item."""
    kiid = getattr(item, "id", None)
    value = getattr(kiid, "value", None)
    if isinstance(value, bytes):
        return value.hex()
    return str(kiid)


def _footprint_reference(fp: Any) -> str:
    return str(fp.reference_field.text.value)


def _extract_fields(fp: Any) -> list[dict[str, Any]]:
    """Extract named field dicts from a footprint's ``texts_and_fields``.

    Uses duck typing so plain test fakes work alongside real kipy objects.
    Items without both ``.name`` and ``.text`` (plain ``BoardText``) are skipped.
    """
    result = []
    for item in fp.texts_and_fields:
        name = getattr(item, "name", None)
        text = getattr(item, "text", None)
        if name is not None and text is not None:
            result.append({"name": str(name), "value": str(getattr(text, "value", ""))})
    return result


def _set_field(fp: Any, name: str, value: str) -> None:
    """Update an existing named field on *fp* or append a new one."""
    lowered = name.lower()
    for item in fp.texts_and_fields:
        field_name = getattr(item, "name", None)
        if field_name is not None and field_name.lower() == lowered:
            item.text.value = value
            return
    # Field not found — create via kipy (late import keeps test fakes light).
    from kipy.board_types import Field  # noqa: PLC0415
    new_field = Field()
    new_field.name = name
    new_field.text.value = value
    fp.definition.add_item(new_field)


def _footprint_attributes_mask(fp: Any) -> int:
    """Encode kipy attribute flags as the bitmask used by this project."""
    mask = 0
    if fp.attributes.exclude_from_position_files:
        mask |= 1 << 2
    if fp.attributes.exclude_from_bill_of_materials:
        mask |= 1 << 3
    return mask


def _apply_footprint_mask(fp: Any, mask: int) -> None:
    """Apply a project bitmask back to a kipy footprint's attribute flags."""
    fp.attributes.exclude_from_position_files = bool(mask & (1 << 2))
    fp.attributes.exclude_from_bill_of_materials = bool(mask & (1 << 3))


def _footprint_payload(fp: Any) -> dict[str, Any]:
    """Serialise a kipy ``FootprintInstance`` to a plain dict payload."""
    return {
        "id": _item_id(fp),
        "reference": _footprint_reference(fp),
        "value": str(fp.value_field.text.value),
        "fpid_name": str(fp.definition.id.name),
        "layer": int(fp.layer),
        "orientation_degrees": float(fp.orientation.degrees),
        "position": {"x": float(fp.position.x), "y": float(fp.position.y)},
        "attributes": _footprint_attributes_mask(fp),
        "fields": _extract_fields(fp),
        "pads": [
            {
                "id": _item_id(pad),
                "number": pad.number,
                "position": {"x": float(pad.position.x), "y": float(pad.position.y)},
            }
            for pad in fp.definition.pads
        ],
        "is_dnp": bool(fp.attributes.do_not_populate),
    }


def _find_footprint(board: Any, footprint_id: str) -> Any:
    """Find a live kipy ``FootprintInstance`` by its string id."""
    for fp in board.get_footprints():
        if _item_id(fp) == footprint_id:
            return fp
    raise KiCadIPCError(f"Footprint not found: {footprint_id}")


# ---------------------------------------------------------------------------
# Board adapter
# ---------------------------------------------------------------------------

class IPCBoardAdapter(BoardAPI):
    """IPC implementation of board-level adapter operations."""

    def __init__(self, client: KiCadIPCClient):
        self.client = client
        self._cached_board = None

    def _board(self):
        """Return a cached kipy Board for the lifetime of this adapter."""
        if self._cached_board is None:
            self._cached_board = self.client.board()
        return self._cached_board

    @_wrap_kipy
    def get_board(self) -> dict[str, Any]:
        """Return a serialised board metadata dict."""
        board = self._board()
        return {
            "id": str(board.document),
            "path": str(board.document.board_filename),
            "copper_layer_count": int(board.get_copper_layer_count()),
        }

    def get_board_filename(self) -> str:
        """Return the absolute path of the active board file."""
        return str(self.get_board().get("path", ""))

    @_wrap_kipy
    def get_all_footprints(self) -> list[Any]:
        """Return all footprint payloads sorted by reference designator."""
        return sorted(
            self.get_footprints(),
            key=lambda fp: str(fp.get("reference", "")),
        )

    @_wrap_kipy
    def get_footprints(self) -> list[Any]:
        """Return footprint payloads in board iteration order."""
        return [_footprint_payload(fp) for fp in self._board().get_footprints()]

    @_wrap_kipy
    def get_footprint_by_reference(self, reference: str) -> Optional[dict[str, Any]]:
        """Return a footprint payload matching `reference`, if present."""
        for fp in self._board().get_footprints():
            if _footprint_reference(fp) == reference:
                return _footprint_payload(fp)
        return None

    @_wrap_kipy
    def get_enabled_layers(self) -> list[int]:
        """Return enabled board layer ids."""
        return list(self._board().get_enabled_layers())

    @_wrap_kipy
    def get_layer_name(self, layer_id: int) -> str:
        """Return the display name for `layer_id`."""
        return str(self._board().get_layer_name(layer_id))  # type: ignore[arg-type]

    def get_design_settings(self) -> Any:
        """Return design settings (not yet exposed by kipy)."""
        return {}

    def get_drawings(self) -> list[Any]:
        """Return drawings (not yet exposed by kipy)."""
        return []

    def refresh_display(self) -> None:
        """No-op: kipy board mutations take effect immediately."""

    @_wrap_kipy
    def get_current_selection(self) -> list[Any]:
        """Return currently selected board items as serialised payloads."""
        items = []
        for item in self._board().get_selection():
            if hasattr(item, "reference_field"):
                items.append(_footprint_payload(item))
            else:
                items.append({"id": _item_id(item)})
        return items

    @_wrap_kipy
    def get_copper_layer_count(self) -> int:
        """Return the board copper-layer count."""
        return int(self._board().get_copper_layer_count())

    @_wrap_kipy
    def get_aux_origin(self) -> IPCPoint:
        """Return the board auxiliary origin as an `IPCPoint`."""
        origin = self._board().get_origin(board_commands_pb2.BOT_DRILL)
        return IPCPoint(float(origin.x), float(origin.y))


# ---------------------------------------------------------------------------
# Footprint adapter
# ---------------------------------------------------------------------------

class IPCFootprintAdapter(FootprintAPI):
    """IPC implementation of footprint-level adapter operations."""

    def __init__(self, client: KiCadIPCClient):
        self.client = client
        self._cached_board = None

    def _board(self):
        """Return a cached kipy Board for the lifetime of this adapter."""
        if self._cached_board is None:
            self._cached_board = self.client.board()
        return self._cached_board

    # ------------------------------------------------------------------
    # Read-only accessors — served from the dict payload
    # ------------------------------------------------------------------

    def get_reference(self, footprint: Any) -> str:
        """Return footprint reference designator from payload data."""
        return str(_payload_key(footprint, "reference"))

    def get_value(self, footprint: Any) -> str:
        """Return footprint value from payload data."""
        return str(_payload_key(footprint, "value"))

    def get_fpid_name(self, footprint: Any) -> str:
        """Return footprint package/library identifier from payload data."""
        return str(_payload_key(footprint, "fpid_name", fallback_keys=("footprint",)))

    def get_layer(self, footprint: Any) -> int:
        """Return footprint layer id from payload data."""
        return int(_payload_key(footprint, "layer", default=0))

    def get_orientation(self, footprint: Any) -> float:
        """Return footprint orientation in degrees from payload data."""
        return float(
            _payload_key(
                footprint, "orientation",
                fallback_keys=("orientation_degrees",),
                default=0.0,
            )
        )

    def get_position(self, footprint: Any) -> tuple[float, float]:
        """Return footprint position `(x, y)` from payload data."""
        point = _coerce_point(_payload_key(footprint, "position", default={}))
        return (point.x, point.y)

    def get_attributes(self, footprint: Any) -> int:
        """Return project-compatible footprint attribute bitmask."""
        return int(_payload_key(footprint, "attributes", default=0) or 0)

    def get_lcsc_value(self, footprint: Any) -> str:
        """Return normalized LCSC code extracted from footprint fields."""
        fields = footprint.get("fields") if isinstance(footprint, dict) else None
        return _extract_lcsc(fields or [])

    def get_exclude_from_pos(self, footprint: Any) -> bool:
        """Return whether footprint is excluded from position files."""
        if isinstance(footprint, dict) and "exclude_from_pos" in footprint:
            return bool(footprint["exclude_from_pos"])
        return bool(self.get_attributes(footprint) & (1 << EXCLUDE_FROM_POS))

    def get_exclude_from_bom(self, footprint: Any) -> bool:
        """Return whether footprint is excluded from BOM output."""
        if isinstance(footprint, dict) and "exclude_from_bom" in footprint:
            return bool(footprint["exclude_from_bom"])
        return bool(self.get_attributes(footprint) & (1 << EXCLUDE_FROM_BOM))

    def get_is_dnp(self, footprint: Any) -> bool:
        """Return whether footprint is marked do-not-populate (DNP)."""
        if isinstance(footprint, dict) and "is_dnp" in footprint:
            return bool(footprint["is_dnp"])
        return False

    def get_pads(self, footprint: Any) -> list[Any]:
        """Return pad payloads for the footprint."""
        if isinstance(footprint, dict) and "pads" in footprint:
            return list(footprint["pads"] or [])
        return []

    # ------------------------------------------------------------------
    # Mutations — find the live kipy object, mutate, push update
    # ------------------------------------------------------------------

    @_wrap_kipy
    def set_attributes(self, footprint: Any, attributes: int) -> None:
        """Apply attribute bitmask and persist the footprint update."""
        board = self._board()
        fp = _find_footprint(board, str(_footprint_id(footprint)))
        _apply_footprint_mask(fp, attributes)
        board.update_items([fp])
        if isinstance(footprint, dict):
            footprint["attributes"] = attributes

    @_wrap_kipy
    def set_lcsc_value(self, footprint: Any, lcsc: str) -> None:
        """Set the `LCSC` field on footprint and persist the update."""
        board = self._board()
        fp = _find_footprint(board, str(_footprint_id(footprint)))
        _set_field(fp, "LCSC", lcsc)
        board.update_items([fp])
        if isinstance(footprint, dict):
            fields = footprint.setdefault("fields", [])
            if isinstance(fields, list):
                for field in fields:
                    if (
                        isinstance(field, dict)
                        and str(field.get("name", "")).lower() == "lcsc"
                    ):
                        field["value"] = lcsc
                        break
                else:
                    fields.append({"name": "LCSC", "value": lcsc})
            if isinstance(fields, dict):
                fields["LCSC"] = lcsc

    @_wrap_kipy
    def toggle_exclude_from_pos(self, footprint: Any) -> bool:
        """Toggle exclude-from-position state and return the new value."""
        new_state = not self.get_exclude_from_pos(footprint)
        board = self._board()
        fp = _find_footprint(board, str(_footprint_id(footprint)))
        fp.attributes.exclude_from_position_files = new_state
        board.update_items([fp])
        if isinstance(footprint, dict):
            footprint["exclude_from_pos"] = new_state
        return new_state

    @_wrap_kipy
    def toggle_exclude_from_bom(self, footprint: Any) -> bool:
        """Toggle exclude-from-BOM state and return the new value."""
        new_state = not self.get_exclude_from_bom(footprint)
        board = self._board()
        fp = _find_footprint(board, str(_footprint_id(footprint)))
        fp.attributes.exclude_from_bill_of_materials = new_state
        board.update_items([fp])
        if isinstance(footprint, dict):
            footprint["exclude_from_bom"] = new_state
        return new_state

    @_wrap_kipy
    def set_selected(self, footprint: Any) -> None:
        """Select the footprint in the active board UI selection."""
        board = self._board()
        fp = _find_footprint(board, str(_footprint_id(footprint)))
        board.add_to_selection([fp])

    @_wrap_kipy
    def clear_selected(self, footprint: Any) -> None:
        """Remove the footprint from the active board UI selection."""
        board = self._board()
        fp = _find_footprint(board, str(_footprint_id(footprint)))
        board.remove_from_selection([fp])


# ---------------------------------------------------------------------------
# Utility adapter
# ---------------------------------------------------------------------------

class IPCUtilityAdapter(UtilityAPI):
    """IPC implementation of utility helpers."""

    def __init__(self, client: Optional[KiCadIPCClient] = None):
        self.client = client

    def from_mm(self, value: float) -> int:
        """Convert millimetres to internal KiCad board units."""
        return int(round(value * 1_000_000))

    def to_mm(self, value: int) -> float:
        """Convert internal KiCad board units to millimetres."""
        return float(value) / 1_000_000.0

    def get_layer_constants(self) -> dict[str, int]:
        """Return canonical layer-id constants consumed by business logic."""
        return {
            "F_Cu": 0,
            "B_Cu": 1,
            "F_SilkS": 2,
            "B_SilkS": 3,
            "F_Mask": 4,
            "B_Mask": 5,
            "F_Paste": 7,
            "B_Paste": 8,
            "Edge_Cuts": 9,
        }

    def get_pcb_constants(self) -> dict[str, Any]:
        """Return PCB object/type constants consumed by business logic."""
        constants: dict[str, Any] = dict(self.get_layer_constants())
        constants.update({"PCB_TEXT": "PCB_TEXT", "PCB_SHAPE": "PCB_SHAPE", "S_RECT": 0})
        return constants

    def get_no_drill_shape(self) -> int:
        """Return sentinel value for 'no drill shape'."""
        return 0

    def get_plot_format_gerber(self) -> int:
        """Return sentinel value for Gerber plot format."""
        return 1

    def get_inner_cu_layer(self, layer: int) -> int:
        """Return inner copper layer id passthrough."""
        return layer

    def create_vector2i(self, x: int, y: int) -> IPCPoint:
        """Create an `IPCPoint` from integer coordinates."""
        return IPCPoint(x, y)

    def create_wx_point(self, x: float, y: float) -> IPCPoint:
        """Create an `IPCPoint` from floating-point coordinates."""
        return IPCPoint(x, y)

    @_wrap_kipy
    def refill_zones(self, board: Any) -> None:
        """Trigger zone refill on the active board when client is available."""
        if self.client is None:
            return
        self.client.board().refill_zones()


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _coerce_point(payload: Any) -> IPCPoint:
    if isinstance(payload, IPCPoint):
        return payload
    if isinstance(payload, dict):
        return IPCPoint(float(payload.get("x", 0)), float(payload.get("y", 0)))
    if isinstance(payload, (tuple, list)) and len(payload) >= 2:
        return IPCPoint(float(payload[0]), float(payload[1]))
    return IPCPoint(0, 0)


def _footprint_id(footprint: Any) -> Any:
    if isinstance(footprint, dict) and "id" in footprint:
        return footprint["id"]
    return footprint


def _payload_key(
    footprint: Any,
    key: str,
    fallback_keys: tuple[str, ...] = (),
    default: Any = "",
) -> Any:
    """Read a key from a dict footprint payload, with optional fallback keys."""
    if isinstance(footprint, dict):
        if key in footprint:
            return footprint[key]
        for fk in fallback_keys:
            if fk in footprint:
                return footprint[fk]
    return default


def _extract_lcsc(fields: Any) -> str:
    """Extract a normalised LCSC part number from a field collection."""
    if isinstance(fields, dict):
        items = list(fields.items())
    elif isinstance(fields, list):
        items = []
        for field in fields:
            if isinstance(field, dict):
                # list of {"name": ..., "value": ...} dicts (from _extract_fields)
                items.append((field.get("name", ""), field.get("value", field.get("text", ""))))
    else:
        items = []

    for key, value in items:
        if re.match(r"lcsc|jlc", str(key), re.IGNORECASE) and re.match(
            r"^C\d+$", str(value)
        ):
            return str(value)
    return ""
