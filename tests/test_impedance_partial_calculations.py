"""Completed layers calculate and cache normally while sibling intent is unfinished."""

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from .test_impedance_calculation_completion import (
    _approved_board,
    _calculate,
    _result,
    draft_api,  # noqa: F401 -- shared constructor fixture
    workers,  # noqa: F401
)
from .test_impedance_draft_dialog import _open, constructor_api  # noqa: F401


def test_partial_draft_runs_complete_layer_through_worker_and_cache(
    draft_api: SimpleNamespace,  # noqa: F811
    workers: list[Any],  # noqa: F811
) -> None:
    """A missing reference on B must not block F scheduling, delivery, or cache adoption."""
    api = draft_api
    config, board = _approved_board(api, prior_results=False, two_layers=True)
    spec = replace(
        config.specifications[0],
        layer_settings=(
            config.specifications[0].layer_settings[0],
            api.model.LayerSettings("B.Cu", ()),
        ),
    )
    config = replace(
        config, specifications=(spec,), reviewed_digest="", included_section_ids=()
    )
    called: list[str] = []

    def calculate(stackup: Any, specification: Any, layer: str, **_kwargs: Any) -> Any:
        called.append(layer)
        return _result(api, stackup, specification, layer)

    # Use the real constructor/show machinery, but no pre-approved cache helper:
    # the whole document is intentionally unfinished and cannot be approved.
    dialog = _open(api, config, board)
    dialog._cancel_calculation()
    workers.clear()
    dialog.calculate_width_callback = calculate
    _calculate(api, dialog, workers)
    assert called == ["F.Cu"]
    assert [(row.layer, row.status) for row in dialog.session.config.width_results] == [
        ("F.Cu", "success")
    ]
    assert not dialog.session.approved
    result = dialog.session.config.width_results[0]
    reopened = _open(api, config, board)
    reopened._seed_width_results_from_cache(lambda: (result,))
    assert reopened.session.config.width_results == (result,)
    assert not reopened.session.approved
