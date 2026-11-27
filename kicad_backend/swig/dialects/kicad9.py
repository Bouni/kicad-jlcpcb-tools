"""KiCad 9: the built-in fields no longer include Footprint."""

from .base import Dialect


class KiCad9(Dialect):
    """KiCad 9's footprints."""

    major = 9
    builtin_fields = ("Reference", "Value", "Datasheet", "Description")
    holds_dnp = True
    keeps_repeated_fields = True
