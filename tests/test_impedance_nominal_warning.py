"""The active nominal-width failure is visibly distinct from pending work."""

from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace

import pytest

from .test_impedance_spec_width_hint import (
    _ninety_ohm_inputs,
    _successful_width,
    constructor_api,
    workers,
)

__all__ = ["constructor_api", "workers"]


@pytest.mark.parametrize("status", ["unavailable", "unsupported", "error"])
def test_current_unavailable_nominal_is_coloured_and_recovery_clears_it(
    constructor_api: SimpleNamespace,
    workers: list[Callable[[], None]],
    status: str,
) -> None:
    """Color follows actual result/input state through retry and correction."""
    api = constructor_api
    stackup, board, single, differential = _ninety_ohm_inputs(api)
    failure = replace(
        _successful_width(api, stackup, single),
        status=status,
        target_width_nm=None,
        message="Sample numerical result unavailable",
    )
    dialog = api.dialog.SpecificationDialog(
        None, board, single, stackup=stackup, width_results=(failure,)
    )
    default = dialog.target.GetForegroundColour()
    assert "Nominal width unavailable" in dialog.width_hint.GetLabel()
    assert dialog.width_hint.GetForegroundColour() != default
    dialog.kind.SetSelection(1)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    assert dialog.width_hint.GetForegroundColour() == default
    corrected = replace(
        dialog._selection_specification(), layer_settings=differential.layer_settings
    )
    dialog.width_results = (_successful_width(api, stackup, corrected),)
    dialog._update_width_comparison(schedule=False)
    assert dialog.width_hint.GetForegroundColour() == default


def test_pending_nominal_uses_normal_text(
    constructor_api: SimpleNamespace, workers: list[Callable[[], None]]
) -> None:
    """Initial pending calculation is not presented as a numerical failure."""
    api = constructor_api
    stackup, board, single, _differential = _ninety_ohm_inputs(api)
    dialog = api.dialog.SpecificationDialog(None, board, single, stackup=stackup)
    assert (
        dialog.width_hint.GetForegroundColour() == dialog.target.GetForegroundColour()
    )


def test_slow_initial_calculation_remains_neutral(
    constructor_api: SimpleNamespace, workers: list[Callable[[], None]]
) -> None:
    """A slow response is still pending, rather than a failed numerical result."""
    api = constructor_api
    stackup, board, single, _ = _ninety_ohm_inputs(api)
    dialog = api.dialog.SpecificationDialog(None, board, single, stackup=stackup)
    default = dialog.width_hint.GetForegroundColour()
    dialog._on_draft_calc_timer(SimpleNamespace())
    assert dialog._draft_calc_inflight
    dialog._on_draft_slow_timer(SimpleNamespace())
    assert "taking longer" in dialog.width_hint.GetLabel()
    assert dialog.width_hint.GetForegroundColour() == default


def test_failed_calculation_stays_coloured_during_retry_and_slow_response(
    constructor_api: SimpleNamespace, workers: list[Callable[[], None]]
) -> None:
    """Retrying a failed input preserves its warning until a usable result arrives."""
    api = constructor_api
    stackup, board, single, _ = _ninety_ohm_inputs(api)
    dialog = api.dialog.SpecificationDialog(None, board, single, stackup=stackup)
    default = dialog.width_hint.GetForegroundColour()
    dialog._on_draft_calc_timer(SimpleNamespace())
    dialog._finish_draft_calculation(
        dialog._draft_calc_generation,
        dialog._draft_calc_digest,
        None,
        "Service offline",
    )
    assert dialog.width_hint.GetForegroundColour() != default
    dialog._on_draft_retry_timer(SimpleNamespace())
    dialog._on_draft_calc_timer(SimpleNamespace())
    dialog._on_draft_slow_timer(SimpleNamespace())
    assert dialog._draft_calc_inflight
    assert dialog.width_hint.GetForegroundColour() != default
