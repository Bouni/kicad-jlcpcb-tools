"""Unofficial LCSC API."""

import io
from pathlib import Path
from typing import Union

import requests  # pylint: disable=import-error


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
        """Download and save a datasheet from the API."""
        r = requests.get(url, stream=True, headers=self.headers, timeout=10)
        if r.status_code != requests.codes.ok:  # pylint: disable=no-member
            return {"success": False, "msg": "non-OK HTTP response status"}
        if not r:
            return {"success": False, "msg": "Failed to download datasheet!"}
        with open(path, "wb") as f:
            f.write(r.content)
        return {"success": True, "msg": "Successfully downloaded datasheet!"}
