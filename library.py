import contextlib
import csv
import logging
import os
import sqlite3
import time
from pathlib import Path
from threading import Thread

import requests
import wx

from .events import MessageEvent, ResetGaugeEvent, UpdateGaugeEvent


class Library:
    """A storage class to get data from a sqlite database and write it back"""

    CSV_URL = "https://jlcpcb.com/componentSearch/uploadComponentInfo"

    def __init__(self, parent, plugin_path):
        self.logger = logging.getLogger(__name__)
        self.parent = parent
        self.plugin_path = plugin_path
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

    def delete_parts_table(self):
        """Delete a table."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                cur.execute(f"DROP TABLE IF EXISTS parts")
                cur.commit()

    def create_parts_table(self, columns):
        """Create the sqlite database tables."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                cols = ",".join([f"'{c}'" for c in columns])
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
            wx.MessageBox(
                f"Failed to download the JLCPCB database CSV, error code {r.status_code}",
                "Download Error",
                style=wx.ICON_ERROR,
            )
            return
        size = int(r.headers.get("Content-Length"))
        filename = r.headers.get("Content-Disposition").split("=")[1]
        self.logger.debug(
            f"Download {filename} with a size of {(size / 1024 / 1024):.2f}MB"
        )
        csv_reader = csv.reader(map(lambda x: x.decode("gbk"), r.raw))
        headers = next(csv_reader)
        self.delete_parts_table()
        self.create_parts_table(headers)
        buffer = []
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
            if buffer:
                con.executemany(query, buffer)
            con.commit()
        wx.PostEvent(self.parent, ResetGaugeEvent())
        end = time.time()
        wx.PostEvent(
            self.parent,
            MessageEvent(
                title="Success",
                text=f"Sucessfully downloaded and imported the JLCPCB database in {end-start:.2f} seconds!",
            ),
        )
