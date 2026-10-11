"""A minimal parser for the footprints in KiCad board and footprint files, without pcbnew.

The plugin reads a board's pads through pcbnew.  This parser is for the
development tools and the tests, which read boards (``.kicad_pcb``) and KiCad's
library footprints (``.kicad_mod``) from plain Python.

It extracts footprint data only and skips tracks, zones, nets and every other
board-level construct.  Python 3.9 compatible, stdlib only.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
import re
from typing import Optional

from .geometry import Pad, normal_number

# The plugin's part-number field names (part_assignments.is_assignment_alias): LCSC,
# JLC or JLCPCB, then optionally Part, Part Number, Part Num, Part No, Part Nr, PN,
# Number, Code or ID, read with case, spaces and punctuation ignored.  Other names that
# start with one of them, such as "JLCPCB Rotation", are not part numbers.
_LCSC_FIELD_NAME = re.compile(
    r"(?:lcsc|jlcpcb|jlc)(?:part(?:number|num|no|nr)?|pn|number|code|id)?"
)
_PART_NUMBER = re.compile(r"C\d+")


def is_lcsc_field(name: str) -> bool:
    """Return True for a field name the plugin reads a part number from."""
    return bool(_LCSC_FIELD_NAME.fullmatch(re.sub(r"[^a-z0-9]", "", name.casefold())))


def lcsc_assignment(fields: Iterable[tuple[str, str]]) -> str:
    """Return the LCSC part number a footprint's fields assign, or "" for none.

    The same reading as the plugin's default assignment
    (``part_assignments.resolve_assignment``), so a board file gives the number the
    plugin reads from the live board.  A repeated
    name keeps its last text.  The part-number fields are taken in order, ``LCSC``
    first and then by name; their texts are read stripped and upper-cased.  Fields
    naming two different numbers are a conflict, and so is an empty first field
    beside a filled one; either gives "".  Otherwise the first field's text is the
    number when it is C and ASCII digits, and "" when it is not.
    """
    named = {str(name): "" if text is None else str(text) for name, text in fields}
    names = sorted(
        (name for name in named if is_lcsc_field(name)),
        key=lambda name: (name.casefold() != "lcsc", name.casefold(), name),
    )
    if not names:
        return ""
    texts = [named[name] for name in names]
    numbers = [text.strip().upper() for text in texts]
    if len({number for number in numbers if number}) > 1 or (
        texts[0] == "" and any(numbers)
    ):
        return ""
    if _PART_NUMBER.fullmatch(numbers[0]) and numbers[0].isascii():
        return numbers[0]
    return ""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class KiCadPad:
    """One pad as stored in the file: local position, size, absolute angle, pin function."""

    number: str  # pad number as string, e.g. "1", "A1"; "" for an unnumbered pad
    x: float  # footprint-local x in mm (pre-rotation of the placed footprint)
    y: float  # footprint-local y in mm
    width: float  # pad width in mm
    height: float  # pad height in mm
    rotation: float = 0.0  # absolute angle from the file, degrees
    shape: str = ""  # 'roundrect', 'rect', 'oval', 'circle', 'custom', ...
    pin_function: str = ""  # from (pinfunction "K"), "" when absent


@dataclass
class KiCadFootprint:
    """One placed footprint: reference, library id, placement, side, LCSC field and pads."""

    reference: str  # e.g. "Q2"
    footprint_name: str  # the library+name from (footprint "Lib:Name" …)
    value: str  # the Value property
    placed_x: float  # board position in mm
    placed_y: float
    placed_rotation: float  # the footprint-level rotation in degrees
    layer: str  # e.g. "F.Cu" (top) or "B.Cu" (bottom)
    lcsc: Optional[str]  # the part number lcsc_assignment reads, None without one
    pads: list[KiCadPad] = field(default_factory=list)
    # The courtyard's box in the footprint frame (mm), None without a courtyard.
    courtyard: Optional[tuple[float, float, float, float]] = None

    @property
    def is_bottom(self) -> bool:
        """Return True when the footprint sits on the back copper layer."""
        return self.layer.startswith("B.")


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

# Tokenize into: '(', ')', quoted strings (content only), atoms
_TOKEN_RE = re.compile(
    r"#[^\n]*"  # line comment (discard)
    r'|"(?:[^"\\]|\\.)*"'  # quoted string (keep quotes for detection)
    r"|[()]"  # paren
    r'|[^\s()"#]+'  # atom
)


def _tokenize(text: str) -> list[str]:
    return [
        m.group() for m in _TOKEN_RE.finditer(text) if not m.group().startswith("#")
    ]


def _unescape(s: str) -> str:
    """Strip surrounding quotes and process escape sequences."""
    inner = s[1:-1]  # remove leading/trailing "
    # Handle \" and \\ — only escapes KiCad actually uses
    inner = inner.replace('\\"', '"').replace("\\\\", "\\")
    return inner


# ---------------------------------------------------------------------------
# S-expression parser
# ---------------------------------------------------------------------------


def _parse_tokens(tokens: list[str], pos: int = 0) -> tuple[object, int]:
    """Recursively parse tokens into nested lists.

    Returns (node, next_pos) where node is either a str (atom/quoted) or
    a list representing an s-expr.
    """
    tok = tokens[pos]
    if tok == "(":
        pos += 1
        lst: list[object] = []
        while tokens[pos] != ")":
            child, pos = _parse_tokens(tokens, pos)
            lst.append(child)
        return lst, pos + 1  # consume ')'
    elif tok == ")":
        raise ValueError(f"Unexpected ')' at position {pos}")
    elif tok.startswith('"'):
        return _unescape(tok), pos + 1
    else:
        return tok, pos + 1


def _parse_sexp(text: str) -> list[object]:
    """Parse the entire file as a list of top-level s-expressions."""
    tokens = _tokenize(text)
    if not tokens:
        return []
    # The .kicad_pcb file is a single top-level s-expression
    result, _ = _parse_tokens(tokens, 0)
    return result  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Tree-walking helpers
# ---------------------------------------------------------------------------


def _tag(node: object) -> Optional[str]:
    """Return the tag (first atom element) of a list node, or None."""
    if isinstance(node, list) and node and isinstance(node[0], str):
        return node[0]
    return None


def _children(node: object) -> list[object]:
    """Return child nodes (all elements except the tag)."""
    if isinstance(node, list):
        return node[1:]
    return []


def _find_direct(node: object, tag: str) -> Optional[list[object]]:
    """Find the first direct child list with the given tag."""
    for child in _children(node):
        if isinstance(child, list) and _tag(child) == tag:
            return child  # type: ignore[return-value]
    return None


def _find_all_direct(node: object, tag: str) -> list[list[object]]:
    """Find all direct child lists with the given tag."""
    return [
        child
        for child in _children(node)  # type: ignore[misc]
        if isinstance(child, list) and _tag(child) == tag
    ]


def _str_arg(node: object, idx: int = 1) -> Optional[str]:
    """Return the idx-th element as str, or None if missing/wrong type."""
    if isinstance(node, list) and len(node) > idx:
        v = node[idx]
        if isinstance(v, str):
            return v
    return None


def _float_arg(node: object, idx: int = 1) -> Optional[float]:
    """Return the idx-th element as float, or None if missing/wrong type."""
    if isinstance(node, list) and len(node) > idx:
        v = node[idx]
        if isinstance(v, str):
            try:
                return float(v)
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------------------
# Footprint extraction
# ---------------------------------------------------------------------------


def _is_soldered(pad_node: list[object]) -> bool:
    """Return True for a pad that is soldered: on a copper layer and not an NPTH hole.

    This is the rule the plugin applies when it reads a board through pcbnew, so a
    board file and the live board give the same set of pads: a numbered NPTH hole
    and a paste-only aperture are left out, and an unnumbered copper pad (a
    thermal or shield tab) is kept.  Its number is then ``""``, and the alignment,
    which pairs pads by name, leaves it out; the pad-box centre counts it.  That
    centre is taken from each pad's position and size, so it is the centre of
    pcbnew's pad bounding box except where the copper is not the pad's size at its
    position: a custom pad's extra primitives, copper offset from its drill, or a
    pad turned by an angle that is not a multiple of 90 degrees.
    """
    if len(pad_node) > 2 and pad_node[2] == "np_thru_hole":
        return False
    layers = _find_direct(pad_node, "layers")
    return any(
        isinstance(layer, str) and layer.endswith(".Cu") for layer in _children(layers)
    )


def _parse_pad(pad_node: list[object]) -> Optional[KiCadPad]:
    """Parse a (pad ...) node into a KiCadPad, or None if it should be skipped."""
    # pad_node[0] = "pad", [1] = number, [2] = type, [3] = shape, ...
    if len(pad_node) < 4:
        return None

    number = pad_node[1]
    if not isinstance(number, str) or not _is_soldered(pad_node):
        return None
    # Leading zeros go as they do from EasyEDA's pad numbers, so 01 on both sides pairs.
    number = normal_number(number)

    at_node = _find_direct(pad_node, "at")
    if at_node is None:
        return None
    px = _float_arg(at_node, 1)
    py = _float_arg(at_node, 2)
    if px is None or py is None:
        return None

    size_node = _find_direct(pad_node, "size")
    if size_node is None:
        return None
    pw = _float_arg(size_node, 1)
    ph = _float_arg(size_node, 2)
    if pw is None or ph is None:
        return None

    rotation = 0.0
    if len(at_node) > 3:
        parsed_rotation = _float_arg(at_node, 3)
        if parsed_rotation is not None:
            rotation = parsed_rotation

    pin_function = ""
    function_node = _find_direct(pad_node, "pinfunction")
    if function_node is not None:
        pin_function = _str_arg(function_node, 1) or ""

    shape = pad_node[3] if len(pad_node) > 3 and isinstance(pad_node[3], str) else ""
    return KiCadPad(
        number=number,
        x=px,
        y=py,
        width=pw,
        height=ph,
        rotation=rotation,
        pin_function=pin_function,
        shape=shape,
    )


_COURTYARD_LAYERS = ("F.CrtYd", "B.CrtYd")
_GRAPHIC_TAGS = ("fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly")


def _courtyard_box(
    fp_node: list[object],
) -> Optional[tuple[float, float, float, float]]:
    """Return the box of the footprint's courtyard graphics (their centrelines), or None.

    Lines, rectangles and polygons contribute their points, arcs their three
    points and circles their full extent; an arc's bulge beyond its points is
    ignored, which errs on the small side.
    """
    xs: list[float] = []
    ys: list[float] = []
    for child in _children(fp_node):
        tag = _tag(child)
        if tag not in _GRAPHIC_TAGS:
            continue
        layer_node = _find_direct(child, "layer")
        if layer_node is None or _str_arg(layer_node) not in _COURTYARD_LAYERS:
            continue
        if tag == "fp_circle":
            centre = _find_direct(child, "center")
            end = _find_direct(child, "end")
            cx, cy = _float_arg(centre, 1), _float_arg(centre, 2)
            ex, ey = _float_arg(end, 1), _float_arg(end, 2)
            if None in (cx, cy, ex, ey):
                continue
            radius = ((ex - cx) ** 2 + (ey - cy) ** 2) ** 0.5
            xs.extend((cx - radius, cx + radius))
            ys.extend((cy - radius, cy + radius))
            continue
        points: list[list[object]] = []
        for key in ("start", "mid", "end"):
            node = _find_direct(child, key)
            if node is not None:
                points.append(node)
        pts = _find_direct(child, "pts")
        if pts is not None:
            points.extend(_find_all_direct(pts, "xy"))
        for node in points:
            x, y = _float_arg(node, 1), _float_arg(node, 2)
            if x is not None and y is not None:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _parse_footprint(fp_node: list[object]) -> Optional[KiCadFootprint]:
    """Parse a (footprint ...) node into a KiCadFootprint."""
    # fp_node[0] = "footprint", fp_node[1] = library+name string
    if len(fp_node) < 2:
        return None

    footprint_name = fp_node[1]
    if not isinstance(footprint_name, str):
        return None

    # Layer — first direct (layer ...) child
    layer_node = _find_direct(fp_node, "layer")
    layer = _str_arg(layer_node) if layer_node is not None else "F.Cu"
    if layer is None:
        layer = "F.Cu"

    # Position — first direct (at x y [rot]) child
    at_node = _find_direct(fp_node, "at")
    placed_x = _float_arg(at_node, 1) or 0.0
    placed_y = _float_arg(at_node, 2) or 0.0
    placed_rotation = 0.0
    if at_node is not None and len(at_node) > 3:
        r = _float_arg(at_node, 3)
        if r is not None:
            placed_rotation = r

    # Properties — iterate over all (property name value ...) children
    reference = ""
    value = ""
    properties: list[tuple[str, str]] = []

    for prop_node in _find_all_direct(fp_node, "property"):
        prop_name = _str_arg(prop_node, 1)
        prop_value = _str_arg(prop_node, 2)
        if prop_name == "Reference":
            reference = prop_value or ""
        elif prop_name == "Value":
            value = prop_value or ""
        elif prop_name:
            properties.append((prop_name, prop_value or ""))
    lcsc = lcsc_assignment(properties) or None
    # KiCad 7 and earlier write the reference and the value as fp_text items.
    for text_node in _find_all_direct(fp_node, "fp_text"):
        kind = _str_arg(text_node, 1)
        text = _str_arg(text_node, 2) or ""
        if kind == "reference" and not reference:
            reference = text
        elif kind == "value" and not value:
            value = text

    # Pads
    pads: list[KiCadPad] = []
    for pad_node in _find_all_direct(fp_node, "pad"):
        pad = _parse_pad(pad_node)
        if pad is not None:
            pads.append(pad)

    return KiCadFootprint(
        reference=reference,
        footprint_name=footprint_name,
        value=value,
        placed_x=placed_x,
        placed_y=placed_y,
        placed_rotation=placed_rotation,
        layer=layer,
        lcsc=lcsc,
        pads=pads,
        courtyard=_courtyard_box(fp_node),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_kicad_pcb_text(text: str) -> list[KiCadFootprint]:
    """Parse a .kicad_pcb's or a .kicad_mod's text and return its footprints.

    A board holds its footprints under ``kicad_pcb``; a library footprint file is
    one ``footprint``.
    """
    root = _parse_sexp(text)
    nodes = (
        [root]
        if _tag(root) == "footprint"
        else [child for child in _children(root) if _tag(child) == "footprint"]
    )
    footprints: list[KiCadFootprint] = []
    for node in nodes:
        fp = _parse_footprint(node)  # type: ignore[arg-type]
        if fp is not None:
            footprints.append(fp)
    return footprints


def footprint_pads(fp: KiCadFootprint) -> list[Pad]:
    """Return the footprint's pads as geometry pads in the footprint's own frame."""
    return [
        Pad(
            number=pad.number,
            x=pad.x,
            y=pad.y,
            width=pad.width,
            height=pad.height,
            rotation=(pad.rotation - fp.placed_rotation) % 360,
            pin_function=pad.pin_function,
            shape=pad.shape,
        )
        for pad in fp.pads
    ]
