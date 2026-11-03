"""Per-part rotation, fit and polarity verdicts.

Pure and stdlib only.  The caller supplies KiCad pads in the footprint's own
frame, EasyEDA pads already converted to millimetres, and the symbol pins.
There are three alignment paths:

- a part with two JLC pads that is polarized, or whose package name carries an
  FD/RD token, aligns by terminal meaning (``polarity``) and never by pad number:
  the KiCad side's reference pad comes from its pin functions or pad 1, and the
  EasyEDA side's from a vote of the package name's token, the symbol's pin-1 label
  and the drawings' ``+`` marks;
- a non-polar part with two pads on each side aligns by axis, or by the one
  pairing its pad shapes fit (``shape``);
- every other part aligns by pad name (``geometry``), or by pin function when the
  numbers do not line up.

Fit is graded per JLC pad after the placement, and a fitting part's body is
compared with the KiCad courtyard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Optional

from .fit import (
    FitReport,
    Placement,
    align,
    anchor,
    assess_fit,
    courtyard_excess,
    fits,
    fits_clearly_better,
    package_origin,
    pair_by_function,
    pair_by_name,
    shape_alignment,
)
from .geometry import Pad, ccw_correction, named_pads, naming_rule_to_cpl
from .naming import parse_package_name
from .polarity import (
    PartKind,
    drawing_reference_pads,
    kicad_reference_pad,
    label_reference_pad,
    part_kind,
    pin1_meaning,
    side_of,
    token_is_weak,
    token_reference_side,
)
from .records import DrawingMarks, SymbolPin, pin1_polarity

CHECKERBOARD_NOTE = (
    "no JLC footprint data for this part (expect a checkerboard in the preview)"
)
YELLOW_NOTE = (
    "JLC's pin-1 marker will sit on the other terminal; numbering difference, "
    "not a rotation error; do not renumber the footprint"
)
# A non-polar two-pad part whose pads fit one pairing clearly better than the other
# takes that pairing, a full turn and method ``shape``.
SHAPE_NOTE = "pads fit one way round only"
# A side with fewer than two named pads (a test point, a one-pad shield or spring)
# gives no alignment, so the part is unknown with this note and no derived rotation.
SINGLE_PAD_NOTE = "fewer than two named pads on one side"
NO_VOTE_NOTE = "polarity unknown; check in JLC preview"
NON_POLAR_TOKEN_NOTE = (
    "orientation token on a non-polar part: KiCad pad 1 goes where the token puts pin 1, "
    "which need not be JLC's pad 1"
)
# A side whose pads all share one number (a standoff's or a shield's legs) cannot say
# which way round the part goes: any turn that puts each pad on some pad would fit.
# A two-terminal part whose drawing splits a terminal and puts both terminals' centres
# in one place aligns by pad number instead of by meaning (see ``resolve``).
SHARED_CENTRE_NOTE = "the terminals share a centre, so the pads align by number"
SINGLE_NUMBER_NOTE = (
    "every pad on one side has the same number; the turn cannot be told"
)
DRAWING_NOTES = {
    "both": "polarity from the + marks of the symbol and the footprint",
    "footprint": "polarity from the footprint's + mark",
    "symbol": "polarity from the symbol's + mark",
}
# The body caveat: the JLC body may overhang the KiCad courtyard by this fraction of
# the courtyard's shorter side, never less than the floor (drawing noise on an 0201)
# nor more than the ceiling (a DIP-40 still flags a wide body).
BODY_EXCESS_FRACTION = 0.10
BODY_EXCESS_MIN_MM = 0.15
BODY_EXCESS_MAX_MM = 1.5

# The statuses whose verdict carries a derived rotation and a placement, so a CPL may
# use the rotation for its angle and the placement for its position.
APPLIED_STATUSES = ("green", "yellow")


@dataclass
class Verdict:
    """One part's verdict: the status, fit and rotation, how they were found, and the notes."""

    status: str = "unknown"  # green | yellow | red | unknown
    fit: str = "no_data"  # fits | fits_larger_pads | fits_tight | count | pitch | numbering | mirror | no_data
    rotation: Optional[int] = None
    method: str = "none"  # geometry | polarity | axis | shape | none
    confidence: str = "none"  # high | medium | low | none
    name_rotation: Optional[int] = None
    polarity_light: Optional[str] = None  # green | yellow | unknown; None for non-polar
    polarity_source: str = ""  # token | label | drawing | ""
    non_polar: bool = False  # True when nothing names a polarity (axis, shape, marked)
    pad_count_kicad: int = 0
    pad_count_jlc: int = 0
    matched_pads: int = 0
    missing_central_pad: bool = False
    overlap_min: float = 0.0
    overlap_mean: float = 0.0
    angular_rms: float = 0.0
    residual_mm: float = 0.0
    body_excess_mm: Optional[float] = None  # the caveat: how far the JLC body overhangs
    notes: list[str] = field(default_factory=list)
    placement: Optional[Placement] = field(default=None, repr=False, compare=False)

    @property
    def note_text(self) -> str:
        """Return the notes joined for storage and display."""
        return "; ".join(self.notes)

    def unresolved(self, status: str, fit: str, note: str) -> Verdict:
        """Mark the verdict as carrying no derived rotation and return it."""
        self.status = status
        self.fit = fit
        self.rotation = None
        self.method = "none"
        self.notes.append(note)
        return self

    @property
    def origin(self) -> Optional[tuple[float, float]]:
        """Return JLC's package origin in the footprint frame, or None.

        Only a verdict the resolver settled as green or yellow carries one: a red,
        unknown or unfinished verdict has no placement a CPL may trust.
        """
        if self.placement is None or self.status not in APPLIED_STATUSES:
            return None
        return package_origin(self.placement)

    def take_placement(self, placement: Placement) -> None:
        """Copy the placement's metrics and keep the placement for the drawing."""
        self.placement = placement
        self.residual_mm = placement.residual
        self.angular_rms = placement.angular_rms
        self.matched_pads = placement.matched

    def take_fit(self, report: FitReport) -> None:
        """Copy the fit report's grade, notes and metrics."""
        self.fit = report.fit
        self.missing_central_pad = report.missing_central_pad
        self.overlap_min = report.overlap_min
        self.overlap_mean = report.overlap_mean
        self.notes.extend(report.notes)


def _mismatch_note(verdict: Verdict, fit: str) -> str:
    """Describe a red fit in the UI vocabulary, following the fit report's decision."""
    if fit == "count":
        return (
            f"does not fit: {verdict.pad_count_kicad} vs {verdict.pad_count_jlc} pads"
        )
    return f"does not fit: pitch (a JLC pad misses its KiCad pad; worst overlap {verdict.overlap_min:.0%})"


def _finish_multi_pin(
    verdict: Verdict, kicad: dict[str, Pad], placement: Placement, by_name: bool = True
) -> Verdict:
    """Set the rotation and status of a multi-pin part whose pads fit.

    ``by_name`` says how the pads were paired: on their numbers or, when that
    fails, by the pin functions the schematic gave them.  Only a by-name alignment
    can rest on "shared pad names", so the notes that say so are its own.
    """
    verdict.rotation = ccw_correction(placement.rotation_deg)
    verdict.method = "geometry"
    if verdict.confidence == "none":
        verdict.confidence = "high"
    if by_name and len(kicad) == 2:
        verdict.confidence = "medium"
        verdict.notes.append(
            "aligned on two shared pad names; remaining pads matched by position"
        )
    if verdict.name_rotation is not None and verdict.name_rotation != verdict.rotation:
        verdict.confidence = "medium"
        verdict.notes.append(
            f"KiCad footprint drawn non-standard; name says {verdict.name_rotation}°"
        )
    if verdict.pad_count_kicad != verdict.pad_count_jlc:
        paired = "shared names" if by_name else "paired pads"
        verdict.notes.append(
            f"pad counts differ ({verdict.pad_count_kicad} vs {verdict.pad_count_jlc}) "
            f"but the {len(kicad)} {paired} align"
        )
    verdict.status = "yellow" if _fits_with_caveat(verdict) else "green"
    return verdict


def _fits_with_caveat(verdict: Verdict) -> bool:
    """Return True when a part that fits is yellow for its fit: tight pads or a missing central pad."""
    return verdict.missing_central_pad or verdict.fit == "fits_tight"


def _resolve_by_function(
    kicad_pads: list[Pad],
    jlc_pads: list[Pad],
    verdict: Verdict,
    symbol_pins: list[SymbolPin],
) -> Optional[Verdict]:
    """Align by pin function when the pad numbers failed; None when that fails too.

    KiCad's ``TO-252-3_TabPin2`` numbers the tab 2 where JLC's DPAK drawing numbers
    it 3: the numbers differ but the schematic's ``D`` and the symbol's ``D`` are the
    same physical pin, so the part fits once the pads pair by function.
    """
    pairing = pair_by_function(kicad_pads, jlc_pads, symbol_pins)
    if pairing is None:
        return None
    placement = align(pairing.kicad, pairing.jlc, kicad_pads, jlc_pads)
    if placement.is_underdetermined or placement.is_mirrored:
        return None
    report = assess_fit(
        pairing.kicad,
        pairing.jlc,
        named_pads(kicad_pads),
        pairing.leftovers,
        placement,
        verdict.pad_count_kicad,
        verdict.pad_count_jlc,
    )
    if report.fit in ("count", "pitch"):
        return None
    verdict.take_placement(placement)
    verdict.take_fit(report)
    verdict.confidence = "medium"
    differences = ", ".join(
        f"JLC pin {jlc_number} ({function}) is pad {kicad_number} on the footprint"
        for function, kicad_number, jlc_number in pairing.differences
    )
    note = "pin numbers differ from JLC's part; paired by pin function"
    if differences:
        note += f": {differences}"
    if pairing.eliminated is not None:
        jlc_number, kicad_number = pairing.eliminated
        note += f"; JLC pin {jlc_number} and pad {kicad_number} paired by elimination"
    verdict.notes.append(note)
    return _finish_multi_pin(verdict, pairing.kicad, placement, by_name=False)


def _resolve_multi_pin(
    kicad_pads: list[Pad],
    jlc_pads: list[Pad],
    verdict: Verdict,
    symbol_pins: list[SymbolPin],
) -> Verdict:
    """Align by pad name, or by pin function when the names fail."""
    if min(verdict.pad_count_kicad, verdict.pad_count_jlc) < 2:
        return verdict.unresolved("unknown", "no_data", SINGLE_NUMBER_NOTE)
    kicad, jlc, jlc_rest = pair_by_name(kicad_pads, jlc_pads)
    if len(kicad) < 2:
        by_function = _resolve_by_function(kicad_pads, jlc_pads, verdict, symbol_pins)
        if by_function is not None:
            return by_function
        return verdict.unresolved(
            "unknown", "no_data", "fewer than two matching pad names"
        )
    placement = align(kicad, jlc, kicad_pads, jlc_pads)
    verdict.take_placement(placement)
    if placement.is_underdetermined:
        return verdict.unresolved("unknown", "no_data", "pad geometry is degenerate")
    if placement.is_mirrored:
        # A numbering that runs the other way is a by-number failure like any
        # other: the schematic's pin functions may still pair the pins.
        by_function = _resolve_by_function(kicad_pads, jlc_pads, verdict, symbol_pins)
        if by_function is not None:
            return by_function
        return verdict.unresolved(
            "red",
            "mirror",
            "pin order reversed: the KiCad footprint's pin numbering runs the opposite way to JLC's part",
        )
    report = assess_fit(
        kicad,
        jlc,
        named_pads(kicad_pads),
        jlc_rest,
        placement,
        verdict.pad_count_kicad,
        verdict.pad_count_jlc,
    )
    if report.fit in ("count", "pitch"):
        by_function = _resolve_by_function(kicad_pads, jlc_pads, verdict, symbol_pins)
        if by_function is not None:
            return by_function
        verdict.take_fit(report)
        kicad_named = named_pads(kicad_pads)
        jlc_named = named_pads(jlc_pads)
        # Only the numbering differs when the package lands every JLC pad and the two
        # sides have as many pads, counted by distinct number (a DPAK's tab and its stub
        # are one pad 2) or one by one (pads numbered 1, 2, 3, 4 on one side and
        # 1, 1, 2, 2 on the other).
        same_numbers = verdict.pad_count_kicad == verdict.pad_count_jlc
        if same_numbers or len(kicad_named) == len(jlc_named):
            angle, landed = shape_alignment(kicad_named, jlc_named)
            if landed == len(jlc_named):
                return verdict.unresolved(
                    "red",
                    "numbering",
                    "pin numbering differs from JLC's part; the package itself aligns at "
                    f"{ccw_correction(angle)}°",
                )
        return verdict.unresolved(
            "red", report.fit, _mismatch_note(verdict, report.fit)
        )
    verdict.take_fit(report)
    return _finish_multi_pin(verdict, kicad, placement)


def _two_pad_pairing(
    kicad: dict[str, Pad],
    jlc: dict[str, Pad],
    kicad_copper: Optional[list[Pad]] = None,
    jlc_copper: Optional[list[Pad]] = None,
) -> tuple[Placement, FitReport]:
    """Align two terminals on two by dict key and grade the fit, without touching a verdict.

    The terminals are the two pads, or :func:`_terminals` of a drawing that splits
    one.  ``kicad_copper`` and ``jlc_copper`` are then the real pads: each
    terminal's largest pad pairs, and the rest of a split terminal's pads are graded
    against the nearest KiCad copper, as on the multi-pin path.
    """
    placement = align(kicad, jlc)
    if kicad_copper is None or jlc_copper is None:
        return placement, assess_fit(
            kicad, jlc, list(kicad.values()), [], placement, 2, 2
        )
    real_k = {key: _largest(kicad_copper, pad.number) for key, pad in kicad.items()}
    real_j = {key: _largest(jlc_copper, pad.number) for key, pad in jlc.items()}
    chosen = {id(pad) for pad in real_j.values()}
    rest = [pad for pad in jlc_copper if id(pad) not in chosen]
    return placement, assess_fit(real_k, real_j, kicad_copper, rest, placement, 2, 2)


def _largest(pads: list[Pad], number: str) -> Pad:
    """Return the largest of the pads one number names, ties broken by the pads themselves."""
    return max(
        (pad for pad in pads if pad.number == number),
        key=lambda pad: (pad.width * pad.height, pad),
    )


def _terminals(pads: list[Pad]) -> list[Pad]:
    """Return one pad per number, so a drawing that splits a terminal is still two terminals.

    A number drawn as one pad is that pad.  A number drawn as several (a PLCC-4 LED's
    1, 1, 2, 2) is its largest pad moved to the number's :func:`~jlcfootprint.fit.anchor`,
    carrying the first pin function its pads give; it votes and aligns as one pad,
    and :func:`_two_pad_pairing` grades the real pads.
    """
    groups: dict[str, list[Pad]] = {}
    for pad in pads:
        groups.setdefault(pad.number, []).append(pad)
    terminals = []
    for number, members in groups.items():
        if len(members) == 1:
            terminals.append(members[0])
            continue
        x, y = anchor(members)
        function = next((pad.pin_function for pad in members if pad.pin_function), "")
        terminals.append(
            _largest(members, number)._replace(x=x, y=y, pin_function=function)
        )
    return terminals


def _swapped(jlc: dict[str, Pad]) -> dict[str, Pad]:
    """Return the other pairing of a two-pad alignment: JLC's two pads trade keys."""
    first, second = jlc
    return {first: jlc[second], second: jlc[first]}


def _two_pad_fit(report: FitReport, verdict: Verdict) -> bool:
    """Fill the fit for a two-pad alignment; return True when the part fits."""
    verdict.take_fit(report)
    if not fits(report):
        verdict.unresolved("red", "pitch", _mismatch_note(verdict, "pitch"))
        return False
    return True


def _reference_vote(
    jlc_named: list[Pad],
    package_name: str,
    symbol_pins: list[SymbolPin],
    marks: Optional[DrawingMarks],
    reference: str,
    notes: list[str],
) -> tuple[Optional[Pad], str, list[str], Optional[str], bool]:
    """Vote for the EasyEDA pad carrying ``reference`` and what pin 1 means there.

    The EasyEDA side's reference terminal has up to four sources: the name's FD/RD
    token, the symbol's pin-1 label, and the ``+`` marks of the symbol and of the
    footprint.  Each names a pad; the majority decides and a tie is inconsistent
    data.  On the two gate boards the marks agree with the truth on every
    polarized part they vote on, and they outvote the token once (C7171, a
    molded-chip tantalum) and the label once (D12).  Over EasyEDA's library the
    token agrees with the marks on nearly every diode and electrolytic can, but
    less often on the molded-chip tantalums.

    Returns ``(jlc_ref, source, outvoted, polarity, weak_token)``: the winning JLC
    pad and where it came from (``token``, ``label`` or ``drawing``); the
    names of the votes it outvoted; the pin-1 polarity the symbol's label
    implies (``None`` when the vote ties or nothing votes), which the caller
    replaces with what the vote says JLC's pad 1 is; and whether an
    unchallenged token is from the one family whose token is weak
    (:func:`jlcfootprint.polarity.token_is_weak`).  When nothing votes, or the
    vote ties, ``jlc_ref`` is ``None`` and ``source`` carries the verdict status
    to use instead -- the reason is already appended to ``notes``.
    """
    side = token_reference_side(package_name, reference)
    polarity = pin1_polarity(symbol_pins)
    token_pad = None
    if side is not None:
        token_pad = next((p for p in jlc_named if side_of(p, jlc_named) == side), None)
        if token_pad is None:
            notes.append("name token could not be applied: JLC pads are drawn vertical")
    label_pad = label_reference_pad(jlc_named, polarity, reference)
    symbol_drawn, footprint_drawn = drawing_reference_pads(jlc_named, marks, reference)
    votes: list[tuple[Pad, str]] = []
    if token_pad is not None:
        votes.append((token_pad, "name token"))
    if label_pad is not None:
        votes.append((label_pad, "symbol pin-1 label"))
    if symbol_drawn is not None:
        votes.append((symbol_drawn, "symbol + mark"))
    if footprint_drawn is not None:
        votes.append((footprint_drawn, "footprint + mark"))
    if not votes:
        notes.append(NO_VOTE_NOTE)
        return None, "unknown", [], None, False
    sides: dict[int, list[str]] = {}
    pads_by_id: dict[int, Pad] = {}
    for pad, name in votes:
        sides.setdefault(id(pad), []).append(name)
        pads_by_id[id(pad)] = pad
    ranked = sorted(sides.values(), key=len, reverse=True)
    if len(ranked) > 1 and len(ranked[0]) == len(ranked[1]):
        notes.append(
            "EasyEDA data inconsistent: "
            + " and ".join(", ".join(names) for names in ranked)
            + " disagree"
        )
        return None, "red", [], None, False
    winners = ranked[0]
    jlc_ref = next(pad for pad, name in votes if name == winners[0])
    outvoted = [name for pad, name in votes if pad is not jlc_ref]
    if outvoted:
        notes.append(
            f"{', '.join(outvoted)} {'names' if len(outvoted) == 1 else 'name'} the other pad; "
            f"{', '.join(winners)} {'decides' if len(winners) == 1 else 'decide'}"
        )
    if token_pad is jlc_ref:
        source = "token"
    elif label_pad is jlc_ref:
        source = "label"
    else:
        source = "drawing"
        if not outvoted:
            drawn_by = [name.split(" ")[0] for name in winners]  # symbol, footprint
            notes.append(DRAWING_NOTES["both" if len(drawn_by) == 2 else drawn_by[0]])
    # A token nothing checked, on the one family whose token is weak, is resolved at
    # medium confidence.
    weak_token = source == "token" and len(votes) == 1 and token_is_weak(package_name)
    return jlc_ref, source, outvoted, polarity, weak_token


def _resolve_polarized(
    kicad_named: list[Pad],
    jlc_named: list[Pad],
    verdict: Verdict,
    kind: PartKind,
    package_name: str,
    symbol_pins: list[SymbolPin],
    marks: Optional[DrawingMarks] = None,
    kicad_copper: Optional[list[Pad]] = None,
    jlc_copper: Optional[list[Pad]] = None,
) -> Verdict:
    """Align a polarized two-pad part by terminal meaning, never by pad number.

    The KiCad side's reference pad comes from its pin functions or, by convention,
    pad 1; :func:`_reference_vote` decides which EasyEDA pad is the reference and
    what its pin 1 means.  Once both reference pads are known the two pads align
    like any other two-pad part.  The pads are one per terminal (:func:`_terminals`);
    ``kicad_copper`` and ``jlc_copper`` are the real pads the fit is graded on.
    """
    if _coincident(kicad_named) or _coincident(jlc_named):
        return verdict.unresolved("unknown", "no_data", "pad geometry is degenerate")
    reference = kind.reference
    side = token_reference_side(package_name, reference)
    if side == "none":
        # A bidirectional TVS has no reference terminal; KiCad's D_TVS symbol names its
        # pins A1/A2, which would otherwise read as two anodes.
        verdict.notes.append("bidirectional part; aligned by axis")
        return _resolve_axis(kicad_named, jlc_named, verdict, kicad_copper, jlc_copper)
    kicad_ref, assumed, note = kicad_reference_pad(
        kicad_named, reference, kind.diode, kind.convention
    )
    if kicad_ref is None:
        return verdict.unresolved("unknown", "no_data", note)
    if note:
        verdict.notes.append(note)
    # A part of no polarized kind is here for its token alone, so only the token
    # votes: a label on it is a switch's A/B or a transistor's C/E, not a terminal.
    vote_notes: list[str] = []
    jlc_ref, source, outvoted, polarity, weak_token = _reference_vote(
        jlc_named,
        package_name,
        symbol_pins if kind.polarized else [],
        marks,
        reference,
        vote_notes,
    )
    if jlc_ref is None and not kind.polarized:
        # A token that cannot be applied (pads drawn upright) leaves the part to its
        # axis, as it would be without one.
        verdict.notes = [n for n in verdict.notes if n != NON_POLAR_TOKEN_NOTE]
        verdict.notes.extend(n for n in vote_notes if n != NO_VOTE_NOTE)
        return _resolve_axis(kicad_named, jlc_named, verdict, kicad_copper, jlc_copper)
    verdict.notes.extend(vote_notes)
    if jlc_ref is None:
        # _reference_vote already recorded why; source carries the status to use.
        verdict.status = source
        verdict.fit = "no_data"
        verdict.rotation = None
        verdict.method = "none"
        return verdict
    verdict.polarity_source = source
    if kind.polarized:
        # The vote settles which EasyEDA pad carries the reference terminal and with it
        # what JLC's pad 1 means, so the pin-1 light does not need a symbol label: the
        # token and the marks tell it too, and an outvoted label is overruled.
        jlc_pad1 = next((p for p in jlc_named if p.number == "1"), None)
        if jlc_pad1 is not None:
            polarity = "K" if (jlc_ref is jlc_pad1) == (reference == "cathode") else "A"
    kicad_other = next(p for p in kicad_named if p is not kicad_ref)
    jlc_other = next(p for p in jlc_named if p is not jlc_ref)
    kicad = {"ref": kicad_ref, "other": kicad_other}
    jlc = {"ref": jlc_ref, "other": jlc_other}
    placement, report = _two_pad_pairing(kicad, jlc, kicad_copper, jlc_copper)
    verdict.take_placement(placement)
    if placement.is_underdetermined:
        return verdict.unresolved("unknown", "no_data", "pad geometry is degenerate")
    # The meaning's own pairing must fit: a polarized part is never turned to fit by
    # shape, so a misfit stays red whatever the swapped pairing does.  When the swapped
    # pairing fits, the note says the polarity and the shapes disagree, so nobody goes
    # looking for a size mismatch.
    if not _two_pad_fit(report, verdict):
        _, swapped = _two_pad_pairing(kicad, _swapped(jlc), kicad_copper, jlc_copper)
        if fits(swapped):
            verdict.notes.append(
                "pad shapes fit the other way round (worst pad "
                f"{swapped.overlap_min * 100:.0f} %); the polarity says this way, so one "
                "footprint may draw its terminals at the other end"
            )
        return verdict
    verdict.rotation = ccw_correction(placement.rotation_deg)
    verdict.method = "polarity"
    verdict.confidence = (
        "medium" if assumed or outvoted or weak_token or source == "drawing" else "high"
    )
    if (
        source == "token"
        and verdict.name_rotation is not None
        and verdict.name_rotation != verdict.rotation
    ):
        # Only a token that was applied can accuse the footprint.  The name's angle
        # assumes KiCad's pad 1 is the reference terminal, so when the schematic puts
        # it on another pad the pin functions explain the difference, not the drawing.
        verdict.confidence = "medium"
        if kicad_ref.number != "1":
            verdict.notes.append(
                f"the schematic puts the {reference} on KiCad pad {kicad_ref.number}; "
                f"the name's {verdict.name_rotation}° assumes pad 1"
            )
        else:
            verdict.notes.append(
                f"KiCad footprint drawn non-standard; name says {verdict.name_rotation}°"
            )
    kicad_pin1 = pin1_meaning(kicad_named, kind)
    if polarity is None or kicad_pin1 is None:
        verdict.polarity_light = "unknown"
        verdict.status = "green"
    elif polarity == kicad_pin1:
        verdict.polarity_light = "green"
        verdict.status = "green"
    else:
        verdict.polarity_light = "yellow"
        verdict.status = "yellow"
        verdict.notes.append(YELLOW_NOTE)
    if _fits_with_caveat(verdict):
        verdict.status = "yellow"
    _, swapped = _two_pad_pairing(kicad, _swapped(jlc), kicad_copper, jlc_copper)
    if fits_clearly_better(swapped, report):
        # Polarity outranks shapes: the meaning's rotation and fit stand, in yellow,
        # and the note says the shapes disagree.
        verdict.status = "yellow"
        verdict.notes.append(
            "pad shapes fit the other way round better (worst pad "
            f"{swapped.overlap_min * 100:.0f} % against {report.overlap_min * 100:.0f} %); "
            "one footprint may draw its terminals at the other end"
        )
    return verdict


def _resolve_axis(
    kicad_named: list[Pad],
    jlc_named: list[Pad],
    verdict: Verdict,
    kicad_copper: Optional[list[Pad]] = None,
    jlc_copper: Optional[list[Pad]] = None,
) -> Verdict:
    """Align a non-polar two-pad part by axis, or by the one pairing its pads fit.

    Both pairings are graded, KiCad's two pads against JLC's and against JLC's
    swapped.  When one fits clearly better, its own placement is the alignment:
    a full turn, method ``shape``.  Otherwise 180 degrees is irrelevant and the
    axis alone gives the rotation, under 180, taken with the pairing that gives it.
    """
    if _coincident(kicad_named) or _coincident(jlc_named):
        return verdict.unresolved("unknown", "no_data", "pad geometry is degenerate")
    kicad = {"a": kicad_named[0], "b": kicad_named[1]}
    jlc = {"a": jlc_named[0], "b": jlc_named[1]}
    placement, report = _two_pad_pairing(kicad, jlc, kicad_copper, jlc_copper)
    if placement.is_underdetermined:
        verdict.take_placement(placement)
        return verdict.unresolved("unknown", "no_data", "pad geometry is degenerate")
    swapped_placement, swapped = _two_pad_pairing(
        kicad, _swapped(jlc), kicad_copper, jlc_copper
    )
    if fits_clearly_better(swapped, report):
        return _shape_alignment(verdict, swapped_placement, swapped)
    if fits_clearly_better(report, swapped):
        return _shape_alignment(verdict, placement, report)
    if ccw_correction(placement.rotation_deg) >= 180:
        # Neither pairing fits clearly better, so 180 degrees is irrelevant and the
        # rotation is the one under 180: keep the pairing that gives it, so that the
        # package origin derived from the placement belongs to the emitted rotation.
        placement, report = swapped_placement, swapped
    verdict.take_placement(placement)
    if not _two_pad_fit(report, verdict):
        return verdict
    verdict.rotation = ccw_correction(placement.rotation_deg)
    verdict.method = "axis"
    verdict.non_polar = True
    verdict.confidence = "high"
    verdict.status = "yellow" if _fits_with_caveat(verdict) else "green"
    return verdict


def _shape_alignment(
    verdict: Verdict, placement: Placement, report: FitReport
) -> Verdict:
    """Settle a non-polar two-pad part on the one pairing its pads fit.

    Nothing names a polarity, but the pads fit one way round clearly better than
    the other, so that way is the alignment and the rotation is a full turn.
    """
    verdict.take_placement(placement)
    verdict.take_fit(report)
    verdict.rotation = ccw_correction(placement.rotation_deg)
    verdict.method = "shape"
    verdict.non_polar = True
    verdict.confidence = "high"
    verdict.notes.append(SHAPE_NOTE)
    verdict.status = "yellow" if _fits_with_caveat(verdict) else "green"
    return verdict


def _coincident(pads: list[Pad]) -> bool:
    """Return True when the pads share one position, so no axis can be drawn through them."""
    return len({(round(pad.x, 6), round(pad.y, 6)) for pad in pads}) < 2


def _finite(pads: list[Pad]) -> bool:
    """Return True when every pad coordinate, size and angle is a finite number."""
    return all(
        math.isfinite(value)
        for pad in pads
        for value in (pad.x, pad.y, pad.width, pad.height, pad.rotation)
    )


def body_threshold_mm(courtyard: tuple[float, float, float, float]) -> float:
    """Return how far the JLC body may overhang this courtyard before the caveat."""
    shorter = min(courtyard[2] - courtyard[0], courtyard[3] - courtyard[1])
    return min(
        max(BODY_EXCESS_FRACTION * shorter, BODY_EXCESS_MIN_MM), BODY_EXCESS_MAX_MM
    )


def _body_caveat(
    verdict: Verdict,
    courtyard: tuple[float, float, float, float],
    body: tuple[float, float, float, float],
) -> None:
    """Note a JLC body that overhangs the KiCad courtyard.

    The status and the rotation stand: the pads fit, the part is only bigger than
    the footprint's author allowed for, which the preview shows.

    A courtyard with a zero-length side measures nothing (a footprint whose
    courtyard is a single line, or a box read from one degenerate graphic), so it
    raises no caveat instead of flagging the whole body as overhang.
    """
    if verdict.placement is None:
        return
    if courtyard[2] - courtyard[0] <= 0 or courtyard[3] - courtyard[1] <= 0:
        return
    excess = courtyard_excess(courtyard, body, verdict.placement)
    if excess <= body_threshold_mm(courtyard):
        return
    verdict.body_excess_mm = round(excess, 2)
    verdict.notes.append(
        f"fits, JLC body {excess:.1f} mm larger than the KiCad courtyard"
    )


def resolve(
    kicad_pads: list[Pad],
    kicad_footprint_name: str,
    record_status: str,
    package_name: str,
    jlc_pads: list[Pad],
    symbol_pins: list[SymbolPin],
    marks: Optional[DrawingMarks] = None,
    kicad_courtyard: Optional[tuple[float, float, float, float]] = None,
) -> Verdict:
    """Return the verdict for one part.

    ``kicad_pads`` are in the footprint's own frame (bottom-side parts already
    un-mirrored by the caller); ``jlc_pads`` are millimetres in KiCad's frame.
    ``marks`` are the drawings' ``+`` marks and the JLC body box
    (:func:`jlcfootprint.drawing.drawing_marks`) and ``kicad_courtyard`` the
    footprint's courtyard box, both optional; together they add the drawings'
    votes on polarity and the body-size caveat.
    """
    verdict = Verdict()
    if record_status == "none":
        return verdict.unresolved("unknown", "no_data", CHECKERBOARD_NOTE)
    if record_status != "ok":
        return verdict.unresolved(
            "unknown", "no_data", "EasyEDA fetch failed; will retry"
        )
    if not _finite(kicad_pads) or not _finite(jlc_pads):
        return verdict.unresolved(
            "unknown", "no_data", "a pad has a non-numeric position or size"
        )
    kicad_named = named_pads(kicad_pads)
    jlc_named = named_pads(jlc_pads)
    # Counts compare distinct numbers: a DPAK's tab and its stub are one pad 2.
    verdict.pad_count_kicad = len({pad.number for pad in kicad_named})
    verdict.pad_count_jlc = len({pad.number for pad in jlc_named})
    kind = part_kind(package_name, kicad_footprint_name, kicad_pads, symbol_pins, marks)
    parsed = parse_package_name(package_name, True if kind.diode else None)
    if parsed.rotation_source == "naming_rule":
        verdict.name_rotation = naming_rule_to_cpl(parsed.rotation_correction)
    if min(len(kicad_named), len(jlc_named)) < 2:
        return verdict.unresolved("unknown", "no_data", SINGLE_PAD_NOTE)
    # A polarized kind (a drawn + among its reasons) or an FD/RD token sends a two-pad
    # part to alignment by meaning.
    marked = kind.polarized or token_reference_side(package_name, "positive") in (
        "left",
        "right",
    )
    # Two terminals are two numbers, however many pads a drawing splits them into.
    if verdict.pad_count_jlc == 2 and marked:
        kicad_terminals = _terminals(kicad_named)
        if len(kicad_terminals) != 2:
            # A two-terminal JLC part on a KiCad footprint with extra pads: align by
            # meaning on the two KiCad pads that share the part's pad names, never by number.
            kicad_terminals = [
                pad
                for pad in kicad_terminals
                if pad.number in {p.number for p in jlc_named}
            ]
            if len(kicad_terminals) != 2:
                return verdict.unresolved(
                    "unknown",
                    "no_data",
                    "two-terminal part on a footprint whose pads do not pair with it",
                )
        jlc_terminals = _terminals(jlc_named)
        terminal_numbers = {pad.number for pad in kicad_terminals}
        kicad_copper = [pad for pad in kicad_named if pad.number in terminal_numbers]
        split = len(kicad_copper) > 2 or len(jlc_named) > 2
        if split and (_coincident(kicad_terminals) or _coincident(jlc_terminals)):
            # One terminal round the other (a coin cell's + tabs either side of its -
            # pad): meaning gives no axis, but the pads' layout fixes the turn, and a
            # numbering that swapped the terminals would not fit.
            verdict.notes.append(SHARED_CENTRE_NOTE)
            return _finish_resolve(
                _resolve_multi_pin(kicad_pads, jlc_pads, verdict, symbol_pins),
                kicad_courtyard,
                marks,
            )
        if not kind.polarized:
            verdict.non_polar = True
            verdict.notes.append(NON_POLAR_TOKEN_NOTE)
        _resolve_polarized(
            kicad_terminals,
            jlc_terminals,
            verdict,
            kind,
            package_name,
            symbol_pins,
            marks,
            kicad_copper,
            jlc_named,
        )
    elif (
        len(kicad_named) == 2
        and len(jlc_named) == 2
        and verdict.pad_count_kicad == verdict.pad_count_jlc == 2
    ):
        # Two pads of one number are not two terminals: the multi-pin path calls
        # them unknown, as it does a four-legged standoff.
        _resolve_axis(kicad_named, jlc_named, verdict)
    else:
        _resolve_multi_pin(kicad_pads, jlc_pads, verdict, symbol_pins)
    return _finish_resolve(verdict, kicad_courtyard, marks)


def _finish_resolve(
    verdict: Verdict,
    kicad_courtyard: Optional[tuple[float, float, float, float]],
    marks: Optional[DrawingMarks],
) -> Verdict:
    """Add the body caveat to a verdict that has a rotation, and return it."""
    if (
        verdict.rotation is not None
        and kicad_courtyard is not None
        and marks is not None
        and marks.body_box is not None
    ):
        _body_caveat(verdict, kicad_courtyard, marks.body_box)
    return verdict
