"""Constants used througout the plugin."""

from enum import IntEnum


class Column(IntEnum):
    """Column positions for main parts table."""

    REFERENCE = 0
    VALUE = 1
    FOOTPRINT = 2
    LCSC = 3
    TYPE = 4
    STOCK = 5
    BOM = 6
    POS = 7
    ROTATION = 8
    SIDE = 9
