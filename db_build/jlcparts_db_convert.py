#!/usr/bin/env python3

"""Use the amazing work of https://github.com/yaqwsx/jlcparts and convert their database into something we can conveniently use for this plugin.

This replaces the old .csv based database creation that JLCPCB no longer supports.
"""

import copy
from datetime import date, datetime
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from typing import Optional
import urllib.request
import zipfile
from zipfile import ZipFile

import click
import humanize


class PriceEntry:
    """Price for a quantity range."""

    def __init__(
        self, min_quantity: int, max_quantity: Optional[int], price_dollars: str
    ):
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
    max_quantity: Optional[int]
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
            f: Optional[PriceEntry] = None
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


class Generate:
    """Base class for database generation."""

    def __init__(self, output_db: Path, chunk_num: Path):
        self.output_db = output_db
        self.jlcparts_db_name = "cache.sqlite3"
        self.compressed_output_db = f"{self.output_db}.zip"
        self.chunk_num = chunk_num

    def remove_original(self):
        """Remove the original output database."""
        if self.output_db.exists():
            self.output_db.unlink()

    def connect_sqlite(self):
        """Connect to the sqlite databases."""
        # connection to the jlcparts db
        db_uri = f"file:{self.jlcparts_db_name}?mode=rw"
        self.conn_jp = sqlite3.connect(db_uri, uri=True)

        # connection to the plugin db we want to write
        self.conn = sqlite3.connect(self.output_db)

    def load_tables(self):
        """Load the input data into the output database."""

        # load the tables into memory
        print("Reading manufacturers")
        res = self.conn_jp.execute("SELECT * FROM manufacturers")
        mans = dict(res.fetchall())

        print("Reading categories")
        res = self.conn_jp.execute("SELECT * FROM categories")
        cats = {i: (c, sc) for i, c, sc in res.fetchall()}

        res = self.conn_jp.execute("select count(*) from components")
        results = res.fetchone()
        print(f"{humanize.intcomma(results[0])} parts to import")

        self.part_count = 0
        print("Reading components")
        res = self.conn_jp.execute("SELECT * FROM components")
        while True:
            comps = res.fetchmany(size=100000)

            print(f"Read {humanize.intcomma(len(comps))} parts")

            # if we have no more parts exit out of the loop
            if len(comps) == 0:
                break

            self.part_count += len(comps)

            # now extract the data from the jlcparts db and fill
            # it into the plugin database
            print("Building parts rows to insert")
            rows = []
            for c in comps:
                price = json.loads(c[10])
                price_str = ",".join(
                    [
                        f"{entry.get('qFrom')}-{entry.get('qTo') if entry.get('qTo') is not None else ''}:{entry.get('price')}"
                        for entry in price
                    ]
                )
                row = (
                    f"C{c[0]}",  # LCSC Part
                    cats[c[1]][0],  # First Category
                    cats[c[1]][1],  # Second Category
                    c[2],  # MFR.Part
                    c[3],  # Package
                    int(c[4]),  # Solder Joint
                    mans[c[5]],  # Manufacturer
                    "Basic" if c[6] else "Extended",  # Library Type
                    c[7],  # Description
                    c[8],  # Datasheet
                    price_str,  # Price
                    str(c[9]),  # Stock
                )
                rows.append(row)

            print("Inserting into parts table")
            self.conn.executemany(
                "INSERT INTO parts VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
            )
            self.conn.commit()

        print("Done importing parts")

    def meta_data(self):
        """Populate the metadata table."""
        # metadata
        db_size = os.stat(self.output_db).st_size
        self.conn.execute(
            "INSERT INTO meta VALUES(?, ?, ?, ?, ?)",
            [
                "cache.sqlite3",
                db_size,
                self.part_count,
                date.today(),
                datetime.now().isoformat(),
            ],
        )
        self.conn.commit()

    def close_sqlite(self):
        """Close sqlite connections."""
        self.conn_jp.close()
        self.conn.close()

    def compress(self):
        """Compress the output database into a new compressed file."""
        print(f"Compressing {self.output_db}")
        with ZipFile(self.compressed_output_db, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(self.output_db)

    def split(self):
        """Split the compressed so we stay below githubs 100M limit."""

        # Set the size of each split file (in bytes)
        split_size = 80000000  # 80 MB

        # Open the zip file for byte-reading
        print(f"Chunking {self.compressed_output_db}")
        with open(self.compressed_output_db, "rb") as z:
            # Read the file data in chunks
            chunk = z.read(split_size)
            chunk_num = 1

            while chunk:
                split_file_name = f"{self.compressed_output_db}.{chunk_num:03}"
                with open(split_file_name, "wb") as split_file:
                    # Write the chunk to the new split file
                    split_file.write(chunk)

                # Read the next chunk of data from the file
                chunk = z.read(split_size)
                chunk_num += 1

            # create a helper file for the downloader which indicates the number of chunk files
            with open(self.chunk_num, "w", encoding="utf-8") as f:
                f.write(str(chunk_num - 1))

    def display_stats(self):
        """Print out some stats."""
        jlcparts_db_size = humanize.naturalsize(os.path.getsize(self.jlcparts_db_name))
        print(f"jlcparts database ({self.jlcparts_db_name}): {jlcparts_db_size}")
        print(f"part count: {humanize.intcomma(self.part_count)}")
        print(
            f"output db: {humanize.naturalsize(os.path.getsize(self.output_db.name))}"
        )
        print(
            f"output db (compressed): {humanize.naturalsize(os.path.getsize(self.compressed_output_db))}"
        )

    def cleanup(self):
        """Remove the compressed zip file und output db after splitting."""

        print(f"Deleting {self.compressed_output_db}")
        os.unlink(self.compressed_output_db)

        print(f"Deleting {self.output_db}")
        os.unlink(self.output_db)


class Jlcpcb(Generate):
    """Sqlite parts database generator."""

    def __init__(self, output_db: Path, skip_cleanup: bool = False):
        chunk_num = Path("chunk_num.txt")
        self.skip_cleanup = skip_cleanup
        super().__init__(output_db, chunk_num)

    def create_tables(self):
        """Create the tables in the output database."""
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS parts (
                'LCSC Part',
                'First Category',
                'Second Category',
                'MFR.Part',
                'Package',
                'Solder Joint',
                'Manufacturer',
                'Library Type',
                'Description',
                'Datasheet',
                'Price',
                'Stock'
            )
            """
        )

        self.conn.execute(
            """
            CREATE UNIQUE INDEX parts_lcsc_part_index
                ON parts ('LCSC Part')
            """
        )

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mapping (
                'footprint',
                'value',
                'LCSC'
            )
            """
        )

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                'filename',
                'size',
                'partcount',
                'date',
                'last_update'
            )
            """
        )

    def build(self):
        """Run all of the steps to generate the database files for upload."""
        self.remove_original()
        self.connect_sqlite()
        self.create_tables()
        self.load_tables()
        self.meta_data()
        self.close_sqlite()
        self.compress()
        self.split()
        self.display_stats()
        if self.skip_cleanup:
            print("Skipping cleanup")
        else:
            self.cleanup()


class JlcpcbFTS5(Generate):
    """FTS5 specific database generation."""

    def __init__(self, output_db: Path, skip_cleanup: bool = False):
        chunk_num = Path("chunk_num_fts5.txt")
        self.skip_cleanup = skip_cleanup
        super().__init__(output_db, chunk_num)

    def create_tables(self):
        """Create tables."""

        # Columns are unindexed to save space in the FTS5 index (and overall database)
        #
        # Solder Joint is unindexed as it contains a numerical count that isn't particular helpful for token searching
        # Price is unindexed as it isn't helpful for token searching
        # Stock is unindexed as it isn't helpful for token searching
        self.conn.execute(
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
            """
        )

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mapping (
                'footprint',
                'value',
                'LCSC'
            )
            """
        )

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                'filename',
                'size',
                'partcount',
                'date',
                'last_update'
            )
            """
        )

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS categories (
                'First Category',
                'Second Category'
            )
            """
        )

    def load_tables(self):
        """Load the input data into the output database."""

        # load the tables into memory
        print("Reading manufacturers")
        res = self.conn_jp.execute("SELECT * FROM manufacturers")
        mans = dict(res.fetchall())

        print("Reading categories")
        res = self.conn_jp.execute("SELECT * FROM categories")
        cats = {i: (c, sc) for i, c, sc in res.fetchall()}

        res = self.conn_jp.execute("select count(*) from components")
        results = res.fetchone()
        print(f"{humanize.intcomma(results[0])} parts to import")

        price_entries_total = 0
        price_entries_deleted_total = 0
        price_entries_duplicates_deleted_total = 0

        self.part_count = 0
        print("Reading components")
        res = self.conn_jp.execute("SELECT * FROM components")
        while True:
            comps = res.fetchmany(size=100000)

            print(f"Read {humanize.intcomma(len(comps))} parts")

            # if we have no more parts exit out of the loop
            if len(comps) == 0:
                break

            self.part_count += len(comps)

            # now extract the data from the jlcparts db and fill
            # it into the plugin database
            print("Building parts rows to insert")
            rows = []
            for c in comps:
                priceInput = json.loads(c[10])

                # parse the price field
                price = Price(priceInput)

                price_entries = Price.reduce_precision(price.price_entries)
                price_entries_total += len(price_entries)

                price_str: str = ""

                # filter parts priced below the cutoff value
                price_entries_cutoff = Price.filter_below_cutoff(price_entries, 0.01)
                price_entries_deleted_total += len(price_entries) - len(
                    price_entries_cutoff
                )

                # alias the variable for the next step
                price_entries = price_entries_cutoff

                # remove duplicates
                price_entries_unique = Price.filter_duplicate_prices(price_entries)
                price_entries_duplicates_deleted_total += len(price_entries) - len(
                    price_entries_unique
                )
                price_entries_deleted_total += len(price_entries) - len(
                    price_entries_unique
                )

                # alias over the variable for the next step
                price_entries = price_entries_unique

                # build the output string that is stored into the parts database
                price_str = ",".join(
                    [
                        f"{entry.min_quantity}-{entry.max_quantity if entry.max_quantity is not None else ''}:{entry.price_dollars_str}"
                        for entry in price_entries
                    ]
                )

                description = c[7]

                # strip ROHS out of descriptions where present
                # and add 'not ROHS' where ROHS is not present
                # as 99% of parts are ROHS at this point
                if " ROHS".lower() not in description.lower():
                    description += " not ROHS"
                else:
                    description = description.replace(" ROHS", "")

                second_category = cats[c[1]][1]

                # strip the 'Second category' out of the description if it
                # is duplicated there
                description = description.replace(second_category, "")

                package = c[3]

                # remove 'Package' from the description if it is duplicated there
                description = description.replace(package, "")

                # replace double spaces with single spaces in description
                description.replace("  ", " ")

                # remove trailing spaces from description
                description = description.strip()

                row = (
                    f"C{c[0]}",  # LCSC Part
                    cats[c[1]][0],  # First Category
                    cats[c[1]][1],  # Second Category
                    c[2],  # MFR.Part
                    package,  # Package
                    int(c[4]),  # Solder Joint
                    mans[c[5]],  # Manufacturer
                    "Basic" if c[6] else "Extended",  # Library Type
                    description,  # Description
                    c[8],  # Datasheet
                    price_str,  # Price
                    str(c[9]),  # Stock
                )
                rows.append(row)

            print("Inserting into parts table")
            self.conn.executemany(
                "INSERT INTO parts VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
            )
            self.conn.commit()

        print(
            f"Price value filtering trimmed {price_entries_deleted_total} (including {price_entries_duplicates_deleted_total} duplicates) out of {price_entries_total} entries {(price_entries_deleted_total / price_entries_total) * 100 if price_entries_total != 0 else 0:.2f}%"
        )
        print("Done importing parts")

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

    def build(self):
        """Run all of the steps to generate the database files for upload."""
        self.remove_original()
        self.connect_sqlite()
        self.create_tables()
        self.load_tables()
        self.populate_categories()
        self.optimize()
        self.meta_data()
        self.close_sqlite()
        self.compress()
        self.split()
        self.display_stats()
        if self.skip_cleanup:
            print("Skipping cleanup")
        else:
            self.cleanup()


class DownloadProgress:
    """Display the download status during the download process."""

    def __init__(self):
        self.last_download_progress_print_time = 0

    def progress_hook(self, count, block_size, total_size):
        """Pass to reporthook."""
        downloaded = count * block_size

        # print at most twice a second
        max_time_between_prints_seconds = 0.5

        now = time.monotonic()
        if (
            now - self.last_download_progress_print_time
            >= max_time_between_prints_seconds
            or count * block_size >= total_size
        ):
            percent = int(downloaded * 100 / total_size) if total_size > 0 else 0

            sys.stdout.write(
                f"\rDownloading: {percent}% ({downloaded}/{total_size} bytes)"
            )
            sys.stdout.flush()
            self.last_download_progress_print_time = now

        if downloaded >= total_size:
            print()  # Finish line


def test_price_precision_reduce():
    """Price precision reduction works as expected."""

    # build high precision price entries
    prices: list[PriceEntry] = []
    initial_price = "0.123456789"
    prices.append(PriceEntry(1, 100, initial_price))

    # run through precision change
    lower_precision_prices = Price.reduce_precision(prices)

    # confirm 3 digits of precision remain
    expected_price_str = "0.123"
    expected_price_val = 0.123

    print(f"{lower_precision_prices[0]}")

    assert lower_precision_prices[0].price_dollars_str == expected_price_str
    assert lower_precision_prices[0].price_dollars == expected_price_val


def test_price_filter_below_cutoff():
    """Price filter below cutoff works as expected."""

    # build price list with some prices lower than the cutoff
    prices: list[PriceEntry] = []
    prices.append(PriceEntry(1, 100, "0.4"))
    prices.append(PriceEntry(101, 200, "0.3"))
    prices.append(PriceEntry(201, 300, "0.2"))
    prices.append(PriceEntry(301, 400, "0.1"))

    # run through cutoff deletion filter
    filtered_prices = Price.filter_below_cutoff(prices, 0.3)

    # confirm prices lower than cutoff were deleted
    assert len(filtered_prices) == 2
    assert filtered_prices[0].price_dollars == 0.4
    assert filtered_prices[1].price_dollars == 0.3


def test_price_duplicate_price_filter():
    """Price duplicates are removed."""
    # build price list with duplicates
    prices: list[PriceEntry] = []
    prices.append(PriceEntry(1, 100, "0.4"))
    prices.append(PriceEntry(101, 200, "0.3"))
    prices.append(PriceEntry(201, 300, "0.2"))
    prices.append(PriceEntry(301, 400, "0.1"))
    prices.append(PriceEntry(401, 500, "0.1"))
    prices.append(PriceEntry(501, 600, "0.1"))
    prices.append(PriceEntry(601, None, "0.1"))

    # run duplicate filter
    unique = Price.filter_duplicate_prices(prices)

    # confirm duplicates were removed
    assert len(unique) == 4
    assert unique[len(unique) - 1].price_dollars_str == "0.1"

    # last value max_quantity is None
    assert unique[len(unique) - 1].max_quantity is None


@click.command()
@click.option(
    "--skip-cleanup",
    is_flag=True,
    show_default=True,
    default=False,
    help="Disable cleanup, intermediate database files will not be deleted",
)
@click.option(
    "--fetch-parts-db",
    is_flag=True,
    show_default=True,
    default=False,
    help="Fetch the upstream parts db from yaqwsx",
)
@click.option(
    "--skip-generate",
    is_flag=True,
    show_default=True,
    default=False,
    help="Skip the DB generation phase",
)
def main(skip_cleanup: bool, fetch_parts_db: bool, skip_generate: bool):
    """Perform the database steps."""

    output_directory = "db_working"
    if not os.path.exists(output_directory):
        os.mkdir(output_directory)
    os.chdir(output_directory)

    if fetch_parts_db:
        base_url = "https://yaqwsx.github.io/jlcparts/data"
        first_file = "cache.zip"

        # discover which tool is available
        # it can be 7z (Linux) or 7zz (brew on OSX, see https://github.com/orgs/Homebrew/discussions/6072)
        seven_zip_tools = ["7z", "7zz"]

        seven_zip_tool = None

        for tool in seven_zip_tools:
            if shutil.which(tool) is not None:
                seven_zip_tool = tool
                break

        if seven_zip_tool is None:
            print(
                f"Unable to find any seven zip tool {seven_zip_tools}, install one to use the fetch db feature"
            )
            sys.exit(1)
        else:
            print(f"Using seven zip tool '{seven_zip_tool}'")

        print(f"Fetching upstream parts database from {base_url}")

        download_progress = DownloadProgress()
        print(f"Fetching first file {first_file}")
        urllib.request.urlretrieve(
            f"{base_url}/{first_file}",
            first_file,
            reporthook=download_progress.progress_hook,
        )

        command = [seven_zip_tool, "l", first_file]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        # NOTE: 'l' lists the contents of the archive and if any of the zip volumes
        # are missing 7-zip will report non-zero. Only error if the exit code is non-zero
        # AND the error text isn't present.
        if result.returncode != 0 and "ERROR = Missing volume" not in result.stdout:
            print(
                f"Error running command '{' '.join(command)}': {result.stdout} {result.stderr}"
            )
            sys.exit(1)

        try:
            # extract the file count by parsing the command output (stdout)
            file_count = None
            for line in result.stdout.splitlines():
                if "Volume Index =" in line:
                    try:
                        file_count = int(line.split("=")[-1].strip())
                        print(f"File count {file_count}")
                    except ValueError as exc:
                        raise ValueError(
                            f"Failed to parse file count as an integer from line: '{line}'."
                        ) from exc
            if file_count is None:
                raise ValueError(
                    "No 'Volume Index =' line found in the command output."
                )
        except ValueError as e:
            print(
                "Unable to retrieve file count from the 7z output. "
                f"Error: {e} "
                "Expected format: 'Volume Index = <number>'. "
                "Please ensure the 7z tool is installed and the archive is valid."
            )
            sys.exit(1)

        # retrieve each file
        # NOTE: Files start with '1'
        for part in range(1, file_count + 1):
            part_file = f"cache.z{part:02d}"
            print(f"\nGetting file {part_file}")
            download_progress = DownloadProgress()
            urllib.request.urlretrieve(
                f"{base_url}/{part_file}",
                part_file,
                reporthook=download_progress.progress_hook,
            )

        # extract the database file
        print(f"\nExtracting {first_file}")
        # pass '-y' that indicates yes to all questions, such as overwriting
        # NOTE: Python's zipfile (https://docs.python.org/3/library/zipfile.html)
        # cannot be used as as of 2025-07-22 "This module does not currently handle multi-disk ZIP files."
        # and that's exactly what we have here
        command = [seven_zip_tool, "x", "-y", first_file]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            print(f"Error extracting {first_file} with command '{' '.join(command)}':")
            print(result.stderr)
            sys.exit(1)

        # remove the intermediate individual zip files now that the database file
        # has been extracted
        for file in Path(".").glob("cache.z*"):
            file.unlink()

    if not skip_generate:
        # sqlite database
        start = datetime.now()
        output_name = "parts.db"
        partsdb = Path(output_name)

        print(f"Generating {output_name} in {output_directory} directory")
        generator = Jlcpcb(partsdb, skip_cleanup)
        generator.build()

        end = datetime.now()
        deltatime = end - start
        print(
            f"Elapsed time: {humanize.precisedelta(deltatime, minimum_unit='seconds')}"
        )

        # sqlite fts5 database
        start = datetime.now()
        output_name = "parts-fts5.db"
        partsdb = Path(output_name)

        print(f"Generating {output_name} in {output_directory} directory")
        generator = JlcpcbFTS5(partsdb, skip_cleanup)
        generator.build()

        end = datetime.now()
        deltatime = end - start
        print(
            f"Elapsed time: {humanize.precisedelta(deltatime, minimum_unit='seconds')}"
        )


if __name__ == "__main__":
    main()
