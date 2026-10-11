"""Parse EasyEDA's responses; this module makes no requests of its own.

The classic per-LCSC response carries two drawings.  ``result.dataStr`` is the
schematic symbol, per part, whose ``P~`` shapes carry pin numbers and optional
labels such as ``A`` and ``K``.  ``result.packageDetail.dataStr`` is the
footprint, per puuid, whose ``PAD~`` shapes carry the pad geometry.  Two parts
that share a footprint can carry different symbols, so pin meaning is per part.
The EasyEDA Pro host answers the batch device lookup with one record per known
part (symbol uuid, footprint uuid, package name) and the per-uuid endpoint with
one document, a footprint or a symbol, in the Pro text form (newline-delimited
JSON arrays).
"""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass, field
import json
from typing import Any, Optional

from .geometry import normal_number
from .records import SymbolPin

# SymbolPin lives in .records so that code judging a part can read it without this
# parser; every record below carries a list of them.


@dataclass
class ComponentRecord:
    """One part's EasyEDA data: the classic per-LCSC response, or the Pro host's three answers joined."""

    lcsc: str
    status: str  # 'ok' | 'none' | 'error'
    error: str = ""
    symbol_uuid: str = ""
    symbol_pins: list[SymbolPin] = field(default_factory=list)
    symbol_shapes: list[str] = field(default_factory=list)
    puuid: str = ""
    package_name: str = ""
    pads: list[dict] = field(
        default_factory=list
    )  # raw canvas units, origin subtracted
    footprint_shapes: list[str] = field(default_factory=list)
    footprint_source: str = ""  # 'component' | 'puuid-endpoint' | ''
    skipped_shapes: int = 0  # PAD~/P~ shapes that could not be read
    # The classic drawing's head x/y, subtracted from its pads; (0, 0) for Pro text,
    # whose coordinates are already about the footprint origin.
    footprint_origin: tuple[float, float] = (0.0, 0.0)


def classic_pin_records(shapes: list[Any]) -> list[tuple[str, str, float, float]]:
    """Return ``(number, label, x, y)`` per classic ``P~`` pin shape, unreadable ones skipped.

    Sections are separated by ``^^``.  Section 0 is
    ``P~show~locked~spice_number~x~y~rotation~gId~id``; section 3 is
    ``show~x~y~rotation~pin_name~alignment~~~color`` and section 4 is the displayed
    pin number in the same layout.  The displayed number is what the library draws
    next to the pin and what matches the footprint's pad numbers (the SPICE field
    disagrees on some symbols), so it is preferred when present.  A pin whose
    position is unreadable keeps the number and label with a position of 0, 0.
    """
    pins: list[tuple[str, str, float, float]] = []
    for shape in shapes:
        if not isinstance(shape, str) or not shape.startswith("P~"):
            continue
        sections = shape.split("^^")
        header = sections[0].split("~")
        if len(header) < 4:
            continue
        number = header[3].strip()
        label = ""
        if len(sections) >= 4:
            name_parts = sections[3].split("~")
            if len(name_parts) >= 5:
                label = name_parts[4].strip()
        if len(sections) >= 5:
            number_parts = sections[4].split("~")
            if len(number_parts) >= 5 and number_parts[4].strip():
                number = number_parts[4].strip()
        x = y = 0.0
        if len(header) >= 6:
            try:
                x, y = float(header[4]), float(header[5])
            except ValueError:
                x = y = 0.0
        pins.append((normal_number(number), label, x, y))
    return pins


def _numbered_pins(
    records: list[tuple[str, str, float, float]],
) -> tuple[list[SymbolPin], int]:
    """Return the pins of ``(number, label, x, y)`` records, plus the count without a number.

    A pin the symbol leaves unnumbered is skipped on purpose and counted instead: it
    cannot be matched to a pad, and the numbered pins of the symbol are unaffected.
    Both formats' pin readers keep this one rule.
    """
    pins: list[SymbolPin] = []
    skipped = 0
    for number, label, _x, _y in records:
        if not number:
            skipped += 1
            continue
        pins.append(SymbolPin(number=number, label=label))
    return pins, skipped


def parse_symbol_pins(shapes: list[Any]) -> tuple[list[SymbolPin], int]:
    """Return the symbol's pins from its ``P~`` shape strings, plus the count of skipped ones.

    A pin is skipped when its shape cannot be read or when it has no number
    (:func:`_numbered_pins`).
    """
    readable = classic_pin_records(shapes)
    unreadable = sum(
        1 for s in shapes if isinstance(s, str) and s.startswith("P~")
    ) - len(readable)
    pins, unnumbered = _numbered_pins(readable)
    return pins, unreadable + unnumbered


def parse_footprint_pads(
    shapes: list[Any], origin_x: float, origin_y: float
) -> tuple[list[dict], int]:
    """Return raw pad dicts from ``PAD~`` shapes (origin subtracted, canvas units) and the skipped count.

    Fields: 1 shape, 2 x, 3 y, 4 width, 5 height, 6 layer, 8 number, 9 hole radius,
    11 rotation.
    """
    pads: list[dict] = []
    skipped = 0
    for shape in shapes:
        if not isinstance(shape, str) or not shape.startswith("PAD~"):
            continue
        parts = shape.split("~")
        if len(parts) < 9:
            skipped += 1
            continue
        try:
            x, y, w, h = (float(parts[i]) for i in (2, 3, 4, 5))
        except ValueError:
            skipped += 1
            continue
        rotation = 0.0
        if len(parts) > 11 and parts[11]:
            try:
                rotation = float(parts[11])
            except ValueError:
                skipped += 1
                continue
        hole = 0.0
        if len(parts) > 9 and parts[9]:
            try:
                hole = float(parts[9])
            except ValueError:
                hole = 0.0
        pads.append(
            {
                "number": normal_number(parts[8].strip()),
                "x": x - origin_x,
                "y": y - origin_y,
                "w": w,
                "h": h,
                "rotation": rotation,
                "shape": parts[1],
                "layer": parts[6],
                "hole": hole,
            }
        )
    return pads, skipped


# 3D-model nodes are artwork, not geometry; the drawing reader needs only the drawing
# commands, and the nodes are half the bytes of a footprint.
_DROPPED_SHAPE_PREFIXES = ("SVGNODE~",)

# The code of a ``success: false`` body that means "no such component" rather than a
# server problem.  The classic per-part endpoint and both per-uuid endpoints answer a
# part or document they do not have with HTTP 200 and
# ``{"success": false, "code": 404, "message": "Component not found"}``.  A refusal
# with no code, or with code 0 (the code EasyEDA puts on its successful answers),
# is not that answer, so it is an error and asked again.
_NOT_FOUND_CODES = frozenset({404, "404"})


def _dict(value: Any) -> dict:
    """Return ``value`` when it is a dict, else an empty dict (malformed input never raises)."""
    return value if isinstance(value, dict) else {}


def _decode_data_str(value: Any) -> dict:
    """Return a drawing's ``dataStr`` as a dict, decoding a JSON string when EasyEDA sends one."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return _dict(value)


def _shape_list(container: dict) -> list[str]:
    """Return the string entries of a ``shape`` array, without 3D-model nodes."""
    shapes = container.get("shape") if isinstance(container.get("shape"), list) else []
    return [
        s
        for s in shapes
        if isinstance(s, str) and not s.startswith(_DROPPED_SHAPE_PREFIXES)
    ]


def _classify_body(body: Any) -> tuple[str, Optional[str], Any]:
    """Classify a response body the way all four parsers open.

    Returns ``(status, error, result)``, by the body's ``success`` flag:

    - the boolean True: an answer.  ``status`` is ``"ok"``, ``error`` is None and
      ``result`` is ``body.get("result")`` exactly as the body carried it,
      unvalidated.  What counts as a usable result differs by endpoint (a
      non-empty dict for the three per-part parsers, a list of devices for the
      batch lookup), so that check stays with each caller.
    - the boolean False: EasyEDA's refusal.  With EasyEDA's not-found code (404) it
      is ``"none"``, EasyEDA having nothing for the part; no code, or any other
      code, is ``"error"``, so it is asked again later.  ``error`` is the code and
      message.
    - missing, or not a boolean (the string ``"false"``, say), or a body that is
      not a JSON object: ``"error"`` with the reason, so it is asked again rather
      than taken as EasyEDA's answer.
    """
    if not isinstance(body, dict):
        return "error", "response is not a JSON object", None
    success = body.get("success")
    if success is True:
        return "ok", None, body.get("result")
    if success is False:
        code = body.get("code")
        detail = f"code {code}: {body.get('message', '')}".strip(": ")
        return ("none" if _is_not_found(code) else "error"), detail, None
    return (
        "error",
        f"response has no boolean success flag (success is {success!r})",
        None,
    )


def _is_not_found(code: Any) -> bool:
    """Return True when a ``success: false`` code means "no such component".

    A JSON code can be a list or a dict.  Neither is a not-found code, even one
    holding 404, and neither can be looked up in a frozenset, so only a hashable
    code is looked up.
    """
    return isinstance(code, Hashable) and code in _NOT_FOUND_CODES


# Why a classic footprint whose head gives no origin is an error.
_NO_ORIGIN = "footprint origin (head x and y) missing or unreadable"


def _classic_origin(head: dict) -> Optional[tuple[float, float]]:
    """Return a classic footprint's origin, its head's x and y in canvas units, or None.

    Shared by the per-LCSC response's packageDetail and the per-uuid endpoint's
    older classic form, the only two places a classic footprint's head is read.
    The pads are read relative to it, so without a readable x and y they would
    sit about a metre from where they belong: the caller makes the record an
    error, asked again, rather than an answer.
    """
    try:
        return float(head["x"]), float(head["y"])
    except (KeyError, TypeError, ValueError):
        return None


def parse_component_response(body: Any, lcsc: str) -> ComponentRecord:
    """Classify and parse one per-LCSC response body.

    ``success: true`` with a null or empty ``result`` means EasyEDA has no component
    for the part (the checkerboard case): status ``none``.  ``success: false`` is
    ``none`` only when it carries EasyEDA's not-found code (404); no code, or any
    other code, is an ``error`` with the code and message kept, so it is asked again rather than
    taken as EasyEDA's answer.  A body without a boolean ``success`` flag, and
    anything malformed, is ``error`` and never raises.  A record without a puuid is
    also ``error``.
    """
    status, error, result = _classify_body(body)
    if status != "ok":
        return ComponentRecord(lcsc=lcsc, status=status, error=error or "")
    if result in (None, {}, [], ""):
        return ComponentRecord(lcsc=lcsc, status="none")
    if not isinstance(result, dict):
        return ComponentRecord(
            lcsc=lcsc, status="error", error="result is not an object"
        )

    data_str = _decode_data_str(result.get("dataStr"))
    head = _dict(data_str.get("head"))
    c_para = _dict(head.get("c_para"))

    record = ComponentRecord(
        lcsc=lcsc,
        status="ok",
        symbol_uuid=str(head.get("uuid") or result.get("uuid") or ""),
        symbol_shapes=_shape_list(data_str),
        puuid=str(head.get("puuid") or ""),
    )
    record.symbol_pins, skipped_pins = parse_symbol_pins(record.symbol_shapes)
    record.skipped_shapes += skipped_pins

    package = _dict(result.get("packageDetail"))
    package_data = _decode_data_str(package.get("dataStr"))
    if package_data:
        package_head = _dict(package_data.get("head"))
        record.package_name = str(
            package.get("title")
            or _dict(package_head.get("c_para")).get("package")
            or c_para.get("package")
            or ""
        )
        record.puuid = record.puuid or str(package.get("uuid") or "")
        record.footprint_shapes = _shape_list(package_data)
        record.footprint_source = "component"
        origin = _classic_origin(package_head)
        if origin is None and record.footprint_shapes:
            record.status = "error"
            record.error = record.error or _NO_ORIGIN
        elif origin is not None:
            record.pads, skipped_pads = parse_footprint_pads(
                record.footprint_shapes, *origin
            )
            record.footprint_origin = origin
            record.skipped_shapes += skipped_pads
        if not record.pads:
            # The footprint is there but cannot be read: asked again, as the per-uuid
            # parser treats a footprint without readable pads.
            record.status = "error"
            record.error = record.error or "packageDetail without readable pads"
    else:
        record.package_name = str(c_para.get("package") or "")
        record.status = "error"
        record.error = record.error or "no packageDetail in the response"

    if not record.puuid:
        record.status = "error"
        record.error = "no puuid in response"
    return record


# ---------------------------------------------------------------------------
# EasyEDA Pro footprint text, as the per-uuid endpoint serves it
# ---------------------------------------------------------------------------

# Pro footprint text is in mils with Y up; the classic canvas unit is 10 mil with Y
# down.  Pro pads are converted to classic canvas units when parsed, so every consumer
# sees one raw pad form: C2132's footprint gives the same pads in both formats
# (tests/test_jlcfootprint_pro_format.py).
PRO_MILS_PER_CANVAS_UNIT = 10.0


@dataclass
class FootprintRecord:
    """One footprint on its own, as the per-uuid endpoint serves it.

    ``pads`` use the same raw form as ``ComponentRecord.pads``: classic canvas units,
    Y down, origin subtracted.
    """

    puuid: str
    status: str = "error"  # 'ok' | 'none' | 'error'
    error: str = ""
    package_name: str = ""
    pads: list[dict] = field(default_factory=list)
    footprint_shapes: list[str] = field(default_factory=list)
    footprint_source: str = "puuid-endpoint"
    skipped_shapes: int = 0
    footprint_origin: tuple[float, float] = (
        0.0,
        0.0,
    )  # classic head x/y; (0, 0) for Pro


def pro_shape_lines(text: str) -> list[str]:
    """Split Pro footprint text into its non-empty record lines."""
    return [line.strip() for line in text.split("\n") if line.strip()]


# The commands that open an arc in a Pro point list.
PRO_ARC_TOKENS = ("A", "CA", "ARC")


def pro_points_straight(points: Any) -> bool:
    """Return True for a Pro point list of straight segments only.

    Such a list holds coordinates with the letter ``L`` between segments.  Any
    other command carries numbers that are not all points: an arc (``ARC``, ``A``
    or ``CA``) its angle, a ``CIRCLE`` its radius, and a command this reader does
    not know is taken the same way.  A box of such a list's numbers would be
    wrong, so a list that is not straight is not boxed.
    """
    return isinstance(points, list) and all(
        (isinstance(value, str) and value.upper() == "L")
        or (isinstance(value, (int, float)) and not isinstance(value, bool))
        for value in points
    )


def _poly_extent(points: Any) -> tuple[float, float]:
    """Return the width and height of a Pro ``POLY`` outline's bounding box in canvas units.

    The outline lists coordinates in mils; the numbers are read in x/y pairs.  An
    outline that is not straight segments only (:func:`pro_points_straight`) is
    unreadable, as a body outline with one is to the drawing reader: an arc's
    angle sits among the coordinates, and a box of its end points would leave out
    the bulge.
    """
    if not pro_points_straight(points):
        raise ValueError("polygon pad outline is not straight segments")
    numbers = [float(value) for value in points if not isinstance(value, str)]
    if len(numbers) < 4:
        raise ValueError("polygon pad without an outline")
    xs = numbers[0::2]
    ys = numbers[1::2]
    return (
        (max(xs) - min(xs)) / PRO_MILS_PER_CANVAS_UNIT,
        (max(ys) - min(ys)) / PRO_MILS_PER_CANVAS_UNIT,
    )


def parse_pro_pads(lines: list[str]) -> tuple[list[dict], int]:
    """Return raw pads from Pro ``["PAD", ...]`` records, plus the count of unreadable ones.

    A record is ``["PAD", id, net, "", layer, number, x, y, rotation, hole, [shape,
    width, height, ...], ...]`` in mils with Y up.  A ``POLY`` shape carries its
    outline as absolute points instead of a size; its bounding box stands in for
    the size (a merged connector pad, for instance) with no rotation of its own.
    The result is in classic canvas units with Y down, the form
    :func:`jlcfootprint.geometry.easyeda_pads_to_mm` converts; the hole is kept as
    a radius like the classic ``PAD~`` field, and the pad number has its leading
    zeros stripped like a classic one's.
    """
    pads: list[dict] = []
    skipped = 0
    for line in lines:
        if not isinstance(line, str) or not line.startswith('["PAD"'):
            continue
        try:
            parts = json.loads(line)
            number = normal_number(str(parts[5]).strip())
            x = float(parts[6]) / PRO_MILS_PER_CANVAS_UNIT
            y = -float(parts[7]) / PRO_MILS_PER_CANVAS_UNIT
            rotation = float(parts[8] or 0.0)
            layer = str(parts[4])
            hole = parts[9]
            shape = parts[10]
            shape_name = str(shape[0])
            if shape_name.upper() == "POLY":
                width, height = _poly_extent(shape[1])
                rotation = 0.0
            else:
                width = float(shape[1]) / PRO_MILS_PER_CANVAS_UNIT
                height = float(shape[2]) / PRO_MILS_PER_CANVAS_UNIT
        except (ValueError, TypeError, IndexError, KeyError):
            skipped += 1
            continue
        hole_radius = 0.0
        if isinstance(hole, list) and len(hole) > 1:
            try:
                hole_radius = float(hole[1]) / PRO_MILS_PER_CANVAS_UNIT / 2.0
            except (ValueError, TypeError):
                hole_radius = 0.0
        pads.append(
            {
                "number": number,
                "x": x,
                "y": y,
                "w": width,
                "h": height,
                "rotation": rotation,
                "shape": shape_name,
                "layer": layer,
                "hole": hole_radius,
            }
        )
    return pads, skipped


def parse_puuid_response(body: Any, puuid: str) -> FootprintRecord:
    """Classify and parse one per-uuid response (``pro.easyeda.com/api/components/{puuid}``).

    The endpoint answers with ``result.dataStr`` as Pro text (newline-delimited JSON
    arrays) or, for older footprints, as the classic dict with ``head`` and ``shape``.
    Status rules follow :func:`parse_component_response`; malformed input never raises.
    """
    record = FootprintRecord(puuid=puuid)
    status, error, result = _classify_body(body)
    if status != "ok":
        record.status = status
        record.error = error or ""
        return record
    if result in (None, {}, [], ""):
        record.status = "none"
        return record
    if not isinstance(result, dict):
        record.error = "result is not an object"
        return record
    data_str = result.get("dataStr")
    record.package_name = str(result.get("display_title") or result.get("title") or "")
    if isinstance(data_str, str) and data_str.lstrip().startswith("["):
        record.footprint_shapes = pro_shape_lines(data_str)
        record.pads, record.skipped_shapes = parse_pro_pads(record.footprint_shapes)
    else:
        package_data = _decode_data_str(data_str)
        head = _dict(package_data.get("head"))
        record.package_name = record.package_name or str(
            _dict(head.get("c_para")).get("package") or ""
        )
        record.footprint_shapes = _shape_list(package_data)
        origin = _classic_origin(head)
        if origin is None and record.footprint_shapes:
            record.error = _NO_ORIGIN
            return record
        if origin is not None:
            record.pads, record.skipped_shapes = parse_footprint_pads(
                record.footprint_shapes, *origin
            )
            record.footprint_origin = origin
    if not record.pads:
        record.error = record.error or "no readable pads in the footprint"
        return record
    record.status = "ok"
    return record


# ----------------------------------------------------------------------
# EasyEDA Pro: the batch device lookup and the per-uuid symbol document
# ----------------------------------------------------------------------

DOCTYPE_SYMBOL = "SYMBOL"


@dataclass
class DeviceHit:
    """One part the batch lookup knows: its symbol uuid, footprint uuid and package name."""

    lcsc: str
    symbol_uuid: str
    puuid: str
    package_name: str


@dataclass
class DevicesResult:
    """What one ``searchByCodes`` answer says about the codes it was asked for.

    ``error`` is non-empty when the answer could not be read; nothing is a hit or a
    miss then, and the codes are to be asked again.
    """

    hits: dict[str, DeviceHit] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    error: str = ""


def parse_devices_response(body: Any, codes: list[str]) -> DevicesResult:
    """Read one batch answer; codes absent from a well-formed answer are misses.

    ``result`` is a list of device records, or a dict whose ``lists`` holds them.  A
    record names its part in ``product_code``, its footprint in ``footprint.uuid``
    (``attributes.Footprint`` says the same) and its symbol in ``attributes.Symbol``;
    ``footprint.display_title`` is the suffix-bearing package name.  A record with no
    footprint uuid is a miss: there is nothing to align.  An answer of any other
    shape (no list of devices, or an entry that is not a record) is an error for
    every code, so the codes are asked again instead of being taken as misses.
    Never raises.
    """
    result = DevicesResult()
    status, error, raw = _classify_body(body)
    if status != "ok":
        # The batch lookup has no "none" to reach: a refusal of any code, like any
        # unreadable answer, says nothing about the codes it was asked for.
        result.error = error or "unreadable answer"
        return result
    items = raw.get("lists") if isinstance(raw, dict) else raw
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        result.error = "result is not a list of devices"
        return result
    for item in items:
        lcsc = str(item.get("product_code") or "").strip()
        footprint = _dict(item.get("footprint"))
        attributes = _dict(item.get("attributes"))
        puuid = str(footprint.get("uuid") or attributes.get("Footprint") or "").strip()
        if not lcsc or not puuid:
            continue
        result.hits[lcsc] = DeviceHit(
            lcsc=lcsc,
            symbol_uuid=str(attributes.get("Symbol") or "").strip(),
            puuid=puuid,
            package_name=str(
                footprint.get("display_title") or footprint.get("title") or ""
            ).strip(),
        )
    result.missing = [code for code in codes if code not in result.hits]
    return result


@dataclass
class SymbolRecord:
    """One schematic symbol on its own: what the per-uuid endpoint yields for a symbol uuid."""

    uuid: str
    status: str = "error"  # 'ok' | 'none' | 'error'
    error: str = ""
    title: str = ""
    pins: list[SymbolPin] = field(default_factory=list)
    shapes: list[str] = field(default_factory=list)  # the Pro record lines
    skipped_pins: int = 0  # PIN records without a displayed number


def pro_doctype(lines: list[str]) -> str:
    """Return the document kind named by the first ``["DOCTYPE", kind, ...]`` record, or ''."""
    for line in lines:
        if not isinstance(line, str) or not line.startswith('["DOCTYPE"'):
            continue
        try:
            parts = json.loads(line)
        except ValueError:
            return ""
        return str(parts[1]).upper() if len(parts) > 1 else ""
    return ""


def pro_pin_records(lines: list[Any]) -> list[tuple[str, str, float, float]]:
    """Return ``(number, label, x, y)`` per Pro ``PIN`` record, in record order.

    A pin is a ``["PIN", id, ?, ?, x, y, ...]`` record.  Its displayed name and
    number are the attribute records ``["ATTR", attrId, pinId, "NAME", text, ...]``
    and ``["ATTR", attrId, pinId, "NUMBER", text, ...]``; the number is what matches
    the footprint's pad numbers, leading zeros stripped by :func:`normal_number` so a
    Pro ``01`` and a classic ``1`` are one pad.  A pin without a number is returned
    with ``""``; :func:`parse_pro_pins` and :func:`jlcfootprint.drawing.symbol_pins`
    drop those, since a pin with no number names no pad.
    """
    order: list[str] = []
    positions: dict[str, tuple[float, float]] = {}
    names: dict[str, str] = {}
    numbers: dict[str, str] = {}
    for line in lines:
        if isinstance(line, list):
            parts: Any = line
        elif isinstance(line, str) and line.startswith(('["PIN"', '["ATTR"')):
            try:
                parts = json.loads(line)
            except ValueError:
                continue
        else:
            continue
        if not isinstance(parts, list) or len(parts) < 2:
            continue
        if parts[0] == "PIN":
            pin_id = str(parts[1])
            order.append(pin_id)
            try:
                positions[pin_id] = (float(parts[4]), float(parts[5]))
            except (IndexError, TypeError, ValueError):
                positions[pin_id] = (0.0, 0.0)
        elif parts[0] == "ATTR" and len(parts) >= 5 and parts[3] in ("NAME", "NUMBER"):
            value = "" if parts[4] is None else str(parts[4]).strip()
            if parts[3] == "NUMBER":
                names_or_numbers, value = numbers, normal_number(value)
            else:
                names_or_numbers = names
            names_or_numbers[str(parts[2])] = value
    return [
        (numbers.get(pin_id, ""), names.get(pin_id, ""), *positions[pin_id])
        for pin_id in order
    ]


def parse_pro_pins(lines: list[str]) -> tuple[list[SymbolPin], int]:
    """Return the pins of Pro symbol text, plus the count of pins without a number.

    Pins come out in record order; see :func:`pro_pin_records` for the records and
    :func:`_numbered_pins` for the pins without a number.
    """
    return _numbered_pins(pro_pin_records(lines))


def parse_symbol_response(body: Any, uuid: str) -> SymbolRecord:
    """Classify and parse one per-uuid answer for a symbol uuid; never raises.

    Status rules follow :func:`parse_puuid_response`.  A body whose document is a
    footprint, is not in the Pro text form or has no readable pin reads as ``error``:
    the uuid names no usable symbol.
    """
    record = SymbolRecord(uuid=uuid)
    status, error, result = _classify_body(body)
    if status != "ok":
        record.status = status
        record.error = error or ""
        return record
    if result in (None, {}, [], ""):
        record.status = "none"
        return record
    if not isinstance(result, dict):
        record.error = "result is not an object"
        return record
    record.title = str(result.get("display_title") or result.get("title") or "")
    data_str = result.get("dataStr")
    if not isinstance(data_str, str) or not data_str.lstrip().startswith("["):
        record.error = "symbol is not in the Pro text form"
        return record
    record.shapes = pro_shape_lines(data_str)
    doctype = pro_doctype(record.shapes)
    if doctype and doctype != DOCTYPE_SYMBOL:
        record.error = f"uuid names a {doctype.lower()}, not a symbol"
        return record
    record.pins, record.skipped_pins = parse_pro_pins(record.shapes)
    if not record.pins:
        record.error = "no readable pins in the symbol"
        return record
    record.status = "ok"
    return record


def assemble_record(
    lcsc: str,
    hit: DeviceHit,
    footprint: FootprintRecord,
    symbol: Optional[SymbolRecord],
) -> ComponentRecord:
    """Join a batch hit and its footprint and symbol documents into one record.

    It is the same join whether the three came from the network or from recorded
    answers.  ``hit`` names the uuids and the batch lookup's package name.
    ``footprint`` is the parsed per-uuid footprint document, and the record takes
    its status, its error text and its count of unreadable shapes, whatever they
    are: a caller that only wants an ``ok`` part checks that itself.  ``symbol`` is
    the parsed per-uuid symbol document, or None when the part names no symbol
    uuid or none was fetched for it; a symbol whose own status is not ``ok``
    leaves the record without pins, the same as no symbol at all.
    """
    record = ComponentRecord(
        lcsc=lcsc,
        status=footprint.status,
        error=footprint.error,
        symbol_uuid=hit.symbol_uuid,
        puuid=hit.puuid,
        package_name=footprint.package_name or hit.package_name,
        pads=footprint.pads,
        footprint_shapes=footprint.footprint_shapes,
        footprint_source="puuid-endpoint",
        footprint_origin=footprint.footprint_origin,
        skipped_shapes=footprint.skipped_shapes,
    )
    if symbol is not None and symbol.status == "ok":
        record.symbol_pins = symbol.pins
        record.symbol_shapes = symbol.shapes
    return record
