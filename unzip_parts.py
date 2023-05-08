#!/bin/env python3

import os
from zipfile import ZipFile


def unzip_parts(path):
    # unzip (needs to go into download function finally)
    # Set the name of the original file
    db_zip_file = os.path.join(path, "parts.db.zip")

    # Open the original file for writing
    with open(db_zip_file, "wb") as db:
        # Get a list of the split files in the split directory
        split_files = [f for f in os.listdir(path) if f.startswith("parts.db.zip.")]

        # Sort the split files by their index
        split_files.sort(key=lambda f: int(f.split(".")[-1]))

        # Iterate over the split files and append their contents to the original file
        for split_file_name in split_files:
            split_path = os.path.join(path, split_file_name)
            # Open the split file
            with open(split_path, "rb") as split_file:
                # Read the file data
                file_data = split_file.read()

                # Append the file data to the original file
                db.write(file_data)

            # Delete the split file
            os.unlink(split_path)

    with ZipFile(db_zip_file, "r") as zf:
        zf.extractall(path)

    os.unlink(db_zip_file)
