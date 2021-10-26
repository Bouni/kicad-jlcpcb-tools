import contextlib
import csv
import logging
import os
import os.path
import re
import shlex
import sqlite3
import threading
from pathlib import Path

import requests


class JLCPCBLibrary:
    CSV_URL = "https://jlcpcb.com/componentSearch/uploadComponentInfo"

    def __init__(self, parent):
        self.parent = parent
        self.logger = logging.getLogger(__name__)
        self.create_folders()
        self.dbfn = os.path.join(self.xlsdir, "jlcpcb_parts.db")
        self.loaded = False
        self.partcount = 0
        self.filename = ""
        self.size = 0
        self.dl_thread = None
        self.download_success = None

    @property
    def isvalid(self):
        return os.path.isfile(self.dbfn) and os.path.getsize(self.dbfn) > 0

    @property
    def download_active(self):
        return self.dl_thread != None

    def query_database(self, query, qargs=[]):
        try:
            with contextlib.closing(sqlite3.connect(self.dbfn)) as con:
                with con as cur:
                    c = cur.execute(query, qargs)
                    return c.fetchall()
        except (sqlite3.OperationalError, ValueError) as e:
            self.logger.error(e)
            return None

    def create_folders(self):
        """Create output folder if not already exist."""
        path, filename = os.path.split(os.path.abspath(__file__))
        self.xlsdir = os.path.join(path, "jlcpcb")
        Path(self.xlsdir).mkdir(parents=True, exist_ok=True)

    def need_download(self):
        """Check if we need to re-download the CSV file and convert to DB"""
        if not self.isvalid:
            return True
        else:
            self.get_info()
            return self.size == 0

    def download(self):
        """Create and return CSV downloader thread"""
        self.download_success = None
        self.dl_thread = CSVDownloader(self)

    def load(self):
        """Connect to JLCPCB library DB"""
        self.get_info()
        self.logger.info(
            f"Loaded %s with {self.partcount} parts", os.path.basename(self.dbfn)
        )
        self.loaded = True

    def get_info(self):
        """Get info about the state of the database"""
        if res := self.query_database("SELECT COUNT(*) FROM jlcpcb_parts"):
            self.partcount = res[0][0]
        if res := self.query_database("SELECT filename, size FROM info"):
            self.filename, size = res[0]
            self.size = int(size)

    def get_stock(self, lcsc):
        if res := self.query_database(
            f'SELECT Stock from jlcpcb_parts WHERE "LCSC Part" = "{lcsc}"'
        ):
            return res[0][0]
        return ""

    def get_packages(self):
        """Get all distinct packages from the library"""
        res = self.query_database("SELECT DISTINCT Package from jlcpcb_parts")
        return sorted([r[0] for r in res])

    def get_manufacturers(self):
        """Get all distinct manufacturers from the library"""
        res = self.query_database("SELECT DISTINCT Manufacturer from jlcpcb_parts")
        return sorted([r[0] for r in res])

    def search(
        self,
        keyword="",
        basic=True,
        extended=False,
        assert_stock=False,
        packages=[],
        manufacturers=[],
    ):
        """Search library for passed on criteria"""

        if len(keyword) < 1:
            return []

        # Split keyword like shell would (so we can quote spaces etc)
        try:
            kws = shlex.split(keyword)
        except ValueError as e:
            self.logger.error("Can't split keyword: %s", str(e))
            return []

        query = 'SELECT "LCSC Part", "MFR.Part", "Package", "Solder Joint", "Library Type", "Manufacturer", "Description", "Price", "Stock" FROM jlcpcb_parts WHERE'

        # Keywords can be in any field but all keywords must be present
        kwq = []
        qargs = []
        for _kw in kws:
            kw = "%" + _kw + "%"
            kwq.append(
                """ ("LCSC Part" LIKE ? OR
                             "First Category" LIKE ? OR
                             "Second Category" LIKE ? OR
                             "MFR.Part" LIKE ? OR
                             "Description" LIKE ? )"""
            )
            qargs.extend([kw, kw, kw, kw, kw])
        query += " AND ".join(kwq)

        ltypes = []
        if basic:
            ltypes.append('"Basic"')
        if extended:
            ltypes.append('"Extended"')
        if ltypes:
            query += ' AND "Library Type" IN (%s)' % (",".join(ltypes))
        if assert_stock:
            query += ' AND "Stock" > 0'
        if packages:
            query += ' AND "Package" IN (%s)' % (
                ",".join(['"' + p + '"' for p in packages])
            )
        if manufacturers:
            query += ' AND "Manufacturer" IN (%s)' % (
                ",".join(['"' + p + '"' for p in manufacturers])
            )

        res = self.query_database(query, qargs)
        return res


class CSVDownloader(threading.Thread):
    """CSV download and conversion thread"""

    def __init__(self, parent):
        threading.Thread.__init__(self)
        self.logger = logging.getLogger(__name__)
        self.parent = parent
        self.want_abort = False
        self.filename = None
        self.size = None
        self.pos = None
        self.headers = None
        self.ncols = None
        self.start()

    def run(self):
        try:
            self.parent.download_success = self.download()
            if self.parent.download_success:
                self.import_csv()
        except Exception as e:
            self.delete_database()

    def download(self):
        """Download the CSV, get some meta data from the request and create a CSV reader."""
        self.logger.debug("Start download")
        r = requests.get(self.parent.CSV_URL, allow_redirects=True, stream=True)
        if r.status_code != requests.codes.ok:
            self.logger.debug("Download failed")
            return False
        self.res = r
        # Check if we get the file size for progress metering
        if size := r.headers.get("Content-Length"):
            self.size = int(size)
            self.pos = 0
        # Get filename
        if cd := r.headers.get("Content-Disposition"):
            if m := re.findall("filename=(.+)", cd):
                self.filename = m[0]
        self.logger.debug("Download success")
        # Decode body and feed into CSV parser
        self.csvr = csv.reader(map(lambda x: x.decode("gbk"), self.res.raw))
        self.headers = next(self.csvr)
        self.ncols = len(self.headers)
        return True

    def delete_database(self):
        """Try to delete the database."""
        try:
            os.unlink(self.parent.dbfn)
            self.logger.debug(f"Successfully deleted database file {self.parent.dbfn}")
        except FileNotFoundError:
            self.logger.error(
                f"Failed to delete database file {self.parent.dbfn}, file not found!"
            )
        except PermissionError:
            self.logger.error(
                f"Failed to delete database file {self.parent.dbfn}, Permission denied!"
            )

    def create_tables(self, con, cur):
        """Create the sqlite db tables."""
        con.execute(
            "CREATE TABLE jlcpcb_parts ("
            + ",".join(['"' + h + '"' for h in self.headers])
            + ")"
        )
        con.execute("CREATE TABLE info (filename, size)")
        cur.execute("INSERT INTO info VALUES(?, ?)", (self.filename, self.size))
        con.commit()

    def store_data(self, con):
        """Write CSV data into the sqlite db."""
        # Create query string
        q = "INSERT INTO jlcpcb_parts VALUES (" + ",".join(["?"] * self.ncols) + ")"
        con.execute("BEGIN TRANSACTION")
        buf = []
        count = 0
        chunks = 1000
        for row in self.csvr:
            count += 1
            # Add to list for batch execution
            # The CSV has a trailing comma so trim to match the headers (which don't..)
            buf.append(row[: self.ncols])
            if count % chunks == 0:
                if self.want_abort:
                    raise Exception("Aborted")
                if self.size:
                    self.pos = self.res.raw.tell() / self.size
                con.executemany(q, buf)
                buf = []
        # Flush any remaining rows
        if buf:
            con.executemany(q, buf)
        con.commit()

    def import_csv(self):
        """import CSV data into sqlite3 database."""
        self.delete_database()
        with contextlib.closing(sqlite3.connect(self.parent.dbfn)) as con:
            with con as cur:
                try:
                    self.create_tables(con, cur)
                    self.store_data(con)
                except (sqlite3.OperationalError, ValueError) as e:
                    self.logger.error(e)

    def abort(self):
        self.want_abort = True
