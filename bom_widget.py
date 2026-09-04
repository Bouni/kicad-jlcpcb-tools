"""BOM estimator panel widget for the main window."""

# pyright: reportMissingImports=false, reportMissingModuleSource=false

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import suppress

import pcbnew  # pylint: disable=import-error
import wx  # pylint: disable=import-error

from .bom_estimation.assembly_mode import (
    AssemblyModeDecision,
    classify_component_product_type,
)
from .bom_estimation.pricing import calculate_bom_estimate, get_assembly_flags
from .bom_estimation.view import (
    format_bom_estimate_summary,
    prepare_bom_price_labels,
    standard_signal_reasons,
)
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
        on_help,
    ):
        self.parent = parent
        self.sizer = wx.BoxSizer(wx.VERTICAL)

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
        self.sizer.ShowItems(bool(show))

    def set_summary_text(self, text: str):
        """Set the estimator summary text block."""
        self.summary_label.SetLabel(text)


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
        set_trigger_refs: Callable[[set[str]], None],
        refresh_rows: Callable[[], None],
        set_summary_text: Callable[[str], None],
    ):
        self._read_parts = read_parts
        self._get_part_details = get_part_details
        self._get_board = get_board
        self._is_force_standard_enabled = is_force_standard_enabled
        self._set_price_label = set_price_label
        self._set_trigger_refs = set_trigger_refs
        self._refresh_rows = refresh_rows
        self._set_summary_text = set_summary_text

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

    def _get_board_standard_context(
        self,
        parts: list[Mapping[str, object]],
        board_count: int,
    ) -> AssemblyModeDecision:
        """Compute standard-mode trigger signals and assembly side usage.

        Walks the board to extract per-reference facts (sides, SMT vs THT,
        and JLC assembly classification), then delegates the policy decision to
        ``AssemblyModeDecision`` so the signal contract has one home.
        """
        board = self._get_board()
        top_refs: set[str] = set()
        bottom_refs: set[str] = set()
        smt_populated_sides: set[str] = set()
        standard_only_refs: set[str] = set()
        economic_only_refs: set[str] = set()
        classification_missing_refs: set[str] = set()

        for part in parts:
            if part.get("exclude_from_bom") or not str(part.get("lcsc") or ""):
                continue

            flags = get_assembly_flags(part)
            if bool(flags.get("is_dnp", False)) or bool(
                part.get("exclude_from_pos", False)
            ):
                continue

            reference = part.get("reference")
            if not reference:
                continue

            footprint = board.FindFootprintByReference(reference)
            if not footprint:
                continue

            side = "bottom" if self._is_on_bottom_side(footprint) else "top"
            reference = str(reference)
            (bottom_refs if side == "bottom" else top_refs).add(reference)

            is_tht = False
            with suppress(TypeError, ValueError):
                is_tht = bool(int(part.get("has_tht") or 0))
            if not is_tht:
                smt_populated_sides.add(side)

            product_type = classify_component_product_type(
                part.get("component_product_type")
            )
            if product_type == 2:
                standard_only_refs.add(reference)
            elif product_type == 1:
                economic_only_refs.add(reference)
            elif product_type is None:
                classification_missing_refs.add(reference)

        return AssemblyModeDecision(
            manual_enabled=bool(self._is_force_standard_enabled()),
            board_count=board_count,
            top_refs=top_refs,
            bottom_refs=bottom_refs,
            smt_populated_side_count=len(smt_populated_sides),
            standard_only_refs=standard_only_refs,
            economic_only_refs=economic_only_refs,
            classification_missing_refs=classification_missing_refs,
        )

    def recompute(self, board_count: int):
        """Recompute and apply estimated BOM+assembly UI/model updates.

        This method is synchronous and must be called on the UI/main thread.
        It performs no background work and relies only on injected callbacks
        for store reads and UI/datamodel updates.
        """
        raw_parts = self._read_parts()
        parts = raw_parts if isinstance(raw_parts, list) else []
        if not any(
            not part.get("exclude_from_bom") and str(part.get("lcsc") or "")
            for part in parts
        ):
            self._set_trigger_refs(set())
            self._refresh_rows()
            reason = "no parts" if not parts else "no assigned BOM parts"
            self._set_summary_text(f"BOM Estimate ({board_count} boards): {reason}")
            return AssemblyModeDecision(
                board_count, bool(self._is_force_standard_enabled())
            )

        decision = self._get_board_standard_context(parts, board_count)

        summary = calculate_bom_estimate(
            parts=parts,
            board_count=board_count,
            get_part_details=self._get_part_details,
            board_standard=decision.board_standard,
            smt_populated_sides=decision.smt_populated_side_count,
        )

        mode = "Standard" if decision.board_standard else "Economic"
        signals = {
            "manual_enabled": decision.manual_enabled,
            "quantity_over_50": decision.quantity_over_50,
            "standard_part_present": bool(decision.standard_only_refs),
            "multi_side_populated": decision.both_sides_populated,
        }
        reasons = standard_signal_reasons(signals)
        reason_text = ", ".join(reasons) if reasons else "none"
        highlight_refs = set(decision.standard_only_refs)
        if decision.both_sides_populated:
            highlight_refs.update(decision.top_refs)
            highlight_refs.update(decision.bottom_refs)

        for reference, price_label in prepare_bom_price_labels(
            parts,
            board_count,
            self._get_part_details,
        ).items():
            self._set_price_label(reference, price_label)

        self._set_trigger_refs(set(highlight_refs))
        self._refresh_rows()

        overview_line, details_line = format_bom_estimate_summary(
            summary,
            board_count,
            mode,
            reason_text,
        )
        self._set_summary_text(f"{overview_line}\n{details_line}")
        return decision
