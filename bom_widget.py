"""BOM estimator panel widget for the main window."""

# pyright: reportMissingImports=false, reportMissingModuleSource=false

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress

import pcbnew  # pylint: disable=import-error
import wx  # pylint: disable=import-error

from .bom_estimation.assembly_mode import AssemblyModeDecision
from .bom_estimation.view import evaluate_bom_estimate, selected_assembly_parts
from .helpers import HighResWxSize


class BomEstimatorWidget:
    """Owns BOM estimator controls and summary label UI."""

    def __init__(
        self,
        parent,
        *,
        window,
        board_count: int,
        force_standard: bool,
        on_board_count_spin,
        on_board_count_text,
        on_board_count_text_timer,
        on_force_standard_changed,
        on_details,
        on_help,
    ):
        self.parent = parent
        self.sizer = wx.BoxSizer(wx.VERTICAL)
        self._visible = True
        self._details_button_label = None

        controls_sizer = wx.BoxSizer(wx.HORIZONTAL)
        controls_sizer.Add(
            wx.StaticText(parent, wx.ID_ANY, "Boards:"),
            0,
            wx.LEFT | wx.RIGHT | wx.ALIGN_CENTER_VERTICAL,
            5,
        )

        self.boards_input = wx.SpinCtrl(
            parent,
            wx.ID_ANY,
            min=5,
            max=10000,
            initial=board_count,
            size=HighResWxSize(window, wx.Size(90, -1)),
        )
        if hasattr(self.boards_input, "SetIncrement"):
            self.boards_input.SetIncrement(5)

        self.text_timer = wx.Timer(parent)
        parent.Bind(wx.EVT_TIMER, on_board_count_text_timer, self.text_timer)
        self.boards_input.Bind(wx.EVT_SPINCTRL, on_board_count_spin)
        self.boards_input.Bind(wx.EVT_TEXT, on_board_count_text)

        controls_sizer.Add(
            self.boards_input,
            0,
            wx.RIGHT | wx.ALIGN_CENTER_VERTICAL,
            10,
        )

        self.standard_checkbox = wx.CheckBox(parent, wx.ID_ANY, "Force Standard")
        self.standard_checkbox.SetValue(force_standard)
        self.standard_checkbox.Bind(wx.EVT_CHECKBOX, on_force_standard_changed)
        controls_sizer.Add(
            self.standard_checkbox,
            0,
            wx.RIGHT | wx.ALIGN_CENTER_VERTICAL,
            10,
        )

        self.details_button = wx.Button(parent, wx.ID_ANY, "Why Standard…")
        self.details_button.Bind(wx.EVT_BUTTON, on_details)
        self.details_button.Hide()
        controls_sizer.Add(
            self.details_button,
            0,
            wx.RIGHT | wx.ALIGN_CENTER_VERTICAL,
            10,
        )

        self.help_button = wx.Button(parent, wx.ID_ANY, "Help")
        self.help_button.SetToolTip(
            wx.ToolTip("Show BOM estimator assumptions and limitations")
        )
        self.help_button.Bind(wx.EVT_BUTTON, on_help)
        controls_sizer.Add(self.help_button, 0, wx.ALIGN_CENTER_VERTICAL, 0)

        self.sizer.Add(controls_sizer, 0, wx.EXPAND)

        self.summary_label = wx.StaticText(
            parent,
            wx.ID_ANY,
            "BOM Estimate: waiting for assigned LCSC parts\n"
            "Assign LCSC parts to calculate cost details",
        )
        self.sizer.Add(
            self.summary_label,
            0,
            wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND,
            5,
        )

    def set_visible(self, show: bool):
        """Show or hide the full estimator panel."""
        self._visible = bool(show)
        self.sizer.ShowItems(self._visible)
        self.details_button.Show(self._visible and bool(self._details_button_label))

    def set_summary_text(self, text: str):
        """Set the estimator summary text block."""
        self.summary_label.SetLabel(text)

    def set_details_button_label(self, label):
        """Show the applicable details action, or hide it when unnecessary."""
        self._details_button_label = label
        if label:
            self.details_button.SetLabel(label)
        self.details_button.Show(self._visible and bool(label))
        self.parent.Layout()


class BomEstimatorController:
    """Coordinates BOM estimator recompute and UI/model updates."""

    def __init__(
        self,
        *,
        read_parts: Callable[[], object],
        get_part_details: Callable[[str], dict],
        get_board: Callable[[], object],
        is_force_standard_enabled: Callable[[], bool],
        set_price_label: Callable[[str, str], None],
        set_standard_only_refs: Callable[[set[str]], None],
        set_summary_text: Callable[[str], None],
        set_details_button_label: Callable[[object], None],
    ) -> None:
        self._read_parts = read_parts
        self._get_part_details = get_part_details
        self._get_board = get_board
        self._is_force_standard_enabled = is_force_standard_enabled
        self._set_price_label = set_price_label
        self._set_standard_only_refs = set_standard_only_refs
        self._set_summary_text = set_summary_text
        self._set_details_button_label = set_details_button_label

    @staticmethod
    def _is_on_bottom_side(footprint) -> bool:
        """Return True when a footprint is on the bottom side.

        Catches AttributeError (older pcbnew API without IsFlipped) and
        RuntimeError (footprint object destroyed by SWIG between layout
        rebuilds) — narrower than the previous broad suppress(Exception).
        """
        with suppress(AttributeError, RuntimeError):
            if bool(footprint.IsFlipped()):
                return True
        return footprint.GetLayer() != pcbnew.F_Cu

    def recompute(self, board_count: int) -> AssemblyModeDecision:
        """Read current board facts and apply one estimate result on the UI thread."""
        raw_parts = self._read_parts()
        parts = raw_parts if isinstance(raw_parts, list) else []
        placed_parts = list(selected_assembly_parts(parts))
        board = self._get_board() if placed_parts else None
        sides = {}
        for part in placed_parts:
            reference = str(part["reference"])
            footprint = board.FindFootprintByReference(reference) if board else None
            if footprint:
                sides[reference] = (
                    "bottom" if self._is_on_bottom_side(footprint) else "top"
                )
        result = evaluate_bom_estimate(
            parts,
            board_count,
            self._get_part_details,
            sides=sides,
            force_standard=bool(self._is_force_standard_enabled()),
        )
        for reference, label in result.price_labels.items():
            self._set_price_label(reference, label)
        self._set_standard_only_refs(set(result.decision.standard_only_refs))
        self._set_details_button_label(result.details_button_label)
        self._set_summary_text(result.summary_text)
        return result.decision
