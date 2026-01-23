"""Component database management."""

from collections.abc import Generator
import json
import sqlite3
import time

from .jlcapi import Component

_CREATE_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS components (
        lcsc INTEGER PRIMARY KEY NOT NULL,
        category_id INTEGER NOT NULL,
        mfr TEXT NOT NULL,
        package TEXT NOT NULL,
        joints INTEGER NOT NULL,
        manufacturer_id INTEGER NOT NULL,
        basic INTEGER NOT NULL,
        preferred INTEGER NOT NULL DEFAULT 0,
        description TEXT NOT NULL,
        datasheet TEXT NOT NULL,
        stock INTEGER NOT NULL,
        price TEXT NOT NULL,
        last_update INTEGER NOT NULL,
        extra TEXT,
        flag INTEGER NOT NULL DEFAULT 0,
        last_on_stock INTEGER NOT NULL DEFAULT 0)
    """,
    """
    CREATE INDEX IF NOT EXISTS components_category
    ON components (category_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS components_manufacturer
    ON components (manufacturer_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS manufacturers (
        id INTEGER PRIMARY KEY NOT NULL,
        name TEXT NOT NULL,
    UNIQUE (id, name))
    """,
    """
    CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY NOT NULL,
        category TEXT NOT NULL,
        subcategory TEXT NOT NULL,
    UNIQUE (category, subcategory))
    """,
]


def fixDescription(description, raw_extra_json):
    """Fix empty descriptions.

    At some point, JLC started returning empty descriptions in
    their parts CSV, but the description is still available in
    the 'extra' JSON in the 'description' field.
    Note that this takes the raw JSON string, not the already
    parsed 'extra' dict - this lets it be used as a SQLite UDF.
    """

    if not description:
        try:
            e = json.loads(raw_extra_json)
            desc = e.get("description", e.get("describe", ""))
            return desc
        except Exception:
            pass
    return description


class ComponentsDatabase:
    """Class to manage the component cache database."""

    def __init__(self, filepath: str) -> None:
        self.conn = sqlite3.connect(filepath)
        self.conn.row_factory = sqlite3.Row
        self.conn.create_function("maybeFixDescription", 2, fixDescription)
        self.manufacturer_cache: dict[str, int | None] = {}
        self.category_cache: dict[tuple[str, str], int | None] = {}
        for stmt in _CREATE_STATEMENTS:
            self.conn.execute(stmt)
        self.conn.commit()

    def manufacturerId(self, name: str) -> int | None:
        """Get the manufacturer ID from the database, inserting if necessary.

        Note that this method has side effects; it inserts into the database and
        commits the transaction if the manufacturer is not already present, so it's
        not safe to do manufacturerId() lookups while inside another running transaction.
        """
        if name in self.manufacturer_cache:
            return self.manufacturer_cache[name]
        cursor = self.conn.execute(
            "SELECT id FROM manufacturers WHERE name = ?", (name,)
        )
        row = cursor.fetchone()
        if row:
            self.manufacturer_cache[name] = row["id"]
            return row["id"]
        cursor = self.conn.execute(
            "INSERT INTO manufacturers (name) VALUES (?)", (name,)
        )
        self.conn.commit()
        manufacturer_id = cursor.lastrowid
        self.manufacturer_cache[name] = manufacturer_id
        return manufacturer_id

    def categoryId(self, category: str, subcategory: str) -> int | None:
        """Get the category ID from the database, inserting if necessary.

        Note that this method has side effects; it inserts into the database and
        commits the transaction if the category is not already present, so it's
        not safe to do categoryId() lookups while inside another running transaction.
        """
        key = (category, subcategory)
        if key in self.category_cache:
            return self.category_cache[key]
        cursor = self.conn.execute(
            "SELECT id FROM categories WHERE category = ? AND subcategory = ?",
            (category, subcategory),
        )
        row = cursor.fetchone()
        if row:
            self.category_cache[key] = row["id"]
            return row["id"]
        cursor = self.conn.execute(
            "INSERT INTO categories (category, subcategory) VALUES (?, ?)",
            (category, subcategory),
        )
        self.conn.commit()
        category_id = cursor.lastrowid
        self.category_cache[key] = category_id
        return category_id

    def close(self) -> None:
        """Close the database connection."""
        self.conn.close()

    def fix_description(self) -> None:
        """Fix empty descriptions in the database by copying from 'extra' JSON.

        Note that this is only useful while boostrapping a copy of the database
        that is missing the descriptions -- e.g. a fresh load of the yaqswx database
        before the empty descriptions are fixed.  It should be harmless to run against
        a fixed database, though, since it will only attempt to fix descriptions that
        are empty or NULL.

        Note that because this runs a large transaction, it can effectively double the
        size of the database while it is running while sqlite journals the changes since
        it can touch all rows in the components table.
        """
        self.conn.execute(
            """
            UPDATE components
            SET description = maybeFixDescription(description, extra)
            WHERE description IS NULL OR description = ''
            """
        )
        self.conn.commit()

    def cleanup_stock(self) -> None:
        """Set stock to zero for components not updated in the last 7 days.

        Since this script should update all in-stock components every time it runs,
        any component that hasn't been updated in the last 7 days can be assumed
        to be out of stock.  Ideally this should only be run if the full database
        update was successful.

        """
        seven_days_ago = int(time.time()) - 7 * 24 * 60 * 60
        now = int(time.time())
        self.conn.execute(
            """
            UPDATE components
            SET stock = 0, last_update = ?
            WHERE stock > 0 AND last_update < ?
            """,
            (now, seven_days_ago),
        )
        self.conn.commit()

    def truncate_old(self) -> None:
        """Truncate components not updated in the last year.

        For components that have been out of stock for over a year, set their
        price and extra fields to empty values to save space.  Neither of these
        fields are indexed in the kicad-jlcpcb-tools usage, so this stock should
        remain searchable but the large price and extra fields that are there
        "just in case" and for backwards compatibility can be removed.

        Because this clears a lot of space, it also runs a VACUUM to compact the
        database to reclaim the freed space.

        Note that both the transaction to clear the old components and the VACUUM
        can take a long time to run and temporarily nearly double the size of the database
        while the changes are journaled.
        """
        one_year_ago = int(time.time()) - 365 * 24 * 60 * 60
        self.conn.execute(
            """
            UPDATE components
            SET price = '[]', extra = '{}' where stock = 0 AND last_on_stock < ?
            """,
            (one_year_ago,),
        )
        self.conn.commit()
        self.conn.execute("VACUUM")

    @staticmethod
    def cols() -> list[str]:
        """Get the list of component database columns."""
        return [
            "lcsc",
            "category_id",
            "manufacturer_id",
            "mfr",
            "package",
            "basic",
            "preferred",
            "description",
            "datasheet",
            "stock",
            "price",
            "extra",
            "joints",
            "last_update",
            "last_on_stock",
        ]

    def update_cache(self, components: "list[Component]") -> None:
        """Update the parts database cache from JLCPCB using UPSERT."""

        # Iterate the components first to set the category and manufacturer IDs.
        # These functions might insert into the database so need to be done outside
        # of the .executemany()'s implicit transaction.
        for comp in components:
            comp["category_id"] = self.categoryId(*comp.categoryKey())
            comp["manufacturer_id"] = self.manufacturerId(comp.manufacturerKey())

        update_cols = [
            col for col in self.cols() if col not in ["lcsc", "last_on_stock"]
        ]
        self.conn.executemany(
            f"""
            INSERT INTO components (
                {", ".join(self.cols())}
            ) VALUES (
                {", ".join(":" + col for col in self.cols())}
            ) ON CONFLICT(lcsc) DO UPDATE SET
            {", ".join(f"{col} = :{col}" for col in update_cols)}
              , last_on_stock = CASE
                    WHEN excluded.stock > 0 THEN excluded.last_update
                    ELSE components.last_on_stock END
            """,
            (comp.asDatabaseRow() for comp in components),
        )
        self.conn.commit()

    def count_components(self, where_clause: str = "") -> int:
        """Count the number of components in the database.

        Args:
            where_clause: SQL WHERE clause (without the WHERE keyword). If empty,
                         all components are counted.

        Returns:
            Number of components matching the where_clause.

        """
        query = "SELECT COUNT(*) AS count FROM components"

        if where_clause:
            query += f" WHERE {where_clause}"

        cursor = self.conn.execute(query)
        row = cursor.fetchone()
        return row["count"] if row else 0

    def fetch_components(
        self, where_clause: str = "", batch_size: int = 100000
    ) -> Generator[list[sqlite3.Row], None, None]:
        """Yield batches of sqlite3.Row objects from the database.

        Args:
            where_clause: SQL WHERE clause (without the WHERE keyword). If empty,
                         all components are returned.
            batch_size: Number of components to yield in each batch (default 100000).

        Yields:
            list[sqlite3.Row]: Lists of rows up to batch_size length.

        Example:
            for batch in db.components_batch_generator("stock > 0", batch_size=10000):
                for row in batch:
                    print(row["lcsc"])

        """
        query = "SELECT * FROM components"

        if where_clause:
            query += f" WHERE {where_clause}"

        query += " ORDER BY lcsc"

        cursor = self.conn.execute(query)

        while True:
            rows = cursor.fetchmany(batch_size)

            if not rows:
                break

            yield rows

    def get_manufacturers(self) -> dict[int, str]:
        """Get all manufacturers from the database.

        Returns:
            Dictionary mapping manufacturer ID to manufacturer name.

        """
        cursor = self.conn.execute("SELECT id, name FROM manufacturers")
        return {row["id"]: row["name"] for row in cursor.fetchall()}

    def get_categories(self) -> dict[int, tuple[str, str]]:
        """Get all categories from the database.

        Returns:
            Dictionary mapping category ID to (category, subcategory) tuple.

        """
        cursor = self.conn.execute("SELECT id, category, subcategory FROM categories")
        return {
            row["id"]: (row["category"], row["subcategory"])
            for row in cursor.fetchall()
        }
