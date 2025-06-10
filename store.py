"""Contains the data storge for a project."""

import contextlib
import csv
import logging
import os
from pathlib import Path
import sqlite3
from typing import Union

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

    def set_order_by(self, n: int):
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

    def read_all(self) -> dict:
        """Read all parts from the database."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con, con as cur:
            con.create_collation("naturalsort", natural_sort_collation)
            con.row_factory = dict_factory
            return cur.execute(
                f"SELECT * FROM part_info ORDER BY {self.order_by} COLLATE naturalsort {self.order_dir}"
            ).fetchall()

    def read_bom_parts(self) -> dict:
        """Read all parts that should be included in the BOM."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con, con as cur:
            con.row_factory = dict_factory
            # Query all parts that are supposed to be in the BOM an have an lcsc number, group the references together
            subquery = "SELECT value, reference, footprint, lcsc FROM part_info WHERE exclude_from_bom = '0' AND lcsc != '' ORDER BY lcsc, reference"
            query = f"SELECT value, GROUP_CONCAT(reference) AS refs, footprint, lcsc  FROM ({subquery}) GROUP BY value, lcsc"
            a = cur.execute(query).fetchall()
            # Query all parts that are supposed to be in the BOM but have no lcsc number
            query = "SELECT value, reference AS refs, footprint, lcsc FROM part_info WHERE exclude_from_bom = '0' AND lcsc = ''"
            b = cur.execute(query).fetchall()
            return a + b

    def create_part(self, part: dict):
        """Create a part in the database."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con, con as cur:
            cur.execute(
                "INSERT INTO part_info VALUES (:reference, :value, :footprint, :lcsc, '', :exclude_from_bom, :exclude_from_pos)",
                part,
            )
            cur.commit()

    def update_part(self, part: dict):
        """Update a part in the database, overwrite lcsc if supplied."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con, con as cur:
            cur.execute(
                "UPDATE part_info set value = :value, footprint = :footprint, lcsc = :lcsc, exclude_from_bom = :exclude_from_bom, exclude_from_pos = :exclude_from_pos WHERE reference = :reference",
                part,
            )
            cur.commit()

    def get_part(self, ref: str) -> dict:
        """Get a part from the database by its reference."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con, con as cur:
            con.row_factory = dict_factory
            return cur.execute(
                "SELECT * FROM part_info WHERE reference = :reference",
                {"reference": ref},
            ).fetchone()

    def set_stock(self, ref: str, stock: Union[int, None]):
        """Set the stock value for a part in the database."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con, con as cur:
            cur.execute(
                "UPDATE part_info SET stock = :stock WHERE reference = :reference",
                {"reference": ref, "stock": stock},
            )
            cur.commit()

    def set_bom(self, ref: str, state: int):
        """Change the BOM attribute for a part in the database."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con, con as cur:
            cur.execute(
                "UPDATE part_info SET exclude_from_bom = :state WHERE reference = :reference",
                {"reference": ref, "state": state},
            )
            cur.commit()

    def set_pos(self, ref: str, state: int):
        """Change the POS attribute for a part in the database."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con, con as cur:
            cur.execute(
                "UPDATE part_info SET exclude_from_pos = :state WHERE reference = :reference",
                {"reference": ref, "state": state},
            )
            cur.commit()

    def set_lcsc(self, ref: str, lcsc: str):
        """Change the LCSC attribute for a part in the database."""
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con, con as cur:
            cur.execute(
                "UPDATE part_info SET lcsc = :lcsc WHERE reference = :reference",
                {"reference": ref, "lcsc": lcsc},
            )
            cur.commit()

    def update_from_board(self):
        """Read all footprints from the board and insert them into the database if they do not exist."""
        for fp in get_valid_footprints(self.board):
            board_part = {
                "reference": fp.GetReference(),
                "value": fp.GetValue(),
                "footprint": str(fp.GetFPID().GetLibItemName()),
                "lcsc": get_lcsc_value(fp),
                "exclude_from_bom": get_exclude_from_bom(fp),
                "exclude_from_pos": get_exclude_from_pos(fp),
            }
            db_part = self.get_part(board_part["reference"])
            # if part is not in the database yet, create it
            if not db_part:
                self.logger.debug(
                    "Part %s does not exist in the database and will be created from the board.",
                    board_part["reference"],
                )
                self.create_part(board_part)
            # if the board part matches the db_part except for the LCSC and the stock value
            elif [
                board_part["reference"],
                board_part["value"],
                board_part["footprint"],
                board_part["exclude_from_bom"],
                board_part["exclude_from_pos"],
            ] == [
                db_part["reference"],
                db_part["value"],
                db_part["footprint"],
                bool(db_part["exclude_from_bom"]),
                bool(db_part["exclude_from_pos"]),
            ]:
                # if part in the database, has no lcsc value the board part has a lcsc value, update including lcsc
                if db_part and not db_part["lcsc"] and board_part["lcsc"]:
                    self.logger.debug(
                        "Part %s is already in the database but without lcsc value, so the value supplied from the board will be set.",
                        board_part["reference"],
                    )
                    self.update_part(board_part)
                # if part in the database, has a lcsc value
                elif db_part and db_part["lcsc"] and board_part["lcsc"]:
                    # update lcsc value as well if setting is accordingly
                    if not self.parent.settings.get("general", {}).get(
                        "lcsc_priority", True
                    ):
                        self.logger.debug(
                            "Part %s is already in the database and has a lcsc value, the value supplied from the board will be ignored.",
                            board_part["reference"],
                        )
                        board_part["lcsc"] = db_part["lcsc"]
                    else:
                        self.logger.debug(
                            "Part %s is already in the database and has a lcsc value, the value supplied from the board will overwrite that in the database.",
                            board_part["reference"],
                        )
                    self.update_part(board_part)
            else:
                # If something changed, we overwrite the part and dump the lcsc value or use the one supplied by the board
                self.logger.debug(
                    "Part %s is already in the database but value, footprint, bom or pos values changed in the board file, part will be updated, lcsc overwritten/cleared.",
                    board_part["reference"],
                )
                self.update_part(board_part)
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
                    self.set_bom(row["reference"], int(row["bom"]))
                    self.set_pos(row["reference"], int(row["pos"]))
                    self.logger.debug(
                        "Update %s from legacy 'part_assignments.csv'", row["reference"]
                    )
            os.rename(csv_file, f"{csv_file}.backup")
