"""Handle the JLCPCB parts database."""

import contextlib
from enum import Enum
import logging
import os
from pathlib import Path
import sqlite3
from threading import Thread
import time
from typing import NamedTuple, Optional

import requests  # pylint: disable=import-error
import wx  # pylint: disable=import-error

from .events import (
    DownloadCompletedEvent,
    DownloadProgressEvent,
    DownloadStartedEvent,
    MessageEvent,
)
from .helpers import PLUGIN_PATH, dict_factory, natural_sort_collation
from .unzip_parts import unzip_parts


class PartsDatabaseInfo(NamedTuple):
    """Information about the parts database."""

    last_update: str
    size: int
    part_count: int


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
        self.localcorrectionsdb_file = os.path.join(
            self.parent.project_path, "jlcpcb", "project.db"
        )
        self.globalcorrectionsdb_file = os.path.join(self.datadir, "corrections.db")
        self.correctionsdb_file = (
            self.globalcorrectionsdb_file
            if self.uses_global_correction_database()
            else self.localcorrectionsdb_file
        )
        self.mappingsdb_file = os.path.join(self.datadir, "mappings.db")
        self.state = None
        self.category_map = {}

        self.logger.debug("partsdb_file %s", self.partsdb_file)
        self.logger.debug("sqlite.sqlite_version %s", sqlite3.sqlite_version)

        self.setup()
        self.check_library()

    def setup(self):
        """Check if folders and database exist, setup if not."""
        if not os.path.isdir(self.datadir):
            self.logger.info(
                "Data directory '%s' does not exist and will be created.", self.datadir
            )
            Path(self.datadir).mkdir(parents=True, exist_ok=True)
        else:
            self.logger.info("Data directory '%s' exists, not creating", self.datadir)

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
            not os.path.isfile(self.correctionsdb_file)
            or os.path.getsize(self.correctionsdb_file) == 0
        ):
            self.create_correction_table()
            self.migrate_corrections()
        if (
            not os.path.isfile(self.mappingsdb_file)
            or os.path.getsize(self.mappingsdb_file) == 0
        ):
            self.create_mapping_table()
            self.migrate_mappings()

    def uses_global_correction_database(self):
        """Check if there is a board specific corrections database or not.

        Returns True if the global database is used.
        """

        try:
            with (
                contextlib.closing(
                    sqlite3.connect(self.localcorrectionsdb_file)
                ) as ldb,
                ldb as lcur,
            ):
                result = lcur.execute(
                    "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='correction')"
                ).fetchone()
                if not result:
                    return True

                return result[0] != 1
        except sqlite3.OperationalError:
            return True

        return True

    def switch_to_global_correction_database(self, use_global):
        """Switches to global or board local database."""

        currently_using_global = (
            self.correctionsdb_file == self.globalcorrectionsdb_file
        )
        if currently_using_global == use_global:
            return

        if use_global:
            try:
                with (
                    contextlib.closing(
                        sqlite3.connect(self.localcorrectionsdb_file)
                    ) as con,
                    con as cur,
                ):
                    cur.execute("DROP TABLE IF EXISTS correction")
                    cur.commit()
                self.correctionsdb_file = self.globalcorrectionsdb_file
            except OSError:
                self.logger.warning("Failed to remove board local corrections file.")
        else:
            global_corrections = self.get_all_correction_data()
            self.correctionsdb_file = self.localcorrectionsdb_file
            self.create_correction_table()
            for regex, rotation, offset in global_corrections:
                self.insert_correction_data(regex, rotation, offset)

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
            "First Category",
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

        self.logger.debug("query '%s'", query)

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

    def create_correction_table(self):
        """Create the correction table."""
        self.logger.debug("Create SQLite table for corrections")
        with (
            contextlib.closing(sqlite3.connect(self.correctionsdb_file)) as con,
            con as cur,
        ):
            cur.execute(
                "CREATE TABLE IF NOT EXISTS correction ('regex', 'rotation', 'offset_x', 'offset_y')"
            )
            cur.commit()

    def get_correction_data(self, regex):
        """Get the correction data by its regex."""
        with (
            contextlib.closing(sqlite3.connect(self.correctionsdb_file)) as con,
            con as cur,
        ):
            return cur.execute(
                f"SELECT * FROM correction WHERE regex = '{regex}'"
            ).fetchone()

    def delete_correction_data(self, regex):
        """Delete a correction from the database."""
        with (
            contextlib.closing(sqlite3.connect(self.correctionsdb_file)) as con,
            con as cur,
        ):
            cur.execute(f"DELETE FROM correction WHERE regex = '{regex}'")
            cur.commit()

    def update_correction_data(self, regex, rotation, offset):
        """Update a correction in the database."""
        with (
            contextlib.closing(sqlite3.connect(self.correctionsdb_file)) as con,
            con as cur,
        ):
            cur.execute(
                f"UPDATE correction SET rotation = '{rotation}', offset_x = '{offset[0]}', offset_y = '{offset[1]}' WHERE regex = '{regex}'"
            )
            cur.commit()

    def insert_correction_data(self, regex, rotation, offset):
        """Insert a correction into the database."""
        with (
            contextlib.closing(sqlite3.connect(self.correctionsdb_file)) as con,
            con as cur,
        ):
            cur.execute(
                "INSERT INTO correction VALUES (?, ?, ?, ?)",
                (regex, rotation, offset[0], offset[1]),
            )
            cur.commit()

    def get_all_correction_data(self):
        """Get all corrections from the database."""
        with (
            contextlib.closing(sqlite3.connect(self.correctionsdb_file)) as con,
            con as cur,
        ):
            try:
                result = cur.execute(
                    "SELECT * FROM correction ORDER BY regex ASC"
                ).fetchall()
                return [(c[0], int(c[1]), (float(c[2]), float(c[3]))) for c in result]
            except sqlite3.OperationalError:
                return []

    def create_mapping_table(self):
        """Create the mapping table."""
        with (
            contextlib.closing(sqlite3.connect(self.mappingsdb_file)) as con,
            con as cur,
        ):
            cur.execute(
                "CREATE TABLE IF NOT EXISTS mapping ('footprint', 'value', 'LCSC')"
            )
            cur.commit()

    def get_mapping_data(self, footprint, value):
        """Get the mapping data by its regex."""
        with (
            contextlib.closing(sqlite3.connect(self.mappingsdb_file)) as con,
            con as cur,
        ):
            return cur.execute(
                f"SELECT * FROM mapping WHERE footprint = '{footprint}' AND value = '{value}'"
            ).fetchone()

    def delete_mapping_data(self, footprint, value):
        """Delete a mapping from the database."""
        with (
            contextlib.closing(sqlite3.connect(self.mappingsdb_file)) as con,
            con as cur,
        ):
            cur.execute(
                f"DELETE FROM mapping WHERE footprint = '{footprint}' AND value = '{value}'"
            )
            cur.commit()

    def update_mapping_data(self, footprint, value, LCSC):
        """Update a mapping in the database."""
        with (
            contextlib.closing(sqlite3.connect(self.mappingsdb_file)) as con,
            con as cur,
        ):
            cur.execute(
                f"UPDATE mapping SET LCSC = '{LCSC}' WHERE footprint = '{footprint}' AND value = '{value}'"
            )
            cur.commit()

    def insert_mapping_data(self, footprint, value, LCSC):
        """Insert a mapping into the database."""
        with (
            contextlib.closing(sqlite3.connect(self.mappingsdb_file)) as con,
            con as cur,
        ):
            cur.execute(
                "INSERT INTO mapping VALUES (?, ?, ?)",
                (footprint, value, LCSC),
            )
            cur.commit()

    def get_all_mapping_data(self):
        """Get all mapping from the database."""
        with (
            contextlib.closing(sqlite3.connect(self.mappingsdb_file)) as con,
            con as cur,
        ):
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
            con.row_factory = dict_factory
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
        wx.PostEvent(self.parent, DownloadStartedEvent())

        # Define basic variables
        url_stub = "https://bouni.github.io/kicad-jlcpcb-tools/"
        cnt_file = "chunk_num_fts5.txt"
        progress_file = os.path.join(self.datadir, "progress.txt")
        chunk_file_stub = "parts-fts5.db.zip."
        completed_chunks = set()

        # Check if there is a progress file
        if os.path.exists(progress_file):
            with open(progress_file) as f:
                # Read completed chunk indices from the progress file
                completed_chunks = {int(line.strip()) for line in f.readlines()}

        # Get the total number of chunks to download
        try:
            r = requests.get(
                url_stub + cnt_file, allow_redirects=True, stream=True, timeout=300
            )
            if r.status_code != requests.codes.ok:
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
                return

            total_chunks = int(r.text)
        except Exception as e:
            wx.PostEvent(
                self.parent,
                MessageEvent(
                    title="Download Error",
                    text=f"Failed to fetch database chunk count, {e}",
                    style="error",
                ),
            )
            self.state = LibraryState.INITIALIZED
            return

        # Re-download incomplete or missing chunks
        for i in range(total_chunks):
            chunk_index = i + 1
            chunk_file = chunk_file_stub + f"{chunk_index:03}"
            chunk_path = os.path.join(self.datadir, chunk_file)

            # Check if the chunk is logged as completed but the file might be incomplete
            if chunk_index in completed_chunks:
                if os.path.exists(chunk_path):
                    # Validate the size of the chunk file
                    try:
                        expected_size = int(
                            requests.head(
                                url_stub + chunk_file, timeout=300
                            ).headers.get("Content-Length", 0)
                        )
                        actual_size = os.path.getsize(chunk_path)
                        if actual_size == expected_size:
                            self.logger.debug(
                                "Skipping already downloaded and validated chunk %d.",
                                chunk_index,
                            )
                            continue
                        else:
                            self.logger.warning(
                                "Chunk %d is incomplete, re-downloading.", chunk_index
                            )
                    except Exception as e:
                        self.logger.warning(
                            "Unable to validate chunk %d, re-downloading. Error: %s",
                            chunk_index,
                            e,
                        )
                else:
                    self.logger.warning(
                        "Chunk %d marked as completed but file is missing, re-downloading.",
                        chunk_index,
                    )

            # Download the chunk
            try:
                with open(chunk_path, "wb") as f:
                    r = requests.get(
                        url_stub + chunk_file,
                        allow_redirects=True,
                        stream=True,
                        timeout=300,
                    )
                    if r.status_code != requests.codes.ok:
                        wx.PostEvent(
                            self.parent,
                            MessageEvent(
                                title="Download Error",
                                text=f"Failed to download chunk {chunk_index}, error code {r.status_code}\n"
                                + "URL was:\n"
                                f"'{url_stub + chunk_file}'",
                                style="error",
                            ),
                        )
                        self.state = LibraryState.INITIALIZED
                        return

                    size = int(r.headers.get("Content-Length", 0))
                    self.logger.debug(
                        "Downloading chunk %d/%d (%.2f MB)",
                        chunk_index,
                        total_chunks,
                        size / 1024 / 1024,
                    )
                    for data in r.iter_content(chunk_size=4096):
                        f.write(data)
                        progress = f.tell() / size * 100
                        wx.PostEvent(self.parent, DownloadProgressEvent(value=progress))
                    self.logger.debug("Chunk %d downloaded successfully.", chunk_index)

                # Update progress file after successful download
                with open(progress_file, "a") as f:
                    f.write(f"{chunk_index}\n")

            except Exception as e:
                wx.PostEvent(
                    self.parent,
                    MessageEvent(
                        title="Download Error",
                        text=f"Failed to download chunk {chunk_index}, {e}",
                        style="error",
                    ),
                )
                self.state = LibraryState.INITIALIZED
                return

        # Delete progress file to indicate the download is complete
        if os.path.exists(progress_file):
            os.remove(progress_file)

        # Combine and extract downloaded files
        self.logger.debug("Combining and extracting zip part files...")
        try:
            unzip_parts(self.parent, self.datadir)
        except Exception as e:
            wx.PostEvent(
                self.parent,
                MessageEvent(
                    title="Extract Error",
                    text=f"Failed to combine and extract the JLCPCB database, {e}",
                    style="error",
                ),
            )
            self.state = LibraryState.INITIALIZED
            return

        # Check if the database file was successfully extracted
        if not os.path.exists(self.partsdb_file):
            wx.PostEvent(
                self.parent,
                MessageEvent(
                    title="Download Error",
                    text="Failed to extract the database file from the downloaded zip.",
                    style="error",
                ),
            )
            self.state = LibraryState.INITIALIZED
            return

        wx.PostEvent(self.parent, DownloadCompletedEvent())
        end = time.time()
        wx.PostEvent(
            self.parent,
            MessageEvent(
                title="Success",
                text=f"Successfully downloaded and imported the JLCPCB database in {end - start:.2f} seconds!",
                style="info",
            ),
        )
        self.state = LibraryState.INITIALIZED

    def create_tables(self, headers):
        """Create all tables."""
        self.create_meta_table()
        self.delete_parts_table()
        self.create_parts_table(headers)
        self.create_correction_table()
        self.create_mapping_table()

    @property
    def categories(self):
        """The primary categories in the database.

        Caching the relatively small set of category and subcategory maps
        gives a noticeable speed improvement over repeatedly reading the
        information from the on-disk database.
        """
        if not self.category_map:
            self.category_map.setdefault("", [])

            # Populate the cache.
            with (
                contextlib.closing(sqlite3.connect(self.partsdb_file)) as con,
                con as cur,
            ):
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

    def migrate_corrections_from_rotation(self):
        """Migrate existing rotations from rotation db to correction db."""
        if not os.path.exists(self.rotationsdb_file):
            return
        with (
            contextlib.closing(sqlite3.connect(self.rotationsdb_file)) as rdb,
            contextlib.closing(sqlite3.connect(self.correctionsdb_file)) as cdb,
            rdb as rcur,
            cdb as ccur,
        ):
            try:
                result = rcur.execute(
                    "SELECT * FROM rotation ORDER BY regex ASC"
                ).fetchall()
                if not result:
                    return
                for r in result:
                    ccur.execute(
                        "INSERT INTO correction VALUES (?, ?, 0, 0)",
                        (r[0], r[1]),
                    )
                    ccur.commit()
                self.logger.debug(
                    "Migrated %d rotations to corrections database.", len(result)
                )
                os.remove(self.rotationsdb_file)
                self.logger.debug("Deleted rotations database.")
            except sqlite3.OperationalError:
                return
            except OSError:
                return

    def migrate_corrections_from_parts(self):
        """Migrate existing rotations from parts db to correction db."""
        with (
            contextlib.closing(sqlite3.connect(self.partsdb_file)) as pdb,
            contextlib.closing(sqlite3.connect(self.correctionsdb_file)) as rdb,
            pdb as pcur,
            rdb as rcur,
        ):
            try:
                result = pcur.execute(
                    "SELECT * FROM rotation ORDER BY regex ASC"
                ).fetchall()
                if not result:
                    return
                for r in result:
                    rcur.execute(
                        "INSERT INTO correction VALUES (?, ?, 0, 0)",
                        (r[0], r[1]),
                    )
                    rcur.commit()
                self.logger.debug(
                    "Migrated %d rotations to separate database.", len(result)
                )
                pcur.execute("DROP TABLE IF EXISTS rotation")
                pcur.commit()
                self.logger.debug("Droped rotations table from parts database.")
            except sqlite3.OperationalError:
                return

    def migrate_corrections(self):
        """Migrate existing rotations from old rotation db and parts db to correction db."""
        self.migrate_corrections_from_rotation()
        self.migrate_corrections_from_parts()

    def migrate_mappings(self):
        """Migrate existing mappings from parts db to mappings db."""
        with (
            contextlib.closing(sqlite3.connect(self.partsdb_file)) as pdb,
            contextlib.closing(sqlite3.connect(self.mappingsdb_file)) as mdb,
            pdb as pcur,
            mdb as mcur,
        ):
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

    def get_parts_db_info(self) -> Optional[PartsDatabaseInfo]:
        """Retrieve the database information."""
        with contextlib.closing(sqlite3.connect(self.partsdb_file)) as con, con as cur:
            try:
                meta = cur.execute(
                    "SELECT last_update, size, partcount FROM meta"
                ).fetchone()
                if meta:
                    return PartsDatabaseInfo(meta[0], meta[1], meta[2])
                return None
            except sqlite3.OperationalError:
                return None
