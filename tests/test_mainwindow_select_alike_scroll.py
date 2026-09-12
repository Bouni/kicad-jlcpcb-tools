"""Regression tests for footprint-list scrolling during auto-select-alike.

The list scrolls its *focused* row into view once the click that triggered a
selection change has been handled. Selecting the alike rows one by one left the
last of them focused, which dragged the row the user clicked out from under
the mouse pointer. The fix replaces the selection in one call and then puts
the focus back where it was.

The focused row is not always the selected one. Ctrl-clicking a row out of a
two-row selection leaves the *other* row selected but the focus on the row
that was clicked, so restoring the focus that was there - rather than focusing
the selected row - is what keeps the viewport still in that case too.

When the alike rows are all selected already there is nothing to expand, and
the control is not touched at all. Replacing a selection with itself is not
free: on GTK it moves the selection anchor, so a following shift-click ranges
from the wrong row, and re-focusing a row scrolls it into view even when the
focus did not move - which is how enabling the option used to jump the list.

``mainwindow.py`` only imports with a full set of GUI stubs in place, so these
tests load it through the shared harness.
"""

from unittest.mock import MagicMock

import pytest

from .wx_harness import load_mainwindow, wx_stubs

mainwindow = load_mainwindow(
    "mainwindow_select_alike_tests",
    wx=wx_stubs(
        Dialog=type("Dialog", (), {}),
        Frame=type("Frame", (), {}),
        NewIdRef=object,
        PostEvent=lambda *_a: None,
    ),
)
JLCPCBTools = mainwindow.JLCPCBTools


class _Item:
    """Stand-in for a wx.dataview.DataViewItem; ``row=None`` is the invalid item."""

    def __init__(self, row):
        self.row = row

    def IsOk(self):
        """Report whether the item refers to a row."""
        return self.row is not None


class _FakeList:
    """A multi-select list whose focus follows clicks and drives scrolling.

    Focus and selection are tracked separately, as the real control keeps
    them: a click focuses the row it lands on whether it selects or deselects
    it, ``Select``/``SetSelections`` focus the row they selected last, and
    ``SetCurrentItem`` moves the focus without touching the selection.

    The post-click scroll is deferred rather than immediate because that is
    what the real control does: it lands after the click has been handled,
    which is why ``settle`` is separate from the calls that change the
    selection. ``SetCurrentItem`` scrolls straight away, as GTK's cursor
    setter does, whether or not the focus actually moved.

    The shift-click anchor follows GTK too: a click puts it on the clicked
    row, ``SetSelections`` moves it onto the last row it selected, and
    ``SetCurrentItem`` leaves it where it is.
    """

    def __init__(self, rows, page, top=0):
        self.items = [_Item(row) for row in range(rows)]
        self.page = page
        self.top = top
        self.selected = set()
        self.current = None
        self.anchor = None
        self.set_selections_calls = 0
        self.set_current_item_calls = 0

    def click(self, row, ctrl=False):
        """Model a user click: focus the row, and select or toggle it."""
        if ctrl:
            self.selected ^= {row}
        else:
            self.selected = {row}
        self.current = row
        self.anchor = row

    def shift_click(self, row):
        """Model a shift-click: select the range from the anchor to the row."""
        low, high = sorted((self.anchor, row))
        self.selected = set(range(low, high + 1))
        self.current = row

    def settle(self):
        """Scroll the focused row into view, as the platform does post-click."""
        if self.current is None:
            return
        if self.current < self.top:
            self.top = self.current
        elif self.current >= self.top + self.page:
            self.top = self.current - self.page + 1

    def GetSelection(self):
        """Return the selected row, or the invalid item unless exactly one is."""
        if len(self.selected) != 1:
            return _Item(None)
        return self.items[next(iter(self.selected))]

    def GetSelections(self):
        """Return every selected row."""
        return [self.items[row] for row in sorted(self.selected)]

    def GetSelectedItemsCount(self):
        """Return the size of the current selection."""
        return len(self.selected)

    def IsSelected(self, item):
        """Report whether the row is part of the selection."""
        return item.row in self.selected

    def Select(self, item):
        """Select one more row, and focus it."""
        self.selected.add(item.row)
        self.current = item.row

    def SetSelections(self, items):
        """Replace the selection, leaving the last row of it focused."""
        self.set_selections_calls += 1
        self.selected = {item.row for item in items}
        if items:
            self.current = max(item.row for item in items)
            self.anchor = self.current

    def GetCurrentItem(self):
        """Return the focused row, or the invalid item when nothing is focused."""
        return _Item(self.current)

    def SetCurrentItem(self, item):
        """Focus a row without changing the selection, scrolling it into view."""
        self.set_current_item_calls += 1
        self.current = item.row
        self.settle()


@pytest.fixture(autouse=True)
def _item_array(monkeypatch):
    """Stand in for the wx item array the selection is handed as."""
    monkeypatch.setattr(mainwindow.dv, "DataViewItemArray", list, raising=False)


def _window(footprint_list, alike_rows):
    """Build the state surface select_alike_parts and its event handler touch."""
    window = object.__new__(JLCPCBTools)
    window.footprint_list = footprint_list
    window.logger = MagicMock()
    window.auto_select_alike = True
    window.select_alike_in_progress = False
    window.right_toolbar = MagicMock()
    window.pcbnew = MagicMock()
    window.partlist_data_model = MagicMock()
    window.partlist_data_model.select_alike.return_value = [
        footprint_list.items[row] for row in alike_rows
    ]
    return window


def test_matches_below_the_fold_do_not_scroll_the_clicked_row_away():
    """Alike rows further down the list must leave the viewport alone."""
    footprint_list = _FakeList(rows=300, page=25, top=0)
    footprint_list.click(7)
    window = _window(footprint_list, alike_rows=[7, 47, 187, 247])

    JLCPCBTools.select_alike_parts(window)
    footprint_list.settle()

    assert footprint_list.top == 0
    assert footprint_list.selected == {7, 47, 187, 247}


def test_matches_above_the_fold_do_not_scroll_the_clicked_row_away():
    """Alike rows further up the list must leave the viewport alone."""
    footprint_list = _FakeList(rows=300, page=25, top=150)
    footprint_list.click(152)
    window = _window(footprint_list, alike_rows=[3, 92, 152])

    JLCPCBTools.select_alike_parts(window)
    footprint_list.settle()

    assert footprint_list.top == 150
    assert footprint_list.selected == {3, 92, 152}


def test_the_clicked_row_keeps_the_focus():
    """Focus drives the platform scroll, so it must stay on the clicked row."""
    footprint_list = _FakeList(rows=300, page=25, top=150)
    footprint_list.click(152)
    window = _window(footprint_list, alike_rows=[152, 260])

    JLCPCBTools.select_alike_parts(window)

    assert footprint_list.current == 152


def test_a_selection_made_without_focus_ends_up_focused():
    """With nothing focused, the selected row is the only sensible fallback."""
    footprint_list = _FakeList(rows=300, page=25, top=0)
    footprint_list.selected = {7}
    window = _window(footprint_list, alike_rows=[7, 47])

    JLCPCBTools.select_alike_parts(window)

    assert footprint_list.current == 7


def test_the_selection_is_replaced_in_a_single_call():
    """One selection change, not one per row, so the list reacts once."""
    footprint_list = _FakeList(rows=300, page=25, top=0)
    footprint_list.click(7)
    window = _window(footprint_list, alike_rows=[7, 47, 187])

    JLCPCBTools.select_alike_parts(window)

    assert footprint_list.set_selections_calls == 1


def test_an_existing_multi_row_selection_is_left_alone():
    """Expanding an already-expanded selection would have nothing to start from."""
    footprint_list = _FakeList(rows=300, page=25, top=0)
    footprint_list.click(7)
    footprint_list.click(47, ctrl=True)
    window = _window(footprint_list, alike_rows=[7, 47, 187])

    JLCPCBTools.select_alike_parts(window)

    assert footprint_list.selected == {7, 47}
    assert footprint_list.set_selections_calls == 0
    assert window.logger.warning.called


def test_deselecting_a_row_does_not_scroll_to_the_one_left_selected():
    """Ctrl-clicking one of two selected rows away must not chase the survivor.

    The selection event then reports a single selected row - the one that was
    *not* clicked, possibly far off screen - and the handler expands it. The
    focus is still on the row that was clicked, and has to stay there.
    """
    footprint_list = _FakeList(rows=300, page=25, top=0)
    footprint_list.click(7)
    footprint_list.click(200, ctrl=True)
    footprint_list.top = 190
    window = _window(footprint_list, alike_rows=[7, 100])

    footprint_list.click(200, ctrl=True)
    JLCPCBTools.OnFootprintSelected(window)
    footprint_list.settle()

    assert footprint_list.selected == {7, 100}
    assert footprint_list.current == 200
    assert footprint_list.top == 190


def test_deselecting_a_row_keeps_the_shift_click_anchor_when_nothing_is_added():
    """A survivor with no alike rows must not have its selection re-applied.

    Re-selecting the survivor moves the shift-click anchor onto it, so the
    user's next shift-click would range from the survivor instead of from the
    row they just ctrl-clicked. With nothing to add, the control is left alone.
    """
    footprint_list = _FakeList(rows=300, page=25, top=0)
    footprint_list.click(7)
    footprint_list.click(200, ctrl=True)
    footprint_list.top = 190
    window = _window(footprint_list, alike_rows=[7])

    footprint_list.click(200, ctrl=True)
    JLCPCBTools.OnFootprintSelected(window)
    footprint_list.settle()
    footprint_list.shift_click(202)

    assert footprint_list.selected == {200, 201, 202}
    assert footprint_list.set_selections_calls == 0
    assert footprint_list.set_current_item_calls == 0


def test_enabling_the_option_with_an_offscreen_selection_does_not_scroll():
    """Turning auto-select on must not jump to a lone selected row offscreen.

    There is nothing to expand, and putting the focus back where it already
    is would still scroll that row into view. The control is left alone.
    """
    footprint_list = _FakeList(rows=300, page=25, top=0)
    footprint_list.click(7)
    footprint_list.top = 150
    window = _window(footprint_list, alike_rows=[7])
    window.auto_select_alike = False
    window.settings = {}
    window.save_settings = MagicMock()
    event = MagicMock()
    event.IsChecked.return_value = True

    JLCPCBTools.toggle_select_alike(window, event)

    assert window.auto_select_alike is True
    assert footprint_list.selected == {7}
    assert footprint_list.top == 150
    assert footprint_list.set_selections_calls == 0
    assert footprint_list.set_current_item_calls == 0
