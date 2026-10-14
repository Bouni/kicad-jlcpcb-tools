"""What differs between the KiCad majors the SWIG backend supports.

Each major keeps a footprint's data a little differently. Its module holds
that version's facts and the few operations that differ; what the versions
share lives once, in ``base``. The backend picks one dialect when it is
made, and its readers and writers call the dialect instead of probing the
bindings.

Dropping a major is deleting its module and its entry in ``_BY_MAJOR``.
"""

from typing import Any

from ... import Unsupported
from .base import Dialect
from .kicad7 import KiCad7
from .kicad8 import KiCad8
from .kicad9 import KiCad9
from .kicad10 import KiCad10

_BY_MAJOR: dict[int, type[Dialect]] = {
    dialect.major: dialect for dialect in (KiCad7, KiCad8, KiCad9, KiCad10)
}


def select(pcbnew: Any) -> Dialect:
    """Return the dialect of the KiCad that ``pcbnew`` belongs to, by its major.

    A development build reports the major it follows: KiCad 11's nightlies
    report 10.99, and get KiCad 10's dialect. A major with no dialect raises
    Unsupported, naming the supported range.
    """
    version = str(pcbnew.GetMajorMinorVersion())
    major = version.split(".", 1)[0]
    dialect = _BY_MAJOR.get(int(major)) if major.isdigit() else None
    if dialect is None:
        raise Unsupported(
            f"JLCPCB Tools works with KiCad {min(_BY_MAJOR)} to {max(_BY_MAJOR)}; "
            f"this is KiCad {version}."
        )
    return dialect()


__all__ = ["Dialect", "select"]
