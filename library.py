import contextlib
import csv
import logging
import os
import re
import shlex
import sqlite3
import time
from datetime import datetime as dt
from enum import Enum
from ntpath import join
from pathlib import Path
from threading import Thread

import requests
import wx

from .events import (
    MessageEvent,
    PopulateFootprintListEvent,
    ResetGaugeEvent,
    UpdateGaugeEvent,
)
from .helpers import PLUGIN_PATH, natural_sort_collation


class LibraryState(Enum):
    INITIALIZED = 0
    UPDATE_NEEDED = 1
    DOWNLOAD_RUNNING = 2


class Library:
    """A storage class to get data from a sqlite database and write it back"""

    CSV_URL = "https://jlcpcb.com/componentSearch/uploadComponentInfo"

    def __init__(self, parent):
        self.logger = logging.getLogger(__name__)
        self.parent = parent
        self.order_by = "LCSC Part"
        self.order_dir = "ASC"
        self.datadir = os.path.join(PLUGIN_PATH, "jlcpcb")
        self.dbfile = os.path.join(self.datadir, "parts.db")
        self.state = None
        self.category_map = {}
        self.setup()
        self.check_library()

    def setup(self):
        """Check if folders and database exist, setup if not"""
        if not os.path.isdir(self.datadir):
            self.logger.info(
                "Data directory 'jlcpcb' does not exist and will be created."
            )
            Path(self.datadir).mkdir(parents=True, exist_ok=True)

    def check_library(self):
        """Check if the database file exists, if not trigger update"""
        if not os.path.isfile(self.dbfile) or os.path.getsize(self.dbfile) == 0:
            self.state = LibraryState.UPDATE_NEEDED
        else:
            self.state = LibraryState.INITIALIZED

    def set_order_by(self, n):
        """Set which value we want to order by when getting data from the database"""
        order_by = [
            "LCSC Part",
            "MFR.Part",
            "Package",
            "Solder Joint",
            "Library Type",
            "Manufacturer",
            "Description",
            "Price",
            "Stock",
        ]
        if self.order_by == order_by[n] and self.order_dir == "ASC":
            self.order_dir = "DESC"
        else:
            self.order_by = order_by[n]
            self.order_dir = "ASC"

    def search(self, parameters):
        """Search the database for parts that meet the given parameters."""
        columns = [
            "LCSC Part",
            "MFR.Part",
            "Package",
            "Solder Joint",
            "Library Type",
            "Manufacturer",
            "Description",
            "Price",
            "Stock",
        ]
        s = ",".join(f'"{c}"' for c in columns)
        query = f"SELECT {s} FROM parts WHERE "

        try:
            keywords = shlex.split(parameters["keyword"])
        except ValueError as e:
            self.logger.error("Can't split keyword: %s", str(e))

        keyword_columns = [
            "LCSC Part",
            "Description",
            "MFR.Part",
            "Package",
            "Manufacturer",
        ]
        query_chunks = []
        for kw in keywords:
            q = " OR ".join(f'"{c}" LIKE "%{kw}%"' for c in keyword_columns)
            query_chunks.append(f"({q})")

        if "manufacturer" in parameters and parameters["manufacturer"] != "":
            p = parameters["manufacturer"]
            query_chunks.append(f'"Manufacturer" LIKE "{p}"')
        if "package" in parameters and parameters["package"] != "":
            p = parameters["package"]
            query_chunks.append(f'"Package" LIKE "{p}"')
        if "category" in parameters and parameters["category"] != "":
            p = parameters["category"]
            query_chunks.append(f'"First Category" LIKE "{p}"')
        if "subcategory" in parameters and parameters["subcategory"] != "":
            p = parameters["subcategory"]
            query_chunks.append(f'"Second Category" LIKE "{p}"')
        if "part_no" in parameters and parameters["part_no"] != "":
            p = parameters["part_no"]
            query_chunks.append(f'"MFR.Part" LIKE "{p}"')
        if "solder_joints" in parameters and parameters["solder_joints"] != "":
            p = parameters["solder_joints"]
            query_chunks.append(f'"Solder Joint" LIKE "{p}"')

        library_types = []
        if parameters["basic"]:
            library_types.append('"Basic"')
        if parameters["extended"]:
            library_types.append('"Extended"')
        if library_types:
            query_chunks.append(f'"Library Type" IN ({",".join(library_types)})')

        if parameters["stock"]:
            query_chunks.append(f'"Stock" > "0"')

        if not query_chunks:
            return []

        query += " AND ".join(query_chunks)
        query += f' ORDER BY "{self.order_by}" COLLATE naturalsort {self.order_dir}'
        query += " LIMIT 1000"

        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            con.create_collation("naturalsort", natural_sort_collation)
            with con as cur:
                return cur.execute(query).fetchall()

    def delete_parts_table(self):
        """Delete the parts table."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                cur.execute(f"DROP TABLE IF EXISTS parts")
                cur.commit()

    def create_meta_table(self):
        """Create the meta table."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                cur.execute(
                    f"CREATE TABLE IF NOT EXISTS meta ('filename', 'size', 'partcount', 'date', 'last_update')"
                )
                cur.commit()

    def create_rotation_table(self):
        """Create the rotation table."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                cur.execute(
                    f"CREATE TABLE IF NOT EXISTS rotation ('regex', 'correction')"
                )
                cur.commit()

    def get_correction_data(self, regex):
        """Get the correction data by its regex."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                return cur.execute(
                    f"SELECT * FROM rotation WHERE regex = '{regex}'"
                ).fetchone()

    def delete_correction_data(self, regex):
        """Delete a correction from the database."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                cur.execute(f"DELETE FROM rotation WHERE regex = '{regex}'")
                cur.commit()

    def update_correction_data(self, regex, rotation):
        """Update a correction in the database."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                cur.execute(
                    f"UPDATE rotation SET correction = '{rotation}' WHERE regex = '{regex}'"
                )
                cur.commit()

    def insert_correction_data(self, regex, rotation):
        """Insert a correction into the database."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                cur.execute(
                    f"INSERT INTO rotation VALUES (?, ?)",
                    (regex, rotation),
                )
                cur.commit()

    def get_all_correction_data(self):
        """get all corrections from the database."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                return [
                    list(c)
                    for c in cur.execute(
                        f"SELECT * FROM rotation ORDER BY regex ASC"
                    ).fetchall()
                ]

    def create_mapping_table(self):
        """Create the mapping table."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                cur.execute(
                    f"CREATE TABLE IF NOT EXISTS mapping ('footprint', 'value', 'LCSC')"
                )
                cur.commit()

    def get_mapping_data(self, footprint, value):
        """Get the mapping data by its regex."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                return cur.execute(
                    f"SELECT * FROM mapping WHERE footprint = '{footprint}' AND value = '{value}'"
                ).fetchone()

    def delete_mapping_data(self, footprint, value):
        """Delete a mapping from the database."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                cur.execute(
                    f"DELETE FROM mapping WHERE footprint = '{footprint}' AND value = '{value}'"
                )
                cur.commit()

    def update_mapping_data(self, footprint, value, LCSC):
        """Update a mapping in the database."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                cur.execute(
                    f"UPDATE mapping SET LCSC = '{LCSC}' WHERE footprint = '{footprint}' AND value = '{value}'"
                )
                cur.commit()

    def insert_mapping_data(self, footprint, value, LCSC):
        """Insert a mapping into the database."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                cur.execute(
                    f"INSERT INTO mapping VALUES (?, ?, ?)",
                    (footprint, value, LCSC),
                )
                cur.commit()

    def get_all_mapping_data(self):
        """get all mapping from the database."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                return [
                    list(c)
                    for c in cur.execute(
                        f"SELECT * FROM mapping ORDER BY footprint ASC"
                    ).fetchall()
                ]

    def update_meta_data(self, filename, size, partcount, date, last_update):
        """Update the meta data table."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                cur.execute(f"DELETE from meta")
                cur.commit()
                cur.execute(
                    f"INSERT INTO meta VALUES (?, ?, ?, ?, ?)",
                    (filename, size, partcount, date, last_update),
                )
                cur.commit()

    def create_parts_table(self, columns):
        """Create the parts table."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                cols = ",".join([f" '{c}'" for c in columns])
                cur.execute(f"CREATE TABLE IF NOT EXISTS parts ({cols})")
                cur.commit()

    def insert_parts(self, data, cols):
        """Insert many parts at once."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            cols = ",".join(["?"] * cols)
            query = f"INSERT INTO parts VALUES ({cols})"
            con.executemany(query, data)
            con.commit()

    def get_part_details(self, lcsc):
        """Get the part details for a list of lcsc numbers."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                numbers = ",".join([f'"{n}"' for n in lcsc])
                return cur.execute(
                    f'SELECT "LCSC Part", "Stock", "Library Type" FROM parts where "LCSC Part" IN ({numbers})'
                ).fetchall()

    def update(self):
        """Update the sqlite parts database from the JLCPCB CSV."""
        Thread(target=self.download).start()

    def download(self):
        """The actual worker thread that downloads and imports the CSV data."""
        self.state = LibraryState.DOWNLOAD_RUNNING
        start = time.time()
        wx.PostEvent(self.parent, ResetGaugeEvent())
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.93 Safari/537.36",
        }
        r = requests.get(
            self.CSV_URL, allow_redirects=True, stream=True, headers=headers
        )
        if r.status_code != requests.codes.ok:
            wx.PostEvent(
                self.parent,
                MessageEvent(
                    title="Download Error",
                    text=f"Failed to download the JLCPCB database CSV, error code {r.status_code}",
                    style="error",
                ),
            )
            return
        size = int(r.headers.get("Content-Length"))
        filename = r.headers.get("Content-Disposition").split("=")[1]
        date = "unknown"
        _date = re.search(r"(\d{4})(\d{2})(\d{2})", filename)
        if _date:
            date = f"{_date.group(1)}-{_date.group(2)}-{_date.group(3)}"
        self.logger.debug(
            f"Download {filename} with a size of {(size / 1024 / 1024):.2f}MB"
        )
        csv_reader = csv.reader(map(lambda x: x.decode("gbk"), r.raw))
        headers = next(csv_reader)
        self.create_meta_table()
        self.delete_parts_table()
        self.create_parts_table(headers)
        self.create_rotation_table()
        self.create_mapping_table()
        buffer = []
        part_count = 0
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            cols = ",".join(["?"] * len(headers))
            query = f"INSERT INTO parts VALUES ({cols})"

            for count, row in enumerate(csv_reader):
                row.pop()
                buffer.append(row)
                if count % 1000 == 0:
                    progress = r.raw.tell() / size * 100
                    wx.PostEvent(self.parent, UpdateGaugeEvent(value=progress))
                    con.executemany(query, buffer)
                    buffer = []
                part_count = count
            if buffer:
                con.executemany(query, buffer)
            con.commit()
        self.update_meta_data(filename, size, part_count, date, dt.now().isoformat())
        wx.PostEvent(self.parent, ResetGaugeEvent())
        end = time.time()
        wx.PostEvent(self.parent, PopulateFootprintListEvent())
        wx.PostEvent(
            self.parent,
            MessageEvent(
                title="Success",
                text=f"Successfully downloaded and imported the JLCPCB database in {end-start:.2f} seconds!",
                style="info",
            ),
        )
        self.state = LibraryState.INITIALIZED

    @property
    def categories(self):
        """The primary categories in the database.

        Caching the relatively small set of category and subcategory maps
        gives a noticeable speed improvement over repeatedly reading the
        information from the on-disk database.
        """
        if self.category_map == {}:
            # Populate the cache.
            with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
                with con as cur:
                    for row in cur.execute(
                        f'SELECT DISTINCT "First Category", "Second Category" FROM parts ORDER BY UPPER("First Category"), UPPER("Second Category")'
                    ):
                        self.category_map.setdefault(row[0], []).append(row[1])
        return list(self.category_map.keys())

    def get_subcategories(self, category):
        """Get the subcategories associated with the given category."""
        return self.category_map[category]
