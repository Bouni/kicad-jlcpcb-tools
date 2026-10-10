"""Supplier assembly facts saved per LCSC code in the shared global database."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
import logging
import os
from pathlib import Path
import sqlite3
import time
from typing import Any

import pytest

from .wx_harness import load_siblings

DAY_S = 24 * 3600


@pytest.fixture
def cache_module() -> Iterator[Any]:
    """Import the real cache module without starting the plugin package."""
    with load_siblings("_assembly_metadata_cache", ["enrichment.cache"], {}) as loaded:
        yield loaded["enrichment.cache"]


def _rows(path: Path) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(path)) as con:
        return con.execute(
            "SELECT lcsc, assembly_process, component_product_type "
            "FROM lcsc_assembly_metadata ORDER BY lcsc"
        ).fetchall()


def test_table_is_added_beside_existing_global_data(
    cache_module: Any, tmp_path: Path
) -> None:
    """Opening the cache never disturbs corrections or part preferences."""
    path = tmp_path / "global.db"
    with closing(sqlite3.connect(path)) as con, con:
        con.execute("CREATE TABLE mapping ('footprint', 'value', 'LCSC')")
        con.execute("INSERT INTO mapping VALUES ('R_0603', '10k', 'C25804')")
    cache_module.AssemblyMetadataCache(str(path))
    with closing(sqlite3.connect(path)) as con:
        assert con.execute("SELECT * FROM mapping").fetchall() == [
            ("R_0603", "10k", "C25804")
        ]
    assert _rows(path) == []


def test_facts_survive_a_new_instance(cache_module: Any, tmp_path: Path) -> None:
    """A board opened again reads its facts without another lookup."""
    path = str(tmp_path / "global.db")
    first = cache_module.AssemblyMetadataCache(path)
    first.merge(" c100 ", "SMT", 0)
    assert first.get("C100") == ("SMT", 0)
    reopened = cache_module.AssemblyMetadataCache(path)
    assert reopened.get("c100") == ("SMT", 0)
    assert not reopened.needs_fetch("C100")


def test_partial_result_keeps_the_saved_fact(cache_module: Any, tmp_path: Path) -> None:
    """A result lacking one fact replaces only the fact it carries."""
    path = tmp_path / "global.db"
    cache = cache_module.AssemblyMetadataCache(str(path))
    cache.merge("C1", "SMT", 2)
    cache.merge("C1", "", 0)
    assert cache.get("C1") == ("SMT", 0)
    cache.merge("C1", "THT", None)
    assert cache.get("C1") == ("THT", 0)
    assert _rows(path) == [("C1", "THT", 0)]


@pytest.mark.parametrize(
    "process,product_type", [("", None), ("  ", None), (None, 7), ("", "x")]
)
def test_empty_or_unrecognised_results_are_never_saved(
    cache_module: Any, tmp_path: Path, process: Any, product_type: Any
) -> None:
    """A failed lookup looks like an unclassified part, so neither is written."""
    path = tmp_path / "global.db"
    cache = cache_module.AssemblyMetadataCache(str(path))
    cache.merge("C1", process, product_type)
    assert cache.get("C1") == ("", None)
    assert cache.needs_fetch("C1")
    assert _rows(path) == []


def test_an_empty_result_keeps_saved_facts_and_their_age(
    cache_module: Any, tmp_path: Path
) -> None:
    """A failed refresh leaves aged facts aged, so the next session retries."""
    path = tmp_path / "global.db"
    cache = cache_module.AssemblyMetadataCache(str(path))
    cache.merge("C1", "SMT", 0)
    with closing(sqlite3.connect(path)) as con, con:
        con.execute("UPDATE lcsc_assembly_metadata SET fetched_at = 0")
    aged = cache_module.AssemblyMetadataCache(str(path))
    aged.merge("C1", "", None)
    assert aged.get("C1") == ("SMT", 0)
    assert aged.needs_fetch("C1")
    assert cache_module.AssemblyMetadataCache(str(path)).needs_fetch("C1")


def test_needs_fetch_follows_missing_facts_and_age(cache_module: Any) -> None:
    """Unknown, incomplete and month-old codes are fetched; fresh ones are not."""
    cache = cache_module.AssemblyMetadataCache(None)
    cache.merge("C1", "SMT", 0)
    cache.merge("C2", "SMT", None)
    cache.merge("C3", "", 2)
    fetched = cache._facts["C1"][2]
    assert cache.needs_fetch("C9")
    assert cache.needs_fetch("C2")
    assert cache.needs_fetch("C3")
    assert not cache.needs_fetch("C1", now=fetched + 30 * DAY_S - 1)
    assert cache.needs_fetch("C1", now=fetched + 30 * DAY_S)
    assert cache.needs_fetch("C1", now=fetched - 1)
    assert cache.get("C1") == ("SMT", 0)


def test_two_windows_cannot_erase_each_others_fact(
    cache_module: Any, tmp_path: Path
) -> None:
    """Instances that loaded before either write still merge on disk."""
    path = str(tmp_path / "global.db")
    first = cache_module.AssemblyMetadataCache(path)
    second = cache_module.AssemblyMetadataCache(path)
    first.merge("C1", "SMT", None)
    second.merge("C1", "", 2)
    assert cache_module.AssemblyMetadataCache(path).get("C1") == ("SMT", 2)


def test_rows_that_cannot_be_read_are_skipped(
    cache_module: Any, tmp_path: Path
) -> None:
    """A malformed row is fetched again instead of stopping the load."""
    path = tmp_path / "global.db"
    cache_module.AssemblyMetadataCache(str(path)).merge("C1", "SMT", 0)
    with closing(sqlite3.connect(path)) as con, con:
        con.execute(
            "INSERT INTO lcsc_assembly_metadata VALUES ('C2', 'SMT', 0, 'later')"
        )
        con.execute("INSERT INTO lcsc_assembly_metadata VALUES ('', 'SMT', 0, 1.0)")
        con.execute("INSERT INTO lcsc_assembly_metadata VALUES ('C3', 'SMT', 9, 1.0)")
    reopened = cache_module.AssemblyMetadataCache(str(path))
    assert reopened.get("C1") == ("SMT", 0)
    assert reopened.get("C2") == ("", None)
    assert reopened.get("C3") == ("SMT", None)
    assert reopened.needs_fetch("C2") and reopened.needs_fetch("C3")


def test_without_a_path_nothing_is_written(cache_module: Any, tmp_path: Path) -> None:
    """The default in-memory cache behaves like the old per-window dictionary."""
    cache = cache_module.AssemblyMetadataCache(None)
    cache.merge("C1", "SMT", 0)
    assert cache.get("C1") == ("SMT", 0)
    assert list(tmp_path.iterdir()) == []


def _unopenable(tmp_path: Path, kind: str) -> Path:
    if kind == "directory":
        path = tmp_path / "global.db"
        path.mkdir()
        return path
    if kind == "missing folder":
        return tmp_path / "absent" / "global.db"
    path = tmp_path / "global.db"
    path.write_bytes(b"this is not a database" * 64)
    return path


@pytest.mark.parametrize("kind", ["directory", "missing folder", "corrupt"])
def test_database_failures_fall_back_to_memory_with_one_warning(
    cache_module: Any,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    kind: str,
) -> None:
    """A broken global.db costs a refetch next time, never an error."""
    path = _unopenable(tmp_path, kind)
    with caplog.at_level(logging.WARNING):
        cache = cache_module.AssemblyMetadataCache(str(path))
        cache.merge("C1", "SMT", 0)
        cache.merge("C2", "THT", 2)
    assert cache.get("C1") == ("SMT", 0)
    assert cache.get("C2") == ("THT", 2)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert str(path) in warnings[0].getMessage()


def test_a_locked_database_skips_the_write_and_later_writes_still_land(
    cache_module: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Another window holding global.db delays nothing beyond the busy timeout."""
    monkeypatch.setattr(cache_module, "BUSY_TIMEOUT_S", 0.01)
    path = tmp_path / "global.db"
    cache = cache_module.AssemblyMetadataCache(str(path))
    with closing(sqlite3.connect(path, isolation_level=None)) as other:
        other.execute("BEGIN EXCLUSIVE")
        with caplog.at_level(logging.WARNING):
            cache.merge("C1", "SMT", 0)
        other.execute("ROLLBACK")
    assert cache.get("C1") == ("SMT", 0)
    assert len(caplog.records) == 1
    cache.merge("C2", "THT", 2)
    assert _rows(path) == [("C2", "THT", 2)]


def test_a_database_locked_at_open_still_saves_later_facts(
    cache_module: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Another window holding a new global.db at open costs only this load."""
    monkeypatch.setattr(cache_module, "BUSY_TIMEOUT_S", 0.01)
    path = tmp_path / "global.db"
    with closing(sqlite3.connect(path, isolation_level=None)) as other:
        other.execute("BEGIN EXCLUSIVE")
        with caplog.at_level(logging.WARNING):
            cache = cache_module.AssemblyMetadataCache(str(path))
        other.execute("ROLLBACK")
    assert len(caplog.records) == 1
    cache.merge("C1", "SMT", 0)
    assert _rows(path) == [("C1", "SMT", 0)]


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="file permissions do not stop root or apply on Windows",
)
def test_a_read_only_database_still_serves_saved_facts(
    cache_module: Any, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A synced or shared global.db that cannot be written still reads."""
    path = tmp_path / "global.db"
    cache_module.AssemblyMetadataCache(str(path)).merge("C1", "SMT", 0)
    path.chmod(0o444)
    try:
        with caplog.at_level(logging.WARNING):
            cache = cache_module.AssemblyMetadataCache(str(path))
            assert cache.get("C1") == ("SMT", 0)
            cache.merge("C2", "THT", 2)
            cache.merge("C3", "THT", 2)
    finally:
        path.chmod(0o644)
    assert cache.get("C2") == ("THT", 2)
    assert len(caplog.records) == 1
    assert _rows(path) == [("C1", "SMT", 0)]


def test_a_partial_refresh_leaves_the_missing_fact_due(
    cache_module: Any, tmp_path: Path
) -> None:
    """A refresh lacking one fact keeps the saved one without vouching for it."""
    path = tmp_path / "global.db"
    cache_module.AssemblyMetadataCache(str(path)).merge("C1", "SMT", 0)
    with closing(sqlite3.connect(path)) as con, con:
        con.execute("UPDATE lcsc_assembly_metadata SET fetched_at = 0")
    aged = cache_module.AssemblyMetadataCache(str(path))
    aged.merge("C1", "SMT", None)
    assert aged.get("C1") == ("SMT", 0)
    assert aged.needs_fetch("C1")
    assert cache_module.AssemblyMetadataCache(str(path)).needs_fetch("C1")
    aged.merge("C1", "THT", 2)
    assert not aged.needs_fetch("C1")
    assert not cache_module.AssemblyMetadataCache(str(path)).needs_fetch("C1")


def test_a_held_lock_delays_a_write_by_well_under_a_second(
    cache_module: Any, tmp_path: Path
) -> None:
    """Writes run on the UI thread and only save a refetch, so a lock costs little."""
    path = tmp_path / "global.db"
    cache = cache_module.AssemblyMetadataCache(str(path))
    with closing(sqlite3.connect(path, isolation_level=None)) as other:
        other.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        cache.merge("C1", "SMT", 0)
        elapsed = time.monotonic() - started
        other.execute("ROLLBACK")
    assert cache.get("C1") == ("SMT", 0)
    assert elapsed < 1.0
