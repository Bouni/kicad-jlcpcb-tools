"""Handle the JLCPCB parts database."""

import contextlib
from enum import Enum
from glob import glob
import logging
import os
from pathlib import Path
import sqlite3
from threading import Thread
import time

import requests  # pylint: disable=import-error
import wx  # pylint: disable=import-error

from .events import (
    MessageEvent,
    PopulateFootprintListEvent,
    ResetGaugeEvent,
    UpdateGaugeEvent,
)
from .helpers import PLUGIN_PATH, dict_factory, natural_sort_collation
from .unzip_parts import unzip_parts


class LibraryState(Enum):
    """The various states of the library."""

    INITIALIZED = 0
    UPDATE_NEEDED = 1
    DOWNLOAD_RUNNING = 2


class Library:
    """A storage class to get data from a sqlite database and write it back."""

    def __init__(self, parent):
        self.logger = logging.getLogger(__name__)
        self.parent = parent
        self.order_by = "LCSC Part"
        self.order_dir = "ASC"
        self.datadir = os.path.join(PLUGIN_PATH, "jlcpcb")
        self.partsdb_file = os.path.join(self.datadir, "parts-fts5.db")
        self.rotationsdb_file = os.path.join(self.datadir, "rotations.db")
        self.mappingsdb_file = os.path.join(self.datadir, "mappings.db")
        self.state = None
        self.category_map = {}
        self.setup()
        self.check_library()

    def setup(self):
        """Check if folders and database exist, setup if not."""
        if not os.path.isdir(self.datadir):
            self.logger.info(
                "Data directory 'jlcpcb' does not exist and will be created."
            )
            Path(self.datadir).mkdir(parents=True, exist_ok=True)

    def check_library(self):
        """Check if the database files exists, if not trigger update / create database."""
        if (
            not os.path.isfile(self.partsdb_file)
            or os.path.getsize(self.partsdb_file) == 0
        ):
            self.state = LibraryState.UPDATE_NEEDED
        else:
            self.state = LibraryState.INITIALIZED
        if (
            not os.path.isfile(self.rotationsdb_file)
            or os.path.getsize(self.rotationsdb_file) == 0
        ):
            self.create_rotation_table()
            self.migrate_rotations()
        if (
            not os.path.isfile(self.mappingsdb_file)
            or os.path.getsize(self.mappingsdb_file) == 0
        ):
            self.create_mapping_table()
            self.migrate_mappings()

    def set_order_by(self, n):
        """Set which value we want to order by when getting data from the database."""
        order_by = [
            "LCSC Part",
            "MFR.Part",
            "Package",
            "Solder Joint",
            "Library Type",
            "Stock",
            "Manufacturer",
            "Description",
            "Price",
        ]
        if self.order_by == order_by[n] and self.order_dir == "ASC":
            self.order_dir = "DESC"
        else:
            self.order_by = order_by[n]
            self.order_dir = "ASC"

    def search(self, parameters):
        """Search the database for parts that meet the given parameters."""

        # skip searching if there are no keywords and the part number
        # field is empty as there are too many parts for the search
        # to reasonbly show the desired part
        if parameters["keyword"] == "" and (
            "part_no" not in parameters or parameters["part_no"] == ""
        ):
            return []

        # Note: this must mach the widget order in PartSelectorDialog init and
        # populate_part_list in parselector.py
        columns = [
            "LCSC Part",
            "MFR.Part",
            "Package",
            "Solder Joint",
            "Library Type",
            "Stock",
            "Manufacturer",
            "Description",
            "Price",
            "First Category"
        ]
        s = ",".join(f'"{c}"' for c in columns)
        query = f"SELECT {s} FROM parts WHERE "

        match_chunks = []
        like_chunks = []

        query_chunks = []

        # Build 'match_chunks' and 'like_chunks' arrays
        #
        # FTS5 (https://www.sqlite.org/fts5.html) has a substring limit of
        # at least 3 characters.
        # 'Substrings consisting of fewer than 3 unicode characters do not
        #  match any rows when used with a full-text query'
        #
        # However, they will still match with a LIKE.
        #
        # So extract out the <3 character strings and add a 'LIKE' term
        # for each of those.
        if parameters["keyword"] != "":
            keywords = parameters["keyword"].split(" ")
            match_keywords_intermediate = []
            for w in keywords:
                # skip over empty keywords
                if w != "":
                    if len(w) < 3:  # LIKE entry
                        kw = f"description LIKE '%{w}%'"
                        like_chunks.append(kw)
                    else:  # MATCH entry
                        kw = f'"{w}"'
                        match_keywords_intermediate.append(kw)
            if match_keywords_intermediate:
                match_entry = " AND ".join(match_keywords_intermediate)
                match_chunks.append(f"{match_entry}")

        if "manufacturer" in parameters and parameters["manufacturer"] != "":
            p = parameters["manufacturer"]
            match_chunks.append(f'"Manufacturer":"{p}"')
        if "package" in parameters and parameters["package"] != "":
            p = parameters["package"]
            match_chunks.append(f'"Package":"{p}"')
        if (
            "category" in parameters
            and parameters["category"] != ""
            and parameters["category"] != "All"
        ):
            p = parameters["category"]
            match_chunks.append(f'"First Category":"{p}"')
        if "subcategory" in parameters and parameters["subcategory"] != "":
            p = parameters["subcategory"]
            match_chunks.append(f'"Second Category":"{p}"')
        if "part_no" in parameters and parameters["part_no"] != "":
            p = parameters["part_no"]
            match_chunks.append(f'"MFR.Part":"{p}"')
        if "solder_joints" in parameters and parameters["solder_joints"] != "":
            p = parameters["solder_joints"]
            match_chunks.append(f'"Solder Joint":"{p}"')

        library_types = []
        if parameters["basic"]:
            library_types.append('"Basic"')
        if parameters["extended"]:
            library_types.append('"Extended"')
        if library_types:
            query_chunks.append(f'"Library Type" IN ({",".join(library_types)})')

        if parameters["stock"]:
            query_chunks.append('"Stock" > "0"')

        if not match_chunks and not like_chunks and not query_chunks:
            return []

        if match_chunks:
            query += "parts MATCH '"
            query += " AND ".join(match_chunks)
            query += "'"

        if like_chunks:
            if match_chunks:
                query += " AND "
            query += " AND ".join(like_chunks)

        if query_chunks:
            if match_chunks or like_chunks:
                query += " AND "
            query += " AND ".join(query_chunks)

        query += f' ORDER BY "{self.order_by}" COLLATE naturalsort {self.order_dir}'
        query += " LIMIT 1000"

        with contextlib.closing(sqlite3.connect(self.partsdb_file)) as con:
            con.create_collation("naturalsort", natural_sort_collation)
            with con as cur:
                return cur.execute(query).fetchall()

    def delete_parts_table(self):
        """Delete the parts table."""
        with contextlib.closing(sqlite3.connect(self.partsdb_file)) as con, con as cur:
            cur.execute("DROP TABLE IF EXISTS parts")
            cur.commit()

    def create_meta_table(self):
        """Create the meta table."""
        with contextlib.closing(sqlite3.connect(self.partsdb_file)) as con, con as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS meta ('filename', 'size', 'partcount', 'date', 'last_update')"
            )
            cur.commit()

    def create_rotation_table(self):
        """Create the rotation table."""
        self.logger.debug("Create SQLite table for rotations")
        with contextlib.closing(
            sqlite3.connect(self.rotationsdb_file)
        ) as con, con as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS rotation ('regex', 'correction')")
            cur.commit()

    def get_correction_data(self, regex):
        """Get the correction data by its regex."""
        with contextlib.closing(
            sqlite3.connect(self.rotationsdb_file)
        ) as con, con as cur:
            return cur.execute(
                f"SELECT * FROM rotation WHERE regex = '{regex}'"
            ).fetchone()

    def delete_correction_data(self, regex):
        """Delete a correction from the database."""
        with contextlib.closing(
            sqlite3.connect(self.rotationsdb_file)
        ) as con, con as cur:
            cur.execute(f"DELETE FROM rotation WHERE regex = '{regex}'")
            cur.commit()

    def update_correction_data(self, regex, rotation):
        """Update a correction in the database."""
        with contextlib.closing(
            sqlite3.connect(self.rotationsdb_file)
        ) as con, con as cur:
            cur.execute(
                f"UPDATE rotation SET correction = '{rotation}' WHERE regex = '{regex}'"
            )
            cur.commit()

    def insert_correction_data(self, regex, rotation):
        """Insert a correction into the database."""
        with contextlib.closing(
            sqlite3.connect(self.rotationsdb_file)
        ) as con, con as cur:
            cur.execute(
                "INSERT INTO rotation VALUES (?, ?)",
                (regex, rotation),
            )
            cur.commit()

    def get_all_correction_data(self):
        """Get all corrections from the database."""
        with contextlib.closing(
            sqlite3.connect(self.rotationsdb_file)
        ) as con, con as cur:
            try:
                result = cur.execute(
                    "SELECT * FROM rotation ORDER BY regex ASC"
                ).fetchall()
                return [(c[0], int(c[1])) for c in result]
            except sqlite3.OperationalError:
                return []

    def create_mapping_table(self):
        """Create the mapping table."""
        with contextlib.closing(
            sqlite3.connect(self.mappingsdb_file)
        ) as con, con as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS mapping ('footprint', 'value', 'LCSC')"
            )
            cur.commit()

    def get_mapping_data(self, footprint, value):
        """Get the mapping data by its regex."""
        with contextlib.closing(
            sqlite3.connect(self.mappingsdb_file)
        ) as con, con as cur:
            return cur.execute(
                f"SELECT * FROM mapping WHERE footprint = '{footprint}' AND value = '{value}'"
            ).fetchone()

    def delete_mapping_data(self, footprint, value):
        """Delete a mapping from the database."""
        with contextlib.closing(
            sqlite3.connect(self.mappingsdb_file)
        ) as con, con as cur:
            cur.execute(
                f"DELETE FROM mapping WHERE footprint = '{footprint}' AND value = '{value}'"
            )
            cur.commit()

    def update_mapping_data(self, footprint, value, LCSC):
        """Update a mapping in the database."""
        with contextlib.closing(
            sqlite3.connect(self.mappingsdb_file)
        ) as con, con as cur:
            cur.execute(
                f"UPDATE mapping SET LCSC = '{LCSC}' WHERE footprint = '{footprint}' AND value = '{value}'"
            )
            cur.commit()

    def insert_mapping_data(self, footprint, value, LCSC):
        """Insert a mapping into the database."""
        with contextlib.closing(
            sqlite3.connect(self.mappingsdb_file)
        ) as con, con as cur:
            cur.execute(
                "INSERT INTO mapping VALUES (?, ?, ?)",
                (footprint, value, LCSC),
            )
            cur.commit()

    def get_all_mapping_data(self):
        """Get all mapping from the database."""
        with contextlib.closing(
            sqlite3.connect(self.mappingsdb_file)
        ) as con, con as cur:
            return [
                list(c)
                for c in cur.execute(
                    "SELECT * FROM mapping ORDER BY footprint ASC"
                ).fetchall()
            ]

    def create_parts_table(self, columns):
        """Create the parts table."""
        with contextlib.closing(sqlite3.connect(self.partsdb_file)) as con, con as cur:
            cols = ",".join([f" '{c}'" for c in columns])
            cur.execute(f"CREATE TABLE IF NOT EXISTS parts ({cols})")
            cur.commit()

    def get_part_details(self, number: str) -> dict:
        """Get the part details for a LCSC number using optimized FTS5 querying."""
        with contextlib.closing(sqlite3.connect(self.partsdb_file)) as con:
            con.row_factory = dict_factory  # noqa: DC05
            cur = con.cursor()
            query = """SELECT "LCSC Part" AS lcsc, "Stock" AS stock, "Library Type" AS type,
                "MFR.Part" as part_no, "Description" as description, "Package" as package,
                "First Category" as category
                FROM parts WHERE parts MATCH :number"""
            cur.execute(query, {"number": number})
            return next((n for n in cur.fetchall() if n["lcsc"] == number), {})

    def update(self):
        """Update the sqlite parts database from the JLCPCB CSV."""
        Thread(target=self.download).start()

    def download(self):
        """Actual worker thread that downloads and imports the parts data."""
        self.state = LibraryState.DOWNLOAD_RUNNING
        start = time.time()
        wx.PostEvent(self.parent, ResetGaugeEvent())
        # Download the zipped parts database
        url_stub = "https://bouni.github.io/kicad-jlcpcb-tools/"
        cnt_file = "chunk_num_fts5.txt"
        cnt = 0
        chunk_file_stub = "parts-fts5.db.zip."
        try:
            r = requests.get(
                url_stub + cnt_file, allow_redirects=True, stream=True, timeout=300
            )
            if r.status_code != requests.codes.ok:  # pylint: disable=no-member
                wx.PostEvent(
                    self.parent,
                    MessageEvent(
                        title="HTTP GET Error",
                        text=f"Failed to fetch count of database parts, error code {r.status_code}\n"
                        + "URL was:\n"
                        f"'{url_stub + cnt_file}'",
                        style="error",
                    ),
                )
                self.state = LibraryState.INITIALIZED
                self.create_tables(["placeholder_invalid_column_fix_errors"])
                return

            self.logger.debug(
                "Parts db is split into %s parts. Proceeding to download...", r.text
            )
            cnt = int(r.text)
            self.logger.debug("Removing any spurious old zip part files...")
            for p in glob(str(Path(self.datadir) / (chunk_file_stub + "*"))):
                self.logger.debug("Removing %s.", p)
                os.unlink(p)
        except Exception as e:  # pylint: disable=broad-exception-caught
            wx.PostEvent(
                self.parent,
                MessageEvent(
                    title="Download Error",
                    text=f"Failed to download the JLCPCB database, {e}",
                    style="error",
                ),
            )
            self.state = LibraryState.INITIALIZED
            self.create_tables(["placeholder_invalid_column_fix_errors"])
            return

        for i in range(cnt):
            chunk_file = chunk_file_stub + f"{i+1:03}"
            with open(os.path.join(self.datadir, chunk_file), "wb") as f:
                try:
                    r = requests.get(
                        url_stub + chunk_file,
                        allow_redirects=True,
                        stream=True,
                        timeout=300,
                    )
                    if r.status_code != requests.codes.ok:  # pylint: disable=no-member
                        wx.PostEvent(
                            self.parent,
                            MessageEvent(
                                title="Download Error",
                                text=f"Failed to download the JLCPCB database, error code {r.status_code}\n"
                                + "URL was:\n"
                                f"'{url_stub + chunk_file}'",
                                style="error",
                            ),
                        )
                        self.state = LibraryState.INITIALIZED
                        self.create_tables(["placeholder_invalid_column_fix_errors"])
                        return

                    size = int(r.headers.get("Content-Length"))
                    self.logger.debug(
                        "Download parts db chunk %d of %d with a size of %.2fMB",
                        i + 1,
                        cnt,
                        size / 1024 / 1024,
                    )
                    for data in r.iter_content(chunk_size=4096):
                        f.write(data)
                        progress = f.tell() / size * 100
                        wx.PostEvent(self.parent, UpdateGaugeEvent(value=progress))
                except Exception as e:  # pylint: disable= broad-exception-caught
                    wx.PostEvent(
                        self.parent,
                        MessageEvent(
                            title="Download Error",
                            text=f"Failed to download the JLCPCB database, {e}",
                            style="error",
                        ),
                    )
                    self.state = LibraryState.INITIALIZED
                    self.create_tables(["placeholder_invalid_column_fix_errors"])
                    return
        # rename existing parts-fts5.db to parts-fts5.db.bak, delete already existing bak file if neccesary
        if os.path.exists(self.partsdb_file):
            if os.path.exists(f"{self.partsdb_file}.bak"):
                os.remove(f"{self.partsdb_file}.bak")
            os.rename(self.partsdb_file, f"{self.partsdb_file}.bak")
        # unzip downloaded parts.zip
        self.logger.debug("Combining and extracting zip part files...")
        try:
            unzip_parts(self.datadir)
        except Exception as e:  # pylint: disable=broad-exception-caught
            wx.PostEvent(
                self.parent,
                MessageEvent(
                    title="Extract Error",
                    text=f"Failed to combine and extract the JLCPCB database, {e}",
                    style="error",
                ),
            )
            self.state = LibraryState.INITIALIZED
            self.create_tables(["placeholder_invalid_column_fix_errors"])
            return
        # check if partsdb_file was successfully extracted
        if not os.path.exists(self.partsdb_file):
            if os.path.exists(f"{self.partsdb_file}.bak"):
                os.rename(f"{self.partsdb_file}.bak", self.partsdb_file)
                wx.PostEvent(
                    self.parent,
                    MessageEvent(
                        title="Download Error",
                        text="Failed to download the JLCPCB database, db was not extracted from zip",
                        style="error",
                    ),
                )
                self.state = LibraryState.INITIALIZED
                self.create_tables(["placeholder_invalid_column_fix_errors"])
                return
        else:
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

    def create_tables(self, headers):
        """Create all tables."""
        self.create_meta_table()
        self.delete_parts_table()
        self.create_parts_table(headers)
        self.create_rotation_table()
        self.create_mapping_table()

    @property
    def categories(self):
        """The primary categories in the database.

        Caching the relatively small set of category and subcategory maps
        gives a noticeable speed improvement over repeatedly reading the
        information from the on-disk database.
        """
        if not self.category_map:
            self.category_map.setdefault("", "")

            # Populate the cache.
            with contextlib.closing(
                sqlite3.connect(self.partsdb_file)
            ) as con, con as cur:
                for row in cur.execute(
                    'SELECT * from categories ORDER BY UPPER("First Category"), UPPER("Second Category")'
                ):
                    self.category_map.setdefault(row[0], []).append(row[1])
        tmp = list(self.category_map.keys())
        tmp.insert(0, "All")
        return tmp

    def get_subcategories(self, category):
        """Get the subcategories associated with the given category."""
        return self.category_map[category]

    def migrate_rotations(self):
        """Migrate existing rotations from parts db to rotations db."""
        with contextlib.closing(
            sqlite3.connect(self.partsdb_file)
        ) as pdb, contextlib.closing(
            sqlite3.connect(self.rotationsdb_file)
        ) as rdb, pdb as pcur, rdb as rcur:
            try:
                result = pcur.execute(
                    "SELECT * FROM rotation ORDER BY regex ASC"
                ).fetchall()
                if not result:
                    return
                for r in result:
                    rcur.execute(
                        "INSERT INTO rotation VALUES (?, ?)",
                        (r[0], r[1]),
                    )
                    rcur.commit()
                self.logger.debug(
                    "Migrated %d rotations to sepetrate database.", len(result)
                )
                pcur.execute("DROP TABLE IF EXISTS rotation")
                pcur.commit()
                self.logger.debug("Droped rotations table from parts database.")
            except sqlite3.OperationalError:
                return

    def migrate_mappings(self):
        """Migrate existing mappings from parts db to mappings db."""
        with contextlib.closing(
            sqlite3.connect(self.partsdb_file)
        ) as pdb, contextlib.closing(
            sqlite3.connect(self.mappingsdb_file)
        ) as mdb, pdb as pcur, mdb as mcur:
            try:
                result = pcur.execute(
                    "SELECT * FROM mapping ORDER BY footprint ASC"
                ).fetchall()
                if not result:
                    return
                for r in result:
                    mcur.execute(
                        "INSERT INTO mapping VALUES (?, ?)",
                        (r[0], r[1]),
                    )
                    mcur.commit()
                self.logger.debug(
                    "Migrated %d mappings to sepetrate database.", len(result)
                )
                pcur.execute("DROP TABLE IF EXISTS mapping")
                pcur.commit()
                self.logger.debug("Droped mappings table from parts database.")
            except sqlite3.OperationalError:
                return

    def get_last_update(self) -> str:
        """Get last update from meta table."""
        with contextlib.closing(sqlite3.connect(self.partsdb_file)) as con, con as cur:
            try:
                last_update = cur.execute("SELECT last_update FROM meta").fetchone()
                if last_update:
                    return last_update[0]
                return ""
            except sqlite3.OperationalError:
                return ""
