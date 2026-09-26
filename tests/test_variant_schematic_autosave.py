"""Export Default assignments independently from fabrication variant selection."""

from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from .native_window_support import choose_output, window_ui

__all__ = ["window_ui"]
pytestmark = pytest.mark.native_wx


def _schematic(ui: Any) -> Path:
    """Create a schematic with distinct Default and named-variant assignments."""
    path = ui.path / "autosave.kicad_sch"
    path.write_text(
        """(kicad_sch
  (symbol
    (lib_id "Device:R")
    (property "Reference" "R1"
      (at 0 0 0)
    )
    (property "LCSC" "C100"
      (at 0 1 0)
    )
    (pin "1"
      (uuid "test-pin")
    )
    (instances
      (project "board"
        (path "/first"
          (reference "R1")
          (unit 1)
          (variant (name "A")
            (field (name "LCSC") (value "C999"))
          )
        )
      )
    )
  )
)
""",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("selected", ["", "A", "B"])
def test_schematic_autosave_uses_current_default_and_preserves_named_output(
    window_ui: Any, selected: str
) -> None:
    """Read fresh native Default fields without changing the output preference."""

    def check(ui: Any) -> None:
        controller = ui.controller
        choose_output(ui, selected)
        ui.board.parts[0].AddVariant("A").SetFieldValue("LCSC", "C222")
        ui.board.parts[0].AddVariant("B").SetFieldValue("LCSC", "C333")
        ui.board.parts[0].SetField("LCSC", "C111")
        before = controller.session.adapter.snapshot()
        remembered = controller.cache.get_output_variant()
        path = _schematic(ui)
        try:
            controller.export_to_schematic([str(path)])
            written = path.read_text(encoding="utf-8")
            assert '(property "LCSC" "C111"' in written
            assert '(field (name "LCSC") (value "C999"))' in written
            assert controller.session.snapshot.get("component-1", "").lcsc == "C111"
            assert controller.session.adapter.snapshot() == before
            assert controller.session.output_variant == selected
            assert controller.cache.get_output_variant() == remembered
            assert controller.output_choice.GetStringSelection() == (
                controller.variant_label(selected)
            )
            assert not ui.messages
        finally:
            path.unlink()

    window_ui.run(check)


@pytest.mark.parametrize("failure", [OSError, ValueError])
def test_schematic_autosave_export_errors_reach_close_handler(
    window_ui: Any, failure: type[Exception]
) -> None:
    """A failed write cannot be mistaken for success by the parent close handler."""

    def check(ui: Any) -> None:
        choose_output(ui, "B")
        module = import_module(
            type(ui.controller).__module__.rsplit(".", 2)[0] + ".schematicexport"
        )
        error = failure("schematic save failed")
        with (
            patch.object(module.SchematicExport, "load_schematic", side_effect=error),
            pytest.raises(failure, match="schematic save failed") as raised,
        ):
            ui.controller.export_to_schematic([str(ui.path / "target.kicad_sch")])
        assert raised.value is error
        assert ui.controller.session.output_variant == "B"
        assert ui.controller.session.reliable and not ui.controller.closed
        assert not ui.messages

    window_ui.run(check)


def test_schematic_autosave_lock_reaches_parent_and_accepts_explicit_approval(
    window_ui: Any,
) -> None:
    """The controller neither bypasses a lock nor consumes its approval prompt."""

    def check(ui: Any) -> None:
        choose_output(ui, "A")
        package = type(ui.controller).__module__.rsplit(".", 2)[0]
        safety = import_module(package + ".schematic_safety")
        path = _schematic(ui)
        lock = Path(safety.get_schematic_lock_path(str(path)))
        lock.write_text('{"username": "test", "hostname": "local"}')
        original = path.read_bytes()
        ui.board.parts[0].SetField("LCSC", "C111")
        try:
            with pytest.raises(safety.SchematicLockedError):
                ui.controller.export_to_schematic([str(path)])
            assert path.read_bytes() == original
            assert not path.with_name(path.name + "_old").exists()
            ui.controller.timer.Notify()
            model = ui.controller.model
            assert (
                model.get_value(
                    model.row_for_component("component-1"),
                    model.column_for("", "lcsc"),
                )
                == "C111"
            )
            ui.controller.export_to_schematic([str(path)], approved_locks=[str(path)])
            assert '(property "LCSC" "C111"' in path.read_text(encoding="utf-8")
            assert ui.controller.session.output_variant == "A"
            assert not ui.messages
        finally:
            lock.unlink()
            path.unlink()

    window_ui.run(check)


@pytest.mark.parametrize("change", ["save_as", "replacement", "read_failure"])
def test_schematic_autosave_refuses_unreliable_board_before_writing(
    window_ui: Any, change: str
) -> None:
    """Board identity or native-read failure propagates before any file is changed."""

    def check(ui: Any) -> None:
        controller = ui.controller
        path = _schematic(ui)
        original = path.read_bytes()
        if change == "save_as":
            owner, attribute = ui.board, "GetFileName"
            replacement = lambda: str(ui.path / "renamed.kicad_pcb")
        elif change == "replacement":
            from .variant_native_support import Board

            owner, attribute, replacement = ui.dialog.pcbnew, "board", Board()
        else:
            owner, attribute, replacement = controller.session.adapter, "snapshot", None
        arguments = (
            {"side_effect": RuntimeError("native read failed")}
            if change == "read_failure"
            else {"new": replacement}
        )
        try:
            with (
                patch.object(owner, attribute, **arguments),
                pytest.raises(RuntimeError, match="Reopen|native read failed"),
            ):
                controller.export_to_schematic([str(path)])
            assert path.read_bytes() == original
            assert not path.with_name(path.name + "_old").exists()
            assert not controller.session.reliable
            assert not controller.view._mutations_enabled
            assert not ui.dialog.generate_button.IsEnabled()
            assert not ui.messages
        finally:
            path.unlink()
            controller.refresh()

    window_ui.run(check)


def test_variant_refresh_waits_until_close_save_prompt_returns(window_ui: Any) -> None:
    """Timer delivery cannot change the displayed snapshot inside a save prompt."""

    def check(ui: Any) -> None:
        controller = ui.controller
        before = controller.session.snapshot
        ui.board.parts[0].SetField("LCSC", "C111")
        ui.dialog._saving_on_close = True
        try:
            controller.timer.Notify()
            assert controller.session.snapshot == before
        finally:
            ui.dialog._saving_on_close = False
        controller.timer.Notify()
        assert controller.session.snapshot.get("component-1", "").lcsc == "C111"
        assert controller.timer.IsRunning() and not ui.messages

    window_ui.run(check)
