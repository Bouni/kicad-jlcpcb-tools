import csv
import locale
import logging
import os
import os.path
import time
from pathlib import Path
import re
import requests
import shlex
import sqlite3
import subprocess
import threading

class JLCPCBLibrary:
    CSV_URL = "https://jlcpcb.com/componentSearch/uploadComponentInfo"

    def __init__(self, parent):
        self.parent = parent
        self.create_folders()
        self.dbfn = os.path.join(self.xlsdir, "jlcpcb_parts.db")
        self.loaded = False
        self.logger = logging.getLogger(__name__)

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
        '''Create and return CSV downloader thread'''
        return CSVDownloader(self.dbfn, self.CSV_URL)

    def load(self):
        """Connect to JLCPCB library DB"""
        self.dbh = sqlite3.connect(self.dbfn)

        self.partcount, self.filename, self.size = self.get_info(self.dbh)
        self.logger.info(f"Loaded %s with {self.partcount} parts", os.path.basename(self.dbfn))
        self.loaded = True

    def get_info(self, dbh = None):
        '''Get info, does not use self.dbh because it can be called before load'''
        try:
            if not dbh:
                dbh = sqlite3.connect(self.dbfn)
            c = dbh.cursor()
            c.execute('SELECT COUNT(*) FROM jlcpcb_parts')
            partcount = c.fetchone()[0]
            c.execute('SELECT filename, size FROM info')
            res = c.fetchone()
            if res:
                filename, size = res
            else:
                return None, None, None
            if size:
                size = int(size)
            return partcount, filename, size
        except (sqlite3.OperationalError, ValueError) as e:
            return None, None, None

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
            kw = '%' + _kw + '%'
            kwq.append( ''' ("LCSC Part" LIKE ? OR
                             "First Category" LIKE ? OR
                             "Second Category" LIKE ? OR
                             "MFR.Part" LIKE ? OR
                             "Description" LIKE ? )''')
            qargs.extend([kw, kw, kw, kw, kw])
        query += ' AND '.join(kwq)

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
        #self.logger.info('Query: %s', query)
        #self.logger.info('Args: %s', qargs)
        try:
            c.execute(query, qargs)
        except (sqlite3.ProgrammingError, sqlite3.OperationalError) as e:
            self.logger.error('Query failed: %s', str(e))

        res = c.fetchall()
        return res

class CSVDownloader(threading.Thread):
    '''CSV download and conversion thread'''
    def __init__(self, dbfn, url):
        threading.Thread.__init__(self)
        self.dbfn = dbfn
        self.url = url
        self.want_abort = False
        self.start()
        self.pos = None

    def run(self):
        try:
            self.download()
        except Exception as e:
            print('Failed ' + str(e))
            # Cleanup the probably broken database
            try:
                os.unlink(self.dbfn)
            except FileNotFoundError:
                pass

    def download(self):
        # Delete any existing DB
        try:
            os.unlink(self.dbfn)
        except FileNotFoundError:
            pass

        dbh = sqlite3.connect(self.dbfn)
        c = dbh.cursor()

        r = requests.get(self.url, allow_redirects = True, stream = True)
        # Check if we get the file size for progress metering
        size = r.headers.get('Content-Length')
        if size:
            size = int(size)
            self.pos = 0

        # Decode body and feed into CSV parser
        csvr = csv.reader(map(lambda x: x.decode('gbk'), r.raw))

        # Create tables
        headers = next(csvr)
        ncols = len(headers)
        c.execute('CREATE TABLE jlcpcb_parts (' + ','.join(['"' + h + '"' for h in headers]) + ')')
        c.execute('CREATE TABLE info (filename, size)')

        # Create query string
        q = 'INSERT INTO jlcpcb_parts VALUES (' + ','.join(['?'] * ncols) + ')'

        c.execute('BEGIN TRANSACTION')
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
                    raise Exception('Aborted')

                if size:
                    self.pos = r.raw.tell() / size
                c.executemany(q, buf)
                buf = []
        # Flush any remaining rows
        if buf:
                c.executemany(q, buf)

        dbh.commit()

        filename = None
        contentdisp = r.headers.get('Content-Disposition')
        if contentdisp:
            m = re.findall("filename=(.+)", contentdisp)
            if m:
                filename = m[0]
        c.execute('INSERT INTO info VALUES(?, ?)', (filename, size))
        dbh.commit()
        dbh.close()

    def abort(self):
        self.want_abort = True
