import contextlib
import csv
import logging
import os
import re
import shlex
import sqlite3
import time
from datetime import datetime as dt
from ntpath import join
from pathlib import Path
from threading import Thread

import requests
import wx

from .events import MessageEvent, ResetGaugeEvent, UpdateGaugeEvent
from .helpers import natural_sort_collation


class Library:
    """A storage class to get data from a sqlite database and write it back"""

    CSV_URL = "https://jlcpcb.com/componentSearch/uploadComponentInfo"

    def __init__(self, parent, plugin_path):
        self.logger = logging.getLogger(__name__)
        self.parent = parent
        self.plugin_path = plugin_path
        self.order_by = "lcsc"
        self.order_dir = "ASC"
        self.datadir = os.path.join(self.plugin_path, "jlcpcb")
        self.dbfile = os.path.join(self.datadir, "parts.db")
        self.setup()

    def setup(self):
        """Check if folders and database exist, setup if not"""
        if not os.path.isdir(self.datadir):
            self.logger.info(
                "Data directory 'jlcpcb' does not exist and will be created."
            )
            Path(self.datadir).mkdir(parents=True, exist_ok=True)
        if not os.path.isfile(self.dbfile):
            self.update()

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
        ]
        query_chunks = []
        for kw in keywords:
            q = " OR ".join(f'"{c}" LIKE "%{kw}%"' for c in keyword_columns)
            query_chunks.append(f"({q})")

        if p := parameters["manufacturer"]:
            query_chunks.append(f'"Manufacturer" LIKE "{p}"')
        if p := parameters["package"]:
            query_chunks.append(f'"Package" LIKE "{p}"')
        if p := parameters["category"]:
            query_chunks.append(
                f'("First Category" LIKE "{p}" OR "Second Category" LIKE "{p}")'
            )
        if p := parameters["part_no"]:
            query_chunks.append(f'"MFR.Part" LIKE "{p}"')
        if p := parameters["solder_joints"]:
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
        query += " LIMIT 1000"

        self.logger.debug(query)

        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
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
                # {'INTEGER' if c == 'Stock' else ''}
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

    def update(self):
        """Update the sqlite parts database from the JLCPCB CSV."""
        Thread(target=self.download).start()

    def download(self):
        """The actual worker thread that downloads and imports the CSV data."""
        start = time.time()
        wx.PostEvent(self.parent, ResetGaugeEvent())
        r = requests.get(self.CSV_URL, allow_redirects=True, stream=True)
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
        if _date := re.search(r"(\d{4})(\d{2})(\d{2})", filename):
            date = f"{_date.group(1)}-{_date.group(2)}-{_date.group(3)}"
        self.logger.debug(
            f"Download {filename} with a size of {(size / 1024 / 1024):.2f}MB"
        )
        csv_reader = csv.reader(map(lambda x: x.decode("gbk"), r.raw))
        headers = next(csv_reader)
        self.create_meta_table()
        self.delete_parts_table()
        self.create_parts_table(headers)
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
        wx.PostEvent(
            self.parent,
            MessageEvent(
                title="Success",
                text=f"Sucessfully downloaded and imported the JLCPCB database in {end-start:.2f} seconds!",
                style="info",
            ),
        )
