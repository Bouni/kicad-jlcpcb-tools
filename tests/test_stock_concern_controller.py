"""Exercise stock concern through real model constructors and window handlers."""

from collections.abc import Iterable, Iterator
import importlib
import json
from pathlib import Path
import sqlite3
import types
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from .stock_test_support import stock_modules
from .test_settings_defaults import plugin_dir, shipped_defaults
from .test_settings_dialog import _dialog, _fire, _settings, _wx
from .test_stock_concern import CellAttr, part
from .wx_harness import load_mainwindow, wx_stubs


class Footprint:
    """Live footprint whose assignment, BOM and DNP state really change."""

    def __init__(
        self,
        lcsc: str = "C1",
        *,
        bom: bool = False,
        pos: bool = False,
        dnp: bool = False,
    ) -> None:
        self.lcsc = lcsc
        self.attributes = (int(bom) << 3) | (int(pos) << 2)
        self.dnp = dnp

    def GetAttributes(self) -> int:
        """Read flags used by the real footprint helpers."""
        return self.attributes

    def SetAttributes(self, value: int) -> None:
        """Persist flags so the subsequent regroup sees the changed BOM."""
        self.attributes = value

    def IsDNP(self) -> bool:
        """Expose live DNP independently of stale database snapshots."""
        return self.dnp

    def GetFields(self) -> list[types.SimpleNamespace]:
        """Expose the current assignment as an existing LCSC field."""
        return [
            types.SimpleNamespace(GetName=lambda: "LCSC", GetText=lambda: self.lcsc)
        ]

    def SetField(self, _name: str, value: str) -> None:
        """Keep the live board assignment in sync with successful transactions."""
        self.lcsc = value

    def GetLayer(self) -> int:
        """Place the fixture on the top side."""
        return 0

    def GetFPID(self) -> types.SimpleNamespace:
        """Supply the identity consumed by the real assignment workflow."""
        return types.SimpleNamespace(GetLibItemName=lambda: "R_0603")

    def GetValue(self) -> str:
        """Supply the value consumed by the real assignment workflow."""
        return "10k"


class Store:
    """Project rows with atomic assignment failures and stateful BOM updates."""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.parts = {record["reference"]: dict(record) for record in records}
        self.fail_write = False

    def read_all(self) -> list[dict[str, Any]]:
        """Return fresh snapshots as the production database does."""
        return [dict(record) for record in self.parts.values()]

    def get_part(self, reference: str) -> Optional[dict[str, Any]]:
        """Return a fresh snapshot of one row, or None for an unknown reference."""
        record = self.parts.get(reference)
        return None if record is None else dict(record)

    def set_lcsc_assignments(
        self, assignments: Iterable[tuple[str, str, Optional[int]]]
    ) -> None:
        """Commit every supplied assignment, or reject the entire transaction."""
        pending = list(assignments)
        if self.fail_write:
            raise sqlite3.OperationalError("assignment write failed")
        for reference, lcsc, stock in pending:
            self.parts[reference].update(lcsc=lcsc, stock=stock)

    def set_bom(self, reference: str, value: int) -> None:
        """Persist the flag written by the real BOM toggle event handler."""
        self.parts[reference]["exclude_from_bom"] = value

    def set_pos(self, reference: str, value: int) -> None:
        """Persist the placement flag without changing stock demand."""
        self.parts[reference]["exclude_from_pos"] = value


@pytest.fixture
def workflow() -> Iterator[types.SimpleNamespace]:
    """Supply real window handlers and models with a controllable event queue."""
    with stock_modules() as models:
        footprints = importlib.import_module(f"{models.package}.footprint_helpers")
        posted: list[tuple[Any, Any]] = []
        idle: list[Any] = []
        wx = wx_stubs(
            Frame=type("Frame", (), {}),
            NewIdRef=lambda: 1,
            PostEvent=lambda window, event: posted.append((window, event)),
            CallAfter=lambda callback: idle.append(callback),
        )
        mainwindow = load_mainwindow(
            "stock_concern_controller_tests",
            wx=wx,
            footprint_helpers={
                name: value
                for name, value in vars(footprints).items()
                if not name.startswith("__")
            },
        )

        def make_window(
            records: list[dict[str, Any]],
            stock: dict[str, object],
            live: Optional[dict[str, Footprint]] = None,
        ) -> Any:
            """Initialize the state surface consumed by actual window workflows."""
            window = object.__new__(mainwindow.JLCPCBTools)
            window.settings = {"part_preferences": {"remember_lcsc_assignments": False}}
            window.store = Store(records)
            window.bom_estimator_board_count = 5
            window.footprints = (
                live
                if live is not None
                else {
                    record["reference"]: Footprint(
                        record["lcsc"],
                        bom=bool(record["exclude_from_bom"]),
                        pos=bool(record["exclude_from_pos"]),
                        dnp=bool(record["is_dnp"]),
                    )
                    for record in records
                }
            )
            board = types.SimpleNamespace(
                FindFootprintByReference=window.footprints.get
            )
            window.pcbnew = types.SimpleNamespace(GetBoard=lambda: board)
            window.partlist_data_model = models.datamodel.PartListDataModel(1.0)
            window.library = MagicMock()
            window.library.state = mainwindow.LibraryState.INITIALIZED
            window.library.get_parts_db_info.return_value = None
            window.SetTitle = MagicMock()
            window._part_preferences_applied_on_open = True
            window.library.get_part_details.side_effect = lambda lcsc: {
                "stock": stock.get(lcsc),
                "type": "Basic",
            }
            window.library.read_correction_data.return_value = types.SimpleNamespace(
                corrections=()
            )
            window.update_correction_status = MagicMock()
            window.get_correction = MagicMock(return_value="0")
            window._get_enrichment_status_label = MagicMock(return_value="")
            window.start_assembly_enrichment = MagicMock()
            window.logger = MagicMock()
            window.recompute_bom_estimate = MagicMock()
            window.footprint_list = MagicMock()
            window.footprint_list.GetSelections.return_value = []
            window._bom_recompute_scheduled = False
            window.hide_bom_parts = False
            window.hide_pos_parts = False
            window.save_settings = MagicMock()
            return window

        def drain() -> None:
            """Deliver posted BOM events before executing deferred UI callbacks."""
            while posted:
                window, event = posted.pop(0)
                window.on_bom_data_changed(event)
            while idle:
                idle.pop(0)()

        yield types.SimpleNamespace(
            make_window=make_window,
            drain=drain,
            posted=posted,
            idle=idle,
            mainwindow=mainwindow,
            models=models,
        )


def test_populate_and_filtered_reopen_use_all_live_bom_demand(
    workflow: types.SimpleNamespace,
) -> None:
    """Hidden POS rows count, while live BOM/DNP exclusions and deletion do not."""
    records = [
        part("R1"),
        part("R2", exclude_from_pos=True),
        part("R3"),
        part("R4"),
        part("REMOVED"),
    ]
    live = {
        "R1": Footprint(),
        "R2": Footprint(pos=True),
        "R3": Footprint(bom=True),
        "R4": Footprint(dnp=True),
    }
    window = workflow.make_window(records, {"C1": 95}, live)
    window.hide_pos_parts = True
    window.populate_footprint_list()
    workflow.drain()
    assert {row[0] for row in window.partlist_data_model.data} == {"R1", "R3", "R4"}
    assert window.partlist_data_model.stock_concern_refs == {"R1", "R2"}
    window.hide_pos_parts = False
    window.populate_footprint_list()
    workflow.drain()
    assert window.partlist_data_model.stock_concern_refs == {"R1", "R2"}
    assert {row[0] for row in window.partlist_data_model.data} == {
        "R1",
        "R2",
        "R3",
        "R4",
    }


def test_assignment_and_removal_update_old_and_new_siblings(
    workflow: types.SimpleNamespace,
) -> None:
    """Moving one part clears its old group and flags the new group's sibling."""
    records = [part("R1"), part("R2"), part("R3", "C2")]
    window = workflow.make_window(records, {"C1": 75, "C2": 75})
    window.populate_footprint_list()
    workflow.drain()
    assert window.partlist_data_model.stock_concern_refs == {"R1", "R2"}

    window.assign_parts(
        types.SimpleNamespace(lcsc="C2", stock="75", type="Basic", references=["R2"])
    )
    workflow.drain()
    assert window.store.parts["R2"]["lcsc"] == "C2"
    assert window.footprints["R2"].lcsc == "C2"
    assert window.partlist_data_model.stock_concern_refs == {"R2", "R3"}

    window.footprint_list.GetSelections.return_value = [
        window.partlist_data_model.data[1]
    ]
    window.remove_lcsc_number()
    workflow.drain()
    assert window.store.parts["R2"]["lcsc"] == ""
    assert window.partlist_data_model.stock_concern_refs == set()


@pytest.mark.parametrize("action", ["assignment", "removal"])
def test_failed_assignment_transaction_preserves_existing_concerns(
    workflow: types.SimpleNamespace,
    action: str,
) -> None:
    """A failed write cannot mutate rows, live assignments or concern state."""
    window = workflow.make_window([part("R1"), part("R2")], {"C1": 75, "C2": 500})
    window.populate_footprint_list()
    workflow.drain()
    window.store.fail_write = True
    if action == "assignment":
        window.assign_parts(
            types.SimpleNamespace(
                lcsc="C2", stock="500", type="Basic", references=["R2"]
            )
        )
    else:
        window.footprint_list.GetSelections.return_value = [
            window.partlist_data_model.data[1]
        ]
        window.remove_lcsc_number()
    assert workflow.posted == []
    assert window.store.parts["R2"]["lcsc"] == "C1"
    assert window.footprints["R2"].lcsc == "C1"
    assert window.partlist_data_model.data[1][3] == "C1"
    assert window.partlist_data_model.stock_concern_refs == {"R1", "R2"}


def test_bom_and_pos_toggle_events_recalculate_current_board_demand(
    workflow: types.SimpleNamespace,
) -> None:
    """BOM changes count; POS-only changes leave the required stock unchanged."""
    window = workflow.make_window([part("R1"), part("R2")], {"C1": 75})
    window.populate_footprint_list()
    workflow.drain()
    window.footprint_list.GetSelections.return_value = [
        window.partlist_data_model.data[1]
    ]
    window.toggle_pos()
    workflow.drain()
    assert window.partlist_data_model.stock_concern_refs == {"R1", "R2"}
    window.toggle_bom()
    workflow.drain()
    assert window.partlist_data_model.stock_concern_refs == set()
    window.toggle_bom()
    workflow.drain()
    assert window.partlist_data_model.stock_concern_refs == {"R1", "R2"}


def test_empty_board_clears_stale_concerns(
    workflow: types.SimpleNamespace,
) -> None:
    """Rebuilding an emptied board removes concerns for deleted footprints."""
    stock = {"C1": 49}
    window = workflow.make_window([part("R1")], stock)
    window.populate_footprint_list()
    workflow.drain()
    assert window.partlist_data_model.stock_concern_refs == {"R1"}
    window.footprints.clear()
    window.populate_footprint_list()
    workflow.drain()
    assert window.partlist_data_model.stock_concern_refs == set()


def test_event_burst_coalesces_and_catalog_lookup_is_once_per_group(
    workflow: types.SimpleNamespace,
) -> None:
    """A single deferred recompute reuses warmed quantities for matching LCSC."""
    window = workflow.make_window([part("R1"), part("R2")], {"C1": 75})
    window.populate_footprint_list()
    workflow.drain()
    window.library.get_part_details.reset_mock()
    window.recompute_bom_estimate.reset_mock()
    window.on_bom_data_changed(None)
    window.on_bom_data_changed(None)
    assert len(workflow.idle) == 1
    workflow.drain()
    window.library.get_part_details.assert_not_called()
    window.recompute_bom_estimate.assert_called_once()


@pytest.mark.parametrize("source", ["store", "catalog"])
def test_failed_recompute_keeps_unknown_stock_risky_and_retry_available(
    workflow: types.SimpleNamespace,
    source: str,
) -> None:
    """Unknown availability warns; a failed project read clears stale reference marks."""
    window = workflow.make_window([part("R1")], {"C1": 49})
    window.populate_footprint_list()
    workflow.drain()
    assert window.partlist_data_model.stock_concern_refs == {"R1"}
    if source == "store":
        original = window.store.read_all
        window.store.read_all = MagicMock(
            side_effect=sqlite3.OperationalError("read failed")
        )
    else:
        window._invalidate_catalog_details()
        original = window.library.get_part_details.side_effect
        window.library.get_part_details.side_effect = sqlite3.OperationalError(
            "read failed"
        )
    window.on_bom_data_changed(None)
    workflow.drain()
    assert window.partlist_data_model.stock_concern_refs == (
        {"R1"} if source == "catalog" else set()
    )
    assert window._bom_recompute_scheduled is False
    if source == "store":
        window.store.read_all = original
    else:
        window.library.get_part_details.side_effect = original
    window.on_bom_data_changed(None)
    workflow.drain()
    assert window.partlist_data_model.stock_concern_refs == {"R1"}


def test_setting_toggle_immediately_clears_and_restores_stock_style(
    workflow: types.SimpleNamespace,
) -> None:
    """Disabling the option repaints the stock cell; reenabling recalculates it."""
    window = workflow.make_window([part("R1")], {"C1": 49})
    window.populate_footprint_list()
    workflow.drain()
    column = window.partlist_data_model.columns["STOCK_COL"]
    for enabled in (False, True):
        window.update_settings(
            types.SimpleNamespace(
                section="highlighting", setting="stock_concern", value=enabled
            )
        )
        workflow.drain()
        attr = CellAttr()
        assert (
            window.partlist_data_model.GetAttr(
                window.partlist_data_model.data[0], column, attr
            )
            is enabled
        )
        assert window.settings["highlighting"]["stock_concern"] is enabled


@pytest.mark.parametrize("saved", [None, False, True])
def test_stock_concern_default_and_saved_setting_survive_reopening(
    workflow: types.SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    saved: Optional[bool],
) -> None:
    """Old files gain enabled concern, while each explicit choice survives reload."""
    mainwindow = workflow.mainwindow
    monkeypatch.setattr(mainwindow, "PLUGIN_PATH", str(tmp_path))
    plugin_dir(tmp_path, shipped_defaults())
    path = tmp_path / "settings.json"
    settings = {"highlighting": {"matches": False}, "custom": "retained"}
    if saved is not None:
        settings["highlighting"]["stock_concern"] = saved
    path.write_text(json.dumps(settings), encoding="utf-8")
    expected = saved if saved is not None else True
    for _ in range(2):
        window = workflow.make_window([part("R1")], {"C1": 49})
        del window.save_settings
        window.load_settings()
        window.populate_footprint_list()
        workflow.drain()
        assert window.settings["highlighting"]["stock_concern"] is expected
        assert window.settings["highlighting"]["matches"] is False
        assert window.settings["custom"] == "retained"
        assert bool(window.partlist_data_model.stock_concern_refs) is expected
    assert (
        json.loads(path.read_text(encoding="utf-8"))["highlighting"]["stock_concern"]
        is expected
    )


def test_failed_concern_migration_preserves_file_and_allows_retry(
    workflow: types.SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed settings replacement leaves prior bytes reloadable on retry."""
    mainwindow = workflow.mainwindow
    monkeypatch.setattr(mainwindow, "PLUGIN_PATH", str(tmp_path))
    plugin_dir(tmp_path, shipped_defaults())
    path = tmp_path / "settings.json"
    original = b'{"highlighting": {"matches": false}, "custom": "retained"}'
    path.write_bytes(original)
    with monkeypatch.context() as failure:
        failure.setattr(
            mainwindow.os,
            "replace",
            MagicMock(side_effect=OSError("replacement denied")),
        )
        window = object.__new__(mainwindow.JLCPCBTools)
        with pytest.raises(OSError, match="replacement denied"):
            window.load_settings()
        assert path.read_bytes() == original
        assert sorted(tmp_path.iterdir()) == [tmp_path / "default_settings.json", path]
    reopened = object.__new__(mainwindow.JLCPCBTools)
    reopened.load_settings()
    assert reopened.settings["highlighting"]["stock_concern"] is True


@pytest.mark.parametrize("enabled", [False, True])
def test_real_settings_constructor_and_checkbox_event(enabled: bool) -> None:
    """The independently labeled setting loads and dispatches its own boolean."""
    settings = _settings(True)
    settings["highlighting"]["stock_concern"] = enabled
    dialog = _dialog(settings)
    control = dialog.stock_concern_setting
    assert control.GetLabel() == "Highlight stock concern"
    assert control.GetValue() is enabled
    control.SetValue(not enabled)
    events = _fire(control)
    assert [(event.section, event.setting, event.value) for event in events] == [
        ("highlighting", "stock_concern", not enabled)
    ]


def test_real_settings_constructor_defaults_missing_concern_to_enabled() -> None:
    """Opening pre-feature settings shows the enabled default without an event."""
    settings = _settings(True)
    settings["highlighting"].pop("stock_concern", None)
    before = len(_wx.posted_events)
    dialog = _dialog(settings)
    assert dialog.stock_concern_setting.GetValue() is True
    assert len(_wx.posted_events) == before


@pytest.mark.parametrize("entry", ["spin", "text"])
def test_board_quantity_handlers_update_concern_without_waiting_for_bom_event(
    workflow: types.SimpleNamespace, entry: str
) -> None:
    """Estimator quantity changes concern immediately, including its hidden panel."""
    window = workflow.make_window([part("R1"), part("R2")], {"C1": 199})
    window.bom_estimator_board_count = 5
    window.bom_estimator_show = False
    window.populate_footprint_list()
    workflow.drain()
    assert window.partlist_data_model.stock_concern_refs == set()
    value = [10]
    control = types.SimpleNamespace(
        GetValue=lambda: value[0], SetValue=lambda new: value.__setitem__(0, new)
    )
    if entry == "spin":
        window.on_bom_estimator_board_count_spinctrl(
            types.SimpleNamespace(GetEventObject=lambda: control)
        )
    else:
        window.bom_estimator_boards_input = control
        window.bom_estimator_text_timer = MagicMock()
        window.on_bom_estimator_board_count_text()
        window.bom_estimator_text_timer.StartOnce.assert_called_once_with(300)
        window.on_bom_estimator_board_count_text_timer()
    assert window.partlist_data_model.stock_concern_refs == {"R1", "R2"}
    assert window.settings["general"]["bom_estimator_boards"] == 10
    assert workflow.posted == []
    value[0] = 1
    window.on_bom_estimator_board_count_spinctrl(
        types.SimpleNamespace(GetEventObject=lambda: control)
    )
    assert value[0] == 5
    assert window.partlist_data_model.stock_concern_refs == set()
