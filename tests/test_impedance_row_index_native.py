"""Verify workbook row references through native controls and dialog events."""

from base64 import b64decode
from hashlib import sha256
import importlib
import os
from pathlib import Path
from struct import pack
import sys
from types import ModuleType
from typing import Any, Optional
from zlib import compress, crc32

import pytest

from scripts.linux_native_impedance_smoke import event, grid_cell_click

from .native_wx_support import isolated_native, pump, run_native, wait_until
from .test_impedance_workbook_sizing_native import assert_columns_fit

pytestmark = [
    pytest.mark.native_wx,
    pytest.mark.skipif(
        os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
        reason="native wx checks require an enabled desktop session",
    ),
]

_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _section_png(section_id: str) -> bytes:
    """Give each row distinct, valid RGB pixels for native display assertions."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        """Write the PNG framing and checksum consumed by native image decoders."""
        return pack(">I", len(data)) + kind + data + pack(">I", crc32(kind + data))

    pixel = sha256(section_id.encode("utf-8")).digest()[:3]
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", compress(b"\x00" + pixel))
        + chunk(b"IEND", b"")
    )


def _assert_displayed(wx: Any, dialog: Any, section_id: str, tracking: Any) -> None:
    """Check selected pixels and actual view evidence, independent of warming order."""
    pane = dialog.preview_pane
    expected_hash = sha256(_section_png(section_id)).hexdigest()
    wait_until(
        wx,
        lambda: pane.displayed
        and pane.selected_section.section_id == section_id
        and pane._capture.sha256 == expected_hash,
    )
    section = pane.selected_section
    assert pane.image_digest(section) == expected_hash
    assert pane._viewed[pane._key(pane._index)] == expected_hash
    spec = next(
        spec
        for spec in dialog.session.config.specifications
        if spec.spec_id == section.spec_id
    )
    expected_digest = tracking.capture_fingerprint(
        tracking.source_fingerprint(dialog.session.snapshot),
        spec,
        section,
        expected_hash,
    )
    record = tracking.find_image(
        dialog.session.config.review_tracking,
        section.spec_id,
        section.layer,
        section_id,
    )
    assert record is not None and record.viewed_at_utc
    assert record.capture_digest == expected_digest
    assert dialog.session.capture_digests[section_id] == expected_digest


def _button(wx: Any, dialog: Any, identifier: int) -> None:
    """Send the native button command through its bound dialog event handlers."""
    control = dialog.FindWindow(identifier)
    event(wx, control, wx.EVT_BUTTON)


def _select(wx: Any, dialog: Any, row: int) -> None:
    """Select a displayed row and await its logical section's native preview."""
    import wx.grid as gridlib

    section_id = dialog._row_ids[row]
    window = dialog.sections.GetGridWindow()
    wait_until(
        wx,
        lambda: dialog._main_first_paint
        and window.GetClientSize().width
        >= dialog.sections.GetColSize(0) + dialog.sections.GetColSize(1)
        and window.GetClientSize().height >= dialog.sections.GetRowSize(row),
        reason="Native workbook has not painted a usable cell viewport",
    )
    event(wx, dialog.sections, gridlib.EVT_GRID_SELECT_CELL, row)
    wait_until(
        wx,
        lambda: dialog.preview_pane.ready
        and getattr(dialog.preview_pane.selected_entry, "section_id", None)
        == section_id,
    )


def _include(dialog: Any, row: int, included: bool) -> None:
    """Change a real toggle and dispatch the native user value-change event."""
    import wx.grid as gridlib

    control = dialog.sections
    section_id = dialog._row_ids[row]
    if control.GetToggleValue(row, 0) != included:
        import wx

        event(wx, control, gridlib.EVT_GRID_CELL_LEFT_CLICK, row)
        wait_until(
            wx,
            lambda: (section_id in dialog.session.included) == included
            and control.GetToggleValue(dialog._row_ids.index(section_id), 0) == included
            and dialog._row_ids[control.GetSelectedRow()] == section_id,
        )
        pump(wx)


def _key(wx: Any, control: Any, code: int) -> None:
    """Use the actual grid keyboard hook without enabling an inline editor."""
    command = wx.KeyEvent(wx.wxEVT_CHAR_HOOK)
    command.SetEventObject(control)
    command.SetKeyCode(code)
    assert control.GetEventHandler().ProcessEvent(command)


def _assert_rows(dialog: Any, count: int) -> None:
    """Keep canonical row numbers attached to sections in checked-first order."""
    assert dialog.sections.GetItemCount() == count, dialog.status.GetValue()
    canonical = tuple(
        section.section_id for section in dialog.session.analysis.sections
    )
    assert dialog._row_ids == tuple(
        section_id
        for included in (True, False)
        for section_id in canonical
        if (section_id in dialog.session.included) == included
    )
    assert [dialog.sections.GetTextValue(row, 1) for row in range(count)] == [
        str(canonical.index(section_id) + 1) for section_id in dialog._row_ids
    ]
    assert [dialog.sections.GetToggleValue(row, 0) for row in range(count)] == [
        section_id in dialog.session.included for section_id in dialog._row_ids
    ]


@pytest.mark.parametrize(
    "direction,expected_row,expected_col",
    [
        ("left", 1, 3),
        ("right", 1, 5),
        ("home", 1, 0),
        ("end", 1, None),
        ("page_up", 0, 4),
        ("page_down", 1, 4),
        ("tab", 1, 5),
        ("left_boundary", 1, 0),
        ("right_boundary", 1, None),
        ("shift_page_down", 0, 4),
        ("ctrl_shift_end", 0, 4),
    ],
)
def test_native_default_keys_preserve_whole_row_selection(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    direction: str,
    expected_row: int,
    expected_col: Optional[int],
) -> None:
    """Default wx grid cursor movement keeps the selected workbook row highlighted."""
    if isolated_native(request, "KICAD_IMPEDANCE_GRID_NATIVE_KEYS_CHILD"):
        return
    from types import SimpleNamespace

    from .test_impedance_draft_dialog import _board, _intent

    package_name = "_impedance_native_keys"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "impedance")]
    monkeypatch.setitem(sys.modules, package_name, package)
    api = SimpleNamespace(
        model=importlib.import_module(f"{package_name}.model"),
        dialog=importlib.import_module(f"{package_name}.dialog"),
        service=importlib.import_module(f"{package_name}.service"),
    )
    snapshot = _board(api)

    def exercise(frame: Any, wx: Any) -> None:
        dialog = api.dialog.ImpedanceDialog(
            frame,
            _intent(api),
            snapshot,
            preview=lambda *_args, **_kwargs: api.service.CapturedImage(_PNG),
            refresh_snapshot=lambda: snapshot,
            fetch_stackup_catalog=lambda *_args, **_kwargs: (),
        )
        failures: list[BaseException] = []

        def interact() -> None:
            try:
                initial_row = (
                    0 if direction in ("shift_page_down", "ctrl_shift_end") else 1
                )
                last_column = dialog.sections.GetNumberCols() - 1
                initial_col = {"left_boundary": 0, "right_boundary": last_column}.get(
                    direction, 4
                )
                grid_cell_click(wx, dialog.sections, initial_row, initial_col)
                pump(wx)
                assert dialog.sections.GetGridCursorRow() == initial_row
                assert dialog.sections.GetGridCursorCol() == initial_col
                assert list(dialog.sections.GetSelectedRows()) == [initial_row]
                before_included = set(dialog.session.included)
                window = dialog.sections.GetGridWindow()
                window.SetFocus()
                key = wx.KeyEvent(wx.wxEVT_KEY_DOWN)
                key.SetEventObject(window)
                key.SetKeyCode(
                    {
                        "left": wx.WXK_LEFT,
                        "right": wx.WXK_RIGHT,
                        "home": wx.WXK_HOME,
                        "end": wx.WXK_END,
                        "page_up": wx.WXK_PAGEUP,
                        "page_down": wx.WXK_PAGEDOWN,
                        "tab": wx.WXK_TAB,
                        "left_boundary": wx.WXK_LEFT,
                        "right_boundary": wx.WXK_RIGHT,
                        "shift_page_down": wx.WXK_PAGEDOWN,
                        "ctrl_shift_end": wx.WXK_END,
                    }[direction]
                )
                key.SetShiftDown(direction in ("shift_page_down", "ctrl_shift_end"))
                key.SetControlDown(direction == "ctrl_shift_end")
                key.SetMetaDown(direction == "ctrl_shift_end")
                assert window.GetEventHandler().ProcessEvent(key)
                pump(wx)
                assert dialog.sections.GetGridCursorCol() == (
                    last_column if expected_col is None else expected_col
                )
                assert dialog.sections.GetGridCursorRow() == expected_row
                assert dialog.sections.GetSelectedRow() == expected_row
                assert list(dialog.sections.GetSelectedRows()) == [expected_row]
                assert all(
                    dialog.sections.IsInSelection(expected_row, column)
                    for column in range(dialog.sections.GetNumberCols())
                )
                assert not any(
                    dialog.sections.IsInSelection(1 - expected_row, column)
                    for column in range(dialog.sections.GetNumberCols())
                )
                assert dialog.session.included == before_included
                _button(wx, dialog, wx.ID_CANCEL)
            except BaseException as error:
                failures.append(error)
                if dialog and dialog.IsModal():
                    dialog.EndModal(wx.ID_ABORT)

        wx.CallAfter(interact)
        try:
            result = dialog.ShowModal()
            if failures:
                raise failures[0]
            assert result == wx.ID_CANCEL
        finally:
            dialog.Destroy()
            pump(wx)

    run_native(exercise)


@pytest.mark.os_input
@pytest.mark.parametrize(
    "backward", [False, True], ids=["last-cell-tab", "first-cell-shift-tab"]
)
def test_native_tab_boundary_requests_dialog_navigation(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    backward: bool,
) -> None:
    """Physical Tab enters and leaves the grid once while retaining its row state."""
    if isolated_native(request, "KICAD_IMPEDANCE_GRID_TAB_FOCUS_CHILD"):
        return
    from types import SimpleNamespace

    from .test_impedance_draft_dialog import _board, _intent

    package_name = "_impedance_native_tab_focus"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "impedance")]
    monkeypatch.setitem(sys.modules, package_name, package)
    api = SimpleNamespace(
        model=importlib.import_module(f"{package_name}.model"),
        dialog=importlib.import_module(f"{package_name}.dialog"),
        service=importlib.import_module(f"{package_name}.service"),
    )
    snapshot = _board(api)

    def exercise(frame: Any, wx: Any) -> None:
        dialog = api.dialog.ImpedanceDialog(
            frame,
            _intent(api),
            snapshot,
            preview=lambda *_args, **_kwargs: api.service.CapturedImage(_PNG),
            refresh_snapshot=lambda: snapshot,
            fetch_stackup_catalog=lambda *_args, **_kwargs: (),
        )
        failures: list[BaseException] = []

        def interact() -> None:
            try:
                table = dialog.sections
                initial_row, initial_col = (
                    (0, 0) if backward else (1, table.GetNumberCols() - 1)
                )
                _select(wx, dialog, initial_row)
                table.SelectRow(initial_row, column=initial_col)
                window = table.GetGridWindow()
                dialog.Raise()
                window.SetFocus()
                wait_until(wx, dialog.IsActive)
                before_included = set(dialog.session.included)
                previous = next(
                    child
                    for child in dialog.GetChildren()
                    if isinstance(child, wx.Button) and child.GetLabel() == "Add…"
                )
                following = dialog.approve_button
                assert following.IsEnabled() and following.IsShownOnScreen()
                simulator = wx.UIActionSimulator()

                def press(code: int, *, shift: bool = False) -> None:
                    """Send real desktop keys and always release the modifier."""
                    if shift:
                        assert simulator.KeyDown(wx.WXK_SHIFT)
                    try:
                        assert simulator.KeyDown(code)
                        assert simulator.KeyUp(code)
                    finally:
                        if shift:
                            assert simulator.KeyUp(wx.WXK_SHIFT)

                press(wx.WXK_TAB, shift=backward)
                wait_until(
                    wx,
                    lambda: wx.Window.FindFocus()
                    is (previous if backward else following),
                    reason="One boundary Tab must leave the composite grid",
                )
                assert table.GetSelectedRow() == initial_row
                assert list(table.GetSelectedRows()) == [initial_row]
                assert table.GetGridCursorCol() == initial_col
                assert dialog.session.included == before_included

                entry = following if backward else previous
                entry.SetFocus()
                wait_until(wx, lambda: wx.Window.FindFocus() is entry)
                press(wx.WXK_TAB, shift=backward)

                def grid_has_focus() -> bool:
                    """Accept native composite focus reporting on GTK and Cocoa."""
                    focus = wx.Window.FindFocus()
                    return focus is table or bool(focus and table.IsDescendant(focus))

                wait_until(wx, grid_has_focus, reason="Tab must enter the cell control")
                assert table.GetSelectedRow() == initial_row
                assert table.GetGridCursorCol() == initial_col
                assert dialog.session.included == before_included
                other_row = 1 - initial_row
                press(wx.WXK_DOWN if backward else wx.WXK_UP)
                wait_until(
                    wx,
                    lambda: table.GetSelectedRow() == other_row
                    and dialog.preview_pane.ready,
                )
                assert list(table.GetSelectedRows()) == [other_row]
                assert dialog.session.included == before_included
                press(wx.WXK_UP if backward else wx.WXK_DOWN)
                wait_until(
                    wx,
                    lambda: table.GetSelectedRow() == initial_row
                    and dialog.preview_pane.ready,
                )
                assert dialog.session.included == before_included
                target = dialog._row_ids[initial_row]
                included_before = target in before_included
                press(wx.WXK_SPACE)
                wait_until(
                    wx,
                    lambda: (target in dialog.session.included) != included_before
                    and dialog._row_ids[table.GetSelectedRow()] == target,
                )
                assert dialog.session.included == (
                    before_included - {target}
                    if included_before
                    else before_included | {target}
                )
                _button(wx, dialog, wx.ID_CANCEL)
            except BaseException as error:
                failures.append(error)
                if dialog and dialog.IsModal():
                    dialog.EndModal(wx.ID_ABORT)

        wx.CallAfter(interact)
        try:
            result = dialog.ShowModal()
            if failures:
                raise failures[0]
            assert result == wx.ID_CANCEL
        finally:
            dialog.Destroy()
            pump(wx)

    run_native(exercise)


@pytest.mark.parametrize(
    "refresh", [False, True], ids=["immediate-save", "immediate-refresh"]
)
def test_native_checkbox_commits_before_queued_projection_rebuild(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    refresh: bool,
) -> None:
    """A native checkbox click survives Save/Refresh before queued row repainting."""
    if isolated_native(request, "KICAD_IMPEDANCE_GRID_IMMEDIATE_INCLUDE_CHILD"):
        return
    from types import SimpleNamespace

    from .test_impedance_draft_dialog import _board, _intent

    package_name = "_impedance_immediate_include_native"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "impedance")]
    monkeypatch.setitem(sys.modules, package_name, package)
    api = SimpleNamespace(
        model=importlib.import_module(f"{package_name}.model"),
        dialog=importlib.import_module(f"{package_name}.dialog"),
        service=importlib.import_module(f"{package_name}.service"),
    )
    snapshot = _board(api)
    saved: list[Any] = []
    saved_inclusion: list[set[str]] = []

    def exercise(frame: Any, wx: Any) -> None:
        import wx.grid as gridlib

        def save(config: Any) -> None:
            """Audit selection at persistence without dispatching queued UI work."""
            saved_inclusion.append(set(dialog.session.included))
            saved.append(config)

        dialog = api.dialog.ImpedanceDialog(
            frame,
            _intent(api),
            snapshot,
            preview=lambda *_args, **_kwargs: api.service.CapturedImage(_PNG),
            refresh_snapshot=lambda: snapshot,
            fetch_stackup_catalog=lambda *_args, **_kwargs: (),
            save_config=save,
        )
        failures: list[BaseException] = []
        remaining: set[str] = set()

        def interact() -> None:
            try:
                assert dialog.sections.GetItemCount() == 2
                assert dialog.sections.GetToggleValue(0, 0)
                removed = dialog._row_ids[0]
                remaining.update(dialog.session.included - {removed})
                event(wx, dialog.sections, gridlib.EVT_GRID_CELL_LEFT_CLICK, 0)
                # Deliberately do not pump after the native mouse command.
                assert removed not in dialog.session.included
                assert not dialog.sections.GetToggleValue(0, 0)
                assert not dialog.session.approved
                if refresh:
                    assert dialog._refresh_rows()
                    assert removed not in dialog.session.included
                    assert not dialog.sections.GetToggleValue(
                        dialog._row_ids.index(removed), 0
                    )
                    _assert_rows(dialog, 2)
                _button(wx, dialog, wx.ID_OK)
            except BaseException as error:
                failures.append(error)
                if dialog and dialog.IsModal():
                    dialog.EndModal(wx.ID_ABORT)

        wx.CallAfter(interact)
        try:
            assert dialog.ShowModal() == wx.ID_OK
            if failures:
                raise failures[0]
            assert len(saved) == 1
            assert saved_inclusion == [remaining]
            # Unapproved workbook row choices retain the existing draft format.
            assert saved[0].included_section_ids == ()
            assert saved[0].reviewed_digest == ""
        finally:
            dialog.Destroy()
            pump(wx)

    run_native(exercise)


def test_native_row_numbers_retain_identity_through_selection_save_and_reopen(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """A double-digit row remains referable after inclusion, refresh and reopening."""
    if isolated_native(request, "KICAD_IMPEDANCE_ROW_INDEX_CHILD"):
        return
    package_name = "_impedance_row_index_native"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "impedance")]
    monkeypatch.setitem(sys.modules, package_name, package)
    model = importlib.import_module(f"{package_name}.model")
    dialog_api = importlib.import_module(f"{package_name}.dialog")
    service = importlib.import_module(f"{package_name}.service")
    captures: list[str] = []
    saved: list[Any] = []
    snapshot = model.BoardSnapshot(
        layers=("F.Cu", "B.Cu"),
        traces=tuple(
            model.Trace(
                f"route-{index}",
                "F.Cu",
                f"SIGNAL_{index:02d}",
                150_000,
                ((0, index * 2_000_000), (100_000, index * 2_000_000)),
            )
            for index in range(12)
        ),
        net_classes=("RF",),
        net_class_memberships=tuple(
            (f"SIGNAL_{index:02d}", ("RF",)) for index in range(12)
        ),
        net_class_context_digest="deterministic-row-index-classes",
        copper_features=tuple(
            model.CopperFeature(
                f"covered-pad-{index}",
                f"SIGNAL_{index:02d}",
                "F.Cu",
                "pad",
                "rect",
                (0, index * 2_000_000),
                1_000_000,
                1_000_000,
            )
            for index in (0, 5)
        ),
    )
    config = model.Config(
        specifications=(
            model.Specification(
                "rf",
                "RF",
                "50",
                "single_ended",
                "RF",
                (model.LayerSettings("F.Cu", ("B.Cu",)),),
            ),
        )
    )

    def preview(section: Any, *, refresh: bool = False) -> Any:
        """Return a native-decodable deterministic image at the capture seam."""
        captures.append(section.section_id)
        return service.CapturedImage(_section_png(section.section_id))

    def exercise(frame: Any, wx: Any) -> None:
        def open_dialog(value: Any) -> Any:
            """Keep constructor, controls and all local state transitions real."""
            return dialog_api.ImpedanceDialog(
                frame,
                value,
                snapshot,
                preview=preview,
                refresh_snapshot=lambda: snapshot,
                fetch_stackup_catalog=lambda _count, **_kwargs: (),
                save_config=saved.append,
            )

        dialog = open_dialog(config)
        failures: list[BaseException] = []
        canonical_ids = tuple(
            section.section_id for section in dialog.session.analysis.sections
        )
        saved_ids: list[tuple[str, ...]] = []

        def interact() -> None:
            try:
                assert [
                    dialog.sections.GetColLabelValue(index)
                    for index in range(dialog.sections.GetNumberCols())
                ] == [
                    "Incl.",
                    "Row",
                    "Layer",
                    "Net class",
                    "Target Ω",
                    "Length",
                    "Net",
                    "Width",
                    "",
                    "Target width",
                    "Segments",
                    "Pair spacing",
                    "",
                    "Target spacing",
                    "Spacing check",
                ]
                _assert_rows(dialog, 12)
                assert dialog.session.analysis is not None
                stubs = tuple(
                    section.section_id
                    for section in dialog.session.analysis.sections
                    if section.exclusion_reason == model.EXCLUSION_COVERED_STUB
                )
                assert stubs == (canonical_ids[0], canonical_ids[5])
                assert dialog._row_ids[-2:] == stubs
                assert [dialog.sections.GetTextValue(row, 1) for row in (10, 11)] == [
                    "1",
                    "6",
                ]
                assert_columns_fit(dialog.sections)

                target_id = canonical_ids[11]
                _select(wx, dialog, dialog._row_ids.index(target_id))
                wait_until(
                    wx,
                    lambda: set(captures).issuperset(dialog.session.included),
                    reason="Background preparation did not render the other routed rows",
                )
                _assert_displayed(wx, dialog, target_id, dialog_api.tracking)
                assert (
                    len(
                        {
                            sha256(_section_png(value)).hexdigest()
                            for value in canonical_ids
                        }
                    )
                    == 12
                )

                for row in range(12):
                    section_id = dialog._row_ids[row]
                    _select(wx, dialog, row)
                    assert dialog.preview_pane.selected_entry.section_id == section_id
                    _assert_displayed(wx, dialog, section_id, dialog_api.tracking)

                # The double-digit canonical row moves between groups on Space.
                # Its selected identity, preview, cursor and label must follow it.
                _select(wx, dialog, dialog._row_ids.index(target_id))
                _key(wx, dialog.sections, wx.WXK_SPACE)
                pump(wx)
                assert target_id not in dialog.session.included
                assert dialog._row_ids[-1] == target_id
                assert dialog.sections.GetSelectedRow() == 11
                assert dialog.sections.GetGridCursorRow() == 11
                assert dialog.sections.IsVisible(11, 1)
                assert dialog.sections.GetTextValue(11, 1) == "12"
                assert not dialog.sections.GetToggleValue(11, 0)
                _assert_displayed(wx, dialog, target_id, dialog_api.tracking)
                assert dialog.preview_pane.selected_entry.section_id == target_id
                assert not dialog.sections.IsCellEditControlEnabled()
                _assert_rows(dialog, 12)
                _key(wx, dialog.sections, wx.WXK_SPACE)
                pump(wx)
                assert target_id in dialog.session.included
                target_row = dialog._row_ids.index(target_id)
                assert target_row == 9
                assert dialog.sections.GetSelectedRow() == target_row
                assert dialog.sections.IsVisible(target_row, 1)
                assert dialog.sections.GetTextValue(target_row, 1) == "12"
                assert dialog.sections.GetToggleValue(target_row, 0)
                _assert_displayed(wx, dialog, target_id, dialog_api.tracking)
                for code, selected in (
                    (wx.WXK_UP, target_row - 1),
                    (wx.WXK_DOWN, target_row),
                ):
                    _key(wx, dialog.sections, code)
                    wait_until(
                        wx,
                        lambda selected=selected: dialog.sections.GetSelectedRow()
                        == selected
                        and dialog.preview_pane.ready
                        and getattr(
                            dialog.preview_pane.selected_entry, "section_id", None
                        )
                        == dialog._row_ids[selected],
                    )
                    _assert_displayed(
                        wx, dialog, dialog._row_ids[selected], dialog_api.tracking
                    )
                _assert_rows(dialog, 12)

                # Native clicks move an included stub to its canonical place and
                # an unchecked routed row behind all remaining included rows.
                _include(dialog, dialog._row_ids.index(stubs[0]), True)
                assert stubs[0] in dialog.session.included
                assert dialog._row_ids[0] == stubs[0]
                assert dialog.sections.GetSelectedRow() == 0
                assert dialog.sections.IsVisible(0, 0)
                _assert_displayed(wx, dialog, stubs[0], dialog_api.tracking)
                unchecked_id = canonical_ids[1]
                _include(dialog, dialog._row_ids.index(unchecked_id), False)
                assert unchecked_id not in dialog.session.included
                unchecked_row = dialog._row_ids.index(unchecked_id)
                assert unchecked_row == 10
                assert dialog.sections.GetSelectedRow() == unchecked_row
                assert dialog.sections.GetGridCursorRow() == unchecked_row
                assert dialog.sections.IsVisible(unchecked_row, 0)
                assert dialog.sections.GetTextValue(unchecked_row, 1) == "2"
                _assert_displayed(wx, dialog, unchecked_id, dialog_api.tracking)
                _assert_rows(dialog, 12)
                ids_before_refresh = dialog._row_ids
                assert dialog._refresh_rows()
                pump(wx)
                _assert_rows(dialog, 12)
                assert dialog._row_ids == ids_before_refresh
                assert dialog._row_ids[dialog.sections.GetSelectedRow()] == unchecked_id
                assert dialog.preview_pane.selected_entry.section_id == unchecked_id
                _assert_displayed(wx, dialog, unchecked_id, dialog_api.tracking)
                _select(wx, dialog, dialog._row_ids.index(target_id))
                event(wx, dialog.approve_button, wx.EVT_BUTTON)
                assert dialog.session.approved
                saved_ids.append(dialog._row_ids)
                _button(wx, dialog, wx.ID_OK)
            except BaseException as error:
                failures.append(error)
                dialog.EndModal(wx.ID_ABORT)

        wx.CallAfter(interact)
        try:
            result = dialog.ShowModal()
            if failures:
                raise failures[0]
            assert result == wx.ID_OK
            assert len(saved) == 1
        finally:
            dialog.Destroy()
            pump(wx)
        reopened = open_dialog(model.Config.from_dict(saved[0].to_dict()))

        def inspect_reopened() -> None:
            """Display a restored row and verify its persisted identity and pixels."""
            try:
                _assert_rows(reopened, 12)
                assert reopened._row_ids == saved_ids[0]
                included = set(saved[0].included_section_ids)
                assert len(included) == 10
                assert reopened.session.approved
                assert reopened.session.included == included
                assert all(
                    reopened.sections.GetToggleValue(row, 0) == (section_id in included)
                    for row, section_id in enumerate(reopened._row_ids)
                )
                _select(wx, reopened, reopened._row_ids.index(canonical_ids[11]))
                _assert_displayed(wx, reopened, canonical_ids[11], dialog_api.tracking)
                _button(wx, reopened, wx.ID_CANCEL)
            except BaseException as error:
                failures.append(error)
                reopened.EndModal(wx.ID_ABORT)

        wx.CallAfter(inspect_reopened)
        try:
            result = reopened.ShowModal()
            if failures:
                raise failures[0]
            assert result == wx.ID_CANCEL
            assert len(saved) == 1
        finally:
            reopened.Destroy()
            pump(wx)

    try:
        run_native(exercise)
    finally:
        for name in tuple(sys.modules):
            if name.startswith(package_name + "."):
                del sys.modules[name]


def test_native_spacing_first_keeps_grid_mouse_keyboard_trace_identity(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """A diagnostic prefix cannot offset canonical grid selection or review evidence."""
    if isolated_native(request, "KICAD_IMPEDANCE_SPACING_ROW_CHILD"):
        return
    package_name = "_impedance_spacing_row_native"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "impedance")]
    monkeypatch.setitem(sys.modules, package_name, package)
    model = importlib.import_module(f"{package_name}.model")
    dialog_api = importlib.import_module(f"{package_name}.dialog")
    service = importlib.import_module(f"{package_name}.service")
    snapshot = model.BoardSnapshot(
        layers=("F.Cu", "B.Cu"),
        traces=tuple(
            model.Trace(
                f"pair-{pair}-{side}",
                "F.Cu",
                f"PAIR{pair}_{side}",
                150_000,
                (
                    (0, pair * 5_000_000 + offset),
                    (2_000_000, pair * 5_000_000 + offset),
                ),
            )
            for pair in range(2)
            for side, offset in (("P", 0), ("N", 250_000))
        ),
        net_classes=("RF",),
        net_class_memberships=tuple(
            (f"PAIR{pair}_{side}", ("RF",)) for pair in range(2) for side in ("P", "N")
        ),
        net_class_context_digest="spacing-first-native-grid",
        differential_pairs=tuple(
            (f"PAIR{pair}_P", f"PAIR{pair}_N") for pair in range(2)
        ),
    )
    config = model.Config(
        specifications=(
            model.Specification(
                "rf",
                "RF",
                "90",
                "differential",
                "RF",
                (model.LayerSettings("F.Cu", ("B.Cu",), 200_000),),
            ),
        )
    )

    def preview(entry: Any, *, refresh: bool = False) -> Any:
        """Give every entry its own immutable decodable pixels at the capture seam."""
        identifier = (
            entry.section_id if isinstance(entry, model.Section) else entry.view_id
        )
        return service.CapturedImage(_section_png(identifier))

    def exercise(frame: Any, wx: Any) -> None:
        """Drive native grid events against the real mixed-list owner integration."""
        dialog = dialog_api.ImpedanceDialog(
            frame,
            config,
            snapshot,
            preview,
            lambda: snapshot,
            fetch_stackup_catalog=lambda _count, **_kwargs: (),
        )
        failures: list[BaseException] = []

        def interact() -> None:
            """Open at a concern, choose both traces, and retain explicit concern choice."""
            try:
                pane = dialog.preview_pane
                wait_until(wx, lambda: pane.ready)
                assert pane.spacing_entries
                assert pane.entries == pane.spacing_entries + pane.sections
                assert pane.selected_entry == pane.spacing_entries[0]
                assert len(pane.sections) == 2
                _assert_rows(dialog, 2)
                assert dialog.session.config.review_tracking.spacing_images == ()
                _select(wx, dialog, 0)
                _assert_displayed(wx, dialog, dialog._row_ids[0], dialog_api.tracking)
                _key(wx, dialog.sections, wx.WXK_DOWN)
                wait_until(wx, lambda: dialog.sections.GetSelectedRow() == 1)
                _assert_displayed(wx, dialog, dialog._row_ids[1], dialog_api.tracking)
                pane.select(0)
                wait_until(
                    wx,
                    lambda: pane.ready
                    and pane.selected_entry == pane.spacing_entries[0],
                )
                dialog._populate_sections()
                assert pane.selected_entry == pane.spacing_entries[0]
                _assert_rows(dialog, 2)
                assert dialog.session.config.review_tracking.spacing_images == ()
                assert not dialog.session.approved
                _button(wx, dialog, wx.ID_CANCEL)
            except BaseException as error:
                failures.append(error)
                dialog.EndModal(wx.ID_ABORT)

        wx.CallAfter(interact)
        try:
            result = dialog.ShowModal()
            if failures:
                raise failures[0]
            assert result == wx.ID_CANCEL
        finally:
            dialog.Destroy()
            pump(wx)

    try:
        run_native(exercise)
    finally:
        for name in tuple(sys.modules):
            if name.startswith(package_name + "."):
                del sys.modules[name]


def test_native_nested_rows_refresh_selection_save_and_reopen(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """A contained USB fragment joins a newly identified shared signal region."""
    if isolated_native(request, "KICAD_IMPEDANCE_NESTED_ROW_CHILD"):
        return
    package_name = "_impedance_nested_row_native"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "impedance")]
    monkeypatch.setitem(sys.modules, package_name, package)
    model = importlib.import_module(f"{package_name}.model")
    dialog_api = importlib.import_module(f"{package_name}.dialog")
    service = importlib.import_module(f"{package_name}.service")
    captures: list[str] = []
    saved: list[Any] = []

    def snapshot(inner_end_x: int) -> Any:
        """Keep independent and outer routes fixed while moving the inner end."""
        return model.BoardSnapshot(
            layers=("F.Cu", "B.Cu"),
            traces=(
                model.Trace(
                    "independent",
                    "F.Cu",
                    "OTHER",
                    203_200,
                    ((0, 0), (1_000_000, 0)),
                ),
                model.Trace(
                    "outer-horizontal",
                    "F.Cu",
                    "/D+",
                    203_200,
                    ((10_000_000, 10_000_000), (20_000_000, 10_000_000)),
                ),
                model.Trace(
                    "outer-vertical",
                    "F.Cu",
                    "/D+",
                    203_200,
                    # Leave the right edge open so the initial D- extension
                    # partially overlaps the box without shorting D+ copper.
                    ((10_000_000, 10_000_000), (10_000_000, 20_000_000)),
                ),
                model.Trace(
                    "inner",
                    "F.Cu",
                    "/D−",
                    203_200,
                    ((15_000_000, 15_000_000), (inner_end_x, 15_000_000)),
                ),
            ),
            net_classes=("USB",),
            net_class_memberships=(
                ("OTHER", ("USB",)),
                ("/D+", ("USB",)),
                ("/D−", ("USB",)),
            ),
            net_class_context_digest="deterministic-nested-row-classes",
        )

    current = [snapshot(21_000_000)]
    config = model.Config(
        specifications=(
            model.Specification(
                "usb",
                "USB",
                "90",
                "single_ended",
                "USB",
                (model.LayerSettings("F.Cu", ("B.Cu",)),),
            ),
        )
    )

    def preview(section: Any, *, refresh: bool = False) -> Any:
        """Record real preview requests while supplying deterministic PNG bytes."""
        captures.append(section.section_id)
        return service.CapturedImage(_section_png(section.section_id))

    def exercise(frame: Any, wx: Any) -> None:
        def open_dialog(value: Any) -> Any:
            """Use the production constructor, row list and save handlers."""
            return dialog_api.ImpedanceDialog(
                frame,
                value,
                current[0],
                preview=preview,
                refresh_snapshot=lambda: current[0],
                fetch_stackup_catalog=lambda _count, **_kwargs: (),
                save_config=saved.append,
            )

        dialog = open_dialog(config)
        failures: list[BaseException] = []
        original_ids = dialog._row_ids
        outer: list[Any] = []
        merged: list[Any] = []
        refreshed_ids: list[tuple[str, ...]] = []

        def interact() -> None:
            try:
                _assert_rows(dialog, 3)
                assert dialog.session.analysis is not None
                outer.append(dialog.session.analysis.sections[1])
                assert outer[0].net_names == ("/D+",)
                assert dialog.session.analysis.sections[2].net_names == ("/D−",)
                _select(wx, dialog, 1)
                wait_until(
                    wx,
                    lambda: set(captures).issuperset(original_ids),
                    reason="Background preparation did not render the other nested rows",
                )
                _assert_displayed(wx, dialog, outer[0].section_id, dialog_api.tracking)

                # Approval refreshes the real board snapshot before accepting
                # rows. The moved endpoint now makes row 3 wholly fit in row 2;
                # the new shared row must own both routes and get a new ID.
                current[0] = snapshot(15_200_000)
                event(wx, dialog.approve_button, wx.EVT_BUTTON)
                _assert_rows(dialog, 2)
                assert not dialog.session.approved
                merged.append(dialog.session.analysis.sections[1])
                assert merged[0].net_names == ("/D+", "/D−")
                assert merged[0].bounds == outer[0].bounds
                assert {trace.trace_id for trace in merged[0].traces} == {
                    "outer-horizontal",
                    "outer-vertical",
                    "inner",
                }
                assert merged[0].section_id not in original_ids
                assert dialog._row_ids == (original_ids[0], merged[0].section_id)
                refreshed_ids.append(dialog._row_ids)
                # The selected former D+-only row no longer exists. The
                # control falls back to the first current included row.
                assert dialog.sections.GetSelectedRow() == 0
                wait_until(
                    wx,
                    lambda: dialog.preview_pane.ready
                    and dialog.preview_pane._index == 0,
                )
                _assert_displayed(wx, dialog, original_ids[0], dialog_api.tracking)
                assert dialog.session.included == set(refreshed_ids[0])
                assert all(dialog.sections.GetToggleValue(row, 0) for row in range(2))
                assert "changed" in dialog.status.GetValue().lower()
                assert dialog.sections.GetTextValue(1, 6) == "/D+, /D−"
                _select(wx, dialog, 1)
                assert dialog.preview_pane._sections[1] == merged[0]
                _assert_displayed(wx, dialog, merged[0].section_id, dialog_api.tracking)

                event(wx, dialog.approve_button, wx.EVT_BUTTON)
                assert dialog.session.approved, dialog.status.GetValue()
                _button(wx, dialog, wx.ID_OK)
            except BaseException as error:
                failures.append(error)
                dialog.EndModal(wx.ID_ABORT)

        wx.CallAfter(interact)
        try:
            result = dialog.ShowModal()
            if failures:
                raise failures[0]
            assert result == wx.ID_OK
            assert len(saved) == 1
            assert saved[0].included_section_ids == refreshed_ids[0]
        finally:
            dialog.Destroy()
            pump(wx)

        reopened = open_dialog(model.Config.from_dict(saved[0].to_dict()))

        def inspect_reopened() -> None:
            try:
                _assert_rows(reopened, 2)
                assert reopened._row_ids == refreshed_ids[0]
                assert reopened.session.approved
                assert reopened.session.analysis.sections[1] == merged[0]
                assert reopened.session.included == set(refreshed_ids[0])
                assert all(reopened.sections.GetToggleValue(row, 0) for row in range(2))
                _select(wx, reopened, 1)
                assert reopened.preview_pane._sections[1] == merged[0]
                _assert_displayed(
                    wx, reopened, merged[0].section_id, dialog_api.tracking
                )
                _button(wx, reopened, wx.ID_CANCEL)
            except BaseException as error:
                failures.append(error)
                reopened.EndModal(wx.ID_ABORT)

        wx.CallAfter(inspect_reopened)
        try:
            result = reopened.ShowModal()
            if failures:
                raise failures[0]
            assert result == wx.ID_CANCEL
            assert len(saved) == 1
        finally:
            reopened.Destroy()
            pump(wx)

    try:
        run_native(exercise)
    finally:
        for name in tuple(sys.modules):
            if name.startswith(package_name + "."):
                del sys.modules[name]
