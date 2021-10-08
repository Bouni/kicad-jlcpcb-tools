import locale
import logging
import os
import time
from pathlib import Path
import requests
import sqlite3
import subprocess

class JLCPCBLibrary:
    CSV_URL = "https://jlcpcb.com/componentSearch/uploadComponentInfo"

    def __init__(self, parent):
        self.parent = parent
        self.create_folders()
        self.csv = os.path.join(self.xlsdir, "jlcpcb_parts.csv")
        self.db = os.path.join(self.xlsdir, "jlcpcb_parts.db")
        self.loaded = False
        self.logger = logging.getLogger(__name__)

    def create_folders(self):
        """Create output folder if not already exist."""
        path, filename = os.path.split(os.path.abspath(__file__))
        self.xlsdir = os.path.join(path, "jlcpcb")
        Path(self.xlsdir).mkdir(parents=True, exist_ok=True)

    def setup_progress_gauge(self, total=None):
        self.parent.gauge.SetRange(total)

    def update_progress_gauge(self, value=None):
        self.parent.gauge.SetValue(value)

    def need_download(self):
        """Check if we need to re-download the CSV file and convert to DB"""
        if not os.path.isfile(self.db) or not os.path.isfile(self.csv) or \
          os.path.getmtime(self.csv) > os.path.getmtime(self.db):
            return True
        # Should check the timestamp of the URL but that is non-trivial
        return False

    def download(self):
        """Download CSV from JLCPCB and convert to sqlite3 DB"""
        self.logger.info("Starting download of library from %s to %s", self.CSV_URL, self.csv)
        with open(self.csv, "wb") as csv:
            r = requests.get(self.CSV_URL, allow_redirects=True, stream=True)
            total_length = r.headers.get("content-length")
            if total_length is None:  # no content length header
                csv.write(r.content)
            else:
                progress = 0
                self.setup_progress_gauge(int(total_length))
                for data in r.iter_content(chunk_size=4096):
                    progress += len(data)
                    self.update_progress_gauge(progress)
                    csv.write(data)
        self.update_progress_gauge(0)

        # Import into DB, need to use iconv to convert to UTF-8 or later queries will fail
        # Nuke stderr because sqlite doesn't that each line ends with a comma
        try:
            os.unlink(self.db)
        except FileNotFoundError:
            pass
        subprocess.check_call(['sqlite3', self.db, '-cmd', '.import --csv "|iconv -f gbk -t UTF-8 %s" jlcpcb_parts' % (self.csv)],
                                  stdin = subprocess.DEVNULL, stderr = subprocess.DEVNULL)
        self.logger.info(f"Converted into %s", self.db)

    def load(self):
        """Connect to JLCPCB library DB"""
        self.logger.info(f"Loading %s", self.db)

        self.dbh = sqlite3.connect(self.db)
        c = self.dbh.cursor()
        c.execute('SELECT COUNT(*) from jlcpcb_parts')
        self.partcount = c.fetchone()[0]
        self.logger.info(f"Loaded Library with {self.partcount} parts")
        self.loaded = True

    def get_packages(self):
        """Get all distinct packages from the library"""
        c = self.dbh.cursor()
        c.execute('SELECT DISTINCT Package from jlcpcb_parts')
        return sorted([r[0] for r in c])

    def get_manufacturers(self):
        """Get all distinct manufacturers from the library"""
        c = self.dbh.cursor()
        c.execute('SELECT DISTINCT Manufacturer from jlcpcb_parts')
        return sorted([r[0] for r in c])

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
            return None
        kw = '%' + keyword + '%'
        query = '''
SELECT "LCSC Part", "MFR.Part", "Package", "Solder Joint", "Library Type", "Manufacturer", "Description", "Price", "Stock" FROM jlcpcb_parts WHERE
        ( "LCSC Part" LIKE ? OR
          "First Category" LIKE ? OR
          "Second Category" LIKE ? OR
          "MFR.Part" LIKE ? OR "Description" LIKE ? )'''
        qargs = [kw, kw, kw, kw, kw]

        ltypes = []
        if basic:
            ltypes.append('"Basic"')
        if extended:
            ltypes.append('"Extended"')
        if ltypes:
            query += ' AND "Library Type" IN (%s)' % (','.join(ltypes))
        if assert_stock:
            query += ' AND "Stock" > 0'
        if packages:
            query += ' AND "Package" IN (%s)' % (','.join(['"' + p + '"' for p in packages]))
        if manufacturers:
            query += ' AND "Manufacturer" IN (%s)' % (','.join(['"' + p + '"' for p in manufacturers]))

        c = self.dbh.cursor()
        try:
            c.execute(query, qargs)
        except (sqlite3.ProgrammingError, sqlite3.OperationalError) as e:
            self.logger.error('Query failed: %s', str(e))

        res = c.fetchall()
        return res
