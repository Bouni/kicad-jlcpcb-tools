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
        self.create_folders()
        self.dbfn = os.path.join(self.xlsdir, "jlcpcb_parts.db")
        self.loaded = False
        self.logger = logging.getLogger(__name__)

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
        if not os.path.isfile(self.dbfn):
            return True
        else:
            partcount, filename, size = self.get_info()
            if not size or size == 0:
                return True
            else:
                return False

    def download(self):
        """Create and return CSV downloader thread"""
        return CSVDownloader(self.dbfn, self.CSV_URL)

    def load(self):
        """Connect to JLCPCB library DB"""
        # self.dbh = sqlite3.connect(self.dbfn)

        self.partcount, self.filename, self.size = self.get_info()
        self.logger.info(
            f"Loaded %s with {self.partcount} parts", os.path.basename(self.dbfn)
        )
        self.loaded = True

    def get_info(self, dbh=None):
        """Get info about the state of the database"""
        partcount = 0
        filename = ""
        size = 0
        res = self.query_database("SELECT COUNT(*) FROM jlcpcb_parts")
        if res:
            partcount = res[0][0]
        res = self.query_database("SELECT filename, size FROM info")
        if res:
            filename, size = res[0]
        size = int(size)
        return partcount, filename, size

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

    def __init__(self, dbfn, url):
        threading.Thread.__init__(self)
        self.dbfn = dbfn
        self.url = url
        self.want_abort = False
        self.start()
        self.pos = None
        self.logger = logging.getLogger(__name__)

    def run(self):
        try:
            self.download()
        except Exception as e:
            print("Failed " + str(e))
            # Cleanup the probably broken database
            try:
                os.unlink(self.dbfn)
            except FileNotFoundError:
                pass
            except PermissionError as e:
                self.logger.error(e)

    def download(self):
        # Delete any existing DB
        try:
            os.unlink(self.dbfn)
        except FileNotFoundError:
            pass
        with contextlib.closing(sqlite3.connect(self.dbfn)) as con:
            with con as cur:
                try:
                    r = requests.get(self.url, allow_redirects=True, stream=True)
                    # Check if we get the file size for progress metering
                    size = r.headers.get("Content-Length")
                    if size:
                        size = int(size)
                        self.pos = 0

                    # Decode body and feed into CSV parser
                    csvr = csv.reader(map(lambda x: x.decode("gbk"), r.raw))

                    # Create tables
                    headers = next(csvr)
                    ncols = len(headers)
                    con.execute(
                        "CREATE TABLE jlcpcb_parts ("
                        + ",".join(['"' + h + '"' for h in headers])
                        + ")"
                    )
                    con.execute("CREATE TABLE info (filename, size)")

                    # Create query string
                    q = (
                        "INSERT INTO jlcpcb_parts VALUES ("
                        + ",".join(["?"] * ncols)
                        + ")"
                    )

                    con.execute("BEGIN TRANSACTION")
                    buf = []
                    count = 0
                    chunks = 1000
                    for row in csvr:
                        count += 1
                        # Add to list for batch execution
                        # The CSV has a trailing comma so trim to match the headers (which don't..)
                        buf.append(row[:ncols])
                        if count % chunks == 0:
                            if self.want_abort:
                                raise Exception("Aborted")

                            if size:
                                self.pos = r.raw.tell() / size
                            con.executemany(q, buf)
                            buf = []
                    # Flush any remaining rows
                    if buf:
                        con.executemany(q, buf)

                    con.commit()

                    filename = None
                    contentdisp = r.headers.get("Content-Disposition")
                    if contentdisp:
                        m = re.findall("filename=(.+)", contentdisp)
                        if m:
                            filename = m[0]
                    cur.execute("INSERT INTO info VALUES(?, ?)", (filename, size))
                    con.commit()
                except (sqlite3.OperationalError, ValueError) as e:
                    self.logger.error(e)

    def abort(self):
        self.want_abort = True
