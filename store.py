import contextlib
import logging
import os
import sqlite3
from pathlib import Path

from pcbnew import GetBoard

from .helpers import (
    get_exclude_from_bom,
    get_exclude_from_pos,
    get_footprint_by_ref,
    get_lcsc_value,
    get_valid_footprints,
)


class Store:
    """A storage class to get data from a sqlite database and write it back"""

    def __init__(self, project_path):
        self.logger = logging.getLogger(__name__)
        self.project_path = project_path
        self.datadir = os.path.join(self.project_path, "jlcpcb")
        self.dbfile = os.path.join(self.datadir, "project.db")
        self.setup()
        self.sync_board()

    def setup(self):
        """Check if folders and database exist, setup if not"""
        if not os.path.isdir(self.datadir):
            self.logger.info(
                "Data directory 'jlcpcb' does not exist and will be created."
            )
            Path(self.datadir).mkdir(parents=True, exist_ok=True)
        if not os.path.isfile(self.dbfile):
            self.logger.info("Database does not exist and will be created.")
            self.create_db()

    def create_db(self):
        """Create the sqlite database tables."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS part_info"
                    "(reference NOT NULL PRIMARY KEY,value ,footprint,lcsc,stock,price,bom,cpl)"
                )
            cur.commit()

    def read_all(self):
        """Read all parts from the database."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                return cur.execute(
                    "SELECT * FROM part_info ORDER BY reference COLLATE NOCASE ASC"
                ).fetchall()

    def create_or_update(self, part):
        """Create or update a pert in the database."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                cur.execute("REPLACE INTO part_info VALUES (?,?,?,?,?,?,?,?)", part)
                cur.commit()

    def get_part(self, ref):
        """Get a part from the database by its reference."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                return cur.execute(
                    "SELECT * FROM part_info WHERE reference=?", (ref,)
                ).fetchone()

    def delete_part(self, ref):
        """Delete a part from the database by its reference."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                cur.execute("DELETE FROM part_info WHERE reference=?", (ref,))
                cur.commit()

    def sync_part(self, ref):
        """Read a part from the board and overwrite the values in the database."""
        fp = get_footprint_by_ref(GetBoard(), ref)
        part = [
            fp.GetReference(),
            fp.GetValue(),
            str(fp.GetFPID().GetLibItemName()),
            get_lcsc_value(fp),
            "",
            "",
            "No" if get_exclude_from_bom(fp) else "Yes",
            "No" if get_exclude_from_pos(fp) else "Yes",
        ]
        self.create_or_update(part)

    def sync_board(self):
        """Read all footprints from the board and insert them into the database if they do not exist."""
        board = GetBoard()
        for fp in get_valid_footprints(board):
            part = [
                fp.GetReference(),
                fp.GetValue(),
                str(fp.GetFPID().GetLibItemName()),
                get_lcsc_value(fp),
                "",
                "",
                "No" if get_exclude_from_bom(fp) else "Yes",
                "No" if get_exclude_from_pos(fp) else "Yes",
            ]
            if not self.get_part(part[0]):
                self.logger.info(
                    f"Part {part[0]} does not exist in the database and will be created from the board."
                )
                self.create_or_update(part)
            else:
                self.logger.info(
                    f"Part {part[0]} exists in the database and will be loaded from the database."
                )
