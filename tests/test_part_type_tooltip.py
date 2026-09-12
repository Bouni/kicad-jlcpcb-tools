"""Construct the fee legend with stateful wx sizing and appearance controls."""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any

import pytest

from .wx_harness import load, wx_stubs


@dataclass
class _Font:
    size: int = 12
    bold: bool = False

    def Bold(self) -> _Font:
        """Return a bold copy, leaving the inherited font unchanged."""
        return replace(self, bold=True)


class _Window:
    def __init__(
        self, parent: _Window | None = None, *, flags: int = 0, label: str = ""
    ) -> None:
        self.parent = parent
        self.flags = flags
        self.label = label
        self.children: list[_Window] = []
        self.shown = False
        self.destroyed = False
        self.font = _Font()
        self.scale = parent.scale if parent else 1
        self.background = None
        self.foreground = None
        self.sizer = None
        self.size = (0, 0)
        if parent:
            parent.children.append(self)

    def GetFont(self) -> _Font:
        """Return the current font rather than a fixed constructor default."""
        return self.font

    def SetFont(self, font: _Font) -> None:
        """Store the font used by subsequent size calculations."""
        self.font = font

    def SetBackgroundColour(self, colour: str) -> None:
        """Store the effective background colour."""
        self.background = colour

    def SetForegroundColour(self, colour: str) -> None:
        """Store the effective text colour."""
        self.foreground = colour

    def FromDIP(self, value: int) -> int:
        """Apply the parent display scale to spacing."""
        return value * self.scale

    def Hide(self) -> None:
        """Keep the popup hidden until the hover controller shows it."""
        self.shown = False

    def Destroy(self) -> None:
        """Destroy owned children and remove this window from its parent."""
        for child in list(self.children):
            child.Destroy()
        self.shown = False
        self.destroyed = True
        if self.parent is not None and self in self.parent.children:
            self.parent.children.remove(self)

    def GetBestSize(self) -> tuple[int, int]:
        """Size static text from its current font and every line of its label."""
        lines = self.label.splitlines()
        return max(map(len, lines)) * self.font.size, len(lines) * self.font.size

    def SetSizerAndFit(self, sizer: _Sizer) -> None:
        """Compute the popup size from its constructed child layout."""
        self.sizer = sizer
        self.size = sizer.GetMinSize()


class _Sizer:
    def __init__(
        self,
        orientation: int = 0,
        *,
        cols: int = 1,
        vgap: int = 0,
        hgap: int = 0,
    ) -> None:
        self.orientation = orientation
        self.cols = cols
        self.vgap = vgap
        self.hgap = hgap
        self.children: list[tuple[Any, int]] = []

    def Add(
        self, child: Any, proportion: int = 0, flag: int = 0, border: int = 0
    ) -> None:
        """Retain child objects so layout reads their latest text and fonts."""
        self.children.append((child, border))

    def GetMinSize(self) -> tuple[int, int]:
        """Calculate table column maxima and row heights including padding."""
        sizes = []
        for child, border in self.children:
            size = (
                child.GetMinSize() if isinstance(child, _Sizer) else child.GetBestSize()
            )
            sizes.append((size[0] + border * 2, size[1] + border * 2))
        widths = [
            max(size[0] for size in sizes[col :: self.cols]) for col in range(self.cols)
        ]
        heights = [
            max(size[1] for size in sizes[start : start + self.cols])
            for start in range(0, len(sizes), self.cols)
        ]
        return (
            sum(widths) + self.hgap * (self.cols - 1),
            sum(heights) + self.vgap * (len(heights) - 1),
        )


def _prepare(*, font_size: int = 12, scale: int = 1) -> SimpleNamespace:
    """Load the real factory with an owner and stateful wx controls."""
    colours = {}
    wx = wx_stubs(
        PopupWindow=_Window,
        StaticText=_Window,
        BoxSizer=_Sizer,
        FlexGridSizer=_Sizer,
        SystemSettings=SimpleNamespace(GetColour=colours.__getitem__),
    )
    colours[wx["wx"].SYS_COLOUR_INFOBK] = "tooltip background"
    colours[wx["wx"].SYS_COLOUR_INFOTEXT] = "tooltip foreground"
    helper = load("type_fee_popup_tests", "part_type_tooltip", wx)
    parent = _Window()
    parent.font = _Font(font_size)
    parent.scale = scale
    return SimpleNamespace(parent=parent, helper=helper)


def _construct(*, font_size: int = 12, scale: int = 1) -> SimpleNamespace:
    """Create a real legend using strictly declared, stateful wx controls."""
    ui = _prepare(font_size=font_size, scale=scale)
    ui.popup = ui.helper.create_type_fee_popup(ui.parent)
    return ui


def test_tooltip_constructs_the_approved_two_column_fee_table() -> None:
    """All types have exact approved fee explanations without eligibility help."""
    ui = _construct()
    cells = ui.popup.children
    rows = [
        tuple(" ".join(cell.label.split()) for cell in cells[start : start + 2])
        for start in range(0, len(cells), 2)
    ]
    assert rows == [
        ("Type", "Feeder loading fee"),
        ("Basic", "None for Economic assembly; yes for Standard assembly"),
        ("Preferred", "None for Economic assembly; yes for Standard assembly"),
        ("Extended", "Yes"),
        ("Blank", "No assigned part or type information available"),
    ]
    assert ui.popup.sizer.children[0][0].cols == 2
    assert ui.popup.parent is ui.parent
    assert not ui.popup.shown
    assert all(cell.parent is ui.popup for cell in cells)


@pytest.mark.parametrize("font_size,scale", [(12, 1), (18, 2)])
def test_tooltip_uses_parent_font_and_system_tooltip_colours(
    font_size: int, scale: int
) -> None:
    """Headings are distinct without changing the parent font or theme."""
    ui = _construct(font_size=font_size, scale=scale)
    for window in [ui.popup, *ui.popup.children]:
        assert window.font.size == font_size
        assert window.background == "tooltip background"
        assert window.foreground == "tooltip foreground"
    assert [cell.font.bold for cell in ui.popup.children] == [True, True] + [False] * 8
    assert not ui.parent.font.bold


def test_tooltip_fits_content_and_scales_without_fixed_column_widths() -> None:
    """Larger inherited fonts and DPI result in larger fully fitted popups."""
    normal = _construct()
    scaled = _construct(font_size=24, scale=2)
    assert normal.popup.size[0] > 0
    assert normal.popup.size[1] > 0
    assert scaled.popup.size == tuple(value * 2 for value in normal.popup.size)
    assert normal.popup.size == normal.popup.sizer.GetMinSize()
    assert scaled.popup.size == scaled.popup.sizer.GetMinSize()
    assert normal.popup is not scaled.popup


@pytest.mark.parametrize("failure_step", ["StaticText", "SetSizerAndFit"])
def test_construction_failure_destroys_popup_and_children_before_propagating(
    monkeypatch: pytest.MonkeyPatch, failure_step: str
) -> None:
    """A partially built legend must not remain owned by the working dialog."""
    ui = _prepare()
    sibling = _Window(ui.parent, label="Existing dialog control")
    popups: list[_Window] = []
    labels: list[_Window] = []
    failure = RuntimeError(f"Failed during {failure_step}")

    def create_popup(parent: _Window, *, flags: int = 0) -> _Window:
        popup = _Window(parent, flags=flags)
        popups.append(popup)
        return popup

    def create_label(parent: _Window, *, label: str) -> _Window:
        if failure_step == "StaticText" and labels:
            raise failure
        window = _Window(parent, label=label)
        labels.append(window)
        return window

    original_fit = _Window.SetSizerAndFit

    def fail_fit(window: _Window, sizer: _Sizer) -> None:
        original_fit(window, sizer)
        raise failure

    monkeypatch.setattr(ui.helper.wx, "PopupWindow", create_popup)
    monkeypatch.setattr(ui.helper.wx, "StaticText", create_label)
    if failure_step == "SetSizerAndFit":
        monkeypatch.setattr(_Window, "SetSizerAndFit", fail_fit)

    with pytest.raises(RuntimeError) as raised:
        ui.helper.create_type_fee_popup(ui.parent)

    assert raised.value is failure
    assert len(popups) == 1
    assert labels, "Failure must happen after child controls have been allocated"
    assert popups[0].destroyed
    assert all(label.destroyed for label in labels)
    assert ui.parent.children == [sibling]
    assert not ui.parent.destroyed
    assert not sibling.destroyed
