"""KiCad 10: the same footprint fields as KiCad 9."""

from .base import Dialect


class KiCad10(Dialect):
    """KiCad 10's footprints."""

    major = 10
    builtin_fields = ("Reference", "Value", "Datasheet", "Description")
    holds_dnp = True
    keeps_repeated_fields = True
