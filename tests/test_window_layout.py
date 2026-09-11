"""Regression coverage for persisted layouts through dialog lifecycle events."""

from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from .wx_harness import load, load_mainwindow, mainwindow_stubs, wx_stubs


class _Size(tuple):
    def __new__(cls, *values: int) -> "_Size":
        if any(not -(2**31) <= value < 2**31 for value in values):
            raise OverflowError("wx dimensions must fit a native signed integer")
        return super().__new__(cls, values)

    def GetWidth(self) -> int:
        return self[0]

    def GetHeight(self) -> int:
        return self[1]


def _scale(value: Any, factor: float) -> Any:
    if isinstance(value, tuple):
        return _Size(*(round(item * factor) for item in value))
    if not -(2**31) <= value < 2**31:
        raise OverflowError("wx dimensions must fit a native signed integer")
    return round(value * factor)


class _Display:
    areas = [(3840, 2160)]

    def __init__(self, index: int = 0) -> None:
        self.index = index

    @staticmethod
    def GetFromWindow(window: Any) -> int:
        return window.display_index

    @staticmethod
    def GetCount() -> int:
        return len(_Display.areas)

    def GetClientArea(self) -> Any:
        width, height = self.areas[self.index]
        return SimpleNamespace(
            width=width,
            height=height,
            GetSize=lambda: _Size(width, height),
            GetWidth=lambda: width,
            GetHeight=lambda: height,
        )


class _TopLevelWindow:
    """Dispatch bound events and retain the state read by the real handlers."""

    def __init__(
        self,
        *_args: Any,
        size: tuple = (1400, 800),
        style: int = 0,
        title: str = "",
        **_kwargs: Any,
    ) -> None:
        self._size = size
        self._style = style
        self._title = title
        self._parent = _args[0] if _args else None
        self._children: list[Any] = []
        if isinstance(self._parent, _TopLevelWindow):
            self._parent._children.append(self)
        self.display_index = 0
        self.scale_factor = getattr(_args[0], "scale_factor", 2) if _args else 2
        self._destroyed = False
        self._bindings: dict[Any, Callable] = {}
        self.GetSize = MagicMock(side_effect=lambda: self._size)
        self.SetSize = MagicMock(side_effect=self._set_size)
        self.IsMaximized = MagicMock(return_value=False)
        self.IsIconized = MagicMock(return_value=False)
        self.IsFullScreen = MagicMock(return_value=False)
        self.FromDIP = lambda value: _scale(value, self.scale_factor)
        self.ToDIP = lambda value: _scale(value, 1 / self.scale_factor)
        for name in (
            "SetAcceleratorTable SetSizer SetSizeHints Centre Destroy"
        ).split():
            setattr(self, name, MagicMock())
        self.Destroy.side_effect = lambda: setattr(self, "_destroyed", True)

    def __bool__(self) -> bool:
        return not self._destroyed

    def Bind(self, event: Any, handler: Callable, *_args: Any, **_kwargs: Any) -> None:
        self._bindings[event] = handler

    def GetWindowStyleFlag(self) -> int:
        return self._style

    def GetParent(self) -> Any:
        return self._parent

    def GetTitle(self) -> str:
        return self._title

    def SetTitle(self, title: str) -> None:
        self._title = title

    def _set_size(self, size: tuple) -> None:
        self._size = tuple(size)
        if getattr(self, "_laid_out", False):
            self._layout()
        self.emit(_wx["wx"].EVT_SIZE)

    def _layout(self) -> None:
        self._laid_out = True
        for child in self._children:
            if isinstance(child, _Panel):
                child._size = self._size
            elif getattr(child, "_is_table", False):
                child.client_width = self._size[0] - 80
        for name in ("part_list", "footprint_list"):
            if hasattr(self, name) and not isinstance(self, _Frame):
                # Table borders and surrounding layout consume client space.
                getattr(self, name).client_width = self._size[0] - 80

    def emit(self, event_type: Any, source: Any = None) -> MagicMock:
        event = MagicMock()
        event.GetEventObject.return_value = self if source is None else source
        event.GetSize.return_value = self._size
        if event_type in self._bindings:
            self._bindings[event_type](event)
        return event

    def Close(self) -> None:
        self.emit(_wx["wx"].EVT_CLOSE)


class _Frame(_TopLevelWindow):
    """A normal frame deliberately has no dialog-only modal operations."""

    def Layout(self) -> bool:
        self._layout()
        return True


class _Panel(_TopLevelWindow):
    """Retain the content area separately from the frame's outer layout."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.Layout = MagicMock(side_effect=self._layout)


class _Dialog(_TopLevelWindow):
    """Child dialogs retain their distinct modal API."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.Layout = MagicMock(side_effect=self._layout)
        self.IsModal = MagicMock(return_value=False)
        self.EndModal = MagicMock(side_effect=self._end_modal)

    def _end_modal(self, _result: int) -> None:
        assert self.IsModal(), "EndModal is invalid for a modeless dialog"
        self.IsModal.return_value = False


def _column(model_column: int, width: int, title: str = "Column") -> MagicMock:
    column = MagicMock()
    column.GetModelColumn.return_value = model_column
    column.GetTitle.return_value = title
    column.specified_width = width
    column.sortable = False
    column.SetSortable.side_effect = lambda value: setattr(column, "sortable", value)
    column.GetWidth.side_effect = lambda: column.specified_width
    column.SetWidth.side_effect = lambda value: setattr(
        column, "specified_width", value
    )
    return column


def _column_by_title(control: MagicMock, title: str) -> MagicMock:
    return next(column for column in control.GetColumns() if column.GetTitle() == title)


def _column_by_model(control: MagicMock, model_column: int) -> MagicMock:
    return next(
        column
        for column in control.GetColumns()
        if column.GetModelColumn() == model_column
    )


def _control(
    columns: Sequence[MagicMock] = (),
    scale: float = 1,
    *,
    client_width: int = 2000,
    stretch: bool = False,
) -> MagicMock:
    """Model the last column's effective stretch without changing its chosen width."""
    control = MagicMock()
    control.client_width = client_width
    control.GetClientSize.side_effect = lambda: _Size(control.client_width, 600)
    control.GetColumns.return_value = []
    control.FromDIP.side_effect = lambda value: _scale(value, scale)
    control.ToDIP.side_effect = lambda value: _scale(value, 1 / scale)

    def append(column: MagicMock) -> None:
        control.GetColumns.return_value.append(column)

        def effective_width() -> int:
            visible = control.GetColumns()
            if stretch and column is visible[-1]:
                return max(
                    column.specified_width,
                    control.client_width
                    - sum(other.specified_width for other in visible[:-1]),
                )
            return column.specified_width

        column.GetWidth.side_effect = effective_width

    control.AppendColumn.side_effect = append

    def append_text(
        label: str, model_column: int, *, width: int, **_kwargs: Any
    ) -> MagicMock:
        column = _column(model_column, width, label)
        control.AppendColumn(column)
        return column

    for name in "AppendTextColumn AppendToggleColumn AppendIconTextColumn".split():
        getattr(control, name).side_effect = append_text
    for column in columns:
        append(column)
    return control


_PACKAGE = "window_layout_tests"
_after: list[tuple[Callable, tuple]] = []


def _call_after(callback: Callable, *args: Any) -> None:
    _after.append((callback, args))


def _drain_callbacks() -> None:
    while _after:
        callback, args = _after.pop(0)
        callback(*args)


@pytest.fixture(autouse=True)
def _clear_callbacks() -> Iterator[None]:
    _after.clear()
    _Display.areas = [(3840, 2160)]
    yield
    _after.clear()


_wx = wx_stubs(
    Dialog=_Dialog,
    Frame=_Frame,
    Panel=_Panel,
    Display=_Display,
    Size=_Size,
    DefaultPosition=None,
    NOT_FOUND=-1,
    DefaultSize=None,
    EmptyString="",
    NullBitmap=None,
    NewId=lambda: 1,
    NewIdRef=lambda: 1,
    GetApp=lambda: True,
    CallAfter=_call_after,
    GetTopLevelParent=lambda window: window,
    **{
        name: MagicMock()
        for name in (
            "Timer StaticText TextCtrl Button ComboBox CheckBox BoxSizer ToolBar Gauge "
            "StaticBoxSizer ScrolledWindow AcceleratorEntry AcceleratorTable"
        ).split()
    },
)
_wx["wx"].DEFAULT_FRAME_STYLE = (
    _wx["wx"].CAPTION
    | _wx["wx"].MINIMIZE_BOX
    | _wx["wx"].MAXIMIZE_BOX
    | _wx["wx"].RESIZE_BORDER
    | _wx["wx"].CLOSE_BOX
    | _wx["wx"].SYSTEM_MENU
)
_wx["wx.adv"].BitmapComboBox = MagicMock()
_wx["wx.dataview"].PyDataViewModel = object
_wx["wx.dataview"].DataViewCustomRenderer = object
_wx["wx.dataview"].DataViewCtrl = MagicMock()
_wx["wx.dataview"].DataViewColumn = (
    lambda label, _renderer, model_column, *, width, **_kwargs: _column(
        model_column, width, label
    )
)
_helpers = load(_PACKAGE, "helpers", _wx)
_helper_symbols = {
    "PLUGIN_PATH": _helpers.PLUGIN_PATH,
    "GetScaleFactor": lambda _window: 2,
    "HighResWxSize": _helpers.HighResWxSize,
    "getVersion": lambda: "test",
    "loadBitmapScaled": lambda *_args: None,
}
_stubs = mainwindow_stubs(
    _PACKAGE,
    wx=_wx,
    datamodel={"PartSelectorDataModel": MagicMock()},
    dataview_highlight={"HighlightedTextRenderer": MagicMock()},
    helpers=_helper_symbols,
)
# Read the actual main-model column names instead of duplicating its schema.
_model_stubs = dict(_stubs)
_model_stubs.pop(f"{_PACKAGE}.dataview_highlight")
_model_stubs.pop(f"{_PACKAGE}.helpers")
datamodel = load(_PACKAGE, "datamodel", _model_stubs)
_main_model = MagicMock(columns=datamodel.PartListDataModel.columns)
mainwindow = load_mainwindow(
    _PACKAGE,
    wx=_wx,
    helpers=_helper_symbols,
    datamodel={"PartListDataModel": _main_model, "STANDARD_ONLY_TOOLTIP": ""},
    dataview_highlight={
        "HighlightedTextRenderer": MagicMock(),
        "decode_highlighted_value": lambda value: (value, []),
        "simplify_footprint_name": lambda value: value,
    },
    bom_widget={
        "BomEstimatorController": MagicMock(),
        "BomEstimatorWidget": MagicMock(),
    },
)
partselector = load(_PACKAGE, "partselector", _stubs)
layout = load(_PACKAGE, "window_layout", _stubs)
schema = load(_PACKAGE, "partselector_columns", _stubs)


def _open_selector(
    monkeypatch: pytest.MonkeyPatch,
    settings: dict,
    *,
    parent: Any = None,
    supports_dip: bool = True,
    fail_at: Optional[str] = None,
    real_search: bool = False,
    scale: float = 2,
    monitor: int = 0,
) -> Any:
    if parent is None:
        parent = SimpleNamespace(
            window=_Dialog(),
            display_index=monitor,
            scale_factor=scale,
            settings={"partselector": settings},
            library=SimpleNamespace(categories=[]),
            is_catalog_available=lambda: True,
            save_settings=MagicMock(),
            _part_selector=None,
        )
        parent.window.scale_factor = scale
        parent.window.display_index = monitor
    control = _control(scale=parent.scale_factor, client_width=0, stretch=True)
    if not supports_dip:
        parent.scale_factor = 1
        if hasattr(parent.window, "FromDIP"):
            del parent.window.FromDIP, parent.window.ToDIP
        control = _control(client_width=0, stretch=True)
        del control.FromDIP, control.ToDIP
        init = _Dialog.__init__

        def init_without_dip(self: _Dialog, *args: Any, **kwargs: Any) -> None:
            init(self, *args, **kwargs)
            if hasattr(self, "FromDIP"):
                del self.FromDIP, self.ToDIP

        monkeypatch.setattr(_Dialog, "__init__", init_without_dip)
    monkeypatch.setattr(partselector.dv.DataViewCtrl, "return_value", control)
    monkeypatch.setattr(partselector.dv.DataViewCtrl, "side_effect", None)
    if not real_search:
        monkeypatch.setattr(partselector.PartSelectorDialog, "search", MagicMock())

    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("UI construction failed")

    if fail_at == "before_controls":
        monkeypatch.setattr(partselector.wx.StaticText, "side_effect", fail)
    elif fail_at == "during_restore":
        append_text = control.AppendTextColumn.side_effect

        def append_with_restore_failure(
            label: str, model_column: int, *, width: int, **kwargs: Any
        ) -> MagicMock:
            column = append_text(label, model_column, width=width, **kwargs)
            if label == "Stock":
                column.SetWidth.side_effect = fail
            return column

        monkeypatch.setattr(
            control.AppendTextColumn, "side_effect", append_with_restore_failure
        )
    selector = object.__new__(partselector.PartSelectorDialog)
    if fail_at:
        with pytest.raises(RuntimeError, match="UI construction failed"):
            partselector.PartSelectorDialog.__init__(selector, parent, {})
    else:
        partselector.PartSelectorDialog.__init__(selector, parent, {})
        parent._part_selector = selector
    return selector


def _open_main(
    monkeypatch: pytest.MonkeyPatch, settings: dict, fail_at: Optional[str] = None
) -> Any:
    control = _control(scale=2, client_width=0, stretch=True)
    monkeypatch.setattr(mainwindow.dv.DataViewCtrl, "return_value", control)

    def create_table(parent: Any, *_args: Any, **_kwargs: Any) -> MagicMock:
        control._is_table = True
        control.GetParent.return_value = parent
        parent._children.append(control)
        return control

    monkeypatch.setattr(mainwindow.dv.DataViewCtrl, "side_effect", create_table)

    def load_settings(window: Any) -> None:
        window.settings = json.loads(json.dumps(settings))

    def init_logger(window: Any) -> None:
        window.logger = MagicMock()

    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("UI construction failed")

    monkeypatch.setattr(mainwindow.JLCPCBTools, "load_settings", load_settings)
    monkeypatch.setattr(mainwindow.JLCPCBTools, "init_logger", init_logger)
    monkeypatch.setattr(mainwindow.JLCPCBTools, "init_data", lambda _self: None)
    if fail_at == "before_controls":
        monkeypatch.setattr(mainwindow.wx.ToolBar, "side_effect", fail)
    elif fail_at == "during_columns":
        monkeypatch.setattr(control.AppendToggleColumn, "side_effect", fail)
    elif fail_at == "during_restore":
        append_text = control.AppendTextColumn.side_effect

        def append_with_restore_failure(
            label: str, model_column: int, *, width: int, **kwargs: Any
        ) -> MagicMock:
            column = append_text(label, model_column, width=width, **kwargs)
            if model_column == datamodel.PartListDataModel.columns["FP_COL"]:
                column.SetWidth.side_effect = fail
            return column

        monkeypatch.setattr(
            control.AppendTextColumn, "side_effect", append_with_restore_failure
        )
    provider = MagicMock()
    provider.get_pcbnew().GetBoard().GetFileName.return_value = "test.kicad_pcb"
    window = object.__new__(mainwindow.JLCPCBTools)
    if fail_at:
        with pytest.raises(RuntimeError, match="UI construction failed"):
            mainwindow.JLCPCBTools.__init__(window, None, provider)
    else:
        mainwindow.JLCPCBTools.__init__(window, None, provider)
    window.library = SimpleNamespace(categories=[])
    window._catalog_ready = True
    window.save_settings = MagicMock()
    return window


def test_semantic_widths_survive_schema_insertion_removal_and_reordering() -> None:
    """Follow semantic column names as saved model IDs change across releases."""
    old_schema = [
        column
        for column in schema.PARTSELECTOR_COLUMNS
        if column.key != "trailing_spacer"
    ]
    old_keys = {index: column.key for index, column in enumerate(old_schema)}
    original = _control(
        [_column(index, 100 + index * 20) for index in old_keys], scale=2
    )
    saved = json.loads(json.dumps(layout.get_column_widths(original, old_keys)))
    # These are model IDs reassigned by a schema change, not just visual reordering.
    new_schema = [
        old_schema[-1],
        replace(old_schema[0], key="new_column"),
        old_schema[0],
        old_schema[5],
    ]
    new_keys = {index: column.key for index, column in enumerate(new_schema)}
    reopened = _control([_column(index, 77) for index in new_keys])

    layout.restore_column_widths(reopened, saved, new_keys)

    assert saved["lcsc"] == 50
    assert [column.GetWidth() for column in reopened.GetColumns()] == [130, 77, 50, 100]


@pytest.mark.parametrize("widths", [None, [], "invalid", 42, {"0": 999}])
def test_invalid_or_legacy_numeric_settings_leave_defaults(widths: object) -> None:
    """Ignore malformed settings and numeric keys from the unreleased draft."""
    control = _control([_column(0, 100)])
    layout.restore_column_widths(control, widths, {0: "stock"})
    control.GetColumns()[0].SetWidth.assert_not_called()


@pytest.mark.parametrize("width", [None, 0, -1, True, 2.5, "100", [], {}])
def test_invalid_width_does_not_prevent_restoring_other_columns(width: object) -> None:
    """Restore valid known widths independently of invalid or obsolete entries."""
    control = _control([_column(0, 100), _column(1, 100)], scale=2)
    layout.restore_column_widths(
        control,
        {"stock": width, "price": 75, "obsolete": 999},
        {0: "stock", 1: "price"},
    )
    assert [column.GetWidth() for column in control.GetColumns()] == [100, 150]


def test_explicit_spacer_exclusion_preserves_a_real_blank_title_column() -> None:
    """Column identity, rather than a blank display label, identifies the spacer."""
    real_column = _column(0, 100, "")
    spacer = _column(1, 500, "Spacer")
    control = _control([real_column, spacer])
    assert layout.get_column_widths(control, {0: "real_column"}) == {"real_column": 100}
    layout.restore_column_widths(
        control, {"real_column": 200, "spacer": 50}, {0: "real_column"}
    )
    assert real_column.GetWidth() == 200
    spacer.SetWidth.assert_not_called()


@pytest.mark.parametrize("state", ["IsMaximized", "IsIconized", "IsFullScreen"])
@pytest.mark.parametrize("via_parent", [False, True])
def test_resize_then_non_normal_close_reopens_latest_normal_size(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    via_parent: bool,
) -> None:
    """Save the last normal resize through direct and parent close paths."""
    settings = {"partselector": {"size": [900, 700], "stock": True}}
    parent = _open_main(monkeypatch, settings) if via_parent else None
    selector = _open_selector(monkeypatch, settings["partselector"], parent=parent)
    selector.SetSize((2400, 1600))
    getattr(selector, state).return_value = True
    selector.SetSize((3600, 2400))
    (parent if via_parent else selector).Close()
    selector.Destroy.assert_called_once()
    assert selector.parent._part_selector is None
    if via_parent:
        parent.Destroy.assert_called_once()
    saved = json.loads(json.dumps(selector.parent.settings))
    assert saved["partselector"]["size"] == [1200, 800]
    assert saved["partselector"]["stock"] is True
    reopened = _open_selector(monkeypatch, saved["partselector"])
    assert reopened.GetSize() == (2400, 1600)


@pytest.mark.parametrize("saved_size", [None, [1100, 750]])
def test_maximizing_without_resize_preserves_default_or_restored_size(
    monkeypatch: pytest.MonkeyPatch,
    saved_size: Optional[list[int]],
) -> None:
    """Retain the opening dimensions even when no normal resize event occurs."""
    selector = _open_selector(monkeypatch, {"size": saved_size})
    initial_size = selector.GetSize()
    selector.IsMaximized.return_value = True
    selector.SetSize((3600, 2400))
    selector.Close()
    saved = json.loads(json.dumps(selector.parent.settings["partselector"]))
    reopened = _open_selector(monkeypatch, saved)
    assert reopened.GetSize() == initial_size


@pytest.mark.parametrize("state", ["IsMaximized", "IsIconized", "IsFullScreen"])
def test_restoring_then_resizing_records_the_new_normal_size(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    """Resume recording normal resizes after leaving a non-normal window state."""
    selector = _open_selector(monkeypatch, {"size": [900, 700]})
    selector.SetSize((2400, 1600))
    state_check = getattr(selector, state)
    state_check.return_value = True
    selector.SetSize((3600, 2400))
    state_check.return_value = False
    selector.SetSize((2400, 1600))
    selector.SetSize((2600, 1800))
    selector.Close()
    saved = json.loads(json.dumps(selector.parent.settings["partselector"]))
    assert saved["size"] == [1300, 900]
    assert _open_selector(monkeypatch, saved).GetSize() == (2600, 1800)


def test_size_events_ignore_children_and_preserve_event_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve the normal size and wx processing when child events propagate."""
    selector = _open_selector(monkeypatch, {})
    selector.SetSize((2400, 1600))
    normal_event = selector.emit(_wx["wx"].EVT_SIZE)
    # A delayed child event during maximize must not replace the normal size.
    selector.IsMaximized.return_value = True
    selector.SetSize((3600, 2400))
    selector.IsMaximized.return_value = False
    child_event = selector.emit(_wx["wx"].EVT_SIZE, selector.part_list)
    selector.IsMaximized.return_value = True
    selector.Close()
    assert selector.parent.settings["partselector"]["size"] == [1200, 800]
    normal_event.Skip.assert_called_once()
    child_event.Skip.assert_called_once()


def test_selector_price_survives_enlarge_save_reopen_and_shrink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not turn automatic stretching into the persisted Price width."""
    selector = _open_selector(monkeypatch, {})
    price = _column_by_title(selector.part_list, "Price")
    price.SetWidth(240)
    selector.SetSize((5000, 2000))
    assert selector.part_list.GetColumns()[-1].GetWidth() > 240
    selector.Close()
    saved = json.loads(json.dumps(selector.parent.settings["partselector"]))
    reopened = _open_selector(monkeypatch, saved)
    reopened.SetSize((1800, 1200))
    reopened_price = _column_by_title(reopened.part_list, "Price")
    assert reopened_price.GetWidth() == 240
    assert saved["column_widths"]["price"] == 120
    assert "trailing_spacer" not in saved["column_widths"]


def test_selector_spacer_preserves_query_fields_row_alignment_and_sorting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the spacer outside SQL and sorting, with an empty aligned row cell."""
    selector = _open_selector(monkeypatch, {})
    columns = partselector.PARTSELECTOR_COLUMNS
    assert columns[-1].key == "trailing_spacer"
    assert columns[-1].db_field is None
    assert not columns[-1].sortable
    assert schema.DB_FIELDS == [
        "LCSC Part",
        "MFR.Part",
        "Package",
        "Library Type",
        "Stock",
        "Manufacturer",
        "Description",
        "Price",
        "First Category",
    ]
    assert len(columns) - 1 not in schema.SORTABLE_COLUMN_INDEX_TO_DB
    selector.parts = {"R1": "C123"}
    db_row = {field: field for field in partselector.DB_FIELDS}
    db_row.update({"LCSC Part": "C123", "Stock": "25", "Price": "1-:0.5"})
    selector.populate_part_list(
        [tuple(db_row[field] for field in partselector.DB_FIELDS)], 0
    )
    row = selector.part_list_model.AddEntry.call_args.args[0]
    assert len(row) == len(columns)
    assert row[0] == "C123" and row[5] == "25"
    assert row[-2:] == ["1 parts: $0.5 each / $0.5 total", ""]
    model = datamodel.PartSelectorDataModel()
    model.ItemToObject = lambda item: item
    assert model.GetColumnCount() == len(row)
    assert model.GetColumnType(len(row) - 1) == "string"
    assert model.GetValue(row, len(row) - 1) == ""
    assert not selector.part_list.GetColumns()[-1].sortable
    assert selector.part_list.GetColumns()[-2].sortable


def test_main_constructor_creates_a_normal_frame_with_window_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Create a regular application window with standard window controls."""
    window = _open_main(monkeypatch, {})

    assert isinstance(window, _Frame)
    assert not isinstance(window, _Dialog)
    assert not hasattr(window, "IsModal")
    assert not hasattr(window, "EndModal")
    assert window.GetTitle() == "JLCPCB Tools [ test ]"
    assert window.GetWindowStyleFlag() == _wx["wx"].DEFAULT_FRAME_STYLE
    for flag in ("MINIMIZE_BOX", "MAXIMIZE_BOX", "RESIZE_BORDER", "CLOSE_BOX"):
        assert window.GetWindowStyleFlag() & getattr(_wx["wx"], flag)


def test_main_form_controls_share_a_panel_and_follow_frame_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Frame forms require a panel, whose layout must follow outer size changes."""
    factories = [
        mainwindow.wx.ToolBar,
        mainwindow.wx.TextCtrl,
        mainwindow.wx.Gauge,
        mainwindow.wx.StaticText,
        mainwindow.BomEstimatorWidget,
    ]
    first_calls = [factory.call_count for factory in factories]
    window = _open_main(monkeypatch, {})

    panel = window.content_panel
    assert isinstance(panel, _Panel)
    assert panel.GetParent() is window
    assert window.footprint_list.GetParent() is panel
    for factory, first_call in zip(factories, first_calls):
        calls = factory.call_args_list[first_call:]
        assert calls
        assert all(call.args[0] is panel for call in calls)
    assert window.footprint_list.GetClientSize()[0] == window.GetSize()[0] - 80
    panel.Layout.reset_mock()
    window._size = (3000, 2000)

    window.Layout()

    panel.Layout.assert_called_once()
    assert window.footprint_list.GetClientSize()[0] == 2920


def test_main_constructor_restores_semantic_widths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restore the main table by its real model column names."""
    window = _open_main(
        monkeypatch,
        {
            "mainwindow": {
                "column_widths": {
                    "FP_COL": 90,
                    "STANDARD_ONLY_COL": 200,
                    "TRAILING_SPACER_COL": 200,
                }
            }
        },
    )
    footprint = _column_by_title(window.footprint_list, "Footprint")
    assert footprint.GetWidth() == 180
    for column in window.footprint_list.GetColumns():
        if column.GetTitle() in {"Std", " "}:
            column.SetWidth.assert_not_called()


@pytest.mark.parametrize("selector_open", [False, True])
def test_parent_close_saves_both_layouts_once_before_destroy(
    monkeypatch: pytest.MonkeyPatch,
    selector_open: bool,
) -> None:
    """Persist both windows before either destruction with one synchronous write."""
    window = _open_main(
        monkeypatch,
        {
            "general": {"unchanged": True},
            "mainwindow": {"column_widths": {"FP_COL": 90}},
        },
    )
    footprint = _column_by_title(window.footprint_list, "Footprint")
    footprint.SetWidth(240)
    issue_widths = {"BOM_COL": 110, "POS_COL": 90}
    for key, width in issue_widths.items():
        column = _column_by_model(
            window.footprint_list, datamodel.PartListDataModel.columns[key]
        )
        column.SetWidth(width * 2)
    snapshots = []
    window.save_settings.side_effect = lambda: snapshots.append(
        json.loads(json.dumps(window.settings))
    )
    window.Destroy.side_effect = lambda: snapshots.append("parent destroyed")
    if selector_open:
        selector = _open_selector(monkeypatch, {}, parent=window)
        selector.SetSize((2400, 1600))
        stock = _column_by_title(selector.part_list, "Stock")
        stock.SetWidth(360)
        selector.Destroy.side_effect = lambda: snapshots.append("selector destroyed")
    window.Close()
    window.save_settings.assert_called_once()
    assert snapshots[0]["mainwindow"]["column_widths"]["FP_COL"] == 120
    assert "TRAILING_SPACER_COL" not in snapshots[0]["mainwindow"]["column_widths"]
    assert "STANDARD_ONLY_COL" not in snapshots[0]["mainwindow"]["column_widths"]
    assert snapshots[0]["general"]["unchanged"] is True
    if selector_open:
        assert snapshots[0]["partselector"]["size"] == [1200, 800]
        assert snapshots[1:] == ["selector destroyed", "parent destroyed"]
    else:
        assert snapshots[1:] == ["parent destroyed"]
    assert window._part_selector is None
    reopened = _open_main(monkeypatch, snapshots[0])
    for key, width in issue_widths.items():
        assert snapshots[0]["mainwindow"]["column_widths"][key] == width
        column = _column_by_model(
            reopened.footprint_list, datamodel.PartListDataModel.columns[key]
        )
        assert column.GetWidth() == width * 2
    if selector_open:
        reopened_selector = _open_selector(monkeypatch, snapshots[0]["partselector"])
        stock = _column_by_title(reopened_selector.part_list, "Stock")
        assert snapshots[0]["partselector"]["column_widths"]["stock"] == 180
        assert stock.GetWidth() == 360


@pytest.mark.parametrize(
    "fail_at", ["before_controls", "during_columns", "during_restore"]
)
def test_incomplete_main_constructor_closes_without_overwriting_saved_layout(
    monkeypatch: pytest.MonkeyPatch,
    fail_at: str,
) -> None:
    """Teardown before controls or logger are ready leaves saved layout intact."""
    saved_layout = {"column_widths": {"REF_COL": 75, "FP_COL": 90}}
    window = _open_main(monkeypatch, {"mainwindow": saved_layout}, fail_at=fail_at)
    assert not hasattr(window, "logger")
    window.Close()
    assert window.settings["mainwindow"] == saved_layout
    window.save_settings.assert_not_called()
    window.Destroy.assert_called_once()


def test_older_wx_preserves_widths_and_size_without_dip_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retain unscaled layout when wx has no DPI conversion API."""
    selector = _open_selector(
        monkeypatch,
        {"size": [1100, 750], "column_widths": {"lcsc": 150}},
        supports_dip=False,
    )
    assert selector.part_list.GetColumns()[0].GetWidth() == 150
    assert selector.GetSize() == (1100, 750)
    selector.SetSize((1200, 800))
    selector.Close()
    saved = selector.parent.settings["partselector"]
    assert saved["size"] == [1200, 800]
    assert saved["column_widths"]["lcsc"] == 150
    selector.parent.save_settings.assert_called_once()


@pytest.mark.parametrize(
    "size",
    [
        [1100, 750],
        None,
        [],
        [900],
        [900, 700, 1],
        [0, 700],
        [900, -1],
        [True, 700],
        [900.5, 700],
        "900,700",
    ],
)
def test_selector_constructor_restores_only_valid_sizes(
    monkeypatch: pytest.MonkeyPatch, size: object
) -> None:
    """Apply only valid saved dimensions through the real constructor."""
    selector = _open_selector(monkeypatch, {"size": size})
    assert selector.GetSize() == ((2200, 1500) if size == [1100, 750] else (2800, 1600))
    selector.Layout.assert_called_once()


@pytest.mark.parametrize("close_path", ["selector", "main", "parent"])
def test_save_failure_still_destroys_windows_and_clears_selector(
    monkeypatch: pytest.MonkeyPatch,
    close_path: str,
) -> None:
    """A settings write error must not strand either window during teardown."""
    window = None if close_path == "selector" else _open_main(monkeypatch, {})
    selector = (
        None if close_path == "main" else _open_selector(monkeypatch, {}, parent=window)
    )
    owner = window if window is not None else selector.parent
    owner.save_settings.side_effect = OSError("settings file unavailable")

    (window if window is not None else selector).Close()

    owner.save_settings.assert_called_once()
    if selector is not None:
        selector.Destroy.assert_called_once()
        assert owner._part_selector is None
    if window is not None:
        window.Destroy.assert_called_once()


@pytest.mark.parametrize("fail_at", ["before_controls", "during_restore"])
def test_incomplete_selector_constructor_preserves_saved_layout(
    monkeypatch: pytest.MonkeyPatch,
    fail_at: str,
) -> None:
    """Skip layout persistence until selector controls and restoration are ready."""
    saved = {"size": [900, 700], "column_widths": {"lcsc": 150, "stock": 100}}
    selector = _open_selector(
        monkeypatch, json.loads(json.dumps(saved)), fail_at=fail_at
    )

    selector.Close()

    assert selector.parent.settings["partselector"] == saved
    selector.parent.save_settings.assert_not_called()
    selector.Destroy.assert_called_once()


def _maximize_with_animation(selector: Any) -> MagicMock:
    """Cocoa reports animation sizes before its maximized flag becomes true."""
    event = selector.emit(_wx["wx"].EVT_MAXIMIZE)
    selector.SetSize((3000, 1900))
    selector.SetSize((3400, 2200))
    selector.IsMaximized.return_value = True
    selector.SetSize((3600, 2400))
    return event


@pytest.mark.parametrize("during_restore", [False, True])
def test_closing_during_native_zoom_animation_preserves_normal_size(
    monkeypatch: pytest.MonkeyPatch,
    during_restore: bool,
) -> None:
    """Ignore intermediate Cocoa zoom sizes on both maximize and restore."""
    selector = _open_selector(monkeypatch, {"size": [900, 700]})
    selector.SetSize((2400, 1600))
    maximize_event = _maximize_with_animation(selector)
    if during_restore:
        _drain_callbacks()
        restore_event = selector.emit(_wx["wx"].EVT_MAXIMIZE)
        selector.IsMaximized.return_value = False
        selector.SetSize((3200, 2000))
    selector.Close()
    saved = json.loads(json.dumps(selector.parent.settings["partselector"]))
    assert saved["size"] == [1200, 800]
    maximize_event.Skip.assert_called_once()
    if during_restore:
        restore_event.Skip.assert_called_once()
    selector.GetSize.reset_mock()
    _drain_callbacks()
    selector.GetSize.assert_not_called()
    assert _open_selector(monkeypatch, saved).GetSize() == (2400, 1600)


def test_restored_size_callback_keeps_a_newer_normal_resize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deferred zoom completion must capture the latest normal size at execution."""
    selector = _open_selector(monkeypatch, {"size": [900, 700]})
    selector.SetSize((2400, 1600))
    _maximize_with_animation(selector)
    _drain_callbacks()
    selector.emit(_wx["wx"].EVT_MAXIMIZE)
    selector.IsMaximized.return_value = False
    selector.SetSize((3200, 2000))
    selector.SetSize((2400, 1600))
    selector.SetSize((2600, 1800))
    _drain_callbacks()
    selector.Close()
    saved = json.loads(json.dumps(selector.parent.settings["partselector"]))
    assert saved["size"] == [1300, 900]
    assert _open_selector(monkeypatch, saved).GetSize() == (2600, 1800)


def test_retarget_and_search_keep_the_current_size_and_column_widths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refresh real selector handlers after retargeting without resetting layout."""
    parent = _open_main(monkeypatch, {})
    parent.library = MagicMock(categories=[])
    db_row = {"LCSC Part": "C321", "Stock": "25", "Price": "1-:0.5"}
    parent.library.search.return_value = [
        tuple(db_row.get(field, "") for field in partselector.DB_FIELDS)
    ]

    def text_control(
        _parent: Any, _id: int, value: str, *_args: Any, **_kwargs: Any
    ) -> MagicMock:
        control = MagicMock()
        control.GetValue.return_value = value
        control.ChangeValue.side_effect = lambda new_value: setattr(
            control.GetValue, "return_value", new_value
        )
        return control

    monkeypatch.setattr(partselector.wx.TextCtrl, "side_effect", text_control)
    selector = _open_selector(monkeypatch, {}, parent=parent, real_search=True)
    selector.SetSize((2400, 1600))
    stock = _column_by_title(selector.part_list, "Stock")
    stock.SetWidth(360)
    widths = [column.GetWidth() for column in selector.part_list.GetColumns()]

    selector.update_for({"R1": "C321"})

    assert selector.keyword.GetValue() == "C321"
    assert parent.library.search.call_args.args[0]["keyword"] == "C321"
    assert selector.part_list_model.AddEntry.call_args.args[0][0] == "C321"
    assert selector.GetSize() == (2400, 1600)
    assert [column.GetWidth() for column in selector.part_list.GetColumns()] == widths


@pytest.mark.parametrize(
    ("saved_size", "expected"),
    [
        ([1, 1], (2200, 1200)),
        ([10000, 10000], (3840, 2160)),
        ([10**100, 10**100], (3840, 2160)),
        ([1280, 720], (2560, 1440)),
    ],
)
def test_selector_bounds_saved_size_before_native_conversion_and_reopens(
    monkeypatch: pytest.MonkeyPatch, saved_size: list[int], expected: tuple[int, int]
) -> None:
    """Unusable positive dimensions are corrected and the correction persists."""
    selector = _open_selector(monkeypatch, {"size": saved_size})
    assert selector.GetSize() == expected
    selector.Close()
    saved = json.loads(json.dumps(selector.parent.settings["partselector"]))
    assert saved["size"] == [value // 2 for value in expected]
    assert _open_selector(monkeypatch, saved).GetSize() == expected


@pytest.mark.parametrize("saved_size", [None, [1, 1], [1280, 720]])
def test_selector_fits_parents_smaller_secondary_monitor(
    monkeypatch: pytest.MonkeyPatch, saved_size: Optional[list[int]]
) -> None:
    """Bounds follow the parent's monitor, including defaults and a capped minimum."""
    _Display.areas = [(3840, 2160), (1600, 1000)]
    settings = {"size": saved_size}
    if saved_size == [1280, 720]:
        original = _open_selector(monkeypatch, settings)
        original.Close()
        settings = json.loads(json.dumps(original.parent.settings["partselector"]))
    selector = _open_selector(monkeypatch, settings, monitor=1)
    assert selector.GetSize() == (1600, 1000)
    selector.Close()
    assert selector.parent.settings["partselector"]["size"] == [800, 500]


def test_missing_parent_monitor_falls_back_to_primary_work_area(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parent outside every display uses the primary display's usable area."""
    _Display.areas = [(2400, 1400), (3840, 2160)]
    selector = _open_selector(monkeypatch, {}, monitor=-1)
    assert selector.GetSize() == (2400, 1400)


@pytest.mark.parametrize("areas", [[], [(0, -1)]])
def test_unavailable_work_area_uses_safe_default_instead_of_unbounded_saved_size(
    monkeypatch: pytest.MonkeyPatch, areas: list[tuple[int, int]]
) -> None:
    """Missing or invalid work areas cannot send arbitrary dimensions into wx."""
    _Display.areas = areas
    selector = _open_selector(monkeypatch, {"size": [10**100, 10**100]}, monitor=-1)
    assert selector.GetSize() == (2800, 1600)


@pytest.mark.parametrize("supports_dip", [True, False])
def test_work_area_clamp_remains_safe_after_fractional_or_legacy_dpi_conversion(
    monkeypatch: pytest.MonkeyPatch, supports_dip: bool
) -> None:
    """DIP rounding and older unscaled wx both keep the dialog inside its bounds."""
    _Display.areas = [(1999, 1199)]
    selector = _open_selector(
        monkeypatch,
        {"size": [10**100, 10**100]},
        scale=1.25,
        supports_dip=supports_dip,
    )
    width, height = selector.GetSize()
    assert 1997 <= width <= 1999
    assert 1197 <= height <= 1199
    selector.Close()
    saved = json.loads(json.dumps(selector.parent.settings["partselector"]))
    reopened = _open_selector(monkeypatch, saved, scale=1.25, supports_dip=supports_dip)
    assert reopened.GetSize() == (width, height)


@pytest.mark.parametrize("window_kind", ["main", "selector"])
@pytest.mark.parametrize("saved_width", [10000, 10**100])
def test_constructor_caps_columns_to_laid_out_table_and_persists_correction(
    monkeypatch: pytest.MonkeyPatch, window_kind: str, saved_width: int
) -> None:
    """A table has no usable client width until its surrounding layout runs."""
    if window_kind == "main":
        window = _open_main(
            monkeypatch, {"mainwindow": {"column_widths": {"FP_COL": saved_width}}}
        )
        control, key, title = window.footprint_list, "FP_COL", "Footprint"
    else:
        window = _open_selector(
            monkeypatch,
            {"size": [1100, 750], "column_widths": {"stock": saved_width}},
        )
        control, key, title = window.part_list, "stock", "Stock"
    column = _column_by_title(control, title)
    assert control.GetClientSize()[0] < window.GetSize()[0]
    assert column.GetWidth() == control.GetClientSize()[0]
    window.Close()
    settings = (
        window.settings["mainwindow"]
        if window_kind == "main"
        else window.parent.settings["partselector"]
    )
    assert settings["column_widths"][key] == control.GetClientSize()[0] // 2
    saved = json.loads(json.dumps(settings))
    if window_kind == "main":
        reopened = _open_main(monkeypatch, {"mainwindow": saved}).footprint_list
    else:
        reopened = _open_selector(monkeypatch, saved).part_list
    restored = _column_by_title(reopened, title)
    assert restored.GetWidth() == column.GetWidth()


def test_column_cap_handles_fractional_dpi_without_native_integer_overflow() -> None:
    """Clamp before native conversion and preserve valid widths on fractional DPI."""
    control = _control(
        [_column(0, 100), _column(1, 100)], scale=1.25, client_width=1999
    )
    layout.restore_column_widths(
        control, {"stock": 10**100, "price": 75}, {0: "stock", 1: "price"}
    )
    widths = [column.GetWidth() for column in control.GetColumns()]
    assert 1997 <= widths[0] <= 1999
    assert widths[1] == 94


def test_unlaid_out_control_keeps_default_widths() -> None:
    """A zero client width must not collapse columns before layout has run."""
    control = _control([_column(0, 100)], client_width=0)
    layout.restore_column_widths(control, {"stock": 10**100}, {0: "stock"})
    column = control.GetColumns()[0]
    assert column.GetWidth() == 100
    column.SetWidth.assert_not_called()


@pytest.mark.parametrize("capture", ["size", "maximize", "callback"])
def test_fullscreen_geometry_never_replaces_last_normal_size(
    monkeypatch: pytest.MonkeyPatch, capture: str
) -> None:
    """Every event path that records dimensions must exclude fullscreen."""
    selector = _open_selector(monkeypatch, {"size": [1280, 720]})
    if capture == "callback":
        selector.emit(_wx["wx"].EVT_MAXIMIZE)
    selector.IsFullScreen.return_value = True
    # The native window has already changed size before the selected handler runs.
    selector._size = (3840, 2160)
    if capture == "callback":
        _drain_callbacks()
    else:
        selector.emit(
            _wx["wx"].EVT_SIZE if capture == "size" else _wx["wx"].EVT_MAXIMIZE
        )
    selector.Close()
    saved = json.loads(json.dumps(selector.parent.settings["partselector"]))
    assert saved["size"] == [1280, 720]
    assert _open_selector(monkeypatch, saved).GetSize() == (2560, 1440)


@pytest.mark.parametrize("close_path", ["selector", "main", "parent"])
@pytest.mark.parametrize("failure", ["capture", "serialization"])
def test_unexpected_persistence_failure_cannot_strand_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, close_path: str, failure: str
) -> None:
    """Capture and real JSON serialization failures always finish window teardown."""
    window = None if close_path == "selector" else _open_main(monkeypatch, {})
    selector = (
        None if close_path == "main" else _open_selector(monkeypatch, {}, parent=window)
    )
    owner = window if window is not None else selector.parent
    if failure == "capture":
        control = window.footprint_list if window is not None else selector.part_list
        control.GetColumns.side_effect = RuntimeError("layout capture failed")
    else:
        previous = b'{"general": {"highlight": true}, "gerber": {}}'
        (tmp_path / "settings.json").write_bytes(previous)
        monkeypatch.setattr(mainwindow, "PLUGIN_PATH", tmp_path)
        owner.save_settings = mainwindow.JLCPCBTools.save_settings.__get__(owner)
        owner.settings["invalid"] = object()
    with pytest.raises(RuntimeError if failure == "capture" else TypeError):
        (window if window is not None else selector).Close()
    if selector is not None:
        selector.Destroy.assert_called_once()
        assert owner._part_selector is None
    if window is not None:
        window.Destroy.assert_called_once()
    if failure == "serialization":
        assert (tmp_path / "settings.json").read_bytes() == previous
        assert [path.name for path in tmp_path.iterdir()] == ["settings.json"]


def test_selector_capture_failure_also_destroys_its_parent_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child close failure cannot skip the main frame's teardown."""
    window = _open_main(monkeypatch, {})
    selector = _open_selector(monkeypatch, {}, parent=window)
    selector.part_list.GetColumns.side_effect = RuntimeError("layout capture failed")
    with pytest.raises(RuntimeError, match="layout capture failed"):
        window.Close()
    assert window._part_selector is None
    selector.Destroy.assert_called_once()
    window.Destroy.assert_called_once()
