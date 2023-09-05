#!/bin/env python3

"""Use the amazing work of https://github.com/yaqwsx/jlcparts and convert their database into something we can conveniently use for this plugin.

This replaces the old .csv based database creation that JLCPCB no longer supports.

Before this script can run, the cache.sqlite3 file has to be
present in db_build folder. Download and reassemble it like
jlcparts does it in their build pipeline:
https://github.com/yaqwsx/jlcparts/blob/1a07e1ff42fef2d35419cfb9ba47df090037cc7b/.github/workflows/update_components.yaml#L45-L50

by @markusdd
"""

from datetime import date, datetime
import json
import os
from pathlib import Path
import sqlite3
import zipfile
from zipfile import ZipFile

import humanize


class Generate:
    """Base class for database generation."""

    def __init__(self, output_db: Path, chunk_num: Path ):
        self.output_db = output_db
        self.jlcparts_db_name = "cache.sqlite3"
        self.compressed_output_db = f"{self.output_db}.zip"
        self.chunk_num = chunk_num


    def remove_original(self):
        """Remove the original output database."""
        if self.output_db.exists():
            self.output_db.unlink()

    def connect_sqlite(self):
        """Connect to the sqlite databases."""
        # connection to the jlcparts db
        db_uri = f"file:{self.jlcparts_db_name}?mode=rw"
        self.conn_jp = sqlite3.connect(db_uri, uri=True)

        # connection to the plugin db we want to write
        self.conn = sqlite3.connect(self.output_db)

    def load_tables(self):
        """Load the input data into the output database."""

        # load the tables into memory
        print("Reading manufacturers")
        res = self.conn_jp.execute("SELECT * FROM manufacturers")
        mans = dict(res.fetchall())

        print("Reading categories")
        res = self.conn_jp.execute("SELECT * FROM categories")
        cats = {i: (c, sc) for i, c, sc in res.fetchall()}

        res = self.conn_jp.execute("select count(*) from components")
        results = res.fetchone()
        print(f"{humanize.intcomma(results[0])} parts to import")

        self.part_count = 0
        print("Reading components")
        res = self.conn_jp.execute("SELECT * FROM components")
        while True:
            comps = res.fetchmany(size=100000)

            print(f"Read {humanize.intcomma(len(comps))} parts")

            # if we have no more parts exit out of the loop
            if len(comps) == 0:
                break

            self.part_count += len(comps)

            # now extract the data from the jlcparts db and fill
            # it into the plugin database
            print("Building parts rows to insert")
            rows = []
            for c in comps:
                price = json.loads(c[10])
                price_str = ",".join(
                    [
                        f"{entry.get('qFrom')}-{entry.get('qTo') if entry.get('qTo') is not None else ''}:{entry.get('price')}"
                        for entry in price
                    ]
                )
                row = (
                    f"C{c[0]}",  # LCSC Part
                    cats[c[1]][0],  # First Category
                    cats[c[1]][1],  # Second Category
                    c[2],  # MFR.Part
                    c[3],  # Package
                    int(c[4]),  # Solder Joint
                    mans[c[5]],  # Manufacturer
                    "Basic" if c[6] else "Extended",  # Library Type
                    c[7],  # Description
                    c[8],  # Datasheet
                    price_str,  # Price
                    str(c[9]),  # Stock
                )
                rows.append(row)

            print("Inserting into parts table")
            self.conn.executemany(
                "INSERT INTO parts VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
            )
            self.conn.commit()

        print("Done importing parts")

    def meta_data(self):
        """Populate the metadata table."""
        # metadata
        db_size = os.stat(self.output_db).st_size
        self.conn.execute(
            "INSERT INTO meta VALUES(?, ?, ?, ?, ?)",
            ["cache.sqlite3", db_size, self.part_count, date.today(), datetime.now().isoformat()],
        )
        self.conn.commit()

    def close_sqlite(self):
        """Close sqlite connections."""
        self.conn_jp.close()
        self.conn.close()

    def compress(self):
        """Compress the output database into a new compressed file."""
        print(f"Compressing {self.output_db}")
        with ZipFile(self.compressed_output_db, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(self.output_db)

    def split(self):
        """Split the compressed so we stay below githubs 100M limit."""

        # Set the size of each split file (in bytes)
        split_size = 80000000  # 80 MB

        # Open the zip file for byte-reading
        print(f"Chunking {self.compressed_output_db}")
        with open(self.compressed_output_db, "rb") as z:
            # Read the file data in chunks
            chunk = z.read(split_size)
            chunk_num = 1

            while chunk:
                split_file_name = f"{self.compressed_output_db}.{chunk_num:03}"
                with open(split_file_name, "wb") as split_file:
                    # Write the chunk to the new split file
                    split_file.write(chunk)

                # Read the next chunk of data from the file
                chunk = z.read(split_size)
                chunk_num += 1

            # create a helper file for the downloader which indicates the number of chunk files
            with open(self.chunk_num, "w", encoding="utf-8") as f:
                f.write(str(chunk_num - 1))
    def display_stats(self):
        """Print out some stats."""
        jlcparts_db_size = humanize.naturalsize(os.path.getsize(self.jlcparts_db_name))
        print(f"jlcparts database ({self.jlcparts_db_name}): {jlcparts_db_size}")
        print(f"output db: {humanize.naturalsize(os.path.getsize(self.output_db.name))}")
        print(f"output db (compressed): {humanize.naturalsize(os.path.getsize(self.compressed_output_db))}")

    def cleanup(self):
        """Remove the compressed zip file und output db after splitting."""

        print(f"Deleting {self.compressed_output_db}")
        os.unlink(self.compressed_output_db)

        print(f"Deleting {self.output_db}")
        os.unlink(self.output_db)

class Jlcpcb(Generate):
    """Sqlite parts database generator."""

    def __init__(self, output_db: Path):
        chunk_num = Path("chunk_num.txt")
        super().__init__(output_db, chunk_num)

    def create_tables(self):
        """Create the tables in the output database."""
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS parts (
                'LCSC Part',
                'First Category',
                'Second Category',
                'MFR.Part',
                'Package',
                'Solder Joint',
                'Manufacturer',
                'Library Type',
                'Description',
                'Datasheet',
                'Price',
                'Stock'
            )
            """
        )

        self.conn.execute(
            """
            CREATE UNIQUE INDEX parts_lcsc_part_index
                ON parts ('LCSC Part')
            """
        )

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mapping (
                'footprint',
                'value',
                'LCSC'
            )
            """
        )

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                'filename',
                'size',
                'partcount',
                'date',
                'last_update'
            )
            """
        )

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rotation (
                'regex',
                'correction'
            )
            """
        )

    def build(self):
        """Run all of the steps to generate the database files for upload."""
        self.remove_original()
        self.connect_sqlite()
        self.create_tables()
        self.load_tables()
        self.meta_data()
        self.close_sqlite()
        self.compress()
        self.split()
        self.display_stats()
        self.cleanup()

class JlcpcbFTS5(Generate):
    """FTS5 specific database generation."""

    def __init__(self, output_db: Path):
        chunk_num = Path("chunk_num_fts5.txt")
        super().__init__(output_db, chunk_num)

    def create_tables(self):
        """Create tables."""
        self.conn.execute(
            """
            CREATE virtual TABLE IF NOT EXISTS parts using fts5 (
                'LCSC Part',
                'First Category',
                'Second Category',
                'MFR.Part',
                'Package',
                'Solder Joint' unindexed,
                'Manufacturer',
                'Library Type',
                'Description',
                'Datasheet',
                'Price' unindexed,
                'Stock' unindexed
            , tokenize="trigram")
            """
        )

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mapping (
                'footprint',
                'value',
                'LCSC'
            )
            """
        )

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                'filename',
                'size',
                'partcount',
                'date',
                'last_update'
            )
            """
        )

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rotation (
                'regex',
                'correction'
            )
            """
        )

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS categories (
                'First Category',
                'Second Category'
            )
            """
        )

    def populate_categories(self):
        """Populate the categories table."""
        self.conn.execute(
            'INSERT INTO categories SELECT DISTINCT "First Category", "Second Category" FROM parts ORDER BY UPPER("First Category"), UPPER("Second Category")'
        )

    def optimize(self):
        """FTS5 optimize to minimize query times."""
        print("Optimizing fts5 parts table")
        self.conn.execute("insert into parts(parts) values('optimize')")
        print("Done optimizing fts5 parts table")

    def build(self):
        """Run all of the steps to generate the database files for upload."""
        self.remove_original()
        self.connect_sqlite()
        self.create_tables()
        self.load_tables()
        self.populate_categories()
        self.optimize()
        self.meta_data()
        self.close_sqlite()
        self.compress()
        self.split()
        self.display_stats()
        self.cleanup()


output_directory = "db_build"
os.chdir(output_directory)


# sqlite database
start = datetime.now()
output_name = "parts.db"
partsdb = Path(output_name)

print(f"Generating {output_name} in {output_directory} directory")
generator = Jlcpcb(partsdb)
generator.build()

end = datetime.now()
deltatime = end - start
print(f"Elapsed time: {humanize.precisedelta(deltatime, minimum_unit='seconds')}")



# sqlite fts5 database
start = datetime.now()
output_name = "parts-fts5.db"
partsdb = Path(output_name)

print(f"Generating {output_name} in {output_directory} directory")
generator = JlcpcbFTS5(partsdb)
generator.build()

end = datetime.now()
deltatime = end - start
print(f"Elapsed time: {humanize.precisedelta(deltatime, minimum_unit='seconds')}")
