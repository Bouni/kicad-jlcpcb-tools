"""Retire the obsolete assignment table only after complete schematic saving."""

from contextlib import closing
from importlib import import_module
from pathlib import Path
import sqlite3
from typing import Any
from unittest.mock import patch

import pytest

from .native_window_support import _SelectableFootprint, modal_handler, window_ui
from .test_schematic_autosave_native import associated_schematic

__all__ = ["window_ui"]
pytestmark = pytest.mark.native_wx


def seed_project(ui: Any, variants: bool) -> Path:
    """Keep stale assignments and unrelated project data across real constructors."""
    if not variants:
        ui.board.names.clear()
        ui.board.current = ""
    path = ui.path / "jlcpcb" / "project.db"
    path.parent.mkdir(exist_ok=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE part_info (reference TEXT, lcsc TEXT)")
        connection.execute("INSERT INTO part_info VALUES ('R1', 'C999')")
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO metadata VALUES ('generation_count', '17')")
        connection.execute("CREATE TABLE other_project_data (payload BLOB)")
        connection.execute("INSERT INTO other_project_data VALUES (?)", (b"keep",))
    return path


def has_legacy(path: Path) -> bool:
    """Inspect persisted schema from a new connection, independently of the store."""
    with closing(sqlite3.connect(path)) as connection:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='part_info'"
            ).fetchone()
            is not None
        )


@pytest.mark.parametrize("variants", [False, True])
@pytest.mark.parametrize("assignment", ["C111", ""])
def test_complete_autosave_drops_only_legacy_table_and_reopen_does_not_recreate_it(
    window_ui: Any, variants: bool, assignment: str
) -> None:
    """Changing or clearing Default saves current native data before retiring SQL."""
    dbfile = seed_project(window_ui, variants)
    path = associated_schematic(window_ui)
    first = True

    def check(ui: Any) -> None:
        nonlocal first
        assert has_legacy(dbfile) is first
        # Both table modes must ignore the stale SQL C999 before it is deleted.
        if variants:
            assert ui.controller.session.snapshot.get("component-1", "").lcsc != "C999"
        else:
            assert ui.dialog.store.get_part("R1")["lcsc"] != "C999"
        if first:
            if variants:
                ui.board.parts[0].SetField("LCSC", assignment)
            else:
                assert ui.dialog._apply_lcsc_assignments({"R1": assignment}) == ["R1"]
        exporter = import_module(ui.mainwindow.__package__ + ".schematicexport")
        write = exporter.atomic_write_schematic

        def checked_write(target: str, content: str) -> None:
            assert has_legacy(dbfile) is first
            write(target, content)

        with patch.object(
            exporter, "atomic_write_schematic", side_effect=checked_write
        ):
            assert ui.dialog.Close() is True
        assert f'(property "LCSC" "{assignment}"' in path.read_text(encoding="utf-8")
        assert not has_legacy(dbfile)
        with closing(sqlite3.connect(dbfile)) as connection:
            assert connection.execute("SELECT * FROM metadata").fetchall() == [
                ("generation_count", "17")
            ]
            assert connection.execute(
                "SELECT * FROM other_project_data"
            ).fetchall() == [(b"keep",)]
        first = False

    window_ui.run(check, check)


@pytest.mark.parametrize("variants", [False, True])
@pytest.mark.parametrize(
    "outcome", ["no_target", "cancel", "discard", "write", "partial"]
)
def test_skipped_canceled_and_incomplete_saves_keep_legacy_assignments(
    window_ui: Any, variants: bool, outcome: str
) -> None:
    """A close decision or an earlier written sheet cannot authorize retirement."""
    dbfile = seed_project(window_ui, variants)
    path = associated_schematic(window_ui)
    original = path.read_bytes()
    safety = import_module(window_ui.mainwindow.__package__ + ".schematic_safety")
    lock = Path(safety.get_schematic_lock_path(str(path)))
    second = window_ui.path / "second.kicad_sch"
    if outcome == "no_target":
        path.unlink()
    elif outcome in ("cancel", "discard"):
        lock.write_text('{"username": "test", "hostname": "local"}')
    elif outcome == "partial":
        second.write_bytes(original)

    def check(ui: Any) -> None:
        exporter = import_module(ui.mainwindow.__package__ + ".schematicexport")
        write = exporter.atomic_write_schematic
        written = []

        def fail_write(target: str, content: str) -> None:
            assert has_legacy(dbfile)
            if outcome == "write" or (outcome == "partial" and written):
                raise OSError("injected schematic write failure")
            write(target, content)
            written.append(target)

        def answer(dialog: Any) -> None:
            result = ui.wx.ID_NO if outcome == "discard" else ui.wx.ID_CANCEL
            dialog.EndModal(result)

        targets = [] if outcome == "no_target" else [str(path)]
        if outcome == "partial":
            targets.append(str(second))
        try:
            with (
                patch.object(
                    ui.mainwindow, "resolve_project_schematics", return_value=targets
                ),
                patch.object(
                    exporter, "atomic_write_schematic", side_effect=fail_write
                ),
                modal_handler(ui, ui.wx.GenericMessageDialog, answer),
            ):
                assert ui.dialog.Close() is (outcome in ("no_target", "discard"))
            assert has_legacy(dbfile)
            with closing(sqlite3.connect(dbfile)) as connection:
                assert connection.execute("SELECT * FROM part_info").fetchall() == [
                    ("R1", "C999")
                ]
            if outcome == "partial":
                assert written == [str(path)]
                assert second.read_bytes() == original
        finally:
            # Fixture teardown closes the window; leave no further save to attempt.
            path.unlink(missing_ok=True)
            second.unlink(missing_ok=True)
            lock.unlink(missing_ok=True)

    window_ui.run(check)


@pytest.mark.parametrize("variants", [False, True])
@pytest.mark.parametrize("decision", ["retry", "close", "forced"])
def test_cleanup_failure_reports_saved_schematic_and_preserves_retry(
    window_ui: Any, variants: bool, decision: str
) -> None:
    """A locked SQL database cannot turn successful schematic saving into data loss."""
    dbfile = seed_project(window_ui, variants)
    path = associated_schematic(window_ui)

    def check(ui: Any) -> None:
        if variants:
            ui.board.parts[0].SetField("LCSC", "C222")
        else:
            assert ui.dialog._apply_lcsc_assignments({"R1": "C222"}) == ["R1"]

        def answer(dialog: Any) -> None:
            assert dialog.GetCaption() == "Legacy assignment cleanup failed"
            assert "The schematics were saved" in dialog.GetMessage()
            assert '(property "LCSC" "C222"' in path.read_text(encoding="utf-8")
            assert has_legacy(dbfile)
            dialog.EndModal(ui.wx.ID_NO if decision == "retry" else ui.wx.ID_YES)

        with closing(sqlite3.connect(dbfile)) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            if decision == "forced":
                with patch.object(ui.wx.GenericMessageDialog, "ShowModal") as show:
                    assert ui.dialog.Close(force=True) is True
                    show.assert_not_called()
            else:
                with modal_handler(ui, ui.wx.GenericMessageDialog, answer):
                    assert ui.dialog.Close() is (decision == "close")
            blocker.rollback()
        assert has_legacy(dbfile)
        if decision == "retry":
            assert ui.dialog.IsEnabled() and not ui.dialog._closing
            assert ui.dialog.Close() is True
            assert not has_legacy(dbfile)

    window_ui.run(check)


@pytest.mark.parametrize("variants", [False, True])
@pytest.mark.parametrize("ambiguous", ["invalid", "conflict"])
def test_ambiguous_native_fields_cannot_clear_schematic_or_retire_legacy_data(
    window_ui: Any, variants: bool, ambiguous: str
) -> None:
    """Only an intentional clear may erase an existing schematic assignment."""
    dbfile = seed_project(window_ui, variants)
    path = associated_schematic(window_ui)
    original = path.read_bytes()
    part = window_ui.board.parts[0]
    if ambiguous == "invalid":
        part.SetField("LCSC", "invalid")
    else:
        part.SetField("LCSC", "C111")
        part.SetField("JLC_PN", "C222")

    def check(ui: Any) -> None:
        def keep_open(dialog: Any) -> None:
            assert dialog.GetCaption() == "Schematic save failed"
            dialog.EndModal(ui.wx.ID_NO)

        try:
            with modal_handler(ui, ui.wx.GenericMessageDialog, keep_open):
                assert ui.dialog.Close() is False
            assert path.read_bytes() == original
            assert not path.with_name(path.name + "_old").exists()
            assert has_legacy(dbfile)
        finally:
            part.SetField("LCSC", "C333")
            if ambiguous == "conflict":
                part.SetField("JLC_PN", "C333")
        assert ui.dialog.Close() is True
        assert '(property "LCSC" "C333"' in path.read_text(encoding="utf-8")
        assert not has_legacy(dbfile)

    window_ui.run(check)


@pytest.mark.parametrize("variants", [False, True])
def test_pinless_symbol_assignment_is_saved_before_retirement(
    window_ui: Any, variants: bool
) -> None:
    """Pinless mounting-hole-style symbols still receive an absent part field."""
    dbfile = seed_project(window_ui, variants)
    path = associated_schematic(window_ui)
    text = path.read_text(encoding="utf-8")
    text = text.replace('    (pin "1"\n      (uuid "test-pin")\n    )\n', "")
    text = text.replace('    (property "LCSC" "C100"\n      (at 0 1 0)\n    )\n', "")
    path.write_text(text, encoding="utf-8")

    def check(ui: Any) -> None:
        assert ui.dialog.Close() is True
        assert '(property "LCSC" "C1"' in path.read_text(encoding="utf-8")
        assert not has_legacy(dbfile)

    window_ui.run(check)


@pytest.mark.parametrize("variants", [False, True])
def test_reused_sheet_conflicting_assignments_do_not_authorize_retirement(
    window_ui: Any, variants: bool
) -> None:
    """A shared symbol cannot silently save just one of its instance assignments."""
    dbfile = seed_project(window_ui, variants)
    second = _SelectableFootprint(window_ui.board, "component-2", "R2")
    second.SetField("LCSC", "C222")
    window_ui.board.parts.append(second)
    path = associated_schematic(window_ui)
    text = path.read_text(encoding="utf-8").replace(
        '      (project "board"\n',
        '      (project "board"\n        (path "/second" (reference "R2") (unit 1))\n',
    )
    path.write_text(text, encoding="utf-8")
    original = path.read_bytes()

    def check(ui: Any) -> None:
        def keep_open(dialog: Any) -> None:
            assert dialog.GetCaption() == "Schematic save failed"
            dialog.EndModal(ui.wx.ID_NO)

        try:
            with modal_handler(ui, ui.wx.GenericMessageDialog, keep_open):
                assert ui.dialog.Close() is False
            assert path.read_bytes() == original
            assert not path.with_name(path.name + "_old").exists()
            assert has_legacy(dbfile)
        finally:
            path.unlink()

    window_ui.run(check)
