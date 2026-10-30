"""Persisted capture selection must use the same approval rule as the workbook."""

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_impedance_automatic_rows import _show
from tests.test_impedance_save_evidence import (
    _approve_child,
    _save,
    constructor_api,  # noqa: F401 -- fixture dependency
    draft_api,  # noqa: F401 -- fixture dependency
    save_api as _save_api,
)

save_api = _save_api


def test_approved_capture_subset_survives_save_reopen_and_html_export(
    save_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unchecking a previously approved capture must not mislabel the report."""
    env = save_api
    api = env.api
    package = api.dialog.__package__
    service = importlib.import_module(f"{package}.service")
    html = importlib.import_module(f"{package}.html_report")
    database_api = importlib.import_module(f"{package}.database")
    repository_api = importlib.import_module(f"{package}.repository")
    dialog = _approve_child(env, monkeypatch, count=2)
    _show(api, dialog)
    history = dialog.session.config.review_tracking
    omitted = dialog._row_ids[0]
    dialog.sections._on_cell_click(
        SimpleNamespace(GetRow=lambda: 0, GetCol=lambda: 0, Skip=lambda: None)
    )
    assert omitted not in dialog.session.included
    api.drain()
    api.paint(dialog.preview_pane)
    dialog.approve_button.emit(api.wx.EVT_BUTTON)
    assert dialog.session.approved, dialog.status.GetValue()
    _save(env, dialog)
    saved = env.state.saved[-1]
    assert saved.reviewed_digest
    assert saved.review_tracking.layers == history.layers

    board_path = tmp_path / "board.kicad_pcb"
    board_path.write_text("(kicad_pcb)\n", encoding="utf-8")
    database = database_api.ImpedanceDatabase(tmp_path / "project.db")
    board_id = database.resolve_board(board_path)
    repository = repository_api.ImpedanceRepository(database, board_id)
    repository.save(saved, 0)
    reopened_database = database_api.ImpedanceDatabase(database.path)
    restored, _revision = repository_api.ImpedanceRepository(
        reopened_database, board_id
    ).load()
    reopened = env.main(restored)
    assert reopened.session.approved
    _show(api, reopened)

    for config in (restored, dialog.session.config):
        plan = service.prepare(config, env.state.snapshot)
        assert plan is not None and len(plan.sections) == 1
        rows = service.report_rows(plan, (api.capture,))
        assert rows[0].layer_approval_current
        assert rows[0].layer_approved_at_utc == history.layers[0].approved_at_utc
        path = html.write_html_report(rows, tmp_path / "subset.html")
        assert "Previous layer settings or captures" not in path.read_text(
            encoding="utf-8"
        )

    # Reincluding the already-reviewed sibling needs no new layer consent.
    reopened.on_screen = True
    reopened_row = reopened._row_ids.index(omitted)
    reopened.sections._on_cell_click(
        SimpleNamespace(
            GetRow=lambda: reopened_row, GetCol=lambda: 0, Skip=lambda: None
        )
    )
    api.drain()
    api.paint(reopened.preview_pane)
    reopened.approve_button.emit(api.wx.EVT_BUTTON)
    _save(env, reopened)
    plan = service.prepare(env.state.saved[-1], env.state.snapshot)
    assert plan is not None and len(plan.sections) == 2
    assert all(
        row.layer_approval_current
        for row in service.report_rows(plan, (api.capture,) * 2)
    )
