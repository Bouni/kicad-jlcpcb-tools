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

# import xlrd, install it if not installed
try:
    import xlrd
except ImportError:
    import subprocess
    import sys

    subprocess.check_call(["python", "-m", "pip", "install", "xlrd"])

import requests


class JLCPCBLibrary:
    def __init__(self):
        self.create_folders()
        self.xls = os.path.join(self.xlsdir, "jlcpcb_parts.xls")
        self.csv = os.path.join(self.xlsdir, "jlcpcb_parts.csv")
        self.df = None
        self.loaded = False
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
        self.df = pd.concat(pd.read_excel(self.xls, sheet_name=None), ignore_index=True)
        self.df.columns = self.df.columns.map(lambda x: x.replace(" ", "_"))
        self.df.columns = self.df.columns.map(lambda x: x.replace(".", "_"))
        self.partcount = len(self.df)
        self.logger.info(f"Loaded Library with {self.partcount} parts")
        self.loaded = True

    def search(self, keyword="", basic=True, extended=False):
        if len(keyword) < 3:
            return []
        query = [
            f"(LCSC_Part.str.contains('{keyword}'))",
            f"(First_Category.str.contains('{keyword}'))",
            f"(Second_Category.str.contains('{keyword}'))",
            f"(MFR_Part.str.contains('{keyword}'))",
            f"(Package.str.contains('{keyword}'))",
            f"(Manufacturer.str.contains('{keyword}'))",
            f"(Description.str.contains('{keyword}'))",
        ]
        df = self.df
        types = []
        if basic:
            types.append("Basic")
        if extended:
            types.append("Extended")
        df = df[df.Library_Type.isin(types)]
        query = " | ".join(query)
        result = df[df.eval(query)]
        return result
