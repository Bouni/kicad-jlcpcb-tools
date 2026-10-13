"""Tests for the nightly parts database update in db_build/jlcparts_db_convert.py."""

import importlib
import sys
from types import ModuleType
from unittest import mock

import pytest

from common.jlcapi import ApiCategory


@pytest.fixture
def convert(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import the update script without keeping its sys.path insert."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    return importlib.import_module("db_build.jlcparts_db_convert")


def test_failed_fetch_stops_the_update_before_stock_cleanup(convert: ModuleType):
    """A category that cannot be fetched in full ends the update with its error.

    cleanup_stock() zeroes the stock of in-stock parts not seen for seven days,
    so a run that lost part of a category must not reach it.
    """
    db = mock.Mock()

    def pages():
        yield [{"componentCode": "C1"}]
        raise KeyError("list")

    fetcher = mock.Mock()
    fetcher.fetchAll.return_value = pages()
    with (
        mock.patch.object(convert, "ComponentsDatabase", return_value=db),
        mock.patch.object(
            convert.JlcApi,
            "fetchCategories",
            return_value=[ApiCategory("Test", "Cat", 2, 1)],
        ),
        mock.patch.object(convert, "CategoryFetch", return_value=fetcher),
        pytest.raises(KeyError),
    ):
        convert.update_components_db_from_api()

    db.update_cache.assert_called_once()
    db.cleanup_stock.assert_not_called()
