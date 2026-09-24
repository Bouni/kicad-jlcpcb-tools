"""Verify workbook row references through native controls and dialog events."""

from base64 import b64decode
import importlib
import os
from pathlib import Path
import sys
from types import ModuleType
from typing import Any

import pytest

from scripts.linux_native_impedance_smoke import event

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


def _button(wx: Any, dialog: Any, identifier: int) -> None:
    """Send the native button command through its bound dialog event handlers."""
    control = dialog.FindWindow(identifier)
    event(wx, control, wx.EVT_BUTTON)


def _select(wx: Any, dialog: Any, row: int) -> None:
    """Change real list selection and await the corresponding preview event."""
    import wx.dataview as dv

    event(wx, dialog.sections, dv.EVT_DATAVIEW_SELECTION_CHANGED, row)
    wait_until(
        wx,
        lambda: dialog.preview_pane._index == row and dialog.preview_pane.ready,
    )


def _include(dialog: Any, row: int, included: bool) -> None:
    """Change a real toggle and dispatch the native user value-change event."""
    import wx.dataview as dv

    control = dialog.sections
    control.SetToggleValue(included, row, 0)
    changed = dv.DataViewEvent(
        dv.EVT_DATAVIEW_ITEM_VALUE_CHANGED.typeId,
        control,
        control.RowToItem(row),
    )
    changed.SetColumn(0)
    changed.SetEventObject(control)
    control.GetEventHandler().ProcessEvent(changed)


def _assert_rows(dialog: Any, count: int) -> None:
    """Read ordinal references from the native table, including unchecked rows."""
    assert dialog.sections.GetItemCount() == count, dialog.status.GetValue()
    assert [dialog.sections.GetTextValue(row, 1) for row in range(count)] == [
        str(row + 1) for row in range(count)
    ]


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
        copper_features=(
            model.CopperFeature(
                "covered-pad",
                "SIGNAL_00",
                "F.Cu",
                "pad",
                "rect",
                (0, 0),
                1_000_000,
                1_000_000,
            ),
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
        return service.CapturedImage(_PNG)

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
        original_ids = dialog._row_ids

        def interact() -> None:
            try:
                assert [
                    dialog.sections.GetColumn(index).GetTitle()
                    for index in range(dialog.sections.GetColumnCount())
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
                ] + ([""] if wx.Platform in ("__WXGTK__", "__WXMAC__") else [])
                _assert_rows(dialog, 12)
                assert dialog.session.analysis is not None
                stub = next(
                    index
                    for index, section in enumerate(dialog.session.analysis.sections)
                    if section.exclusion_reason == model.EXCLUSION_COVERED_STUB
                )
                assert not dialog.sections.GetToggleValue(stub, 0)
                assert dialog.sections.GetTextValue(stub, 1) == str(stub + 1)
                assert_columns_fit(dialog.sections)

                for row in range(12):
                    _select(wx, dialog, row)
                    assert (
                        dialog.preview_pane._sections[
                            dialog.preview_pane._index
                        ].section_id
                        == original_ids[row]
                    )
                    assert captures[-1] == original_ids[row]
                _include(dialog, stub, True)
                wait_until(wx, lambda: original_ids[stub] in dialog.session.included)
                assert dialog.sections.GetToggleValue(stub, 0)
                unchecked = (stub + 1) % 12
                _include(dialog, unchecked, False)
                wait_until(
                    wx, lambda: original_ids[unchecked] not in dialog.session.included
                )
                assert not dialog.sections.GetToggleValue(unchecked, 0)
                _assert_rows(dialog, 12)
                assert dialog._refresh_rows()
                pump(wx)
                _assert_rows(dialog, 12)
                assert dialog._row_ids == original_ids
                assert dialog.sections.GetToggleValue(stub, 0)
                assert not dialog.sections.GetToggleValue(unchecked, 0)
                assert dialog.sections.GetSelectedRow() == 11
                _select(wx, dialog, 11)
                event(wx, dialog.approve_button, wx.EVT_BUTTON)
                assert dialog.session.approved
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
        try:
            _assert_rows(reopened, 12)
            assert reopened._row_ids == original_ids
            included = set(saved[0].included_section_ids)
            assert len(included) == 11
            assert reopened.session.included == included
            assert all(
                reopened.sections.GetToggleValue(row, 0) == (section_id in included)
                for row, section_id in enumerate(original_ids)
            )
        finally:
            reopened.Destroy()
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
        return service.CapturedImage(_PNG)

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
                assert captures[-1] == outer[0].section_id

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
                assert captures[-1] == original_ids[0]
                assert dialog.session.included == set(refreshed_ids[0])
                assert all(dialog.sections.GetToggleValue(row, 0) for row in range(2))
                assert "changed" in dialog.status.GetValue().lower()
                assert dialog.sections.GetTextValue(1, 6) == "/D+, /D−"
                _select(wx, dialog, 1)
                assert dialog.preview_pane._sections[1] == merged[0]
                assert captures[-1] == merged[0].section_id

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
                assert captures[-1] == merged[0].section_id
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
