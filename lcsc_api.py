"""Unofficial LCSC API."""

import io
from pathlib import Path
from typing import Union

import requests  # pylint: disable=import-error

# JLCPCB serves the files a part record names by their access ids: the part
# pictures, and in dataManualFileAccessId the datasheet PDF itself.
JLC_FILE_URL = "https://jlcpcb.com/api/file/downloadByFileSystemAccessId/{}"


class LCSC_API:
    """Unofficial LCSC API."""

    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/102.0.0.0 Safari/537.36"
        }  # pretend we are browser, otherwise their cloud service blocks the request

    def get_part_data(self, lcsc_number: str) -> dict:
        """Get data for a given LCSC number from the API."""
        r = requests.get(
            f"https://cart.jlcpcb.com/shoppingCart/smtGood/getComponentDetail?componentCode={lcsc_number}",
            headers=self.headers,
            timeout=10,
        )
        if r.status_code != requests.codes.ok:  # pylint: disable=no-member
            return {"success": False, "msg": "non-OK HTTP response status"}
        data = r.json()
        if not data.get("data"):
            return {
                "success": False,
                "msg": "returned JSON data does not have expected 'data' attribute",
            }
        return {"success": True, "data": data}

    def download_bitmap(self, url: str) -> Union[io.BytesIO, None]:
        """Download a picture of the part from the API."""
        content = requests.get(url, headers=self.headers, timeout=10).content
        return io.BytesIO(content)

    def download_datasheet(self, url: str, path: Path):
        """Download a datasheet and save it only if it is a PDF.

        LCSC's datasheet links answer with a web page that displays the PDF,
        with a successful status, so the status alone does not show that the
        file is a datasheet.
        """
        try:
            r = requests.get(url, headers=self.headers, timeout=10)
        except requests.RequestException as error:
            return {"success": False, "msg": f"Failed to download datasheet: {error}"}
        if r.status_code != requests.codes.ok:  # pylint: disable=no-member
            return {"success": False, "msg": "non-OK HTTP response status"}
        if not r.content.startswith(b"%PDF-"):
            return {"success": False, "msg": "The datasheet link did not return a PDF."}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "wb") as f:
                f.write(r.content)
        except OSError as error:
            return {
                "success": False,
                "msg": f"Could not save the datasheet to {path}: {error}",
            }
        return {"success": True, "msg": f"Saved the datasheet to {path}"}
