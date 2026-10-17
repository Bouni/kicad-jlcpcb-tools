"""Check real workbook column geometry across current content and empty states."""

from base64 import b64decode
import importlib
import os
from pathlib import Path
import sys
import time
from types import ModuleType
from typing import Any, Optional

import pytest

from scripts.linux_native_impedance_smoke import event

from .native_wx_support import isolated_native, pump, run_native, wait_until

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


def _diagnostic(message: str) -> None:
    """Preserve the last native phase even when a child crashes before reporting."""
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


def assert_columns_fit(
    control: Any, *, phase: str = "workbook", expected_rows: Optional[int] = None
) -> None:
    """Await allocated native geometry without weakening current-content bounds."""
    import wx

    started = time.monotonic()
    rows = control.GetItemCount()
    if expected_rows is not None:
        assert rows == expected_rows, (
            phase,
            "unexpected row count",
            rows,
            expected_rows,
        )
    has_spacer = wx.Platform in ("__WXGTK__", "__WXMAC__")
    assert control.GetColumnCount() == (12 if has_spacer else 11)
    if has_spacer:
        assert control.GetColumn(11).GetTitle() == ""
        assert all(control.GetTextValue(row, 11) == "" for row in range(rows))
    em = control.GetTextExtent("M")[0]
    contents: list[tuple[str, ...]] = []
    bounds: list[tuple[int, int]] = []
    # Native views assign surplus space to the trailing inert spacer. Only eleven
    # product columns must remain compact as the control gets wider.
    for index in range(11):
        column = control.GetColumn(index)
        strings = [column.GetTitle()]
        if index:
            strings.extend(control.GetTextValue(row, index) for row in range(rows))
        contents.append(tuple(strings))
        if index == 8:
            # Reserve the comparison position even while no row needs a marker.
            strings.append("!=")
        content_width = max(control.GetTextExtent(text)[0] for text in strings)
        # Permit native header/renderer padding while rejecting large blank
        # allocations unrelated to any current value. The bound scales with
        # the user's font and display, avoiding a fixed platform pixel width.
        bounds.append((content_width, content_width + 4 * em))

    def allocated() -> bool:
        """Read the last allocation, which GTK updates in a later layout phase."""
        return control.GetItemCount() == rows and all(
            minimum <= control.GetColumn(index).GetWidth() <= maximum
            for index, (minimum, maximum) in enumerate(bounds)
        )

    _diagnostic(f"Workbook sizing: {phase}, rows={rows}, awaiting allocation")
    try:
        wait_until(
            wx, allocated, reason=f"{phase}: native column allocation did not fit"
        )
    except AssertionError as error:
        geometry = [
            (
                index,
                control.GetColumn(index).GetTitle(),
                bounds[index],
                control.GetColumn(index).GetMinWidth(),
                control.GetColumn(index).GetWidth(),
            )
            for index in range(11)
        ]
        raise AssertionError(
            f"{phase}: after {time.monotonic() - started:.3f}s, "
            f"rows={control.GetItemCount()} expected={rows}; "
            f"columns=(index, title, allowed, requested minimum, allocated): {geometry}"
        ) from error
    # A worker completion must not make old-content bounds accidentally pass.
    assert contents == [
        (control.GetColumn(index).GetTitle(),)
        + tuple(control.GetTextValue(row, index) for row in range(rows) if index)
        for index in range(11)
    ], (phase, "Workbook contents changed while awaiting native geometry")
    _diagnostic(
        f"Workbook sizing: {phase}, allocation fitted in "
        f"{time.monotonic() - started:.3f}s"
    )


def test_native_workbook_sizes_offscreen_rows_refresh_retry_and_empty_state(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """An offscreen long net fits, then columns shrink with short or absent rows."""
    if isolated_native(request, "KICAD_IMPEDANCE_WORKBOOK_SIZING_CHILD"):
        return
    package_name = "_impedance_workbook_sizing_native"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "impedance")]
    monkeypatch.setitem(sys.modules, package_name, package)
    model = importlib.import_module(f"{package_name}.model")
    dialog_api = importlib.import_module(f"{package_name}.dialog")
    service = importlib.import_module(f"{package_name}.service")
    count = 900
    long_name = "OFFSCREEN_IMPEDANCE_BRANCH_" + "RF_TRACE_" * 10

    def snapshot(
        *, long_net: bool = False, empty: bool = False, row_count: int = count
    ) -> Any:
        """Keep rows physically separate and place one long name deep in the model."""
        names = tuple(
            long_name if long_net and index == 450 else f"SIGNAL_{index:03d}"
            for index in range(row_count)
        )
        return model.BoardSnapshot(
            layers=("F.Cu", "B.Cu"),
            traces=()
            if empty
            else tuple(
                model.Trace(
                    f"route-{index}",
                    "F.Cu",
                    name,
                    150_000,
                    ((0, index * 2_000_000), (100_000, index * 2_000_000)),
                )
                for index, name in enumerate(names)
            ),
            net_classes=("RF",),
            net_class_memberships=tuple((name, ("RF",)) for name in names),
            net_class_context_digest="deterministic-native-sizing-classes",
            copper_features=(
                model.CopperFeature(
                    "covered-pad",
                    names[0],
                    "F.Cu",
                    "pad",
                    "rect",
                    (0, 0),
                    1_000_000,
                    1_000_000,
                ),
            ),
        )

    current = [snapshot()]
    unavailable = [False]
    saved: list[Any] = []
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

    def refresh() -> Any:
        """Expose a recoverable real board-read failure at the snapshot seam."""
        if unavailable[0]:
            raise RuntimeError("Native sizing test board is temporarily unavailable")
        return current[0]

    def exercise(frame: Any, wx: Any) -> None:
        import wx.dataview as dv

        started = time.monotonic()

        def phase(label: str) -> None:
            """Leave the current workflow stage in crash and timeout diagnostics."""
            _diagnostic(
                f"Workbook sizing phase: {label} at {time.monotonic() - started:.3f}s"
            )

        def open_dialog(value: Any) -> Any:
            """Use persisted configuration and the real constructor on reopening."""
            phase("construct dialog")
            return dialog_api.ImpedanceDialog(
                frame,
                value,
                current[0],
                preview=lambda *_args, **_kwargs: service.CapturedImage(_PNG),
                refresh_snapshot=refresh,
                fetch_stackup_catalog=lambda _count, **_kwargs: (),
                save_config=saved.append,
            )

        dialog = open_dialog(config)
        failures: list[BaseException] = []

        def resize_without_stretching(label: str) -> None:
            """Await real resizing while retaining widths, selection and inclusion."""
            original_size = dialog.GetSize()
            intrinsic = tuple(
                dialog.sections.GetColumn(index).GetWidth() for index in range(11)
            )
            selected = dialog.sections.GetSelectedRow()
            included = set(dialog.session.included)
            for width in (original_size.width + 600, original_size.width):
                previous_client_width = dialog.sections.GetClientSize().width
                dialog.SetSize((width, original_size.height))
                wait_until(
                    wx,
                    lambda width=width, previous_client_width=previous_client_width: (
                        dialog.GetSize().width == width
                        and dialog.sections.GetClientSize().width
                        != previous_client_width
                        and not dialog._workbook_fit_queued
                    ),
                    reason=f"{label}: native dialog did not resize to {width}",
                )
                assert_columns_fit(dialog.sections, phase=f"{label}: resize to {width}")
                assert (
                    tuple(
                        dialog.sections.GetColumn(index).GetWidth()
                        for index in range(11)
                    )
                    == intrinsic
                )
                dialog.Layout()
                assert_columns_fit(dialog.sections, phase=f"{label}: explicit Layout")
                assert (
                    tuple(
                        dialog.sections.GetColumn(index).GetWidth()
                        for index in range(11)
                    )
                    == intrinsic
                )
                assert dialog.sections.GetSelectedRow() == selected
                assert dialog.session.included == included

        def uncheck_selected_row() -> tuple[str, set[str]]:
            """Exercise actual selection and include events before refitting."""
            selected = 2
            event(wx, dialog.sections, dv.EVT_DATAVIEW_SELECTION_CHANGED, selected)
            wait_until(wx, lambda: dialog.sections.GetSelectedRow() == selected)
            selected_id = dialog._row_ids[selected]
            dialog.sections.SetToggleValue(False, selected, 0)
            changed = dv.DataViewEvent(
                dv.EVT_DATAVIEW_ITEM_VALUE_CHANGED.typeId,
                dialog.sections,
                dialog.sections.RowToItem(selected),
            )
            changed.SetColumn(0)
            changed.SetEventObject(dialog.sections)
            dialog.sections.GetEventHandler().ProcessEvent(changed)
            assert selected_id not in dialog.session.included
            return selected_id, set(dialog.session.included)

        def interact() -> None:
            try:
                phase("initial rows")
                assert dialog.sections.GetItemCount() == count
                assert_columns_fit(
                    dialog.sections, phase="initial rows", expected_rows=count
                )
                resize_without_stretching("initial rows")
                initial_net_width = dialog.sections.GetColumn(6).GetWidth()
                stub = next(
                    row
                    for row, section in enumerate(dialog.session.analysis.sections)
                    if section.exclusion_reason == model.EXCLUSION_COVERED_STUB
                )
                assert dialog.sections.GetTextValue(stub, 7) == "Covered stub"
                assert dialog.sections.GetTextValue(stub, 8) == ""
                assert dialog.sections.GetTextValue(stub, 9) == ""
                assert not dialog.sections.GetToggleValue(stub, 0)

                phase("populate offscreen long net")
                current[0] = snapshot(long_net=True)
                assert dialog._refresh_rows()
                long_row = next(
                    row
                    for row in range(dialog.sections.GetItemCount())
                    if dialog.sections.GetTextValue(row, 6) == long_name
                )
                assert long_row > 100
                assert long_row < count - 100
                assert_columns_fit(
                    dialog.sections, phase="offscreen long net", expected_rows=count
                )

                def long_row_is_offscreen() -> bool:
                    """Wait for GTK's row geometry as well as its column allocation."""
                    rectangle = dialog.sections.GetItemRect(
                        dialog.sections.RowToItem(long_row)
                    )
                    return (
                        rectangle.IsEmpty()
                        or rectangle.GetTop() >= dialog.sections.GetClientSize().height
                    )

                wait_until(
                    wx,
                    long_row_is_offscreen,
                    reason=f"Long net row {long_row} did not reach offscreen geometry",
                )
                rectangle = dialog.sections.GetItemRect(
                    dialog.sections.RowToItem(long_row)
                )
                assert (
                    rectangle.IsEmpty()
                    or rectangle.GetTop() >= dialog.sections.GetClientSize().height
                ), rectangle
                long_net_width = dialog.sections.GetColumn(6).GetWidth()
                assert long_net_width > initial_net_width

                # The large model is needed to challenge offscreen sampling.
                # Keep subsequent failure/retry cases small and bounded.
                phase("replace long net with short rows")
                current[0] = snapshot(row_count=12)
                assert dialog._refresh_rows()
                assert_columns_fit(
                    dialog.sections, phase="short rows", expected_rows=12
                )
                assert dialog.sections.GetColumn(6).GetWidth() < long_net_width

                phase("read failure clears rows")
                unavailable[0] = True
                assert not dialog._refresh_rows()
                assert dialog.sections.GetItemCount() == 0
                assert "temporarily unavailable" in dialog.status.GetValue()
                assert_columns_fit(
                    dialog.sections, phase="read failure", expected_rows=0
                )
                assert dialog.sections.GetColumn(6).GetWidth() < long_net_width

                phase("retry restores short rows")
                unavailable[0] = False
                current[0] = snapshot(row_count=12)
                event(wx, dialog.retry_button, wx.EVT_BUTTON)
                assert dialog.sections.GetItemCount() == 12
                assert_columns_fit(dialog.sections, phase="retry", expected_rows=12)

                selected_id, included = uncheck_selected_row()
                resize_without_stretching("selected retry rows")
                dialog._populate_sections()
                assert_columns_fit(
                    dialog.sections, phase="repopulate selected rows", expected_rows=12
                )
                assert dialog._row_ids[dialog.sections.GetSelectedRow()] == selected_id
                assert dialog.session.included == included
                phase("save short rows for reopening")
                event(wx, dialog.FindWindow(wx.ID_OK), wx.EVT_BUTTON)
            except BaseException as error:
                failures.append(error)
                if dialog and dialog.IsModal():
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

        dialog = open_dialog(model.Config.from_dict(saved[0].to_dict()))

        def interact_reopened() -> None:
            try:
                phase("reopened short rows")
                assert_columns_fit(
                    dialog.sections, phase="reopened rows", expected_rows=12
                )
                assert dialog.session.config.specifications == saved[0].specifications
                assert dialog.session.config.stackup == saved[0].stackup
                assert dialog.session.config.width_results == saved[0].width_results
                assert dialog.session.included == {
                    section.section_id
                    for section in dialog.session.analysis.sections
                    if not section.exclusion_reason
                }
                reopened_net_width = dialog.sections.GetColumn(6).GetWidth()
                resize_without_stretching("reopened rows")
                phase("empty board clears rows")
                current[0] = snapshot(empty=True, row_count=12)
                assert dialog._refresh_rows()
                assert_columns_fit(
                    dialog.sections, phase="empty board", expected_rows=0
                )
                assert dialog.sections.GetColumn(6).GetWidth() < reopened_net_width
                resize_without_stretching("empty board")
                phase("close with a queued native column fit")
                changed = wx.SizeEvent(
                    dialog.sections.GetSize(), dialog.sections.GetId()
                )
                changed.SetEventObject(dialog.sections)
                dialog.sections.GetEventHandler().ProcessEvent(changed)
                assert dialog._workbook_fit_queued
                event(wx, dialog.FindWindow(wx.ID_CANCEL), wx.EVT_BUTTON)
            except BaseException as error:
                failures.append(error)
                if dialog and dialog.IsModal():
                    dialog.EndModal(wx.ID_ABORT)

        wx.CallAfter(interact_reopened)
        try:
            result = dialog.ShowModal()
            if failures:
                raise failures[0]
            assert result == wx.ID_CANCEL
        finally:
            dialog.Destroy()
            wait_until(wx, lambda: not dialog, reason="Reopened workbook did not close")

    try:
        run_native(exercise)
    finally:
        for name in tuple(sys.modules):
            if name.startswith(package_name + "."):
                del sys.modules[name]
