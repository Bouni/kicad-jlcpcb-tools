"""Complete LCSC assignment actions, durable failure recovery and native lifetimes."""

from collections.abc import Callable
from contextlib import closing
from copy import deepcopy
import sqlite3
from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
import weakref

import pytest

from . import part_preferences_test_support as support
from .part_preferences_test_support import (
    Footprint,
    act,
    info_messages,
    project_rows,
    reject_second_project_update,
)

mainwindow = support.mainwindow
make_window = support.make_window


@pytest.mark.parametrize("action", ["picker", "paste", "apply"])
def test_assignment_syncs_board_store_model_and_survives_reopen(
    action: str,
    make_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All assignment paths must agree and survive normal board precedence on reopen."""
    window = make_window()
    window.store.set_assembly_metadata("R1", "SMT", 2, expected_lcsc="C100")

    if action == "apply":
        window.library.save_part_preferences([("R_0603", "10k", "C200")])
        window.library.save_part_preferences.reset_mock()
    act(action, window, mainwindow, monkeypatch, "C200")

    fp = window.pcbnew.GetBoard().FindFootprintByReference("R1")
    assert fp.field.text == "C200"
    assert window.test_rows["R1"]["lcsc"] == "C200"
    assert window.store.get_part("R1")["stock"] == 27
    assert window.store.get_part("R1")["assembly_process"] == ""
    assert window.store.get_part("R1")["component_product_type"] is None
    reopened = mainwindow.Store(window, window.project_path, window.pcbnew.GetBoard())
    assert reopened.get_part("R1")["lcsc"] == "C200"
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
    assert [row["lcsc"] for row in project_rows(window)] == ["C200", "C200"]
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

    assert window.store.get_part("R1")["lcsc"] == "C100"
    assert window.store.get_part("R2")["lcsc"] == "C200"
    window.start_assembly_enrichment.assert_called_once_with(["R2"])


def _seed_enrichment(window: Any) -> None:
    """Give every representation observable values that a failed action must retain."""
    with closing(sqlite3.connect(window.store.dbfile)) as connection, connection:
        connection.executemany(
            "UPDATE part_info SET stock = ?, assembly_process = ?, "
            "component_product_type = ? WHERE reference = ?",
            [(11, "SMT", 1, "R1"), (22, "THT", 2, "R2")],
        )
    window.populate_footprint_list()
    window.populate_footprint_list.reset_mock()


@pytest.mark.parametrize("action", ["picker", "paste", "apply", "clear"])
@pytest.mark.parametrize("column", ["lcsc", "stock"])
def test_later_project_failure_preserves_entire_action_and_emits_no_success(
    make_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    column: str,
) -> None:
    """A selection is one transaction, including distinct manual Apply groups."""
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
    _seed_enrichment(window)
    before_database = project_rows(window)
    before_model = deepcopy(window.test_rows)
    before_preferences = library.get_all_part_preferences()
    reject_second_project_update(window, column)

    act(action, window, mainwindow, monkeypatch)

    assert project_rows(window) == before_database
    assert window.test_rows == before_model
    assert [fp.field.text for fp in window.pcbnew.GetBoard().GetFootprints()] == [
        "C100",
        "C200",
    ]
    assert library.get_all_part_preferences() == before_preferences
    window.partlist_data_model.set_lcsc.assert_not_called()
    window.partlist_data_model.remove_lcsc_number.assert_not_called()
    window.start_assembly_enrichment.assert_not_called()
    mainwindow.wx.PostEvent.assert_not_called()
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
    _seed_enrichment(window)
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

    assert [(row["lcsc"], row["stock"]) for row in project_rows(window)] == [
        ("C999", 99),
        ("C999", 99),
    ]
    assert all(row["assembly_process"] == "" for row in project_rows(window))
    assert all(row["component_product_type"] is None for row in project_rows(window))
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
    _seed_enrichment(window)
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
    assert [row["lcsc"] for row in project_rows(window)] == expected
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
    assert window.store.get_part("R1")["lcsc"] == "C100"
    assert window.store.get_part("R2")["lcsc"] == "C200"
    window.logger.warning.assert_called()

    assert info_messages(window) == []


@pytest.mark.parametrize("column", ["lcsc", "stock"])
def test_auto_fill_rolls_back_all_groups_and_keeps_window_usable(
    make_window: Callable[..., Any], column: str
) -> None:
    """A later project write failure must roll back the complete opening's fill."""
    window = make_window(
        footprints=[
            Footprint("R1", value="10k", lcsc=""),
            Footprint("R2", value="20k", lcsc=""),
        ],
        part_preferences={("R_0603", "10k"): "C100", ("R_0603", "20k"): "C200"},
    )
    before = project_rows(window)
    reject_second_project_update(window, column)

    window.init_store()

    assert project_rows(window) == before
    assert all(not fp.field.text for fp in window.pcbnew.GetBoard().GetFootprints())
    assert all(not part["lcsc"] for part in window.test_rows.values())
    window.populate_footprint_list.assert_called_once()
    window.logger.warning.assert_called()

    assert info_messages(window) == []


def test_project_database_unavailable_at_startup_keeps_settings_reachable(
    make_window: Callable[..., Any], mainwindow: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure before preference lookup must not abort the already-created window."""
    window = make_window()

    def unavailable_store(*_args: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(mainwindow, "Store", unavailable_store)
    window.init_store()

    assert window.store is None
    assert window.test_rows == {}
    assert window.upper_toolbar.enabled[mainwindow.ID_GENERATE] is False
    assert window.upper_toolbar.enabled.get(mainwindow.ID_SETTINGS, True) is True
    window.footprint_list.Enable.assert_called_with(False)
    window.right_toolbar.Enable.assert_called_with(False)
    assert (
        "Settings remains available"
        in window.project_storage_status.SetLabel.call_args.args[0]
    )
    window.logger.warning.assert_called()


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


def test_clear_keeps_native_selection_array_alive_during_model_updates(
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
    window.partlist_data_model.get_lcsc.side_effect = lambda item: window.test_rows[
        item.get_reference()
    ]["lcsc"]
    remove_row = window.partlist_data_model.remove_lcsc_number.side_effect
    window.partlist_data_model.remove_lcsc_number.side_effect = lambda item: remove_row(
        item.get_reference()
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
    assert thread.call_args.kwargs["args"][0] == {"C100": ["R1"]}
    assert window.assembly_enrichment_generation > 1
    before = window.store.get_part("R1")
    window.on_assembly_enrichment_progress(
        SimpleNamespace(
            generation=1,
            lcsc="C100",
            refs=["R1"],
            metadata={"assembly_process": "SMT", "component_product_type": 1},
        )
    )
    assert window.store.get_part("R1") == before


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
        assert window.store.get_part("R1")["lcsc"] == lcsc

    window.remove_lcsc_number()
    assert {
        name: field.text for name, field in footprint.fields.items()
    } == dict.fromkeys(fields, "")
    window.init_store()
    reopened = make_window(board=window.pcbnew.GetBoard(), settings=window.settings)
    reopened.init_store()
    assert reopened.store.get_part("R1")["lcsc"] == ""
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
        assert window.store.get_part("R1")["lcsc"] == lcsc
    window.remove_lcsc_number()
    assert {name: field.text for name, field in footprint.fields.items()} == {
        alias: "",
        **metadata,
    }
    window.init_store()
    assert window.store.get_part("R1")["lcsc"] == ""
