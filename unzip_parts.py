#!/bin/env python3


def unzip_parts():
    # unzip (needs to go into download function finally)
    # Set the name of the original file
    db_zip_file = Path("parts.db.zip")
    split_dir = Path("db_download")

    # Open the original file for writing
    with open(db_zip_file, "wb") as db:
        # Get a list of the split files in the split directory
        split_files = [
            f for f in os.listdir(split_dir) if f.startswith("parts.db.zip.")
        ]

        # Sort the split files by their index
        split_files.sort(key=lambda f: int(f.split(".")[-1]))

        # Iterate over the split files and append their contents to the original file
        for split_file_name in split_files:
            # Open the split file
            with open(split_dir / split_file_name, "rb") as split_file:
                # Read the file data
                file_data = split_file.read()

                # Append the file data to the original file
                db.write(file_data)

            # Delete the split file
            os.unlink(split_dir / split_file_name)

    with ZipFile("parts.db.zip", "r") as zf:
        zf.extractall()

    os.unlink(db_zip_file)
