"""Persist unfinished intent without granting permission to export it."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from impedance import model
from impedance.database import ImpedanceDatabase
from impedance.matching import analyze
from impedance.repository import ImpedanceRepository
from impedance.service import prepare


def _specification() -> model.Specification:
    """Describe a complete differential class routed on both outer layers."""
    return model.Specification(
        "usb",
        "USB",
        "90",
        "differential_coplanar",
        "USB",
        (
            model.LayerSettings("F.Cu", ("In1.Cu",), 180_000, 220_000),
            model.LayerSettings("B.Cu", ("In2.Cu",), 180_000, 220_000),
        ),
    )


def _snapshot() -> model.BoardSnapshot:
    """Use complete native pair metadata so readiness, not selection, is tested."""
    return model.BoardSnapshot(
        layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"),
        traces=tuple(
            model.Trace(
                f"{layer}-{net}", layer, net, 200_000, ((0, y), (20_000_000, y))
            )
            for layer in ("F.Cu", "B.Cu")
            for net, y in (("USB_P", 0), ("USB_N", 380_000))
        ),
        context_digest="board-context",
        net_classes=("USB",),
        net_class_memberships=(("USB_P", ("USB",)), ("USB_N", ("USB",))),
        net_class_context_digest="class-context",
        differential_pairs=(("USB_P", "USB_N"),),
    )


def _repository(tmp_path: Path) -> ImpedanceRepository:
    """Exercise the actual SQLite envelope and board-scoped revision contract."""
    board = tmp_path / "board.kicad_pcb"
    board.write_text("(kicad_pcb)\n", encoding="utf-8")
    database = ImpedanceDatabase(tmp_path / "jlcpcb" / "project.db")
    return ImpedanceRepository(database, database.resolve_board(board))


def _legacy_payload(version: int) -> dict[str, Any]:
    """Represent historical schemas independently of the current serializer."""
    payload: dict[str, Any] = {
        "schema_version": version,
        "enabled": True,
        "specifications": [
            {
                "spec_id": "usb",
                "label": "USB",
                "target_ohms": "90",
                "kind": "differential_coplanar",
                "net_class": "USB",
                "layer_settings": [
                    {
                        "layer": layer,
                        "reference_layers": [reference],
                        "spacing_nm": 180_000,
                        "ground_gap_nm": 220_000,
                    }
                    for layer, reference in (("F.Cu", "In1.Cu"), ("B.Cu", "In2.Cu"))
                ],
                "excluded_layers": [],
            }
        ],
        "reviewed_digest": "previous-approved-report",
        "included_section_ids": ["previous-section"],
        "review_tracking": {"images": [], "layers": []},
        "stackup": None,
        "width_results": [],
    }
    if version == 4:
        payload.update(section_groups=[], legacy_specifications=[])
    return payload


@pytest.mark.parametrize("missing", ["target", "references", "spacing", "ground_gap"])
def test_incomplete_v6_intent_roundtrips_real_storage(
    tmp_path: Path, missing: str
) -> None:
    """One unfinished layer or target must not discard the other valid edits."""
    spec = _specification()
    settings = spec.layer_settings[1]
    if missing == "target":
        spec = replace(spec, target_ohms="")
    else:
        fields = {"references": "reference_layers", "spacing": "spacing_nm"}
        field = fields.get(missing, "ground_gap_nm")
        settings = replace(settings, **{field: () if missing == "references" else None})
        spec = replace(spec, layer_settings=(spec.layer_settings[0], settings))
    draft = model.Config(enabled=True, specifications=(spec,))
    repository = _repository(tmp_path)

    assert repository.save(draft, 0) == 1
    reopened = ImpedanceRepository(
        ImpedanceDatabase(repository.database.path), repository.board_id
    )
    restored, revision = reopened.load()

    assert (restored, revision) == (draft, 1)
    assert restored.reviewed_digest == ""
    assert restored.included_section_ids == ()
    assert restored.review_tracking == model.Config().review_tracking
    record = repository.database.load_config(repository.board_id)
    assert record is not None
    assert record["version"] == 1
    assert record["payload"]["schema_version"] == 8
    assert record["payload"]["specifications"][0]["linked_layer_groups"] == []
    assert record["payload"]["specifications"][0]["independent_layers"] == []
    for operation in (
        lambda: model.validate_config(restored, _snapshot().layers),
        lambda: analyze(restored, _snapshot()),
        lambda: prepare(restored, _snapshot()),
    ):
        with pytest.raises(model.ValidationError):
            operation()


def test_excluded_only_draft_roundtrips_but_is_not_ready(tmp_path: Path) -> None:
    """Excluding all present layers can be saved, but cannot create a report."""
    spec = replace(
        _specification(), layer_settings=(), excluded_layers=("F.Cu", "B.Cu")
    )
    draft = model.Config(enabled=True, specifications=(spec,))
    repository = _repository(tmp_path)
    assert repository.save(draft, 0) == 1
    assert repository.load() == (draft, 1)
    with pytest.raises(model.ValidationError, match="at least one"):
        model.validate_config(draft)


@pytest.mark.parametrize(
    "changes",
    [
        {"target_ohms": " "},
        {"target_ohms": "not a number"},
        {"target_ohms": "-90"},
        {"target_ohms": "NaN"},
        {"net_class": ""},
        {"layer_settings": ()},
        {"layer_settings": (model.LayerSettings("F.Cu", ("F.Cu",)),)},
        {"layer_settings": (model.LayerSettings("F.Cu", ("In1.Cu",) * 2),)},
        {"layer_settings": (model.LayerSettings("F.Cu", (), 0),)},
        {"layer_settings": (model.LayerSettings("F.Cu", (), True),)},
        {"layer_settings": (model.LayerSettings("F.Cu", (), "180000"),)},
        {"layer_settings": (model.LayerSettings("F.Cu", (), None, -1),)},
    ],
)
def test_storage_rejects_malformed_present_values(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    """Allow missing values without weakening identities or dimensional types."""
    repository = _repository(tmp_path)
    config = model.Config(
        enabled=True, specifications=(replace(_specification(), **changes),)
    )
    with pytest.raises(model.ValidationError):
        repository.save(config, 0)
    assert repository.database.load_config(repository.board_id) is None


@pytest.mark.parametrize("version", [4, 5])
def test_legacy_load_is_read_only_and_preserves_physical_intent(
    tmp_path: Path, version: int
) -> None:
    """Only old v4 approval is revoked; v5 approval is not changed by metadata."""
    repository = _repository(tmp_path)
    payload = _legacy_payload(version)
    repository.database.save_config(repository.board_id, payload, True, 0)
    before = repository.database.load_config(repository.board_id)

    config, revision = repository.load()

    assert revision == 1
    assert repository.database.load_config(repository.board_id) == before
    assert config.specifications == (_specification(),)
    assert config.specifications[0].linked_layer_groups == ()
    assert config.specifications[0].independent_layers == ()
    assert config.reviewed_digest == (
        "" if version == 4 else payload["reviewed_digest"]
    )
    assert config.included_section_ids == (
        () if version == 4 else ("previous-section",)
    )
    assert config.to_dict()["schema_version"] == 8


@pytest.mark.parametrize("version", [4, 5])
def test_legacy_incomplete_values_remain_corrupt_without_writing(
    tmp_path: Path, version: int
) -> None:
    """The new draft contract does not reinterpret damaged historical records."""
    repository = _repository(tmp_path)
    payload = _legacy_payload(version)
    payload["specifications"][0]["layer_settings"][0]["spacing_nm"] = None
    repository.database.save_config(repository.board_id, payload, True, 0)
    before = repository.database.load_config(repository.board_id)
    with pytest.raises(model.ValidationError, match="positive pair spacing"):
        repository.load()
    assert repository.database.load_config(repository.board_id) == before


def test_sync_metadata_roundtrips_without_changing_physical_approval() -> None:
    """Link preferences do not change routing, calculator inputs, or report intent."""
    original = model.Config(enabled=True, specifications=(_specification(),))
    linked = replace(
        original,
        specifications=(
            replace(_specification(), linked_layer_groups=(("F.Cu", "B.Cu"),)),
        ),
    )
    assert model.Config.from_dict(linked.to_dict()) == linked
    assert analyze(linked, _snapshot()).digest == analyze(original, _snapshot()).digest


def test_excluded_independent_override_roundtrips_without_linking() -> None:
    """An explicit independent preference survives exclusion and later inclusion."""
    spec = replace(
        _specification(),
        excluded_layers=("In1.Cu",),
        independent_layers=("F.Cu", "In1.Cu"),
    )
    config = model.Config(specifications=(spec,))
    assert model.Config.from_dict(config.to_dict()) == config


@pytest.mark.parametrize(
    "metadata",
    [
        {"linked_layer_groups": (("F.Cu",),)},
        {"linked_layer_groups": (("F.Cu", "F.Cu"),)},
        {"linked_layer_groups": (("F.Cu", "B.Cu"), ("B.Cu", "F.Cu"))},
        {"linked_layer_groups": (("F.Cu", "In1.Cu"),)},
        {"linked_layer_groups": (("F.Cu", "B.Cu"),), "independent_layers": ("B.Cu",)},
        {"independent_layers": ("F.Cu", "F.Cu")},
        {"independent_layers": ("In1.Cu",)},
    ],
)
def test_sync_metadata_rejects_ambiguous_or_unconfigured_membership(
    metadata: dict[str, Any],
) -> None:
    """A layer belongs to one linked group or one explicit independent override."""
    spec = replace(_specification(), **metadata)
    with pytest.raises(model.ValidationError):
        model.validate_storage_config(model.Config(specifications=(spec,)))


def test_v6_decoder_rejects_unknown_fields_without_mutating_input() -> None:
    """Draft support retains strict JSON shapes and never silently drops fields."""
    payload = _legacy_payload(5)
    payload["schema_version"] = 6
    spec = payload["specifications"][0]
    spec.update(linked_layer_groups=[], independent_layers=[], raw_spacing="unfinished")
    original = deepcopy(payload)
    with pytest.raises(model.ValidationError, match="Invalid specification"):
        model.Config.from_dict(payload)
    assert payload == original
