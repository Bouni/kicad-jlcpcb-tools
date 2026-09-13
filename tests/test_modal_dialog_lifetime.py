"""Ordinary dialog callers own destruction after the modal loop has unwound."""

from collections.abc import Iterator
from itertools import count
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from .modal_test_support import ModalDialog
from .wx_harness import load_siblings, mainwindow_stubs, wx_stubs


@pytest.fixture
def ui() -> Iterator[dict[str, Any]]:
    """Use production callers and close handlers with only the modal boundary fake."""
    package = "modal_lifetime_tests"
    stubs = mainwindow_stubs(
        package,
        wx=wx_stubs(Dialog=object, Frame=object, NewIdRef=count().__next__),
        dblib={"LIBRARY_CONFIGS": {}},
    )
    names = ("corrections", "part_preferences", "settings", "mainwindow")
    for name in names:
        stubs.pop(f"{package}.{name}", None)
    with load_siblings(package, names, stubs) as modules:
        yield modules


@pytest.mark.parametrize(
    "method,module,class_name,filter_id,expected_filter",
    [
        ("manage_corrections", "corrections", "CorrectionManagerDialog", None, ""),
        (
            "manage_part_preferences",
            "part_preferences",
            "PartPreferencesDialog",
            None,
            None,
        ),
        ("manage_settings", "settings", "SettingsDialog", None, None),
        (
            "add_correction",
            "corrections",
            "CorrectionManagerDialog",
            "REFERENCE",
            "^R1$",
        ),
        (
            "add_correction",
            "corrections",
            "CorrectionManagerDialog",
            "PACKAGE",
            "^Resistor:R_0603",
        ),
        ("add_correction", "corrections", "CorrectionManagerDialog", "NAME", "10k"),
    ],
)
@pytest.mark.parametrize("raises", [False, True])
def test_ordinary_callers_destroy_after_modal_exit_and_reopen(
    ui: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    module: str,
    class_name: str,
    filter_id: Optional[str],
    expected_filter: Optional[str],
    raises: bool,
) -> None:
    """Menu and toolbar paths preserve modal exit, cleanup, refresh, and reuse."""
    main = ui["mainwindow"]
    close = getattr(ui[module], class_name).quit_dialog
    children: list[ModalDialog] = []
    refreshes: list[int] = []
    failure = RuntimeError("modal operation failed") if raises else None

    def create(parent: Any, *args: object) -> ModalDialog:
        assert parent is window
        assert args == (() if expected_filter is None else (expected_filter,))
        child = ModalDialog()
        child.quit_dialog = lambda: close(child)
        child.error, child.instances = failure, children
        return child

    def refresh() -> None:
        assert all(child.destroyed for child in children)
        refreshes.append(len(children))

    monkeypatch.setattr(main, class_name, create)
    window = SimpleNamespace(
        footprint_list=SimpleNamespace(GetSelections=lambda: [1]),
        partlist_data_model=SimpleNamespace(
            get_reference=lambda _item: "R1",
            get_footprint=lambda _item: "Resistor:R_0603",
            get_value=lambda _item: "10k",
        ),
        populate_footprint_list=refresh,
    )
    command = getattr(main.JLCPCBTools, method)
    args = (
        (
            SimpleNamespace(
                GetId=lambda: getattr(main, f"ID_CONTEXT_MENU_ADD_ROT_BY_{filter_id}")
            ),
        )
        if filter_id
        else ()
    )
    for _attempt in range(2):
        if raises:
            with pytest.raises(RuntimeError, match="modal operation failed"):
                command(window, *args)
        else:
            command(window, *args)
        assert all(child.destroyed for child in children)
    assert len(children) == 2
    assert refreshes == ([1, 2] if module == "corrections" and not raises else [])
