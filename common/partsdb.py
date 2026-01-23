"""Parts database generation and management."""

from datetime import date, datetime
import os
from pathlib import Path
import sqlite3
from typing import Any

import humanize

from .componentdb import ComponentsDatabase
from .filemgr import FileManager
from .progress import NestedProgressBar
from .translate import ComponentTranslator

# Register date adapter to avoid deprecation warning
# See: https://docs.python.org/3/library/sqlite3.html#sqlite3-adapter-converter-recipes


def _adapt_date(val: date) -> str:
    """Adapt datetime.date to ISO format string for SQLite storage."""
    return val.isoformat()


def _convert_date(val: bytes) -> date:
    """Convert ISO format string from SQLite back to datetime.date."""
    return date.fromisoformat(val.decode())


sqlite3.register_adapter(date, _adapt_date)
sqlite3.register_converter("date", _convert_date)

_CREATE_STATEMENTS = [
    """
    CREATE virtual TABLE IF NOT EXISTS parts using fts5 (
        'LCSC Part',
        'First Category',
        'Second Category',
        'MFR.Part',
        'Package',
        'Solder Joint' unindexed,
        'Manufacturer',
        'Library Type',
        'Description',
        'Datasheet' unindexed,
        'Price' unindexed,
        'Stock' unindexed
    , tokenize="trigram")
    """,
    """
    CREATE TABLE IF NOT EXISTS mapping (
        'footprint',
        'value',
        'LCSC'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meta (
        'filename',
        'size',
        'partcount',
        'date',
        'last_update'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS categories (
        'First Category',
        'Second Category'
    )
    """,
]


class Generate:
    """Orchestrates the generation of parts database from component data.

    This class manages the workflow of fetching components from a component database,
    translating them to parts database format, and reporting statistics.
    """

    def __init__(
        self,
        componentdb: "ComponentsDatabase",
        partsdb: "PartsDatabase",
        progress: "NestedProgressBar",
        translator: ComponentTranslator | None = None,
    ):
        """Initialize the generator.

        Args:
            componentdb: ComponentsDatabase instance to fetch components from
            partsdb: PartsDatabase instance to insert translated parts into
            translator: Optional pre-configured ComponentTranslator. If not provided,
                       one will be created from the componentdb lookup tables.
            progress: Optional NestedProgressBar for progress reporting. If not provided,
                     progress will be printed to console.

        """
        self.componentdb = componentdb
        self.partsdb = partsdb
        self.translator = translator
        self.progress = progress
        self.total_components = 0
        self.loaded_components = 0

    def generate(self, where_clause: str = "") -> None:
        """Generate parts database by translating components.

        Fetches components from the component database in batches, translates them,
        and inserts them into the parts database. Statistics are tracked and reported.

        Args:
            where_clause: Optional SQL WHERE clause to filter components.

        """
        # Initialize translator if not provided
        if self.translator is None:
            manufacturers = self.componentdb.get_manufacturers()
            categories = self.componentdb.get_categories()
            self.translator = ComponentTranslator(manufacturers, categories)

        total_components = self.componentdb.count_components(where_clause=where_clause)
        self.total_components = total_components
        print(
            f"Translating {humanize.intcomma(total_components)} components to parts database"
        )

        # Use progress bar if provided, otherwise just iterate
        with self.progress.outer(total_components, "Translating components") as pbar:  # type: ignore
            self._process_batches(where_clause, pbar)

        self.partsdb.post_build()
        print("Done importing parts")

    def _process_batches(self, where_clause: str, pbar=None) -> None:
        """Process component batches.

        Args:
            where_clause: SQL WHERE clause to filter components.
            pbar: Optional progress bar callback for updating outer bar.

        """
        batch_count = 0
        for batch in self.componentdb.fetch_components(where_clause=where_clause):
            batch_count += 1

            # Translate rows
            translated_rows = []
            for component_row in batch:
                translated_row = self.translator.translate(component_row)  # type: ignore
                translated_rows.append(translated_row)
            self.loaded_components += len(batch)

            if pbar is None:
                print(
                    f"Processed {humanize.intcomma(self.loaded_components)} / "
                    + f"{humanize.intcomma(self.total_components)} components"
                )
            else:
                pbar.update(len(batch))

            # Insert into parts database
            self.partsdb.update_parts(translated_rows)

    def report_stats(self) -> None:
        """Report statistics from the translation process."""
        if self.translator is None:
            print("No data processed yet")
            return

        (
            price_entries_total,
            price_entries_deleted_total,
            price_entries_duplicates_deleted_total,
        ) = self.translator.get_statistics()
        print("Translation Statistics:")
        print(f"Total price entries processed: {price_entries_total}")
        print(
            f"Price value filtering trimmed {price_entries_deleted_total} (including {price_entries_duplicates_deleted_total} duplicates) out of {price_entries_total} entries {(price_entries_deleted_total / price_entries_total) * 100 if price_entries_total != 0 else 0:.2f}%"
        )


class PartsDatabase:
    """Manages generation of parts database from component data."""

    def __init__(
        self,
        output_db: Path,
        archive_dir: Path,
        chunk_num: Path = Path("chunk_num_fts5.txt"),
        skip_cleanup: bool = False,
    ):
        """Initialize the parts database manager.

        Args:
            output_db: Path where the output database will be written
            archive_dir: Directory where split archive files will be stored
            chunk_num: Path to chunk number sentinel file
            jlcparts_db_name: Name of the source jlcparts database
            skip_cleanup: If True, don't delete temporary files after splitting

        """
        self.output_db = output_db
        self.archive_dir = archive_dir
        self.chunk_num = chunk_num
        self.skip_cleanup = skip_cleanup

        self.remove_original()
        self.conn = sqlite3.connect(self.output_db)
        self.part_count = 0
        self.create_tables()

    def remove_original(self):
        """Remove the original output database."""
        if self.output_db.exists():
            self.output_db.unlink()

    def close_sqlite(self):
        """Close sqlite connections."""
        self.conn.close()

    def create_tables(self):
        """Create tables."""
        for stmt in _CREATE_STATEMENTS:
            self.conn.execute(stmt)
        self.conn.commit()

    def update_parts(self, rows: list[dict[str, Any]]) -> None:
        """Update parts database with a list of component rows.

        Args:
            rows: List of sqlite3.Row objects from components table
            translator: ComponentTranslator to use for translating rows

        """
        if not rows:
            return

        data = rows[0]
        columns = ", ".join([f'"{k}"' for k in data])
        placeholders = ", ".join(
            [f":{k.replace(' ', '_').replace('.', '_')}" for k in data]
        )
        newrows = [
            {k.replace(" ", "_").replace(".", "_"): v for k, v in row.items()}
            for row in rows
        ]
        self.conn.executemany(
            f"INSERT INTO parts ({columns}) VALUES ({placeholders})", newrows
        )
        self.conn.commit()

        self.part_count += len(rows)

    def populate_categories(self):
        """Populate the categories table."""
        self.conn.execute(
            'INSERT INTO categories SELECT DISTINCT "First Category", "Second Category" FROM parts ORDER BY UPPER("First Category"), UPPER("Second Category")'
        )

    def optimize(self):
        """FTS5 optimize to minimize query times."""
        print("Optimizing fts5 parts table")
        self.conn.execute("insert into parts(parts) values('optimize')")
        print("Done optimizing fts5 parts table")

    def meta_data(self):
        """Populate the metadata table."""
        db_size = os.stat(self.output_db).st_size
        self.conn.execute(
            "INSERT INTO meta VALUES(?, ?, ?, ?, ?)",
            [
                "cache.sqlite3",
                db_size,
                self.part_count,
                date.today().isoformat(),
                datetime.now().isoformat(),
            ],
        )
        self.conn.commit()

    def split(self):
        """Split the compressed database so we stay below GitHub's 100MB limit.

        Uses FileManager to split the file and create a sentinel file.
        This maintains compatibility with the previous output format.
        """
        file_manager = FileManager(
            file_path=self.output_db,
            chunk_size=80000000,  # 80 MB to stay well below GitHub's 100MB limit
            sentinel_filename=str(self.chunk_num),
        )
        file_manager.compress_and_split(output_dir=self.archive_dir)

    def cleanup(self):
        """Remove the compressed zip file and output db after splitting."""
        print(f"Deleting {self.output_db}")
        os.unlink(self.output_db)

    def post_build(self):
        """Actions to perform after building the database."""
        print(
            f"Generated parts database with {humanize.intcomma(self.part_count)} parts"
        )
        self.optimize()
        self.meta_data
        self.close_sqlite()
        self.split()
        if self.skip_cleanup:
            print("Skipping cleanup")
        else:
            self.cleanup()
