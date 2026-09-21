"""Regression tests for Issue #854: UI label clipping and toolbar truncation on GTK.

Verifies:
1. CorrectionManagerDialog labels and text inputs use unconstrained heights (-1)
   instead of hardcoded 15px / 24px sizes.
2. JLCPCBTools.right_toolbar is added to table_sizer with proportion=0 to prevent
   GTK layout compression.
3. JLCPCBTools.right_toolbar calculates an adequate minimum width based on label
   text extents and DPI scaling.
4. PartSelectorDialog labels and search inputs use unconstrained heights (-1)
   instead of hardcoded 15px / 24px sizes.
"""

from collections import defaultdict
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from .wx_harness import (
    load,
    load_correction_modules,
    load_mainwindow,
    mainwindow_stubs,
    wx_stubs,
)

_board_path: Optional[str] = None


@pytest.fixture(autouse=True)
def _setup_board_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    board_file = tmp_path_factory.mktemp("issue854-board") / "test.kicad_pcb"
    board_file.write_text("(kicad_pcb)\n", encoding="utf-8")
    monkeypatch.setattr(f"{__name__}._board_path", str(board_file))


class _Size(tuple):
    def __new__(cls, *values: int) -> "_Size":
        return super().__new__(cls, values)

    def GetWidth(self) -> int:
        return self[0]

    def GetHeight(self) -> int:
        return self[1]


@dataclass
class _SizerItem:
    child: Any
    proportion: int
    flag: int
    border: int


class _Sizer:
    def __init__(self, orientation: int = 0, *_args: Any) -> None:
        self.orientation = orientation
        self.items: list[_SizerItem] = []
        self.min_size: Optional[tuple] = None

    def Add(
        self, child: Any, proportion: int = 0, flag: int = 0, border: int = 0
    ) -> _SizerItem:
        item = _SizerItem(child, proportion, flag, border)
        self.items.append(item)
        return item

    def SetMinSize(self, size: tuple) -> None:
        self.min_size = size

    def AddStretchSpacer(self, proportion: int = 1) -> _SizerItem:
        return self.Add((0, 0), proportion)

    def contains(self, child: Any) -> bool:
        return any(
            item.child is child
            or isinstance(item.child, _Sizer)
            and item.child.contains(child)
            for item in self.items
        )


class _TopLevelWindow:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self._children: list[Any] = []
        self._sizer: Optional[_Sizer] = None
        self.scale_factor = 1
        self.display_index = 0
        self.GetSize = MagicMock(return_value=_Size(1200, 800))
        self.SetSize = MagicMock()
        self.IsMaximized = MagicMock(return_value=False)
        self.IsIconized = MagicMock(return_value=False)
        self.IsFullScreen = MagicMock(return_value=False)
        self.FromDIP = lambda value: value
        self.ToDIP = lambda value: value
        for name in ("SetAcceleratorTable", "SetSizeHints", "Centre", "Destroy"):
            setattr(self, name, MagicMock())

    def SetEscapeId(self, escape_id: int) -> None:
        self._escape_id = escape_id

    def GetEscapeId(self) -> int:
        return getattr(self, "_escape_id", -1)

    def Bind(self, *args: Any, **kwargs: Any) -> None:
        pass

    def SetSizer(self, sizer: Optional[_Sizer]) -> None:
        self._sizer = sizer

    def GetSizer(self) -> Optional[_Sizer]:
        return self._sizer

    def Layout(self) -> bool:
        return True


class _Dialog(_TopLevelWindow):
    pass


class _Frame(_TopLevelWindow):
    pass


class _Panel(_TopLevelWindow):
    pass


class _Display:
    areas = [(3840, 2160)]

    def __init__(self, index: int = 0) -> None:
        self.index = index

    @staticmethod
    def GetFromWindow(window: Any) -> int:
        return getattr(window, "display_index", 0)

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


class _ToolBar:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.min_size: Optional[_Size] = None
        self.tools: dict[int, Any] = {}
        self._text_extents: dict[str, tuple[int, int]] = {}

    def AddTool(self, *args: Any, **kwargs: Any) -> Any:
        tool = MagicMock()
        tool_id = args[0] if args else kwargs.get("toolId", 0)
        self.tools[tool_id] = tool
        return tool

    def AddControl(self, *args: Any, **kwargs: Any) -> None:
        pass

    def AddSeparator(self) -> None:
        pass

    def AddStretchableSpace(self) -> None:
        pass

    def AddCheckTool(self, *args: Any, **kwargs: Any) -> Any:
        tool = MagicMock()
        tool_id = args[0] if args else kwargs.get("toolId", 0)
        self.tools[tool_id] = tool
        return tool

    def ToggleTool(self, *args: Any, **kwargs: Any) -> None:
        pass

    def Realize(self) -> None:
        pass

    def SetMinSize(self, size: Any) -> None:
        self.min_size = size

    def GetMinSize(self) -> Optional[_Size]:
        return self.min_size

    def GetTextExtent(self, text: str) -> tuple[int, int]:
        return self._text_extents.get(text, (len(text) * 8, 16))

    def Enable(self, *args: Any, **kwargs: Any) -> None:
        pass

    def EnableTool(self, *args: Any, **kwargs: Any) -> None:
        pass

    def Bind(self, *args: Any, **kwargs: Any) -> None:
        pass


def test_corrections_dialog_unconstrained_label_and_input_heights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CorrectionManagerDialog labels and TextCtrls must use height=-1, not fixed 15/24px."""
    created_labels: dict[str, Any] = {}
    created_inputs: list[tuple[str, Any]] = []

    def mock_static_text(
        parent: Any, id_: int, label: str, *args: Any, **kwargs: Any
    ) -> Any:
        created_labels[label] = kwargs.get("size")
        ctrl = MagicMock()
        ctrl.label = label
        return ctrl

    def mock_text_ctrl(
        parent: Any, id_: int, value: str, *args: Any, **kwargs: Any
    ) -> Any:
        size = kwargs.get("size")
        if size is None and len(args) >= 2:
            size = args[1]
        created_inputs.append((value, size))
        ctrl = MagicMock()
        ctrl.value = value
        return ctrl

    custom_wx = wx_stubs(
        Dialog=type("Dialog", (), {}),
        StaticText=mock_static_text,
        TextCtrl=mock_text_ctrl,
        Button=MagicMock(),
        CheckBox=MagicMock(),
        Size=_Size,
        DefaultPosition=(-1, -1),
        DefaultSize=(-1, -1),
        NOT_FOUND=-1,
        ToolTip=lambda text: text,
        PostEvent=MagicMock(),
        MessageBox=MagicMock(),
        MessageDialog=MagicMock(),
        NewId=lambda: 1,
        AcceleratorEntry=MagicMock(),
        AcceleratorTable=MagicMock(),
    )
    custom_wx["wx.dataview"].DataViewListCtrl = MagicMock()

    with load_correction_modules(wx=custom_wx) as modules:
        wx = modules.wx

        for name in ("Bind", "SetAcceleratorTable", "SetSizer", "Layout", "Centre"):
            monkeypatch.setattr(wx.Dialog, name, MagicMock(), raising=False)
        monkeypatch.setattr(
            wx.Dialog, "__init__", lambda *args, **kwargs: None, raising=False
        )
        monkeypatch.setattr(
            wx,
            "BoxSizer",
            lambda orientation=0, *args: MagicMock(),
            raising=False,
        )
        monkeypatch.setattr(
            wx,
            "StaticBoxSizer",
            lambda *args, **kwargs: MagicMock(),
            raising=False,
        )
        monkeypatch.setattr(wx, "ToolTip", lambda text: text, raising=False)
        monkeypatch.setattr(
            modules.corrections, "HighResWxSize", lambda _win, size: size
        )
        monkeypatch.setattr(modules.corrections, "loadBitmapScaled", MagicMock())

        parent = SimpleNamespace(
            window=object(),
            scale_factor=1,
            library=MagicMock(get_all_correction_data=MagicMock(return_value=[])),
        )

        _ = modules.corrections.CorrectionManagerDialog(parent, "TestFootprint")

        # Verify labels: "Regex", "Rotation", "Offset X", "Offset Y"
        expected_labels = ["Regex", "Rotation", "Offset X", "Offset Y"]
        for label_name in expected_labels:
            assert label_name in created_labels, f"Label {label_name} was not created"
            label_size = created_labels[label_name]
            assert label_size is not None
            # Height must be -1 (unconstrained), NOT 15
            assert label_size[1] == -1, (
                f"Label '{label_name}' height was {label_size[1]}, expected -1"
            )

        # Verify text controls
        for val, input_size in created_inputs:
            assert input_size is not None
            # Height must be -1 (unconstrained), NOT 24
            assert input_size[1] == -1, (
                f"TextCtrl for '{val}' height was {input_size[1]}, expected -1"
            )


def _create_mainwindow_test_env(
    monkeypatch: pytest.MonkeyPatch,
    scale_factor: int = 1,
    custom_extents: Optional[dict[str, tuple[int, int]]] = None,
) -> tuple[Any, Any]:
    """Instantiate JLCPCBTools in a stubbed wx environment."""
    pkg = "ui_sizing_tests"

    toolbar_instance = _ToolBar()
    if custom_extents:
        toolbar_instance._text_extents.update(custom_extents)

    stubs = wx_stubs(
        Dialog=_Dialog,
        Frame=_Frame,
        Panel=_Panel,
        BoxSizer=_Sizer,
        StaticBoxSizer=_Sizer,
        Size=_Size,
        DefaultPosition=(-1, -1),
        DefaultSize=(-1, -1),
        NOT_FOUND=-1,
        EmptyString="",
        NullBitmap=None,
        NewId=lambda: 1,
        NewIdRef=lambda: 1,
        GetApp=lambda: True,
        CallAfter=lambda fn, *args: None,
        GetTopLevelParent=lambda win: win,
        ToolBar=lambda *args, **kwargs: toolbar_instance,
        **{
            name: MagicMock()
            for name in (
                "Timer StaticText TextCtrl Button ComboBox CheckBox Gauge "
                "ScrolledWindow AcceleratorEntry AcceleratorTable"
            ).split()
        },
    )
    stubs["wx"].DEFAULT_FRAME_STYLE = 0
    stubs["wx.adv"].BitmapComboBox = MagicMock()
    stubs["wx.dataview"].PyDataViewModel = object
    stubs["wx.dataview"].DataViewCustomRenderer = object
    data_view_ctrl = MagicMock()
    data_view_ctrl.GetColumns.return_value = []
    data_view_ctrl.GetClientSize.return_value = _Size(1000, 600)
    data_view_ctrl.ToDIP = lambda val: val
    data_view_ctrl.FromDIP = lambda val: val
    stubs["wx.dataview"].DataViewCtrl = MagicMock(return_value=data_view_ctrl)
    stubs["wx.dataview"].DataViewColumn = MagicMock()

    helpers_module = load(pkg, "helpers", stubs)
    helpers_override = {
        "PLUGIN_PATH": helpers_module.PLUGIN_PATH,
        "GetScaleFactor": lambda _window: scale_factor,
        "HighResWxSize": lambda _win, size: _Size(
            size[0] * scale_factor if size[0] > 0 else size[0],
            size[1] * scale_factor if size[1] > 0 else size[1],
        ),
        "getVersion": lambda: "test",
        "loadBitmapScaled": lambda *_args: None,
    }

    mw_module = load_mainwindow(
        pkg,
        wx=stubs,
        helpers=helpers_override,
        datamodel={
            "PartListDataModel": MagicMock(columns=defaultdict(int)),
            "STANDARD_ONLY_TOOLTIP": "",
        },
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

    monkeypatch.setattr(
        mw_module.JLCPCBTools,
        "load_settings",
        lambda self: setattr(self, "settings", {}),
    )
    monkeypatch.setattr(mw_module.JLCPCBTools, "init_logger", lambda self: None)
    monkeypatch.setattr(mw_module.JLCPCBTools, "init_data", lambda self: None)

    provider = MagicMock()
    assert _board_path is not None
    provider.get_pcbnew().GetBoard().GetFileName.return_value = _board_path

    window = object.__new__(mw_module.JLCPCBTools)
    mw_module.JLCPCBTools.__init__(window, None, provider)
    window.library = SimpleNamespace(categories=[])

    return window, toolbar_instance


def test_mainwindow_right_toolbar_sizer_proportion_is_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """right_toolbar must be added to table_sizer with proportion=0 (not 1)."""
    window, toolbar = _create_mainwindow_test_env(monkeypatch)

    # Walk the window's sizer hierarchy to locate table_sizer containing right_toolbar
    frame_sizer = window.GetSizer()
    assert frame_sizer is not None

    found_toolbar_item: Optional[_SizerItem] = None

    def search_sizer(sizer: _Sizer) -> None:
        nonlocal found_toolbar_item
        for item in sizer.items:
            if item.child is toolbar or item.child is window.right_toolbar:
                found_toolbar_item = item
                return
            if isinstance(item.child, _Sizer):
                search_sizer(item.child)
            elif hasattr(item.child, "GetSizer") and item.child.GetSizer() is not None:
                search_sizer(item.child.GetSizer())

    search_sizer(frame_sizer)

    assert found_toolbar_item is not None, (
        "right_toolbar was not found in any sizer item"
    )
    # The fix ensures proportion=0 so the toolbar doesn't stretch or compress
    assert found_toolbar_item.proportion == 0, (
        f"Expected toolbar proportion=0, got {found_toolbar_item.proportion}"
    )


def test_mainwindow_right_toolbar_min_size_calculation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """right_toolbar SetMinSize must enforce at least 170px width (DPI-scaled)."""
    # 1x scale: base floor of 170px
    _window, toolbar = _create_mainwindow_test_env(monkeypatch, scale_factor=1)
    min_size = toolbar.GetMinSize()
    assert min_size is not None, "SetMinSize was not called on right_toolbar"
    assert min_size[0] >= 170, f"Expected min_width >= 170, got {min_size[0]}"
    assert min_size[1] == -1, f"Expected min_height == -1, got {min_size[1]}"

    # HiDPI 2x scale: scaled floor of 340px
    _window_2x, toolbar_2x = _create_mainwindow_test_env(monkeypatch, scale_factor=2)
    min_size_2x = toolbar_2x.GetMinSize()
    assert min_size_2x is not None, "SetMinSize was not called on 2x right_toolbar"
    assert min_size_2x[0] >= 340, (
        f"Expected min_width >= 340 at 2x scale, got {min_size_2x[0]}"
    )
    assert min_size_2x[1] == -1, f"Expected min_height == -1, got {min_size_2x[1]}"


def test_mainwindow_right_toolbar_adapts_to_large_text_extent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """right_toolbar SetMinSize expands if label text extent requires > 170px."""
    # Simulate a wide font/GTK theme where the longest label is 180px
    extents = {
        "Export to schematic": (180, 16),
        "Assign LCSC number": (150, 16),
    }
    _window, toolbar = _create_mainwindow_test_env(
        monkeypatch, scale_factor=1, custom_extents=extents
    )

    min_size = toolbar.GetMinSize()
    assert min_size is not None
    # With 180px text + 24px padding = 204px, which exceeds 170
    assert min_size[0] >= 204, f"Expected min_width >= 204, got {min_size[0]}"


class _NativeWxSizeStub:
    """Non-tuple wx.Size object returned by real wx.Window.GetTextExtent."""

    def __init__(self, width: int, height: int) -> None:
        self.x = width
        self.y = height
        self.width = width
        self.height = height

    def GetWidth(self) -> int:
        return self.x

    def GetHeight(self) -> int:
        return self.y


def test_mainwindow_right_toolbar_adapts_to_wx_size_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """right_toolbar SetMinSize accepts real wx.Size objects (not tuples)."""
    extents = {
        "Export to schematic": _NativeWxSizeStub(190, 16),
        "Assign LCSC number": _NativeWxSizeStub(150, 16),
    }
    _window, toolbar = _create_mainwindow_test_env(
        monkeypatch, scale_factor=1, custom_extents=extents
    )

    min_size = toolbar.GetMinSize()
    assert min_size is not None
    # 190px text + 24px padding = 214px
    assert min_size[0] >= 214, f"Expected min_width >= 214, got {min_size[0]}"


def test_mainwindow_right_toolbar_nonnumeric_text_extent_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """right_toolbar safely falls back to 170 DIP if GetTextExtent returns mocks or non-numerics."""
    pkg = "ui_sizing_tests_mock"

    toolbar_instance = _ToolBar()
    # Mock GetTextExtent returning a MagicMock (like in test_window_layout)
    toolbar_instance.GetTextExtent = MagicMock(return_value=MagicMock())

    stubs = wx_stubs(
        Dialog=_Dialog,
        Frame=_Frame,
        Panel=_Panel,
        BoxSizer=_Sizer,
        StaticBoxSizer=_Sizer,
        Size=_Size,
        DefaultPosition=(-1, -1),
        DefaultSize=(-1, -1),
        NOT_FOUND=-1,
        EmptyString="",
        NullBitmap=None,
        NewId=lambda: 1,
        NewIdRef=lambda: 1,
        GetApp=lambda: True,
        CallAfter=lambda fn, *args: None,
        GetTopLevelParent=lambda win: win,
        ToolBar=lambda *args, **kwargs: toolbar_instance,
        **{
            name: MagicMock()
            for name in (
                "Timer StaticText TextCtrl Button ComboBox CheckBox Gauge "
                "ScrolledWindow AcceleratorEntry AcceleratorTable"
            ).split()
        },
    )
    stubs["wx"].DEFAULT_FRAME_STYLE = 0
    stubs["wx.adv"].BitmapComboBox = MagicMock()
    stubs["wx.dataview"].PyDataViewModel = object
    stubs["wx.dataview"].DataViewCustomRenderer = object
    data_view_ctrl = MagicMock()
    data_view_ctrl.GetColumns.return_value = []
    data_view_ctrl.GetClientSize.return_value = _Size(1000, 600)
    data_view_ctrl.ToDIP = lambda val: val
    data_view_ctrl.FromDIP = lambda val: val
    stubs["wx.dataview"].DataViewCtrl = MagicMock(return_value=data_view_ctrl)
    stubs["wx.dataview"].DataViewColumn = MagicMock()

    helpers_module = load(pkg, "helpers", stubs)
    helpers_override = {
        "PLUGIN_PATH": helpers_module.PLUGIN_PATH,
        "GetScaleFactor": lambda _window: 1,
        "HighResWxSize": lambda _win, size: size,
        "getVersion": lambda: "test",
        "loadBitmapScaled": lambda *_args: None,
    }

    mw_module = load_mainwindow(
        pkg,
        wx=stubs,
        helpers=helpers_override,
        datamodel={
            "PartListDataModel": MagicMock(columns=defaultdict(int)),
            "STANDARD_ONLY_TOOLTIP": "",
        },
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

    monkeypatch.setattr(
        mw_module.JLCPCBTools,
        "load_settings",
        lambda self: setattr(self, "settings", {}),
    )
    monkeypatch.setattr(mw_module.JLCPCBTools, "init_logger", lambda self: None)
    monkeypatch.setattr(mw_module.JLCPCBTools, "init_data", lambda self: None)

    provider = MagicMock()
    assert _board_path is not None
    provider.get_pcbnew().GetBoard().GetFileName.return_value = _board_path

    window = object.__new__(mw_module.JLCPCBTools)
    mw_module.JLCPCBTools.__init__(window, None, provider)

    min_size = toolbar_instance.GetMinSize()
    assert min_size is not None
    # Must safely fall back to 170 without error
    assert min_size[0] == 170
    assert min_size[1] == -1


def test_partselector_dialog_unconstrained_label_and_input_heights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PartSelectorDialog labels and search inputs must use height=-1, not fixed 15/24px."""
    pkg = "partselector_sizing_tests"

    created_labels: dict[str, Any] = {}
    created_inputs: list[tuple[str, Any]] = []

    def mock_static_text(
        parent: Any, id_: int, label: str, *args: Any, **kwargs: Any
    ) -> Any:
        created_labels[label] = kwargs.get("size")
        ctrl = MagicMock()
        ctrl.label = label
        return ctrl

    def mock_text_ctrl(
        parent: Any, id_: int, value: str, *args: Any, **kwargs: Any
    ) -> Any:
        size = kwargs.get("size")
        if size is None and len(args) >= 2:
            size = args[1]
        created_inputs.append((value, size))
        ctrl = MagicMock()
        ctrl.value = value
        return ctrl

    def mock_combo_box(
        parent: Any, id_: int, value: str, *args: Any, **kwargs: Any
    ) -> Any:
        size = kwargs.get("size")
        if size is None and len(args) >= 2:
            size = args[1]
        created_inputs.append((value, size))
        ctrl = MagicMock()
        ctrl.value = value
        return ctrl

    def mock_check_box(
        parent: Any, id_: int, label: str, *args: Any, **kwargs: Any
    ) -> Any:
        size = kwargs.get("size")
        if size is None and len(args) >= 2:
            size = args[1]
        created_inputs.append((label, size))
        ctrl = MagicMock()
        ctrl.label = label
        return ctrl

    stubs = wx_stubs(
        Dialog=_Dialog,
        Frame=_Frame,
        Panel=_Panel,
        BoxSizer=_Sizer,
        StaticBoxSizer=_Sizer,
        Size=_Size,
        Display=_Display,
        DefaultPosition=(-1, -1),
        DefaultSize=(-1, -1),
        NOT_FOUND=-1,
        EmptyString="",
        NullBitmap=None,
        NewId=lambda: 1,
        NewIdRef=lambda: 1,
        GetApp=lambda: True,
        CallAfter=lambda fn, *args: None,
        GetTopLevelParent=lambda win: win,
        StaticText=mock_static_text,
        TextCtrl=mock_text_ctrl,
        ComboBox=mock_combo_box,
        CheckBox=mock_check_box,
        Button=MagicMock(),
        Timer=MagicMock(),
        ScrolledWindow=MagicMock(),
        AcceleratorEntry=MagicMock(),
        AcceleratorTable=MagicMock(),
    )
    stubs["wx"].DEFAULT_DIALOG_STYLE = 0
    stubs["wx.dataview"].PyDataViewModel = object
    stubs["wx.dataview"].DataViewCustomRenderer = object
    data_view_ctrl = MagicMock()
    data_view_ctrl.GetColumns.return_value = []
    data_view_ctrl.GetClientSize.return_value = _Size(1000, 600)
    data_view_ctrl.ToDIP = lambda val: val
    data_view_ctrl.FromDIP = lambda val: val
    stubs["wx.dataview"].DataViewCtrl = MagicMock(return_value=data_view_ctrl)
    stubs["wx.dataview"].DataViewColumn = MagicMock()

    helpers_module = load(pkg, "helpers", stubs)
    helpers_override = {
        "PLUGIN_PATH": helpers_module.PLUGIN_PATH,
        "GetScaleFactor": lambda _window: 1,
        "HighResWxSize": lambda _win, size: size,
        "getVersion": lambda: "test",
        "loadBitmapScaled": lambda *_args: None,
    }

    full_stubs = mainwindow_stubs(
        pkg,
        wx=stubs,
        helpers=helpers_override,
        datamodel={"PartSelectorDataModel": MagicMock()},
        dataview_highlight={
            "HighlightedTextRenderer": MagicMock(),
            "decode_highlighted_value": lambda value: (value, []),
            "simplify_footprint_name": lambda value: value,
        },
    )

    ps_module = load(pkg, "partselector", full_stubs)
    monkeypatch.setattr(ps_module.PartSelectorDialog, "search", MagicMock())

    parent = SimpleNamespace(
        window=_Dialog(),
        display_index=0,
        scale_factor=1,
        settings={"partselector": {}},
        library=SimpleNamespace(categories=[]),
        is_catalog_available=lambda: True,
        save_settings=MagicMock(),
        _part_selector=None,
    )

    _ = ps_module.PartSelectorDialog(parent, {})

    # Expected labels
    expected_labels = [
        "Keywords",
        "Manufacturer",
        "Package",
        "Category",
        "Part number",
        "Solder joints",
        "Subcategory",
        "Include basic parts",
        "Include preferred parts",
        "Include extended parts",
        "Only show parts in stock",
    ]
    for label_name in expected_labels:
        assert label_name in created_labels, f"Label {label_name} was not created"
        size = created_labels[label_name]
        assert size is not None
        assert size[1] == -1, f"Label '{label_name}' height was {size[1]}, expected -1"

    # All created inputs must have height == -1
    assert len(created_inputs) >= 11
    for name, size in created_inputs:
        assert size is not None
        assert size[1] == -1, f"Input '{name}' height was {size[1]}, expected -1"
