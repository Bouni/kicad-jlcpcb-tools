"""Translation and mapping between component database and parts database formats."""

import copy
import json
import sqlite3


class PriceEntry:
    """Price for a quantity range."""

    def __init__(self, min_quantity: int, max_quantity: int | None, price_dollars: str):
        self.min_quantity = min_quantity
        self.max_quantity = max_quantity
        self.price_dollars_str = price_dollars
        self.price_dollars = float(self.price_dollars_str)

    @classmethod
    def Parse(cls, price_entry: dict[str, str]):
        """Parse an individual price entry."""

        price_dollars_str = price_entry["price"]

        min_quantity = int(price_entry["qFrom"])
        max_quantity = (
            int(price_entry["qTo"]) if price_entry.get("qTo") is not None else None
        )

        return cls(min_quantity, max_quantity, price_dollars_str)

    def __repr__(self):
        """Conversion to string function."""
        return f"{self.min_quantity}-{self.max_quantity if self.max_quantity is not None else ''}:{self.price_dollars_str}"

    min_quantity: int
    max_quantity: int | None
    price_dollars_str: str  # to avoid rounding due to float conversion
    price_dollars: float


class Price:
    """Price parsing and management functions."""

    def __init__(self, part_price: list[dict[str, str]]):
        """Format of part_price is determined by json.loads()."""
        self.price_entries = []
        for price in part_price:
            self.price_entries.append(PriceEntry.Parse(price))

    price_entries: list[PriceEntry]

    @staticmethod
    def reduce_precision(entries: list[PriceEntry]) -> list[PriceEntry]:
        """Reduce the precision of price entries to 3 significant digits."""

        """Values after this are not particularly helpful unless many thousands
        of the part is used, and at those quantities of boards and parts
        the contract manufacturer is likely to have special deals."""

        pe = entries
        for i in range(len(pe)):
            pe[i].price_dollars_str = f"{pe[i].price_dollars:.3f}"
            pe[i].price_dollars = round(pe[i].price_dollars, 3)

        return entries

    @staticmethod
    def filter_below_cutoff(
        entries: list[PriceEntry], cutoff_price_dollars: float
    ) -> list[PriceEntry]:
        """Remove PriceEntry values with a price_dollars below cutoff_price_dollars. Keep the first entry if one exists. Assumes order is highest price to lowest price."""

        filtered_entries: list[PriceEntry] = []

        # some components have no price entries
        if len(entries) >= 1:
            # always include the first entry.
            filtered_entries.append(entries[0])
            for entry in entries[1:]:
                # add the entries with a price greater than the cutoff
                if entry.price_dollars >= cutoff_price_dollars:
                    filtered_entries.append(entry)

        if len(filtered_entries) > 0:
            # ensure the last entry in the list has a max_quantity of None
            # as that price continues out indefinitely
            filtered_entries[len(filtered_entries) - 1].max_quantity = None

        return filtered_entries

    @staticmethod
    def filter_duplicate_prices(entries: list[PriceEntry]) -> list[PriceEntry]:
        """Remove entries with duplicate price_dollar_str values, merging quantities so there aren't gaps."""

        # copy.deepcopy() is used to value modifications from altering the original values.
        price_entries_unique: list[PriceEntry] = []
        if len(entries) > 1:
            first = 0
            second = 1
            f: PriceEntry | None = None
            while True:
                if f is None:
                    f = copy.deepcopy(entries[first])

                # stop when the second element is at the end of the list
                if second >= len(entries):
                    break

                # if match, copy over the quantity and advance the second, keep searching for a mismatch
                if f.price_dollars_str == entries[second].price_dollars_str:
                    f.max_quantity = entries[second].max_quantity
                    second += 1
                else:  # if no match, add the first and then start looking at the second
                    price_entries_unique.append(f)
                    first = second
                    second = first + 1
                    f = None

            # always add the final first entry when we run out of elements to process
            price_entries_unique.append(f)
        else:  # only a single entry, nothing to de-duplicate
            price_entries_unique = entries

        return price_entries_unique

    @staticmethod
    def process(price_json: str) -> tuple[str, int, int, int]:
        """Process price data and return price string and statistics.

        Returns:
            tuple of (price_str, total_entries, entries_deleted, duplicates_deleted)

        """
        priceInput = json.loads(price_json)

        # parse the price field
        price = Price(priceInput)

        price_entries = Price.reduce_precision(price.price_entries)
        entries_total = len(price_entries)

        # filter parts priced below the cutoff value
        price_entries_cutoff = Price.filter_below_cutoff(price_entries, 0.01)
        entries_deleted = len(price_entries) - len(price_entries_cutoff)

        # alias the variable for the next step
        price_entries = price_entries_cutoff

        # remove duplicates
        price_entries_unique = Price.filter_duplicate_prices(price_entries)
        duplicates_deleted = len(price_entries) - len(price_entries_unique)
        entries_deleted += duplicates_deleted

        # alias over the variable for the next step
        price_entries = price_entries_unique

        # build the output string that is stored into the parts database
        price_str = ",".join(
            [
                f"{entry.min_quantity}-{entry.max_quantity if entry.max_quantity is not None else ''}:{entry.price_dollars_str}"
                for entry in price_entries
            ]
        )

        return price_str, entries_total, entries_deleted, duplicates_deleted


def library_type(row: sqlite3.Row) -> str:
    """Return library type string."""
    if row["basic"]:
        return "Basic"
    if row["preferred"]:
        return "Preferred"
    return "Extended"


def process_description(
    description: str, extra_json: str | None, category: str, package: str
) -> str:
    """Process and clean component description.

    Args:
        description: Original description from component
        extra_json: Extra JSON field containing additional description
        category: Second category to remove from description
        package: Package type to remove from description

    Returns:
        Cleaned description string

    """
    # default to 'description', override it with the 'description' property from
    # 'extra' if it exists
    if extra_json is not None:
        try:
            extra = json.loads(extra_json)
            if "description" in extra:
                description = extra["description"]
        except Exception:
            pass

    # strip ROHS out of descriptions where present
    # and add 'not ROHS' where ROHS is not present
    # as 99% of parts are ROHS at this point
    if " ROHS".lower() not in description.lower():
        description += " not ROHS"
    else:
        description = description.replace(" ROHS", "")

    # strip the 'Second category' out of the description if it
    # is duplicated there
    description = description.replace(category, "")

    # remove 'Package' from the description if it is duplicated there
    description = description.replace(package, "")

    # replace double spaces with single spaces in description
    description = description.replace("  ", " ")

    # remove trailing spaces from description
    description = description.strip()

    return description


class ComponentTranslator:
    """Translates component database rows to parts database rows while tracking statistics."""

    def __init__(
        self,
        manufacturers: dict[int, str],
        categories: dict[int, tuple[str, str]],
    ):
        """Initialize the translator.

        Args:
            manufacturers: Dict mapping manufacturer_id to manufacturer name
            categories: Dict mapping category_id to (first_category, second_category) tuple

        """
        self.manufacturers = manufacturers
        self.categories = categories
        self.price_entries_total = 0
        self.price_entries_deleted = 0
        self.price_entries_duplicates_deleted = 0

    def translate(self, component_row: sqlite3.Row) -> dict[str, str | int]:
        """Convert a component database row to a parts database row.

        Args:
            component_row: Row from components table in component database

        Returns:
            Dict ready to insert into parts FTS5 table

        """
        lcsc = component_row["lcsc"]
        category_id = component_row["category_id"]
        manufacturer_id = component_row["manufacturer_id"]

        price_str, total, deleted, duplicates = Price.process(component_row["price"])
        self.price_entries_total += total
        self.price_entries_deleted += deleted
        self.price_entries_duplicates_deleted += duplicates

        description = process_description(
            component_row["description"],
            component_row["extra"],
            self.categories[category_id][1],
            component_row["package"],
        )

        libType = library_type(component_row)

        row = {
            "LCSC Part": f"C{lcsc}",
            "First Category": self.categories[category_id][0],
            "Second Category": self.categories[category_id][1],
            "MFR.Part": component_row["mfr"],
            "Package": component_row["package"],
            "Solder Joint": int(component_row["joints"]),
            "Manufacturer": self.manufacturers[manufacturer_id],
            "Library Type": libType,
            "Description": description,
            "Datasheet": component_row["datasheet"],
            "Price": price_str,
            "Stock": str(component_row["stock"]),
        }

        return row

    def get_statistics(self) -> tuple[int, int, int]:
        """Get price processing statistics.

        Returns:
            Tuple of (total_entries, deleted_entries, duplicates_deleted)

        """
        return (
            self.price_entries_total,
            self.price_entries_deleted,
            self.price_entries_duplicates_deleted,
        )
