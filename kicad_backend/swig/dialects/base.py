"""What the KiCad majors with field objects share: KiCad 8 and later.

A major's module states its own facts and overrides only what its version
does differently. The operations take live SWIG objects, which stay inside
``kicad_backend.swig``.
"""

from typing import Any

from ...records import FieldRecord


class Dialect:
    """One KiCad major's facts, and the operations that differ between majors.

    The facts say what a footprint holds in this major:

    - ``major``: the KiCad major.
    - ``builtin_fields``: the fields every footprint has, in KiCad's order.
    - ``holds_dnp``: whether a footprint has a DNP flag.
    - ``keeps_repeated_fields``: whether a footprint keeps two fields with
      one name.
    """

    major: int
    builtin_fields: tuple[str, ...]
    holds_dnp: bool
    keeps_repeated_fields: bool

    def fields(self, footprint: Any) -> list[FieldRecord]:
        """Read a footprint's fields in KiCad's order, every one of them.

        KiCad's footprint dialog accepts a second field with a name the
        footprint already has, and the footprint keeps both until the board
        is reopened; the state built from them resolves the name as
        reopening does.
        """
        return [
            FieldRecord(str(field.GetName()), str(field.GetText()))
            for field in footprint.GetFields()
        ]

    def is_dnp(self, footprint: Any) -> bool:
        """Read the footprint's DNP flag."""
        return bool(footprint.IsDNP())
