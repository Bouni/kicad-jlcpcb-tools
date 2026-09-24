"""Canonical catalog spellings for passive component values.

The catalog writes one spelling per value; a schematic writes several.  A part
described ``100nF`` cannot be found by searching ``0.1uF``, which is what the
value field says on the board.

This module rewrites a board value into the spelling the catalog uses, so the
part selector can prefill a search that matches.  It never guesses: the caller
supplies the quantity being measured, from the reference designator, and a value
that does not fit that quantity is left alone rather than reinterpreted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import re
from typing import Optional


@dataclass(frozen=True)
class Quantity:
    """One measurable quantity and how the catalog spells it.

    ``rungs`` are the SI prefixes the catalog uses for this quantity, largest
    first.  ``prefixes`` maps every spelling a schematic might use onto one of
    them, which is also where prefix case is decided: M and m belong to
    different quantities and must never be folded together.  ``prefix_words``
    are the prefixes spelled with more than one letter.  They are matched in
    any case, because unlike M and m no two of them differ by case alone.
    """

    # Only the name is shown when one of these appears in an assertion message;
    # the tables behind it are long and say nothing a reader needs there.
    name: str
    rungs: tuple[str, ...] = field(repr=False)
    symbol: str = field(repr=False)
    top_rung: str = field(repr=False)
    bottom_rung: str = field(repr=False)
    units: frozenset = field(repr=False)
    prefixes: Mapping[str, str] = field(repr=False)
    rkm_prefixes: Mapping[str, str] = field(repr=False)
    prefix_words: Mapping[str, str] = field(default_factory=dict, repr=False)


# k and K are the same prefix by convention, but M and m are not: the tables are
# keyed by the exact character, which is what keeps a 0.5mΩ shunt off the mega
# rung.  Milliohms are a real catalog spelling -- current-sense resistors are
# written 100mΩ far more often than 0.1Ω -- so resistance carries a milli rung,
# and nothing below it: no description spells a resistance in microohms, and the
# parts under a milliohm are written 0.5mΩ rather than 500uΩ.
RESISTANCE = Quantity(
    name="resistance",
    rungs=("M", "k", "", "m"),
    symbol="Ω",
    top_rung="M",
    # Nothing is spelled in microohms; parts under a milliohm are written 0.5mΩ.
    bottom_rung="m",
    units=frozenset({"ω", "r", "o", "ohm", "ohms"}),
    prefixes={"M": "M", "k": "k", "K": "k", "m": "m"},
    rkm_prefixes={"M": "M", "k": "k", "K": "k", "m": "m", "R": "", "r": ""},
    # meg is how SPICE spells mega, since M means milli there, and SPICE ignores
    # case.  No catalog description spells it.
    prefix_words={"meg": "M"},
)

CAPACITANCE = Quantity(
    name="capacitance",
    rungs=("m", "u", "n", "p"),
    symbol="F",
    # Electrolytics are written 4700uF, never 4.7mF: 2,848 capacitor values on
    # the u rung are >= 1000, against none on the n rung.  So milli is an input
    # spelling here, not an output one.
    top_rung="u",
    bottom_rung="p",  # 12,096 capacitor values are in pF
    units=frozenset({"f"}),
    prefixes={"m": "m", "u": "u", "U": "u", "n": "n", "N": "n", "p": "p", "P": "p"},
    rkm_prefixes={"m": "m", "u": "u", "U": "u", "n": "n", "p": "p", "P": "p"},
)

INDUCTANCE = Quantity(
    name="inductance",
    rungs=("m", "u", "n", "p"),
    symbol="H",
    top_rung="m",
    # No inductor description uses pH at all, while 110 are written 0.xnH, so
    # nano is the floor: scaling 0.6nH down to 600pH would find nothing.
    bottom_rung="n",
    units=frozenset({"h"}),
    prefixes={"m": "m", "u": "u", "U": "u", "n": "n", "N": "n", "p": "p", "P": "p"},
    rkm_prefixes={"m": "m", "u": "u", "U": "u", "n": "n", "p": "p", "P": "p"},
)

# Reference designator prefixes, as KiCad's own symbol libraries assign them.
# Matched whole, never by first letter: LD is a laser diode and LS a speaker,
# neither of which is an inductor, while TH is a thermistor and is a resistor.
# A prefix that is absent maps to no quantity, so its value is left untouched.
_QUANTITY_BY_REFERENCE_PREFIX = {
    "R": RESISTANCE,
    "RN": RESISTANCE,
    "RV": RESISTANCE,
    "TH": RESISTANCE,
    "C": CAPACITANCE,
    "CN": CAPACITANCE,
    "L": INDUCTANCE,
}

_REFERENCE_PREFIX_RE = re.compile(r"^([A-Za-z]+)")

# 0.1uF, 4.7k, 100p, 100Ω, 10 -- a magnitude with an optional prefix and unit.
# A leading zero is only allowed before a decimal point, which keeps the
# leading-zero package codes (0603, 0402, 0201, 01005) out.  The ones that do
# not start with a zero -- 1206, 2512 -- still parse, as they did before this
# change; they reach the box as a value that finds nothing either way.
_PLAIN_RE = re.compile(r"^(?P<num>0?\.\d+|[1-9]\d*(?:\.\d+)?|0)\s*(?P<rest>[^\d\s]*)$")

# RKM / IEC 60062, where the prefix letter replaces the decimal point: 4k7, 2u2,
# 1M5, 4R7, and 0R001 for a milliohm.  Part numbers are kept out by the letter
# being looked up in the quantity's own table rather than by limiting the
# digits: N is not an RKM prefix for any quantity, so 1N4148 and 2N3904 cannot
# parse.  The integer part may be omitted for a sub-unit value -- R47 is 0.47Ω
# -- but only for the base-unit letter, so a mounting hole labelled M3 cannot
# read as 0.3MΩ.
_RKM_RE = re.compile(
    r"^(?P<int>\d*)(?P<letter>[^\W\d_])(?P<frac>\d+)(?P<rest>[^\d\s]*)$"
)


def _format_decimal(value: Decimal) -> str:
    """Format a Decimal without exponent notation or trailing zeros."""
    normalized = value.normalize()
    exponent = normalized.as_tuple().exponent
    if isinstance(exponent, int) and exponent > 0:
        return str(int(normalized))
    return f"{normalized:f}"


def _clean(text: str) -> str:
    """Fold both micro signs onto 'u', which is what the catalog writes."""
    return text.strip().replace("µ", "u").replace("μ", "u")


def _resolve_rung(rest: str, quantity: Quantity) -> Optional[str]:
    """Return the ladder rung named by a value's trailing text, or None.

    ``rest`` is whatever followed the digits: a prefix, a unit, both, or
    nothing.  None means it does not name this quantity, for either of two
    reasons: the unit belongs to another one (``nF`` asked about as a
    resistance), or the prefix has no rung on this ladder (``uΩ``, since milli
    is the smallest resistance the catalog spells).
    """
    if rest == "" or rest.casefold() in quantity.units:
        # An unprefixed magnitude only means something where the ladder has a
        # base rung.  Resistance does ('100' is 100Ω); capacitance does not,
        # since the catalog has no use for bare farads.
        return "" if "" in quantity.rungs else None
    # Words first, so meg is read whole before its m can be read as milli.
    folded = rest.casefold()
    for word, rung in quantity.prefix_words.items():
        if folded.startswith(word):
            tail = folded[len(word) :]
            return rung if not tail or tail in quantity.units else None
    rung = quantity.prefixes.get(rest[0])
    if rung is None:
        return None
    tail = rest[1:].casefold()
    if tail and tail not in quantity.units:
        return None
    return rung


def _scale(value: Decimal, rung: str, quantity: Quantity) -> tuple[Decimal, str]:
    """Move along the ladder to the rung the catalog would print this value on.

    Down while the mantissa is below one, up while it reaches a thousand, and
    never outside ``top_rung``..``bottom_rung`` -- the span each quantity is
    actually written in.  Both ends are evidence, not symmetry: capacitance
    stops at uF because 4700uF is what electrolytics are called, and inductance
    stops at nH because no inductor is described in pH.
    """
    index = quantity.rungs.index(rung)
    ceiling = quantity.rungs.index(quantity.top_rung)
    floor = quantity.rungs.index(quantity.bottom_rung)

    # A value can arrive outside the range the catalog prints -- 1mF on a
    # capacitor, 100pH on an inductor.  Bring it inside before looking at the
    # magnitude at all, so it never keeps a spelling that has no parts.
    while index < ceiling:
        value *= Decimal("1000")
        index += 1
    while index > floor:
        value /= Decimal("1000")
        index -= 1

    if value == 0:
        # Zero is zero on any rung, and a jumper is spelled 0Ω -- never 0kΩ or
        # 0mΩ.  Quantities without a base rung keep whatever they arrived on.
        return value, ("" if "" in quantity.rungs else rung)
    while value < Decimal("1") and index < floor:
        value *= Decimal("1000")
        index += 1
    while value >= Decimal("1000") and index > ceiling:
        value /= Decimal("1000")
        index -= 1
    return value, quantity.rungs[index]


def _parse(text: str, quantity: Quantity) -> Optional[tuple[Decimal, str]]:
    """Return (magnitude, rung) for a value of this quantity, or None."""
    rkm = _RKM_RE.match(text)
    if rkm is not None:
        rung = quantity.rkm_prefixes.get(rkm.group("letter"))
        if rung is None:
            return None
        if not rkm.group("int") and rung != "":
            return None
        rest = rkm.group("rest")
        if rest and rest.casefold() not in quantity.units:
            return None
        try:
            return Decimal(f"{rkm.group('int') or '0'}.{rkm.group('frac')}"), rung
        except InvalidOperation:  # pragma: no cover - the pattern admits digits only
            return None

    plain = _PLAIN_RE.match(text)
    if plain is None:
        return None
    rung = _resolve_rung(plain.group("rest"), quantity)
    if rung is None:
        return None
    try:
        return Decimal(plain.group("num")), rung
    except InvalidOperation:  # pragma: no cover - the pattern admits digits only
        return None


def canonicalize(value: Optional[str], quantity: Optional[Quantity]) -> Optional[str]:
    """Return the catalog's spelling of ``value``, or None to leave it alone.

    ``0.1uf`` becomes ``100nF``, ``4K7`` becomes ``4.7kΩ``, ``100p`` becomes
    ``100pF``.  None means "no opinion, use what the board says": the quantity
    is unknown, the value does not parse, or it names a different quantity than
    was asked for.  A unit the value spells out always beats the quantity, so
    ``100nF`` on a reference designator that looks like a resistor is left alone
    rather than rewritten.
    """
    if quantity is None:
        return None
    text = _clean("" if value is None else str(value))
    if not text:
        return None

    parsed = _parse(text, quantity)
    if parsed is None:
        return None
    magnitude, rung = parsed
    magnitude, rung = _scale(magnitude, rung, quantity)
    return f"{_format_decimal(magnitude)}{rung}{quantity.symbol}"


def quantity_for_reference(reference: str) -> Optional[Quantity]:
    """Return the quantity a reference designator measures, or None if unknown.

    The leading run of letters is matched, whole, against the prefixes KiCad's
    libraries actually assign to passives.  Whole matters because the first
    letter does not decide it: LD is a laser diode and LS a speaker, neither an
    inductor.  Anything else -- a diode, a custom library's own prefix, a power
    symbol like #PWR03 -- returns None, and the value is left untouched.

    A separator ends the run rather than disqualifying the reference, so
    R_SHUNT1 and C_IN1 are read as a resistor and a capacitor.  That is
    deliberate: such a designator is the component its first segment names, and
    requiring letters-then-digits instead would drop R1A, the multi-unit form,
    and R?, the unannotated one.
    """
    text = "" if reference is None else str(reference).strip()
    match = _REFERENCE_PREFIX_RE.match(text)
    if match is None:
        return None
    return _QUANTITY_BY_REFERENCE_PREFIX.get(match.group(1).upper())


# A resistance on the milli or mega rung, written with its unit: 10mΩ, 1.5MΩ.
# The catalog's full-text index folds case, and these are the terms where case
# is the whole meaning -- 62 resistor values are written both ways in a
# 717,025-part snapshot.  Either ohm sign is accepted; the catalog writes only
# U+03A9, never U+2126.  The digits are ASCII on purpose: the match is all a
# caller needs to know the term holds no GLOB or LIKE metacharacter.
_EXACT_CASE_RESISTANCE_RE = re.compile(r"[0-9]*\.?[0-9]+[mM][\u03a9\u2126]")


def exact_case_resistance(term: str) -> Optional[str]:
    """Return a search term in the catalog's spelling if its case must match.

    ``10mΩ`` is a current-sense shunt and ``10MΩ`` a bias resistor, and nothing
    but the case of one letter tells them apart.  A term that spells out both
    the prefix and the ohm sign means one of them exactly, so it comes back
    with the ohm sign the catalog uses.  Anything else returns None, including
    a bare ``10m``: with no unit its case is not evidence of anything, since a
    megohm is often written in lower case on a schematic.
    """
    if _EXACT_CASE_RESISTANCE_RE.fullmatch(term) is None:
        return None
    return term.replace("\u2126", "\u03a9")
