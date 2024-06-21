"""Contains the data storge for a project."""

import contextlib
import csv
import logging
import os
from pathlib import Path
import sqlite3

from .helpers import (
    dict_factory,
    get_exclude_from_bom,
    get_exclude_from_pos,
    get_lcsc_value,
    get_valid_footprints,
    natural_sort_collation,
)


class Store:
    """A storage class to get data from a sqlite database and write it back."""

    def __init__(self, parent, project_path, board):
        self.logger = logging.getLogger(__name__)
        self.parent = parent
        self.project_path = project_path
        self.board = board
        self.datadir = os.path.join(self.project_path, "jlcpcb")
        self.dbfile = os.path.join(self.datadir, "project.db")
        self.order_by = "reference"
        self.order_dir = "ASC"
        self.setup()
        self.update_from_board()

    def setup(self):
        """Check if folders and database exist, setup if not."""
        if not os.path.isdir(self.datadir):
            self.logger.info(
                "Data directory 'jlcpcb' does not exist and will be created."
            )
            Path(self.datadir).mkdir(parents=True, exist_ok=True)
        self.create_db()

    def set_order_by(self, n):
        """Set which value we want to order by when getting data from the database."""
        if n > 7:
            return
        # The following two cases are just a temporary hack and will eventually be replaced by
        # direct sorting via DataViewListCtrl rather than via SQL query
        if n == 4:
            return
        if n > 4:
            n = n - 1
        order_by = [
            "reference",
            "value",
            "footprint",
            "lcsc",
            "stock",
            "exclude_from_bom",
            "exclude_from_pos",
        ]
        if self.order_by == order_by[n] and self.order_dir == "ASC":
            self.order_dir = "DESC"
        else:
            self.order_by = order_by[n]
            self.order_dir = "ASC"

    def create_db(self):
        """Create the sqlite database tables."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con, con as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS part_info ("
                "reference NOT NULL PRIMARY KEY,"
                "value TEXT NOT NULL,"
                "footprint TEXT NOT NULL,"
                "lcsc TEXT,"
                "stock NUMERIC,"
                "exclude_from_bom NUMERIC DEFAULT 0,"
                "exclude_from_pos NUMERIC DEFAULT 0"
                ")",
            )
            cur.commit()

    def read_all(self):
        """Read all parts from the database."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            con.create_collation("naturalsort", natural_sort_collation)
            con.row_factory = dict_factory
            with con as cur:
                return cur.execute(
                    f"SELECT * FROM part_info ORDER BY {self.order_by} COLLATE naturalsort {self.order_dir}"
                ).fetchall()

    def read_bom_parts(self):
        """Read all parts that should be included in the BOM."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con, con as cur:
            # Query all parts that are supposed to be in the BOM an have an lcsc number, group the references together
            subquery = "SELECT value, reference, footprint, lcsc FROM part_info WHERE exclude_from_bom = '0' AND lcsc != '' ORDER BY lcsc, reference"
            query = f"SELECT value, GROUP_CONCAT(reference) AS refs, footprint, lcsc  FROM ({subquery}) GROUP BY lcsc"
            a = [list(part) for part in cur.execute(query).fetchall()]
            # Query all parts that are supposed to be in the BOM but have no lcsc number
            query = "SELECT value, reference, footprint, lcsc FROM part_info WHERE exclude_from_bom = '0' AND lcsc = ''"
            b = [list(part) for part in cur.execute(query).fetchall()]
            return a + b

    def read_pos_parts(self):
        """Read all parts that should be included in the POS."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            con.create_collation("naturalsort", natural_sort_collation)
            with con as cur:
                # Query all parts that are supposed to be in the POS
                query = "SELECT reference, value, footprint, lcsc FROM part_info WHERE exclude_from_pos = '0' ORDER BY reference COLLATE naturalsort ASC"
                return [list(part) for part in cur.execute(query).fetchall()]

    def create_part(self, part):
        """Create a part in the database."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con, con as cur:
            cur.execute("INSERT INTO part_info VALUES (?,?,?,?,'',?,?)", part)
            cur.commit()

    def update_part(self, part):
        """Update a part in the database, overwrite lcsc if supplied."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con, con as cur:
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
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con, con as cur:
            return cur.execute(
                "SELECT * FROM part_info WHERE reference=?", (ref,)
            ).fetchone()

    def delete_part(self, ref):
        """Delete a part from the database by its reference."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con, con as cur:
            cur.execute("DELETE FROM part_info WHERE reference=?", (ref,))
            cur.commit()

    def set_stock(self, ref, stock):
        """Set the stock value for a part in the database."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con, con as cur:
            cur.execute(
                f"UPDATE part_info SET stock = '{int(stock)}' WHERE reference = '{ref}'"
            )
            cur.commit()

    def set_bom(self, ref, state):
        """Change the BOM attribute for a part in the database."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con, con as cur:
            cur.execute(
                f"UPDATE part_info SET exclude_from_bom = '{int(state)}' WHERE reference = '{ref}'"
            )
            cur.commit()

    def set_pos(self, ref, state):
        """Change the BOM attribute for a part in the database."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con, con as cur:
            cur.execute(
                f"UPDATE part_info SET exclude_from_pos = '{int(state)}' WHERE reference = '{ref}'"
            )
            cur.commit()

    def set_lcsc(self, ref, value):
        """Change the BOM attribute for a part in the database."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con, con as cur:
            cur.execute(
                f"UPDATE part_info SET lcsc = '{value}' WHERE reference = '{ref}'"
            )
            cur.commit()

    def update_from_board(self):
        """Read all footprints from the board and insert them into the database if they do not exist."""
        for fp in get_valid_footprints(self.board):
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
                    "Part %s does not exist in the database and will be created from the board.",
                    part[0],
                )
                self.create_part(part)
            elif (
                part[0:3] == list(dbpart[0:3])
                and part[4:] == [bool(x) for x in dbpart[5:]]
            ):  # if the board part matches the dbpart except for the LCSC and the stock value,
                # if part in the database, has no lcsc value the board part has a lcsc value, update including lcsc
                if dbpart and not dbpart[3] and part[3]:
                    self.logger.debug(
                        "Part %s is already in the database but without lcsc value, so the value supplied from the board will be set.",
                        part[0],
                    )
                    self.update_part(part)
                # if part in the database, has a lcsc value
                elif dbpart and dbpart[3] and part[3]:
                    # update lcsc value as well if setting is accordingly
                    if not self.parent.settings.get("general", {}).get(
                        "lcsc_priority", True
                    ):
                        self.logger.debug(
                            "Part %s is already in the database and has a lcsc value, the value supplied from the board will be ignored.",
                            part[0],
                        )
                        part.pop(3)
                    else:
                        self.logger.debug(
                            "Part %s is already in the database and has a lcsc value, the value supplied from the board will overwrite that in the database.",
                            part[0],
                        )
                    self.update_part(part)
            else:
                # If something changed, we overwrite the part and dump the lcsc value or use the one supplied by the board
                self.logger.debug(
                    "Part %s is already in the database but value, footprint, bom or pos values changed in the board file, part will be updated, lcsc overwritten/cleared.",
                    part[0],
                )
                self.update_part(part)
        self.import_legacy_assignments()
        self.clean_database()

    def clean_database(self):
        """Delete all parts from the database that are no longer present on the board."""
        refs = [f"'{fp.GetReference()}'" for fp in get_valid_footprints(self.board)]
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con, con as cur:
            cur.execute(
                f"DELETE FROM part_info WHERE reference NOT IN ({','.join(refs)})"
            )
            cur.commit()

    def import_legacy_assignments(self):
        """Check if assignments of an old version are found and merge them into the database."""
        csv_file = os.path.join(self.project_path, "jlcpcb", "part_assignments.csv")
        if os.path.isfile(csv_file):
            with open(csv_file, encoding="utf-8") as f:
                csvreader = csv.DictReader(
                    f, fieldnames=("reference", "lcsc", "bom", "pos")
                )
                for row in csvreader:
                    self.set_lcsc(row["reference"], row["lcsc"])
                    self.set_bom(row["reference"], row["bom"])
                    self.set_pos(row["reference"], row["pos"])
                    self.logger.debug(
                        "Update %s from legacy 'part_assignments.csv'", row["reference"]
                    )
            os.rename(csv_file, f"{csv_file}.backup")
