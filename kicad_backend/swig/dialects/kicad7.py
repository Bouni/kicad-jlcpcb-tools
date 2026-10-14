"""KiCad 7: Reference and Value stand apart, and the other fields are a property map.

The map holds one text per name and is never drawn. A footprint has no DNP
flag.
"""

from typing import Any

from ...records import FieldRecord
from .base import Dialect


class KiCad7(Dialect):
    """KiCad 7's footprints."""

    major = 7
    builtin_fields = ("Reference", "Value")
    holds_dnp = False
    keeps_repeated_fields = False

    def fields(self, footprint: Any) -> list[FieldRecord]:
        """Read Reference and Value, then the property map."""
        return [
            FieldRecord("Reference", str(footprint.GetReference())),
            FieldRecord("Value", str(footprint.GetValue())),
            *(
                FieldRecord(str(name), str(text))
                for name, text in footprint.GetProperties().items()
            ),
        ]

    def is_dnp(self, footprint: Any) -> bool:
        """Report every footprint fitted: KiCad 7 has no DNP flag."""
        return False
