"""Regression tests for footprint-list scrolling during auto-select-alike.

The list scrolls its *focused* row into view once the click that triggered a
selection change has been handled. On wxOSX the focused row follows the
selection, so selecting the alike rows one by one left the last of them
focused, which dragged the row the user clicked out from under the mouse
pointer. The fix replaces the selection in one call and then puts the focus
back where it was.

GTK keeps its cursor apart from the selection and never scrolled here, so on
GTK the fix has to leave the control exactly as selecting the missing rows one
at a time did. Three details of GTK decide that. Ctrl-clicking a row out of a
two-row selection leaves the *other* row selected but the cursor on the row
that was clicked. Replacing the selection moves the shift-click anchor onto the
last row it selects. And focusing a row scrolls it into view whether or not the
cursor moved, so the focus is only put back when it actually moved.

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

    def __eq__(self, other):
        """Compare by row, as a DataViewItem compares the item it refers to."""
        return self.row == other.row

    def __hash__(self):
        """Hash by row, to agree with ``__eq__``."""
        return hash(self.row)

    def IsOk(self):
        """Report whether the item refers to a row."""
        return self.row is not None


class _FakeList:
    """A multi-select list that tracks focus and selection separately.

    A click focuses the row it lands on, whether it selects or deselects it,
    and puts the shift-click anchor there. The post-click scroll is deferred
    rather than immediate because that is what the real control does: it lands
    after the click has been handled, which is why ``settle`` is separate from
    the calls that change the selection.

    How the programmatic calls move the focus and the anchor depends on the
    port, so the subclasses provide them.
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

    def GetCurrentItem(self):
        """Return the focused row, or the invalid item when nothing is focused."""
        return _Item(self.current)


class _CocoaList(_FakeList):
    """wxOSX, where the focused row follows the selection.

    ``Select`` focuses the row it selects, ``SetSelections`` leaves the highest
    selected row focused, and ``SetCurrentItem`` is ``Select``, since a Cocoa
    table cannot focus a row without selecting it. None of them scroll.
    """

    def Select(self, item):
        """Select one more row, and focus it."""
        self.selected.add(item.row)
        self.current = item.row

    def SetSelections(self, items):
        """Replace the selection, leaving its highest row focused."""
        self.set_selections_calls += 1
        self.selected = {item.row for item in items}
        if items:
            self.current = max(self.selected)

    def SetCurrentItem(self, item):
        """Focus a row by selecting it."""
        self.set_current_item_calls += 1
        self.Select(item)


class _GtkList(_FakeList):
    """wxGTK, where the cursor stays put while the selection changes.

    ``Select`` and ``SetSelections`` never move the cursor, but each row they
    newly select becomes the shift-click anchor. ``SetSelections`` clears the
    selection first, so it leaves the anchor on the last row it was given.
    ``SetCurrentItem`` moves the cursor and scrolls it into view straight away,
    whether or not it moved.
    """

    def Select(self, item):
        """Select one more row, moving the anchor onto it if it was unselected."""
        if item.row not in self.selected:
            self.selected.add(item.row)
            self.anchor = item.row

    def SetSelections(self, items):
        """Replace the selection, leaving the anchor on the last row given."""
        self.set_selections_calls += 1
        self.selected = set()
        for item in items:
            self.Select(item)

    def SetCurrentItem(self, item):
        """Move the cursor and scroll it into view, even if it was already there."""
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
    footprint_list = _CocoaList(rows=300, page=25, top=0)
    footprint_list.click(7)
    window = _window(footprint_list, alike_rows=[7, 47, 187, 247])

    JLCPCBTools.select_alike_parts(window)
    footprint_list.settle()

    assert footprint_list.top == 0
    assert footprint_list.selected == {7, 47, 187, 247}


def test_matches_above_the_fold_do_not_scroll_the_clicked_row_away():
    """Alike rows further up the list must leave the viewport alone."""
    footprint_list = _CocoaList(rows=300, page=25, top=150)
    footprint_list.click(152)
    window = _window(footprint_list, alike_rows=[3, 92, 152])

    JLCPCBTools.select_alike_parts(window)
    footprint_list.settle()

    assert footprint_list.top == 150
    assert footprint_list.selected == {3, 92, 152}


def test_the_clicked_row_keeps_the_focus():
    """Focus drives the platform scroll, so it must stay on the clicked row."""
    footprint_list = _CocoaList(rows=300, page=25, top=150)
    footprint_list.click(152)
    window = _window(footprint_list, alike_rows=[152, 260])

    JLCPCBTools.select_alike_parts(window)

    assert footprint_list.current == 152


def test_the_selection_is_replaced_in_a_single_call():
    """One selection change, not one per row, so the list reacts once."""
    footprint_list = _CocoaList(rows=300, page=25, top=0)
    footprint_list.click(7)
    window = _window(footprint_list, alike_rows=[7, 47, 187])

    JLCPCBTools.select_alike_parts(window)

    assert footprint_list.set_selections_calls == 1


def test_an_existing_multi_row_selection_is_left_alone():
    """Expanding an already-expanded selection would have nothing to start from."""
    footprint_list = _CocoaList(rows=300, page=25, top=0)
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
    footprint_list = _GtkList(rows=300, page=25, top=0)
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
    footprint_list = _GtkList(rows=300, page=25, top=0)
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
    footprint_list = _GtkList(rows=300, page=25, top=0)
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


@pytest.mark.parametrize("alike_rows", [[7, 100], [7, 180]])
def test_expanding_an_offscreen_selection_on_enable_does_not_scroll(alike_rows):
    """Turning auto-select on must not jump to the offscreen row it expands.

    Replacing the selection leaves the GTK cursor on that row, so there is no
    focus to put back, and focusing it anyway would scroll it into view.
    """
    footprint_list = _GtkList(rows=300, page=25, top=0)
    footprint_list.click(7)
    footprint_list.top = 150
    window = _window(footprint_list, alike_rows=alike_rows)
    window.auto_select_alike = False
    window.settings = {}
    window.save_settings = MagicMock()
    event = MagicMock()
    event.IsChecked.return_value = True

    JLCPCBTools.toggle_select_alike(window, event)

    assert footprint_list.selected == set(alike_rows)
    assert footprint_list.current == 7
    assert footprint_list.top == 150
    assert footprint_list.set_current_item_calls == 0


def _select_missing_rows_one_by_one(footprint_list, alike_rows):
    """Select each alike row that is not selected yet, one call per row."""
    for row in alike_rows:
        item = footprint_list.items[row]
        if not footprint_list.IsSelected(item):
            footprint_list.Select(item)


def _gtk_list(selected, focus, top):
    """Build a GTK list with one row selected and the cursor and anchor on ``focus``."""
    footprint_list = _GtkList(rows=300, page=25, top=top)
    footprint_list.selected = {selected}
    footprint_list.current = focus
    footprint_list.anchor = focus
    return footprint_list


@pytest.mark.parametrize(
    ("selected", "focus", "top", "alike_rows"),
    [
        pytest.param(7, 7, 0, [7, 47, 187, 247], id="clicked-first-match"),
        pytest.param(152, 152, 150, [3, 92, 152], id="clicked-last-match"),
        pytest.param(7, 200, 190, [7, 100], id="ctrl-click-survivor"),
        pytest.param(7, 7, 150, [7, 100], id="offscreen-with-match-above"),
        pytest.param(7, 7, 150, [7, 180], id="offscreen-with-match-below"),
        pytest.param(7, None, 150, [7, 47], id="no-cursor"),
    ],
)
def test_gtk_state_matches_selecting_the_missing_rows_one_at_a_time(
    selected, focus, top, alike_rows
):
    """On GTK, replacing the selection must change nothing else natively.

    Selecting the missing alike rows one call at a time moves neither the
    cursor nor the viewport, and leaves the shift-click anchor on the last row
    it adds. Replacing the selection in one call has to end in that same state,
    without focusing anything, whichever row is selected and wherever the
    cursor and the viewport are.
    """
    expected = _gtk_list(selected, focus, top)
    _select_missing_rows_one_by_one(expected, alike_rows)
    footprint_list = _gtk_list(selected, focus, top)

    JLCPCBTools.select_alike_parts(_window(footprint_list, alike_rows))

    assert footprint_list.selected == expected.selected
    assert footprint_list.current == expected.current
    assert footprint_list.anchor == expected.anchor
    assert footprint_list.top == expected.top
    assert footprint_list.set_current_item_calls == 0
