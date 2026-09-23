"""Contains the Action Plugin."""

from collections.abc import Callable
from functools import partial
import os
from typing import Any, Optional

import pcbnew  # pylint: disable=import-error
import wx

from .board_context import BoardContextChanged, board_identity
from .core.version import is_supported_version


class JLCPCBPlugin(pcbnew.ActionPlugin):
    """JLCPCBPlugin instance of ActionPlugin."""

    def defaults(self) -> None:
        """Define defaults."""
        # pylint: disable=attribute-defined-outside-init
        self.name = "JLCPCB Tools"
        self.category = "Fabrication data generation"
        self.description = (
            "Generate JLCPCB-compatible Gerber, Excellon, BOM and CPL files"
        )
        self.show_toolbar_button = True
        path, _ = os.path.split(os.path.abspath(__file__))
        self.icon_file_name = os.path.join(path, "jlcpcb-icon.png")
        self._inside_run = False
        self._dispatching = False
        self._pending_action: Optional[Callable[[], None]] = None
        self._action_error: Optional[BaseException] = None

    def Run(self) -> None:
        """Open the modeless window or execute one native menu-dispatched edit."""
        if not is_supported_version(pcbnew.GetBuildVersion()):
            wx.MessageBox(
                "JLCPCB Tools requires KiCad 7.0 or newer.",
                "Unsupported KiCad version",
                wx.OK | wx.ICON_ERROR,
            )
            return

        if self._pending_action is not None:
            action, self._pending_action = self._pending_action, None
            try:
                if not pcbnew.IsActionRunning():
                    raise RuntimeError("KiCad did not enter its native action wrapper")
                action()
            except BaseException as error:
                # Return normally so KiCad can finish its undo/dirty transaction.
                # The caller re-raises the original error after ProcessEvent returns.
                self._action_error = error
            return

        from .mainwindow import JLCPCBTools  # noqa: PLC0415

        try:
            frames = [
                frame
                for frame in wx.GetTopLevelWindows()
                if frame
                and not frame.IsBeingDeleted()
                and hasattr(frame, "GetMenuBar")
                and self._menu_id(frame) is not None
            ]
            if len(frames) != 1:
                raise RuntimeError(
                    "Unable to identify the PCB editor's JLCPCB Tools command"
                )
            context = (frames[0], self._board_id())
            self._inside_run = True
            dialog = JLCPCBTools(
                None, board_action=partial(self.run_board_action, context=context)
            )
            dialog.Center()
            dialog.Show()
        except Exception as exc:
            wx.MessageBox(str(exc), "JLCPCB Tools", wx.OK | wx.ICON_ERROR)
        finally:
            self._inside_run = False

    @staticmethod
    def _board_id() -> str:
        """Capture native lifetime and filename without retaining a stale board."""
        board = pcbnew.GetBoard()
        return board_identity(board)

    def _menu_id(self, frame: Any) -> Optional[int]:
        """Resolve this plugin's unique current command, including rebuilt menus."""
        menubar = frame.GetMenuBar()
        if not menubar:
            return None

        def matching_ids(menu: Any) -> list[int]:
            ids = []
            for item in menu.GetMenuItems():
                submenu = item.GetSubMenu()
                if submenu:
                    ids.extend(matching_ids(submenu))
                elif item.GetItemLabelText() == self.name:
                    ids.append(item.GetId())
            return ids

        ids = [
            item_id for menu, _ in menubar.GetMenus() for item_id in matching_ids(menu)
        ]
        return ids[0] if len(ids) == 1 else None

    def run_board_action(
        self, action: Callable[[], None], *, context: tuple[Any, str]
    ) -> None:
        """Use KiCad's action wrapper to give modeless edits native undo and dirty state.

        The SWIG bindings expose neither BOARD_COMMIT nor editor OnModify.
        KiCad's existing menu handler wraps Run in its native transaction:
        pcbnew/python/scripting/pcbnew_action_plugins.cpp::RunActionPlugin.
        """
        if self._dispatching:
            raise RuntimeError("A JLCPCB Tools board edit is already running")
        frame, board_id = context
        if not frame or frame.IsBeingDeleted() or self._board_id() != board_id:
            raise BoardContextChanged(
                "The original PCB is no longer open; reopen JLCPCB Tools"
            )
        if not frame.IsEnabled():
            raise RuntimeError(
                "Close the PCB editor's active dialog before editing parts"
            )
        if self._inside_run:
            if not pcbnew.IsActionRunning():
                raise RuntimeError("KiCad did not enter its native action wrapper")
            action()  # Constructor preference application belongs to the original Run.
            return
        if pcbnew.IsActionRunning():
            raise RuntimeError("Another KiCad action is already running")
        menu_id = self._menu_id(frame)
        if menu_id is None:
            raise RuntimeError("The PCB editor's JLCPCB Tools command is unavailable")

        def apply_to_original_board() -> None:
            if self._board_id() != board_id:
                raise BoardContextChanged(
                    "The original PCB is no longer open; reopen JLCPCB Tools"
                )
            action()

        self._dispatching = True
        self._pending_action = apply_to_original_board
        self._action_error = None
        try:
            frame.GetEventHandler().ProcessEvent(
                wx.CommandEvent(wx.EVT_MENU.typeId, menu_id)
            )
            if self._pending_action is not None:
                raise RuntimeError("KiCad did not execute the requested board edit")
            if self._action_error is not None:
                raise self._action_error
        finally:
            self._pending_action = None
            self._action_error = None
            self._dispatching = False
