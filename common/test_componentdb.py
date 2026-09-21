"""Tests for the componentdb module."""

from collections.abc import Iterator
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Optional

import pytest

from common.componentdb import ComponentsDatabase, fixDescription

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_db(tmp_path: Path) -> Iterator[ComponentsDatabase]:
    """Create a temporary test database and clean up after test."""
    db_path = tmp_path / "test_components.db"
    db = ComponentsDatabase(str(db_path))
    yield db
    db.close()


# ============================================================================
# fixDescription Tests
# ============================================================================


@pytest.mark.parametrize(
    "description, extra_json, expected",
    [
        ("", '{"description": "Extracted description"}', "Extracted description"),
        (None, '{"description": "Extracted description"}', "Extracted description"),
        (
            "Original description",
            '{"description": "Should not use this"}',
            "Original description",
        ),
        ("", '{"describe": "Describe fallback"}', "Describe fallback"),
        ("", "invalid json", ""),
        ("", '{"other_key": "value"}', ""),
        ("", "{}", ""),
    ],
)
def test_fix_description(
    description: Optional[str], extra_json: str, expected: str
) -> None:
    """Existing descriptions win; missing descriptions use supported JSON fallbacks."""
    assert fixDescription(description, extra_json) == expected


# ============================================================================
# ComponentsDatabase Tests
# ============================================================================


def _insert_component(db: ComponentsDatabase, lcsc: int, **values: Any) -> None:
    """Insert a real component row with concise overrides for the tested fields."""
    fields = {
        "lcsc": lcsc,
        "category_id": 1,
        "mfr": "MFR",
        "package": "0805",
        "joints": 2,
        "manufacturer_id": 1,
        "basic": 1,
        "description": "Component",
        "datasheet": "http://example.com",
        "stock": 100,
        "price": "[]",
        "last_update": int(time.time()),
        **values,
    }
    db.conn.execute(
        f"INSERT INTO components ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
        list(fields.values()),
    )


class TestComponentsDatabase:
    """Tests for ComponentsDatabase class."""

    def test_components_database_init(
        self, temp_db: ComponentsDatabase, tmp_path: Path
    ) -> None:
        """ComponentsDatabase initializes with database file."""
        db_path = tmp_path / "test_components.db"
        assert db_path.exists()
        assert temp_db.conn is not None

    def test_components_database_creates_tables(self, temp_db):
        """ComponentsDatabase creates all required tables."""
        cursor = temp_db.conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]

        assert "components" in tables
        assert "manufacturers" in tables
        assert "categories" in tables

    def test_components_database_creates_indexes(self, temp_db):
        """ComponentsDatabase creates all required indexes."""
        cursor = temp_db.conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='index'")
        indexes = [row[0] for row in cursor.fetchall()]

        assert "components_category" in indexes
        assert "components_manufacturer" in indexes

    def test_components_database_row_factory(self, temp_db):
        """ComponentsDatabase uses row_factory for sqlite3.Row."""
        assert temp_db.conn.row_factory == sqlite3.Row

    def test_components_database_has_fix_description_udf(self, temp_db):
        """ComponentsDatabase registers maybeFixDescription UDF."""
        # Test by using the function in a query
        cursor = temp_db.conn.cursor()
        result = cursor.execute(
            "SELECT maybeFixDescription('', ?)",
            (json.dumps({"description": "Test"}),),
        ).fetchone()

        assert result[0] == "Test"

    def test_manufacturer_id_new_manufacturer(self, temp_db):
        """ComponentsDatabase manufacturerId inserts new manufacturer."""
        mfr_id = temp_db.manufacturerId("Samsung")
        assert mfr_id is not None
        assert mfr_id > 0

    def test_manufacturer_id_duplicate_returns_same(self, temp_db):
        """ComponentsDatabase manufacturerId returns same ID for duplicate."""
        mfr_id_1 = temp_db.manufacturerId("Samsung")
        mfr_id_2 = temp_db.manufacturerId("Samsung")

        assert mfr_id_1 == mfr_id_2

    def test_manufacturer_id_caching(self, temp_db):
        """ComponentsDatabase caches manufacturer IDs."""
        temp_db.manufacturerId("Intel")
        assert "Intel" in temp_db.manufacturer_cache

        cached_id = temp_db.manufacturer_cache["Intel"]
        new_id = temp_db.manufacturerId("Intel")

        assert cached_id == new_id

    def test_manufacturer_id_different_manufacturers(self, temp_db):
        """ComponentsDatabase assigns different IDs to different manufacturers."""
        samsung_id = temp_db.manufacturerId("Samsung")
        intel_id = temp_db.manufacturerId("Intel")

        assert samsung_id != intel_id

    def test_category_id_new_category(self, temp_db):
        """ComponentsDatabase categoryId inserts new category."""
        cat_id = temp_db.categoryId("Resistors", "Fixed Resistors")
        assert cat_id is not None
        assert cat_id > 0

    def test_category_id_duplicate_returns_same(self, temp_db):
        """ComponentsDatabase categoryId returns same ID for duplicate."""
        cat_id_1 = temp_db.categoryId("Resistors", "Fixed Resistors")
        cat_id_2 = temp_db.categoryId("Resistors", "Fixed Resistors")

        assert cat_id_1 == cat_id_2

    def test_category_id_caching(self, temp_db):
        """ComponentsDatabase caches category IDs."""
        temp_db.categoryId("Capacitors", "Ceramic")
        key = ("Capacitors", "Ceramic")
        assert key in temp_db.category_cache

        cached_id = temp_db.category_cache[key]
        new_id = temp_db.categoryId("Capacitors", "Ceramic")

        assert cached_id == new_id

    def test_category_id_different_subcategories(self, temp_db):
        """ComponentsDatabase assigns different IDs to different subcategories."""
        ceramic_id = temp_db.categoryId("Capacitors", "Ceramic")
        film_id = temp_db.categoryId("Capacitors", "Film")

        assert ceramic_id != film_id

    def test_count_components_empty(self, temp_db):
        """ComponentsDatabase count_components returns 0 for empty database."""
        count = temp_db.count_components()
        assert count == 0

    def test_count_components_with_data(self, temp_db):
        """ComponentsDatabase count_components returns correct count."""
        # Add some components
        for i in range(5):
            _insert_component(
                temp_db,
                100000 + i,
                mfr=f"MFR{i}",
                description=f"Component {i}",
            )
        temp_db.conn.commit()

        count = temp_db.count_components()
        assert count == 5

    def test_count_components_with_where_clause(self, temp_db):
        """ComponentsDatabase count_components filters with where_clause."""
        # Add components with different stock levels
        for i in range(5):
            stock = 100 if i < 3 else 0
            _insert_component(
                temp_db,
                100000 + i,
                mfr=f"MFR{i}",
                description=f"Component {i}",
                stock=stock,
            )
        temp_db.conn.commit()

        total = temp_db.count_components()
        in_stock = temp_db.count_components("stock > 0")

        assert total == 5
        assert in_stock == 3

    def test_fetch_components_empty(self, temp_db):
        """ComponentsDatabase fetch_components returns empty list for empty database."""
        batches = list(temp_db.fetch_components())
        assert len(batches) == 0

    def test_fetch_components_single_batch(self, temp_db):
        """ComponentsDatabase fetch_components yields components in batches."""
        # Add 5 components
        for i in range(5):
            _insert_component(
                temp_db,
                100000 + i,
                mfr=f"MFR{i}",
                description=f"Component {i}",
            )
        temp_db.conn.commit()

        batches = list(temp_db.fetch_components(batch_size=10))

        assert len(batches) == 1
        assert len(batches[0]) == 5

    def test_fetch_components_multiple_batches(self, temp_db):
        """ComponentsDatabase fetch_components splits into multiple batches."""
        # Add 25 components
        for i in range(25):
            _insert_component(
                temp_db,
                100000 + i,
                mfr=f"MFR{i}",
                description=f"Component {i}",
            )
        temp_db.conn.commit()

        batches = list(temp_db.fetch_components(batch_size=10))

        assert len(batches) == 3
        assert len(batches[0]) == 10
        assert len(batches[1]) == 10
        assert len(batches[2]) == 5

    def test_fetch_components_with_where_clause(self, temp_db):
        """ComponentsDatabase fetch_components filters with where_clause."""
        # Add components with different stock levels
        for i in range(5):
            stock = 100 if i % 2 == 0 else 0
            _insert_component(
                temp_db,
                100000 + i,
                mfr=f"MFR{i}",
                description=f"Component {i}",
                stock=stock,
            )
        temp_db.conn.commit()

        batches = list(temp_db.fetch_components("stock > 0"))

        assert len(batches) == 1
        assert len(batches[0]) == 3

    def test_get_manufacturers_empty(self, temp_db):
        """ComponentsDatabase get_manufacturers returns empty dict for empty database."""
        manufacturers = temp_db.get_manufacturers()
        assert len(manufacturers) == 0

    def test_get_manufacturers_with_data(self, temp_db):
        """ComponentsDatabase get_manufacturers returns all manufacturers."""
        mfr_samsung = temp_db.manufacturerId("Samsung")
        mfr_intel = temp_db.manufacturerId("Intel")

        manufacturers = temp_db.get_manufacturers()

        assert len(manufacturers) == 2
        assert mfr_samsung is not None
        assert mfr_intel is not None
        assert manufacturers[mfr_samsung] == "Samsung"
        assert manufacturers[mfr_intel] == "Intel"

    def test_get_categories_empty(self, temp_db):
        """ComponentsDatabase get_categories returns empty dict for empty database."""
        categories = temp_db.get_categories()
        assert len(categories) == 0

    def test_get_categories_with_data(self, temp_db):
        """ComponentsDatabase get_categories returns all categories."""
        cat_resistor = temp_db.categoryId("Resistors", "Fixed")
        cat_capacitor = temp_db.categoryId("Capacitors", "Ceramic")

        categories = temp_db.get_categories()

        assert len(categories) == 2
        assert cat_resistor is not None
        assert cat_capacitor is not None
        assert categories[cat_resistor] == ("Resistors", "Fixed")
        assert categories[cat_capacitor] == ("Capacitors", "Ceramic")

    def test_cols_static_method(self):
        """ComponentsDatabase.cols returns expected column list."""
        cols = ComponentsDatabase.cols()

        assert isinstance(cols, list)
        assert "lcsc" in cols
        assert "category_id" in cols
        assert "manufacturer_id" in cols
        assert "mfr" in cols
        assert "package" in cols
        assert "basic" in cols
        assert "preferred" in cols
        assert "description" in cols
        assert "datasheet" in cols
        assert "stock" in cols
        assert "price" in cols
        assert "extra" in cols
        assert "joints" in cols
        assert "last_update" in cols

    def test_fix_description_method(self, temp_db):
        """ComponentsDatabase fix_description updates empty descriptions."""
        # Insert component with empty description but extra JSON with description
        extra = json.dumps({"description": "Fixed Description"})
        _insert_component(
            temp_db,
            100000,
            description="",
            extra=extra,
        )
        temp_db.conn.commit()

        temp_db.fix_description()

        cursor = temp_db.conn.cursor()
        cursor.execute("SELECT description FROM components WHERE lcsc = ?", (100000,))
        result = cursor.fetchone()

        assert result[0] == "Fixed Description"

    @pytest.mark.parametrize("days_old, expected", [(8, 0), (0, 100)])
    def test_cleanup_stock_respects_update_age(
        self,
        temp_db: ComponentsDatabase,
        days_old: int,
        expected: int,
    ) -> None:
        """Only stale catalog entries lose their reported stock."""
        _insert_component(
            temp_db, 100000, last_update=int(time.time()) - days_old * 86400
        )
        temp_db.conn.commit()
        temp_db.cleanup_stock()
        assert (
            temp_db.conn.execute(
                "SELECT stock FROM components WHERE lcsc = 100000"
            ).fetchone()[0]
            == expected
        )

    @pytest.mark.parametrize(
        "stock, expected",
        [(0, ("[]", "{}")), (100, ("[1.00, 2.00]", '{"retained": true}'))],
    )
    def test_truncate_old_preserves_only_in_stock_details(
        self,
        temp_db: ComponentsDatabase,
        stock: int,
        expected: tuple[str, str],
    ) -> None:
        """Expired unavailable parts release detail data; stocked parts retain it."""
        _insert_component(
            temp_db,
            100000,
            stock=stock,
            price="[1.00, 2.00]",
            extra='{"retained": true}',
            last_on_stock=int(time.time()) - 400 * 86400,
        )
        temp_db.conn.commit()
        temp_db.truncate_old()
        assert (
            tuple(
                temp_db.conn.execute(
                    "SELECT price, extra FROM components WHERE lcsc = 100000"
                ).fetchone()
            )
            == expected
        )
