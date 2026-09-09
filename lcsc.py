"""Canonical handling of LCSC part numbers.

:class:`Lcsc` is the preferred form: a value that has been through it is a real
part number in canonical form, which a ``str`` never tells you. The plain
string helpers are the same answers rendered as strings, for the boundaries
that must hand one to sqlite, wx or a CSV writer.

Absence is ``None``, never an empty :class:`Lcsc`, and there is no
``.valid()`` -- a part in hand is always valid. The empty string means "no
part" only at the edges, and :func:`format_lcsc` is where that conversion
happens.
"""

from dataclasses import dataclass
import re
from typing import Optional

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

    This is the string-level form: :meth:`Lcsc.parse` asks the same question
    and answers with the part itself.
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

    This is the one search of the pattern in text; :meth:`Lcsc.find_in` is
    this function with the result handed back as a part.
    """
    if not text:
        return ""
    match = _PART_NUMBER.search(str(text).upper())
    return match.group(0) if match else ""


@dataclass(frozen=True)
class Lcsc:
    """A JLCPCB/LCSC part number, known to be well formed and canonical.

    Construct one through :meth:`parse` or :meth:`find_in`, which answer
    ``None`` when the value does not name a part. The constructor rejects
    anything that is not a part number, so an ``Lcsc`` in hand needs no
    further checking. It is frozen, so it can be a dict key, and it renders as
    the bare number, so it can be formatted straight into a query, a CSV cell
    or a log line.

    Well formed means exactly what :func:`is_lcsc_part` means -- there is one
    definition of the shape, and the type is built on it rather than beside
    it, so a value the predicate accepts always builds a part and one it
    rejects never does.

    Not ordered: part numbers identify rather than measure. Sort with an
    explicit key if a display ever needs one.

        >>> part = Lcsc.parse(" c12345 ")
        >>> str(part)
        'C12345'
        >>> f"ordering {part}"
        'ordering C12345'
    """

    value: str

    def __post_init__(self):
        """Reject anything that is not a canonical part number."""
        canonical = normalize_lcsc(self.value)
        if not is_lcsc_part(canonical):
            raise ValueError(f"not an LCSC part number: {self.value!r}")
        # frozen dataclasses refuse plain assignment, even from __post_init__
        object.__setattr__(self, "value", canonical)

    @classmethod
    def parse(cls, value) -> Optional["Lcsc"]:
        """Return the part this value names, or None if it names none.

        Use this wherever a value is *claimed* to be a part number -- a
        schematic field, a database column -- and the claim has to be checked.
        """
        try:
            return cls(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def find_in(cls, text) -> Optional["Lcsc"]:
        """Return the first part number appearing anywhere in text.

        Use this for text a person pasted, where the number arrives with
        whatever surrounded it. It is lenient about the surroundings only: a
        value that is not a part number does not become one by being pasted
        into the right box.
        """
        found = extract_lcsc(text)
        return cls(found) if found else None

    def __str__(self) -> str:
        """Render as the bare canonical part number."""
        return self.value


def format_lcsc(part) -> str:
    """Render a part, or the absence of one, for something that wants a string.

    The boundary between ``None`` inside the plugin and the empty string that
    sqlite columns, CSV cells and wx list cells use for the same thing.
    """
    return str(part) if part is not None else ""
