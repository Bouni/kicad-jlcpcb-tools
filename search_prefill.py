"""The search the part selector opens with, for one footprint on the board.

There are two ways into the selector -- the main footprint list, and the
variant matrix whenever the board defines design variants -- and both open it
on the same search: the value in the catalog's spelling, then the package the
footprint names.  Building that text in one place is what keeps the two from
drifting apart.
"""

from .dataview_highlight import simplify_footprint_name
from .value_normalize import canonicalize, quantity_for_reference


def prefill_search(reference: str, value: str, footprint: str) -> str:
    """Return the search text the part selector opens with for one footprint.

    ``C1`` valued ``0.1uF`` on ``Capacitor_SMD:C_0603_1608Metric`` gives
    ``100nF 0603``, so a board value of 0.1uF, 4k7 or 100p finds the catalog's
    100nF, 4.7kΩ and 100pF.  The reference designator says which quantity the
    value measures; when it says nothing useful, or the value does not parse,
    the value is passed through untouched.  The footprint adds its package
    designator, or nothing when the name carries none.
    """
    canonical = canonicalize(value, quantity_for_reference(reference))
    text = value if canonical is None else canonical
    package = simplify_footprint_name(footprint)
    return f"{text} {package}" if package else text
