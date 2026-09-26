"""Canonical handling of LCSC part numbers."""

import re

# A C and a run of decimal digits from any script, so a number is always read
# whole: "C123４" is one malformed token, never C123 with something after it.
# LCSC issues ASCII digits only, and the part-preference validator this module
# absorbs has always rejected the rest, so only an ASCII token is a part
# number. No anchors, so the same pattern answers both questions below:
# matched in full for "is this a part number", searched for in "is there one
# in this text".
_PART_NUMBER = re.compile(r"C\d+")


def normalize_lcsc(value):
    """Return an LCSC part number in canonical form for keying and comparison.

    Part numbers reach the plugin from a footprint field, a pasted clipboard
    string, the parts database and saved part preferences. The current entry
    points canonicalise what they accept, but a project database written by an
    earlier release can still hold a number as it was typed then, and nothing
    rewrites stored rows. Corrections are keyed exactly, so every key and every
    lookup passes through here or a rule silently never fires.
    """
    if not value:
        return ""
    return str(value).strip().upper()


def is_lcsc_part(value):
    """Report whether a value names an LCSC part number.

    The value is normalised first, so a number typed into a schematic field
    with a stray space or in lower case still reads as the part it names.
    Testing raw text against a bare C-plus-digits pattern rejects exactly the
    values issue #773 is about, so the test belongs beside the normaliser and
    every caller gets the same answer.

    It is strict on purpose: the value has to be a part number and nothing
    else. Text that merely contains one is :func:`extract_lcsc`'s business.
    """
    value = normalize_lcsc(value)
    return bool(_PART_NUMBER.fullmatch(value)) and value.isascii()


def extract_lcsc(text):
    """Return the first LCSC part number appearing anywhere in text.

    This reads what a person pasted, so it is lenient where
    :func:`is_lcsc_part` is strict: a number copied out of a web page or a
    parts table arrives surrounded by whatever came with it, and the number is
    still the point. A schematic field claiming to be a part number gets no
    such benefit of the doubt, which is why the two are separate functions
    rather than one with a flag.

    It takes the first C-plus-digits run it finds, so text that leads with a
    capacitor designator (a parts-table row beginning with C1, say) yields
    the designator rather than the part. No real LCSC number has fewer than
    four digits, but shorter values are accepted everywhere else in the
    plugin and by the tests over this path, so no floor is applied here.

    The run is read whole and then judged by :func:`is_lcsc_part`, so a run
    with a digit from another script in it ("C12345６") yields nothing rather
    than its ASCII prefix, which would name a different part. A superscript
    is not a decimal digit and ends the run, so a footnote marker ("C123¹")
    does not spoil the number before it.

    The result is canonical, so what is pasted and what is stored agree.
    """
    if not text:
        return ""
    match = _PART_NUMBER.search(str(text).upper())
    token = match.group(0) if match else ""
    return token if is_lcsc_part(token) else ""


# Text typed or pasted into the Enter LCSC prompt names a part only where the
# number stands alone: no letter or digit touches it, so the C0603 inside
# RC0603FR or C0603C104K5RACTU is never read as a part, while the "_" or "/" a
# product link puts before it is fine. As with _PART_NUMBER, the digit run is
# read whole and then judged by is_lcsc_part.
_STANDALONE_PART_NUMBER = re.compile(r"(?<![^\W_])C\d+(?![^\W_])", re.IGNORECASE)


def parse_lcsc_entry(text):
    """Return the one LCSC part number in typed or pasted text, or "".

    The Enter LCSC prompt accepts a bare number and also a product link copied
    from lcsc.com or jlcpcb.com, so the number is searched for rather than
    matched against the whole text. It is stricter than :func:`extract_lcsc`
    because the prompt shows what it read and waits for OK: the number has to
    stand alone, and text naming two different numbers is ambiguous and yields
    nothing, so the prompt never guesses which was meant.
    """
    found = {
        normalize_lcsc(match)
        for match in _STANDALONE_PART_NUMBER.findall(str(text or ""))
    }
    if len(found) != 1:
        return ""
    (lcsc,) = found
    return lcsc if is_lcsc_part(lcsc) else ""
