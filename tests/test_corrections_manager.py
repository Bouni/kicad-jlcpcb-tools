"""Exercise part-number rules through the real Corrections Manager constructor."""

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.correction_test_support import make_library, raw_rows, seed_raw
from tests.test_corrections_import_export import (
    field_values,
    install_manager_controls,
    manager,
    select,
    set_inputs,
)
from tests.test_library_lcsc_corrections import execute, lcsc_rows
from tests.wx_harness import load_correction_modules


@pytest.fixture
def modules() -> Iterator[SimpleNamespace]:
    """Keep production siblings under one package for the full test lifecycle."""
    with load_correction_modules() as loaded:
        yield loaded


@pytest.fixture
def setup(
    modules: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[SimpleNamespace, Any, Any]:
    """Provide real production imports, real SQLite, and captured controls."""
    library = make_library(modules.library, tmp_path)
    return modules, library, manager(modules, library, monkeypatch)


def kinds(dialog: Any) -> list[str]:
    """Read the Kind column as displayed."""
    return [row[-1] for row in dialog.corrections_list.rows]


@pytest.mark.parametrize(
    ("footprint", "lcsc_part", "text", "ticked"),
    [("^SOT-23", "", "^SOT-23", False), ("", "c12345", "c12345", True)],
)
def test_constructor_prefills_the_form_for_the_requested_kind(
    modules: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    footprint: str,
    lcsc_part: str,
    text: str,
    ticked: bool,
) -> None:
    """The parts list opens the manager either on a pattern or on a part number."""
    library = make_library(modules.library, tmp_path)
    install_manager_controls(modules, library, monkeypatch)
    parent = SimpleNamespace(library=library, scale_factor=1, window=object())
    dialog = modules.corrections.CorrectionManagerDialog(
        parent, footprint, lcsc_part=lcsc_part
    )
    assert dialog.regex.GetValue() == text
    assert dialog.lcsc_mode.GetValue() is ticked
    assert dialog.corrections_list.columns[-1] == "Kind"


def test_save_routes_each_kind_to_its_own_table(
    setup: tuple[SimpleNamespace, Any, Any],
) -> None:
    """The checkbox decides which table a rule goes to, and the list says which."""
    modules, library, dialog = setup
    set_inputs(dialog, pattern="^SOT-23", rotation="180")
    assert dialog.save_correction() is True
    assert [row[1] for row in raw_rows(library)] == ["^SOT-23"]
    assert lcsc_rows(library) == []

    dialog.lcsc_mode.SetValue(True)
    set_inputs(dialog, pattern="c12345", rotation="90")
    assert dialog.save_correction() is True

    assert lcsc_rows(library) == [(1, "C12345", 90, 0.0, 0.0)]
    assert [row[1] for row in raw_rows(library)] == ["^SOT-23"]
    assert kinds(dialog) == ["Footprint", "LCSC"]
    assert dialog.selected_record.kind == "lcsc"
    assert field_values(dialog) == ("C12345", "90", "0.0", "0.0")
    assert modules.wx.PostEvent.call_count == 2


@pytest.mark.parametrize("key", ["^C12345$", "C12345|C999", "SOT-23", "C", ""])
def test_invalid_part_number_is_refused_and_inputs_kept(
    setup: tuple[SimpleNamespace, Any, Any], key: str
) -> None:
    """A part rule that is not a bare part number would silently never fire."""
    modules, library, dialog = setup
    dialog.lcsc_mode.SetValue(True)
    set_inputs(dialog, pattern=key, rotation="90")
    inputs = field_values(dialog)

    assert dialog.save_correction() is False

    assert lcsc_rows(library) == []
    assert raw_rows(library) == []
    assert field_values(dialog) == inputs
    assert dialog.lcsc_mode.GetValue() is True
    assert "C12345" in str(modules.wx.MessageBox.call_args)
    modules.wx.PostEvent.assert_not_called()


def test_switching_kind_leaves_the_selected_rule_alone(
    setup: tuple[SimpleNamespace, Any, Any],
) -> None:
    """Editing the key moves a rule; changing its kind describes a new one.

    One checkbox click must not silently delete an unrelated pattern rule.
    """
    modules, library, dialog = setup
    library.insert_correction_data("^SOT-23", 180, (0, 0))
    dialog.populate_corrections_list()
    select(dialog, 0)
    dialog.lcsc_mode.SetValue(True)
    dialog.regex.SetValue("C12345")

    assert dialog.save_correction() is True

    assert raw_rows(library) == [(1, "^SOT-23", 180, 0.0, 0.0)]
    assert lcsc_rows(library) == [(1, "C12345", 180, 0.0, 0.0)]
    assert (dialog.selected_record.kind, dialog.selected_record.rowid) == ("lcsc", 1)
    modules.wx.PostEvent.assert_called_once()


def test_checkbox_only_change_survives_a_list_refresh(
    setup: tuple[SimpleNamespace, Any, Any],
) -> None:
    """A changed kind is an unsaved edit and survives a list refresh like changed text.

    Ticking the LCSC checkbox with no text edit must not be silently reverted
    when some unrelated event triggers ``populate_corrections_list``.
    """
    _modules, library, dialog = setup
    library.insert_correction_data("^SOT-23", 180, (0, 0))
    dialog.populate_corrections_list()
    select(dialog, 0)
    entered = field_values(dialog)
    dialog.lcsc_mode.SetValue(True)

    dialog.populate_corrections_list()

    assert dialog.lcsc_mode.GetValue() is True
    assert field_values(dialog) == entered


def test_editing_the_key_within_one_kind_still_moves_the_rule(
    setup: tuple[SimpleNamespace, Any, Any],
) -> None:
    """Renaming a part rule repairs that row rather than adding a second one."""
    _modules, library, dialog = setup
    library.insert_lcsc_correction_data("C12345", 90, (0, 0))
    dialog.populate_corrections_list()
    select(dialog, 0)
    assert dialog.lcsc_mode.GetValue() is True
    dialog.regex.SetValue("C99999")
    dialog.rotation.SetValue("180")

    assert dialog.save_correction() is True

    assert lcsc_rows(library) == [(1, "C99999", 180, 0.0, 0.0)]


def test_a_matching_key_of_the_other_kind_is_not_a_conflict(
    setup: tuple[SimpleNamespace, Any, Any],
) -> None:
    """A pattern rule spelled C12345 and a part rule for C12345 are independent."""
    modules, library, dialog = setup
    library.insert_correction_data("C12345", 180, (0, 0))
    dialog.populate_corrections_list()
    dialog.lcsc_mode.SetValue(True)
    set_inputs(dialog, pattern="C12345", rotation="90")

    assert dialog.save_correction() is True

    modules.wx.MessageDialog.assert_not_called()
    assert raw_rows(library) == [(1, "C12345", 180, 0.0, 0.0)]
    assert lcsc_rows(library) == [(1, "C12345", 90, 0.0, 0.0)]
    assert kinds(dialog) == ["Footprint", "LCSC"]


def test_identical_existing_part_rule_selects_without_writing(
    setup: tuple[SimpleNamespace, Any, Any],
) -> None:
    """Entering a rule that already exists, however spelled, just selects it."""
    modules, library, dialog = setup
    library.insert_lcsc_correction_data("C12345", 90, (0, 0))
    dialog.populate_corrections_list()
    dialog.lcsc_mode.SetValue(True)
    set_inputs(dialog, pattern=" c12345 ", rotation="90")

    assert dialog.save_correction() is True

    assert lcsc_rows(library) == [(1, "C12345", 90, 0.0, 0.0)]
    assert (dialog.selected_record.kind, dialog.selected_record.rowid) == ("lcsc", 1)
    modules.wx.PostEvent.assert_not_called()


def test_replacing_a_part_rule_by_another_spelling_needs_confirmation(
    setup: tuple[SimpleNamespace, Any, Any],
) -> None:
    """Different values for one part number are a replacement the user confirms."""
    modules, library, dialog = setup
    library.insert_lcsc_correction_data("C12345", 90, (0, 0))
    dialog.populate_corrections_list()
    dialog.lcsc_mode.SetValue(True)
    set_inputs(dialog, pattern="c12345", rotation="180")

    assert dialog.save_correction() is False
    assert lcsc_rows(library) == [(1, "C12345", 90, 0.0, 0.0)]
    assert modules.wx.MessageDialog.call_args.args[2] == "Part number exists!"

    modules.wx.MessageDialog.return_value.ShowModal.return_value = modules.wx.ID_YES
    assert dialog.save_correction() is True
    # The replaced row is deleted first, so SQLite hands out its rowid again.
    assert lcsc_rows(library) == [(1, "C12345", 180, 0.0, 0.0)]


def test_delete_targets_the_selected_kind(
    setup: tuple[SimpleNamespace, Any, Any],
) -> None:
    """Row 1 of one table is not row 1 of the other."""
    _modules, library, dialog = setup
    library.insert_correction_data("R1", 90, (0, 0))
    library.insert_lcsc_correction_data("C1", 180, (0, 0))
    dialog.populate_corrections_list()
    assert kinds(dialog) == ["Footprint", "LCSC"]

    select(dialog, 1)
    assert dialog.lcsc_mode.GetValue() is True
    assert dialog.delete_correction() is True
    assert lcsc_rows(library) == []
    assert raw_rows(library) == [(1, "R1", 90, 0.0, 0.0)]

    select(dialog, 0)
    assert dialog.lcsc_mode.GetValue() is False
    assert dialog.delete_correction() is True
    assert raw_rows(library) == []


def test_invalid_part_rule_is_listed_for_repair(
    modules: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed part rule is shown with its kind and repaired in place."""
    library = make_library(modules.library, tmp_path)
    execute(
        library.correctionsdb_file,
        "INSERT INTO lcsc_correction VALUES ('C1', '47u', 0, 0)",
    )
    dialog = manager(modules, library, monkeypatch)
    (row,) = dialog.corrections_list.rows
    assert row[0] == "C1"
    assert "rotation" in row[4]
    assert row[5] == "LCSC"
    assert "need repair" in dialog.correction_status.value

    select(dialog, 0)
    dialog.rotation.SetValue("90")
    assert dialog.save_correction() is True
    assert lcsc_rows(library) == [(1, "C1", 90, 0.0, 0.0)]
    assert "need repair" not in dialog.correction_status.value


def test_export_writes_pattern_rules_only(
    setup: tuple[SimpleNamespace, Any, Any], tmp_path: Path
) -> None:
    """The CSV format has no part-number field, so part rules stay out of it."""
    _modules, library, dialog = setup
    library.insert_correction_data("^SOT-23", 180, (0, 0))
    library.insert_lcsc_correction_data("C12345", 90, (0, 0))
    path = tmp_path / "export.csv"

    assert dialog._export_corrections(path) is True

    assert path.read_text().splitlines() == [
        '"Pattern","Rotation","Offset X","Offset Y"',
        '"^SOT-23","180","0.0","0.0"',
    ]


def test_seeded_rows_of_both_kinds_survive_a_scope_switch(
    setup: tuple[SimpleNamespace, Any, Any],
) -> None:
    """Switching to a project database through the manager carries both kinds."""
    modules, library, dialog = setup
    seed_raw(library, [("^SOT-23", 180, 0, 0)])
    library.insert_lcsc_correction_data("C12345", 90, (0, 0))
    modules.wx.MessageDialog.return_value.ShowModal.return_value = modules.wx.ID_YES

    assert dialog.on_global_corrections_changed() is True

    assert library.correctionsdb_file == library.localcorrectionsdb_file
    assert kinds(dialog) == ["Footprint", "LCSC"]
    assert lcsc_rows(library) == [(1, "C12345", 90, 0.0, 0.0)]


def test_replacement_prompt_names_kinds_and_does_not_promise_a_kind_switch(
    setup: tuple[SimpleNamespace, Any, Any],
) -> None:
    """With a pattern row selected, a part rule save is an insert, and says so."""
    modules, library, dialog = setup
    library.insert_correction_data("^SOT-23", 180, (0, 0))
    library.insert_lcsc_correction_data("C12345", 90, (0, 0))
    dialog.populate_corrections_list()
    select(dialog, 0)
    dialog.lcsc_mode.SetValue(True)
    set_inputs(dialog, pattern="C12345", rotation="45")

    assert dialog.save_correction() is False

    prompt = modules.wx.MessageDialog.return_value.ExtendedMessage
    assert "Row 1 (LCSC): 90°, 0.0/0.0" in prompt
    assert "will become" not in prompt
    assert modules.wx.MessageDialog.call_args.args[2] == "Part number exists!"
    assert raw_rows(library) == [(1, "^SOT-23", 180, 0.0, 0.0)]
    assert lcsc_rows(library) == [(1, "C12345", 90, 0.0, 0.0)]


def test_replacement_prompt_names_the_edited_row_of_the_same_kind(
    setup: tuple[SimpleNamespace, Any, Any],
) -> None:
    """Renaming a part rule onto another's key says which row is being edited."""
    modules, library, dialog = setup
    library.insert_lcsc_correction_data("C1", 90, (0, 0))
    library.insert_lcsc_correction_data("C2", 180, (0, 0))
    dialog.populate_corrections_list()
    select(dialog, 1)
    dialog.regex.SetValue("C1")

    assert dialog.save_correction() is False

    prompt = modules.wx.MessageDialog.return_value.ExtendedMessage
    assert "Row 1 (LCSC): 90°, 0.0/0.0" in prompt
    assert "The selected row 2 (LCSC) will become 'C1'." in prompt


def test_export_reports_the_part_rules_it_leaves_out(
    setup: tuple[SimpleNamespace, Any, Any], tmp_path: Path
) -> None:
    """A backup that silently lost part rules would surprise on re-import."""
    modules, library, dialog = setup
    library.insert_correction_data("^SOT-23", 180, (0, 0))
    assert dialog._export_corrections(tmp_path / "patterns.csv") is True
    modules.wx.MessageBox.assert_not_called()

    library.insert_lcsc_correction_data("C1", 90, (0, 0))
    library.insert_lcsc_correction_data("C2", 90, (0, 0))
    assert dialog._export_corrections(tmp_path / "mixed.csv") is True

    modules.wx.MessageBox.assert_called_once()
    message = modules.wx.MessageBox.call_args.args[0]
    assert message.startswith("2 LCSC part-number rule")
    assert "pattern rules only" in message
