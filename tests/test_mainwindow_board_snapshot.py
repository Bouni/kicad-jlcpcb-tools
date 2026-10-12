"""The window reads the board through its backend and holds the last snapshot."""

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from . import part_preferences_test_support as support
from .part_preferences_test_support import Footprint, act
from .wx_harness import load_siblings, module

mainwindow = support.mainwindow
make_window = support.make_window

with load_siblings(
    "_board_snapshot_estimator",
    ["bom_widget"],
    {
        "wx": module("wx"),
        "_board_snapshot_estimator.helpers": module("helpers", HighResWxSize=object),
    },
) as _loaded:
    BomEstimatorController = _loaded["bom_widget"].BomEstimatorController

# What the window logs once R2 has two fields named LCSC.
REPEATED_FIELD_NOTE = (
    'Footprint R2 has more than one field named "LCSC". KiCad keeps only the '
    "last one when the board is reopened. Remove the extra field from the "
    "footprint."
)


class CountingBackend:
    """Count the full board reads a window makes."""

    def __init__(self, backend: Any) -> None:
        self.backend = backend
        self.snapshots = 0

    def snapshot(self) -> Any:
        """Read the whole board, counting the read."""
        self.snapshots += 1
        return self.backend.snapshot()


def _counted(window: Any) -> CountingBackend:
    """Count the window's board reads from now on."""
    window.backend = CountingBackend(window.backend)
    return window.backend


def test_lookups_answer_from_the_last_read_until_the_board_is_read_again(
    make_window: Callable[..., Any],
) -> None:
    """A PCB edit shows once the list is rebuilt; single lookups read nothing."""
    window = make_window()
    backend = _counted(window)
    footprint = window.pcbnew.GetBoard().FindFootprintByReference("R1")
    footprint.SetField("LCSC", "C200")

    assert window.store.get_part("R1")["lcsc"] == "C100"
    assert window.store.get_part("R1")["lcsc"] == "C100"
    assert backend.snapshots == 0
    window.populate_footprint_list()
    assert backend.snapshots == 1
    assert window.store.get_part("R1")["lcsc"] == "C200"
    assert backend.snapshots == 1


@pytest.mark.parametrize("action", ["picker", "paste", "apply", "clear"])
def test_each_edit_is_read_back_once_before_its_rows_are_published(
    action: str,
    make_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The snapshot an edit publishes from already holds the edit."""
    window = make_window(part_preferences={("R_0603", "10k"): "C200"})
    backend = _counted(window)
    published = []
    window.partlist_data_model.set_lcsc.side_effect = lambda ref, lcsc, *_: (
        published.append((lcsc, window.store.get_part(ref)["lcsc"]))
    )

    act(action, window, mainwindow, monkeypatch, "C200")

    expected = "" if action == "clear" else "C200"
    assert (
        window._board_snapshot.by_reference("R1")[0].default.fields["LCSC"].text
        == expected
    )
    assert all(lcsc == stored for lcsc, stored in published)
    # Apply reads the board when the action starts; every edit reads it back.
    assert backend.snapshots == (2 if action == "apply" else 1)


def test_held_board_checks_the_live_board_before_answering(
    make_window: Callable[..., Any], mainwindow: Any
) -> None:
    """A replaced board fails a lookup instead of answering from the old snapshot."""
    window = make_window(footprints=[Footprint("R1")])
    window._read_board()
    replacement = support.Board([Footprint("R1", lcsc="C900")])
    replacement.filename = window.pcbnew.GetBoard().filename
    window.pcbnew.GetBoard = lambda: replacement

    with pytest.raises(mainwindow.BoardChanged):
        window.store.get_part("R1")
    with pytest.raises(mainwindow.BoardChanged):
        window._held_board()


def _shared_uuid_window(make_window: Callable[..., Any]) -> Any:
    """Open a window on a board where R2 has R1's UUID.

    KiCad gives every footprint its own, but a board file can hold two
    footprints with one, and KiCad keeps both when it opens the file.
    """
    footprints = [Footprint("R1", lcsc=""), Footprint("R2", lcsc="C200")]
    footprints[1].uuid = footprints[0].uuid
    return make_window(footprints=footprints)


def test_footprints_sharing_a_uuid_list_like_any_other(
    make_window: Callable[..., Any],
) -> None:
    """Both rows show, each with its own assignment, and nothing stops."""
    window = _shared_uuid_window(make_window)
    _real(window, "populate_footprint_list")

    window.populate_footprint_list()

    assert {
        reference: window.store.get_part(reference)["lcsc"]
        for reference in ("R1", "R2")
    } == {"R1": "", "R2": "C200"}
    assert window._project_storage_unavailable is False


def _repeated_field_window(make_window: Callable[..., Any]) -> Any:
    """Open a window, then give R2 a second LCSC field in the PCB editor.

    KiCad's footprint dialog accepts a second field with a name the footprint
    already has, and keeps both until the board is reopened.
    """
    window = make_window(footprints=[Footprint("R1", lcsc=""), Footprint("R2")])
    footprint = window.pcbnew.GetBoard().footprints["R2"]
    footprint.fields["Second LCSC"] = support.Field("LCSC", "C555")
    return window


def _notes(window: Any) -> list[str]:
    """Return the repeated-field notes the window has logged, as users read them."""
    return [
        call.args[0] % call.args[1:]
        for call in window.logger.warning.call_args_list
        if "more than one field" in call.args[0]
    ]


def _activate(window: Any, active: bool) -> None:
    """Activate or deactivate the window, as switching windows does."""
    window.on_window_activated(
        SimpleNamespace(Skip=lambda: None, GetActive=lambda: active)
    )


def _real(window: Any, name: str) -> None:
    """Run the window's own method where the harness put a stand-in."""
    delattr(window, name)


def _refreshing_window(make_window: Callable[..., Any]) -> Any:
    """Open a window whose activation runs the list, enrichment, stock and estimate.

    Enrichment works out its targets but fetches nothing.
    """
    window = make_window(footprints=[Footprint("R1"), Footprint("R2", lcsc="C200")])
    window.assembly_lookup.request = MagicMock()
    window._why_standard_dialog = None
    window._bom_recompute_scheduled = False
    window.is_catalog_available = lambda: True
    for name in (
        "populate_footprint_list",
        "start_assembly_enrichment",
        "recompute_bom_estimate",
    ):
        _real(window, name)
    window.bom_estimator_board_count = 5
    window.bom_estimator_controller = BomEstimatorController(
        read_parts=window.read_assembly_parts,
        get_part_details=lambda _lcsc: {},
        get_board=window._held_board,
        is_force_standard_enabled=lambda: False,
        set_price_label=MagicMock(),
        set_standard_only_refs=MagicMock(),
        set_summary_text=MagicMock(),
        set_details_button_label=MagicMock(),
    )
    return window


def test_an_activation_reads_the_board_once(
    make_window: Callable[..., Any], mainwindow: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An activation's refresh and the estimate it schedules share one read.

    The list, enrichment, stock concerns and both estimates run on the main
    thread, where nothing can change the board meanwhile. An action still
    reads the board when it starts, and once the window was deactivated a
    scheduled estimate reads it again.
    """
    window = _refreshing_window(make_window)
    backend = _counted(window)

    _activate(window, True)
    window._run_coalesced_bom_recompute()
    assert backend.snapshots == 1

    window.footprint_list.GetSelections.return_value = ["R1"]
    act("paste", window, mainwindow, monkeypatch, "C300")
    assert window.pcbnew.GetBoard().footprints["R1"].field.text == "C300"
    after_paste = backend.snapshots
    assert after_paste > 1

    _activate(window, True)
    assert backend.snapshots == after_paste + 1
    _activate(window, False)
    window._run_coalesced_bom_recompute()
    assert backend.snapshots == after_paste + 2


def test_two_fields_of_one_name_are_noted_once_per_activation(
    make_window: Callable[..., Any], mainwindow: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The list keeps working and shows the later field; the log says so once.

    The first list rebuild after an activation notes it. Rebuilds and
    actions within the same activation do not repeat it.
    """
    window = _repeated_field_window(make_window)
    _real(window, "populate_footprint_list")
    board = window.pcbnew.GetBoard()

    window.populate_footprint_list()
    assert _notes(window) == [REPEATED_FIELD_NOTE]
    assert window.store.get_part("R2")["lcsc"] == "C555"

    # Showing the window activates it; Hide BOM rebuilds the list.
    _activate(window, True)
    window.populate_footprint_list()
    window.footprint_list.GetSelections.return_value = ["R1"]
    act("paste", window, mainwindow, monkeypatch, "C200")
    assert board.footprints["R1"].field.text == "C200"
    assert _notes(window) == [REPEATED_FIELD_NOTE]

    _activate(window, False)
    _activate(window, True)
    assert _notes(window) == [REPEATED_FIELD_NOTE] * 2
    assert window._project_storage_unavailable is False
    window.project_storage_status.SetLabel.assert_not_called()
