#!/bin/env python3
"""Module for unziping and merging split db zip file."""

import logging
import os
from zipfile import ZipFile

import wx  # pylint: disable=import-error

from .events import (
    UnzipCombiningProgressEvent,
    UnzipCombiningStartedEvent,
    UnzipExtractingCompletedEvent,
    UnzipExtractingProgressEvent,
    UnzipExtractingStartedEvent,
)


def unzip_parts(parent, path):
    """Unzip and merge split zip file."""
    logger = logging.getLogger(__name__)
    logger.debug("Combine zip chunks")
    wx.PostEvent(parent, UnzipCombiningStartedEvent())
    # unzip (needs to go into download function finally)
    # Set the name of the original file
    db_zip_file = os.path.join(path, "parts-fts5.db.zip")

    # Open the original file for writing
    with open(db_zip_file, "wb") as db:
        # Get a list of the split files in the split directory
        split_files = [
            f for f in os.listdir(path) if f.startswith("parts-fts5.db.zip.")
        ]

        # Sort the split files by their index
        split_files.sort(key=lambda f: int(f.split(".")[-1]))

        # Iterate over the split files and append their contents to the original file
        for i, split_file_name in enumerate(split_files, 1):
            split_path = os.path.join(path, split_file_name)
            # Open the split file
            with open(split_path, "rb") as split_file:
                # Read the file data
                while file_data := split_file.read(1024 * 1024):
                    # Append the file data to the original file
                    db.write(file_data)

            # Delete the split file
            os.unlink(split_path)
            progress = 100 / len(split_files) * i
            wx.PostEvent(parent, UnzipCombiningProgressEvent(value=progress))

    with ZipFile(db_zip_file, "r") as zf:
        logger.debug("Extract zip file")
        wx.PostEvent(parent, UnzipExtractingStartedEvent())
        file_info = zf.infolist()[0]
        file_size = file_info.file_size
        with (
            zf.open(file_info) as source,
            open(os.path.join(path, file_info.filename), "wb") as target,
        ):
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                target.write(chunk)
                progress = target.tell() / file_size * 100
                wx.PostEvent(parent, UnzipExtractingProgressEvent(value=progress))

    os.unlink(db_zip_file)
    wx.PostEvent(parent, UnzipExtractingCompletedEvent())
