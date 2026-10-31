"""Terminal vocabulary for polarized two-pad parts.

What kind of part a two-pad part is (diode, polarized capacitor, another polarized
part, or none of these), and which pad is its cathode or its positive terminal: on
the KiCad side from the schematic's pin functions, and on the EasyEDA side from the
package name's FD/RD token, the symbol's pin-1 label and the drawings' ``+`` marks.
Anode and positive terminal are the same idea here, as are cathode and negative
terminal: an LED whose symbol says ``+``/``-`` and a capacitor whose symbol says
``A``/``K`` must both resolve.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Optional

from .geometry import Pad
from .naming import CATHODE_PIN1_FAMILIES, extract_family, extract_orientation_tokens
from .records import PIN1_ANODE_LABELS, PIN1_CATHODE_LABELS, DrawingMarks, SymbolPin

# Polarized capacitor families: aluminum electrolytic and tantalum.  A part in one of
# them is a polarized capacitor (:func:`part_kind`).  Their pin 1 is the positive
# terminal, so they read FD/RD the standard way and must not appear in
# naming.CATHODE_PIN1_FAMILIES.
#
# CP-* prefix = "Capacitor Polarized" in EasyEDA naming conventions.
# CASE-* = EIA standard tantalum case codes (A=3216, B=3528, C=6032, D=7343, E=7360).
POLARIZED_CAP_FAMILIES: frozenset[str] = frozenset(
    {
        # Through-hole aluminum electrolytic
        "CP-TH",
        # SMD aluminum electrolytic (cylindrical; CP = Capacitor Polarized)
        "CP-SMD",
        # Tantalum SMD — EIA standard case codes
        "CASE-A",
        "CASE-B",
        "CASE-C",
        "CASE-D",
        "CASE-E",
    }
)

# Pin-function tokens, after normalisation, that name the terminals.  ``C`` is a
# collector on KiCad's transistor symbols and a cathode on some vendor symbols; it is
# accepted as a cathode only when the part is already known to be a diode.
_CATHODE_TOKENS = frozenset({"K", "CATHODE", "CAT"})
_ANODE_TOKENS = frozenset({"A", "ANODE", "AN"})
_POSITIVE_TOKENS = frozenset({"+", "POS", "POSITIVE"})
_NEGATIVE_TOKENS = frozenset({"-", "NEG", "NEGATIVE"})
_CAP_LABELS = frozenset({"+", "-", "POS", "NEG"})
# The symbol labels that name a diode's terminals.  A part is a diode by its labels
# only when they name both: ``A`` alone is also a switch's pin, and ``C`` alone a
# transistor's collector.
_CATHODE_LABELS = PIN1_CATHODE_LABELS - _CAP_LABELS
_ANODE_LABELS = PIN1_ANODE_LABELS - _CAP_LABELS
# Pin functions that say "no connection" rather than naming a signal: they carry no
# terminal and do not disqualify the other pads' functions.
_NO_FUNCTION_TEXTS = frozenset({"NC", "N/C", "N.C.", "~", "DNC", "NP", "NU"})
# The molded-chip tantalum drawings (``CAP-SMD_L3.2-W1.6-RD``), as against the cans
# (``CAP-SMD_BD5.0-L5.3``): the family whose FD/RD token disagrees most often with its
# own drawings' ``+`` marks over EasyEDA's library, read by :func:`token_is_weak`.
_MOLDED_CHIP_NAME = re.compile(r"CAP-SMD_L[\d.]", re.IGNORECASE)
# A sign carrying the pin number KiCad appends to a pin's name on the board (``+_1``,
# ``-_2``), the same suffix as ``K_1``; matched whole, so ``V+_8`` is not a sign.
_NUMBERED_SIGN = re.compile(r"([+-])_\d+")

# Polarized two-terminal parts that are neither diodes nor capacitors and take the
# positive terminal as their reference: batteries, buzzers and microphones.  KiCad
# names their footprints Battery... (BatteryHolder_... among them), Buzzer_... and
# MagneticBuzzer_...; EasyEDA's families are BAT-..., BUZ... and MIC-..., where MIC-
# keeps its hyphen so that the diode packages MICROSMP and MICRO-MELF stay diodes.
# BATT-, BATTERY- and BEEP- are other spellings of the same parts in EasyEDA's
# library.
POSITIVE_REFERENCE_FOOTPRINTS = ("BATTERY", "BUZZER_", "MAGNETICBUZZER_")
POSITIVE_REFERENCE_FAMILIES = ("BAT-", "BATT-", "BATTERY-", "BUZ", "BEEP-", "MIC-")


@dataclass(frozen=True)
class PartKind:
    """What a two-pad part is, as far as its polarity goes, and how that polarity is read.

    ``reference`` is the terminal a polarized alignment is anchored on: a diode's
    cathode, the positive terminal of anything else.  ``convention`` is what KiCad's
    pad 1 is taken to be when no pin function says, for the note recording that
    assumption.  ``diode`` lets ``C`` read as a cathode (see the token note above) and
    turns the naming rule's FD/RD tokens cathode-first.  A part of no polarized kind
    (:data:`OTHER`) reaches the polarized alignment only through an FD/RD token, which
    says where pin 1 goes as it does for a capacitor's positive terminal.
    """

    name: str
    reference: str
    convention: str
    diode: bool = False
    polarized: bool = True


DIODE = PartKind("diode", reference="cathode", convention="K", diode=True)
POLAR_CAP = PartKind("polar_cap", reference="positive", convention="+")
# A battery, buzzer or microphone: polarized, neither a diode nor a capacitor.
POLAR = PartKind("polar", reference="positive", convention="+")
OTHER = PartKind(
    "other", reference="positive", convention="the token's pin 1", polarized=False
)

OPPOSITE = {
    "cathode": "anode",
    "anode": "cathode",
    "positive": "negative",
    "negative": "positive",
}
SAME_MEANING = {
    "cathode": {"cathode", "negative"},
    "negative": {"cathode", "negative"},
    "anode": {"anode", "positive"},
    "positive": {"anode", "positive"},
}


def normalise_function(text: str) -> str:
    """Reduce a KiCad pin function such as ``K_1``, ``+_1`` or ``anode`` to a bare upper-case token.

    The letters before the first other character are the token, so ``K_1`` reads as
    ``K`` and ``V+_8`` as ``V``.  A sign, bare or with KiCad's pin-number suffix
    (``+_1``, ``-_2``), reads as the sign itself.
    """
    token = text.strip().upper()
    if token in ("+", "-"):
        return token
    numbered = _NUMBERED_SIGN.fullmatch(token)
    if numbered:
        return numbered.group(1)
    letters = ""
    for char in token:
        if not char.isalpha():
            break
        letters += char
    return letters


def is_no_function(text: str) -> bool:
    """Return True for an empty pin function or one that says "not connected".

    A name with no leading letters (``1``, ``+5V``, ``~{CS}``) is a function all the
    same: it names a pin, and :func:`function_terminals` must not skip it.
    """
    raw = text.strip().upper()
    return (
        not raw
        or raw in _NO_FUNCTION_TEXTS
        or normalise_function(text) in ("NC", "DNC", "NP", "NU")
    )


def terminal_of(pad: Pad, diode: bool = False) -> str:
    """Return ``cathode``, ``anode``, ``positive``, ``negative`` or ``""`` from the pin function.

    ``C`` counts as a cathode only when ``diode`` is True (see the token note above).
    """
    token = normalise_function(pad.pin_function)
    if token in _CATHODE_TOKENS or (diode and token == "C"):
        return "cathode"
    if token in _ANODE_TOKENS:
        return "anode"
    if token in _POSITIVE_TOKENS:
        return "positive"
    if token in _NEGATIVE_TOKENS:
        return "negative"
    return ""


def function_terminals(pads: list[Pad], diode: bool = False) -> set[str]:
    """Return the terminals the pads' functions name, or nothing when one names something else.

    A switch symbol's ``A``/``B`` must not read as an anode: the functions count only
    when every function on the pads is a terminal name, where an empty or "not
    connected" function (``NC``, ``DNC``, ``~``) is no function at all rather than a
    disqualifying one.  Numbered names read as their letters, so a connector's
    ``A1``/``A2`` name two anodes here, as the pins of KiCad's bidirectional TVS
    symbol do; :func:`part_kind` counts pad functions as a diode's only when they
    name a cathode.
    """
    terminals: set[str] = set()
    for pad in pads:
        if is_no_function(pad.pin_function):
            continue
        terminal = terminal_of(pad, diode)
        if not terminal:
            return set()
        terminals.add(terminal)
    return terminals


def part_kind(
    package_name: str,
    kicad_footprint_name: str,
    kicad_pads: list[Pad],
    symbol_pins: list[SymbolPin],
    marks: Optional[DrawingMarks] = None,
) -> PartKind:
    """Return :data:`DIODE`, :data:`POLAR_CAP`, :data:`POLAR` or :data:`OTHER`.

    Evidence is weighed from the most to the least reliable: the KiCad footprint's name
    and its pads' functions, then EasyEDA's package family, then the symbol's pin
    labels.  LED symbols labelled ``+``/``-`` are common, so labels alone never outrank
    a diode family, and ``+``/``-`` pad functions name a capacitor only when nothing
    else says diode.  A diode's evidence must name its cathode: pad functions that
    name only anodes are as likely a connector's ``A1``/``A2``, and labels count only
    when they name both terminals.  :data:`POLAR` is a polarized part whose reference
    is its positive terminal although it is neither a diode nor a capacitor: a
    battery, buzzer or microphone by its footprint name or family, ranked after the
    diode and capacitor names and families, or a part nothing else classifies whose
    EasyEDA symbol or footprint drawing carries a ``+`` mark (``marks``).
    """
    family = extract_family(package_name).upper()
    footprint = kicad_footprint_name.rsplit(":", 1)[-1].upper()
    terminals = function_terminals(kicad_pads)
    labels = {pin.label.strip().upper() for pin in symbol_pins}
    polarity_tokens = {
        t for t in extract_orientation_tokens(package_name) if t in ("FD", "RD")
    }
    if footprint.startswith(("D_", "LED_")) or "cathode" in terminals:
        return DIODE
    if footprint.startswith(("CP_", "C_ELEC", "TANTALUM")):
        return POLAR_CAP
    if family in CATHODE_PIN1_FAMILIES:
        return DIODE
    if family in POLARIZED_CAP_FAMILIES or (
        family.startswith("CAP") and polarity_tokens
    ):
        return POLAR_CAP
    # Batteries, buzzers and microphones, after the diode and capacitor rules and
    # before the symbol labels.
    if footprint.startswith(POSITIVE_REFERENCE_FOOTPRINTS) or family.startswith(
        POSITIVE_REFERENCE_FAMILIES
    ):
        return POLAR
    if labels & _CATHODE_LABELS and labels & _ANODE_LABELS:
        return DIODE
    if terminals & {"positive", "negative"} or labels & _CAP_LABELS:
        return POLAR_CAP
    # A drawn + marks an otherwise unclassified part as polarized.
    if marks is not None and (marks.positive_pin or marks.positive_pad):
        return POLAR
    return OTHER


def kicad_reference_pad(
    pads: list[Pad],
    reference: str,
    diode: bool = False,
    convention: Optional[str] = None,
) -> tuple[Optional[Pad], bool, str]:
    """Return (pad, assumed, note) for the KiCad pad carrying ``reference``.

    A pin function naming the reference terminal, or its opposite on the other of two
    pads, decides; anode and positive (cathode and negative) are interchangeable.  Two
    pads claiming the same terminal are contradictory: no pad, with a note.  Without any
    usable function, pad 1 is assumed and the note says what it is assumed to be
    (``convention``, by default K for a cathode reference and + otherwise).
    """
    wanted = SAME_MEANING[reference]
    opposite = SAME_MEANING[OPPOSITE[reference]]
    claims_reference = [pad for pad in pads if terminal_of(pad, diode) in wanted]
    claims_opposite = [pad for pad in pads if terminal_of(pad, diode) in opposite]
    if len(claims_reference) > 1 or len(claims_opposite) > 1:
        return None, False, "contradictory pin functions on the KiCad footprint"
    if claims_reference:
        return claims_reference[0], False, ""
    if claims_opposite and len(pads) == 2:
        return next(pad for pad in pads if pad is not claims_opposite[0]), False, ""
    if convention is None:
        convention = "K" if reference == "cathode" else "+"
    for pad in pads:
        if pad.number == "1":
            return pad, True, f"assumed KiCad pad 1 = {convention}"
    return None, True, "no pad 1 on the KiCad side"


def side_of(pad: Pad, pads: list[Pad]) -> Optional[str]:
    """Return ``left`` or ``right`` for one of two pads, or None when their axis is not horizontal."""
    other = next(p for p in pads if p is not pad)
    dx, dy = pad.x - other.x, pad.y - other.y
    if abs(dx) <= abs(dy):
        return None
    return "left" if dx < 0 else "right"


def token_reference_side(package_name: str, reference: str) -> Optional[str]:
    """Return ``left``, ``right``, ``none`` (bidirectional) or None (no token) from FD/RD/BI.

    Forward direction puts the part's marking band on the right of the EasyEDA
    drawing (JLC's zero orientation) and reverse direction on the left.  The band
    is the cathode of a diode and the negative end of a capacitor, so FD means
    cathode right and positive left.  Over EasyEDA's library that reading agrees
    with the drawings' ``+`` marks on 201 of 203 electrolytic cans and on 39 of 45
    molded-chip tantalums.  Where the two disagree (C7171 is one), the resolver
    counts the marks as votes of their own, and two marks outvote the token.
    """
    polarity = [
        t for t in extract_orientation_tokens(package_name) if t in ("FD", "RD", "BI")
    ]
    if not polarity:
        return None
    token = polarity[-1]
    if token == "BI":
        return "none"
    band_side = "right" if token == "FD" else "left"
    if reference in SAME_MEANING["negative"]:
        return band_side
    return "left" if band_side == "right" else "right"


def token_is_weak(package_name: str) -> bool:
    """Return True where an unchallenged FD/RD token deserves only medium confidence.

    The molded-chip tantalum family (``CAP-SMD_L…``, as against the ``CAP-SMD_BD…``
    cans) is the one family whose token disagrees with its drawings' ``+`` marks on a
    measurable share of EasyEDA's library: 39 of 45 agree, against 201 of 203 cans and
    effectively every diode.  A part of that family whose drawings carry no mark to
    check the token against -- a drawing without a ``+`` -- is therefore resolved, but
    not with high confidence.  Cans and diodes keep theirs.
    """
    return bool(_MOLDED_CHIP_NAME.match(package_name.strip()))


def label_reference_pad(
    jlc_pads: list[Pad], polarity: Optional[str], reference: str
) -> Optional[Pad]:
    """Return the EasyEDA pad carrying ``reference`` from the symbol's pin-1 label, if known.

    ``polarity`` is 'K' (pin 1 is the cathode or negative terminal) or 'A' (anode or
    positive).
    """
    if polarity is None:
        return None
    pad1 = next((p for p in jlc_pads if p.number == "1"), None)
    if pad1 is None:
        return None
    other = next((p for p in jlc_pads if p is not pad1), None)
    if other is None:
        return None
    pin1_is_reference = (polarity == "K") == (reference == "cathode")
    return pad1 if pin1_is_reference else other


def drawing_reference_pads(
    jlc_pads: list[Pad], marks: Optional[DrawingMarks], reference: str
) -> tuple[Optional[Pad], Optional[Pad]]:
    """Return the EasyEDA pad carrying ``reference`` per the symbol's and the footprint's ``+``.

    The symbol's ``+`` names a pin number and the footprint's ``+`` a pad number
    (:mod:`jlcfootprint.drawing`); both name the positive terminal, the anode of a
    diode.  A drawing without a mark gives None.  The two come back apart so that
    each casts its own vote in the resolver: two marks naming different pads are
    one disagreement more, outvoted when the token and the label side with one of
    them and a tie otherwise.
    """
    if marks is None:
        return None, None
    by_number = {pad.number: pad for pad in jlc_pads}

    def carrying(number: Optional[str]) -> Optional[Pad]:
        positive = by_number.get(number) if number else None
        if positive is None:
            return None
        if reference in SAME_MEANING["positive"]:
            return positive
        return next((pad for pad in jlc_pads if pad is not positive), None)

    return carrying(marks.positive_pin), carrying(marks.positive_pad)


def pin1_meaning(pads: list[Pad], kind: PartKind) -> Optional[str]:
    """Return 'A' or 'K' for what the KiCad side means by pad 1.

    Pad 1's own function decides; failing that, the other pad's function implies it;
    failing both, the convention for the kind (pad 1 = K on diodes, + on capacitors).
    """
    pad1 = next((p for p in pads if p.number == "1"), None)
    if pad1 is None:
        return None
    terminal = terminal_of(pad1, kind.diode)
    if not terminal and len(pads) == 2:
        other = terminal_of(next(p for p in pads if p is not pad1), kind.diode)
        terminal = OPPOSITE.get(other, "")
    if terminal in ("cathode", "negative"):
        return "K"
    if terminal in ("anode", "positive"):
        return "A"
    return "K" if kind.diode else "A"
