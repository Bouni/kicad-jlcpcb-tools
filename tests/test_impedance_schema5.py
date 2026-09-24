"""Keep schema upgrades explicit, board-scoped, and atomic in real SQLite."""

from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from impedance.database import ConfigConflictError, ImpedanceDatabase
from impedance.model import Config, ValidationError
from impedance.repository import ImpedanceRepository

STAMP = "2026-09-08T12:34:56.000000Z"


def _v4_payload() -> dict[str, Any]:
    """Describe historical JSON independently of the current model serializer."""
    return {
        "schema_version": 4,
        "enabled": True,
        "specifications": [
            {
                "spec_id": "rf",
                "label": "RF 50 Ω",
                "target_ohms": "50",
                "kind": "single_ended",
                "net_class": "RF",
                "excluded_layers": [],
                "layer_settings": [
                    {
                        "layer": "F.Cu",
                        "reference_layers": ["B.Cu"],
                        "spacing_nm": None,
                        "ground_gap_nm": None,
                    }
                ],
            }
        ],
        "reviewed_digest": "old-approved-report",
        "included_section_ids": ["old-section"],
        "section_groups": [["old-section", "another-section"]],
        "legacy_specifications": [],
        "review_tracking": {
            "images": [
                {
                    "spec_id": "rf",
                    "layer": "F.Cu",
                    "section_id": "old-section",
                    "capture_digest": "a" * 64,
                    "viewed_at_utc": STAMP,
                }
            ],
            "layers": [
                {
                    "spec_id": "rf",
                    "layer": "F.Cu",
                    "settings_digest": "b" * 64,
                    "approved_at_utc": STAMP,
                    "captures": [["old-section", "a" * 64]],
                }
            ],
        },
        "stackup": {
            "stackup_id": "selected",
            "name": "Selected two-layer construction",
            "layer_count": 2,
            "thickness_mm": "1.6",
            "outer_copper_oz": "1",
            "inner_copper_oz": "",
            "preferred": True,
            "charge_status": "none",
            "layers": [],
            "calculator_id": "calculator-selected",
            "source_url": "https://jlcpcb.com/impedance",
            "retrieved_at_utc": STAMP,
            "source_payload_json": '{"order":{"label":"retain"},"calculator":{}}',
        },
        "width_results": [
            {
                "spec_id": "rf",
                "layer": "F.Cu",
                "input_digest": "c" * 64,
                "status": "success",
                "target_width_nm": 203_200,
                "calculated_at_utc": STAMP,
                "provider": "JLCPCB",
                "message": "",
                "response_json": json.dumps(
                    {
                        "model": "microstrip",
                        "units": "mil",
                        "adapter_revision": 1,
                        "parameters": {"Z0": "50", "H1": "8", "Er1": "4.2"},
                        "result": {"W1": "8"},
                        "construction": {"dielectric_mm": "0.2"},
                        "assumptions": ["Nominal vendor calculation"],
                    }
                ),
            }
        ],
    }


def _repository(tmp_path: Path, name: str = "board") -> ImpedanceRepository:
    """Construct real board identity and repository objects, without plugin UI."""
    board = tmp_path / f"{name}.kicad_pcb"
    board.write_text("(kicad_pcb)\n", encoding="utf-8")
    database = ImpedanceDatabase(tmp_path / "jlcpcb" / "project.db")
    return ImpedanceRepository(database, database.resolve_board(board))


def _seed(repository: ImpedanceRepository, payload: dict[str, Any]) -> str:
    """Preserve deliberately noncanonical JSON as an existing revision-seven row."""
    raw = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with repository.database.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO board_feature_config VALUES (?, 'impedance', 1, 7, 1, ?)",
            (repository.board_id, raw),
        )
    return raw


def _state(database: ImpedanceDatabase) -> tuple[str, ...]:
    """Compare all persistent rows and schemas."""
    with database.connect() as connection:
        return tuple(connection.iterdump())


def test_v4_load_preserves_intent_and_history_without_writing(tmp_path: Path) -> None:
    """Upgrade only in memory; historical viewing does not authorize new rows."""
    repository = _repository(tmp_path)
    payload = _v4_payload()
    _seed(repository, payload)
    before = _state(repository.database)

    config, revision = repository.load()

    assert revision == 7
    assert _state(repository.database) == before
    upgraded = config.to_dict()
    assert upgraded["schema_version"] == 5
    assert upgraded["enabled"] is True
    assert upgraded["specifications"] == payload["specifications"]
    assert upgraded["review_tracking"] == payload["review_tracking"]
    assert upgraded["reviewed_digest"] == ""
    assert upgraded["included_section_ids"] == []
    assert "section_groups" not in upgraded and "legacy_specifications" not in upgraded
    assert upgraded["stackup"] == {
        key: value
        for key, value in payload["stackup"].items()
        if key != "source_payload_json"
    }
    result = config.width_results[0]
    assert (result.spec_id, result.layer, result.input_digest) == (
        "rf",
        "F.Cu",
        "c" * 64,
    )
    assert (result.status, result.target_width_nm, result.calculated_at_utc) == (
        "success",
        203_200,
        STAMP,
    )
    assert result.model == "microstrip"
    assert result.assumptions == ("Nominal vendor calculation",)
    assert len(result.calculation_digest) == 64
    assert "response_json" not in upgraded["width_results"][0]


def test_upgrade_and_ordinary_save_reopen_and_preserve_other_board(
    tmp_path: Path,
) -> None:
    """Upgraded settings reopen, and both saves preserve the other board's row."""
    repository = _repository(tmp_path)
    _seed(repository, _v4_payload())
    other = _repository(tmp_path, "other")
    _seed(other, _v4_payload())
    other_before = other.database.load_config(other.board_id)
    config, revision = repository.load()

    assert repository.save(config, revision) == 8
    reopened = ImpedanceRepository(
        ImpedanceDatabase(repository.database.path), repository.board_id
    )
    assert reopened.load() == (config, 8)
    assert reopened.save(config, 8) == 9
    assert other.database.load_config(other.board_id) == other_before


def test_stale_upgrade_revision_cannot_mutate(tmp_path: Path) -> None:
    """A stale editor must fail before replacing any persistent settings."""
    repository = _repository(tmp_path)
    _seed(repository, _v4_payload())
    config, revision = repository.load()
    before = _state(repository.database)
    with pytest.raises(ConfigConflictError):
        repository.save(config, revision - 1)
    assert _state(repository.database) == before


def test_failed_upgrade_write_preserves_exact_previous_record(tmp_path: Path) -> None:
    """A failed replacement leaves the old settings and other persistent rows intact."""
    repository = _repository(tmp_path)
    _seed(repository, _v4_payload())
    config, revision = repository.load()
    with repository.database.connect(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER reject_upgrade BEFORE UPDATE ON board_feature_config "
            "BEGIN SELECT RAISE(ABORT, 'injected upgrade failure'); END"
        )
    before = _state(repository.database)
    with pytest.raises(sqlite3.DatabaseError, match="injected upgrade failure"):
        repository.save(config, revision)
    assert _state(repository.database) == before


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_retired_formats_require_explicit_reset_and_preserve_other_board(
    tmp_path: Path,
    version: int,
) -> None:
    """Reading never changes old settings; explicit reset replaces only their row."""
    repository = _repository(tmp_path)
    payload = _v4_payload()
    payload["schema_version"] = version
    if version == 4:
        payload["legacy_specifications"] = [{"spec_id": "old-width-filter"}]
    _seed(repository, payload)
    other = _repository(tmp_path, "other")
    _seed(other, _v4_payload())
    other_before = other.database.load_config(other.board_id)
    before = _state(repository.database)
    with pytest.raises(ValidationError, match="Reset settings"):
        repository.load()
    assert _state(repository.database) == before
    token = repository.database.config_reset_token(repository.board_id)
    assert (
        repository.database.reset_config(repository.board_id, token, Config().to_dict())
        == 8
    )
    assert repository.load() == (Config(), 8)
    assert other.database.load_config(other.board_id) == other_before


def test_new_board_does_not_inherit_another_boards_schema5_configuration(
    tmp_path: Path,
) -> None:
    """Opening a different PCB never copies another board's intent or approval."""
    repository = _repository(tmp_path)
    config = replace(
        Config.from_dict(_v4_payload()),
        reviewed_digest="approved",
        included_section_ids=("old-section",),
    )
    assert repository.save(config, 0) == 1
    assert repository.save(config, 1) == 2
    before = repository.database.load_config(repository.board_id)
    copied_path = tmp_path / "copy.kicad_pcb"
    copied_path.write_text("(kicad_pcb)\n", encoding="utf-8")
    copied_id = repository.database.resolve_board(copied_path)
    copied, revision = ImpedanceRepository(repository.database, copied_id).load()
    assert revision == 0
    assert copied == Config()
    assert copied.enabled is False
    assert copied.reviewed_digest == "" and copied.included_section_ids == ()
    assert copied.review_tracking == Config().review_tracking
    assert copied.width_results == ()
    assert repository.database.load_config(copied_id) is None
    assert repository.database.load_config(repository.board_id) == before
