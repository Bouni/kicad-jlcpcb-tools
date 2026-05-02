"""BOM estimator panel widget for the main window."""

# pyright: reportMissingImports=false, reportMissingModuleSource=false

from collections.abc import Callable, Mapping
from contextlib import suppress
import json

import wx  # pylint: disable=import-error

from .bom_estimation.pricing import calculate_bom_estimate
from .bom_estimation.view import format_bom_estimate_summary, prepare_bom_price_labels
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
        """Return True when a footprint is on the bottom side."""
        with suppress(Exception):  # pylint: disable=broad-exception-caught
            if bool(footprint.IsFlipped()):
                return True
        return str(footprint.GetLayer()) != "0"

    @staticmethod
    def _board_has_v_cut_drawings(board) -> bool:
        """Detect whether the board contains drawings on any V-cut layer."""
        for drawing in board.GetDrawings():
            get_layer = getattr(drawing, "GetLayer", None)
            if not callable(get_layer):
                continue
            layer_id = get_layer()
            with suppress(Exception):  # pylint: disable=broad-exception-caught
                layer_name = str(board.GetLayerName(layer_id)).upper()
                normalized = layer_name.replace("-", "_")
                if "V_CUT" in normalized or "VCUT" in normalized:
                    return True
        return False

    def _get_board_standard_context(
        self,
        parts: list[Mapping[str, object]],
        board_count: int,
    ) -> dict[str, object]:
        """Compute standard-mode trigger signals and assembly side usage."""
        board = self._get_board()
        populated_sides = set()
        populated_refs = set()
        smt_populated_sides = set()
        standard_part_present = False
        standard_part_refs = set()

        for part in parts:
            if part.get("exclude_from_bom") or not str(part.get("lcsc") or ""):
                continue

            flags = {}
            with suppress(TypeError, ValueError, json.JSONDecodeError):
                flags = json.loads(part.get("assembly_flags") or "{}")
            if bool(flags.get("is_dnp", False)) or bool(
                flags.get("exclude_from_pos", False)
            ):
                continue

            reference = part.get("reference")
            if not reference:
                continue

            footprint = board.FindFootprintByReference(reference)
            if not footprint:
                continue

            side = "bottom" if self._is_on_bottom_side(footprint) else "top"
            populated_sides.add(side)
            populated_refs.add(reference)

            is_tht = False
            with suppress(TypeError, ValueError):
                is_tht = bool(int(part.get("has_tht") or 0))
            if not is_tht:
                smt_populated_sides.add(side)

            with suppress(TypeError, ValueError):
                if int(part.get("component_product_type")) != 0:
                    standard_part_present = True
                    standard_part_refs.add(reference)

        signals = {
            "manual_enabled": bool(self._is_force_standard_enabled()),
            "qty_50_plus": board_count >= 50,
            "v_cut_drawings": self._board_has_v_cut_drawings(board),
            "standard_part_present": standard_part_present,
            "multi_side_populated": len(populated_sides) > 1,
        }
        trigger_references = set(standard_part_refs)
        if signals["multi_side_populated"]:
            trigger_references.update(populated_refs)

        return {
            "signals": signals,
            "board_standard": any(signals.values()),
            "smt_populated_sides": len(smt_populated_sides),
            "trigger_references": trigger_references,
        }

    @staticmethod
    def _standard_signal_reasons(signals: Mapping[str, object]) -> list[str]:
        """Build user-facing reason labels for active Standard-mode triggers."""
        reason_map = [
            ("manual_enabled", "manual"),
            ("qty_50_plus", "qty≥50"),
            ("v_cut_drawings", "V-cut layer"),
            ("standard_part_present", "standard part"),
            ("multi_side_populated", "both sides populated"),
        ]
        return [label for key, label in reason_map if signals.get(key)]

    def recompute(self, board_count: int):
        """Recompute and apply estimated BOM+assembly UI/model updates."""
        raw_parts = self._read_parts()
        parts = raw_parts if isinstance(raw_parts, list) else []
        if not parts:
            self._set_trigger_refs(set())
            self._refresh_rows()
            self._set_summary_text(f"BOM Estimate ({board_count} boards): no parts")
            return

        bom_parts = [
            part
            for part in parts
            if not part.get("exclude_from_bom") and str(part.get("lcsc") or "")
        ]
        if not bom_parts:
            self._set_trigger_refs(set())
            self._refresh_rows()
            self._set_summary_text(
                f"BOM Estimate ({board_count} boards): no assigned BOM parts"
            )
            return

        standard_context = self._get_board_standard_context(parts, board_count)

        summary = calculate_bom_estimate(
            parts=parts,
            board_count=board_count,
            get_part_details=self._get_part_details,
            board_standard=bool(standard_context["board_standard"]),
            smt_populated_sides=int(standard_context["smt_populated_sides"]),
        )

        mode = "Standard" if standard_context["board_standard"] else "Economic"
        reasons = self._standard_signal_reasons(standard_context["signals"])
        reason_text = ", ".join(reasons) if reasons else "none"
        highlight_refs = (
            standard_context["trigger_references"]
            if standard_context["board_standard"]
            else set()
        )

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
