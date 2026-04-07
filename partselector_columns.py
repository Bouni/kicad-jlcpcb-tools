"""Shared column definitions for the part selector UI and queries."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PartSelectorColumn:
    """Column metadata for the part selector list."""

    key: str
    label: str
    db_field: str | None
    width: int
    align: str
    sortable: bool = True
    model_type: str = "string"


PARTSELECTOR_COLUMNS: list[PartSelectorColumn] = [
    PartSelectorColumn("lcsc", "LCSC", "LCSC Part", 60, "center"),
    PartSelectorColumn("mfr_number", "MFR Number", "MFR.Part", 140, "left"),
    PartSelectorColumn("package", "Package", "Package", 100, "left"),
    PartSelectorColumn("type", "Type", "Library Type", 50, "left"),
    PartSelectorColumn("params", "Params", None, 150, "center", sortable=False),
    PartSelectorColumn("stock", "Stock", "Stock", 50, "center"),
    PartSelectorColumn("mfr", "Manufacturer", "Manufacturer", 100, "left"),
    PartSelectorColumn("description", "Description", "Description", 300, "left"),
    PartSelectorColumn("price", "Price", "Price", 100, "left"),
]

EXTRA_DB_FIELDS: list[str] = ["First Category"]

DB_FIELDS: list[str] = [
    c.db_field for c in PARTSELECTOR_COLUMNS if c.db_field is not None
] + EXTRA_DB_FIELDS

COLUMN_INDEX: dict[str, int] = {
    column.key: idx for idx, column in enumerate(PARTSELECTOR_COLUMNS)
}

SORTABLE_COLUMN_INDEX_TO_DB: dict[int, str] = {
    idx: column.db_field
    for idx, column in enumerate(PARTSELECTOR_COLUMNS)
    if column.sortable and column.db_field is not None
}

MODEL_COLUMN_TYPES: list[str] = [column.model_type for column in PARTSELECTOR_COLUMNS]

PARAMS_COLUMN_KEY = "params"
