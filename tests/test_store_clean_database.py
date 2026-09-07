"""Keep board reference text out of the clean_database DELETE statement."""

import logging

import pytest

from .wx_harness import load, package_stubs, wx_stubs

_PACKAGE = "_store_clean_database_tests"

# get_valid_footprints only requires a reference to start with a word
# character, so quotes reach clean_database untouched.
QUOTED_REFERENCE = "U1'A"

# sqlite3.execute refuses to chain statements, so the reachable damage is a
# reference that smuggles an extra entry into the NOT IN keep-list.
SMUGGLING_REFERENCE = "R1', 'C9"


class _Footprint:
    """Expose the single board accessor clean_database relies on."""

    def __init__(self, reference):
        self.reference = reference

    def GetReference(self):
        """Return the board reference designator."""
        return self.reference


class _Board:
    """Return a fixed set of footprints for reference collection."""

    def __init__(self, references):
        self.footprints = [_Footprint(reference) for reference in references]

    def GetFootprints(self):
        """Return every footprint currently on the board."""
        return self.footprints


@pytest.fixture
def make_store(tmp_path):
    """Build a Store on real SQLite without the constructor's board sync."""
    stubs = {**package_stubs(_PACKAGE), **wx_stubs()}
    store_module = load(_PACKAGE, "store", stubs)

    def _make(board_references, stored_references):
        store = store_module.Store.__new__(store_module.Store)
        store.logger = logging.getLogger(_PACKAGE)
        store.dbfile = str(tmp_path / "project.db")
        store.board = _Board(board_references)
        # Constructor defaults that read_all interpolates into its ORDER BY.
        store.order_by = "reference"
        store.order_dir = "ASC"
        store.create_db()
        for reference in stored_references:
            store.create_part(
                {
                    "reference": reference,
                    "value": "10k",
                    "footprint": "R_0603",
                    "lcsc": "",
                    "exclude_from_bom": 0,
                    "exclude_from_pos": 0,
                }
            )
        return store

    return _make


def _references(store):
    """Return the references still recorded in the project database."""
    return sorted(part["reference"] for part in store.read_all())


def test_quoted_reference_survives_cleanup(make_store):
    """Keep a part whose reference contains a quote and is still on the board."""
    store = make_store(
        ["R1", QUOTED_REFERENCE],
        ["R1", QUOTED_REFERENCE, "C9"],
    )

    store.clean_database()

    assert _references(store) == sorted(["R1", QUOTED_REFERENCE])


def test_quoted_reference_is_removed_once_off_the_board(make_store):
    """Drop a quoted reference that no longer exists on the board."""
    store = make_store(["R1"], ["R1", QUOTED_REFERENCE])

    store.clean_database()

    assert _references(store) == ["R1"]


def test_reference_text_cannot_extend_the_keep_list(make_store):
    """Bind reference text so one designator cannot spare unrelated stale parts."""
    store = make_store([SMUGGLING_REFERENCE], [SMUGGLING_REFERENCE, "C9"])

    store.clean_database()

    assert _references(store) == [SMUGGLING_REFERENCE]


def test_empty_board_clears_every_stored_reference(make_store):
    """Keep the empty keep-list working, which SQLite accepts as `NOT IN ()`."""
    store = make_store([], ["R1", "C9"])

    store.clean_database()

    assert _references(store) == []
