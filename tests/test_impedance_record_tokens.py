"""Native snapshot validation retains exact parsing while avoiding per-token copies."""

from __future__ import annotations

from collections.abc import Callable
import random
import re
from typing import Union

import pytest

from impedance import board_copy

_LEGACY_TOKENS = re.compile(r'"(?:\\.|[^"\\])*"|[()]|[^\s()"]+')


def _legacy_tokens(document: str, expected_root: str = "") -> list[str]:
    """Freeze the pre-optimization parser as an independent compatibility oracle."""
    tokens = []
    position = 0
    for match in _LEGACY_TOKENS.finditer(document):
        if document[position : match.start()].strip():
            raise board_copy.BoardCopyError("KiCad returned malformed snapshot text.")
        tokens.append(match.group())
        position = match.end()
    if document[position:].strip() or len(tokens) < 3 or tokens[0] != "(":
        raise board_copy.BoardCopyError("KiCad returned an incomplete snapshot record.")
    if expected_root and tokens[1] != expected_root:
        raise board_copy.BoardCopyError(
            "KiCad returned an invalid board snapshot root."
        )
    depth = 0
    for index, token in enumerate(tokens):
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        if depth <= 0 and index != len(tokens) - 1:
            raise board_copy.BoardCopyError("KiCad returned multiple snapshot roots.")
    if depth or tokens[-1] != ")":
        raise board_copy.BoardCopyError("KiCad returned an incomplete snapshot record.")
    return tokens


def _outcome(callback: Callable[..., object], *args: object) -> tuple[str, object]:
    """Keep successful tokens and exact actionable rejection messages observable."""
    try:
        return "accepted", callback(*args)
    except board_copy.BoardCopyError as error:
        return "rejected", str(error)


class _MeasuredText(str):
    """Count real temporary character substrings, independent of regex strategy."""

    slices = 0

    def __getitem__(self, index: Union[int, slice]) -> str:
        if isinstance(index, slice):
            self.slices += 1
        return super().__getitem__(index)


class _MeasuredTokens(list[str]):
    """Count temporary token-list slices without replacing list state or reads."""

    slices = 0

    def __getitem__(self, index: Union[int, slice]) -> Union[str, list[str]]:
        if isinstance(index, slice):
            self.slices += 1
        return super().__getitem__(index)


def _footprint() -> str:
    """Use native-format multi-pad geometry with quoted text and named nets."""
    pads = "\n".join(
        f'(pad "{index}" smd rect (at {index}.0 0) (size 1 2) '
        f'(layers "F.Cu") (net "Controlled0_SIGNAL{index % 3}"))'
        for index in range(32)
    )
    return (
        '(footprint "Package:RF (connector)" (layer "F.Cu") '
        '(property "Reference" "J1") (property "Value" "RF \\"front end\\"") '
        f"{pads})"
    )


def test_large_native_record_does_not_copy_a_character_substring_per_token() -> None:
    """Validation work must avoid hundreds of discarded whitespace substrings."""
    document = _MeasuredText(_footprint())
    assert board_copy._record_tokens(document) == _legacy_tokens(str(document))
    assert document.slices <= 2


def test_native_named_net_check_does_not_copy_a_token_pair_at_every_position() -> None:
    """Checking named-net syntax needs indexed reads, without temporary lists."""
    tokens = _MeasuredTokens(board_copy._record_tokens(_footprint()))
    board_copy._check_named_nets(tokens)
    assert tokens.slices == 0


@pytest.mark.parametrize("root", ["", "kicad_pcb", "footprint"])
@pytest.mark.parametrize(
    "document",
    [
        "",
        "(",
        ")",
        "()",
        "(root)",
        "(root) (other)",
        "(root))",
        "(root ((",
        '(root "',
        '(root "unfinished',
        '(root "unfinished)',
        '(root) "',
        '"',
        '""',
        '"unfinished',
        '(root "a\\")',
        '(root "a\\"',
        '(root "escaped \\"quote\\"")',
        '(root "backslash \\\\")',
        '(root "line\\\nfeed")',
        '(root "text (net 27)")',
        '(root (net "27"))',
        "(root (net 27))",
        "(root (net))",
        "\u2003(kicad_pcb (version 20260206))\r\n",
        '(footprint "Name (with parentheses)" (property "Value" "é RF"))',
        _footprint(),
    ],
)
def test_quoted_atoms_malformed_records_and_roots_match_saved_parser(
    document: str, root: str
) -> None:
    """Quoted delimiters and malformed/trailing quote distinctions remain exact."""
    assert _outcome(board_copy._record_tokens, document, root) == _outcome(
        _legacy_tokens, document, root
    )


def test_deterministic_adversarial_native_records_match_saved_parser() -> None:
    """Challenge token boundaries, escaping, whitespace and error precedence."""
    randomizer = random.Random(250)
    alphabet = list('()"\\ netkicad_pcb09\t\r\n') + ["\u2003", "é"]
    for _ in range(5_000):
        document = "".join(
            randomizer.choice(alphabet) for _ in range(randomizer.randrange(81))
        )
        for root in ("", "kicad_pcb", "footprint"):
            assert _outcome(board_copy._record_tokens, document, root) == _outcome(
                _legacy_tokens, document, root
            ), (document, root)


@pytest.mark.parametrize(
    "document",
    ['(segment (net "RF"))', '(segment (net "27"))', '(property "Value" "(net 27)")'],
)
def test_named_net_format_keeps_quoted_names_and_text_unchanged(document: str) -> None:
    """Quoted text is a single atom even when it resembles legacy syntax."""
    board_copy._check_named_nets(board_copy._record_tokens(document))


@pytest.mark.parametrize("document", ["(net 27)", "(segment (net 27))", "(net (code))"])
def test_numeric_or_nonquoted_net_values_remain_rejected(document: str) -> None:
    """The faster syntax check cannot mix per-item numeric and named mappings."""
    with pytest.raises(board_copy.BoardCopyError, match="named-net format"):
        board_copy._check_named_nets(board_copy._record_tokens(document))
