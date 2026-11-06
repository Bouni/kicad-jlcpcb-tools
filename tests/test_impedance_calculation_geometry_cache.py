"""Width completion reuses physical matching without reusing stale review inputs.

The matching spy delegates to production geometry. The dialog cases use the real
constructor and completion handler with the existing stateful control harness;
native event delivery remains covered by the unchanged idle-fairness test.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import importlib
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest

from .test_impedance_calculation_completion import (
    _approved_board,
    _result,
    _setup,
    draft_api as _draft_api,
    workers as _workers,
)
from .test_impedance_draft_dialog import constructor_api  # noqa: F401

draft_api = _draft_api
workers = _workers


def _matching(api: SimpleNamespace) -> Any:
    """Use the matching module belonging to this fixture's private package."""
    return importlib.import_module(api.dialog.__package__ + ".matching")


def _matching_calls(
    api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[Any, ...]]:
    """Observe actual geometry work without replacing its results or state."""
    module = _matching(api)
    original = module._netclass_sections
    calls: list[tuple[Any, ...]] = []

    def match(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "_netclass_sections", match)
    return calls


def test_session_width_change_reuses_matching_and_rebinds_review_digest(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new nominal affects approval and digest, but not matched PCB geometry."""
    api = draft_api
    config, board = _approved_board(api, prior_results=True)
    session = api.dialog.ReviewSession(config, board)
    before = session.refresh()
    assert session.approved
    delivered = replace(config.width_results[0], target_width_nm=200_000)
    expected = _matching(api).analyze(
        replace(config, width_results=(delivered,)), board
    )
    calls = _matching_calls(api, monkeypatch)

    session.replace_calculation_context(config.stackup, (delivered,))
    after = session.refresh()

    assert after == expected
    assert after.sections == before.sections
    assert after.digest != before.digest
    assert session.config.width_results == (delivered,)
    assert not session.approved
    assert session.config.reviewed_digest == ""
    assert calls == [], "Updating nominal widths repeated physical geometry matching"


def test_dialog_completion_updates_nominal_cells_without_rematching(
    draft_api: SimpleNamespace,
    workers: list[Callable[[], None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production constructor/completion path keeps rows and updates targets."""
    api = draft_api
    dialog, _persisted = _setup(api, workers, prior_results=True)
    before = dialog.session.analysis
    assert before is not None
    assert dialog.session.approved
    row_ids = tuple(dialog._row_ids)
    assert all(row[9] == "0.15 mm" for row in dialog.sections.table_rows)
    delivered = replace(dialog.session.config.width_results[0], target_width_nm=200_000)
    expected = _matching(api).analyze(
        replace(dialog.session.config, width_results=(delivered,)),
        dialog.session.snapshot,
    )
    calls = _matching_calls(api, monkeypatch)
    dialog._calculation_cancel = Event()

    dialog._finish_calculation(
        dialog._calculation_generation,
        dialog._calculation_key(dialog.session.config, dialog.session.snapshot),
        (delivered,),
        "",
    )
    api.drain()

    assert dialog.session.analysis == expected
    assert dialog.session.config.width_results == (delivered,)
    assert dialog.session.analysis.digest != before.digest
    assert tuple(dialog._row_ids) == row_ids
    assert all(row[9] == "0.2 mm" for row in dialog.sections.table_rows)
    assert all(row[8] == "!=" for row in dialog.sections.table_rows)
    assert not dialog.session.approved
    assert dialog.session.config.reviewed_digest == ""
    assert dialog._calculation_cancel is None
    assert calls == [], (
        "Width completion repeated physical geometry matching on the GUI thread"
    )


@pytest.mark.parametrize("other_status", ["success", "unsupported", "error"])
def test_multilayer_completion_reuses_matching_for_partial_results(
    draft_api: SimpleNamespace,
    workers: list[Callable[[], None]],
    monkeypatch: pytest.MonkeyPatch,
    other_status: str,
) -> None:
    """Mixed provider results refresh both layer targets without rescanning copper."""
    api = draft_api
    dialog, _persisted = _setup(api, workers, prior_results=True, two_layers=True)
    config = dialog.session.config
    delivered = tuple(
        _result(
            api,
            config.stackup,
            config.specifications[0],
            layer,
            width_nm=200_000,
            status="success" if layer == "F.Cu" else other_status,
        )
        for layer in ("F.Cu", "B.Cu")
    )
    expected = _matching(api).analyze(
        replace(config, width_results=delivered), dialog.session.snapshot
    )
    calls = _matching_calls(api, monkeypatch)
    populations: list[None] = []
    original_populate = dialog._populate_sections

    def populate(*args: Any, **kwargs: Any) -> None:
        """Count complete projections while retaining real grid and preview state."""
        populations.append(None)
        original_populate(*args, **kwargs)

    monkeypatch.setattr(dialog, "_populate_sections", populate)
    dialog._calculation_cancel = Event()

    dialog._finish_calculation(
        dialog._calculation_generation,
        dialog._calculation_key(config, dialog.session.snapshot),
        delivered,
        "",
    )
    api.drain()

    assert set(dialog.session.config.width_results) == set(delivered)
    assert dialog.session.analysis == expected
    assert not dialog.session.approved
    for row in dialog.sections.table_rows:
        expected_target = (
            "0.2 mm"
            if row[2] == "F.Cu" or other_status == "success"
            else "Not calculated"
        )
        assert row[9] == expected_target
    assert calls == [], "Partial calculator results repeated physical geometry matching"
    assert populations == [None], (
        "Completion projected the same workbook more than once"
    )


def test_differential_width_change_reuses_geometry_and_rechecks_spacing_evidence(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nominals preserve diagnostic identities while revoking old workbook consent."""
    api = draft_api
    config, board = _approved_board(api, prior_results=False)
    spec = replace(
        config.specifications[0],
        kind="differential",
        target_ohms="90",
        layer_settings=(
            api.model.LayerSettings("F.Cu", ("B.Cu",), spacing_nm=150_000),
        ),
    )
    previous = _result(api, config.stackup, spec, "F.Cu")
    session = api.dialog.ReviewSession(
        replace(
            config,
            specifications=(spec,),
            width_results=(previous,),
            reviewed_digest="",
            included_section_ids=(),
        ),
        board,
    )
    before = session.refresh()
    entries = session.spacing_entries
    assert entries
    for entry in entries:
        session.record_spacing_view(
            entry, api.capture.sha256, "2026-10-24T12:34:56.000001Z"
        )
        session.approve_spacing_image(entry, api.capture.sha256)
    session.approve()
    assert session.approved
    history = session.config.review_tracking
    delivered = replace(previous, target_width_nm=200_000)
    expected = _matching(api).analyze(
        replace(session.config, width_results=(delivered,)), board
    )
    calls = _matching_calls(api, monkeypatch)

    session.replace_calculation_context(config.stackup, (delivered,))
    actual = session.refresh()

    assert actual == expected
    assert actual.sections == before.sections
    assert actual.digest != before.digest
    assert session.spacing_entries == entries
    assert session.config.review_tracking == history
    assert session.pending_spacing_reviews == entries
    assert not any(session.spacing_review_current(entry) for entry in entries)
    assert not session.approved
    assert calls == []


def test_equivalent_reconstructed_snapshot_reuses_physical_matching(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh immutable snapshot with equal copper is safe for width-only reuse."""
    api = draft_api
    config, board = _approved_board(api, prior_results=True)
    session = api.dialog.ReviewSession(config, board)
    session.refresh()
    reconstructed = replace(
        board, traces=tuple(replace(trace) for trace in board.traces)
    )
    assert reconstructed is not board
    assert reconstructed == board
    delivered = replace(config.width_results[0], target_width_nm=200_000)
    expected = _matching(api).analyze(
        replace(config, width_results=(delivered,)), reconstructed
    )
    calls = _matching_calls(api, monkeypatch)

    session.replace_calculation_context(config.stackup, (delivered,))
    actual = session.refresh(reconstructed)

    assert actual == expected
    assert session.snapshot is reconstructed
    assert not session.approved
    assert calls == []


def test_save_and_reopen_nominal_change_retains_history_without_old_approval(
    draft_api: SimpleNamespace,
    workers: list[Callable[[], None]],
) -> None:
    """Saving new targets never revives old consent or discards recorded image views."""
    api = draft_api
    dialog, persisted = _setup(api, workers, prior_results=True)
    history = dialog.session.config.review_tracking
    assert history.images
    assert dialog.session.approved
    old_reviewed = dialog.session.config.reviewed_digest
    delivered = replace(dialog.session.config.width_results[0], target_width_nm=200_000)
    dialog._calculation_cancel = Event()
    dialog._finish_calculation(
        dialog._calculation_generation,
        dialog._calculation_key(dialog.session.config, dialog.session.snapshot),
        (delivered,),
        "",
    )
    assert dialog.session.config.reviewed_digest == ""
    assert dialog.session.analysis.digest != old_reviewed

    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    api.drain()

    assert dialog.modal_result == api.wx.ID_OK
    saved = api.model.Config.from_dict(persisted["config"].to_dict())
    assert saved.width_results == (delivered,)
    assert saved.reviewed_digest == ""
    assert saved.review_tracking == history
    reopened = api.dialog.ReviewSession(saved, dialog.session.snapshot)
    assert reopened.refresh() == dialog.session.analysis
    assert not reopened.approved
    assert reopened.config.reviewed_digest == ""
    assert reopened.config.review_tracking == history


@pytest.mark.parametrize(
    "change",
    ["trace", "context", "memberships", "native-pairs", "copper", "bounds"],
)
def test_snapshot_changes_rebuild_matching(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """Physical matching cannot survive a changed owned board snapshot."""
    api = draft_api
    config, board = _approved_board(api, prior_results=True)
    session = api.dialog.ReviewSession(config, board)
    session.refresh()
    if change == "trace":
        changed = replace(board, traces=board.traces[1:])
    elif change == "context":
        changed = replace(board, context_digest="new-board-context")
    elif change == "memberships":
        changed = replace(
            board,
            net_class_memberships=(("CLK_P", ("Default",)),)
            + board.net_class_memberships[1:],
        )
    elif change == "native-pairs":
        changed = replace(board, differential_pairs=())
    elif change == "copper":
        changed = replace(
            board,
            copper_features=(
                api.model.CopperFeature(
                    "pad", "CLK_P", "F.Cu", "pad", "circle", (0, 0), 400_000, 400_000
                ),
            ),
        )
    else:
        changed = replace(
            board, board_bounds=(-1_000_000, -1_000_000, 6_000_000, 6_000_000)
        )
    expected = _matching(api).analyze(config, changed)
    calls = _matching_calls(api, monkeypatch)

    actual = session.refresh(changed)

    assert actual == expected
    assert calls, f"Changed snapshot {change} reused obsolete physical matching"


@pytest.mark.parametrize(
    "change", ["target", "class", "algorithm-policy", "capture-policy", "stub-policy"]
)
def test_specification_and_matching_policy_changes_rebuild_matching(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """Edits to intent and algorithm policy cannot reuse old matched sections."""
    api = draft_api
    config, board = _approved_board(api, prior_results=True)
    session = api.dialog.ReviewSession(config, board)
    session.refresh()
    if change.endswith("-policy"):
        module = _matching(api)
        attribute = {
            "algorithm-policy": "ALGORITHM_VERSION",
            "capture-policy": "CAPTURE_POLICY_REVISION",
            "stub-policy": "COVERED_STUB_POLICY_REVISION",
        }[change]
        monkeypatch.setattr(module, attribute, getattr(module, attribute) + 1)
    else:
        spec = config.specifications[0]
        spec = (
            replace(spec, target_ohms="75")
            if change == "target"
            else replace(
                spec,
                net_class="Default",
                layer_settings=(api.model.LayerSettings("B.Cu", ("F.Cu",)),),
            )
        )
        session.replace_specifications((spec,))
    expected = _matching(api).analyze(session.config, board)
    calls = _matching_calls(api, monkeypatch)

    actual = session.refresh()

    assert actual == expected
    assert calls, f"Changed {change} reused obsolete physical matching"


def test_differential_spacing_policy_change_rebuilds_matching(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new pair measurement policy cannot retain old physical analysis."""
    api = draft_api
    config, board = _approved_board(api, prior_results=False)
    spec = replace(
        config.specifications[0],
        kind="differential",
        target_ohms="90",
        layer_settings=(
            api.model.LayerSettings("F.Cu", ("B.Cu",), spacing_nm=200_000),
        ),
    )
    session = api.dialog.ReviewSession(
        replace(
            config, specifications=(spec,), reviewed_digest="", included_section_ids=()
        ),
        board,
    )
    before = session.refresh()
    module = importlib.import_module(api.dialog.__package__ + ".spacing_checks")
    monkeypatch.setattr(
        module,
        "SPACING_CHECK_POLICY_REVISION",
        module.SPACING_CHECK_POLICY_REVISION + 1,
    )
    expected = _matching(api).analyze(session.config, board)
    calls = _matching_calls(api, monkeypatch)

    actual = session.refresh()

    assert actual == expected
    assert actual.digest != before.digest
    assert calls, "Changed spacing policy reused obsolete physical matching"


def test_width_change_preserves_nonempty_matching_warnings(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cached geometry retains an explicit excluded-layer warning and fresh digest."""
    api = draft_api
    config, board = _approved_board(api, prior_results=True, two_layers=True)
    spec = replace(
        config.specifications[0],
        layer_settings=config.specifications[0].layer_settings[:1],
        excluded_layers=("B.Cu",),
    )
    session = api.dialog.ReviewSession(replace(config, specifications=(spec,)), board)
    before = session.refresh()
    assert any("explicitly excluded" in warning for warning in before.warnings)
    delivered = tuple(
        replace(result, target_width_nm=200_000)
        for result in session.config.width_results
    )
    expected = _matching(api).analyze(
        replace(session.config, width_results=delivered), board
    )
    calls = _matching_calls(api, monkeypatch)

    session.replace_calculation_context(config.stackup, delivered)
    actual = session.refresh()

    assert actual == expected
    assert actual.warnings == before.warnings
    assert actual.digest != before.digest
    assert calls == []


def test_stackup_change_recomputes_digest_and_rejects_old_nominal(
    draft_api: SimpleNamespace,
    workers: list[Callable[[], None]],
) -> None:
    """An unchanged trace layout cannot preserve approval for new stackup inputs."""
    api = draft_api
    dialog, _persisted = _setup(api, workers, prior_results=True)
    before = dialog.session.analysis
    config = dialog.session.config
    changed = replace(config.stackup, thickness_mm="0.400")
    expected = _matching(api).analyze(
        replace(config, stackup=changed), dialog.session.snapshot
    )

    dialog.session.replace_calculation_context(changed, config.width_results)
    assert dialog._refresh_rows(dialog.session.snapshot)

    assert dialog.session.analysis == expected
    assert dialog.session.analysis.digest != before.digest
    assert not dialog.session.approved
    assert all(row[9] == "Pending" for row in dialog.sections.table_rows)
    current = _result(api, changed, config.specifications[0], "F.Cu", width_nm=250_000)
    dialog._calculation_cancel = Event()
    dialog._finish_calculation(
        dialog._calculation_generation,
        dialog._calculation_key(dialog.session.config, dialog.session.snapshot),
        (current,),
        "",
    )
    assert current in dialog.session.config.width_results
    assert all(row[9] == "0.25 mm" for row in dialog.sections.table_rows)
    assert not dialog.session.approved


@pytest.mark.parametrize("change", ["snapshot", "specification"])
def test_invalid_changed_inputs_cannot_reuse_valid_matching(
    draft_api: SimpleNamespace,
    change: str,
) -> None:
    """A previously valid cache cannot hide current board or intent failures."""
    api = draft_api
    config, board = _approved_board(api, prior_results=True)
    session = api.dialog.ReviewSession(config, board)
    session.refresh()
    if change == "snapshot":
        board = replace(board, net_class_error="Native class read failed")
    else:
        session.replace_specifications(
            (replace(config.specifications[0], target_ohms="invalid"),)
        )

    with pytest.raises(api.model.ValidationError):
        session.refresh(board)

    assert session.analysis is None
    assert not session.approved
    assert session.config.reviewed_digest == ""
