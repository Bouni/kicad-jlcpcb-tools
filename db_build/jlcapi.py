"""JLCPCB API interaction classes and utilities."""

from collections.abc import Callable
import json
import time
from typing import Any, NamedTuple

from cachetools import TTLCache, cached
from ratelimit import limits, sleep_and_retry
import requests
from retry import retry


class ApiCategory(NamedTuple):
    """Component category from JLCPCB API."""

    primary: str
    secondary: str
    count: int

    def __repr__(self) -> str | None:
        """Return the string representation of the ApiCategory."""
        return (
            f"{self.primary} {f'| {self.secondary}' if self.secondary else ''} ({self.count})"
            if self.primary
            else ""
        )


class JlcApi:
    """Stateless class to interact with the JLCPCB API.

    This is mostly a collection of static methods for fetching data.
    """

    BASE_URL = "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood"

    @cached(cache=TTLCache(maxsize=1, ttl=180))
    @retry(Exception, tries=5, delay=2, backoff=2)
    @staticmethod
    def getToken() -> str:
        """Fetch a new XSRF token from JLCPCB. Caches the result for 3 minutes."""
        resp = requests.get(f"{JlcApi.BASE_URL}/getXSRFToken")
        token = resp.cookies.get_dict()["XSRF-TOKEN"]
        return token

    @staticmethod
    def componentList(token: str, request: dict[str, Any]) -> Any:
        """Fetch component list from JLCPCB API.

        This doesn't retry explicitly but is intended to be called from a method
        decorated with @retry.

        Args:
            token: XSRF token string
            request: Request payload as a dictionary

        Returns:
            Parsed JSON response body

        """
        url = f"{JlcApi.BASE_URL}/selectSmtComponentList"
        headers = {
            "Content-Type": "application/json",
            "X-XSRF-TOKEN": token,
        }
        resp = requests.post(url, headers=headers, json=request)
        if resp.status_code != 200:
            raise RuntimeError(f"Cannot fetch component list: {resp.text}")
        body = resp.json()
        if body["code"] in [563, 564, 404, 429]:
            # Intentionally don't raise an exception here - these are effectively
            # "no data" responses so retrying doesn't help.
            return {}
        if body["code"] != 200:
            raise RuntimeError(f"{body['code']}: {body['message']}")
        return body

    @retry(Exception, tries=5, delay=30, backoff=2)
    @staticmethod
    def fetchCategories(instockOnly: bool) -> list[ApiCategory]:
        """Fetch component categories from JLCPCB.

        Grabs the list of all categories available via the API along with
        their stock counts.

        Args:
            instockOnly: If True, only fetch categories and counts of in-stock items

        Returns:
            List of ApiCategory objects (which are simple wrappers around primary/secondary/count)

        """
        token = JlcApi.getToken()
        request = {
            "searchType": 1,
            "presaleTypes": ["stock"] if instockOnly else [],
        }
        body = JlcApi.componentList(token, request)
        categories = []
        for primary in body["data"]["sortAndCountVoList"]:
            primaryName = primary["sortName"]
            for secondary in primary["childSortList"]:
                secondaryName = secondary["sortName"]
                count = secondary["componentCount"]
                categories.append(ApiCategory(primaryName, secondaryName, count))
        return categories

    @staticmethod
    def collapseCategories(
        categories: list[ApiCategory], limit: int
    ) -> list[ApiCategory]:
        """Collapse small secondary categories into their primary category.

        JLC's API limits the size of a page to 1000 items, and allows only 100 pages per
        query, so categories with more than 100,000 items need to be split by subcategory.
        However, most top-level categories don't need to be split to stay under this limit.
        (I have no idea what will happen if a single secondary category exceeds this limit -
        The SMD resistor category has ~90K in-stock items as of early 2026)

        For each primary category, if the sum of all secondary categories under it
        is less than the limit, replace them all with a single category with an
        empty secondary category name.  This is useful to reduce the number of API
        calls to JLC - there are lots of tiny subcategories that we would otherwise
        make separate requests for.

        Args:
            categories: Full list of ApiCategory objects
            limit: Minimum count threshold for keeping secondary categories

        Returns:
            Modified list of categories with collapsed entries

        """
        # Group categories by primary
        primary_groups: dict[str, list[ApiCategory]] = {}
        for cat in categories:
            if cat.primary not in primary_groups:
                primary_groups[cat.primary] = []
            primary_groups[cat.primary].append(cat)

        result = []
        for primary, cats in primary_groups.items():
            total_count = sum(cat.count for cat in cats)

            if total_count < limit:
                # Collapse: replace all secondary categories with one collapsed entry
                result.append(ApiCategory(primary, "", total_count))
            else:
                # Keep as-is
                result.extend(cats)
        return result


class CategoryFetch:
    """Class to manage the lifecycle of fetching components from a category.

    Args:
        category: ApiCategory object representing the category to fetch
        rateLimit: If True, apply rate limiting to API calls (default: True)
        pageSize: Number of items per page to fetch (default: 1000, maximum allowed by API)

    """

    def __init__(
        self, category: ApiCategory, rateLimit: bool = True, pageSize: int = 1000
    ) -> None:
        self.category = category
        self.instockOnly = True
        self.pageSize = pageSize
        self.rateLimit = rateLimit
        self.currentPage = 1
        self.token = JlcApi.getToken()

    @sleep_and_retry
    @limits(calls=1, period=3)
    def _rateLimitedNextPage(self) -> list[Any]:
        """Rate-limited wrapper around _fetchNextPage."""
        return self._fetchNextPage()

    @retry(Exception, tries=5, delay=2, backoff=2)
    def _fetchNextPage(self) -> list[Any]:
        request = {
            "searchType": 2,
            "presaleTypes": ["stock"] if self.instockOnly else [],
            "firstSortName": self.category.primary,
            "currentPage": self.currentPage,
            "pageSize": self.pageSize,
        }
        if self.category.secondary:
            request["secondSortName"] = self.category.secondary
        body = JlcApi.componentList(self.token, request)
        if not body:
            return []
        components = body["data"]["componentPageInfo"]["list"]
        self.currentPage += 1
        return components

    def fetchAll(self, callback: Callable[[list[Any]], None]):
        """Fetch all components in the category, invoking callback for each page.

        Args:
            callback: Function to call with each page of components.  The callback
                should accept a single argument: a list of component dicts.

        """
        while True:
            components = (
                self._rateLimitedNextPage() if self.rateLimit else self._fetchNextPage()
            )
            if not components:
                break
            callback(components)


class LcscId:
    """LCSC ID wrapper.

    It can be confusing whether you're working with an LCSC ID as a string like "C12345"
    or as an integer like 12345.  This class wraps both representations and provides
    conversion methods.  Generally the DB wants integer format, while the API and user-facing
    tools want string format.
    """

    def __init__(self, lcsc: str | int) -> None:
        self.lcsc = lcsc

    def toDbKey(self) -> int:
        """Convert to database / integer format."""
        if isinstance(self.lcsc, int):
            return self.lcsc
        return int(self.lcsc[1:])

    def toComponent(self) -> str:
        """Convert to API / string format."""
        if isinstance(self.lcsc, str) and self.lcsc.startswith("C"):
            return self.lcsc
        return f"C{self.lcsc}"


class Component(dict[str, Any]):
    """Component from JLCPCB API."""

    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__(data)
        self.data = data

    def asDatabaseRow(self) -> dict[str, Any]:
        """Convert to database row format."""
        return {
            "lcsc": LcscId(self["componentCode"]).toDbKey(),
            "category_id": self["category_id"],
            "manufacturer_id": self["manufacturer_id"],
            "joints": 0,  # Set to zero, not in API, needed for inseting new items
            "mfr": self["componentModelEn"],
            "package": self["componentSpecificationEn"],
            "manufacturer": self["componentBrandEn"],
            "basic": 1 if self["componentLibraryType"] == "base" else 0,
            "preferred": 1 if self["preferredComponentFlag"] else 0,
            "description": self["describe"],
            "datasheet": self["dataManualUrl"]
            or "https://jlcpcb.com/partdetail/" + self["urlSuffix"],
            "stock": self["stockCount"],
            "price": self.translated_component_prices(),
            "extra": self.stripForExtra(),
            "last_update": int(time.time()),
            "last_on_stock": int(time.time()),
        }

    def categoryKey(self) -> tuple[str, str]:
        """Get the category key (primary, secondary).

        For whatever reason, the JLCPCB API uses "secondSortName" for primary
        categories and "firstSortName" for secondary categories on the returned
        components, despite the opposite naming on the category listing endpoint.
        """
        return (self["secondSortName"], self["firstSortName"])

    def manufacturerKey(self) -> str:
        """Get the manufacturer name."""
        return self["componentBrandEn"]

    def stripForExtra(self) -> str:
        """Get extra data as JSON string, excluding known fields."""
        extra_data = self.data.copy()
        # Remove fields that are already represented elsewhere
        for field in [
            "componentCode",
            "firstSortName",
            "secondSortName",
            "componentModelEn",
            "componentSpecificationEn",
            "componentBrandEn",
            "componentLibraryType",
            "preferredComponentFlag",
            "describe",
            "dataManualUrl",
            "componentPriceList",
            "imageList",
            "componentPrices",
            "buyComponentPrices",
        ]:
            extra_data.pop(field, None)
        for key in list(extra_data.keys()):
            if extra_data[key] is None:
                extra_data.pop(key)
        return json.dumps(extra_data)

    def translated_component_prices(self) -> str:
        """Translate component prices from JLC API format to internal format.

        Input format:
            [
                {"startNumber": 1, "endNumber": 199, "productPrice": 0.0122},
                {"startNumber": 200, "endNumber": 599, "productPrice": 0.0098},
                ...
            ]

        Output format:
            '[{"qFrom": 1, "qTo": 199, "price": 0.0122}, ...]'

        Args:
            component_prices: List of price brackets from the API

        Returns:
            JSON string of transformed price brackets

        """
        component_prices = self["componentPrices"]
        if not component_prices:
            return "[]"

        # Transform the price brackets
        transformed_prices = []
        for bracket in component_prices:
            transformed_bracket = {
                "qFrom": bracket["startNumber"],
                "qTo": bracket["endNumber"] if bracket["endNumber"] != -1 else None,
                "price": bracket["productPrice"],
            }
            transformed_prices.append(transformed_bracket)

        # Return as JSON string
        return json.dumps(transformed_prices)
