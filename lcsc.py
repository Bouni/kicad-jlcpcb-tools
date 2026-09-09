"""Canonical handling of LCSC part numbers."""

import re

# No anchors, so the same pattern answers both questions below: matched in
# full for "is this a part number", searched for in "is there one in this
# text". The digits are spelled out as [0-9] rather than \d because \d also
# admits digits from other scripts, which LCSC does not issue and which the
# part-preference validator this module absorbs has always rejected.
_PART_NUMBER = re.compile(r"C[0-9]+")


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
    return bool(_PART_NUMBER.fullmatch(normalize_lcsc(value)))


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

    The result is canonical, so what is pasted and what is stored agree.
    """
    if not text:
        return ""
    match = _PART_NUMBER.search(str(text).upper())
    return match.group(0) if match else ""
