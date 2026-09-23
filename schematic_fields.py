"""Update placed-symbol assignment fields without rewriting other schematic data."""

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
import re
from typing import Any

from .lcsc import normalize_lcsc
from .part_assignments import is_assignment_alias

_QUOTED = r'"(?:\\[^\r\n]|[^"\\\r\n])*"'
_SPACE = " \t\r\n\0"
_TOKEN = re.compile(
    rf'(?P<comment>^[ \t\0]*\#[^\r\n]*)|{_QUOTED}|[()]|[^ \t\r\n\0()"]+', re.MULTILINE
)
_PROPERTY = re.compile(rf"\(property[ \t\r\n\0]+({_QUOTED})[ \t\r\n\0]+({_QUOTED})")
_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    '"': '"',
    "\\": "\\",
}
_ESCAPED = re.compile(rb'\\([abfnrtv"\\]|x[0-9a-fA-F]{0,2}|[0-7]{1,3})')


def _unquote(quoted: str) -> str:
    """Decode KiCad's byte escapes, retaining unknown escapes like DSNLEXER.

    Hex and octal escapes can encode individual bytes of one UTF-8 character.
    KiCad also permits literal tabs and does not interpret JSON's Unicode escapes.
    """

    def decode(match: re.Match[bytes]) -> bytes:
        value = match.group(1)
        if value.startswith(b"x"):
            return bytes([int(value[1:], 16)]) if len(value) > 1 else b"x"
        if value[:1] in b"01234567":
            return bytes([int(value, 8) % 256])
        return _ESCAPES[value.decode("ascii")].encode("utf-8")

    return _ESCAPED.sub(decode, quoted[1:-1].encode("utf-8")).decode("utf-8")


def _quote(value: str) -> str:
    """Escape the same four characters as KiCad's OUTPUTFORMATTER::Quotes."""
    return (
        '"'
        + value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        + '"'
    )


@dataclass(frozen=True)
class _Form:
    """A balanced S-expression's name and original character bounds."""

    name: str
    start: int
    end: int


def _forms(text: str, start: int, end: int) -> Iterator[_Form]:
    """Yield direct forms, ignoring parentheses inside escaped quoted strings."""
    depth = 0
    name = ""
    form_start = start
    position = start
    for token in _TOKEN.finditer(text, start, end):
        if text[position : token.start()].strip(_SPACE):
            raise ValueError("Unterminated quoted string in schematic")
        position = token.end()
        if token.group("comment") is not None:
            continue
        value = token.group()
        if value == "(":
            if depth == 0:
                form_start, name = token.start(), ""
            depth += 1
        elif value == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("Unbalanced schematic S-expression")
            if depth == 0:
                yield _Form(name, form_start, token.end())
        elif depth == 1 and not name:
            name = value
    if text[position:end].strip(_SPACE):
        raise ValueError("Unterminated quoted string in schematic")
    if depth:
        raise ValueError("Unbalanced schematic S-expression")


def _children(text: str, form: _Form) -> Iterator[_Form]:
    """Read only immediate child forms, keeping nested variant data separate."""
    return _forms(text, form.start + 1, form.end - 1)


def _new_field(text: str, reference: _Form, value: str, version7: bool) -> str:
    """Place a hidden canonical field beside the Reference when no alias exists."""
    prefix = text[text.rfind("\n", 0, reference.start) + 1 : reference.start]
    indent = prefix if not prefix.strip() else "    "
    location = next(
        (
            text[form.start : form.end]
            for form in _children(text, reference)
            if form.name == "at"
        ),
        "(at 0 0 0)",
    )
    encoded = _quote(value)
    if version7:
        return (
            f'\n{indent}(property "LCSC" {encoded} {location}\n'
            f"{indent}  (effects (font (size 1.27 1.27)) hide)\n{indent})"
        )
    return (
        f'\n{indent}(property "LCSC" {encoded}\n{indent}  {location}\n'
        f"{indent}  (effects (font (size 1.27 1.27)) (hide yes))\n{indent})"
    )


def update_assignment_fields(
    text: str, parts: Iterable[Mapping[str, Any]], *, version7: bool
) -> str:
    """Synchronize every base assignment alias using the board's field policy.

    A matching source row with an empty value clears existing aliases. A symbol
    absent from the source is untouched. Existing field names, formatting, library
    symbols, and named-variant overrides are retained; only base value spans change.
    """
    assignments: dict[str, str] = {}
    for part in parts:
        assignments.setdefault(part["reference"], normalize_lcsc(part["lcsc"]))

    edits: list[tuple[int, int, str]] = []
    for root in _forms(text, 0, len(text)):
        if root.name != "kicad_sch":
            continue
        for symbol in _children(text, root):
            if symbol.name != "symbol":
                continue
            children = list(_children(text, symbol))
            if not any(form.name == "lib_id" for form in children):
                continue
            properties = [
                (form, match, _unquote(match.group(1)))
                for form in children
                if form.name == "property"
                and (match := _PROPERTY.match(text, form.start, form.end))
            ]
            reference = next(
                (
                    (form, match)
                    for form, match, name in properties
                    if name == "Reference"
                ),
                None,
            )
            if reference is None:
                continue
            reference_form, reference_match = reference
            ref = _unquote(reference_match.group(2))
            if ref not in assignments:
                continue
            value = assignments[ref]
            aliases = [
                match for _form, match, name in properties if is_assignment_alias(name)
            ]
            if aliases:
                encoded = _quote(value)
                edits.extend(
                    (match.start(2), match.end(2), encoded) for match in aliases
                )
            elif value:
                edits.append(
                    (
                        reference_form.end,
                        reference_form.end,
                        _new_field(text, reference_form, value, version7),
                    )
                )

    pieces = []
    offset = 0
    for start, end, replacement in sorted(edits):
        pieces.extend((text[offset:start], replacement))
        offset = end
    pieces.append(text[offset:])
    return "".join(pieces)
