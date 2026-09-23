"""Update placed-symbol assignment fields without rewriting other schematic data."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
import re

from .part_assignments import is_assignment_alias

_QUOTED = r'"(?:\\[^\r\n]|[^"\\\r\n])*"'
_SPACE = " \t\r\n\0"
_TOKEN = re.compile(
    rf'(?P<comment>^[ \t\0]*\#[^\r\n]*)|{_QUOTED}|[()]|[^ \t\r\n\0()"]+', re.MULTILINE
)
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


@dataclass(frozen=True)
class _Atom:
    """A decoded argument together with its original token bounds."""

    text: str
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


def _arguments(text: str, form: _Form) -> list[_Atom]:
    """Read leading arguments through the same comment-aware token grammar."""
    arguments = []
    head = True
    for token in _TOKEN.finditer(text, form.start + 1, form.end - 1):
        if token.group("comment") is not None:
            continue
        if head:
            head = False
            continue
        value = token.group()
        if value in {"(", ")"}:
            break
        arguments.append(
            _Atom(
                _unquote(value) if value.startswith('"') else value,
                token.start(),
                token.end(),
            )
        )
    return arguments


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


def update_symbol_fields(
    text: str,
    assignments: Mapping[str, str],
    bom_states: Mapping[str, bool],
    *,
    version7: bool,
) -> str:
    """Apply prevalidated UUID-targeted base fields, retaining all other bytes.

    The hierarchy index authenticates UUIDs and instance consensus before this
    renderer receives a decision. Neither references nor stale instance tables
    participate in selecting the placed symbol.
    """
    edits: list[tuple[int, int, str]] = []
    roots = tuple(_forms(text, 0, len(text)))
    if len(roots) != 1 or roots[0].name != "kicad_sch":
        raise ValueError("Expected one complete kicad_sch document")
    for symbol in _children(text, roots[0]):
        if symbol.name != "symbol":
            continue
        children = list(_children(text, symbol))
        if not any(form.name in {"lib_id", "lib_name"} for form in children):
            continue
        uuids = [
            args[0].text
            for form in children
            if form.name == "uuid" and (args := _arguments(text, form))
        ]
        if len(uuids) != 1:
            continue
        symbol_uuid = uuids[0]
        properties = [
            (form, args[0].text, args[1])
            for form in children
            if form.name == "property" and len(args := _arguments(text, form)) >= 2
        ]
        reference = next(
            (form for form, name, _value in properties if name == "Reference"),
            None,
        )
        if symbol_uuid in assignments:
            value = assignments[symbol_uuid]
            aliases = [
                value for _form, name, value in properties if is_assignment_alias(name)
            ]
            if aliases:
                encoded = _quote(value)
                edits.extend((alias.start, alias.end, encoded) for alias in aliases)
            elif value:
                if reference is None:
                    raise ValueError(f"Symbol {symbol_uuid} has no Reference property")
                edits.append(
                    (
                        reference.end,
                        reference.end,
                        _new_field(text, reference, value, version7),
                    )
                )
        if symbol_uuid in bom_states:
            desired = "no" if bom_states[symbol_uuid] else "yes"
            states = [form for form in children if form.name == "in_bom"]
            if states:
                for form in states:
                    args = _arguments(text, form)
                    if len(args) != 1 or args[0].text not in {"yes", "no"}:
                        raise ValueError(f"Invalid BOM state on symbol {symbol_uuid}")
                    edits.append((args[0].start, args[0].end, desired))
            else:
                edits.append(
                    (children[0].end, children[0].end, f"\n    (in_bom {desired})")
                )
    pieces = []
    offset = 0
    for start, end, replacement in sorted(edits):
        pieces.extend((text[offset:start], replacement))
        offset = end
    pieces.append(text[offset:])
    return "".join(pieces)
