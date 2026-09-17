"""Share supplier facts by code without mixing projects or losing partial data."""

from contextlib import closing
import sqlite3
from typing import Any

from . import test_part_preferences_storage as support

part_preferences_library = support.part_preferences_library


def test_partial_metadata_is_shared_without_erasing_known_fields(
    part_preferences_library: Any,
) -> None:
    """Separate library instances share supplier facts and retain valid class zero."""
    library = part_preferences_library
    library.create_lcsc_metadata_table()
    library.merge_lcsc_metadata(" c123 ", {"assembly_process": " SMT "})
    library.merge_lcsc_metadata("C123", {"component_product_type": 0})
    library.merge_lcsc_metadata(
        "C123", {"assembly_process": "", "component_product_type": 99}
    )
    library.merge_lcsc_metadata("invalid", {"assembly_process": "THT"})
    other = type(library).__new__(type(library))
    other.part_preferences_db_file = library.part_preferences_db_file
    other.logger = library.logger
    assert other.get_lcsc_metadata(["C123", "C999", "invalid"]) == {
        "C123": {"assembly_process": "SMT", "component_product_type": 0}
    }
    with closing(sqlite3.connect(library.part_preferences_db_file)) as db:
        assert db.execute("SELECT count(*) FROM lcsc_metadata").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM mapping").fetchone() == (0,)


def test_failed_cache_write_remains_usable_and_retries_only_on_explicit_merge(
    part_preferences_library: Any,
) -> None:
    """A failed optional write stays in this library, never entering another path."""
    library = part_preferences_library
    library.create_lcsc_metadata_table()
    with closing(sqlite3.connect(library.part_preferences_db_file)) as db, db:
        db.execute(
            "CREATE TRIGGER fail_cache BEFORE INSERT ON lcsc_metadata "
            "BEGIN SELECT RAISE(ABORT, 'cache unavailable'); END"
        )
    library.merge_lcsc_metadata(
        "C123", {"assembly_process": "SMT", "component_product_type": 0}
    )
    assert library.get_lcsc_metadata(["C123"])["C123"]["component_product_type"] == 0
    original = library.part_preferences_db_file
    library.part_preferences_db_file = original + ".other"
    library.create_lcsc_metadata_table()
    assert library.get_lcsc_metadata(["C123"]) == {}
    library.part_preferences_db_file = original
    with closing(sqlite3.connect(original)) as db, db:
        assert db.execute("SELECT count(*) FROM lcsc_metadata").fetchone() == (0,)
        db.execute("DROP TRIGGER fail_cache")
    library.merge_lcsc_metadata("C123", {"assembly_process": "THT"})
    assert library.get_lcsc_metadata(["C123"])["C123"] == {
        "assembly_process": "THT",
        "component_product_type": 0,
    }
    library.merge_lcsc_metadata("C123", {"component_product_type": 1})
    assert library.get_lcsc_metadata(["C123"])["C123"]["component_product_type"] == 1


def test_cache_batches_large_boards(part_preferences_library: Any) -> None:
    """More codes than older SQLite parameter limits can be queried together."""
    library = part_preferences_library
    library.create_lcsc_metadata_table()
    library.merge_lcsc_metadata("C1200", {"component_product_type": 2})
    assert library.get_lcsc_metadata(f"C{i}" for i in range(1201)) == {
        "C1200": {"assembly_process": None, "component_product_type": 2}
    }
