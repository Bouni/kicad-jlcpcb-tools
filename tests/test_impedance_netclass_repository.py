"""Keep schema upgrades read-only and preserve board-scoped database revisions."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

import pytest

from impedance.database import ConfigConflictError, ImpedanceDatabase
from impedance.model import Config, ValidationError
from impedance.repository import ImpedanceRepository


def _netclass_payload(version: int = 5) -> dict[str, Any]:
    """Describe class intent without relying on the current serializer."""
    result = {
        "schema_version": version,
        "enabled": False,
        "specifications": [
            {
                "spec_id": "usb",
                "label": "USB Ω",
                "target_ohms": "90",
                "kind": "differential",
                "net_class": "USB",
                "layer_settings": [
                    {
                        "layer": "F.Cu",
                        "reference_layers": ["In1.Cu"],
                        "spacing_nm": 203200,
                        "ground_gap_nm": None,
                    },
                    {
                        "layer": "In2.Cu",
                        "reference_layers": ["In1.Cu", "In3.Cu"],
                        "spacing_nm": 170000,
                        "ground_gap_nm": None,
                    },
                ],
                "excluded_layers": ["B.Cu"],
            }
        ],
        "reviewed_digest": "",
        "included_section_ids": [],
        "review_tracking": {"images": [], "layers": []},
        "stackup": None,
        "width_results": [],
    }
    if version == 4:
        result.update(
            reviewed_digest="previous-approval",
            included_section_ids=["old-row"],
            section_groups=[["old-row", "old-row-2"]],
            legacy_specifications=[],
        )
    return result


class MemoryDatabase:
    """Expose the stable storage envelope and detect accidental load-time writes."""

    def __init__(self, payload: dict[str, Any], version: Any = 1) -> None:
        self.record = {
            "version": version,
            "revision": 4,
            "enabled": payload["enabled"],
            "payload": deepcopy(payload),
        }
        self.saves: list[tuple[str, dict[str, Any], bool, int]] = []

    def load_config(self, board_id: str) -> Optional[dict[str, Any]]:
        """Return a detached record just as the real database does."""
        return deepcopy(self.record)

    def save_config(
        self,
        board_id: str,
        payload: dict[str, Any],
        enabled: bool,
        expected_revision: int,
    ) -> int:
        """Retain optimistic revision semantics while recording each write."""
        if expected_revision != self.record["revision"]:
            raise ValueError("Configuration revision changed.")
        self.saves.append((board_id, deepcopy(payload), enabled, expected_revision))
        self.record = {
            "version": 1,
            "revision": expected_revision + 1,
            "enabled": enabled,
            "payload": deepcopy(payload),
        }
        return expected_revision + 1


@pytest.mark.parametrize("version", [1, 2, 3])
def test_retired_schema_requires_explicit_reset_without_database_write(
    version: int,
) -> None:
    """Unsupported historical filters stay intact until the user requests a reset."""
    payload = {
        "schema_version": version,
        "enabled": False,
        "specifications": [{"selectors": [{"layer": "F.Cu", "width_nm": 180000}]}],
    }
    database = MemoryDatabase(payload)
    before = deepcopy(database.record)
    with pytest.raises(ValidationError, match="Reset settings"):
        ImpedanceRepository(database, "board-a").load()
    assert database.record == before
    assert database.saves == []


def test_loading_schema_four_upgrades_only_in_memory_and_revokes_approval() -> None:
    """Opening retains net-class intent without carrying old report authorization."""
    database = MemoryDatabase(_netclass_payload(4))
    before = deepcopy(database.record)
    config, revision = ImpedanceRepository(database, "board-a").load()
    assert revision == 4
    assert config.to_dict() == _netclass_payload()
    assert config.reviewed_digest == ""
    assert config.included_section_ids == ()
    assert database.record == before
    assert database.saves == []


def test_explicit_save_upgrades_payload_without_changing_database_envelope() -> None:
    """Payload schema and generic storage envelope have independent versions."""
    database = MemoryDatabase(_netclass_payload(4))
    repository = ImpedanceRepository(database, "board-a")
    config, revision = repository.load()
    assert repository.save(config, revision) == 5
    assert database.record["version"] == 1
    assert database.record["payload"] == _netclass_payload()
    assert repository.load() == (config, 5)
    assert len(database.saves) == 1


def test_real_database_upgrade_reopen_and_conflict_preserve_other_board(
    tmp_path: Path,
) -> None:
    """Real SQLite must not lose another board or permit stale editor writes."""
    database_path = tmp_path / "jlcpcb" / "project.db"
    database = ImpedanceDatabase(database_path)
    board_paths = (tmp_path / "usb.kicad_pcb", tmp_path / "radio.kicad_pcb")
    for path in board_paths:
        path.write_text("(kicad_pcb)", encoding="utf-8")
    first_id, other_id = (database.resolve_board(path) for path in board_paths)
    database.save_config(first_id, _netclass_payload(4), False, 0)
    other_payload = _netclass_payload()
    other_payload["specifications"][0]["label"] = "Other board"
    database.save_config(other_id, other_payload, False, 0)
    before = database.load_config(first_id)
    other_before = database.load_config(other_id)

    first = ImpedanceRepository(database, first_id)
    config, revision = first.load()
    stale_config, stale_revision = ImpedanceRepository(database, first_id).load()
    assert database.load_config(first_id) == before
    assert config.to_dict() == _netclass_payload()
    changed = replace(config, enabled=True)
    assert first.save(changed, revision) == 2
    reopened = ImpedanceDatabase(database_path)
    assert reopened.load_config(first_id)["version"] == 1
    assert reopened.load_config(first_id)["payload"]["schema_version"] == 5
    assert ImpedanceRepository(reopened, first_id).load() == (changed, 2)
    with pytest.raises(ConfigConflictError):
        ImpedanceRepository(reopened, first_id).save(stale_config, stale_revision)
    assert ImpedanceRepository(reopened, first_id).load() == (changed, 2)
    assert reopened.load_config(other_id) == other_before


def test_real_database_roundtrips_netclass_and_per_layer_overrides(
    tmp_path: Path,
) -> None:
    """Normal saves preserve class choice, exclusions, gaps, and both inner planes."""
    database_path = tmp_path / "jlcpcb" / "project.db"
    database = ImpedanceDatabase(database_path)
    board_path = tmp_path / "usb.kicad_pcb"
    board_path.write_text("(kicad_pcb)", encoding="utf-8")
    board_id = database.resolve_board(board_path)
    payload = _netclass_payload()
    database.save_config(board_id, payload, False, 0)
    before = database.load_config(board_id)
    repository = ImpedanceRepository(database, board_id)
    config, revision = repository.load()
    assert config.to_dict() == payload
    assert config.specifications[0].net_class == "USB"
    assert database.load_config(board_id) == before
    changed = replace(config, enabled=True)
    assert repository.save(changed, revision) == 2
    reopened = ImpedanceDatabase(database_path)
    assert ImpedanceRepository(reopened, board_id).load() == (changed, 2)
    expected = deepcopy(payload)
    expected["enabled"] = True
    assert reopened.load_config(board_id) == {
        "version": 1,
        "revision": 2,
        "enabled": True,
        "payload": expected,
    }


@pytest.mark.parametrize("version", [0, 2, 3, True, False, "1", 1.0, None])
def test_unknown_database_envelopes_are_not_payload_versions(version: Any) -> None:
    """A supported payload never licenses a different database envelope contract."""
    database = MemoryDatabase(_netclass_payload(), version)
    before = deepcopy(database.record)
    with pytest.raises(ValidationError, match="database version"):
        ImpedanceRepository(database, "board-a").load()
    assert database.record == before
    assert database.saves == []


@pytest.mark.parametrize(
    "corruption", ["future_version", "retired_filter", "missing_class", "unknown_field"]
)
def test_malformed_or_future_payloads_remain_untouched(corruption: str) -> None:
    """Strict decoding must never guess incomplete intent or discard stored fields."""
    payload = _netclass_payload()
    if corruption == "future_version":
        payload["schema_version"] = 6
    elif corruption == "retired_filter":
        payload["specifications"][0]["net_names"] = ["USB_P", "USB_N"]
    elif corruption == "missing_class":
        del payload["specifications"][0]["net_class"]
    else:
        payload["unexpected"] = "must not be dropped"
    database = MemoryDatabase(payload)
    before = deepcopy(database.record)
    with pytest.raises(ValidationError):
        ImpedanceRepository(database, "board-a").load()
    assert database.record == before
    assert database.saves == []


def test_empty_current_payload_uses_existing_database_envelope() -> None:
    """A new disabled configuration reopens without a database schema migration."""
    payload = _netclass_payload()
    payload["specifications"] = []
    database = MemoryDatabase(payload)
    assert ImpedanceRepository(database, "board-a").load() == (Config(), 4)
    assert database.record["payload"] == payload
    assert database.saves == []
