#!/bin/env python3

"""
Use the amazing work of https://github.com/yaqwsx/jlcparts and
convert their database into something we can conveniently use for
this plugin.
This replaces the old .csv based database creation that JLCPCB
no longer supports.

Before this script can run, the cache.sqlite3 file has to be
present in db_build folder. Download and reassemble it like
jlcparts does it in their build pipeline:
https://github.com/yaqwsx/jlcparts/blob/1a07e1ff42fef2d35419cfb9ba47df090037cc7b/.github/workflows/update_components.yaml#L45-L50

by @markusdd
"""

import json
import os
import sqlite3
import zipfile
from datetime import date
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile

os.makedirs("db_build", exist_ok=True)
os.chdir("db_build")

partsdb = Path("parts.db")

# we want to rebuild a new parts.db, so remove the old one
if partsdb.exists():
    partsdb.unlink()

# connection to the jlcparts db
conn_jp = sqlite3.connect("cache.sqlite3")

# connection to the plugin db we want to write
conn = sqlite3.connect(partsdb)

# schema creation
conn.execute(
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

conn.execute(
    """
    CREATE TABLE IF NOT EXISTS mapping (
        'footprint',
        'value',
        'LCSC'
    )
    """
)

conn.execute(
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

conn.execute(
    """
    CREATE TABLE IF NOT EXISTS rotation (
        'regex',
        'correction'
    )
    """
)

# load the tables into memory
res = conn_jp.execute("SELECT * FROM manufacturers")
mans = {i: m for i, m in res.fetchall()}

res = conn_jp.execute("SELECT * FROM categories")
cats = {i: (c, sc) for i, c, sc in res.fetchall()}

res = conn_jp.execute("SELECT * FROM components")
comps = res.fetchall()

conn_jp.close()

# now extract the data from the jlcparts db and fill
# it into the plugin database
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

conn.executemany("INSERT INTO parts VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
conn.commit()
db_size = os.stat(partsdb).st_size
conn.execute(
    "INSERT INTO meta VALUES(?, ?, ?, ?, ?)",
    ["cache.sqlite3", db_size, len(comps), date.today(), datetime.now().isoformat()],
)
conn.commit()
conn.close()

# compress the result
with ZipFile("parts.db.zip", "w", zipfile.ZIP_DEFLATED) as zf:
    zf.write(partsdb)

# split the archive on byte level so we stay below githubs 100M limit

# Set the size of each split file (in bytes)
split_size = 80000000  # 80 MB

# Open the zip file for byte-reading
with open("parts.db.zip", "rb") as z:
    # Read the file data in chunks
    chunk = z.read(split_size)
    chunk_num = 1

    while chunk:
        split_file_name = f"parts.db.zip.{chunk_num:03}"
        with open(split_file_name, "wb") as split_file:
            # Write the chunk to the new split file
            split_file.write(chunk)

        # Read the next chunk of data from the file
        chunk = z.read(split_size)
        chunk_num += 1

    # create a helper file for the downloader which indicates the number of chunk files
    with open("chunk_num.txt", "w") as f:
        f.write(str(chunk_num - 1))

# remove the large zip file und uncompressed db after splitting
os.unlink("parts.db.zip")
os.unlink(partsdb)
