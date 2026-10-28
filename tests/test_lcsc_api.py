"""Datasheet downloads save a PDF and refuse anything else."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from .wx_harness import load

lcsc_api = load("lcsc_api_tests", "lcsc_api", {})

PDF = b"%PDF-1.5\n%binary datasheet"
VIEWER_PAGE = b'<!DOCTYPE html><html lang="en"><head><title>Datasheet</title>'


def _get(status: int, content: bytes) -> Any:
    """Return a requests.get double answering every URL with one response."""
    calls: list[str] = []

    def get(url: str, **_kwargs: Any) -> SimpleNamespace:
        calls.append(url)
        return SimpleNamespace(status_code=status, content=content)

    get.calls = calls
    return get


def test_download_datasheet_saves_a_pdf(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A PDF response is written to the path given."""
    get = _get(200, PDF)
    monkeypatch.setattr(lcsc_api.requests, "get", get)
    path = tmp_path / "C25804.pdf"

    result = lcsc_api.LCSC_API().download_datasheet("https://example.com/ds", path)

    assert result == {"success": True, "msg": f"Saved the datasheet to {path}"}
    assert path.read_bytes() == PDF
    assert get.calls == ["https://example.com/ds"]


@pytest.mark.parametrize(
    ("status", "content", "message"),
    [
        (200, VIEWER_PAGE, "The datasheet link did not return a PDF."),
        (404, PDF, "non-OK HTTP response status"),
    ],
    ids=["viewer-page", "not-found"],
)
def test_download_datasheet_refuses_anything_but_a_pdf(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: int,
    content: bytes,
    message: str,
) -> None:
    """LCSC's viewer page, served with a 200, is not saved as a .pdf."""
    monkeypatch.setattr(lcsc_api.requests, "get", _get(status, content))
    path = tmp_path / "C25804.pdf"

    result = lcsc_api.LCSC_API().download_datasheet("https://example.com/ds", path)

    assert result == {"success": False, "msg": message}
    assert not path.exists()


def test_download_datasheet_reports_a_network_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A timeout or refused connection comes back as a failure, not an exception."""

    def get(_url: str, **_kwargs: Any) -> None:
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(lcsc_api.requests, "get", get)
    path = tmp_path / "C25804.pdf"

    result = lcsc_api.LCSC_API().download_datasheet("https://example.com/ds", path)

    assert result == {
        "success": False,
        "msg": "Failed to download datasheet: connection refused",
    }
    assert not path.exists()


def test_download_datasheet_reports_a_file_it_cannot_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A path that can't be written comes back as a failure, not an exception."""
    monkeypatch.setattr(lcsc_api.requests, "get", _get(200, PDF))

    result = lcsc_api.LCSC_API().download_datasheet("https://example.com/ds", tmp_path)

    assert result["success"] is False
    assert result["msg"].startswith(f"Could not save the datasheet to {tmp_path}: ")


def test_download_datasheet_reports_a_folder_it_cannot_create(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A datasheets folder that can't be made is a failure, and only a PDF makes one."""
    monkeypatch.setattr(lcsc_api.requests, "get", _get(200, PDF))
    (tmp_path / "datasheets").write_text("not a folder")
    path = tmp_path / "datasheets" / "C25804.pdf"

    result = lcsc_api.LCSC_API().download_datasheet("https://example.com/ds", path)

    assert result["success"] is False
    assert result["msg"].startswith(f"Could not save the datasheet to {path}: ")


def test_download_datasheet_makes_the_folder_for_a_pdf_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The datasheets folder appears with the first PDF, not on a refused page."""
    path = tmp_path / "datasheets" / "C25804.pdf"
    monkeypatch.setattr(lcsc_api.requests, "get", _get(200, VIEWER_PAGE))

    lcsc_api.LCSC_API().download_datasheet("https://example.com/ds", path)

    assert not path.parent.exists()
    monkeypatch.setattr(lcsc_api.requests, "get", _get(200, PDF))

    lcsc_api.LCSC_API().download_datasheet("https://example.com/ds", path)

    assert path.read_bytes() == PDF
