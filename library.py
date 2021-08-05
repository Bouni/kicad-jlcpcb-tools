import csv
import locale
import logging
import os
import time
from pathlib import Path

# Hack to avoid wx + pandas error "ValueError: unknown locale: en-GB"
locale.setlocale(locale.LC_ALL, "en")

# import pandas, install it if not installed and import afterwards
try:
    import pandas as pd
except ImportError:
    import subprocess
    import sys

    subprocess.check_call(["python", "-m", "pip", "install", "pandas"])
    import pandas as pd

try:
    import xlrd
except ImportError:
    import subprocess
    import sys

    subprocess.check_call(["python", "-m", "pip", "install", "xlrd"])
    import xlrd

import requests


class JLCPCBLibrary:
    def __init__(self):
        self.create_folders()
        self.xls = os.path.join(self.xlsdir, "jlcpcb_parts.xls")
        self.csv = os.path.join(self.xlsdir, "jlcpcb_parts.csv")
        self.df = None
        self.logger = logging.getLogger(__name__)

    def create_folders(self):
        """Create output folder if not already exist."""
        path, filename = os.path.split(os.path.abspath(__file__))
        self.xlsdir = os.path.join(path, "jlcpcb")
        Path(self.xlsdir).mkdir(parents=True, exist_ok=True)

    def download(self, progress):
        """Download XLS and convert it into CSV"""
        self.logger.info("Start downloading library")
        XLS_URL = "https://jlcpcb.com/componentSearch/uploadComponentInfo"
        with open(self.xls, "wb") as xls:
            r = requests.get(XLS_URL, allow_redirects=True, stream=True)
            total_length = r.headers.get("content-length")
            if total_length is None:  # no content length header
                xls.write(r.content)
            else:
                dl = 0
                total_length = int(total_length)
                progress.SetRange(total_length)
                for data in r.iter_content(chunk_size=4096):
                    dl += len(data)
                    progress.SetValue(dl)
                    xls.write(data)
        progress.SetValue(0)

    def load(self):
        self.logger.info("Start loading library")
        self.df = pd.concat(pd.read_excel(self.xls, sheet_name=None), ignore_index=True)
        self.df.columns = self.df.columns.map(lambda x: x.replace(" ", "_"))
        self.df.columns = self.df.columns.map(lambda x: x.replace(".", "_"))
        self.partcount = len(self.df)
        self.logger.info(f"Loaded Library with {self.partcount} parts")

    def search(
        self,
        lcsc_part="",
        first_category="",
        second_category="",
        mfr_part="",
        package="",
        solder_joint="",
        mfr="",
        library_type="",
        description="",
    ):
        query_parts = []
        if lcsc_part:
            query_parts.append(f"(LCSC_Part.str.match('{lcsc_part}'))")
        if first_category:
            query_parts.append(f"(First_Category.str.match('{first_category}'))")
        if second_category:
            query_parts.append(f"(Second_Category.str.match('{second_category}'))")
        if mfr_part:
            query_parts.append(f"(MFR_Part.str.contains('{mfr_part}'))")
        if package:
            query_parts.append(f"(Package.str.contains('{package}'))")
        if solder_joint:
            query_parts.append(f"(Solder_Joint.str.match('{solder_joint}'))")
        if mfr:
            query_parts.append(f"(Manufacturer.str.contains('{mfr}'))")
        if library_type:
            query_parts.append(f"(Library_Type.str.match('{library_type}'))")
        if description:
            query_parts.append(f"(Description.str.contains('{description}'))")
        query = " & ".join(query_parts)
        print(query)
        print(self.df[self.df.eval(query)])
