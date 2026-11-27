"""KiCad 8: a built-in Footprint field."""

from .base import Dialect


class KiCad8(Dialect):
    """KiCad 8's footprints."""

    major = 8
    builtin_fields = ("Reference", "Value", "Footprint", "Datasheet", "Description")
    holds_dnp = True
    keeps_repeated_fields = True
