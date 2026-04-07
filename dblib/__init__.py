"""Database configuration library for parts database generation.

This module provides database configuration classes and constants for managing
different parts database variants without pulling in heavy dependencies.
"""

import time
from typing import NamedTuple


class PartDatabaseConfig(NamedTuple):
    """Configuration for part database generation."""

    name: str
    chunk_file_name: str
    where_clause: str
    display_name: str
    populate_preferred: bool = False


class DatabaseConfig:
    """Predefined database configurations."""

    @staticmethod
    def preferredAndBasic() -> PartDatabaseConfig:
        """Select only preferred and basic parts."""
        return PartDatabaseConfig(
            name="basic-parts-fts5.db",
            chunk_file_name="chunk_num_basic_parts_fts5.txt",
            where_clause="basic = 1 OR preferred = 1",
            display_name="Basic + Preferred Library",
            populate_preferred=True,
        )

    @staticmethod
    def allParts() -> PartDatabaseConfig:
        """Select all parts.

        This is the most backwards-compatible database, and therefore uses
        the default "parts-fts5.db" name.
        """
        return PartDatabaseConfig(
            name="parts-fts5.db",
            chunk_file_name="chunk_num_fts5.txt",
            where_clause="TRUE",
            display_name="Full Library - All Parts",
            populate_preferred=False,
        )

    @staticmethod
    def ignoreObsoleteParts(obsolete_threshold_days: int = 365) -> PartDatabaseConfig:
        """Select all parts except obsolete parts."""
        filter_seconds = int(time.time()) - obsolete_threshold_days * 24 * 60 * 60
        return PartDatabaseConfig(
            name="current-parts-fts5.db",
            chunk_file_name="chunk_num_current_parts_fts5.txt",
            where_clause=f"NOT (stock = 0 AND last_on_stock < {filter_seconds})",
            display_name="Current Parts (Exclude Obsolete)",
            populate_preferred=True,
        )

    @staticmethod
    def emptyParts() -> PartDatabaseConfig:
        """Select no parts."""
        return PartDatabaseConfig(
            name="empty-parts-fts5.db",
            chunk_file_name="chunk_num_empty_parts_fts5.txt",
            where_clause="FALSE",
            display_name="Empty Library - No parts!",
        )


# Library configuration mapping: defines available library options
LIBRARY_CONFIGS = {
    "all-parts": DatabaseConfig.allParts(),
    "basic-preferred": DatabaseConfig.preferredAndBasic(),
    "current-parts": DatabaseConfig.ignoreObsoleteParts(),
    "empty": DatabaseConfig.emptyParts(),
}

DEFAULT_LIBRARY = "current-parts"


__all__ = [
    "DatabaseConfig",
    "DEFAULT_LIBRARY",
    "LIBRARY_CONFIGS",
    "PartDatabaseConfig",
]
