"""Copy live KiCad geometry into immutable impedance records."""

from collections.abc import Iterable
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Optional

from .board_copy import BoardCopyError, _record_tokens, empty_board
from .model import BoardSnapshot, Bounds, CopperFeature, Trace
from .net_metadata import netclass_context, pairing_context
from .project_context import ProjectContextError, project_context_records
from .text_context import TextContextError, text_context_records

Point = tuple[int, int]
_FILL_CACHE_TOKEN = re.compile(r"\((filled_polygon|fill_segments|pcbplotparams)\b")
_FILL_STATE_TOKEN = re.compile(r"\(fill\s+yes(?=[\s()])")
_SEXPR_NAME = re.compile(r"\(\s*([\w_]+)")
_FLASH_READ_ERRORS = (
    AttributeError,
    TypeError,
    ValueError,
    RuntimeError,
    OverflowError,
)


class BoardSnapshotError(RuntimeError):
    """Report unavailable or invalid live-board geometry."""


def copper_layers(board: Any, pcbnew_module: Any) -> dict[str, int]:
    """Return enabled copper names in physical stack order using runtime IDs."""
    try:
        layer_ids = tuple(board.GetEnabledLayers().CuStack())
    except AttributeError as error:
        raise BoardSnapshotError(
            "KiCad cannot enumerate enabled copper layers."
        ) from error
    layers = {}
    for layer_id in layer_ids:
        if hasattr(pcbnew_module, "LayerName"):
            name = str(pcbnew_module.LayerName(layer_id))
        else:
            name = _canonical_layer_name(layer_id, pcbnew_module)
        if not name.endswith(".Cu") or name in layers:
            raise BoardSnapshotError("KiCad returned an invalid copper-layer stack.")
        layers[name] = int(layer_id)
    return layers


def _canonical_layer_name(layer_id: int, pcbnew_module: Any) -> str:
    """Resolve constant names without depending on user-renamed layer labels."""
    names = ("F_Cu",) + tuple(f"In{index}_Cu" for index in range(1, 31)) + ("B_Cu",)
    for name in names:
        if getattr(pcbnew_module, name, None) == layer_id:
            return name.replace("_", ".")
    raise BoardSnapshotError(f"Cannot resolve copper layer {layer_id}.")


def _point(value: Any) -> Point:
    """Copy a KiCad point without retaining a SWIG reference."""
    return int(value.x), int(value.y)


def _is_via(item: Any, pcbnew_module: Any) -> bool:
    """Exclude vias before inspecting the PCB_TRACK base-class API."""
    via_class = getattr(pcbnew_module, "PCB_VIA", None)
    if isinstance(via_class, type) and isinstance(item, via_class):
        return True
    class_name = (
        str(item.GetClass()) if hasattr(item, "GetClass") else type(item).__name__
    )
    return "VIA" in class_name.upper()


def _is_arc(item: Any, pcbnew_module: Any) -> bool:
    """Recognize native arcs even when bindings omit their midpoint getter."""
    arc_class = getattr(pcbnew_module, "PCB_ARC", None)
    if isinstance(arc_class, type) and isinstance(item, arc_class):
        return True
    class_getter = getattr(item, "GetClass", None)
    return hasattr(item, "GetMid") or (
        callable(class_getter) and str(class_getter()).upper() == "PCB_ARC"
    )


def _feature_id(item: Any, data: tuple[Any, ...]) -> str:
    """Stable identity for one pad/via copper flash on one layer."""
    uuid = getattr(item, "m_Uuid", None)
    if uuid is not None and hasattr(uuid, "AsString"):
        # One physical item can flash multiple copper layers; bind the layer in.
        layer = data[1] if len(data) > 1 else ""
        return f"{uuid.AsString()}:{layer}"
    serialized = json.dumps(data, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode("ascii")).hexdigest()


def _pad_shape_name(shape: Any, pcbnew_module: Any) -> str:
    """Map a KiCad pad shape onto the matching containment primitives."""
    circle = getattr(pcbnew_module, "PAD_SHAPE_CIRCLE", None)
    oval = getattr(pcbnew_module, "PAD_SHAPE_OVAL", None)
    if circle is not None and shape == circle:
        return "circle"
    if oval is not None and shape == oval:
        return "oval"
    return "rect"


def _pad_rotation_deg(pad: Any) -> float:
    """Read pad orientation in degrees for local-frame containment."""
    getter = getattr(pad, "GetOrientationDegrees", None)
    if callable(getter):
        return float(getter())
    orientation = getattr(pad, "GetOrientation", None)
    if callable(orientation):
        value = orientation()
        as_degrees = getattr(value, "AsDegrees", None)
        if callable(as_degrees):
            return float(as_degrees())
    raise BoardSnapshotError(
        "KiCad cannot read the pad orientation for copper coverage."
    )


def _item_on_layer(item: Any, layer_id: int) -> bool:
    """Require an actual native flash; layer membership alone cannot prove copper."""
    flash = getattr(item, "FlashLayer", None)
    if callable(flash):
        try:
            return bool(flash(layer_id))
        except _FLASH_READ_ERRORS:
            return False
    return False


def _copper_features(
    board: Any, pcbnew_module: Any, layers: dict[str, int]
) -> tuple[CopperFeature, ...]:
    """Collect pad and via copper flashes for covered-stub detection."""
    features: list[CopperFeature] = []
    for item in board.GetTracks():
        if not _is_via(item, pcbnew_module):
            continue
        net = str(item.GetNetname()) if hasattr(item, "GetNetname") else ""
        for layer_name, layer_id in layers.items():
            if not _item_on_layer(item, layer_id):
                continue
            try:
                # KiCad 10 padstacks may have different diameters on each layer.
                # The inherited no-argument width is not physical-layer evidence.
                width = int(item.GetWidth(layer_id))
                center = _point(item.GetPosition())
            except _FLASH_READ_ERRORS:
                continue
            if width <= 0:
                continue
            feature_id = _feature_id(item, ("via", layer_name, net, width, center))
            features.append(
                CopperFeature(
                    feature_id=feature_id,
                    net=net,
                    layer=layer_name,
                    kind="via",
                    shape="circle",
                    center=center,
                    size_x_nm=width,
                    size_y_nm=width,
                )
            )
    footprints = board.GetFootprints() if hasattr(board, "GetFootprints") else ()
    for footprint in footprints:
        pads = footprint.Pads() if hasattr(footprint, "Pads") else ()
        for pad in pads:
            net = str(pad.GetNetname()) if hasattr(pad, "GetNetname") else ""
            for layer_name, layer_id in layers.items():
                if not _item_on_layer(pad, layer_id):
                    continue
                try:
                    # No-argument size/shape are F.Cu compatibility getters.
                    # ShapePos includes the native layer offset and its rotation.
                    size = pad.GetSize(layer_id)
                    size_x, size_y = int(size.x), int(size.y)
                    center = _point(pad.ShapePos(layer_id))
                    shape = _pad_shape_name(pad.GetShape(layer_id), pcbnew_module)
                    rotation = _pad_rotation_deg(pad)
                except _FLASH_READ_ERRORS:
                    # Missing precise APIs cannot justify excluding any route.
                    continue
                if size_x <= 0 or size_y <= 0 or not math.isfinite(rotation):
                    continue
                feature_id = _feature_id(
                    pad,
                    ("pad", layer_name, net, shape, size_x, size_y, center, rotation),
                )
                features.append(
                    CopperFeature(
                        feature_id=feature_id,
                        net=net,
                        layer=layer_name,
                        kind="pad",
                        shape=shape,
                        center=center,
                        size_x_nm=size_x,
                        size_y_nm=size_y,
                        rotation_deg=rotation,
                    )
                )
    return tuple(sorted(features, key=lambda feature: feature.feature_id))


def sample_arc(
    start: Point, middle: Point, end: Point, width_nm: int
) -> tuple[Point, ...]:
    """Approximate a three-point arc, retaining endpoints and cardinal extrema."""
    bx, by = middle[0] - start[0], middle[1] - start[1]
    cx, cy = end[0] - start[0], end[1] - start[1]
    determinant = 2 * (bx * cy - by * cx)
    if determinant == 0:
        return (start, middle, end) if middle not in (start, end) else (start, end)
    center_x = ((bx * bx + by * by) * cy - (cx * cx + cy * cy) * by) / determinant
    center_y = (bx * (cx * cx + cy * cy) - cx * (bx * bx + by * by)) / determinant
    radius = math.hypot(center_x, center_y)
    angle_start = math.atan2(-center_y, -center_x)
    angle_mid = math.atan2(by - center_y, bx - center_x)
    angle_end = math.atan2(cy - center_y, cx - center_x)
    tau = 2 * math.pi
    ccw_sweep = (angle_end - angle_start) % tau
    midpoint_sweep = (angle_mid - angle_start) % tau
    sweep = ccw_sweep if midpoint_sweep <= ccw_sweep else ccw_sweep - tau
    error_nm = max(1.0, min(2_000.0, width_nm / 20))
    step = 2 * math.acos(max(-1.0, 1 - min(error_nm / radius, 1)))
    count = max(1, math.ceil(abs(sweep) / max(step, 1e-9)))
    if count > 65_536:
        raise BoardSnapshotError("An arc is too large to render accurately.")
    fractions = {index / count for index in range(count + 1)}
    for angle in (0, math.pi / 2, math.pi, 3 * math.pi / 2, angle_mid):
        distance = (
            (angle - angle_start) % tau if sweep > 0 else -((angle_start - angle) % tau)
        )
        fraction = distance / sweep
        if 0 <= fraction <= 1:
            fractions.add(fraction)
    result = [
        (
            round(
                start[0] + center_x + radius * math.cos(angle_start + fraction * sweep)
            ),
            round(
                start[1] + center_y + radius * math.sin(angle_start + fraction * sweep)
            ),
        )
        for fraction in sorted(fractions)
    ]
    result[0], result[-1] = start, end
    return tuple(dict.fromkeys(result))


def _trace_id(item: Any, data: tuple[Any, ...]) -> str:
    """Use a persistent item UUID, falling back to an exact geometry digest."""
    uuid = getattr(item, "m_Uuid", None)
    if uuid is not None and hasattr(uuid, "AsString"):
        return str(uuid.AsString())
    serialized = json.dumps(data, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode("ascii")).hexdigest()


def _without_fill_cache(serialized: str) -> str:
    """Exclude generated fills and plot preferences, retaining PCB definitions."""
    result = []
    position = 0
    quoted = False
    escaped = False
    while position < len(serialized):
        character = serialized[position]
        if character == "(" and not quoted:
            fill_state = _FILL_STATE_TOKEN.match(serialized, position)
            if fill_state is not None:
                # KiCad emits the bare yes from ZONE.IsFilled(), independently
                # of the nested fill-mode/thermal settings that define copper.
                result.append("(fill")
                position = fill_state.end()
                continue
            token = _FILL_CACHE_TOKEN.match(serialized, position)
            if token is not None:
                depth, end = 1, position + 1
                nested_quoted, nested_escaped = False, False
                while depth and end < len(serialized):
                    nested = serialized[end]
                    if nested == '"' and not nested_escaped:
                        nested_quoted = not nested_quoted
                    if not nested_quoted:
                        depth += (nested == "(") - (nested == ")")
                    nested_escaped = nested == "\\" and not nested_escaped
                    end += 1
                if depth:
                    raise BoardSnapshotError(
                        "KiCad returned an incomplete zone record."
                    )
                position = end
                continue
        if character == '"' and not escaped:
            quoted = not quoted
        escaped = character == "\\" and not escaped
        result.append(character)
        position += 1
    # Whitespace outside quoted strings separates tokens only; removed cache
    # records must not leave a changing number of blank lines in the digest.
    tokens = re.findall(r'"(?:\\.|[^"\\])*"|[()]|[^\s()"]+', "".join(result))
    normalized = []
    previous = ""
    for token in tokens:
        if previous and previous != "(" and token != ")":
            normalized.append(" ")
        normalized.append(token)
        previous = token
    return "".join(normalized)


def _board_settings_records(board: Any, pcbnew_module: Any) -> list[str]:
    """Fingerprint a detached settings board; full formatting can update fonts."""
    io_class = getattr(pcbnew_module, "PCB_IO_KICAD_SEXPR", None)
    output_class = getattr(pcbnew_module, "STRING_FORMATTER", None)
    if (
        io_class is None
        or output_class is None
        or not hasattr(board, "GetDesignSettings")
        or not hasattr(io_class, "FormatBoardToFormatter")
    ):
        if hasattr(board, "GetDesignSettings"):
            settings = board.GetDesignSettings()
            if hasattr(settings, "GetBoardThickness"):
                return [f"board-thickness:{int(settings.GetBoardThickness())}"]
        return []
    try:
        with empty_board(pcbnew_module) as settings_board:
            settings_board.SetDesignSettings(board.GetDesignSettings())
            enabled_layers = board.GetEnabledLayers()
            settings_board.SetEnabledLayers(enabled_layers)
            # Match the settings copied for rendering. Title/property substitutions
            # can change visible PCB text without changing a drawing's raw record.
            for suffix in ("PageSettings", "TitleBlock", "Properties"):
                getter = getattr(board, "Get" + suffix, None)
                if callable(getter):
                    getattr(settings_board, "Set" + suffix)(getter())
            for suffix in ("LayerName", "LayerType"):
                getter = getattr(board, "Get" + suffix, None)
                if callable(getter):
                    setter = getattr(settings_board, "Set" + suffix)
                    for layer_id in enabled_layers.Seq():
                        setter(layer_id, getter(layer_id))
            output = output_class()
            formatter = io_class()
            formatter.FormatBoardToFormatter(output, settings_board)
            serialized = str(output.GetString())
    except BoardCopyError as error:
        raise BoardSnapshotError(str(error)) from error
    except Exception as error:
        raise BoardSnapshotError(
            f"Cannot fingerprint rendered board settings: {error}"
        ) from error
    depth, start = 0, 0
    quoted, escaped = False, False
    records = []
    for index, character in enumerate(serialized):
        if character == '"' and not escaped:
            quoted = not quoted
        if not quoted:
            if character == "(":
                if depth == 1:
                    start = index
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 1:
                    token = _SEXPR_NAME.match(serialized, start)
                    if token is not None and token.group(1) in {
                        "general",
                        "layers",
                        "paper",
                        "title_block",
                        "property",
                        "setup",
                    }:
                        records.append(
                            _without_fill_cache(serialized[start : index + 1])
                        )
        escaped = character == "\\" and not escaped
    if depth != 0 or quoted:
        raise BoardSnapshotError("KiCad returned an incomplete board settings record.")
    return records


def _footprint_context_record(serialized: str) -> str:
    """Omit explicitly hidden fields without dropping native geometry or fills.

    Inspect only direct footprint property children. Quoted text containing
    ``(hide yes)`` and nested non-field data must remain literal source content.
    Canonical tokens keep removed fields from leaving whitespace in the hash.
    """
    root = _SEXPR_NAME.match(serialized.lstrip())
    if root is None or root.group(1) != "footprint":
        # Retain unfamiliar records in full rather than excluding unknown data.
        return serialized
    try:
        tokens = _record_tokens(serialized, "footprint")
    except BoardCopyError as error:
        raise BoardSnapshotError(str(error)) from error
    retained = []
    depth, position = 0, 0
    while position < len(tokens):
        token = tokens[position]
        if depth == 1 and tokens[position : position + 2] == ["(", "property"]:
            child_depth, end, hidden = 1, position + 1, False
            while child_depth:
                if child_depth == 1 and tokens[end : end + 4] == [
                    "(",
                    "hide",
                    "yes",
                    ")",
                ]:
                    hidden = True
                child_depth += (tokens[end] == "(") - (tokens[end] == ")")
                end += 1
            if not hidden:
                retained.extend(tokens[position:end])
            position = end
            continue
        retained.append(token)
        depth += (token == "(") - (token == ")")
        position += 1
    return "footprint-context:" + json.dumps(
        retained, ensure_ascii=True, separators=(",", ":")
    )


def _context_digest(board: Any, pcbnew_module: Any, vias: Iterable[Any]) -> str:
    """Hash rendered context, including fills, without changing the live board."""
    items = [(item, "") for item in vias]
    for method in ("GetFootprints", "GetDrawings", "Zones", "Groups", "Generators"):
        if hasattr(board, method):
            items.extend((item, method) for item in getattr(board, method)())
    records = _board_settings_records(board, pcbnew_module)
    try:
        records.extend(text_context_records(board, pcbnew_module))
    except TextContextError as error:
        raise BoardSnapshotError(str(error)) from error
    filename_getter = getattr(board, "GetFileName", None)
    if callable(filename_getter):
        filename = str(filename_getter())
        if filename:
            try:
                records.extend(project_context_records(Path(filename)))
            except ProjectContextError as error:
                raise BoardSnapshotError(str(error)) from error
    if not items:
        return hashlib.sha256(
            json.dumps(sorted(records), ensure_ascii=True).encode("ascii")
        ).hexdigest()
    formatter_class = getattr(pcbnew_module, "PCB_IO_KICAD_SEXPR", None)
    if formatter_class is None:
        formatter_class = getattr(pcbnew_module, "PCB_IO", None)
    formatter = formatter_class() if formatter_class is not None else None
    if formatter is None or not all(
        hasattr(formatter, name) for name in ("Format", "GetStringOutput")
    ):
        raise BoardSnapshotError(
            "This KiCad version cannot fingerprint PCB context for review."
        )
    for item, collection in items:
        formatter.Format(item)
        serialized = str(formatter.GetStringOutput(True))
        # Cached plots contain the actual filled polygons. Omitting them from
        # the source revision could reuse old pixels after a live zone refill.
        if collection == "GetFootprints":
            records.append(_footprint_context_record(serialized))
        elif collection == "Zones":
            records.append(serialized)
        else:
            records.append(_without_fill_cache(serialized))
        zones = (
            (item,)
            if collection == "Zones"
            else (
                tuple(item.Zones())
                if collection == "GetFootprints" and hasattr(item, "Zones")
                else ()
            )
        )
        for zone in zones:
            if collection == "Zones":
                zone_record = serialized
            else:
                formatter.Format(zone)
                zone_record = str(formatter.GetStringOutput(True))
            readiness = []
            for name in ("IsFilled", "NeedRefill"):
                getter = getattr(zone, name, None)
                if callable(getter):
                    readiness.append((name, bool(getter())))
            # Associate flags with this zone, not a sorted multiset that could
            # remain unchanged when two different zones swap readiness states.
            zone_digest = hashlib.sha256(zone_record.encode("utf-8")).hexdigest()
            records.append(json.dumps((zone_digest, readiness), ensure_ascii=True))
    return hashlib.sha256(
        json.dumps(sorted(records), ensure_ascii=True).encode("ascii")
    ).hexdigest()


def _board_bounds(board: Any) -> Optional[Bounds]:
    """Read the same physical outline used to frame native capture images."""
    getter = getattr(board, "GetBoardEdgesBoundingBox", None)
    if not callable(getter):
        return None
    box = getter()
    left, top = int(box.GetX()), int(box.GetY())
    right, bottom = left + int(box.GetWidth()), top + int(box.GetHeight())
    return (left, top, right, bottom) if right > left and bottom > top else None


def snapshot_board(
    board: Any, pcbnew_module: Any, *, netclass_source: Any = None
) -> BoardSnapshot:
    """Read board geometry, optionally resolving net classes from its live source.

    Detached plot boards can retain default net settings after project assignment.
    Read class metadata separately without sharing or replacing native settings;
    tracks, rendered context and differential pairing always come from ``board``.
    """
    layers = copper_layers(board, pcbnew_module)
    names_by_id = {value: name for name, value in layers.items()}
    traces = []
    vias = []
    for item in board.GetTracks():
        if _is_via(item, pcbnew_module):
            vias.append(item)
            continue
        layer = names_by_id.get(int(item.GetLayer()))
        if layer is None:
            continue
        width = int(item.GetWidth())
        start, end = _point(item.GetStart()), _point(item.GetEnd())
        is_native_arc = _is_arc(item, pcbnew_module)
        midpoint_getter = getattr(item, "GetMid", None)
        if is_native_arc and not callable(midpoint_getter):
            raise BoardSnapshotError("KiCad cannot read the native arc midpoint.")
        points = (
            sample_arc(start, _point(midpoint_getter()), end, width)
            if is_native_arc
            else (start, end)
        )
        net = str(item.GetNetname())
        traces.append(
            Trace(
                _trace_id(item, (layer, net, width, points)),
                layer,
                net,
                width,
                points,
                is_native_arc=is_native_arc,
            )
        )
    net_classes = netclass_context(
        board if netclass_source is None else netclass_source,
        (trace.net for trace in traces),
    )
    differential_pairs = pairing_context(board, (trace.net for trace in traces))
    return BoardSnapshot(
        tuple(layers),
        tuple(sorted(traces, key=lambda trace: trace.trace_id)),
        _context_digest(board, pcbnew_module, vias),
        net_classes.classes,
        net_classes.memberships,
        net_classes.digest,
        net_classes.error,
        differential_pairs.pairs,
        differential_pairs.error,
        _copper_features(board, pcbnew_module, layers),
        _board_bounds(board),
    )
