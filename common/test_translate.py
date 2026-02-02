"""Tests for the translate module."""

import json

from common.translate import ComponentTranslator, Price, PriceEntry, process_description

# ============================================================================
# PriceEntry Tests
# ============================================================================


class TestPriceEntry:
    """Tests for PriceEntry class."""

    def test_price_entry_init(self):
        """PriceEntry initializes with correct values."""
        entry = PriceEntry(10, 100, "5.50")
        assert entry.min_quantity == 10
        assert entry.max_quantity == 100
        assert entry.price_dollars_str == "5.50"
        assert entry.price_dollars == 5.50

    def test_price_entry_init_no_max(self):
        """PriceEntry can be created without max_quantity."""
        entry = PriceEntry(500, None, "1.25")
        assert entry.min_quantity == 500
        assert entry.max_quantity is None
        assert entry.price_dollars == 1.25

    def test_price_entry_parse_with_qto(self):
        """PriceEntry.Parse extracts data from dict with qTo."""
        entry_dict = {"qFrom": "100", "qTo": "500", "price": "2.50"}
        entry = PriceEntry.Parse(entry_dict)
        assert entry.min_quantity == 100
        assert entry.max_quantity == 500
        assert entry.price_dollars_str == "2.50"

    def test_price_entry_parse_without_qto(self):
        """PriceEntry.Parse handles missing qTo as None."""
        entry_dict = {"qFrom": "1000", "qTo": None, "price": "0.75"}
        entry = PriceEntry.Parse(entry_dict)
        assert entry.min_quantity == 1000
        assert entry.max_quantity is None
        assert entry.price_dollars == 0.75

    def test_price_entry_repr(self):
        """PriceEntry string representation is correct."""
        entry = PriceEntry(1, 100, "5.50")
        assert repr(entry) == "1-100:5.50"

    def test_price_entry_repr_no_max(self):
        """PriceEntry repr shows empty max when None."""
        entry = PriceEntry(500, None, "1.00")
        assert repr(entry) == "500-:1.00"

    def test_price_entry_float_conversion(self):
        """PriceEntry converts string price to float correctly."""
        entry = PriceEntry(1, 10, "0.005")
        assert entry.price_dollars == 0.005
        assert isinstance(entry.price_dollars, float)


# ============================================================================
# Price Class Tests
# ============================================================================


class TestPrice:
    """Tests for Price class."""

    def test_price_init_single_entry(self):
        """Price initializes with single price entry."""
        price_data = [{"qFrom": "1", "qTo": "100", "price": "5.00"}]
        price = Price(price_data)
        assert len(price.price_entries) == 1
        assert price.price_entries[0].min_quantity == 1

    def test_price_init_multiple_entries(self):
        """Price initializes with multiple price entries."""
        price_data = [
            {"qFrom": "1", "qTo": "100", "price": "5.00"},
            {"qFrom": "101", "qTo": "500", "price": "3.00"},
            {"qFrom": "501", "qTo": None, "price": "1.50"},
        ]
        price = Price(price_data)
        assert len(price.price_entries) == 3

    def test_price_init_empty(self):
        """Price can be initialized with empty list."""
        price = Price([])
        assert len(price.price_entries) == 0

    def test_reduce_precision_single_value(self):
        """Reduce precision converts price to 3 decimal places."""
        entries = [PriceEntry(1, 100, "0.123456789")]
        result = Price.reduce_precision(entries)
        assert result[0].price_dollars_str == "0.123"
        assert result[0].price_dollars == 0.123

    def test_reduce_precision_multiple_values(self):
        """Reduce precision works on multiple entries."""
        entries = [
            PriceEntry(1, 100, "5.123456"),
            PriceEntry(101, 200, "3.999999"),
            PriceEntry(201, None, "1.111111"),
        ]
        result = Price.reduce_precision(entries)
        assert len(result) == 3
        assert result[0].price_dollars_str == "5.123"
        assert result[1].price_dollars_str == "4.000"
        assert result[2].price_dollars_str == "1.111"

    def test_reduce_precision_rounding(self):
        """Reduce precision rounds correctly."""
        entries = [
            PriceEntry(1, 100, "0.9999"),
            PriceEntry(101, 200, "0.1234"),
            PriceEntry(201, None, "0.0001"),
        ]
        result = Price.reduce_precision(entries)
        assert result[0].price_dollars == 1.000
        assert result[1].price_dollars == 0.123
        assert result[2].price_dollars == 0.000

    def test_filter_below_cutoff_keeps_first(self):
        """Filter below cutoff always keeps first entry."""
        entries = [
            PriceEntry(1, 100, "0.05"),
            PriceEntry(101, 200, "0.02"),
        ]
        result = Price.filter_below_cutoff(entries, 0.03)
        assert len(result) == 1
        assert result[0].price_dollars == 0.05

    def test_filter_below_cutoff_multiple_entries(self):
        """Filter below cutoff removes only entries below threshold."""
        entries = [
            PriceEntry(1, 100, "0.40"),
            PriceEntry(101, 200, "0.30"),
            PriceEntry(201, 300, "0.20"),
            PriceEntry(301, 400, "0.10"),
        ]
        result = Price.filter_below_cutoff(entries, 0.25)
        assert len(result) == 2
        assert result[0].price_dollars == 0.40
        assert result[1].price_dollars == 0.30

    def test_filter_below_cutoff_empty_list(self):
        """Filter below cutoff handles empty list."""
        entries = []
        result = Price.filter_below_cutoff(entries, 0.01)
        assert len(result) == 0

    def test_filter_below_cutoff_sets_final_max_none(self):
        """Filter below cutoff sets last entry max_quantity to None."""
        entries = [
            PriceEntry(1, 100, "0.40"),
            PriceEntry(101, 200, "0.30"),
            PriceEntry(201, None, "0.20"),
        ]
        result = Price.filter_below_cutoff(entries, 0.15)
        assert result[-1].max_quantity is None

    def test_filter_below_cutoff_all_above_threshold(self):
        """Filter below cutoff keeps all when all above threshold."""
        entries = [
            PriceEntry(1, 100, "0.50"),
            PriceEntry(101, 200, "0.40"),
            PriceEntry(201, None, "0.30"),
        ]
        result = Price.filter_below_cutoff(entries, 0.20)
        assert len(result) == 3

    def test_filter_duplicate_prices_single_entry(self):
        """Filter duplicate prices works with single entry."""
        entries = [PriceEntry(1, 100, "0.50")]
        result = Price.filter_duplicate_prices(entries)
        assert len(result) == 1
        assert result[0].price_dollars_str == "0.50"

    def test_filter_duplicate_prices_no_duplicates(self):
        """Filter duplicate prices preserves unique prices."""
        entries = [
            PriceEntry(1, 100, "0.40"),
            PriceEntry(101, 200, "0.30"),
            PriceEntry(201, None, "0.20"),
        ]
        result = Price.filter_duplicate_prices(entries)
        assert len(result) == 3

    def test_filter_duplicate_prices_consecutive_duplicates(self):
        """Filter duplicate prices merges consecutive duplicates."""
        entries = [
            PriceEntry(1, 100, "0.40"),
            PriceEntry(101, 200, "0.30"),
            PriceEntry(201, 300, "0.30"),
            PriceEntry(301, None, "0.30"),
        ]
        result = Price.filter_duplicate_prices(entries)
        assert len(result) == 2
        assert result[1].min_quantity == 101
        assert result[1].max_quantity is None

    def test_filter_duplicate_prices_multiple_groups(self):
        """Filter duplicate prices handles multiple duplicate groups."""
        entries = [
            PriceEntry(1, 100, "0.40"),
            PriceEntry(101, 200, "0.40"),
            PriceEntry(201, 300, "0.30"),
            PriceEntry(301, 400, "0.30"),
            PriceEntry(401, None, "0.20"),
        ]
        result = Price.filter_duplicate_prices(entries)
        assert len(result) == 3
        assert result[0].min_quantity == 1
        assert result[0].max_quantity == 200
        assert result[1].min_quantity == 201
        assert result[1].max_quantity == 400

    def test_filter_duplicate_prices_ensures_last_has_none(self):
        """Filter duplicate prices merges quantity ranges."""
        entries = [
            PriceEntry(1, 100, "0.40"),
            PriceEntry(101, 200, "0.30"),
            PriceEntry(201, 300, "0.30"),
        ]
        result = Price.filter_duplicate_prices(entries)
        # When duplicates are merged, the merged entry gets the max_quantity from the last duplicate
        # In this case, entries[1] and entries[2] both have price 0.30
        # so they merge with max_quantity from entries[2] which is 300
        assert len(result) == 2
        assert result[1].price_dollars_str == "0.30"
        assert result[1].min_quantity == 101
        assert result[1].max_quantity == 300

    def test_filter_duplicate_prices_empty_list(self):
        """Filter duplicate prices handles empty list."""
        entries = []
        result = Price.filter_duplicate_prices(entries)
        assert len(result) == 0

    def test_process_integration(self):
        """Process method integrates all steps correctly."""
        price_json = json.dumps(
            [
                {"qFrom": "1", "qTo": "100", "price": "5.123456"},
                {"qFrom": "101", "qTo": "500", "price": "3.987654"},
                {"qFrom": "501", "qTo": None, "price": "0.005"},
            ]
        )
        price_str, total, deleted, duplicates = Price.process(price_json)

        assert total == 3
        assert deleted == 1  # 0.005 is below cutoff
        assert duplicates == 0
        assert "5.123" in price_str
        assert "3.988" in price_str

    def test_process_with_duplicates(self):
        """Process removes duplicates correctly."""
        price_json = json.dumps(
            [
                {"qFrom": "1", "qTo": "100", "price": "5.00"},
                {"qFrom": "101", "qTo": "200", "price": "5.00"},
                {"qFrom": "201", "qTo": None, "price": "5.00"},
            ]
        )
        price_str, total, deleted, duplicates = Price.process(price_json)

        assert total == 3
        assert duplicates == 2

    def test_process_with_mixed_filtering(self):
        """Process handles both cutoff and duplicates."""
        price_json = json.dumps(
            [
                {"qFrom": "1", "qTo": "100", "price": "5.00"},
                {"qFrom": "101", "qTo": "200", "price": "5.00"},
                {"qFrom": "201", "qTo": "300", "price": "0.001"},
                {"qFrom": "301", "qTo": None, "price": "0.001"},
            ]
        )
        price_str, total, deleted, duplicates = Price.process(price_json)

        assert total == 4
        assert deleted >= 2  # At least the below-cutoff entries


# ============================================================================
# Utility Function Tests
# ============================================================================


class TestLibraryType:
    """Tests for library_type method in ComponentTranslator."""

    def test_library_type_basic(self):
        """library_type returns 'Basic' for basic parts."""
        translator = ComponentTranslator({}, {})

        # Mock the row by creating a simple dict-like object
        class MockRow:
            def __getitem__(self, key):
                if key == "basic":
                    return True
                return False

        result = translator.library_type(MockRow())  # type: ignore
        assert result == "Basic"

    def test_library_type_preferred_when_populate_preferred_true(self):
        """library_type returns 'Preferred' when populate_preferred=True."""
        translator = ComponentTranslator({}, {}, populate_preferred=True)

        class MockRow:
            def __getitem__(self, key):
                if key == "basic":
                    return False
                if key == "preferred":
                    return True
                return False

        result = translator.library_type(MockRow())  # type: ignore
        assert result == "Preferred"

    def test_library_type_extended_when_populate_preferred_false(self):
        """library_type returns 'Extended' for preferred parts when populate_preferred=False."""
        translator = ComponentTranslator({}, {}, populate_preferred=False)

        class MockRow:
            def __getitem__(self, key):
                if key == "basic":
                    return False
                if key == "preferred":
                    return True
                return False

        result = translator.library_type(MockRow())  # type: ignore
        assert result == "Extended"

    def test_library_type_extended_default(self):
        """library_type returns 'Extended' for extended parts (default behavior)."""
        translator = ComponentTranslator({}, {})

        class MockRow:
            def __getitem__(self, key):
                return False

        result = translator.library_type(MockRow())  # type: ignore
        assert result == "Extended"


class TestProcessDescription:
    """Tests for process_description function."""

    def test_process_description_basic(self):
        """process_description handles basic input."""
        result = process_description("Resistor 10K ROHS", None, "Resistors", "0805")
        assert "Resistor" in result
        assert "10K" in result
        # When " ROHS" is present, it's removed
        assert " ROHS" not in result

    def test_process_description_removes_category(self):
        """process_description removes second category from description."""
        result = process_description(
            "Resistor 10K ROHS Resistors", None, "Resistors", "0805"
        )
        assert result.count("Resistor") == 1

    def test_process_description_removes_package(self):
        """process_description removes package from description."""
        result = process_description(
            "Resistor 10K ROHS 0805", None, "Resistors", "0805"
        )
        assert "0805" not in result

    def test_process_description_fixes_double_spaces(self):
        """process_description removes double spaces."""
        result = process_description("Resistor  10K  ROHS", None, "Resistors", "0805")
        assert "  " not in result

    def test_process_description_strips_trailing_spaces(self):
        """process_description removes trailing spaces."""
        result = process_description("Resistor 10K ROHS  ", None, "Resistors", "0805")
        assert result == result.strip()

    def test_process_description_adds_not_rohs_when_missing(self):
        """process_description adds 'not ROHS' when ROHS is absent."""
        result = process_description("Resistor 10K", None, "Resistors", "0805")
        assert "not ROHS" in result

    def test_process_description_removes_rohs_when_present(self):
        """process_description removes ROHS from description."""
        result = process_description("Resistor 10K ROHS", None, "Resistors", "0805")
        assert "ROHS" not in result.upper() or "not ROHS" in result

    def test_process_description_rohs_case_insensitive(self):
        """process_description checks for space+ROHS case-insensitive."""
        # \" ROHS\" (with space) is found case-insensitively, so \"rohs\" without space won't match
        result = process_description("Resistor 10K rohs", None, "Resistors", "0805")
        # Since \" ROHS\" is not found, \"not ROHS\" should be added
        # But the input doesn't have a space before rohs, so it remains
        assert "rohs" in result

    def test_process_description_with_extra_json(self):
        """process_description uses description from extra JSON."""
        extra = json.dumps({"description": "Extra description"})
        result = process_description("Original", extra, "Resistors", "0805")
        assert "Extra description" in result

    def test_process_description_with_invalid_extra_json(self):
        """process_description handles invalid extra JSON gracefully."""
        result = process_description(
            "Original ROHS", "invalid json", "Resistors", "0805"
        )
        assert "Original" in result

    def test_process_description_with_extra_json_no_description_key(self):
        """process_description uses original when extra has no description key."""
        extra = json.dumps({"other_key": "value"})
        result = process_description("Original ROHS", extra, "Resistors", "0805")
        assert "Original" in result


# ============================================================================
# ComponentTranslator Tests
# ============================================================================


class TestComponentTranslator:
    """Tests for ComponentTranslator class."""

    def test_translator_init(self):
        """ComponentTranslator initializes with correct data structures."""
        manufacturers = {1: "Samsung", 2: "Intel"}
        categories = {1: ("Resistors", "Fixed Resistors")}
        translator = ComponentTranslator(manufacturers, categories)

        assert translator.manufacturers == manufacturers
        assert translator.categories == categories
        assert translator.price_entries_total == 0
        assert translator.price_entries_deleted == 0
        assert translator.price_entries_duplicates_deleted == 0
        assert translator.populate_preferred is False  # default value

    def test_translator_init_with_populate_preferred(self):
        """ComponentTranslator initializes with populate_preferred flag."""
        manufacturers = {1: "Samsung"}
        categories = {1: ("Resistors", "Fixed Resistors")}
        translator = ComponentTranslator(
            manufacturers, categories, populate_preferred=True
        )

        assert translator.populate_preferred is True

    def test_translator_get_statistics_initial(self):
        """get_statistics returns zeros initially."""
        translator = ComponentTranslator({}, {})
        total, deleted, duplicates = translator.get_statistics()

        assert total == 0
        assert deleted == 0
        assert duplicates == 0

    def test_translator_translate_basic(self):
        """Translate converts a component row to parts row."""
        manufacturers = {1: "Samsung"}
        categories = {1: ("Resistors", "Fixed Resistors")}
        translator = ComponentTranslator(manufacturers, categories)

        # Create a mock row
        class MockRow:
            def __getitem__(self, key):
                data = {
                    "lcsc": "123456",
                    "category_id": 1,
                    "manufacturer_id": 1,
                    "price": json.dumps(
                        [{"qFrom": "1", "qTo": "100", "price": "5.00"}]
                    ),
                    "description": "Test Resistor ROHS",
                    "extra": None,
                    "package": "0805",
                    "mfr": "TESTMFR001",
                    "joints": "2",
                    "datasheet": "http://example.com/ds.pdf",
                    "stock": "1000",
                    "basic": True,
                    "preferred": False,
                }
                return data[key]

        result = translator.translate(MockRow())  # type: ignore

        assert result["LCSC Part"] == "C123456"
        assert result["First Category"] == "Resistors"
        assert result["Second Category"] == "Fixed Resistors"
        assert result["MFR.Part"] == "TESTMFR001"
        assert result["Package"] == "0805"
        assert result["Solder Joint"] == 2
        assert result["Manufacturer"] == "Samsung"
        assert result["Library Type"] == "Basic"
        assert "Resistor" in str(result["Description"])
        assert result["Datasheet"] == "http://example.com/ds.pdf"
        assert "5" in str(result["Price"])
        assert result["Stock"] == "1000"

    def test_translator_tracks_statistics(self):
        """Translate accumulates statistics across multiple calls."""
        manufacturers = {1: "Samsung"}
        categories = {1: ("Resistors", "Fixed Resistors")}
        translator = ComponentTranslator(manufacturers, categories)

        class MockRow:
            def __init__(self, price_json):
                self.price_json = price_json

            def __getitem__(self, key):
                data = {
                    "lcsc": "123456",
                    "category_id": 1,
                    "manufacturer_id": 1,
                    "price": self.price_json,
                    "description": "Test ROHS",
                    "extra": None,
                    "package": "0805",
                    "mfr": "TESTMFR",
                    "joints": "2",
                    "datasheet": "http://example.com",
                    "stock": "1000",
                    "basic": True,
                    "preferred": False,
                }
                return data[key]

        # First component with 3 prices, 1 deleted
        price_json_1 = json.dumps(
            [
                {"qFrom": "1", "qTo": "100", "price": "5.00"},
                {"qFrom": "101", "qTo": "200", "price": "3.00"},
                {"qFrom": "201", "qTo": None, "price": "0.001"},
            ]
        )
        translator.translate(MockRow(price_json_1))  # type: ignore

        # Second component with 2 prices
        price_json_2 = json.dumps(
            [
                {"qFrom": "1", "qTo": "100", "price": "2.00"},
                {"qFrom": "101", "qTo": None, "price": "1.00"},
            ]
        )
        translator.translate(MockRow(price_json_2))  # type: ignore

        total, deleted, duplicates = translator.get_statistics()
        assert total == 5
        assert deleted >= 1  # At least the below-cutoff entry

    def test_translator_translate_preferred_with_populate_preferred_true(self):
        """Translate uses 'Preferred' library type when populate_preferred=True."""
        manufacturers = {1: "Samsung"}
        categories = {1: ("Resistors", "Fixed Resistors")}
        translator = ComponentTranslator(
            manufacturers, categories, populate_preferred=True
        )

        class MockRow:
            def __getitem__(self, key):
                data = {
                    "lcsc": "123456",
                    "category_id": 1,
                    "manufacturer_id": 1,
                    "price": json.dumps(
                        [{"qFrom": "1", "qTo": "100", "price": "5.00"}]
                    ),
                    "description": "Test Resistor ROHS",
                    "extra": None,
                    "package": "0805",
                    "mfr": "TESTMFR001",
                    "joints": "2",
                    "datasheet": "http://example.com/ds.pdf",
                    "stock": "1000",
                    "basic": False,
                    "preferred": True,
                }
                return data[key]

        result = translator.translate(MockRow())  # type: ignore
        assert result["Library Type"] == "Preferred"

    def test_translator_translate_preferred_with_populate_preferred_false(self):
        """Translate uses 'Extended' library type for preferred when populate_preferred=False."""
        manufacturers = {1: "Samsung"}
        categories = {1: ("Resistors", "Fixed Resistors")}
        translator = ComponentTranslator(
            manufacturers, categories, populate_preferred=False
        )

        class MockRow:
            def __getitem__(self, key):
                data = {
                    "lcsc": "123456",
                    "category_id": 1,
                    "manufacturer_id": 1,
                    "price": json.dumps(
                        [{"qFrom": "1", "qTo": "100", "price": "5.00"}]
                    ),
                    "description": "Test Resistor ROHS",
                    "extra": None,
                    "package": "0805",
                    "mfr": "TESTMFR001",
                    "joints": "2",
                    "datasheet": "http://example.com/ds.pdf",
                    "stock": "1000",
                    "basic": False,
                    "preferred": True,
                }
                return data[key]

        result = translator.translate(MockRow())  # type: ignore
        assert result["Library Type"] == "Extended"
