"""Tests for the jlcapi module."""

import json
from pathlib import Path
import sys
from unittest import mock

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from common.jlcapi import ApiCategory, CategoryFetch, Component, JlcApi, LcscId

# ============================================================================
# ApiCategory Tests
# ============================================================================


class TestApiCategory:
    """Tests for ApiCategory NamedTuple."""

    def test_api_category_creation(self):
        """ApiCategory can be created with primary, secondary, and count."""
        cat = ApiCategory("Resistors", "Thick Film", 10000)
        assert cat.primary == "Resistors"
        assert cat.secondary == "Thick Film"
        assert cat.count == 10000

    def test_api_category_repr_with_secondary(self):
        """ApiCategory repr shows primary and secondary with count."""
        cat = ApiCategory("Capacitors", "Ceramic", 5000)
        result = repr(cat)
        assert "Capacitors" in result
        assert "Ceramic" in result
        assert "5000" in result

    def test_api_category_repr_without_secondary(self):
        """ApiCategory repr shows only primary with count when secondary is empty."""
        cat = ApiCategory("Diodes", "", 1500)
        result = repr(cat)
        assert "Diodes" in result
        assert "1500" in result
        assert "|" not in result

    def test_api_category_repr_with_empty_primary(self):
        """ApiCategory repr returns empty string when primary is empty."""
        cat = ApiCategory("", "Sub", 100)
        result = repr(cat)
        assert result == ""


# ============================================================================
# LcscId Tests
# ============================================================================


class TestLcscId:
    """Tests for LcscId conversion class."""

    def test_lcsc_id_from_string(self):
        """LcscId can be initialized with string format."""
        lcsc = LcscId("C12345")
        assert lcsc.lcsc == "C12345"

    def test_lcsc_id_from_integer(self):
        """LcscId can be initialized with integer format."""
        lcsc = LcscId(12345)
        assert lcsc.lcsc == 12345

    def test_to_db_key_from_string(self):
        """LcscId.toDbKey converts string to integer."""
        lcsc = LcscId("C12345")
        assert lcsc.toDbKey() == 12345

    def test_to_db_key_from_integer(self):
        """LcscId.toDbKey returns integer unchanged."""
        lcsc = LcscId(12345)
        assert lcsc.toDbKey() == 12345

    def test_to_component_from_string(self):
        """LcscId.toComponent returns string unchanged if already C-prefixed."""
        lcsc = LcscId("C12345")
        assert lcsc.toComponent() == "C12345"

    def test_to_component_from_integer(self):
        """LcscId.toComponent adds C prefix to integer."""
        lcsc = LcscId(12345)
        assert lcsc.toComponent() == "C12345"

    def test_to_component_from_string_without_prefix(self):
        """LcscId.toComponent adds C prefix if missing."""
        lcsc = LcscId("12345")
        assert lcsc.toComponent() == "C12345"


# ============================================================================
# Component Tests
# ============================================================================


class TestComponent:
    """Tests for Component class."""

    @pytest.fixture
    def sample_component_data(self):
        """Sample component data from API."""
        return {
            "componentCode": "C12345",
            "category_id": 1,
            "manufacturer_id": 5,
            "componentModelEn": "1k Resistor",
            "componentSpecificationEn": "0805",
            "componentBrandEn": "Samsung",
            "componentLibraryType": "base",
            "preferredComponentFlag": False,
            "describe": "1k resistor",
            "dataManualUrl": "https://example.com/datasheet.pdf",
            "stockCount": 10000,
            "componentPrices": [
                {"startNumber": 1, "endNumber": 99, "productPrice": 0.05},
                {"startNumber": 100, "endNumber": 999, "productPrice": 0.04},
            ],
            "imageList": [],
            "componentPriceList": [],
            "buyComponentPrices": [],
            "firstSortName": "Resistors",
            "secondSortName": "Thick Film",
            "urlSuffix": "123456",
            "other_field": "other_value",
        }

    def test_component_creation(self, sample_component_data):
        """Component can be created from data dict."""
        comp = Component(sample_component_data)
        assert comp["componentCode"] == "C12345"
        assert comp["category_id"] == 1

    def test_component_as_dict(self, sample_component_data):
        """Component inherits from dict and behaves like one."""
        comp = Component(sample_component_data)
        assert comp.get("componentCode") == "C12345"
        assert "manufacturer_id" in comp

    def test_component_category_key(self, sample_component_data):
        """Component.categoryKey returns (secondary, primary) tuple."""
        comp = Component(sample_component_data)
        primary, secondary = comp.categoryKey()
        assert primary == "Thick Film"
        assert secondary == "Resistors"

    def test_component_manufacturer_key(self, sample_component_data):
        """Component.manufacturerKey returns manufacturer name."""
        comp = Component(sample_component_data)
        assert comp.manufacturerKey() == "Samsung"

    def test_component_translated_prices(self, sample_component_data):
        """Component.translated_component_prices converts price format."""
        comp = Component(sample_component_data)
        result = json.loads(comp.translated_component_prices())

        assert len(result) == 2
        assert result[0]["qFrom"] == 1
        assert result[0]["qTo"] == 99
        assert result[0]["price"] == 0.05
        assert result[1]["qFrom"] == 100
        assert result[1]["qTo"] == 999
        assert result[1]["price"] == 0.04

    def test_component_translated_prices_handles_negative_one_end(self):
        """Component.translated_component_prices converts -1 endNumber to None."""
        data = {
            "componentPrices": [
                {"startNumber": 1, "endNumber": -1, "productPrice": 0.02},
            ],
        }
        comp = Component(data)
        result = json.loads(comp.translated_component_prices())

        assert result[0]["qTo"] is None

    def test_component_translated_prices_empty(self):
        """Component.translated_component_prices returns empty list for no prices."""
        comp = Component({"componentPrices": []})
        result = comp.translated_component_prices()
        assert result == "[]"

    def test_component_as_database_row(self, sample_component_data):
        """Component.asDatabaseRow converts to database format."""
        comp = Component(sample_component_data)
        row = comp.asDatabaseRow()

        assert row["lcsc"] == 12345
        assert row["category_id"] == 1
        assert row["manufacturer_id"] == 5
        assert row["mfr"] == "1k Resistor"
        assert row["package"] == "0805"
        assert row["manufacturer"] == "Samsung"
        assert row["basic"] == 1
        assert row["preferred"] == 0
        assert row["description"] == "1k resistor"
        assert row["stock"] == 10000
        assert "last_update" in row
        assert "last_on_stock" in row

    def test_component_as_database_row_datasheet_fallback(self):
        """Component.asDatabaseRow uses fallback datasheet URL if none provided."""
        data = {
            "componentCode": "C12345",
            "componentModelEn": "Component",
            "componentSpecificationEn": "0805",
            "componentBrandEn": "Brand",
            "componentLibraryType": "base",
            "preferredComponentFlag": False,
            "describe": "Description",
            "dataManualUrl": None,
            "stockCount": 100,
            "category_id": 1,
            "manufacturer_id": 1,
            "componentPrices": [],
            "urlSuffix": "suffix123",
        }
        comp = Component(data)
        row = comp.asDatabaseRow()
        assert row["datasheet"] == "https://jlcpcb.com/partdetail/suffix123"

    def test_component_as_database_row_preferred_flag(self):
        """Component.asDatabaseRow sets preferred when flag is true."""
        data = {
            "componentCode": "C12345",
            "componentModelEn": "Component",
            "componentSpecificationEn": "0805",
            "componentBrandEn": "Brand",
            "componentLibraryType": "advanced",
            "preferredComponentFlag": True,
            "describe": "Description",
            "dataManualUrl": None,
            "stockCount": 100,
            "category_id": 1,
            "manufacturer_id": 1,
            "componentPrices": [],
            "urlSuffix": "suffix123",
        }
        comp = Component(data)
        row = comp.asDatabaseRow()
        assert row["basic"] == 0
        assert row["preferred"] == 1

    def test_component_strip_for_extra(self, sample_component_data):
        """Component.stripForExtra removes known fields from extra data."""
        comp = Component(sample_component_data)
        extra = json.loads(comp.stripForExtra())

        # Known fields should be removed
        assert "componentCode" not in extra
        assert "componentModelEn" not in extra
        assert "componentPrices" not in extra
        assert "dataManualUrl" not in extra

        # Other fields should remain
        assert "other_field" in extra
        assert extra["other_field"] == "other_value"

    def test_component_strip_for_extra_removes_none_values(self):
        """Component.stripForExtra removes None values."""
        data = {
            "other_field": "value",
            "none_field": None,
        }
        comp = Component(data)
        extra = json.loads(comp.stripForExtra())

        assert "none_field" not in extra
        assert "other_field" in extra


# ============================================================================
# JlcApi Tests
# ============================================================================


class TestJlcApi:
    """Tests for JlcApi static class."""

    def test_jlcapi_base_url(self):
        """JlcApi has correct base URL."""
        assert "jlcpcb.com" in JlcApi.BASE_URL
        assert "smtGood" in JlcApi.BASE_URL

    @mock.patch("common.jlcapi.requests.get")
    def test_get_token_success(self, mock_get):
        """JlcApi.getToken fetches and returns XSRF token."""
        mock_response = mock.Mock()
        mock_response.cookies.get_dict.return_value = {"XSRF-TOKEN": "test_token_123"}
        mock_get.return_value = mock_response

        # Clear cache to ensure fresh fetch
        JlcApi.getToken.cache_clear()

        token = JlcApi.getToken()
        assert token == "test_token_123"
        mock_get.assert_called_once()

    @mock.patch("common.jlcapi.requests.post")
    def test_component_list_success(self, mock_post):
        """JlcApi.componentList returns parsed JSON response."""
        response_data = {
            "code": 200,
            "message": "Success",
            "data": {"components": []},
        }
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = response_data
        mock_post.return_value = mock_response

        result = JlcApi.componentList("token123", {"request": "data"})
        assert result == response_data
        mock_post.assert_called_once()

    @mock.patch("common.jlcapi.requests.post")
    def test_component_list_http_error(self, mock_post):
        """JlcApi.componentList raises on non-200 status."""
        mock_response = mock.Mock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_post.return_value = mock_response

        with pytest.raises(RuntimeError, match="Cannot fetch component list"):
            JlcApi.componentList("token123", {})

    @mock.patch("common.jlcapi.requests.post")
    def test_component_list_api_error_no_data(self, mock_post):
        """JlcApi.componentList returns empty dict for certain error codes."""
        response_data = {"code": 563, "message": "Error"}
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = response_data
        mock_post.return_value = mock_response

        result = JlcApi.componentList("token123", {})
        assert result == {}

    @mock.patch("common.jlcapi.requests.post")
    def test_component_list_api_error_rate_limit(self, mock_post):
        """JlcApi.componentList returns empty dict for rate limit error (429)."""
        response_data = {"code": 429, "message": "Too Many Requests"}
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = response_data
        mock_post.return_value = mock_response

        result = JlcApi.componentList("token123", {})
        assert result == {}

    @mock.patch("common.jlcapi.requests.post")
    def test_component_list_api_error_not_found(self, mock_post):
        """JlcApi.componentList returns empty dict for 404 error."""
        response_data = {"code": 404, "message": "Not Found"}
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = response_data
        mock_post.return_value = mock_response

        result = JlcApi.componentList("token123", {})
        assert result == {}

    @mock.patch("common.jlcapi.requests.post")
    def test_component_list_other_error_raises(self, mock_post):
        """JlcApi.componentList raises for other error codes."""
        response_data = {"code": 400, "message": "Bad Request"}
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = response_data
        mock_post.return_value = mock_response

        with pytest.raises(RuntimeError, match="400"):
            JlcApi.componentList("token123", {})

    def test_collapse_categories_no_collapse_needed(self):
        """JlcApi.collapseCategories keeps categories above limit."""
        categories = [
            ApiCategory("Resistors", "Thick Film", 60000),
            ApiCategory("Resistors", "Thin Film", 50000),
            ApiCategory("Capacitors", "Ceramic", 100000),
        ]
        result = JlcApi.collapseCategories(categories, limit=100000)

        # Resistors total (110000) >= limit so kept
        # Capacitors total (100000) is not < limit so kept
        assert len(result) == 3
        assert result == categories

    def test_collapse_categories_collapse_small(self):
        """JlcApi.collapseCategories collapses categories with total < limit."""
        categories = [
            ApiCategory("Diodes", "General", 5000),
            ApiCategory("Diodes", "Schottky", 3000),
            ApiCategory("Resistors", "Thick Film", 50000),
        ]
        result = JlcApi.collapseCategories(categories, limit=100000)

        # Diodes total (8000) < limit, should be collapsed
        # Resistors total (50000) < limit, should also be collapsed
        assert len(result) == 2
        assert any(
            cat.primary == "Diodes" and cat.secondary == "" and cat.count == 8000
            for cat in result
        )
        assert any(
            cat.primary == "Resistors" and cat.secondary == "" and cat.count == 50000
            for cat in result
        )

    def test_collapse_categories_multiple_primaries(self):
        """JlcApi.collapseCategories handles multiple primary categories."""
        categories = [
            ApiCategory("A", "Sub1", 10000),
            ApiCategory("A", "Sub2", 15000),
            ApiCategory("B", "Sub1", 5000),
            ApiCategory("B", "Sub2", 8000),
            ApiCategory("C", "Sub1", 60000),
        ]
        result = JlcApi.collapseCategories(categories, limit=100000)

        # A and B should be collapsed, C kept
        a_collapsed = [cat for cat in result if cat.primary == "A"]
        assert len(a_collapsed) == 1
        assert a_collapsed[0].count == 25000
        assert a_collapsed[0].secondary == ""


# ============================================================================
# CategoryFetch Tests
# ============================================================================


class TestCategoryFetch:
    """Tests for CategoryFetch class."""

    @mock.patch("common.jlcapi.JlcApi.getToken")
    def test_category_fetch_init(self, mock_get_token):
        """CategoryFetch initializes with category and settings."""
        mock_get_token.return_value = "test_token"
        category = ApiCategory("Resistors", "Thick Film", 5000)

        fetch = CategoryFetch(category, rateLimit=True, pageSize=500)

        assert fetch.category == category
        assert fetch.pageSize == 500
        assert fetch.rateLimit is True
        assert fetch.currentPage == 1
        assert fetch.instockOnly is True

    @mock.patch("common.jlcapi.JlcApi.getToken")
    def test_category_fetch_builds_request_with_secondary(self, mock_get_token):
        """CategoryFetch builds request with secondary category."""
        mock_get_token.return_value = "test_token"
        category = ApiCategory("Resistors", "Thick Film", 5000)
        fetch = CategoryFetch(category, rateLimit=False, pageSize=100)

        with mock.patch.object(fetch, "_fetchNextPage", return_value=[]):
            list(fetch.fetchAll())

        # The request should include both primary and secondary

    @mock.patch("common.jlcapi.JlcApi.getToken")
    @mock.patch("common.jlcapi.JlcApi.componentList")
    def test_category_fetch_collapsed_category(
        self, mock_component_list, mock_get_token
    ):
        """CategoryFetch handles collapsed categories (empty secondary)."""
        mock_get_token.return_value = "test_token"
        mock_component_list.return_value = {}
        category = ApiCategory("Resistors", "", 80000)

        fetch = CategoryFetch(category, rateLimit=False)
        list(fetch.fetchAll())

        # Should have made at least one API call
        assert mock_component_list.call_count >= 1

    @mock.patch("common.jlcapi.JlcApi.getToken")
    @mock.patch("common.jlcapi.JlcApi.componentList")
    def test_category_fetch_single_page(self, mock_component_list, mock_get_token):
        """CategoryFetch.fetchAll yields single page when components fit."""
        mock_get_token.return_value = "test_token"

        components_page1 = [{"componentCode": "C1"}, {"componentCode": "C2"}]
        mock_component_list.side_effect = [
            {"data": {"componentPageInfo": {"list": components_page1}}},
            {"data": {"componentPageInfo": {"list": []}}},
        ]

        category = ApiCategory("Resistors", "Thick Film", 2)
        fetch = CategoryFetch(category, rateLimit=False)

        pages = list(fetch.fetchAll())
        assert len(pages) == 1
        assert pages[0] == components_page1

    @mock.patch("common.jlcapi.JlcApi.getToken")
    @mock.patch("common.jlcapi.JlcApi.componentList")
    def test_category_fetch_multiple_pages(self, mock_component_list, mock_get_token):
        """CategoryFetch.fetchAll yields multiple pages."""
        mock_get_token.return_value = "test_token"

        page1 = [{"componentCode": "C1"}]
        page2 = [{"componentCode": "C2"}]
        page3 = []

        mock_component_list.side_effect = [
            {"data": {"componentPageInfo": {"list": page1}}},
            {"data": {"componentPageInfo": {"list": page2}}},
            {"data": {"componentPageInfo": {"list": page3}}},
        ]

        category = ApiCategory("Test", "Cat", 100)
        fetch = CategoryFetch(category, rateLimit=False)

        pages = list(fetch.fetchAll())
        assert len(pages) == 2
        assert pages[0] == page1
        assert pages[1] == page2

    @mock.patch("common.jlcapi.JlcApi.getToken")
    @mock.patch("common.jlcapi.JlcApi.componentList")
    def test_category_fetch_empty_result(self, mock_component_list, mock_get_token):
        """CategoryFetch.fetchAll handles empty response."""
        mock_get_token.return_value = "test_token"
        mock_component_list.return_value = {}

        category = ApiCategory("Test", "Cat", 0)
        fetch = CategoryFetch(category, rateLimit=False)

        pages = list(fetch.fetchAll())
        assert len(pages) == 0

    @mock.patch("common.jlcapi.JlcApi.getToken")
    @mock.patch("common.jlcapi.JlcApi.componentList")
    def test_category_fetch_tracks_page_number(
        self, mock_component_list, mock_get_token
    ):
        """CategoryFetch increments page number on each fetch."""
        mock_get_token.return_value = "test_token"

        mock_component_list.side_effect = [
            {"data": {"componentPageInfo": {"list": [{"componentCode": "C1"}]}}},
            {"data": {"componentPageInfo": {"list": []}}},
        ]

        category = ApiCategory("Test", "Cat", 100)
        fetch = CategoryFetch(category, rateLimit=False)

        list(fetch.fetchAll())

        # Check that currentPage was incremented twice (1 -> 2 on first fetch, 2 -> 3 on second empty fetch)
        assert fetch.currentPage == 3
