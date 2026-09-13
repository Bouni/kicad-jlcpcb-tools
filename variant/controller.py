"""Main-dialog integration for simultaneous native KiCad design variants."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
import re
from threading import Thread
from typing import Any, Optional
from uuid import uuid4

import wx

from ..bom_estimation.assembly_mode import (
    ComponentProductType,
    classify_component_product_type,
)
from ..bom_estimation.view import BomEstimateResult, evaluate_bom_estimate
from ..correction_data import resolve_shared_corrections
from ..corrections import CorrectionManagerDialog
from ..dataview_highlight import simplify_footprint_name
from ..derive_params import params_for_part
from ..enrichment.providers import LCSCAssemblyMetadataProvider
from ..partselector import PartSelectorDialog
from .matrix_model import EDITABLE_FIELDS, CatalogMetadata, CorrectionState, MatrixModel
from .matrix_view import MatrixTarget, VariantMatrixView, coordinates_for
from .native import BoardVariantSnapshot, VariantEdit, VariantNativeAdapter
from .session import VariantSession, VariantSessionError


@dataclass(frozen=True)
class _Presentation:
    """One prepared source shared by display-only changes and output selection."""

    source_token: str
    board_count: int
    force_standard: bool
    metadata: dict[tuple[str, str], CatalogMetadata]
    corrections: dict[str, CorrectionState]
    estimates: dict[str, BomEstimateResult]
    rows: dict[str, list[dict[str, Any]]]


class VariantMainController:
    """Keep matrix focus, assignment sessions and output selection independent."""

    def __init__(self, dialog: Any, cache: Any) -> None:
        self.dialog = dialog
        self.cache = cache
        self.session = VariantSession(
            VariantNativeAdapter(dialog.pcbnew.GetBoard(), cache.board_id),
            cache,
            dialog.pcbnew.GetBoard,
        )
        self.closed = False
        self._refreshing = False
        self._clipboard: Any = None
        self._clipboard_nonce = ""
        self._pending: set[str] = set()
        self._render_queued = False
        self._presentation: Optional[_Presentation] = None
        self._attempted: set[str] = set()
        self._metadata_errors: set[str] = set()
        self.panel = wx.Panel(dialog.footprint_list.GetParent())
        layout = wx.BoxSizer(wx.VERTICAL)
        tools = wx.BoxSizer(wx.HORIZONTAL)
        tools.Add(
            wx.StaticText(self.panel, label="Output variant:"),
            0,
            wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
            5,
        )
        self.output_choice = wx.Choice(self.panel)
        self.output_choice.Bind(wx.EVT_CHOICE, self._on_output)
        tools.Add(self.output_choice, 0, wx.RIGHT, 12)
        self.differences = wx.CheckBox(self.panel, label="Differences only")
        self.differences.Bind(wx.EVT_CHECKBOX, self._on_display_changed)
        tools.Add(self.differences, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        self.show_footprint_library = wx.CheckBox(
            self.panel, label="Show footprint library"
        )
        self.show_footprint_library.SetValue(False)
        self.show_footprint_library.Bind(wx.EVT_CHECKBOX, self._on_display_changed)
        tools.Add(
            self.show_footprint_library,
            0,
            wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
            10,
        )
        refresh_button = wx.Button(self.panel, label="Refresh")
        refresh_button.Bind(wx.EVT_BUTTON, lambda event: self.refresh())
        tools.Add(refresh_button, 0, wx.RIGHT, 5)
        layout.Add(tools, 0, wx.EXPAND | wx.BOTTOM, 5)
        self.view = VariantMatrixView(
            self.panel,
            MatrixModel(
                self.session.snapshot,
                board_count=dialog.bom_estimator_board_count,
                show_footprint_library=self.show_footprint_library.GetValue(),
            ),
            on_target_changed=self._on_target,
            on_selection_changed=self._on_selection_changed,
            on_activate=self.select_part,
            on_edit=self._on_edit,
            on_action=self.dispatch_action,
        )
        layout.Add(self.view, 1, wx.EXPAND)
        self.panel.SetSizer(layout)
        table = dialog.footprint_list.GetContainingSizer()
        table.Replace(dialog.footprint_list, self.panel)
        dialog.footprint_list.Hide()
        if tooltip := getattr(dialog, "_type_cell_tooltip", None):
            tooltip.stop()
        dialog.right_toolbar.ToggleTool(8, False)
        dialog.right_toolbar.SetToolShortHelp(
            8, "Explicitly select matching components within this variant"
        )
        preferences = self.cache.get_display_preferences()
        self.dialog.hide_bom_parts = bool(preferences.get("require_bom", False))
        self.dialog.hide_pos_parts = bool(preferences.get("require_pos", False))
        self.dialog.right_toolbar.ToggleTool(13, self.dialog.hide_bom_parts)
        self.dialog.right_toolbar.ToggleTool(14, self.dialog.hide_pos_parts)
        for attribute, active, field in (
            ("hide_bom_button", self.dialog.hide_bom_parts, "BOM"),
            ("hide_pos_button", self.dialog.hide_pos_parts, "POS"),
        ):
            button = getattr(self.dialog, attribute, None)
            if button is not None:
                button.SetLabel(f"{'Show' if active else 'Hide'} excluded {field}")
        self.differences.SetValue(bool(preferences.get("differences_only", False)))
        self.show_footprint_library.SetValue(
            preferences.get("show_footprint_library") is True
        )
        self.recompute()
        self.view.restore_preferences(preferences)
        self.timer = wx.Timer(dialog)
        dialog.Bind(wx.EVT_TIMER, self._on_timer, self.timer)
        self.timer.Start(1500)
        dialog.Layout()

    @property
    def model(self) -> Any:
        """Resolve actions against the displayed model, including during a drag."""
        return self.view.model

    @property
    def output_name(self) -> str:
        """Return a readable output label without converting it into an identity."""
        return self.variant_label(self.session.output_variant)

    def variant_label(self, name: str) -> str:
        """Preserve KiCad's distinct label for the base, including localization."""
        return {item.name: item.label for item in self.session.snapshot.variants}.get(
            name, name
        )

    def _error(self, error: BaseException) -> None:
        if self.closed:
            return
        self.dialog.logger.warning("Variant operation failed: %s", error)
        # Native modal entry can take mouse capture. Invalidate a stale drag
        # before that event can publish any queued table update.
        self._update_enabled()
        wx.MessageBox(str(error), "Design variants", wx.OK | wx.ICON_ERROR, self.dialog)

    def _publish_output(self) -> None:
        names = [variant.name for variant in self.session.snapshot.variants]
        selected = self.session.output_variant
        self.output_choice.SetItems([self.variant_label(name) for name in names])
        self.output_choice.SetSelection(
            names.index(selected) if selected in names else wx.NOT_FOUND
        )
        self.view.set_output_variant(selected if selected in names else None)
        self.dialog.generate_button.SetLabel(f"Generate {self.output_name}")
        self.dialog.right_toolbar.EnableTool(16, selected == "")
        self.dialog.right_toolbar.SetToolShortHelp(
            16,
            "Export explicit Default assignments to the schematic"
            if selected == ""
            else "Named-variant schematic export is unavailable; select Default as Output variant.",
        )
        self._update_enabled()

    def output_rows(self) -> list[dict[str, Any]]:
        """Read captured output rows independently of matrix focus or fabrication."""
        self.session._check_board()
        snapshot = self.session.snapshot
        name = self.session.output_variant
        if name not in {variant.name for variant in snapshot.variants}:
            name = ""
        return self.cache.assembly_rows(snapshot, name)

    def _update_enabled(self) -> None:
        ready = self.session.reliable and not self.session.generating
        available = self.session.output_variant in {
            item.name for item in self.session.snapshot.variants
        }
        self.view.set_mutations_enabled(ready)
        self.output_choice.Enable(ready)
        self.dialog.generate_button.Enable(ready and available)
        self.dialog.right_toolbar.Enable(ready)
        self.dialog.right_toolbar.EnableTool(
            16, ready and self.session.output_variant == ""
        )
        self.dialog.right_toolbar.EnableTool(13, not self.session.generating)
        self.dialog.right_toolbar.EnableTool(14, not self.session.generating)
        self._on_target(self.view.selected_target)

    def catalog_changed(self) -> None:
        """Consume the parent's catalog publication without replacing its provider."""
        if not self.session.generating:
            self._presentation = None
            self.refresh()
        else:
            # end_generation refreshes after releasing the captured native source.
            self._update_enabled()

    def refresh(self) -> None:
        """Reread live native data before publishing a complete matrix."""
        if self.closed or self._refreshing or self.session.generating:
            return
        self._refreshing = True
        try:
            self.session.refresh()
            self.dialog._invalidate_catalog_details()
            self._presentation = None
            self._attempted.intersection_update(self._pending)
            self._metadata_errors.clear()
            self.render()
            self.start_enrichment()
            if getattr(self, "timer", None) is not None:
                self.timer.Start(1500)
        except Exception as error:
            self.session.reliable = False
            self._error(error)
        finally:
            self._refreshing = False

    def _on_timer(self, event: Any) -> None:
        if self.closed or self._refreshing or self.session.generating:
            return
        try:
            previous = self.session.snapshot.source_token
            reliable = self.session.reliable
            latest = self.session.refresh()
            if not reliable or latest.source_token != previous:
                self.render()
                self.start_enrichment()
        except Exception as error:
            # One visible failure is sufficient; Refresh remains a deliberate retry.
            self.session.reliable = False
            self.timer.Stop()
            if reliable:
                self._error(error)

    def _details(self, lcsc: str) -> dict[str, Any]:
        if not lcsc or not self.dialog.is_catalog_available():
            return {}
        return self.dialog._catalog_get_part_details(lcsc, strict=True) or {}

    def recompute(self) -> None:
        """Report preparation failures at the main dialog's event boundary."""
        if self.closed or self.session.generating:
            return
        try:
            self.render()
        except Exception as error:
            self._error(error)

    def render(self) -> None:
        """Publish a projection of the captured source without rereading KiCad."""
        try:
            self._render_snapshot()
        except Exception:
            self.session.reliable = False
            self._update_enabled()
            raise

    def _on_display_changed(self, _event: Any) -> None:
        self.recompute()

    def _prepare(self, snapshot: BoardVariantSnapshot) -> _Presentation:
        """Calculate shared corrections and per-variant estimates once per source."""
        metadata: dict[tuple[str, str], CatalogMetadata] = {}
        corrections: dict[str, CorrectionState] = {}
        estimates: dict[str, BomEstimateResult] = {}
        rows_by_variant: dict[str, list[dict[str, Any]]] = {}
        correction_snapshot = self.dialog.library.read_correction_data()
        self.dialog.update_correction_status(correction_snapshot)
        rules = correction_snapshot.corrections
        matches = (
            resolve_shared_corrections(snapshot, rules) if rules is not None else {}
        )
        for part in snapshot.for_variant(""):
            if rules is None:
                corrections[part.component_id] = CorrectionState(status="error")
                continue
            match = matches[part.component_id]
            rotation = match.correction.rotation if match else 0
            offset = match.correction.offset if match else (0.0, 0.0)
            angle = part.pcb_angle if part.side == "TOP" else 180 - part.pcb_angle
            corrections[part.component_id] = CorrectionState(
                rotation=rotation,
                offset_x=offset[0],
                offset_y=offset[1],
                source=match.source if match else "",
                final_angle=(angle + rotation) % 360,
            )
        for variant in snapshot.variants:
            parts = snapshot.for_variant(variant.name)
            rows = self.cache.assembly_rows(snapshot, variant.name)
            rows_by_variant[variant.name] = rows
            result = evaluate_bom_estimate(
                rows,
                self.dialog.bom_estimator_board_count,
                self._details,
                sides={part.reference: part.side.lower() for part in parts},
                force_standard=self.dialog.bom_estimator_force_standard,
            )
            estimates[variant.name] = result
            by_reference = {row["reference"]: row for row in rows}
            for part in parts:
                cached = by_reference.get(part.reference, {})
                catalog = self._details(part.lcsc)
                classification = classify_component_product_type(
                    cached.get("component_product_type")
                )
                classification_known = bool(part.lcsc) and classification is not None
                metadata[(part.component_id, variant.name)] = CatalogMetadata(
                    params=params_for_part(catalog),
                    type=str(catalog.get("type") or ""),
                    standard=(classification == ComponentProductType.STANDARD_ONLY)
                    if classification_known
                    else None,
                    stock=catalog.get("stock"),
                    price=result.prices.get(part.reference),
                    price_label=result.price_labels.get(part.reference, ""),
                    status=(
                        "pending"
                        if part.lcsc in self._pending
                        else "error"
                        if part.lcsc in self._metadata_errors
                        else "complete"
                        if classification_known
                        else "missing"
                    ),
                    lcsc=part.lcsc,
                    package=str(catalog.get("package") or ""),
                    description=str(catalog.get("description") or ""),
                )
        return _Presentation(
            snapshot.source_token,
            self.dialog.bom_estimator_board_count,
            self.dialog.bom_estimator_force_standard,
            metadata,
            corrections,
            estimates,
            rows_by_variant,
        )

    def _render_snapshot(self) -> None:
        snapshot = self.session.snapshot
        prepared = self._presentation
        if prepared is None or (
            prepared.source_token,
            prepared.board_count,
            prepared.force_standard,
        ) != (
            snapshot.source_token,
            self.dialog.bom_estimator_board_count,
            self.dialog.bom_estimator_force_standard,
        ):
            prepared = self._prepare(snapshot)
            self._presentation = prepared
        model = MatrixModel(
            snapshot,
            prepared.metadata,
            prepared.corrections,
            board_count=prepared.board_count,
            show_footprint_library=self.show_footprint_library.GetValue(),
        )
        model.set_filter(
            require_bom=self.dialog.hide_bom_parts,
            require_pos=self.dialog.hide_pos_parts,
            differences_only=self.differences.GetValue(),
        )
        self.view.set_model(model)
        result = prepared.estimates.get(self.session.output_variant)
        details_dialog = getattr(self.dialog, "_why_standard_dialog", None)
        self.dialog.bom_estimator_decision = result.decision if result else None
        self.dialog.bom_widget.set_summary_text(
            result.summary_text
            if result
            else f"Output variant unavailable: {self.output_name}. Select an existing output variant."
        )
        self.dialog.bom_widget.set_details_button_label(
            result.details_button_label if result else None
        )
        if details_dialog is not None:
            if result is None:
                details_dialog.Close()
            else:
                details_dialog.update_content(
                    result.decision, prepared.rows[self.session.output_variant]
                )
        self._publish_output()
        self.panel.Layout()

    def _on_output(self, event: Any) -> None:
        """Keep the choice and generation target aligned after preference failures."""
        if self.closed:
            return
        try:
            self.session.set_output_variant(
                self.session.snapshot.variants[event.GetSelection()].name
            )
            self.render()
        except Exception as error:
            names = [variant.name for variant in self.session.snapshot.variants]
            selected = self.session.output_variant
            self.output_choice.SetSelection(
                names.index(selected) if selected in names else wx.NOT_FOUND
            )
            self._error(error)

    def _on_target(self, target: Optional[MatrixTarget]) -> None:
        if self.closed:
            return
        enabled = bool(
            target is not None
            and target.variant is not None
            and self.view.selected_component_ids()
            and self.session.reliable
            and not self.session.generating
        )
        self.dialog.enable_part_specific_toolbar_buttons(enabled)
        self.dialog.right_toolbar.EnableTool(8, enabled)

    def _on_selection_changed(self, component_ids: tuple[str, ...]) -> None:
        """Mirror completed matrix selection onto live physical PCB footprints."""
        if self.closed:
            return
        self._on_target(self.view.selected_target)
        pcbnew = self.dialog.pcbnew
        try:
            board = pcbnew.GetBoard()
            if not self.session.adapter.is_current_board(board):
                return
            footprints = tuple(board.GetFootprints())
            wanted = set(component_ids)
            targets = [fp for fp in footprints if str(fp.m_Uuid.AsString()) in wanted]
            # GetCurrentSelection can also contain tracks or other board items.
            # Include flagged footprints so a prior programmatic selection is
            # cleared even when it is absent from the native selection tool.
            selected = list(pcbnew.GetCurrentSelection())
            selected.extend(fp for fp in footprints if fp.IsSelected())
            for item in selected:
                item.ClearSelected()
            for footprint in targets:
                footprint.SetSelected()
            pcbnew.Refresh()
        except Exception as error:
            self.dialog.logger.warning("Could not select PCB footprints: %s", error)

    def _targets(self) -> tuple[Any, ...]:
        state = self.view.capture_state()
        target, components = state.target, state.components
        if target is None or target.variant is None or not components:
            raise VariantSessionError(
                "Select components within one variant; use Copy to variants for multiple destinations."
            )
        return tuple(
            self.session.snapshot.target(component, target.variant)
            for component in components
        )

    def _apply(self, edits: Sequence[Any]) -> None:
        try:
            self.session.apply(edits)
        except Exception:
            # Native compensation can successfully reread newer external state.
            # Publish that recovered view before accepting another grid action.
            if self.session.reliable:
                try:
                    self.render()
                except Exception as refresh_error:
                    self.dialog.logger.warning(
                        "Could not display recovered variant state: %s", refresh_error
                    )
            raise
        self.render()
        self.start_enrichment()
        self.dialog.pcbnew.Refresh()

    def select_part(self, *_: Any) -> None:
        """Open or explicitly retarget a selector with immutable native destinations."""
        try:
            targets = self._targets()
            context = self.session.begin_assignment(targets)
            parts = [
                self.session.snapshot.get(target.component_id, target.variant_name)
                for target in targets
            ]
            selection = {}
            for part in parts:
                value = part.value
                if part.reference.startswith("R"):
                    if value.endswith(("R", "r", "o")):
                        value = value[:-1]
                    value += "Ω"
                footprint = simplify_footprint_name(part.footprint.rsplit(":", 1)[-1])
                selection[part.reference] = (
                    f"{value} {footprint}" if footprint else value
                )
            label = f"Assign {', '.join(selection)} — Variant {self.variant_label(targets[0].variant_name)}"
            selector = self.dialog._part_selector
            if selector is None:
                selector = PartSelectorDialog(
                    self.dialog,
                    selection,
                    assignment_context=context,
                    assignment_label=label,
                )
                self.dialog._part_selector = selector
                selector.Show()
            else:
                selector.update_for(
                    selection, assignment_context=context, assignment_label=label
                )
            selector.Raise()
        except Exception as error:
            self._error(error)

    def assign_parts(self, event: Any) -> None:
        """Use the event's captured assignment session, never the current grid row."""
        if self.closed:
            return
        try:
            context = self.session.accept_assignment(
                getattr(event, "assignment_context", None)
            )
            self._apply(
                tuple(
                    VariantEdit(target, (("lcsc", event.lcsc),))
                    for target in context.targets
                )
            )
        except Exception as error:
            self._error(error)

    def _on_edit(self, target: Any, value: Any) -> None:
        if self.closed:
            return
        try:
            native = self.session.snapshot.target(target.component_id, target.variant)
            self._apply((VariantEdit(native, ((target.field, value),)),))
        except Exception as error:
            self._error(error)

    def toggle(self, fields: Sequence[str]) -> None:
        """Toggle selected inclusion settings using their positive UI polarity."""
        try:
            edits = []
            for target in self._targets():
                part = self.session.snapshot.get(
                    target.component_id, target.variant_name
                )
                edits.append(
                    VariantEdit(
                        target,
                        tuple((field, not getattr(part, field)) for field in fields),
                    )
                )
            self._apply(edits)
        except Exception as error:
            self._error(error)

    def remove(self) -> None:
        """Explicitly clear selected LCSC overrides, preserving all other settings."""
        try:
            self._apply(
                tuple(
                    VariantEdit(target, (("lcsc", ""),)) for target in self._targets()
                )
            )
        except Exception as error:
            self._error(error)

    def start_enrichment(self) -> None:
        """Request missing supplier facts once per LCSC identifier."""
        if self.closed:
            return
        new = self.cache.get_missing_metadata(self.session.snapshot) - self._attempted
        if not new:
            return
        self._pending.update(new)
        self._attempted.update(new)
        self._queue_render()

        def worker() -> None:
            provider = LCSCAssemblyMetadataProvider(min_interval_seconds=1.0)
            try:
                for lcsc, metadata in provider.fetch_iter(list(new)):
                    wx.CallAfter(self._enriched, lcsc, metadata or {})
            except Exception as error:
                wx.CallAfter(self._enrichment_failed, tuple(new), str(error))
            finally:
                wx.CallAfter(self._enrichment_finished, tuple(new))

        Thread(target=worker, daemon=True).start()

    def _enriched(self, lcsc: str, metadata: dict[str, Any]) -> None:
        if self.closed:
            return
        self._pending.discard(lcsc)
        if not self.session.reliable:
            return
        try:
            self.session._check_board()
            self._metadata_errors.discard(lcsc)
            self.cache.set_assembly_metadata(
                lcsc,
                metadata.get("assembly_process", ""),
                metadata.get("component_product_type"),
            )
            self._queue_render()
        except Exception as error:
            self._error(error)

    def _queue_render(self) -> None:
        """Combine queued supplier publications without interrupting table gestures."""
        self._presentation = None
        if (
            self.closed
            or not self.session.reliable
            or self.session.generating
            or self._render_queued
        ):
            return
        self._render_queued = True
        wx.CallAfter(self._render_pending)

    def _render_pending(self) -> None:
        self._render_queued = False
        if self.session.reliable:
            self.recompute()

    def _enrichment_finished(self, lcscs: tuple[str, ...]) -> None:
        if self.closed:
            return
        if self._pending.intersection(lcscs):
            self._pending.difference_update(lcscs)
            self._queue_render()

    def _enrichment_failed(self, lcscs: tuple[str, ...], message: str) -> None:
        if self.closed:
            return
        self._metadata_errors.update(self._pending.intersection(lcscs))
        self.dialog.logger.warning(
            "Variant metadata unavailable; use Refresh to retry: %s", message
        )

    def begin_generation(self, corrections: Any) -> None:
        """Freeze the explicit output source before preparing any fabrication data."""
        snapshot, variant = self.session.begin_generation()
        self._publish_output()
        self.dialog.fabrication.begin_generation(
            snapshot, variant, corrections, self.session.validate_generation
        )

    def end_generation(self) -> None:
        """Clean private artifacts and release the matrix after every exit path."""
        try:
            self.dialog.fabrication.abort_generation()
        finally:
            self.session.end_generation()
            self.refresh()

    def close(self) -> None:
        """Invalidate asynchronous callbacks before the wx controls are destroyed."""
        if self.closed:
            return
        self.closed = True
        self.session.reliable = False
        self.timer.Stop()
        self.view.set_mutations_enabled(False)
        state = self.view.capture_preferences()
        state["differences_only"] = self.differences.GetValue()
        state["show_footprint_library"] = self.show_footprint_library.GetValue()
        state["require_bom"] = self.dialog.hide_bom_parts
        state["require_pos"] = self.dialog.hide_pos_parts
        try:
            self.cache.set_display_preferences(state)
        except Exception as error:
            self.dialog.logger.warning(
                "Could not save variant display preferences: %s", error
            )

    def dispatch_action(self, action: str, target: Any = None) -> None:
        """Dispatch explicit matrix actions through captured clipboard/edit targets."""
        if self.closed:
            return
        # Clipboard and preference actions are implemented below in this controller.
        handler = getattr(self, f"action_{action}", None)
        if handler is not None:
            try:
                handler(target)
            except Exception as error:
                self._error(error)

    def export_to_schematic(self, paths: Sequence[str]) -> None:
        """Report failed or unavailable Default exports at the event boundary."""
        from ..schematicexport import SchematicExport

        try:
            self.session.require_editable()
            self.session.refresh()
            self.render()
            SchematicExport(self.dialog).load_schematic(
                paths,
                variant_name=self.session.output_variant,
                parts=self.cache.assembly_rows(self.session.snapshot, ""),
            )
        except Exception as error:
            self._error(error)

    def action_correction(self, target: Any = None) -> None:
        """Edit the existing shared correction rule in its current database scope."""
        if self.closed or target is None:
            return
        if target.variant is not None or target.field != "correction":
            return
        if self.session.generating:
            raise VariantSessionError("Finish generation before editing corrections.")
        self.session._check_board()
        snapshot = self.session.adapter.snapshot()
        part = snapshot.get(target.component_id, "")
        pattern = "^" + re.escape(part.footprint.rsplit(":", 1)[-1]) + "$"
        try:
            with CorrectionManagerDialog(self.dialog, pattern) as manager:
                # Manager construction may recover or migrate its database. Use
                # that fresh snapshot to select a stored row, never an old index.
                stored = manager.correction_snapshot
                if stored.corrections is not None:
                    match = resolve_shared_corrections(snapshot, stored.corrections)[
                        part.component_id
                    ]
                    if match is not None:
                        record = next(
                            (
                                row
                                for row in stored.rows
                                if row.correction == match.correction
                            ),
                            None,
                        )
                        if record is not None:
                            manager.populate_corrections_list(
                                selected_rowid=record.rowid
                            )
                manager.ShowModal()
        finally:
            self.refresh()

    def _cell(self, target: Any) -> tuple[int, int]:
        if target is None:
            raise VariantSessionError("Select a cell first.")
        coordinates = coordinates_for(self.model, target)
        if coordinates is None:
            raise VariantSessionError(
                "The selected component or field is no longer visible. Select it again."
            )
        return coordinates

    def _write_clipboard(self, payload: Any) -> None:
        if not wx.TheClipboard.Open():
            raise VariantSessionError("The clipboard is busy. Try Copy again.")
        try:
            nonce = uuid4().hex
            data = wx.DataObjectComposite()
            data.Add(wx.TextDataObject(payload.plain_text), True)
            marker = wx.CustomDataObject("application/x-jlcpcb-variant-cell")
            marker.SetData(nonce.encode("ascii"))
            data.Add(marker)
            if not wx.TheClipboard.SetData(data):
                raise VariantSessionError(
                    "Could not write the clipboard. Try Copy again."
                )
            self._clipboard = payload
            self._clipboard_nonce = nonce
        finally:
            wx.TheClipboard.Close()

    def action_copy_cell(self, target: Optional[MatrixTarget]) -> None:
        """Copy one displayed cell, retaining its field identity for later paste."""
        self._write_clipboard(self.model.copy_cell(*self._cell(target)))

    def action_copy(self, target: Optional[MatrixTarget]) -> None:
        """Copy selected variant settings or selected values from a shared column."""
        row, column = self._cell(target)
        if target is not None and target.variant is not None:
            targets = self._targets()
            payload = self.model.copy_block(
                tuple(item.component_id for item in targets), targets[0].variant_name
            )
        else:
            selected = set(self.view.selected_physical_component_ids())
            if not selected:
                raise VariantSessionError("Select components to copy.")
            payload = replace(
                self.model.copy_cell(row, column),
                rows=tuple(
                    self.model.copy_cell(index, column).rows[0]
                    for index, component in enumerate(self.model.rows)
                    if component.component_id in selected
                ),
            )
        self._write_clipboard(payload)

    def action_paste(self, target: Optional[MatrixTarget]) -> None:
        """Validate the entire typed operation before any native mutation."""
        self.session.require_editable()
        if target is None or target.variant is None:
            raise VariantSessionError("Select components within one variant to paste.")
        targets = self._targets()
        if not wx.TheClipboard.Open():
            raise VariantSessionError("The clipboard is busy. Try Paste again.")
        try:
            text = wx.TextDataObject()
            if not wx.TheClipboard.GetData(text):
                raise VariantSessionError(
                    "The clipboard contains no text value to paste."
                )
            marker = wx.CustomDataObject("application/x-jlcpcb-variant-cell")
            internal = (
                bool(wx.TheClipboard.GetData(marker))
                and bytes(marker.GetData()).decode("ascii", errors="replace")
                == self._clipboard_nonce
            )
            contents = text.GetText()
        finally:
            wx.TheClipboard.Close()
        payload = self._clipboard if internal else None
        if (payload is None or payload.cell_field is not None) and (
            target.field not in EDITABLE_FIELDS
        ):
            raise VariantSessionError(
                "Select an editable Value, LCSC, BOM, POS or POP cell to paste a value."
            )
        if payload is not None:
            field = target.field if payload.cell_field is not None else None
            operation = self.model.plan_paste(
                payload,
                tuple(item.component_id for item in targets),
                target.variant,
                field,
            )
        else:
            if len(targets) != 1:
                raise VariantSessionError(
                    "External text pastes into one cell. Use Copy on selected variant components to copy their settings."
                )
            operation = self.model.plan_text_paste(
                contents, targets[0].component_id, target.variant, target.field
            )
        self._apply(operation)
        self.dialog.logger.info(
            "Pasted %s into %s / %s",
            ", ".join(field for field, _ in operation[0].changes),
            ", ".join(
                dict.fromkeys(
                    self.session.snapshot.get(
                        edit.target.component_id, edit.target.variant_name
                    ).reference
                    for edit in operation
                )
            ),
            self.variant_label(target.variant),
        )

    def action_copy_to(self, target: Any) -> None:
        """Choose a bounded field/ref/destination product and copy effective values."""
        self.session.require_editable()
        targets = self._targets()
        source = targets[0].variant_name
        components = tuple(item.component_id for item in targets)
        fields = ("value", "lcsc", "bom", "pos", "pop")
        # Capture all effective source values before either dialog can change focus.
        source_model = self.model
        references = ", ".join(
            source_model.snapshot.get(component, source).reference
            for component in components
        )
        with wx.MultiChoiceDialog(
            self.dialog,
            f"Source: {self.variant_label(source)}\nComponents: {references}\nChoose fields to copy.",
            "Copy to variants — fields",
            ["Value", "LCSC", "BOM inclusion", "POS inclusion", "POP (populated)"],
        ) as choose_fields:
            choose_fields.SetSelections(
                [fields.index(target.field)]
                if target.field in fields
                else list(range(len(fields)))
            )
            if choose_fields.ShowModal() != wx.ID_OK:
                return
            chosen_fields = tuple(
                fields[index] for index in choose_fields.GetSelections()
            )
        if not chosen_fields:
            raise VariantSessionError("Select at least one field to copy.")
        payload = source_model.copy_block(components, source, fields=chosen_fields)
        destinations = tuple(name for name in source_model.variants if name != source)
        with wx.MultiChoiceDialog(
            self.dialog,
            f"Copy {', '.join(chosen_fields)} from {self.variant_label(source)}\nComponents: {references}\nSelect exact destination variants.",
            "Copy to variants — destinations",
            [self.variant_label(name) for name in destinations],
        ) as choose_destinations:
            if choose_destinations.ShowModal() != wx.ID_OK:
                return
            chosen = tuple(
                destinations[index] for index in choose_destinations.GetSelections()
            )
        operation = source_model.plan_copy_to_variants(payload, chosen)
        self._apply(operation)

    def action_use_base(self, target: Any) -> None:
        """Remove only the native LCSC override, retaining unrelated fields/flags."""
        targets = self._targets()
        if targets[0].variant_name == "":
            raise VariantSessionError("Default already contains the base assignment.")
        self._apply(tuple(VariantEdit(item, use_base=("lcsc",)) for item in targets))

    def action_remove(self, target: Any) -> None:
        """Clear assignments through the same explicit native edit coordinator."""
        self.remove()

    def action_details(self, target: Any) -> None:
        """Expose full cell values and provenance without depending on color."""
        wx.MessageBox(
            self.model.cell_details(*self._cell(target)),
            target.label,
            wx.OK | wx.ICON_INFORMATION,
            self.dialog,
        )

    def part_details(self) -> None:
        """Inspect the part at the explicit variant target."""
        try:
            target = self._targets()[0]
            part = self.session.snapshot.get(target.component_id, target.variant_name)
            self.dialog.show_part_details_dialog(part.lcsc)
        except Exception as error:
            self._error(error)

    def save_preferences(self) -> None:
        """Save only deliberately selected effective assignment preferences."""
        try:
            parts = [
                self.session.snapshot.get(item.component_id, item.variant_name)
                for item in self._targets()
            ]
            self.dialog._save_part_preferences(
                (part.footprint.rsplit(":", 1)[-1], part.value, part.lcsc)
                for part in parts
            )
        except Exception as error:
            self._error(error)

    def apply_preferences(self) -> None:
        """Apply manual preferences within the selected native variant only."""
        try:
            edits = []
            for target in self._targets():
                part = self.session.snapshot.get(
                    target.component_id, target.variant_name
                )
                preferred = self.dialog.library.get_part_preference(
                    part.footprint.rsplit(":", 1)[-1], part.value
                )
                if preferred:
                    edits.append(VariantEdit(target, (("lcsc", preferred),)))
            if edits:
                self._apply(edits)
        except Exception as error:
            self._error(error)

    def action_save_preferences(self, target: Any) -> None:
        """Expose the explicit preference action in the matrix menu."""
        self.save_preferences()

    def action_apply_preferences(self, target: Any) -> None:
        """Expose the explicit preference action in the matrix menu."""
        self.apply_preferences()

    def select_alike(self) -> None:
        """Select matching visible rows within one explicitly chosen variant."""
        try:
            target = self._targets()[0]
            part = self.session.snapshot.get(target.component_id, target.variant_name)
            visible = {row.component_id for row in self.model.rows}
            components = tuple(
                item.component_id
                for item in self.session.snapshot.for_variant(target.variant_name)
                if item.component_id in visible
                and (item.value, item.footprint) == (part.value, part.footprint)
            )
            self.view.select_components(components, target.variant_name)
            self.dialog.right_toolbar.ToggleTool(8, False)
        except Exception as error:
            self._error(error)
