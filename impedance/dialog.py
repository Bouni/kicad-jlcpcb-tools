"""Transactional controlled-impedance specification and section review dialog."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from decimal import InvalidOperation
from typing import Optional
from uuid import uuid4
from weakref import ReferenceType, ref

import wx

from . import review_tracking as tracking
from .catalog_cache import CatalogCache
from .catalog_controller import CatalogController, CatalogState
from .matching import analyze, preview_sections, validate_review
from .model import (
    Analysis,
    BoardSnapshot,
    Config,
    LayerSettings,
    Section,
    Specification,
    Stackup,
    Trace,
    WidthResult,
    format_length,
    parse_length,
    positive_decimal,
    validate_config,
)
from .service import CapturedImage

KIND_CHOICES = (
    ("single_ended", "Single Ended (Non coplanar)"),
    ("differential", "Differential Pair (Non coplanar)"),
    ("single_ended_coplanar", "Single Ended (Coplanar)"),
    ("differential_coplanar", "Differential Pair (Coplanar)"),
)

REFERENCE_HELP = "Verify that the selected copper layers are ground reference planes."


def parse_distance(value: str, units: str) -> int:
    """Convert a positive mm or mil value into an exact integer nanometre value."""
    return parse_length(value, units)


def format_distance(value: int, units: str = "mm") -> str:
    """Format an internal dimension without rounding away its exact value."""
    return format_length(value, units)


def adjacent_reference_layers(
    layers: tuple[str, ...], signal_layer: str
) -> tuple[str, ...]:
    """Suggest the immediately neighboring copper layers in physical stack order."""
    if signal_layer not in layers:
        raise ValueError("Choose a signal layer from the enabled copper stack.")
    index = layers.index(signal_layer)
    return tuple(
        layers[neighbor]
        for neighbor in (index - 1, index + 1)
        if 0 <= neighbor < len(layers)
    )


class ReferenceLayerSelection:
    """Apply initial adjacent defaults while retaining explicitly chosen reference planes."""

    def __init__(
        self,
        layers: tuple[str, ...],
        signal_layer: str,
        selected: Optional[tuple[str, ...]] = None,
    ) -> None:
        defaults = adjacent_reference_layers(layers, signal_layer)
        if selected is not None and (
            any(layer not in layers for layer in selected) or signal_layer in selected
        ):
            raise ValueError(
                "Saved reference layers must be enabled copper layers distinct from the signal layer."
            )
        self.layers = layers
        self.signal_layer = signal_layer
        self.automatic = selected is None
        self.selected = (
            defaults
            if selected is None
            else tuple(layer for layer in layers if layer in selected)
        )

    def set_manual(self, selected: tuple[str, ...]) -> tuple[str, ...]:
        """Record explicit choices and report removal of the signal layer if checked."""
        if any(layer not in self.layers for layer in selected):
            raise ValueError(
                "Reference layers must belong to the enabled copper stack."
            )
        removed = (self.signal_layer,) if self.signal_layer in selected else ()
        self.selected = tuple(
            layer
            for layer in self.layers
            if layer in selected and layer != self.signal_layer
        )
        self.automatic = False
        return removed

    def use_adjacent(self) -> None:
        """Explicitly restore defaults and let them follow subsequent signal-layer changes."""
        self.selected = adjacent_reference_layers(self.layers, self.signal_layer)
        self.automatic = True


def dimension_fields_for_kind(kind: str) -> tuple[bool, bool]:
    """Identify the pair-spacing and coplanar-ground-gap fields used by an impedance type."""
    if kind not in dict(KIND_CHOICES):
        raise ValueError("Choose a supported impedance type.")
    return kind.startswith("differential"), kind.endswith("_coplanar")


def _begin_main_catalog(owner: ReferenceType[ImpedanceDialog], epoch: int) -> None:
    """Start after main-dialog first show, unless a picker already joined the check."""
    dialog = owner()
    if dialog is None or dialog._closing or epoch != dialog._catalog_epoch:
        return
    try:
        if not dialog:
            return
        if not dialog.IsShownOnScreen():
            dialog._catalog_check_scheduled = False
            return
        dialog._ensure_catalog_started()
    except RuntimeError:
        # Destruction can occur before a queued first-show callback is delivered.
        return
    except Exception as error:
        dialog._show_catalog_error(str(error))


class _RowsRefreshed(ValueError):
    """Stop an action whose original PCB rows were replaced during verification."""


class ReviewSession:
    """Own one private document: settings, detected rows, selection and approval."""

    def __init__(self, config: Config, snapshot: BoardSnapshot) -> None:
        self.config, self.snapshot = config, snapshot
        self.analysis: Optional[Analysis] = None
        self.included: set[str] = set()
        self.config = self.save_result(snapshot)

    @property
    def approved(self) -> bool:
        """Derive readiness from the current revision, never a parallel flag."""
        return bool(
            self.analysis
            and self.config.reviewed_digest == self.analysis.digest
            and self.included
            and self.included == set(self.config.included_section_ids)
        )

    def invalidate_review(self) -> None:
        """Retain historical events and editable selection, revoke export approval."""
        self.config = replace(self.config, reviewed_digest="", included_section_ids=())

    def replace_specifications(self, specifications: tuple[Specification, ...]) -> None:
        """Replace specifications, retain relevant history, and invalidate review."""
        if specifications == self.config.specifications:
            return
        retained = {spec.spec_id for spec in specifications}
        self.config = replace(
            self.config,
            specifications=specifications,
            review_tracking=tracking.retain_specs(
                self.config.review_tracking, retained
            ),
            width_results=tuple(
                result
                for result in self.config.width_results
                if result.spec_id in retained
            ),
        )
        self.invalidate_review()
        self.analysis = None
        self.included.clear()

    def replace_calculation_context(
        self, stackup: Optional[Stackup], width_results: tuple[WidthResult, ...]
    ) -> None:
        """Replace the calculation inputs and invalidate the current row review."""
        self.config = replace(self.config, stackup=stackup, width_results=width_results)
        self.invalidate_review()
        self.analysis = None
        self.included.clear()

    def refresh(self, snapshot: Optional[BoardSnapshot] = None) -> Analysis:
        """Populate rows automatically, preserving unchanged selections/approval."""
        previous = self.analysis
        self.snapshot = snapshot if snapshot is not None else self.snapshot
        try:
            result = analyze(self.config, self.snapshot)
        except Exception:
            self.analysis = None
            self.included.clear()
            self.invalidate_review()
            raise
        available = {section.section_id for section in result.sections}
        if previous is not None and previous.digest == result.digest:
            self.included &= available
        elif self.config.reviewed_digest == result.digest:
            self.included = set(self.config.included_section_ids) & available
        else:
            self.included = available
        self.analysis = result
        if self.config.reviewed_digest:
            try:
                validate_review(replace(self.config, enabled=True), result)
            except ValueError:
                self.invalidate_review()
        return result

    def set_included(self, section_ids: Iterable[str]) -> None:
        """Validate the selected rows and invalidate review when selection changes."""
        if self.analysis is None:
            raise ValueError("Workbook rows are not available.")
        included = set(section_ids)
        if not included <= {section.section_id for section in self.analysis.sections}:
            raise ValueError("A selected section is no longer present.")
        if included != self.included:
            self.invalidate_review()
        self.included = included

    def approve(self) -> None:
        """Approve exactly the checked rows of the current document."""
        if self.analysis is None or not self.included:
            raise ValueError("Include at least one current PCB section.")
        rows = {
            (section.spec_id, section.layer, section.section_id)
            for section in self.analysis.sections
        }
        candidate = replace(
            self.config,
            enabled=True,
            reviewed_digest=self.analysis.digest,
            included_section_ids=tuple(
                section.section_id
                for section in self.analysis.sections
                if section.section_id in self.included
            ),
            review_tracking=replace(
                self.config.review_tracking,
                images=tuple(
                    record
                    for record in self.config.review_tracking.images
                    if (record.spec_id, record.layer, record.section_id) in rows
                ),
            ),
        )
        validate_review(candidate, self.analysis)
        self.config = candidate

    def save_result(self, snapshot: Optional[BoardSnapshot] = None) -> Config:
        """Save valid intent independently of whether report review is complete."""
        validate_config(self.config)
        if self.config.reviewed_digest:
            try:
                reviewed = replace(self.config, enabled=True)
                validate_review(
                    reviewed,
                    analyze(
                        reviewed, snapshot if snapshot is not None else self.snapshot
                    ),
                )
                return self.config
            except ValueError:
                pass
        return replace(self.config, reviewed_digest="", included_section_ids=())


@dataclass
class _LayerDraft:
    """Keep unsaved per-layer text, including invalid input, across navigation."""

    references: tuple[str, ...]
    automatic: bool = True
    spacing: str = ""
    ground_gap: str = ""
    units: str = "mm"
    included: bool = True
    confirmed: bool = False


class SpecificationDialog(wx.Dialog):
    """Review all routed copper belonging to one explicitly selected net class."""

    def __init__(
        self,
        parent: wx.Window,
        snapshot: BoardSnapshot,
        specification: Optional[Specification] = None,
        preview: Optional[Callable[..., CapturedImage]] = None,
        verify_snapshot: Optional[Callable[[BoardSnapshot], None]] = None,
        appearance_context: Optional[Callable[[], object]] = None,
        review_tracking: tracking.ReviewTracking = tracking.ReviewTracking(),
        stackup: Optional[Stackup] = None,
        width_results: tuple[WidthResult, ...] = (),
    ) -> None:
        if specification is not None:
            try:
                validate_config(
                    Config(specifications=(specification,)), snapshot.layers
                )
            except ValueError as error:
                raise ValueError(
                    f"Saved specification cannot be edited with the enabled board stack: {error} "
                    "Correct the board stack, or explicitly remove and replace this specification; its saved data has not been changed."
                ) from error
        super().__init__(
            parent,
            title="Impedance specification",
            size=(1040, 700),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.snapshot = snapshot
        self.verify_snapshot = verify_snapshot
        self.original = specification
        self.stackup = stackup
        self.width_results = width_results
        self.specification: Optional[Specification] = None
        self._spec_id = (
            specification.spec_id if specification is not None else str(uuid4())
        )
        self.review_tracking = tracking.for_spec(review_tracking, self._spec_id)
        self._source_digest = tracking.source_fingerprint(snapshot)
        self._width_units = "mm"
        self._exact_dimensions: dict[tuple[str, str], int] = {}
        self.layers = tuple(snapshot.layers)
        self._active_layer = ""
        self._active_class = ""
        self._layer_names: list[str] = []
        self._layer_drafts: dict[str, _LayerDraft] = {}
        self._setting_fields = False
        self._target_automatic = specification is None
        self._setting_target = False
        self._validation_control: Optional[wx.Window] = None
        self._selection_error = ""
        self._selection_error_field = ""
        self._field_labels: dict[str, wx.StaticText] = {}
        self._field_label_defaults: dict[str, tuple[str, wx.Colour]] = {}
        self._field_indicator_states: dict[str, str] = {}
        root = wx.BoxSizer(wx.VERTICAL)
        content = wx.BoxSizer(wx.HORIZONTAL)
        left = wx.BoxSizer(wx.VERTICAL)
        self.scroll_hint = wx.StaticText(self, label="Settings — scroll for more ↓")
        left.Add(self.scroll_hint, 0, wx.BOTTOM, 6)
        self.form = wx.ScrolledWindow(self, style=wx.VSCROLL | wx.BORDER_SIMPLE)
        self.form.SetScrollRate(0, 12)
        fields = wx.FlexGridSizer(cols=2, vgap=8, hgap=12)
        fields.AddGrowableCol(1, 1)
        self._net_class_names = list(snapshot.net_classes)
        self.net_class = wx.Choice(
            self.form, choices=["Choose a net class…"] + self._net_class_names
        )
        self.net_class.SetSelection(0)
        self.class_hint = wx.StaticText(self.form, label="")
        self.class_hint.Wrap(250)
        self.label = wx.TextCtrl(self.form)
        self.target = wx.TextCtrl(self.form)
        self.target.SetValue("50")
        self.kind = wx.Choice(self.form, choices=[label for _, label in KIND_CHOICES])
        self.kind.SetSelection(0)
        self.layer = wx.Choice(self.form, choices=list(self.layers))
        if self.layers:
            self.layer.SetSelection(0)
        self.units = wx.Choice(self.form, choices=["mm", "mil"])
        self.units.SetSelection(0)
        self.include_layer = wx.CheckBox(
            self.form, label="Include this layer in the workbook"
        )
        self.include_layer.SetValue(True)
        self.layer_hint = wx.StaticText(self.form, label="")
        self.layer_hint.Wrap(250)
        self.width_hint = wx.StaticText(
            self.form, label="Select a net class and signal layer."
        )
        self.width_hint.Wrap(250)
        self.approval_hint = wx.StaticText(self.form, label="Not recorded")
        self.approval_hint.Wrap(250)
        self.references = wx.CheckListBox(self.form, choices=list(self.layers))
        self.references.SetMinSize((-1, 104))
        self.adjacent_button = wx.Button(self.form, label="Use adjacent layers")
        self.reference_hint = wx.StaticText(self.form, label=REFERENCE_HELP)
        self.reference_hint.Wrap(250)
        reference_fields = wx.BoxSizer(wx.VERTICAL)
        reference_fields.Add(self.references, 0, wx.EXPAND)
        reference_fields.Add(self.adjacent_button, 0, wx.TOP, 6)
        reference_fields.Add(self.reference_hint, 0, wx.TOP | wx.EXPAND, 6)
        self.spacing = wx.TextCtrl(self.form)
        self.ground_gap = wx.TextCtrl(self.form)
        self.spacing_label = wx.StaticText(self.form, label="Differential pair spacing")
        self.ground_gap_label = wx.StaticText(self.form, label="Coplanar ground gap")
        for name, label, control in (
            ("net_class", "Net class", self.net_class),
            ("class_hint", "Membership", self.class_hint),
            ("label", "Label", self.label),
            ("target", "Desired impedance (Ω)", self.target),
            ("kind", "Impedance type", self.kind),
            ("layer", "Signal copper layer (A)", self.layer),
            ("layer_hint", "Routed copper", self.layer_hint),
            ("approval_hint", "Layer approval", self.approval_hint),
            ("include_layer", "Layer selection", self.include_layer),
            ("references", "Reference copper layers (B)", reference_fields),
            ("units", "Dimension units", self.units),
            ("width_hint", "Width comparison", self.width_hint),
            ("spacing", self.spacing_label, self.spacing),
            ("ground_gap", self.ground_gap_label, self.ground_gap),
        ):
            field_label = (
                wx.StaticText(self.form, label=label)
                if isinstance(label, str)
                else label
            )
            self._field_labels[name] = field_label
            self._field_label_defaults[name] = (
                field_label.GetLabel(),
                field_label.GetForegroundColour(),
            )
            fields.Add(field_label, 0, wx.ALIGN_CENTER_VERTICAL)
            fields.Add(control, 1, wx.EXPAND)
        self.spacing.SetToolTip(
            "Edge-to-edge spacing between the differential pair's traces."
        )
        self.ground_gap.SetToolTip(
            "Edge-to-edge gap from the signal traces to coplanar ground copper."
        )
        self.references.SetToolTip(
            "Select reference copper layers for workbook column B. The signal layer is recorded separately in column A."
        )
        form_layout = wx.BoxSizer(wx.VERTICAL)
        form_layout.Add(fields, 0, wx.ALL | wx.EXPAND, 10)
        self.form.SetSizer(form_layout)
        left.Add(self.form, 1, wx.EXPAND)
        self.footer = wx.Panel(self)
        self.footer.SetBackgroundColour(
            wx.SystemSettings.GetColour(wx.SYS_COLOUR_BTNFACE)
        )
        footer_layout = wx.BoxSizer(wx.VERTICAL)
        footer_layout.Add(wx.StaticLine(self.footer), 0, wx.BOTTOM | wx.EXPAND, 8)
        self.active_layer_status = wx.StaticText(
            self.footer, label="No signal layer selected"
        )
        footer_layout.Add(
            self.active_layer_status, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 8
        )
        self.validation_message = wx.StaticText(self.footer, label="")
        self.validation_message.Wrap(470)
        footer_layout.Add(self.validation_message, 0, wx.ALL | wx.EXPAND, 8)
        self.approve_layer = wx.Button(self.footer, label="Approve layer")
        footer_layout.Add(self.approve_layer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.footer.SetSizer(footer_layout)
        left.Add(self.footer, 0, wx.TOP | wx.EXPAND, 8)
        content.Add(left, 1, wx.RIGHT | wx.EXPAND, 12)
        from .dialog_preview import WorkbookPreview

        self.preview_pane = WorkbookPreview(
            self,
            preview,
            verify_current=self._verify_current_snapshot,
            appearance_context=appearance_context,
            appearance_changed=self._invalidate_layer_approvals,
            review_failed=self._invalidate_current_layer_approval,
            image_viewed=self._on_image_viewed,
        )
        content.Add(self.preview_pane.window, 1, wx.EXPAND)
        root.Add(content, 1, wx.ALL | wx.EXPAND, 12)
        root.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL | wx.EXPAND, 12)
        self.SetSizer(root)
        self.SetMinSize((980, 620))
        self.net_class.Bind(wx.EVT_CHOICE, self._on_class)
        self.include_layer.Bind(wx.EVT_CHECKBOX, self._on_include_layer)
        self.approve_layer.Bind(wx.EVT_BUTTON, self._on_approve_layer)
        self.target.Bind(wx.EVT_TEXT, self._on_target)
        self.spacing.Bind(wx.EVT_TEXT, self._on_layer_field)
        self.ground_gap.Bind(wx.EVT_TEXT, self._on_layer_field)
        self.layer.Bind(wx.EVT_CHOICE, self._on_layer)
        self.references.Bind(wx.EVT_CHECKLISTBOX, self._on_references)
        self.adjacent_button.Bind(wx.EVT_BUTTON, self._on_adjacent)
        self.kind.Bind(wx.EVT_CHOICE, self._on_kind)
        self.units.Bind(wx.EVT_CHOICE, self._on_units)
        self.Bind(wx.EVT_BUTTON, self._on_save, id=wx.ID_OK)
        self.Bind(wx.EVT_SHOW, self._on_show)
        self.reference_selection = ReferenceLayerSelection(self.layers, self.layers[0])
        self._populate_references()
        if specification is not None:
            self._load(specification)
        else:
            self._update_controls()
        self._on_kind(None)
        self._update_field_indicators()
        self.Layout()
        self.CentreOnParent()

    def _on_show(self, event: wx.ShowEvent) -> None:
        """Resume a preview whose initial queued capture ran before native show."""
        if event.GetEventObject() is self and event.IsShown():
            wx.CallAfter(self.preview_pane.show_selected)
        event.Skip()

    def _load(self, specification: Specification) -> None:
        """Populate controls from a private edit source."""
        self.label.SetValue(specification.label)
        self._setting_target = True
        try:
            self.target.SetValue(specification.target_ohms)
        finally:
            self._setting_target = False
        for index, (kind, _) in enumerate(KIND_CHOICES):
            if specification.kind == kind:
                self.kind.SetSelection(index)
        self._target_automatic = False
        self._active_class = specification.net_class
        if specification.net_class not in self._net_class_names:
            self._net_class_names.append(specification.net_class)
        self.net_class.SetItems(["Choose a net class…"] + self._net_class_names)
        self.net_class.SetSelection(
            self._net_class_names.index(specification.net_class) + 1
        )
        self._load_layer_settings(specification)
        self._refresh_class_layers()

    def _load_layer_settings(self, specification: Specification) -> None:
        """Retain saved dimensions but never infer current approval from persistence."""
        self._layer_drafts = {
            settings.layer: _LayerDraft(
                references=settings.reference_layers,
                automatic=False,
                spacing=format_distance(settings.spacing_nm)
                if settings.spacing_nm is not None
                else "",
                ground_gap=format_distance(settings.ground_gap_nm)
                if settings.ground_gap_nm is not None
                else "",
            )
            for settings in specification.layer_settings
        }
        for layer in specification.excluded_layers:
            self._layer_drafts[layer] = _LayerDraft(
                adjacent_reference_layers(self.layers, layer),
                included=False,
                confirmed=True,
            )

    def _signal_layer(self) -> str:
        """Keep annotated dropdown labels separate from canonical copper-layer names."""
        return self._active_layer

    def _layer_status(self, layer: str) -> str:
        """Describe addressed exclusions distinctly from visual approval."""
        draft = self._layer_drafts[layer]
        if not draft.included and draft.confirmed:
            return "excluded"
        return "approved" if draft.confirmed else "needs review"

    def _update_layer_labels(self) -> None:
        """Refresh review-state labels without emitting choice events or changing identity."""
        self.layer.SetItems(
            [f"{layer} — {self._layer_status(layer)}" for layer in self._layer_names]
        )
        if self._active_layer in self._layer_names:
            self.layer.SetSelection(self._layer_names.index(self._active_layer))
        self.active_layer_status.SetLabel(
            f"{self._active_layer} — {self._layer_status(self._active_layer)}"
            if self._active_layer
            else "No signal layer selected"
        )
        self._update_approval_time()

    def _update_approval_time(self) -> None:
        """Show historical approval without using its timestamp as a review gate."""
        layer = self._signal_layer()
        record = tracking.find_layer(self.review_tracking, self._spec_id, layer)
        if record is None:
            message = "Not recorded"
        else:
            draft = self._layer_drafts.get(layer)
            current = draft is not None and draft.included and draft.confirmed
            label = "Approved at" if current else "Previous approval"
            message = f"{label}: " + tracking.format_timestamp(
                record.approved_at_utc, local=True
            )
        if self.approval_hint.GetLabel() == message:
            return
        self.approval_hint.SetLabel(message)
        self.approval_hint.Wrap(250)
        self.form.Layout()
        self.form.FitInside()

    def _on_image_viewed(
        self, section: Section, image_sha256: str, viewed_at_utc: str
    ) -> None:
        """Keep successful display events private until both dialogs are accepted."""
        digest = tracking.capture_fingerprint(
            self._source_digest,
            self._selection_specification(),
            section,
            image_sha256,
        )
        self.review_tracking = tracking.put_image(
            self.review_tracking,
            tracking.ImageView(
                self._spec_id, section.layer, section.section_id, digest, viewed_at_utc
            ),
        )

    def _record_layer_approval(
        self, spec: Specification, layer: str, *, already_confirmed: bool = False
    ) -> None:
        """Stamp an approval transition against all exact captures the user viewed."""
        captures = tuple(
            sorted(
                (
                    section.section_id,
                    tracking.capture_fingerprint(
                        self._source_digest, spec, section, image_sha256
                    ),
                )
                for section, image_sha256 in self.preview_pane.reviewed_capture_hashes()
            )
        )
        digest = tracking.layer_fingerprint(
            self._source_digest,
            spec,
            layer,
            self.preview_pane.sections,
            stackup=self.stackup,
            width_results=self.width_results,
        )
        previous = tracking.find_layer(self.review_tracking, self._spec_id, layer)
        if (
            already_confirmed
            and previous is not None
            and previous.settings_digest == digest
            and previous.captures == captures
        ):
            return
        self.review_tracking = tracking.put_layer(
            self.review_tracking,
            tracking.LayerApproval(
                self._spec_id, layer, digest, tracking.utc_now(), captures
            ),
        )

    def _capture_draft(self, confirmed: bool = False) -> _LayerDraft:
        """Keep raw invalid input safely, independently of the newly selected layer."""
        return _LayerDraft(
            self.reference_selection.selected,
            automatic=self.reference_selection.automatic,
            spacing=self.spacing.GetValue(),
            ground_gap=self.ground_gap.GetValue(),
            units=self.units.GetStringSelection(),
            included=bool(self.include_layer.GetValue()),
            confirmed=confirmed,
        )

    def _stash_class_layer(self) -> None:
        """Save the old layer by its cached identity, not the already changed choice."""
        if self._active_layer:
            previous = self._layer_drafts[self._active_layer]
            current = self._capture_draft()
            current.confirmed = previous.confirmed and replace(
                current, confirmed=False
            ) == replace(previous, confirmed=False)
            self._layer_drafts[self._active_layer] = current

    def _show_draft(self, layer: str, draft: _LayerDraft) -> None:
        """Display a layer's own unit and text without interpreting incomplete input."""
        self._setting_fields = True
        try:
            self.reference_selection = ReferenceLayerSelection(
                self.layers, layer, None if draft.automatic else draft.references
            )
            self.units.SetStringSelection(draft.units)
            self._width_units = draft.units
            self.spacing.SetValue(draft.spacing)
            self.ground_gap.SetValue(draft.ground_gap)
            self.include_layer.SetValue(draft.included)
            self._populate_references()
        finally:
            self._setting_fields = False

    def _on_class(self, event: wx.CommandEvent) -> None:
        """Start a fresh per-layer review without borrowing another class's settings."""
        self._clear_validation()
        index = self.net_class.GetSelection() - 1
        self._active_class = (
            self._net_class_names[index]
            if 0 <= index < len(self._net_class_names)
            else ""
        )
        self._active_layer = ""
        self._layer_names = []
        self._layer_drafts = {}
        self._refresh_class_layers()

    def _class_traces(self) -> tuple[Trace, ...]:
        """Use exact constituent membership and ignore unassigned copper."""
        names = {
            net
            for net, classes in self.snapshot.net_class_memberships
            if net and self._active_class in classes
        }
        return tuple(trace for trace in self.snapshot.traces if trace.net in names)

    def _refresh_class_layers(self) -> None:
        """Discover routed layers and surface newly encountered layers as unreviewed."""
        traces = self._class_traces()
        routed = {trace.layer for trace in traces}
        for layer in routed:
            if layer not in self._layer_drafts:
                self._layer_drafts[layer] = _LayerDraft(
                    adjacent_reference_layers(self.layers, layer)
                )
        choices = [layer for layer in self.layers if layer in self._layer_drafts]
        self._layer_names = choices
        if choices:
            layer = self._active_layer if self._active_layer in choices else choices[0]
            self._active_layer = layer
            self._update_layer_labels()
            self._show_draft(layer, self._layer_drafts[layer])
        else:
            self._active_layer = ""
            self._update_layer_labels()
        self._update_controls()

    def _update_controls(self) -> None:
        """Explain metadata availability and keep inactive controls unambiguous."""
        active = bool(self._active_layer)
        included = active and bool(self.include_layer.GetValue())
        self.net_class.Enable(not bool(self.snapshot.net_class_error))
        self.layer.Enable(active)
        self.include_layer.Enable(active)
        self.approve_layer.Enable(active)
        self.references.Enable(included)
        self.adjacent_button.Enable(included)
        if self.snapshot.net_class_error:
            hint = (
                self.snapshot.net_class_error
                + " Check net-class assignments in KiCad and reopen this dialog."
            )
            summary = "Net-class matching is unavailable."
        elif not self._active_class:
            hint = "Choose a net class explicitly, including Default if intended."
            summary = "No net class selected."
        elif self._active_class not in self.snapshot.net_classes:
            hint = f"Saved net class {self._active_class!r} is missing. Choose an existing class."
            summary = "The saved class cannot be matched."
        else:
            traces = self._class_traces()
            hint = f"{len({trace.net for trace in traces})} routed nets in {self._active_class}. All actual widths are matched."
            summaries = []
            for layer in self.layers:
                widths = sorted(
                    {trace.width_nm for trace in traces if trace.layer == layer}
                )
                if widths:
                    status = self._layer_status(layer)
                    summaries.append(
                        f"{layer}: {', '.join(format_distance(width) for width in widths)} mm ({status})"
                    )
            summary = (
                "\n".join(summaries) or "No routed named nets belong to this class."
            )
        self.class_hint.SetLabel(hint)
        self.class_hint.Wrap(250)
        self.layer_hint.SetLabel(summary)
        self.layer_hint.Wrap(250)
        self._update_layer_labels()
        self._on_kind(None)
        self._refresh_preview()
        self._update_width_comparison()
        self.form.Layout()
        self.form.FitInside()
        self.Layout()

    def _on_include_layer(self, event: wx.CommandEvent) -> None:
        """Exclusion changes remain a private draft until applied or saved."""
        self._clear_validation()
        self._stash_class_layer()
        self._update_controls()

    def _on_layer_field(self, event: Optional[wx.CommandEvent]) -> None:
        """Mark genuinely edited class-layer fields unreviewed without reacting to loading."""
        if not self._setting_fields and self._active_layer:
            self._clear_validation()
            self._stash_class_layer()
            self._update_controls()
        elif not self._setting_fields:
            self._clear_validation()
            self._update_field_indicators()

    def _validated_fields(
        self, layer: str, draft: _LayerDraft
    ) -> tuple[Optional[LayerSettings], dict[str, str]]:
        """One interpretation of raw inputs for indicators, comparisons and approval."""
        pair, ground = dimension_fields_for_kind(
            KIND_CHOICES[self.kind.GetSelection()][0]
        )
        included = bool(layer and draft.included)
        errors: dict[str, str] = {}
        dimensions: dict[str, int] = {}
        for name, value, required in (
            ("target", self.target.GetValue(), True),
            ("spacing", draft.spacing, included and pair),
            ("ground_gap", draft.ground_gap, included and ground),
        ):
            if not required:
                continue
            if not value.strip():
                errors[name] = "Required"
                continue
            try:
                if name == "target":
                    positive_decimal(value.strip(), "desired impedance")
                else:
                    dimensions[name] = self._read_dimension(value, draft.units)
            except (ValueError, InvalidOperation):
                errors[name] = "Invalid"
        if included and not draft.references:
            errors["references"] = "Required"
        if not self._active_class:
            errors["net_class"] = "Required"
        elif not layer:
            errors["layer"] = "Required"
        if self._selection_error_field:
            errors.setdefault(self._selection_error_field, "Invalid")
        settings = (
            None
            if errors or not included
            else LayerSettings(
                layer,
                draft.references,
                dimensions.get("spacing"),
                dimensions.get("ground_gap"),
            )
        )
        return settings, errors

    def _draft_settings(self, layer: str, draft: _LayerDraft) -> LayerSettings:
        """Report the first actionable form error without changing raw draft values."""
        settings, errors = self._validated_fields(layer, draft)
        if settings is None:
            field = next(iter(errors), "layer")
            self._validation_control = getattr(self, field)
            label = self._field_label_defaults[field][0]
            raise ValueError(
                f"{layer}: {label} — {errors.get(field, 'Required').lower()}."
            )
        return settings

    def _review_current_images(self) -> bool:
        """Present the next pending image without approving it in the same click."""
        from .dialog_preview import REVIEW_INSTRUCTION

        self._validation_control = self.preview_pane.rows
        if self.preview_pane.advance_to_unreviewed():
            self.validation_message.SetLabel(REVIEW_INSTRUCTION)
            self.validation_message.Wrap(470)
            self.Layout()
            return False
        self.preview_pane.ensure_reviewed()
        return True

    def _confirm_current_layer(self) -> bool:
        """Confirm one layer only when its images were viewed before this click."""
        self._ensure_selection_valid()
        if not self._active_layer:
            raise ValueError("Choose a net class with routed named nets.")
        self._verify_current_snapshot()
        self.preview_pane.ensure_appearance_current()
        self._validation_control = self.target
        positive_decimal(self.target.GetValue().strip(), "desired impedance")
        draft = self._capture_draft()
        if draft.included:
            settings = self._draft_settings(self._active_layer, draft)
            if not self._review_current_images():
                return False
            previous = self._layer_drafts.get(self._active_layer)
            unchanged = (
                previous is not None and replace(previous, confirmed=False) == draft
            )
            self._record_layer_approval(
                replace(self._selection_specification(), layer_settings=(settings,)),
                self._active_layer,
                already_confirmed=bool(previous and previous.confirmed and unchanged),
            )
        draft.confirmed = True
        self._layer_drafts[self._active_layer] = draft
        return True

    def _advance_to_pending_layer(self) -> bool:
        """Navigate to the next unaddressed layer without approving its first image."""
        routed = {trace.layer for trace in self._class_traces()}
        pending = {
            layer
            for layer in self.layers
            if layer in routed and not self._layer_drafts[layer].confirmed
        }
        if not pending:
            return False
        current = self.layers.index(self._active_layer)
        cyclic = self.layers[current + 1 :] + self.layers[: current + 1]
        next_layer = next(layer for layer in cyclic if layer in pending)
        self.layer.SetSelection(self._layer_names.index(next_layer))
        self._on_layer(None)
        self.validation_message.SetLabel(
            f"Review {next_layer}, then approve or exclude it."
        )
        self.validation_message.Wrap(470)
        self.Layout()
        return True

    def _on_approve_layer(self, event: wx.CommandEvent) -> None:
        """Walk pending images, then approve this layer and start the next layer."""
        try:
            self._clear_validation()
            if not self._confirm_current_layer():
                return
            self._update_controls()
            if not self._advance_to_pending_layer():
                self.validation_message.SetLabel(
                    "All signal layers are approved or excluded. Choose OK to save."
                )
                self.validation_message.Wrap(470)
                self.Layout()
        except (InvalidOperation, ValueError) as error:
            self._show_validation(str(error))

    def _on_target(self, event: wx.CommandEvent) -> None:
        """Only genuinely untouched defaults may follow a change of impedance kind."""
        if not self._setting_target:
            self._target_automatic = False
            self._layer_drafts = {
                layer: replace(draft, confirmed=False) if draft.included else draft
                for layer, draft in self._layer_drafts.items()
            }
            if self._active_layer:
                self._clear_validation()
                self._update_controls()
            else:
                self._clear_validation()
                self._update_field_indicators()

    def _update_width_comparison(self) -> None:
        """Compare routed widths with current nominal results without changing copper."""
        from .width_checks import format_width_delta_nm, format_width_nm, width_check

        widths = sorted(
            {
                trace.width_nm
                for trace in self._class_traces()
                if trace.layer == self._active_layer
            }
        )
        if not widths:
            self.width_hint.SetLabel("No routed widths on the selected signal layer.")
            return
        if not self.include_layer.GetValue():
            self.width_hint.SetLabel(
                "Actual: "
                + ", ".join(format_width_nm(width) for width in widths)
                + "\nExcluded from the workbook and nominal-width calculation."
            )
            self.width_hint.Wrap(250)
            return
        spec = self._selection_specification()
        settings, errors = self._validated_fields(
            self._active_layer, self._capture_draft()
        )
        if errors or settings is None:
            self.width_hint.SetLabel(
                "Actual: "
                + ", ".join(format_width_nm(width) for width in widths)
                + "\nComplete this layer's settings to compare calculated widths."
            )
            self.width_hint.Wrap(250)
            return
        spec = replace(spec, layer_settings=(settings,))
        config = Config(
            specifications=(spec,),
            stackup=self.stackup,
            width_results=self.width_results,
        )
        lines = []
        details: list[str] = []
        for width in widths:
            check = width_check(config, spec, self._active_layer, width)
            if (
                check.result_current
                and check.target_width_nm is not None
                and check.delta_nm is not None
            ):
                lines.append(
                    f"Actual {format_width_nm(width)}; nominal {format_width_nm(check.target_width_nm)}; "
                    f"difference {format_width_delta_nm(check.delta_nm)}."
                )
            else:
                lines.append(f"Actual {format_width_nm(width)}. {check.message}")
            if check.model and not details:
                details.append("Calculation model: " + check.model)
                details.extend(check.assumptions)
        self.width_hint.SetLabel("\n".join(lines + details))
        self.width_hint.Wrap(250)

    def _read_dimension(self, value: str, units: str) -> int:
        """Retain exact board dimensions behind recurring mm-to-mil decimals."""
        key = (value.strip(), units)
        if key in self._exact_dimensions:
            return self._exact_dimensions[key]
        return parse_distance(value, units)

    def _on_layer(self, event: Optional[wx.CommandEvent]) -> None:
        """Update automatic references without replacing a manual reference choice."""
        self._clear_validation()
        index = self.layer.GetSelection()
        if not 0 <= index < len(self._layer_names):
            return
        self._stash_class_layer()
        self._active_layer = self._layer_names[index]
        self._show_draft(self._active_layer, self._layer_drafts[self._active_layer])
        self._update_controls()

    def _populate_references(self, removed: tuple[str, ...] = ()) -> None:
        """Show reference choices and any removal required by a signal-layer change."""
        for index, layer in enumerate(self.layers):
            self.references.Check(index, layer in self.reference_selection.selected)
        if removed:
            message = (
                f"Removed {', '.join(removed)} from references because it is the signal layer. "
                "Choose another reference or use adjacent layers."
            )
        elif self.reference_selection.automatic:
            message = "Using adjacent layers. " + REFERENCE_HELP
        else:
            message = "Using your selected reference layers. " + REFERENCE_HELP
        self.reference_hint.SetLabel(message)
        self.reference_hint.Wrap(250)
        self.Layout()

    def _on_references(self, event: wx.CommandEvent) -> None:
        """Keep explicit checklist choices until the user requests new defaults."""
        selected = tuple(
            layer
            for index, layer in enumerate(self.layers)
            if self.references.IsChecked(index)
        )
        removed = self.reference_selection.set_manual(selected)
        self._populate_references(removed)
        self._on_layer_field(None)

    def _on_adjacent(self, event: wx.CommandEvent) -> None:
        """Restore adjacent layer defaults and allow them to follow signal changes."""
        self.reference_selection.use_adjacent()
        self._populate_references()
        self._on_layer_field(None)

    def _on_kind(self, event: Optional[wx.CommandEvent]) -> None:
        """Enable only pair-spacing and ground-gap fields required by the selected type."""
        if event is not None:
            self._clear_validation()
        pair, ground = dimension_fields_for_kind(
            KIND_CHOICES[self.kind.GetSelection()][0]
        )
        if event is not None:
            self._stash_class_layer()
            self._layer_drafts = {
                layer: replace(draft, confirmed=False) if draft.included else draft
                for layer, draft in self._layer_drafts.items()
            }
        included = bool(self._active_layer) and bool(self.include_layer.GetValue())
        self.spacing.Enable(pair and included)
        self.spacing_label.Enable(pair and included)
        self.ground_gap.Enable(ground and included)
        self.ground_gap_label.Enable(ground and included)
        if getattr(self, "_target_automatic", False):
            self._setting_target = True
            try:
                self.target.SetValue("90" if pair else "50")
            finally:
                self._setting_target = False
        if event is not None:
            self._update_controls()
        self._update_field_indicators()

    def _selection_specification(self) -> Specification:
        """Describe actual matching intent without inventing missing fabrication dimensions."""
        return Specification(
            self._spec_id,
            self.label.GetValue().strip(),
            self.target.GetValue().strip(),
            KIND_CHOICES[self.kind.GetSelection()][0],
            self._active_class,
        )

    def _refresh_preview(self) -> None:
        """Show exactly the workbook candidate geometry, independently of missing gap fields."""
        self._update_approval_time()
        self._selection_error = ""
        self._selection_error_field = ""
        problem = self._selection_problem()
        if problem is not None:
            self._set_selection_error(*problem)
            return
        layer = self._signal_layer()
        if not self.include_layer.GetValue():
            self.preview_pane.clear(
                "This layer is excluded from the workbook.", preserve_review=True
            )
            self._update_field_indicators()
            return
        spec: Optional[Specification] = None
        try:
            spec = self._selection_specification()
            sections = preview_sections(spec, self.snapshot, layer)
            if not sections:
                self._set_selection_error(
                    "No routed traces in this class use the selected layer.",
                    "layer",
                )
                return
            context = (
                self.snapshot,
                spec.net_class,
                spec.kind,
                layer,
            )
            self.preview_pane.set_sections(sections, context)
        except (ValueError, InvalidOperation) as error:
            message = str(error)
            if spec is not None:
                prefix = f"{spec.label or spec.spec_id}: "
                if message.startswith(prefix):
                    selection = f"Net class {spec.net_class!r}"
                    message = f"{selection}: {message[len(prefix) :]}"
            field = ""
            if "differential" in message.lower():
                field = "kind"
                message += (
                    " Assign complete differential pairs to a dedicated net class in KiCad, "
                    "or select Single Ended for unpaired signals. Reopen after changing KiCad assignments."
                )
            self._set_selection_error(message, field)
            return
        self._update_field_indicators()

    def _selection_problem(self) -> Optional[tuple[str, str]]:
        """Distinguish selection errors before layer discovery hides their cause."""
        if self.snapshot.net_class_error:
            return (
                self.snapshot.net_class_error
                + " Check net-class assignments in KiCad and reopen.",
                "net_class",
            )
        if not self._active_class:
            return "Choose a net class to preview its routed traces.", "net_class"
        if self._active_class not in self.snapshot.net_classes:
            return (
                f"Saved net class {self._active_class!r} is unavailable. Assign the intended nets to a class in KiCad and reopen.",
                "net_class",
            )
        if not self._class_traces():
            return (
                f"Net class {self._active_class!r} has no routed named nets. Check the class assignments in KiCad.",
                "net_class",
            )
        if not self._signal_layer():
            return "Choose a signal layer to preview its workbook captures.", "layer"
        return None

    def _set_selection_error(self, message: str, field: str) -> None:
        """Retain deterministic errors until matching inputs are repaired."""
        self._selection_error = message
        self._selection_error_field = field
        # Invalid inputs remove the displayed image, not evidence for an
        # unchanged layer. Freshness and
        # appearance failures independently invalidate all view history.
        self.preview_pane.clear(message, error=True, preserve_review=True)
        self._update_field_indicators()

    def _ensure_selection_valid(self) -> None:
        """Focus the actual selection blocker before requesting fabrication gaps."""
        self._refresh_preview()
        if self._selection_error:
            self._validation_control = (
                getattr(self, self._selection_error_field)
                if self._selection_error_field
                else self.preview_pane.rows
            )
            raise ValueError(self._selection_error)

    def _update_field_indicators(self) -> None:
        """Mark relevant blank/invalid values without modifying or validating drafts."""
        if not getattr(self, "_field_labels", None) or self._setting_fields:
            return
        _, states = self._validated_fields(self._active_layer, self._capture_draft())
        changed = False
        for name, label in self._field_labels.items():
            base, normal_colour = self._field_label_defaults[name]
            marker = states.get(name, "")
            text = f"{base} — {marker}" if marker else base
            colour = wx.Colour(205, 40, 40) if marker else normal_colour
            # Always restore the original label, never append to its wrapped text.
            if self._field_indicator_states.get(name) != marker:
                # Store before native setters/layout can deliver nested events.
                self._field_indicator_states[name] = marker
                label.SetLabel(text)
                label.Wrap(175)
                label.SetForegroundColour(colour)
                label.Refresh()
                changed = True
        if changed:
            self.form.Layout()
            self.form.FitInside()

    def _clear_validation(self) -> None:
        """Clear inline validation without changing draft text or approval state."""
        self.validation_message.SetLabel("")

    def _show_validation(self, message: str) -> None:
        """Display errors in place and focus the field that needs attention."""
        self.validation_message.SetLabel(message)
        self.validation_message.Wrap(470)
        if self._validation_control is not None:
            self._validation_control.SetFocus()
        self._update_field_indicators()
        self.Layout()

    def _invalidate_current_layer_approval(self) -> None:
        """Keep a failed capture's layer pending even if the user navigates away."""
        draft = self._layer_drafts.get(self._active_layer)
        if draft is not None and draft.included and draft.confirmed:
            self._layer_drafts[self._active_layer] = replace(draft, confirmed=False)
            self._update_layer_labels()

    def _invalidate_layer_approvals(self) -> None:
        """Require included layers to be reviewed again after visual context changes."""
        self._layer_drafts = {
            layer: replace(draft, confirmed=False) if draft.included else draft
            for layer, draft in self._layer_drafts.items()
        }
        self._update_layer_labels()

    def _verify_current_snapshot(self) -> None:
        """Reject current visual approval if the live board no longer matches this editor."""
        if self.verify_snapshot is None:
            return
        try:
            self.verify_snapshot(self.snapshot)
        except Exception as error:
            self._invalidate_layer_approvals()
            message = (
                str(error)
                or "The current board could not be verified. Reopen the specification and review again."
            )
            pane = getattr(self, "preview_pane", None)
            if pane is not None:
                pane.clear(message)
                self._validation_control = pane.rows
            raise ValueError(message) from error

    def _on_units(self, event: wx.CommandEvent) -> None:
        """Convert entered dimensions when the display unit changes."""
        units = self.units.GetStringSelection()
        changes = []
        pair, ground = dimension_fields_for_kind(
            KIND_CHOICES[self.kind.GetSelection()][0]
        )
        controls = (
            (self.spacing, pair),
            (self.ground_gap, ground),
        )
        try:
            for control, required in controls:
                value = control.GetValue().strip()
                if value:
                    try:
                        dimension = self._read_dimension(value, self._width_units)
                    except ValueError:
                        if required:
                            raise
                        continue
                    changes.append(
                        (control, dimension, format_distance(dimension, units))
                    )
        except ValueError:
            self.units.SetStringSelection(self._width_units)
            self._validation_control = control
            self._show_validation("Enter valid dimensions before changing units.")
            return
        self._setting_fields = True
        try:
            for control, dimension, text in changes:
                control.SetValue(text)
                self._exact_dimensions[text, units] = dimension
        finally:
            self._setting_fields = False
        self._width_units = units
        if self._active_layer:
            confirmed = self._layer_drafts[self._active_layer].confirmed
            self._layer_drafts[self._active_layer] = self._capture_draft(
                confirmed=confirmed
            )
            self._update_controls()
        else:
            self._clear_validation()
            self._refresh_preview()

    def _on_save(self, event: wx.CommandEvent) -> None:
        """Validate fields before returning a replacement specification."""
        try:
            self._clear_validation()
            self._verify_current_snapshot()
            self._ensure_selection_valid()
            self._validation_control = self.target
            target = positive_decimal(
                self.target.GetValue().strip(), "desired impedance"
            )
            self._save_class_specification(format(target, "f"))
        except (InvalidOperation, ValueError) as error:
            self.specification = None
            self._show_validation(str(error) or "Enter a valid impedance value.")

    def _save_class_specification(self, target: str) -> None:
        """Save one class intent only after every discovered layer is addressed."""
        self._validation_control = self.net_class
        if self.snapshot.net_class_error:
            raise ValueError(
                self.snapshot.net_class_error
                + " Check net-class assignments in KiCad and reopen."
            )
        if (
            not self._active_class
            or self._active_class not in self.snapshot.net_classes
        ):
            raise ValueError("Choose an existing net class explicitly.")
        self._stash_class_layer()
        self._refresh_class_layers()
        if not self._confirm_current_layer():
            return
        self._update_controls()
        routed = {trace.layer for trace in self._class_traces()}
        if not routed:
            raise ValueError("No routed named nets belong to this class.")
        if self._advance_to_pending_layer():
            return
        settings = tuple(
            self._draft_settings(layer, self._layer_drafts[layer])
            for layer in self.layers
            if layer in self._layer_drafts and self._layer_drafts[layer].included
        )
        if not any(setting.layer in routed for setting in settings):
            raise ValueError("Include at least one routed signal layer.")
        candidate = Specification(
            spec_id=self._spec_id,
            label=self.label.GetValue().strip(),
            target_ohms=target,
            kind=KIND_CHOICES[self.kind.GetSelection()][0],
            net_class=self._active_class,
            layer_settings=settings,
            excluded_layers=tuple(
                layer
                for layer in self.layers
                if layer in self._layer_drafts
                and not self._layer_drafts[layer].included
                and self._layer_drafts[layer].confirmed
            ),
        )
        validate_config(Config(specifications=(candidate,)), self.layers)
        self._accept_specification(candidate)

    def _accept_specification(self, candidate: Specification) -> None:
        """Retain latest events for accepted rows, not an ever-growing route history."""
        included_layers = {settings.layer for settings in candidate.layer_settings}
        retained_rows = {
            (section.layer, section.section_id)
            for layer in self.layers
            if layer in included_layers
            for section in preview_sections(candidate, self.snapshot, layer)
        }
        retained_layers = included_layers | set(candidate.excluded_layers)
        self.review_tracking = tracking.ReviewTracking(
            images=tuple(
                record
                for record in self.review_tracking.images
                if (record.layer, record.section_id) in retained_rows
            ),
            layers=tuple(
                record
                for record in self.review_tracking.layers
                if record.layer in retained_layers
            ),
        )
        self.specification = candidate
        self.EndModal(wx.ID_OK)


class ImpedanceDialog(wx.Dialog):
    """Configure intended impedance and explicitly approve detected PCB sections."""

    def __init__(
        self,
        parent: wx.Window,
        config: Config,
        snapshot: BoardSnapshot,
        preview: Callable[..., CapturedImage],
        refresh_snapshot: Callable[[], BoardSnapshot],
        verify_snapshot: Optional[Callable[[BoardSnapshot], None]] = None,
        appearance_context: Optional[Callable[[], object]] = None,
        load_stackup_catalog: Optional[Callable[[int], CatalogCache]] = None,
        save_stackup_catalog: Optional[Callable[[int, CatalogCache], None]] = None,
        fetch_stackup_catalog: Optional[Callable[..., tuple[Stackup, ...]]] = None,
        load_stackup_colors: Optional[
            Callable[[], dict[str, tuple[int, int, int, int]]]
        ] = None,
        save_config: Optional[Callable[[Config], None]] = None,
    ) -> None:
        super().__init__(
            parent,
            title="JLCPCB controlled impedance",
            size=(1040, 740),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.config = config
        self.session = ReviewSession(config, snapshot)
        self.preview_callback = preview
        self.verify_snapshot = verify_snapshot
        self.appearance_context = appearance_context
        self.refresh_snapshot = refresh_snapshot
        self.save_config = save_config
        self.load_stackup_catalog = load_stackup_catalog
        self.save_stackup_catalog = save_stackup_catalog
        self.fetch_stackup_catalog = fetch_stackup_catalog
        self.load_stackup_colors = load_stackup_colors
        self._catalog_controller: Optional[CatalogController] = None
        self._catalog_unsubscribe: Optional[Callable[[], None]] = None
        self._catalog_epoch = 0
        self._catalog_check_scheduled = False
        self._closing = False
        self._child_dialog_open = False
        self._row_ids: tuple[str, ...] = ()
        self._rows_error = ""
        root = wx.BoxSizer(wx.VERTICAL)
        stackup_row = wx.BoxSizer(wx.HORIZONTAL)
        self.stackup_summary = wx.StaticText(self, label="")
        self.stackup_summary.Wrap(630)
        stackup_row.Add(self.stackup_summary, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.stackup_button = wx.Button(self, label="Select stackup…")
        self.stackup_button.Bind(wx.EVT_BUTTON, self._on_stackup)
        stackup_row.Add(self.stackup_button, 0, wx.RIGHT, 8)
        root.Add(stackup_row, 0, wx.ALL | wx.EXPAND, 12)
        self.catalog_status = wx.TextCtrl(
            self,
            value="Catalog: waiting for the impedance dialog to open.",
            size=(-1, 36),
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.BORDER_NONE,
        )
        root.Add(self.catalog_status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)
        self.summary_tabs = wx.Notebook(self)
        specification_page = wx.Panel(self.summary_tabs)
        width_page = wx.Panel(self.summary_tabs)
        self.summary_tabs.AddPage(specification_page, "Specifications")
        self.summary_tabs.AddPage(width_page, "Width comparisons")
        self.specifications = wx.ListCtrl(
            specification_page, style=wx.LC_REPORT | wx.LC_SINGLE_SEL
        )
        for index, (title, width) in enumerate(
            (
                ("Label", 150),
                ("Type", 170),
                ("Ω", 65),
                ("Signal layers", 110),
                ("Width (mm)", 110),
                ("References", 150),
                ("Net class", 200),
            )
        ):
            self.specifications.InsertColumn(index, title, width=width)
        self.specifications.SetMinSize((-1, 105))
        specification_layout = wx.BoxSizer(wx.VERTICAL)
        specification_layout.Add(self.specifications, 1, wx.EXPAND)
        specification_page.SetSizer(specification_layout)
        self.summary_tabs.SetMinSize((-1, 125))
        root.Add(self.summary_tabs, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 12)
        spec_buttons = wx.BoxSizer(wx.HORIZONTAL)
        self._selection_buttons: list[wx.Button] = []
        for label, handler in (
            ("Add…", self._on_add),
            ("Edit…", self._on_edit),
            ("Remove", self._on_remove),
        ):
            button = wx.Button(self, label=label)
            button.Bind(wx.EVT_BUTTON, handler)
            spec_buttons.Add(button, 0, wx.RIGHT, 8)
            if label != "Add…":
                self._selection_buttons.append(button)
        self.summary_tabs.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self._on_summary_page)
        root.Add(spec_buttons, 0, wx.ALL | wx.EXPAND, 12)
        self.width_comparisons = wx.ListCtrl(
            width_page, style=wx.LC_REPORT | wx.LC_SINGLE_SEL
        )
        for index, (title, width) in enumerate(
            (
                ("Class / layer", 170),
                ("Target Ω", 65),
                ("Actual width", 170),
                ("Nominal calculated width", 180),
                ("Actual − nominal", 170),
                ("Calculation status", 240),
            )
        ):
            self.width_comparisons.InsertColumn(index, title, width=width)
        self.width_comparisons.SetMinSize((-1, 105))
        width_layout = wx.BoxSizer(wx.VERTICAL)
        width_layout.Add(self.width_comparisons, 1, wx.EXPAND)
        width_page.SetSizer(width_layout)
        content = wx.BoxSizer(wx.HORIZONTAL)
        left = wx.BoxSizer(wx.VERTICAL)
        left.Add(
            wx.StaticText(self, label="Workbook rows — include the rows to export:"),
            0,
            wx.BOTTOM,
            6,
        )
        self.sections = wx.CheckListBox(self, style=wx.LB_SINGLE)
        self.sections.SetMinSize((350, 130))
        self.sections.Bind(wx.EVT_CHECKLISTBOX, self._on_include)
        self.sections.Bind(wx.EVT_LISTBOX, self._on_preview)
        left.Add(self.sections, 1, wx.EXPAND)
        self.review_status = wx.StaticText(self, label="")
        self._review_text_colour = self.review_status.GetForegroundColour()
        left.Add(self.review_status, 0, wx.TOP | wx.BOTTOM | wx.EXPAND, 8)
        self.approve_button = wx.Button(self, label="Approve workbook rows")
        self.approve_button.Bind(wx.EVT_BUTTON, self._on_approve)
        left.Add(self.approve_button, 0, wx.EXPAND)
        self.retry_button = wx.Button(self, label="Retry")
        self.retry_button.Bind(wx.EVT_BUTTON, self._on_retry_rows)
        self.retry_button.Hide()
        left.Add(self.retry_button, 0, wx.TOP | wx.EXPAND, 8)
        content.Add(left, 4, wx.RIGHT | wx.EXPAND, 12)
        from .dialog_preview import WorkbookPreview

        self.preview_pane = WorkbookPreview(
            self,
            preview,
            verify_current=self._verify_review_snapshot,
            appearance_context=appearance_context,
            appearance_changed=self.session.invalidate_review,
            review_failed=self._preview_failed,
            image_viewed=self._on_main_image_viewed,
            active=lambda: bool(self)
            and not self._closing
            and not self._child_dialog_open,
            hide_navigation=True,
            state_changed=self._populate_review_status,
        )
        content.Add(self.preview_pane.window, 6, wx.EXPAND)
        root.Add(content, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)
        self.status = wx.TextCtrl(
            self,
            value="Add a specification to create workbook rows.",
            size=(-1, 44),
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.BORDER_NONE,
        )
        root.Add(self.status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        buttons.AddStretchSpacer()
        buttons.Add(
            self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALIGN_CENTER_VERTICAL
        )
        self.FindWindow(wx.ID_OK).SetLabel("Save and close")
        root.Add(buttons, 0, wx.ALL | wx.EXPAND, 12)
        self.Bind(wx.EVT_BUTTON, self._on_save, id=wx.ID_OK)
        self.Bind(wx.EVT_BUTTON, self._on_cancel, id=wx.ID_CANCEL)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_SHOW, self._on_main_show)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self.specifications.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_edit)
        self.SetSizer(root)
        self.SetMinSize((980, 680))
        self._populate_specifications()
        self._refresh_rows(snapshot)
        self.CentreOnParent()

    def _on_main_show(self, event: wx.ShowEvent) -> None:
        """Start the catalog and selected capture only after the dialog is visible."""
        if (
            event.GetEventObject() is self
            and event.IsShown()
            and not self._closing
            and not self._catalog_check_scheduled
        ):
            self._catalog_check_scheduled = True
            wx.CallAfter(_begin_main_catalog, ref(self), self._catalog_epoch)
        if event.GetEventObject() is self and event.IsShown() and not self._closing:
            # EVT_SHOW can precede actual native visibility on some platforms.
            wx.CallAfter(self._queue_selected_preview)
        event.Skip()

    def _ensure_catalog_started(
        self, layer_count: Optional[int] = None
    ) -> CatalogController:
        """Join one check, replacing its owner only when the copper count changes."""
        if self._closing:
            raise ValueError("The impedance dialog is closing.")
        count = (
            len(self.session.snapshot.layers) if layer_count is None else layer_count
        )
        controller = self._catalog_controller
        if controller is None or controller.layer_count != count:
            self._stop_catalog()
            owner = ref(self)

            def owner_current() -> bool:
                """Avoid saving after native destruction or a layer-count replacement."""
                dialog = owner()
                return (
                    dialog is not None
                    and not dialog._closing
                    and bool(dialog)
                    and dialog._catalog_controller is not None
                    and dialog._catalog_controller.layer_count == count
                )

            controller = CatalogController(
                count,
                load_cache=self.load_stackup_catalog,
                save_cache=self.save_stackup_catalog,
                fetch_catalog=self.fetch_stackup_catalog,
                owner_current=owner_current,
            )
            self._catalog_controller = controller
            self._catalog_unsubscribe = controller.subscribe(self._on_catalog_state)
        controller.ensure_started()
        return controller

    def _on_catalog_state(self, state: CatalogState) -> None:
        """Keep catalog activity visible independently of section-review messages."""
        if self._closing or not self:
            return
        count = (
            self._catalog_controller.layer_count
            if self._catalog_controller is not None
            else len(self.session.snapshot.layers)
        )
        self.catalog_status.ChangeValue(
            f"Catalog ({count} copper layers): {state.message}"
        )
        self.catalog_status.SetForegroundColour(
            wx.Colour(195, 35, 35)
            if state.error
            else wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT)
        )
        self.catalog_status.Refresh()

    def _show_catalog_error(self, message: str) -> None:
        """Do not let controller-setup failures overwrite final section-review state."""
        if not self._closing and self:
            self.catalog_status.ChangeValue(
                "Catalog: " + (message or "The catalog check could not start.")
            )
            self.catalog_status.SetForegroundColour(wx.Colour(195, 35, 35))
            self.catalog_status.Refresh()

    def _stop_catalog(self) -> None:
        """Invalidate queued startup/delivery before detaching and canceling its owner."""
        self._catalog_epoch += 1
        if self._catalog_unsubscribe is not None:
            self._catalog_unsubscribe()
            self._catalog_unsubscribe = None
        if self._catalog_controller is not None:
            self._catalog_controller.close()
            self._catalog_controller = None

    def _sync_catalog_count(self, snapshot: BoardSnapshot) -> None:
        """Follow a newly observed board count without blocking its separate review."""
        try:
            self._ensure_catalog_started(len(snapshot.layers))
        except Exception as error:
            self._show_catalog_error(str(error))

    def _populate_stackup(self) -> None:
        """Display the saved vendor construction without implying a KiCad board edit."""
        stackup = self.session.config.stackup
        if stackup is None:
            text = "Stackup: not selected — nominal widths are not calculated."
        else:
            text = (
                f"Stackup: {stackup.name} · {stackup.layer_count} copper layers · "
                f"{stackup.thickness_mm} mm · outer {stackup.outer_copper_oz} oz"
            )
            if stackup.inner_copper_oz:
                text += f" / inner {stackup.inner_copper_oz} oz"
            if stackup.layer_count != len(self.session.snapshot.layers):
                text += (
                    " — layer count differs from this board; select a matching stackup."
                )
        self.stackup_summary.SetLabel(text)
        self.stackup_summary.Wrap(630)

    def _populate_width_comparisons(self) -> None:
        """Show actual-minus-nominal dimensions using the report's shared semantics."""
        from .width_checks import format_width_delta_nm, format_width_nm, width_check

        self.width_comparisons.DeleteAllItems()
        config = self.session.config
        snapshot = self.session.snapshot
        for spec in config.specifications:
            nets = {
                net
                for net, classes in snapshot.net_class_memberships
                if net and spec.net_class in classes
            }
            for settings in spec.layer_settings:
                widths = sorted(
                    {
                        trace.width_nm
                        for trace in snapshot.traces
                        if trace.net in nets and trace.layer == settings.layer
                    }
                )
                for actual in widths:
                    check = width_check(config, spec, settings.layer, actual)
                    nominal = (
                        format_width_nm(check.target_width_nm)
                        if check.result_current and check.target_width_nm is not None
                        else "Not calculated"
                    )
                    delta = (
                        format_width_delta_nm(check.delta_nm)
                        if check.delta_nm is not None
                        else "—"
                    )
                    index = self.width_comparisons.InsertItem(
                        self.width_comparisons.GetItemCount(),
                        f"{spec.net_class} / {settings.layer}",
                    )
                    for column, value in enumerate(
                        (
                            spec.target_ohms,
                            format_width_nm(actual),
                            nominal,
                            delta,
                            check.message,
                        ),
                        start=1,
                    ):
                        self.width_comparisons.SetItem(index, column, value)

    def _on_stackup(self, event: wx.CommandEvent) -> None:
        """Select explicit board intent independently of automatic catalog checks."""
        from .stackup_dialog import StackupDialog
        from .stackup_model import stackup_fingerprint

        try:
            snapshot = self.refresh_snapshot()
            count = len(snapshot.layers)
            controller = self._ensure_catalog_started(count)
            dialog = StackupDialog(
                self,
                count,
                self.session.config.stackup,
                catalog_controller=controller,
                load_copper_colors=self.load_stackup_colors,
            )
            self._child_dialog_open = True
            try:
                if dialog.ShowModal() != wx.ID_OK or dialog.stackup is None:
                    return
                try:
                    if self.refresh_snapshot() != snapshot:
                        raise ValueError(
                            "The board changed while selecting a stackup. Reopen the selector for the current board."
                        )
                except Exception:
                    self._invalidate_visual_review()
                    raise
                current = self.session.config
                changed = stackup_fingerprint(current.stackup) != stackup_fingerprint(
                    dialog.stackup
                )
                if changed:
                    self.session.replace_calculation_context(
                        dialog.stackup, current.width_results
                    )
                else:
                    self.session.config = replace(current, stackup=dialog.stackup)
                refreshed = self._refresh_rows(snapshot)
                if changed and refreshed:
                    self._error(
                        "Stackup changed. Review width comparisons and approve the updated workbook rows."
                    )
            finally:
                self._child_dialog_open = False
                dialog.Destroy()
                self._queue_selected_preview()
        except Exception as error:
            self._error(str(error))

    def _on_cancel(self, event: wx.CommandEvent) -> None:
        """Discard local editing state through the normal Cancel event."""
        self._closing = True
        self._stop_catalog()
        event.Skip()

    def _on_close(self, event: wx.CloseEvent) -> None:
        """Prevent worker completion from touching a closing native dialog."""
        self._closing = True
        self._stop_catalog()
        event.Skip()

    def _on_destroy(self, event: wx.WindowDestroyEvent) -> None:
        """Cancel owned work even when native destruction bypasses modal buttons."""
        if event.GetEventObject() is self:
            self._closing = True
            self._stop_catalog()
        event.Skip()

    def _populate_specifications(self) -> None:
        """Refresh the specification summary without changing the edit state."""
        self.specifications.DeleteAllItems()
        kinds = dict(KIND_CHOICES)
        for specification in self.session.config.specifications:
            index = self.specifications.InsertItem(
                self.specifications.GetItemCount(), specification.label
            )
            for column, text in enumerate(
                (
                    kinds.get(specification.kind, specification.kind),
                    specification.target_ohms,
                    ", ".join(
                        settings.layer for settings in specification.layer_settings
                    ),
                    "All actual widths",
                    "; ".join(
                        f"{settings.layer}: {', '.join(settings.reference_layers)}"
                        for settings in specification.layer_settings
                    ),
                    specification.net_class,
                ),
                start=1,
            ):
                self.specifications.SetItem(index, column, text)

    def _on_summary_page(self, event: wx.BookCtrlEvent) -> None:
        """Never apply edit/remove actions to a selection hidden in another tab."""
        for button in self._selection_buttons:
            button.Enable(self.summary_tabs.GetSelection() == 0)
        event.Skip()

    def _refresh_rows(
        self,
        snapshot: Optional[BoardSnapshot] = None,
        preferred_spec_id: Optional[str] = None,
    ) -> bool:
        """Refresh the document once, retaining valid editable settings on failure."""
        if self._closing:
            return False
        try:
            current = self.refresh_snapshot() if snapshot is None else snapshot
            if (
                self._catalog_controller is not None
                and self._catalog_controller.layer_count != len(current.layers)
            ):
                self._sync_catalog_count(current)
            self.session.refresh(current)
            self._rows_error = ""
        except Exception as error:
            self.session.analysis = None
            self.session.included.clear()
            self.session.invalidate_review()
            self._rows_error = str(error) or "Workbook rows could not be populated."
        self._populate_stackup()
        self._populate_width_comparisons()
        self._populate_sections(preferred_spec_id)
        return not self._rows_error

    def _on_retry_rows(self, event: wx.CommandEvent) -> None:
        if self._refresh_rows():
            self.preview_pane.select(self.sections.GetSelection(), refresh=True)

    def _populate_sections(self, preferred_spec_id: Optional[str] = None) -> None:
        """Present the document while retaining the selected logical row."""
        analysis = self.session.analysis
        old_index = self.sections.GetSelection()
        selected_id = (
            self._row_ids[old_index] if 0 <= old_index < len(self._row_ids) else None
        )
        specifications = {
            spec.spec_id: spec for spec in self.session.config.specifications
        }
        self.sections.Clear()
        self._row_ids = (
            tuple(section.section_id for section in analysis.sections)
            if analysis
            else ()
        )
        if analysis:
            for index, section in enumerate(analysis.sections):
                spec = specifications[section.spec_id]
                self.sections.Append(
                    f"{spec.label or spec.target_ohms + ' Ω'} · {section.layer} · {format_distance(section.width_nm)} mm · {', '.join(section.net_names)} · {len(section.traces)} segments"
                )
                self.sections.Check(index, section.section_id in self.session.included)
            preferred = next(
                (
                    section.section_id
                    for section in analysis.sections
                    if section.spec_id == preferred_spec_id
                ),
                None,
            )
            selected_id = preferred or selected_id
            if self._row_ids:
                index = (
                    self._row_ids.index(selected_id)
                    if selected_id in self._row_ids
                    else next(
                        (
                            i
                            for i, row in enumerate(self._row_ids)
                            if row in self.session.included
                        ),
                        0,
                    )
                )
                self.sections.SetSelection(index)
                selected_id = self._row_ids[index]
            self.preview_pane.set_sections(
                analysis.sections, self.session.snapshot, selected_id
            )
        else:
            self.preview_pane.clear(
                self._rows_error or "No workbook rows are available."
            )
        self.FindWindow(wx.ID_OK).Enable(True)
        if self._rows_error:
            text = self._rows_error + " Settings can still be saved as a draft."
        elif self.preview_pane.failure:
            text = self.preview_pane.failure
        elif not self.session.config.specifications:
            text = "Add a specification to create workbook rows. You can save the stackup and continue later."
        elif not analysis or not analysis.sections:
            text = "No matching workbook rows. Check the selected net classes and signal layers."
        elif self.session.approved:
            text = "Workbook rows approved. Save and close when ready."
        else:
            text = f"{len(analysis.sections)} workbook rows found; {len(self.session.included)} included. Review the rows, then approve them below the list."
            if analysis.warnings:
                text += "\n" + "\n".join(str(warning) for warning in analysis.warnings)
        self.status.SetValue(text)
        self._populate_review_status()
        self.Layout()

    def _populate_review_status(self) -> None:
        """Derive approval controls from the document and shared image component."""
        count = len(self.session.included)
        rows = f"{count} workbook {'row' if count == 1 else 'rows'}"
        reviewed = self.session.approved
        self.review_status.SetLabel(
            f"✓ {rows} approved"
            if reviewed
            else "Needs approval"
            if count
            else "No workbook rows to approve"
        )
        self.review_status.SetForegroundColour(
            wx.Colour(35, 135, 60) if reviewed else self._review_text_colour
        )
        self.approve_button.SetLabel(
            "Workbook rows approved"
            if reviewed
            else f"Approve {rows}"
            if count
            else "Approve workbook rows"
        )
        self.approve_button.Enable(
            bool(self.session.analysis and count and self.preview_pane.ready)
            and not reviewed
            and not self._rows_error
        )
        self.retry_button.Show(bool(self._rows_error or self.preview_pane.failure))
        self.retry_button.Enable(True)

    def _queue_selected_preview(self) -> None:
        """Resume the shared component after native show or a child dialog."""
        if not self._closing and not self._rows_error:
            self.preview_pane.show_selected()

    def _edit_specification(self, index: Optional[int]) -> None:
        """Commit a successful child dialog into this dialog's private session."""
        specifications = list(self.session.config.specifications)
        original = specifications[index] if index is not None else None
        try:
            snapshot = self.refresh_snapshot()
            self._sync_catalog_count(snapshot)
            dialog = SpecificationDialog(
                self,
                snapshot,
                original,
                preview=self.preview_callback,
                verify_snapshot=self.verify_snapshot,
                appearance_context=self.appearance_context,
                review_tracking=self.session.config.review_tracking,
                stackup=self.session.config.stackup,
                width_results=self.session.config.width_results,
            )
        except Exception as error:
            self._error(str(error))
            return
        self._child_dialog_open = True
        try:
            if dialog.ShowModal() != wx.ID_OK or dialog.specification is None:
                return
            if index is None:
                specifications.append(dialog.specification)
            else:
                specifications[index] = dialog.specification
            changed = original != dialog.specification
            self.session.replace_specifications(tuple(specifications))
            self.session.config = replace(
                self.session.config,
                review_tracking=tracking.merge_spec(
                    self.session.config.review_tracking,
                    dialog.review_tracking,
                    dialog.specification.spec_id,
                ),
            )
            self._populate_specifications()
            self._refresh_rows(
                preferred_spec_id=dialog.specification.spec_id if changed else None
            )
        finally:
            self._child_dialog_open = False
            dialog.Destroy()
            self._queue_selected_preview()

    def _on_add(self, event: wx.CommandEvent) -> None:
        """Add a specification after child-dialog acceptance."""
        self._edit_specification(None)

    def _on_edit(self, event: wx.CommandEvent) -> None:
        """Edit the selected specification."""
        if self.summary_tabs.GetSelection() != 0:
            return
        index = self.specifications.GetFirstSelected()
        if index >= 0:
            self._edit_specification(index)

    def _on_remove(self, event: wx.CommandEvent) -> None:
        """Remove the selected rule and invalidate its scan."""
        if self.summary_tabs.GetSelection() != 0:
            return
        index = self.specifications.GetFirstSelected()
        if index >= 0:
            specifications = list(self.session.config.specifications)
            del specifications[index]
            self.session.replace_specifications(tuple(specifications))
            self._populate_specifications()
            self._refresh_rows()

    def _on_include(self, event: wx.CommandEvent) -> None:
        """Invalidate approval when a candidate checkbox changes."""
        if self.session.analysis is None:
            return
        self.session.set_included(
            section.section_id
            for index, section in enumerate(self.session.analysis.sections)
            if self.sections.IsChecked(index)
        )
        self._populate_sections()

    def _on_preview(self, event: wx.CommandEvent) -> None:
        self.preview_pane.select(event.GetSelection())

    def _on_main_image_viewed(
        self, section: Section, image_sha256: str, viewed_at: str
    ) -> None:
        """Keep successfully displayed image events in the private document."""
        spec = next(
            spec
            for spec in self.session.config.specifications
            if spec.spec_id == section.spec_id
        )
        digest = tracking.capture_fingerprint(
            tracking.source_fingerprint(self.session.snapshot),
            spec,
            section,
            image_sha256,
        )
        self.session.config = replace(
            self.session.config,
            review_tracking=tracking.put_image(
                self.session.config.review_tracking,
                tracking.ImageView(
                    section.spec_id,
                    section.layer,
                    section.section_id,
                    digest,
                    viewed_at,
                ),
            ),
        )

    def _preview_failed(self) -> None:
        self.session.invalidate_review()
        self._populate_review_status()
        self._error(self.preview_pane.failure)

    def _on_approve(self, event: wx.CommandEvent) -> None:
        """Approve the checked candidates explicitly."""
        try:
            if not self.preview_pane.ready:
                raise ValueError(
                    "The preview needs to finish successfully before approval. Choose Retry if it failed."
                )
            self.preview_pane.ensure_appearance_current()
            previous = (
                self.session.analysis.digest
                if self.session.analysis is not None
                else None
            )
            if not self._refresh_rows():
                return
            if (
                self.session.analysis is None
                or self.session.analysis.digest != previous
            ):
                self._error(
                    "Workbook rows changed. Review the updated rows, then approve them."
                )
                return
            self._verify_review_snapshot()
            self.session.approve()
            self._populate_sections()
        except ValueError as error:
            self._error(str(error))

    def _on_save(self, event: wx.CommandEvent) -> None:
        """Commit settings while still open; a failed save retains the working copy."""
        if self._closing:
            return
        try:
            try:
                snapshot = self.refresh_snapshot()
            except Exception:
                self._fail_rows(
                    "The current board could not be read. Retry when it is available."
                )
                raise
            if snapshot != self.session.snapshot:
                self._refresh_rows(snapshot)
            try:
                self.preview_pane.ensure_appearance_current()
            except Exception:
                # An unavailable/changed palette invalidates approval, not intent.
                self.session.invalidate_review()
            candidate = self.session.save_result(snapshot)
            if self.session.config.reviewed_digest and not candidate.reviewed_digest:
                self._invalidate_visual_review()
            if self.save_config is not None:
                self.save_config(candidate)
            # Only successful persistence may publish the result and close. A
            # standalone preview harness can omit persistence and inspect config.
            self.config = candidate
            self.session.config = candidate
            self._closing = True
            self._stop_catalog()
            self.EndModal(wx.ID_OK)
        except Exception as error:
            self._populate_sections()
            self._error(str(error))

    def _error(self, message: str) -> None:
        """Show an actionable error while retaining the dialog's edit state."""
        self.status.SetValue(message)
        self.Layout()

    def _verify_review_snapshot(self) -> None:
        """Replace stale live rows without allowing the interrupted action to continue."""
        try:
            current = self.refresh_snapshot()
            if current != self.session.snapshot:
                self._refresh_rows(current)
                raise _RowsRefreshed(
                    "The board changed. Review the updated workbook rows, then repeat the action."
                )
        except _RowsRefreshed:
            raise
        except Exception as error:
            self._fail_rows(str(error) or "The current board could not be verified.")
            raise ValueError(
                str(error)
                or "The current board could not be verified. Retry when it is available."
            ) from error

    def _fail_rows(self, message: str) -> None:
        """Remove stale rows and pixels after a failed live-board verification."""
        self.session.analysis = None
        self.session.included.clear()
        self.session.invalidate_review()
        self._rows_error = message
        self._populate_sections()

    def _invalidate_visual_review(self) -> None:
        """Clear stale pixels and export approval, while allowing draft settings saves."""
        self.session.invalidate_review()
        self.preview_pane.clear("Review the updated workbook images.")
        self.FindWindow(wx.ID_OK).Enable(True)
        self._populate_review_status()
