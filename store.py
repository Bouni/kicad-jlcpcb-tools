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
    natural_sort_collation,
)


class Store:
    """A storage class to get data from a sqlite database and write it back"""

    def __init__(self, project_path):
        self.logger = logging.getLogger(__name__)
        self.project_path = project_path
        self.datadir = os.path.join(self.project_path, "jlcpcb")
        self.dbfile = os.path.join(self.datadir, "project.db")
        self.setup()
        self.update_from_board()

    def setup(self):
        """Check if folders and database exist, setup if not"""
        if not os.path.isdir(self.datadir):
            self.logger.info(
                "Data directory 'jlcpcb' does not exist and will be created."
            )
            Path(self.datadir).mkdir(parents=True, exist_ok=True)
        self.create_db()

    def create_db(self):
        """Create the sqlite database tables."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS part_info ("
                    "reference NOT NULL PRIMARY KEY,"
                    "value TEXT NOT NULL,"
                    "footprint TEXT NOT NULL,"
                    "lcsc TEXT,"
                    "exclude_from_bom NUMERIC DEFAULT 0,"
                    "exclude_from_pos NUMERIC DEFAULT 0"
                    ")"
                )
            cur.commit()

    def read_all(self):
        """Read all parts from the database."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            con.create_collation("naturalsort", natural_sort_collation)
            with con as cur:
                return [
                    list(part)
                    for part in cur.execute(
                        "SELECT * FROM part_info ORDER BY reference COLLATE naturalsort ASC"
                    ).fetchall()
                ]

    def create_part(self, part):
        """Create a part in the database."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                cur.execute("INSERT INTO part_info VALUES (?,?,?,?,?,?)", part)
                cur.commit()

    def update_part(self, part):
        """Update a part in the database, overwrite lcsc if supplied."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                if len(part) == 6:
                    cur.execute(
                        "UPDATE part_info set value = ?, footprint = ?, lcsc = ?, exclude_from_bom = ?, exclude_from_pos = ? WHERE reference = ?",
                        part[1:] + part[0:1],
                    )
                else:
                    cur.execute(
                        "UPDATE part_info set value = ?, footprint = ?, exclude_from_bom = ?, exclude_from_pos = ? WHERE reference = ?",
                        part[1:] + part[0:1],
                    )

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

    def set_bom(self, ref, state):
        """Change the BOM attribute for a part in the database."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                cur.execute(
                    f"UPDATE part_info SET exclude_from_bom = '{int(state)}' WHERE reference = '{ref}'"
                )
                cur.commit()

    def set_pos(self, ref, state):
        """Change the BOM attribute for a part in the database."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                cur.execute(
                    f"UPDATE part_info SET exclude_from_pos = '{int(state)}' WHERE reference = '{ref}'"
                )
                cur.commit()

    def set_lcsc(self, ref, value):
        """Change the BOM attribute for a part in the database."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                cur.execute(
                    f"UPDATE part_info SET lcsc = '{value}' WHERE reference = '{ref}'"
                )
                cur.commit()

    def update_from_board(self):
        """Read all footprints from the board and insert them into the database if they do not exist."""
        board = GetBoard()
        for fp in get_valid_footprints(board):
            part = [
                fp.GetReference(),
                fp.GetValue(),
                str(fp.GetFPID().GetLibItemName()),
                get_lcsc_value(fp),
                get_exclude_from_bom(fp),
                get_exclude_from_pos(fp),
            ]
            dbpart = self.get_part(part[0])
            # if part is not in the database yet, create it
            if not dbpart:
                self.logger.debug(
                    f"Part {part[0]} does not exist in the database and will be created from the board."
                )
                self.create_part(part)
            # if part in the database, has no lcsc value the board part has a lcsc value, update including lcsc
            elif dbpart and not dbpart[3] and part[3]:
                self.logger.debug(
                    f"Part {part[0]} is already in the database but without lcsc value, so the value supplied from the board will be set."
                )
                self.update_part(part)
            # if part in the database, has a lcsc value, update except lcsc
            elif dbpart and dbpart[3] and part[3]:
                self.logger.debug(
                    f"Part {part[0]} is already in the database and has a lcsc value, the value supplied from the board will be ignored."
                )
                part.pop(3)
                self.update_part(part)

        self.clean_database()

    def clean_database(self):
        """Delete all parts from the database that are no longer present on the board."""
        refs = [f"'{fp.GetReference()}'" for fp in get_valid_footprints(GetBoard())]
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            with con as cur:
                cur.execute(
                    f"DELETE FROM part_info WHERE reference NOT IN ({','.join(refs)})"
                )
                cur.commit()
