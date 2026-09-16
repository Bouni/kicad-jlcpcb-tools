"""Complete LCSC actions with live board state, storage failures and item lifetimes."""

from collections.abc import Callable
from contextlib import closing
from pathlib import Path
import sqlite3
from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
import weakref

import pytest

from . import part_preferences_test_support as support
from .part_preferences_test_support import Footprint, act, board_rows, info_messages

mainwindow = support.mainwindow
make_window = support.make_window


@pytest.mark.parametrize("action", ["picker", "paste", "apply"])
def test_assignment_syncs_board_store_model_and_survives_window_recreation(
    action: str,
    make_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All assignment paths agree after reopening against the same unsaved board."""
    window = make_window()
    window.library.merge_lcsc_metadata(
        "C100", {"assembly_process": "SMT", "component_product_type": 2}
    )

    if action == "apply":
        window.library.save_part_preferences([("R_0603", "10k", "C200")])
        window.library.save_part_preferences.reset_mock()
    act(action, window, mainwindow, monkeypatch, "C200")

    fp = window.pcbnew.GetBoard().FindFootprintByReference("R1")
    assert fp.field.text == "C200"
    assert window.test_rows["R1"]["lcsc"] == "C200"
    assert window.store.read_all()[0]["stock"] is None
    assert window.test_rows["R1"]["stock"] == 27
    assert window.store.read_all()[0]["assembly_process"] == ""
    assert window.store.read_all()[0]["component_product_type"] is None
    reopened = mainwindow.Store(window, window.project_path, window.pcbnew.GetBoard())
    assert reopened.read_all()[0]["lcsc"] == "C200"
    window.start_assembly_enrichment.assert_called_once_with(["R1"])
    assert mainwindow.wx.PostEvent.called or window.recompute_bom_estimate.called
    if action == "apply":
        window.library.save_part_preferences.assert_not_called()
    else:
        window.library.save_part_preferences.assert_called_once_with(
            [("R_0603", "10k", "C200")]
        )


@pytest.mark.parametrize("action", ["picker", "paste", "apply", "open"])
def test_assignment_caches_missing_catalog_details_once_per_action(
    action: str,
    make_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catalog misses still assign every matching reference, without repeated reads."""
    window = make_window(
        footprints=[Footprint(lcsc=""), Footprint("R2", lcsc="")],
        part_preferences={("R_0603", "10k"): "C200"},
    )
    window.library.get_part_details.return_value = {}
    if action == "open":
        window.test_rows.clear()
        window.init_store()
    else:
        act(action, window, mainwindow, monkeypatch, "C200")
    window.library.get_part_details.assert_called_once_with("C200")
    assert [row["lcsc"] for row in board_rows(window)] == ["C200", "C200"]
    assert [row["lcsc"] for row in window.test_rows.values()] == ["C200", "C200"]
    assert all(
        fp.field.text == "C200" for fp in window.pcbnew.GetBoard().GetFootprints()
    )
    mainwindow.JLCPCBTools.populate_footprint_list(window)
    window.start_assembly_enrichment.assert_called_once()


@pytest.mark.parametrize("action", ["paste", "apply"])
def test_selected_deleted_footprint_is_skipped(
    action: str,
    make_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale selections must not mutate deleted footprints or prevent live assignments."""
    window = make_window(footprints=[Footprint(), Footprint("R2")])
    del window.pcbnew.GetBoard().footprints["R1"]

    if action == "apply":
        window.library.save_part_preferences([("R_0603", "10k", "C200")])
        window.library.save_part_preferences.reset_mock()
    act(action, window, mainwindow, monkeypatch, "C200")

    assert [(row["reference"], row["lcsc"]) for row in window.store.read_all()] == [
        ("R2", "C200")
    ]
    window.start_assembly_enrichment.assert_called_once_with(["R2"])


def _seed_enrichment(window: Any, mainwindow: Any) -> None:
    """Store reusable metadata for the currently assigned supplier codes."""
    for lcsc, process, product_type in (("C100", "SMT", 1), ("C200", "THT", 2)):
        window.library.merge_lcsc_metadata(
            lcsc,
            {"assembly_process": process, "component_product_type": product_type},
        )
    window.populate_footprint_list()
    window.populate_footprint_list.reset_mock()
    mainwindow.wx.PostEvent.reset_mock()


@pytest.mark.parametrize("action", ["picker", "paste", "apply", "clear"])
def test_later_native_failure_reports_partial_changes_and_refreshes_actual_board(
    make_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    """A failed second footprint cannot hide an earlier successful native edit."""
    window = make_window(
        footprints=[
            Footprint("R1", value="10k", lcsc="C100"),
            Footprint("R2", value="20k", lcsc="C200"),
        ]
    )
    library = window.library
    library.save_part_preferences(
        [("R_0603", "10k", "C777"), ("R_0603", "20k", "C888")]
    )
    _seed_enrichment(window, mainwindow)
    before_preferences = library.get_all_part_preferences()
    monkeypatch.setattr(
        window.pcbnew.GetBoard().FindFootprintByReference("R2"),
        "SetField",
        MagicMock(side_effect=RuntimeError("later assignment rejected")),
    )

    act(action, window, mainwindow, monkeypatch)

    first = "" if action == "clear" else "C777" if action == "apply" else "C999"
    assert [row["lcsc"] for row in board_rows(window)] == [first, "C200"]
    assert [window.test_rows[ref]["lcsc"] for ref in ("R1", "R2")] == [first, "C200"]
    assert [fp.field.text for fp in window.pcbnew.GetBoard().GetFootprints()] == [
        first,
        "C200",
    ]
    assert library.get_all_part_preferences() == before_preferences
    assert mainwindow.wx.PostEvent.called or window.recompute_bom_estimate.called
    assert info_messages(window) == []
    assert "later assignment rejected" in str(window.logger.warning.call_args)


def test_failed_automatic_remembering_keeps_committed_project_choice(
    make_window: Callable[..., Any], mainwindow: Any
) -> None:
    """Shared preference failure must not discard the successful explicit selection."""
    window = make_window(
        footprints=[Footprint("R1", value="10k"), Footprint("R2", value="20k")]
    )
    library = window.library
    _seed_enrichment(window, mainwindow)
    with (
        closing(sqlite3.connect(library.part_preferences_db_file)) as connection,
        connection,
    ):
        connection.execute(
            "CREATE TRIGGER reject_second_preference BEFORE INSERT ON mapping "
            "WHEN NEW.value = '20k' "
            "BEGIN SELECT RAISE(ABORT, 'later preference rejected'); END"
        )

    window.assign_parts(
        SimpleNamespace(references=["R1", "R2"], lcsc="C999", type="Basic", stock=99)
    )

    assert [(row["lcsc"], row["stock"]) for row in board_rows(window)] == [
        ("C999", None),
        ("C999", None),
    ]
    assert all(row["assembly_process"] == "" for row in board_rows(window))
    assert all(row["component_product_type"] is None for row in board_rows(window))
    assert all(
        fp.field.text == "C999" for fp in window.pcbnew.GetBoard().GetFootprints()
    )
    assert all(row["lcsc"] == "C999" for row in window.test_rows.values())
    assert library.get_all_part_preferences() == []
    window.start_assembly_enrichment.assert_called_once_with(["R1", "R2"])
    mainwindow.wx.PostEvent.assert_called_once()
    assert info_messages(window) == []
    assert "later preference rejected" in str(window.logger.warning.call_args)


@pytest.mark.parametrize("automatic", [False, True], ids=["manual-save", "picker"])
def test_success_log_counts_distinct_changed_preferences_and_skips_noop(
    make_window: Callable[..., Any], automatic: bool
) -> None:
    """Several references sharing a key are one changed preference in the log."""
    window = make_window(
        footprints=[
            Footprint("R1", value="10k", lcsc="C999"),
            Footprint("R2", value="10k", lcsc="C999"),
            Footprint("R3", value="20k", lcsc="C999"),
            Footprint("R4", value="30k", lcsc="C999"),
        ]
    )
    library = window.library
    library.save_part_preferences(
        [("R_0603", "10k", "C100"), ("R_0603", "20k", "C999")]
    )

    for _attempt in range(2):
        if automatic:
            window.assign_parts(
                SimpleNamespace(
                    references=["R1", "R2", "R3", "R4"],
                    lcsc="C999",
                    type="Basic",
                    stock=99,
                )
            )
        else:
            window.save_selected_part_preferences()

    assert library.get_all_part_preferences() == [
        ["R_0603", "10k", "C999"],
        ["R_0603", "20k", "C999"],
        ["R_0603", "30k", "C999"],
    ]
    messages = info_messages(window)
    assert len(messages) == 1
    assert messages[0].startswith("Saved 2 part preference(s).")
    if automatic:
        assert "Remember my part preferences" in messages[0]
        assert "Settings > Part preferences" in messages[0]


@pytest.mark.parametrize("action", ["picker", "paste", "apply"])
def test_optional_enrichment_read_failure_keeps_accepted_assignment_and_notifies(
    make_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    """Post-commit metadata lookup failure must not misreport or interrupt the choice."""
    window = make_window(
        footprints=[
            Footprint("R1", value="10k", lcsc="C100"),
            Footprint("R2", value="20k", lcsc="C200"),
        ]
    )
    library = window.library
    library.save_part_preferences(
        [("R_0603", "10k", "C777"), ("R_0603", "20k", "C888")]
    )
    _seed_enrichment(window, mainwindow)
    window.pending_assembly_enrichment = set()
    window.assembly_enrichment_generation = 0
    window.start_assembly_enrichment = MethodType(
        mainwindow.JLCPCBTools.start_assembly_enrichment, window
    )
    monkeypatch.setattr(
        window.store,
        "get_assembly_enrichment_targets",
        MagicMock(side_effect=sqlite3.OperationalError("metadata lookup unavailable")),
    )

    act(action, window, mainwindow, monkeypatch)

    expected = ["C999", "C999"] if action != "apply" else ["C777", "C888"]
    assert [row["lcsc"] for row in board_rows(window)] == expected
    assert [
        fp.field.text for fp in window.pcbnew.GetBoard().GetFootprints()
    ] == expected
    assert [window.test_rows[ref]["lcsc"] for ref in ("R1", "R2")] == expected
    assert library.get_all_part_preferences() == [
        ["R_0603", "10k", expected[0]],
        ["R_0603", "20k", expected[1]],
    ]
    mainwindow.wx.PostEvent.assert_called_once()
    warnings = [str(call) for call in window.logger.warning.call_args_list]
    assert len(warnings) == 1
    assert "enrichment" in warnings[0].lower()
    assert "metadata lookup unavailable" in warnings[0]
    assert "Unable to apply" not in warnings[0]
    assert window.pending_assembly_enrichment == set()
    assert window.assembly_enrichment_generation == 0
    window.partlist_data_model.set_enrichment_status.assert_not_called()


def test_manual_save_rolls_back_all_preferences_when_a_later_key_fails(
    make_window: Callable[..., Any],
) -> None:
    """One explicit save action must not leave half of its global changes saved."""
    window = make_window(
        footprints=[
            Footprint("R1", value="10k", lcsc="C100"),
            Footprint("R2", value="20k", lcsc="C200"),
        ]
    )
    with closing(sqlite3.connect(window.library.part_preferences_db_file)) as db, db:
        db.execute(
            "CREATE TRIGGER reject_second_preference BEFORE INSERT ON mapping "
            "WHEN NEW.value = '20k' "
            "BEGIN SELECT RAISE(ABORT, 'second preference rejected'); END"
        )

    window.save_selected_part_preferences()

    assert window.library.get_all_part_preferences() == []
    assert window.store.read_all()[0]["lcsc"] == "C100"
    assert window.store.read_all()[1]["lcsc"] == "C200"
    window.logger.warning.assert_called()

    assert info_messages(window) == []


def test_auto_fill_native_failure_keeps_window_usable_and_exposes_actual_changes(
    make_window: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opening displays successful earlier fills if a later footprint rejects its edit."""
    window = make_window(
        footprints=[
            Footprint("R1", value="10k", lcsc=""),
            Footprint("R2", value="20k", lcsc=""),
        ],
        part_preferences={("R_0603", "10k"): "C100", ("R_0603", "20k"): "C200"},
    )
    monkeypatch.setattr(
        window.pcbnew.GetBoard().FindFootprintByReference("R2"),
        "SetField",
        MagicMock(side_effect=RuntimeError("later assignment rejected")),
    )

    window.init_store()

    assert [row["lcsc"] for row in board_rows(window)] == ["C100", ""]
    assert [window.test_rows[ref]["lcsc"] for ref in ("R1", "R2")] == ["C100", ""]
    assert window.populate_footprint_list.called
    window.logger.warning.assert_called()
    assert info_messages(window) == []


def test_corrupt_project_database_does_not_block_board_reads_or_assignment(
    make_window: Callable[..., Any], mainwindow: Any
) -> None:
    """Project counter corruption is reported only when counter persistence is used."""
    window = make_window()
    project_database = Path(window.store.dbfile)
    project_database.parent.mkdir(parents=True, exist_ok=True)
    project_database.write_bytes(b"invalid SQLite project database")

    window.init_store()
    window.assign_parts(
        SimpleNamespace(references=["R1"], lcsc="C999", type="Basic", stock=99)
    )

    assert window.store.read_all()[0]["lcsc"] == "C999"
    assert window.test_rows["R1"]["lcsc"] == "C999"
    assert window.upper_toolbar.enabled.get(mainwindow.ID_SETTINGS, True) is True
    assert window.right_toolbar.Enable.call_args.args == (True,)
    assert project_database.read_bytes() == b"invalid SQLite project database"
    with pytest.raises(sqlite3.DatabaseError):
        window.store.get_generation_count()
    with pytest.raises(sqlite3.DatabaseError):
        window.store.increment_generation_count()
    assert window.store.read_all()[0]["lcsc"] == "C999"


def test_existing_project_assignments_are_ignored_and_preserved_during_edits(
    make_window: Callable[..., Any], mainwindow: Any
) -> None:
    """Obsolete project records and CSV cannot restore an explicitly cleared board field."""
    window = make_window(
        settings={"part_preferences": {"fill_empty_lcsc_assignments_on_open": False}}
    )
    project_database = Path(window.store.dbfile)
    project_database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(project_database)) as database, database:
        database.execute(
            "CREATE TABLE part_info (reference TEXT, lcsc TEXT, stock INTEGER)"
        )
        database.execute("INSERT INTO part_info VALUES ('R1', 'C777', 999)")
    database_before = project_database.read_bytes()
    csv_file = project_database.parent / "part_assignments.csv"
    csv_file.write_text("Reference,LCSC\nR1,C888\n")
    csv_before = csv_file.read_bytes()

    window.init_store()
    assert window.store.read_all()[0]["lcsc"] == "C100"
    window.remove_lcsc_number()
    window.init_store()
    reopened = mainwindow.Store(window, window.project_path, window.pcbnew.GetBoard())

    assert reopened.read_all()[0]["lcsc"] == ""
    assert window.test_rows["R1"]["lcsc"] == ""
    assert project_database.read_bytes() == database_before
    assert csv_file.read_bytes() == csv_before


def test_assignment_creates_hidden_field_and_preserves_other_metadata(
    make_window: Callable[..., Any],
) -> None:
    """A missing assignment field is created once and kept hidden through clearing."""
    footprint = Footprint(fields={"JLCPCB Rotation Offset": "90"})
    window = make_window(footprints=[footprint])

    window.assign_parts(
        SimpleNamespace(references=["R1"], lcsc="C999", type="Basic", stock=99)
    )

    assert footprint.field.text == "C999"
    assert footprint.field.visible is False
    assert footprint.fields["JLCPCB Rotation Offset"].text == "90"
    window.remove_lcsc_number()
    assert footprint.field.text == ""
    assert footprint.field.visible is False
    assert footprint.fields["JLCPCB Rotation Offset"].text == "90"


def test_alias_failure_after_first_field_change_refreshes_actual_assignment(
    make_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial edit within one footprint is visible even before a reference completes."""
    footprint = Footprint(fields={"LCSC": "C100", "JLCPCB": "C100"})
    window = make_window(footprints=[footprint])
    original_set_field = footprint.SetField

    def reject_second_alias(name: str, value: str) -> None:
        if name == "JLCPCB":
            raise RuntimeError("second alias rejected")
        original_set_field(name, value)

    monkeypatch.setattr(footprint, "SetField", reject_second_alias)
    window.assign_parts(
        SimpleNamespace(references=["R1"], lcsc="C999", type="Basic", stock=99)
    )

    assert footprint.field.text == "C999"
    assert footprint.fields["JLCPCB"].text == "C100"
    assert window.store.read_all()[0]["lcsc"] == "C999"
    assert window.test_rows["R1"]["lcsc"] == "C999"
    window.library.save_part_preferences.assert_not_called()
    assert "second alias rejected" in str(window.logger.warning.call_args)
    assert info_messages(window) == []
    assert mainwindow.wx.PostEvent.called or window.recompute_bom_estimate.called


def test_library_bootstrap_failure_disables_dependent_tools_until_recovery(
    make_window: Callable[..., Any], mainwindow: Any
) -> None:
    """Settings remains available when even the Library object cannot be created."""
    window = make_window()
    available_library = window.library
    window.library = None
    window._set_project_storage_error(OSError("library data path is not a directory"))
    for tool in (
        mainwindow.ID_GENERATE,
        mainwindow.ID_DOWNLOAD,
        mainwindow.ID_PART_PREFERENCES,
        mainwindow.ID_CORRECTIONS,
    ):
        assert window.upper_toolbar.enabled.get(tool, True) is False
    assert window.upper_toolbar.enabled.get(mainwindow.ID_SETTINGS, True) is True

    window.library = available_library
    window.init_store()
    assert all(window.upper_toolbar.enabled.values())


def test_clear_keeps_native_selection_array_alive_while_reading_references(
    make_window: Callable[..., Any],
) -> None:
    """Wx selection items borrow their native storage from the returned array."""

    class Selections:
        """Invalidate borrowed item handles when their native array is released."""

        def __iter__(self) -> Any:
            yield Item(self, "R1")
            yield Item(self, "R2")

    class Item:
        """Retain only a weak reference, as wx wrappers do for selection entries."""

        def __init__(self, owner: Selections, reference: str) -> None:
            self.owner = weakref.ref(owner)
            self.reference = reference

        def get_reference(self) -> str:
            """Refuse to dereference a native item whose owner was freed."""
            assert self.owner() is not None, "Native selection array was released"
            return self.reference

    window = make_window(footprints=[Footprint("R1"), Footprint("R2")])
    window.footprint_list.GetSelections.side_effect = Selections
    window.partlist_data_model.get_reference.side_effect = (
        lambda item: item.get_reference()
    )
    window.remove_lcsc_number()
    assert all(part["lcsc"] == "" for part in window.store.read_all())
    assert all(fp.field.text == "" for fp in window.pcbnew.GetBoard().GetFootprints())
    assert all(part["lcsc"] == "" for part in window.test_rows.values())


def test_storage_recovery_restarts_invalidated_pending_enrichment(
    make_window: Callable[..., Any], mainwindow: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery must reschedule metadata whose old worker results are now stale."""
    window = make_window()
    window.pending_assembly_enrichment = {"C100"}
    window.assembly_enrichment_generation = 1
    thread = MagicMock()
    monkeypatch.setattr(mainwindow, "Thread", thread)
    window.start_assembly_enrichment = (
        mainwindow.JLCPCBTools.start_assembly_enrichment.__get__(window)
    )
    window._set_project_storage_error(sqlite3.OperationalError("database is locked"))
    assert window.pending_assembly_enrichment == set()
    window.init_store()
    thread.assert_called_once()
    assert thread.call_args.kwargs["args"][0] == ["C100"]
    assert window.assembly_enrichment_generation > 1
    before = window.store.read_all()[0]
    window.on_assembly_enrichment_progress(
        SimpleNamespace(
            generation=1,
            lcsc="C100",
            metadata={"assembly_process": "SMT", "component_product_type": 1},
        )
    )
    assert window.store.read_all()[0] == before


@pytest.mark.parametrize(
    "fields",
    [
        {"JLCPCB": "C100"},
        {"LCSC": "C100", "JLCPCB": "C101"},
        {"LCSC": "", "JLCPCB": "C101"},
    ],
)
def test_explicit_assignment_and_clear_keep_all_aliases_consistent(
    make_window: Callable[..., Any], fields: dict[str, str]
) -> None:
    """C-number assignments can be changed and cleared without leaving a hidden alias."""
    footprint = Footprint(fields=fields)
    window = make_window(footprints=[footprint])
    window.settings["part_preferences"] = {"fill_empty_lcsc_assignments_on_open": False}

    for lcsc in ("C123", "C456"):
        window.assign_parts(
            SimpleNamespace(references=["R1"], lcsc=lcsc, type="Basic", stock=27)
        )
        assert {
            name: field.text for name, field in footprint.fields.items()
        } == dict.fromkeys(fields, lcsc)
        assert window.store.read_all()[0]["lcsc"] == lcsc

    window.remove_lcsc_number()
    assert {
        name: field.text for name, field in footprint.fields.items()
    } == dict.fromkeys(fields, "")
    window.init_store()
    reopened = make_window(board=window.pcbnew.GetBoard(), settings=window.settings)
    reopened.init_store()
    assert reopened.store.read_all()[0]["lcsc"] == ""
    assert window.test_rows["R1"]["lcsc"] == ""


@pytest.mark.parametrize(
    "alias", ["LCSC", "JLC_PN", "LCSC P/N", "JLCPCB Part #", "LCSC custom code"]
)
def test_assignment_preserves_other_supplier_fields_when_assigning_and_clearing(
    make_window: Callable[..., Any], alias: str
) -> None:
    """Explicit part choices must not corrupt placement offsets or supplier URLs."""
    metadata = {
        "JLCPCB Rotation Offset": "90",
        "JLCPCB Position Offset": "0, 0.2",
        "JLCPCB Layer Override": "bottom",
        "LCSC URL": "https://example.test/component",
        "JLCPCB empty metadata": "",
    }
    footprint = Footprint(fields={alias: "C100", **metadata})
    window = make_window(
        footprints=[footprint],
        settings={"part_preferences": {"fill_empty_lcsc_assignments_on_open": False}},
    )
    for lcsc in ("C123", "C456"):
        window.assign_parts(
            SimpleNamespace(references=["R1"], lcsc=lcsc, type="Basic", stock=27)
        )
        assert {name: field.text for name, field in footprint.fields.items()} == {
            alias: lcsc,
            **metadata,
        }
        window = make_window(board=window.pcbnew.GetBoard(), settings=window.settings)
        window.init_store()
        assert window.store.read_all()[0]["lcsc"] == lcsc
    window.remove_lcsc_number()
    assert {name: field.text for name, field in footprint.fields.items()} == {
        alias: "",
        **metadata,
    }
    window.init_store()
    assert window.store.read_all()[0]["lcsc"] == ""
