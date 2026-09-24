"""Outages preserve persisted catalogs from both supported payload versions."""

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from .test_impedance_catalog_controller import api  # noqa: F401 -- shared fixture


@pytest.mark.parametrize(
    ("version", "timestamp"),
    [
        (1, None),
        (2, None),
        (2, "invalid timestamp"),
        (2, "2001-01-01T00:00:00.000000Z"),
        (2, "2100-01-01T00:00:00.000000Z"),
    ],
)
def test_failed_refresh_preserves_old_payload_then_persists_complete_recovery(
    api: SimpleNamespace,  # noqa: F811
    tmp_path: Path,
    version: int,
    timestamp: Optional[str],
) -> None:
    """Real SQL storage remains byte-identical on failure, even for old caches."""
    package = api.controller.__package__
    database_module = importlib.import_module(f"{package}.database")
    repository_module = importlib.import_module(f"{package}.repository")
    database = database_module.ImpedanceDatabase(tmp_path / "project.db")
    saved = api.model.Stackup("saved", "Saved construction", 4, "1.6", "1", "0.5")
    payload = {
        "schema_version": version,
        "stackups": [api.model.stackup_to_dict(saved)],
    }
    if timestamp is not None:
        payload["checked_at_utc"] = timestamp
    database.save_stackup_catalog(4, payload)
    read = repository_module.ImpedanceRepository.read_stackup_catalog
    old_cache = read(database, 4)
    before = database.path.read_bytes()
    outcomes: list[object] = [
        api.transport.CatalogUnavailable("Temporary connection interruption"),
        (saved,),
    ]

    def fetch(*_args: Any, **_kwargs: Any) -> tuple[Any, ...]:
        """Return a transient outage, then a complete immutable provider response."""
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, tuple)
        return outcome

    def save(layer_count: int, cache: Any) -> None:
        """Use the real repository serializer without adding board intent."""
        repository = repository_module.ImpedanceRepository(
            database, "unregistered-board"
        )
        repository.save_stackup_catalog(layer_count, cache)

    controller = api.controller.CatalogController(
        4,
        load_cache=lambda count: read(database, count),
        save_cache=save,
        fetch_catalog=fetch,
        refresh_gate=api.gate,
    )
    try:
        controller.ensure_started()
        api.workers[0].run()
        assert controller.state.cache == old_cache
        assert not controller.state.error
        assert read(database, 4) == old_cache
        assert database.load_stackup_catalog(4) == payload
        assert database.path.read_bytes() == before
        api.gui.advance(300)
        api.workers[1].run()
        recovered = read(database, 4)
        assert recovered.stackups == (saved,)
        assert recovered.checked_at_utc != old_cache.checked_at_utc
        assert database.load_stackup_catalog(4)["schema_version"] == 2
        assert database.load_config("unregistered-board") is None
        assert not controller.state.error
    finally:
        controller.close()
