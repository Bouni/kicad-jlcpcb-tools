"""Small records the parsers fill and the code that judges a part reads.

``SymbolPin`` (a pin's number and label), the pin-1 label sets, the reading
``pin1_polarity`` takes from them, and ``DrawingMarks`` (what the drawings say
about polarity and the body box).  They live apart from the EasyEDA parser and
the drawing reader so that code which only judges a part never imports either.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Symbol pin labels that name pin 1's terminal; anything else says nothing.
PIN1_CATHODE_LABELS = frozenset({"K", "C", "CA", "CAT", "CATHODE", "K1", "NEG", "-"})
PIN1_ANODE_LABELS = frozenset({"A", "AN", "ANODE", "A1", "AK", "POS", "+"})


@dataclass
class SymbolPin:
    """One schematic-symbol pin: its number and its label text (may be empty)."""

    number: str
    label: str


@dataclass(frozen=True)
class DrawingMarks:
    """What the drawings say: the ``+`` pin, the ``+`` pad and the body box in millimetres."""

    positive_pin: Optional[str] = None
    positive_pad: Optional[str] = None
    body_box: Optional[tuple[float, float, float, float]] = None


def pin1_polarity(pins: list[SymbolPin]) -> Optional[str]:
    """Return 'K' or 'A' when pin 1's label names its terminal, else None."""
    for pin in pins:
        if pin.number != "1":
            continue
        label = pin.label.upper()
        if label in PIN1_CATHODE_LABELS:
            return "K"
        if label in PIN1_ANODE_LABELS:
            return "A"
    return None
