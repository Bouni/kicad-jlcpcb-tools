"""Tests for the dblibrary module."""

from pathlib import Path
import re
import sys
import time

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from dblib import DEFAULT_LIBRARY, LIBRARY_CONFIGS, DatabaseConfig, PartDatabaseConfig

# ============================================================================
# PartDatabaseConfig Tests
# ============================================================================


class TestPartDatabaseConfig:
    """Tests for PartDatabaseConfig class."""

    def test_part_database_config_creation(self):
        """PartDatabaseConfig can be created with all fields."""
        config = PartDatabaseConfig(
            name="test.db",
            chunk_file_name="chunk_test.txt",
            where_clause="stock > 0",
            display_name="Test Database",
            populate_preferred=True,
        )
        assert config.name == "test.db"
        assert config.chunk_file_name == "chunk_test.txt"
        assert config.where_clause == "stock > 0"
        assert config.display_name == "Test Database"
        assert config.populate_preferred is True

    def test_part_database_config_default_populate_preferred(self):
        """PartDatabaseConfig defaults populate_preferred to False."""
        config = PartDatabaseConfig(
            name="test.db",
            chunk_file_name="chunk_test.txt",
            where_clause="TRUE",
            display_name="Test Database",
        )
        assert config.populate_preferred is False

    def test_part_database_config_is_immutable(self):
        """PartDatabaseConfig is immutable (NamedTuple)."""
        config = PartDatabaseConfig(
            name="test.db",
            chunk_file_name="chunk_test.txt",
            where_clause="TRUE",
            display_name="Test Database",
        )
        try:
            config.name = "modified.db"  # type: ignore
            assert False, "Should not be able to modify NamedTuple"
        except AttributeError:
            pass  # Expected


# ============================================================================
# DatabaseConfig Tests
# ============================================================================


class TestDatabaseConfig:
    """Tests for DatabaseConfig class."""

    def test_preferred_and_basic_config(self):
        """PreferredAndBasic returns correct configuration."""
        config = DatabaseConfig.preferredAndBasic()

        assert config.name == "basic-parts-fts5.db"
        assert config.chunk_file_name == "chunk_num_basic_parts_fts5.txt"
        assert config.where_clause == "basic = 1 OR preferred = 1"
        assert config.display_name == "Basic + Preferred Library"
        assert config.populate_preferred is True

    def test_all_parts_config(self):
        """AllParts returns correct configuration."""
        config = DatabaseConfig.allParts()

        assert config.name == "parts-fts5.db"
        assert config.chunk_file_name == "chunk_num_fts5.txt"
        assert config.where_clause == "TRUE"
        assert config.display_name == "Full Library - All Parts"
        assert config.populate_preferred is False

    def test_ignore_obsolete_parts_default(self):
        """IgnoreObsoleteParts returns correct configuration with default threshold."""
        before_time = int(time.time()) - 365 * 24 * 60 * 60
        config = DatabaseConfig.ignoreObsoleteParts()
        after_time = int(time.time()) - 365 * 24 * 60 * 60

        assert config.name == "current-parts-fts5.db"
        assert config.chunk_file_name == "chunk_num_current_parts_fts5.txt"
        assert config.display_name == "Current Parts (Exclude Obsolete)"
        assert config.populate_preferred is True

        # Check that the where clause contains a timestamp within reasonable bounds
        assert "NOT (stock = 0 AND last_on_stock <" in config.where_clause

        # Extract the timestamp from the where clause

        match = re.search(r"last_on_stock < (\d+)", config.where_clause)
        assert match is not None
        timestamp = int(match.group(1))

        # Allow 2 seconds of margin for test execution time
        assert before_time - 2 <= timestamp <= after_time + 2

    def test_ignore_obsolete_parts_custom_threshold(self):
        """IgnoreObsoleteParts accepts custom threshold."""
        days = 180
        before_time = int(time.time()) - days * 24 * 60 * 60
        config = DatabaseConfig.ignoreObsoleteParts(obsolete_threshold_days=days)
        after_time = int(time.time()) - days * 24 * 60 * 60

        assert config.name == "current-parts-fts5.db"
        # Verify the timestamp is approximately correct (within a small margin)
        assert "NOT (stock = 0 AND last_on_stock <" in config.where_clause

        match = re.search(r"last_on_stock < (\d+)", config.where_clause)
        assert match is not None
        timestamp = int(match.group(1))

        # Allow 2 seconds of margin for test execution time
        assert before_time - 2 <= timestamp <= after_time + 2

    def test_empty_parts_config(self):
        """EmptyParts returns correct configuration."""
        config = DatabaseConfig.emptyParts()

        assert config.name == "empty-parts-fts5.db"
        assert config.chunk_file_name == "chunk_num_empty_parts_fts5.txt"
        assert config.where_clause == "FALSE"
        assert config.display_name == "Empty Library - No parts!"
        assert config.populate_preferred is False

    def test_all_configs_return_part_database_config(self):
        """All DatabaseConfig methods return PartDatabaseConfig instances."""
        configs = [
            DatabaseConfig.preferredAndBasic(),
            DatabaseConfig.allParts(),
            DatabaseConfig.ignoreObsoleteParts(),
            DatabaseConfig.emptyParts(),
        ]

        for config in configs:
            assert isinstance(config, PartDatabaseConfig)

    def test_configs_have_unique_names(self):
        """All default configurations have unique database names."""
        configs = [
            DatabaseConfig.preferredAndBasic(),
            DatabaseConfig.allParts(),
            DatabaseConfig.ignoreObsoleteParts(),
            DatabaseConfig.emptyParts(),
        ]

        names = [config.name for config in configs]
        assert len(names) == len(set(names)), "Database names should be unique"

    def test_configs_have_unique_chunk_files(self):
        """All default configurations have unique chunk file names."""
        configs = [
            DatabaseConfig.preferredAndBasic(),
            DatabaseConfig.allParts(),
            DatabaseConfig.ignoreObsoleteParts(),
            DatabaseConfig.emptyParts(),
        ]

        chunk_files = [config.chunk_file_name for config in configs]
        assert len(chunk_files) == len(set(chunk_files)), (
            "Chunk file names should be unique"
        )


# ============================================================================
# LIBRARY_CONFIGS and DEFAULT_LIBRARY Tests
# ============================================================================


class TestLibraryConfigs:
    """Tests for LIBRARY_CONFIGS and DEFAULT_LIBRARY constants."""

    def test_library_configs_exists(self):
        """LIBRARY_CONFIGS is defined and is a dict."""
        assert LIBRARY_CONFIGS is not None
        assert isinstance(LIBRARY_CONFIGS, dict)

    def test_default_library_exists(self):
        """DEFAULT_LIBRARY is defined and is a string."""
        assert DEFAULT_LIBRARY is not None
        assert isinstance(DEFAULT_LIBRARY, str)

    def test_default_library_in_configs(self):
        """DEFAULT_LIBRARY key exists in LIBRARY_CONFIGS."""
        assert DEFAULT_LIBRARY in LIBRARY_CONFIGS

    def test_library_configs_has_expected_keys(self):
        """LIBRARY_CONFIGS has all expected configuration keys."""
        expected_keys = {"all-parts", "basic-preferred", "current-parts", "empty"}
        assert set(LIBRARY_CONFIGS.keys()) == expected_keys

    def test_library_configs_values_are_part_database_configs(self):
        """All LIBRARY_CONFIGS values are PartDatabaseConfig instances."""
        for key, config in LIBRARY_CONFIGS.items():
            assert isinstance(config, PartDatabaseConfig), (
                f"Config for '{key}' should be PartDatabaseConfig"
            )

    def test_library_configs_have_display_names(self):
        """All LIBRARY_CONFIGS entries have non-empty display_name."""
        for key, config in LIBRARY_CONFIGS.items():
            assert config.display_name, (
                f"Config for '{key}' should have non-empty display_name"
            )

    def test_library_configs_display_names_unique(self):
        """All display_name values in LIBRARY_CONFIGS are unique."""
        display_names = [config.display_name for config in LIBRARY_CONFIGS.values()]
        assert len(display_names) == len(set(display_names)), (
            "Display names should be unique"
        )

    def test_library_configs_db_names_unique(self):
        """All database names in LIBRARY_CONFIGS are unique."""
        db_names = [config.name for config in LIBRARY_CONFIGS.values()]
        assert len(db_names) == len(set(db_names)), "Database names should be unique"

    def test_default_library_is_current_parts(self):
        """DEFAULT_LIBRARY points to 'current-parts'."""
        assert DEFAULT_LIBRARY == "current-parts"
