"""Retire manual groups safely while preserving automatic pair review boundaries."""

from copy import deepcopy
from dataclasses import replace

import pytest

from impedance import matching, model


def _snapshot() -> model.BoardSnapshot:
    """Provide two native differential pairs within one explicitly assigned class."""
    nets = ("A", "B", "C", "D")
    return model.BoardSnapshot(
        layers=("F.Cu", "B.Cu"),
        traces=tuple(
            model.Trace(
                net.lower(),
                "F.Cu",
                net,
                200000,
                ((0, index * 400000), (10000000, index * 400000)),
            )
            for index, net in enumerate(nets)
        ),
        context_digest="native-board",
        net_classes=("USB",),
        net_class_memberships=tuple((net, ("USB",)) for net in nets),
        net_class_context_digest="native-classes",
        differential_pairs=(("A", "B"), ("C", "D")),
    )


def _config(*, differential: bool = True) -> model.Config:
    """Keep pair identity native while varying only automatic row grouping."""
    return model.Config(
        enabled=True,
        specifications=(
            model.Specification(
                spec_id="usb",
                label="USB",
                target_ohms="90" if differential else "50",
                kind="differential" if differential else "single_ended",
                net_class="USB",
                layer_settings=(
                    model.LayerSettings(
                        "F.Cu",
                        ("B.Cu",),
                        200000 if differential else None,
                    ),
                ),
            ),
        ),
    )


@pytest.mark.parametrize(
    "old_groups",
    [
        [["old-route", "missing-route"]],
        [["old-a", "old-b", "old-c", "old-d"]],
        [["old-a", "old-b"], ["old-c", "old-d"]],
    ],
)
def test_v4_manual_groups_never_override_native_pair_rows_or_preserve_approval(
    old_groups: list[list[str]],
) -> None:
    """Retired group IDs are discarded, not used to join unrelated native pairs."""
    payload = _config().to_dict()
    payload.update(
        schema_version=4,
        section_groups=old_groups,
        legacy_specifications=[],
        reviewed_digest="old-approved-review",
        included_section_ids=["old-combined-row"],
    )
    original = deepcopy(payload)

    restored = model.Config.from_dict(payload)
    current = matching.analyze(restored, _snapshot())

    assert payload == original, "Loading must not rewrite the historical document."
    assert restored.reviewed_digest == "" and restored.included_section_ids == ()
    assert "section_groups" not in restored.to_dict()
    assert {row.net_names for row in current.sections} == {("A", "B"), ("C", "D")}
    with pytest.raises(model.ValidationError, match="review"):
        matching.validate_review(restored, current)
    approved = replace(
        restored,
        reviewed_digest=current.digest,
        included_section_ids=tuple(row.section_id for row in current.sections),
    )
    reopened = model.Config.from_dict(approved.to_dict())
    matching.validate_review(reopened, matching.analyze(reopened, _snapshot()))


def test_retired_filters_require_explicit_reset_even_with_native_pair_metadata() -> (
    None
):
    """A valid board cannot license silently reinterpreting a historical manual selection."""
    payload = _config().to_dict()
    payload.update(
        schema_version=4,
        section_groups=[["a", "b"]],
        legacy_specifications=[{"spec_id": "old-filter", "net_names": ["A", "B"]}],
    )
    before = deepcopy(payload)
    with pytest.raises(model.ValidationError, match="Reset settings.*replace"):
        model.Config.from_dict(payload)
    assert payload == before


@pytest.mark.parametrize("failure", ("pair-metadata", "overlapping-specifications"))
def test_unrelated_matching_failure_is_not_hidden_by_group_retirement(
    failure: str,
) -> None:
    """Upgrading old groups must not hide broken native identity or conflicting classes."""
    payload = _config().to_dict()
    payload.update(
        schema_version=4, section_groups=[["old-a", "old-b"]], legacy_specifications=[]
    )
    config = model.Config.from_dict(payload)
    snapshot = _snapshot()
    if failure == "pair-metadata":
        snapshot = replace(
            snapshot, differential_pair_error="Native pair metadata unavailable."
        )
    else:
        config = replace(
            config,
            specifications=config.specifications
            + (
                replace(
                    config.specifications[0], spec_id="duplicate", label="Duplicate"
                ),
            ),
        )
    original = config.to_dict()
    with pytest.raises(model.ValidationError):
        matching.analyze(config, snapshot)
    assert config.to_dict() == original
