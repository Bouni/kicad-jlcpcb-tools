"""Transactional controlled-impedance specification and section review dialog."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, replace
from decimal import InvalidOperation
import logging
from threading import Event, Thread
import time
from typing import TYPE_CHECKING, Optional
from uuid import uuid4
from weakref import ReferenceType, ref

import wx
import wx.adv
import wx.dataview as dv

from . import review_tracking as tracking
from .calculator_config_cache import (
    CalculatorConfigCache,
    calculator_config_check_due,
    calculator_config_checked_at,
)
from .capture_retry import retry_delay_ms
from .catalog_cache import CatalogCache
from .catalog_controller import CatalogController, CatalogState, compatible_stackup
from .matching import analyze, preview_sections, validate_review, workbook_row_cells
from .model import (
    EXCLUSION_COVERED_STUB,
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
    validate_storage_config,
)
from .service import CapturedImage, CaptureEvidence

if TYPE_CHECKING:
    from .jlcpcb_calculator import ProviderConfiguration

_LOG = logging.getLogger(__name__)

KIND_CHOICES = (
    ("single_ended", "Single Ended (Non coplanar)"),
    ("differential", "Differential Pair (Non coplanar)"),
    ("single_ended_coplanar", "Single Ended (Coplanar)"),
    ("differential_coplanar", "Differential Pair (Coplanar)"),
)

REFERENCE_HELP = "Verify that the selected copper layers are ground reference planes."
WIDTH_REFRESH_RETRY_MS = 3 * 60 * 1000
WIDTH_REFRESH_SLOW_MS = 15 * 1000
WIDTH_REFRESH_SLOW_PER_JOB_MS = 10 * 1000
DRAFT_WIDTH_CALC_DEBOUNCE_MS = 500
DRAFT_WIDTH_CALC_RETRY_MS = 60 * 1000
CAPTURE_PREPARE_INTERVAL_MS = 25
CAPTURE_CACHE_BYTES = 64 * 1024 * 1024
CALCULATOR_LINK_LABEL = "JLCPCB Impedance calculator"
CALCULATOR_URL = "https://jlcpcb.com/pcb-impedance-calculator"
INACTIVE_DIMENSION_PLACEHOLDER = "N/A"
# Results worth persisting the moment they arrive: the answer for these exact
# inputs, or a construction the calculator deterministically refuses. Transient
# failures ("unavailable"/"error") are retried instead of cached.
SETTLED_WIDTH_STATUSES = frozenset(("success", "unsupported"))
SPEC_REVIEW_APPROVED = "Approved"
SPEC_REVIEW_NEEDS_APPROVAL = "Needs approval"
SPEC_REVIEW_NEEDS_CONFIGURATION = "Needs configuration"
SPEC_REVIEW_NO_MATCHING_ROWS = "No matching rows"
# Review column only: white text on coloured backgrounds via DataViewItemAttr.
# Always applied for every status so Cocoa cannot retain a cleared background.
SPEC_REVIEW_APPROVED_BACKGROUND = (35, 135, 60)
SPEC_REVIEW_PENDING_BACKGROUND = (160, 90, 16)
SPEC_REVIEW_TEXT_COLOUR = (255, 255, 255)
_SPECIFICATION_COLUMNS = (
    ("Review", 120),
    ("Net class", 180),
    ("Signal layers", 110),
    ("Ω", 65),
    ("Type", 170),
    ("Label", 150),
    ("Width (mm)", 110),
    ("References", 150),
)
_WORKBOOK_COLUMNS = (
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
)


def _configuration_complete(
    spec: Specification, layers: Optional[tuple[str, ...]] = None
) -> bool:
    """Check physical settings without consulting captures or review history."""
    try:
        validate_config(Config(specifications=(spec,)), layers)
    except ValueError:
        return False
    return True


def _needs_configuration(spec: Specification, snapshot: BoardSnapshot) -> bool:
    """Distinguish missing intent from an otherwise complete visual review."""
    if not _configuration_complete(spec, snapshot.layers):
        return True
    members = {
        net
        for net, classes in snapshot.net_class_memberships
        if spec.net_class in classes
    }
    routed = {trace.layer for trace in snapshot.traces if trace.net in members}
    configured = {settings.layer for settings in spec.layer_settings} | set(
        spec.excluded_layers
    )
    return bool(routed - configured)


def specification_review_label(
    spec_id: str,
    *,
    approved: bool,
    analysis: Optional[Analysis],
    included: Iterable[str],
    layer_review_current: bool = False,
) -> str:
    """Return the Review-column status for one net-class specification.

    Completing the specification dialog's layer/image review marks the class
    Approved. A legacy workbook approval that still includes the class also
    counts. Unmatched classes are called out separately.
    """
    if analysis is None:
        return SPEC_REVIEW_NO_MATCHING_ROWS
    sections = tuple(
        section for section in analysis.sections if section.spec_id == spec_id
    )
    if not sections:
        return SPEC_REVIEW_NO_MATCHING_ROWS
    if layer_review_current:
        return SPEC_REVIEW_APPROVED
    included_ids = set(included)
    represented = any(section.section_id in included_ids for section in sections)
    if approved and represented:
        return SPEC_REVIEW_APPROVED
    return SPEC_REVIEW_NEEDS_APPROVAL


def specification_review_cell_style(
    label: str,
) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Return (background, foreground) for a Review-column status label."""
    background = (
        SPEC_REVIEW_APPROVED_BACKGROUND
        if label == SPEC_REVIEW_APPROVED
        else SPEC_REVIEW_PENDING_BACKGROUND
    )
    return background, SPEC_REVIEW_TEXT_COLOUR


def apply_specification_review_cell_style(
    label: str, attr: dv.DataViewItemAttr
) -> bool:
    """Paint Review-column white text on the status background."""
    background, foreground = specification_review_cell_style(label)
    attr.SetBackgroundColour(wx.Colour(*background))
    attr.SetColour(wx.Colour(*foreground))
    return True


class _SpecificationListStore(dv.DataViewListStore):
    """Apply Review-column cell colours without tinting other columns."""

    def GetAttrByRow(self, row: int, col: int, attr: dv.DataViewItemAttr) -> bool:
        if col != 0:
            return False
        return apply_specification_review_cell_style(self.GetValueByRow(row, col), attr)


def _save_calculator_configuration(
    save: Callable[[CalculatorConfigCache], None],
    cache: CalculatorConfigCache,
    context: str,
) -> None:
    """Persist completed metadata on GUI even if its originating dialog has closed."""
    try:
        save(cache)
    except Exception as error:
        _LOG.info(
            "JLCPCB width: %s provider-config cache write skipped (%s)",
            context,
            error,
        )


def _calculator_configuration_handoff(
    load: Optional[Callable[[], CalculatorConfigCache]],
    save: Optional[Callable[[CalculatorConfigCache], None]],
    *,
    context: str,
) -> tuple[
    Optional[ProviderConfiguration],
    Optional[Callable[[ProviderConfiguration], None]],
]:
    """Read on GUI and give the worker copied metadata and a queued publication hook."""
    configuration = None
    if load is not None:
        try:
            cache = load()
            if not calculator_config_check_due(cache):
                from .jlcpcb_calculator import ProviderConfiguration

                configuration = deepcopy(
                    ProviderConfiguration(
                        cache.models, cache.copper, cache.coating, cache.limits
                    )
                )
        except Exception as error:
            _LOG.info(
                "JLCPCB width: %s provider-config cache unused (%s)", context, error
            )
    _LOG.info(
        "JLCPCB width: %s %s",
        context,
        "using cached provider configuration"
        if configuration is not None
        else "will fetch provider configuration from JLCPCB",
    )
    if save is None:
        return configuration, None

    def publish(provider: ProviderConfiguration) -> None:
        """Queue detached completed metadata without consulting the native owner."""
        cache = deepcopy(
            CalculatorConfigCache(
                models=provider.models,
                copper=provider.copper,
                coating=provider.coating,
                limits=provider.limits,
                checked_at_utc=calculator_config_checked_at(),
            )
        )
        wx.CallAfter(_save_calculator_configuration, save, cache, context)

    return configuration, publish


def _deliver_draft_width_result(
    owner: ReferenceType[SpecificationDialog],
    generation: int,
    digest: str,
    result: Optional[WidthResult],
    failure: str,
) -> None:
    """Resolve the native owner only after GUI dispatch; its handler rejects stale work."""
    dialog = owner()
    if dialog is not None:
        dialog._finish_draft_calculation(generation, digest, result, failure)


def _deliver_width_results(
    owner: ReferenceType[ImpedanceDialog],
    generation: int,
    key: tuple[object, ...],
    results: tuple[WidthResult, ...],
    failure: str,
) -> None:
    """Resolve the batch owner only on GUI, retaining its lifecycle and generation checks."""
    dialog = owner()
    if dialog is not None:
        dialog._finish_calculation(generation, key, results, failure)


def _persist_width_result(
    save: Optional[Callable[[WidthResult], None]],
    result: WidthResult,
    *,
    context: str,
) -> None:
    """Cache one settled solve from a worker thread without failing the batch.

    Persistence failures must never discard a usable in-memory result, so any
    exception is logged and swallowed here, mirroring the calculator-config path.
    """
    if save is None or result.status not in SETTLED_WIDTH_STATUSES:
        return
    try:
        save(result)
    except Exception as error:
        _LOG.info(
            "JLCPCB width: %s cache write skipped for %s/%s: %s",
            context,
            result.spec_id,
            result.layer,
            error,
        )
    else:
        _LOG.info(
            "JLCPCB width: %s cached %s/%s status=%s",
            context,
            result.spec_id,
            result.layer,
            result.status,
        )


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


def _is_inactive_dimension(value: str) -> bool:
    """Recognize the disabled-field placeholder without treating it as user input."""
    return value.strip().casefold() == INACTIVE_DIMENSION_PLACEHOLDER.casefold()


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
        self._analysis_key: Optional[tuple[object, ...]] = None
        self.included: set[str] = set()
        # Loaded hashes are current evidence, not image-view events. None marks
        # an unavailable capture while historical records remain untouched.
        self.capture_digests: dict[str, Optional[str]] = {}
        self.config = self.save_result(snapshot)
        if self.analysis is not None:
            self.included = (
                set(self.config.included_section_ids)
                if self.config.reviewed_digest
                else {
                    section.section_id
                    for section in self.analysis.sections
                    if not section.exclusion_reason
                }
            )

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
        self.config = replace(
            self.config,
            reviewed_digest="",
            included_section_ids=(),
            reviewed_captures=(),
        )

    def replace_specifications(self, specifications: tuple[Specification, ...]) -> None:
        """Replace specifications, retain relevant history, and invalidate review."""
        if specifications == self.config.specifications:
            return
        physical_changed = tuple(
            replace(spec, linked_layer_groups=(), independent_layers=())
            for spec in specifications
        ) != tuple(
            replace(spec, linked_layer_groups=(), independent_layers=())
            for spec in self.config.specifications
        )
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
        if physical_changed:
            self.invalidate_review()
            self.analysis = None
            self.included.clear()
            self.capture_digests.clear()

    def replace_calculation_context(
        self, stackup: Optional[Stackup], width_results: tuple[WidthResult, ...]
    ) -> None:
        """Replace the calculation inputs and invalidate the current row review."""
        self.config = replace(self.config, stackup=stackup, width_results=width_results)
        self.invalidate_review()
        self.analysis = None
        self.included.clear()
        self.capture_digests.clear()

    def refresh(self, snapshot: Optional[BoardSnapshot] = None) -> Analysis:
        """Populate rows automatically, preserving unchanged selections/approval."""
        previous = self.analysis
        self.snapshot = snapshot if snapshot is not None else self.snapshot
        try:
            incomplete = any(
                _needs_configuration(spec, self.snapshot)
                for spec in self.config.specifications
            )
            if incomplete:
                validate_storage_config(self.config)
                self.invalidate_review()
                # Complete specifications stay previewable while another class
                # is unfinished. Approval/export always validates the full config.
                display = replace(
                    self.config,
                    specifications=tuple(
                        spec
                        for spec in self.config.specifications
                        if not _needs_configuration(spec, self.snapshot)
                    ),
                )
                result = (
                    previous
                    if previous is not None
                    and self._analysis_key == self._analysis_inputs(self.snapshot)
                    else analyze(display, self.snapshot)
                )
            else:
                result = (
                    previous
                    if previous is not None
                    and self._analysis_key == self._analysis_inputs(self.snapshot)
                    else analyze(self.config, self.snapshot)
                )
        except Exception:
            self.analysis = None
            self.included.clear()
            self.capture_digests.clear()
            self.invalidate_review()
            raise
        all_ids = {section.section_id for section in result.sections}
        # Covered stubs stay unchecked by default; an explicit check is preserved.
        default_included = {
            section.section_id
            for section in result.sections
            if not section.exclusion_reason
        }
        if previous is not None and previous.digest == result.digest:
            self.included &= all_ids
        elif self.config.reviewed_digest == result.digest:
            self.included = set(self.config.included_section_ids) & all_ids
        else:
            self.included = default_included
        self.analysis = result
        self._analysis_key = self._analysis_inputs(self.snapshot)
        if previous is not None and previous.digest != result.digest:
            self.capture_digests.clear()
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
        all_ids = {section.section_id for section in self.analysis.sections}
        requested = set(section_ids)
        if not requested <= all_ids:
            raise ValueError("A selected section is no longer present.")
        # Covered stubs stay unchecked by default, but an explicit check is kept.
        included = requested
        if included != self.included:
            self.invalidate_review()
        self.included = included

    def approve(self, captures: Optional[Mapping[str, str]] = None) -> None:
        """Approve exactly the checked rows of the current document."""
        if self.analysis is None or not self.included:
            raise ValueError("Include at least one current PCB section.")
        if captures is not None and set(captures) != self.included:
            raise ValueError(
                "Every included workbook image must be verified before approval."
            )
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
            reviewed_captures=tuple(
                (section.section_id, captures[section.section_id])
                for section in self.analysis.sections
                if captures is not None and section.section_id in self.included
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

    def approve_if_layer_reviews_complete(
        self, *, required_section_ids: Optional[set[str]] = None
    ) -> bool:
        """Approve the workbook when every matched class has current layer reviews."""
        if self.approved:
            return True
        if self.analysis is None or not self.included:
            return False
        source_digest = tracking.source_fingerprint(self.snapshot)
        for spec in self.config.specifications:
            if not any(
                section.spec_id == spec.spec_id for section in self.analysis.sections
            ):
                continue
            if not tracking.specification_layer_review_current(
                self.config.review_tracking,
                source_digest,
                spec,
                self.analysis.sections,
                stackup=self.config.stackup,
                width_results=self.config.width_results,
                capture_digests=self.capture_digests,
                required_section_ids=required_section_ids,
            ):
                return False
        try:
            self.approve()
        except ValueError:
            return False
        return True

    def _analysis_inputs(self, snapshot: BoardSnapshot) -> tuple[object, ...]:
        """Identify every input behind the cached matching result."""
        from .matching import (
            ALGORITHM_VERSION,
            CAPTURE_POLICY_REVISION,
            COVERED_STUB_POLICY_REVISION,
        )
        from .stackup_model import CALCULATION_REVISION

        return (
            snapshot,
            self.config.specifications,
            self.config.stackup,
            self.config.width_results,
            ALGORITHM_VERSION,
            CAPTURE_POLICY_REVISION,
            COVERED_STUB_POLICY_REVISION,
            CALCULATION_REVISION,
        )

    def save_result(self, snapshot: Optional[BoardSnapshot] = None) -> Config:
        """Save valid intent independently of whether report review is complete."""
        validate_storage_config(self.config)
        if self.config.reviewed_digest:
            try:
                reviewed = replace(self.config, enabled=True)
                current = snapshot if snapshot is not None else self.snapshot
                analysis = (
                    self.analysis
                    if self.analysis is not None
                    and self._analysis_key == self._analysis_inputs(current)
                    else analyze(reviewed, current)
                )
                self.analysis = analysis
                self._analysis_key = self._analysis_inputs(current)
                validate_review(reviewed, analysis)
                return self.config
            except ValueError:
                pass
        return replace(
            self.config,
            reviewed_digest="",
            included_section_ids=(),
            reviewed_captures=(),
        )


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
        calculate_width: Optional[Callable[..., WidthResult]] = None,
        load_calculator_config: Optional[Callable[[], CalculatorConfigCache]] = None,
        save_calculator_config: Optional[
            Callable[[CalculatorConfigCache], None]
        ] = None,
        save_width_result: Optional[Callable[[WidthResult], None]] = None,
        skip_stub_review: Optional[Callable[[], bool]] = None,
        preserve_approved_width_results: bool = False,
    ) -> None:
        if specification is not None:
            try:
                validate_storage_config(Config(specifications=(specification,)))
                members = {
                    net
                    for net, classes in snapshot.net_class_memberships
                    if specification.net_class in classes
                }
                routed = {
                    trace.layer for trace in snapshot.traces if trace.net in members
                }
                included = tuple(
                    item
                    for item in specification.layer_settings
                    if item.layer in routed
                )
                excluded = tuple(
                    layer for layer in specification.excluded_layers if layer in routed
                )
                active = replace(
                    specification,
                    layer_settings=included,
                    excluded_layers=excluded,
                    linked_layer_groups=(),
                    independent_layers=(),
                )
                # Only current signal entries are checked against the current
                # stack. Removed unused entries are pruned from the private edit.
                # References on active entries still require enabled copper.
                if included or excluded:
                    validate_storage_config(
                        Config(specifications=(active,)), snapshot.layers
                    )
            except ValueError as error:
                raise ValueError(
                    f"Saved specification cannot be edited with the enabled board stack: {error} "
                    "Correct the board stack, or explicitly remove and replace this specification; its saved data has not been changed."
                ) from error
        super().__init__(
            parent,
            title="Impedance specification",
            size=(1248, 740),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._window_id = self.GetId()
        self._closing = False
        self.snapshot = snapshot
        self.verify_snapshot = verify_snapshot
        self.preview_callback = preview
        self._capture_prepare_timer = wx.Timer(self)
        self._capture_closed = False
        self._capture_generation = 0
        self._capture_context: Optional[tuple[str, str, tuple[str, ...]]] = None
        self._capture_cache: OrderedDict[Section, CapturedImage] = OrderedDict()
        self._capture_cache_bytes = 0
        self._capture_hashes: dict[Section, str] = {}
        self._capture_failed: set[Section] = set()
        self._capture_display_failed: set[Section] = set()
        self._capture_errors: dict[Section, str] = {}
        self._capture_retry_at: dict[Section, float] = {}
        self._capture_retry_attempts: dict[Section, int] = {}
        self._capture_failed_layers: set[str] = set()
        self._capture_sections: dict[tuple[str, str, str], tuple[Section, ...]] = {}
        self._capture_restore_layers: set[str] = set()
        self._capture_first_paint = False
        self.skip_stub_review = skip_stub_review or (lambda: True)
        self.original = specification
        self.stackup = stackup
        self.width_results = width_results
        self._preserve_approved_width_results = preserve_approved_width_results
        self.calculate_width_callback = calculate_width
        self.load_calculator_config = load_calculator_config
        self.save_calculator_config = save_calculator_config
        self.save_width_result = save_width_result
        self._draft_calc_generation = 0
        self._draft_calc_digest = ""
        self._draft_calc_cancel: Optional[Event] = None
        self._draft_calc_timer = wx.Timer(self)
        self._draft_slow_timer = wx.Timer(self)
        self._draft_retry_timer = wx.Timer(self)
        self._draft_calc_alert = ""
        self._draft_calc_inflight = False
        self._draft_calc_started_at = 0.0
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
        self._linked_layer_groups: tuple[tuple[str, ...], ...] = ()
        self._independent_layers: set[str] = set()
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
        left_panel = wx.Panel(self)
        left = wx.BoxSizer(wx.VERTICAL)
        self.scroll_hint = wx.StaticText(
            left_panel, label="Settings — scroll for more ↓"
        )
        left.Add(self.scroll_hint, 0, wx.BOTTOM, 6)
        self.form = wx.ScrolledWindow(left_panel, style=wx.VSCROLL | wx.BORDER_SIMPLE)
        self.form.SetScrollRate(0, 12)
        fields = wx.FlexGridSizer(cols=2, vgap=8, hgap=12)
        fields.AddGrowableCol(1, 1)
        self._net_class_names = list(snapshot.net_classes)
        self.net_class = wx.Choice(
            self.form, choices=["Choose a net class…"] + self._net_class_names
        )
        self.net_class.SetSelection(0)
        self.class_hint = wx.StaticText(self.form, label="")
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
        self.width_comparison = wx.Panel(self.form)
        width_comparison_layout = wx.BoxSizer(wx.VERTICAL)
        self.width_rows = wx.FlexGridSizer(cols=3, vgap=2, hgap=8)
        width_comparison_layout.Add(self.width_rows, 0, wx.EXPAND)
        self.width_hint = wx.StaticText(
            self.width_comparison, label="Select a net class and signal layer."
        )
        self._width_hint_text_colour = self.width_hint.GetForegroundColour()
        width_comparison_layout.Add(self.width_hint, 0, wx.EXPAND | wx.TOP, 4)
        self.width_comparison.SetSizer(width_comparison_layout)
        self.approval_hint = wx.StaticText(self.form, label="Not recorded")
        self.references = wx.CheckListBox(self.form, choices=list(self.layers))
        self.references.SetMinSize((-1, 104))
        self.adjacent_button = wx.Button(self.form, label="Use adjacent layers")
        self.reference_hint = wx.StaticText(self.form, label=REFERENCE_HELP)
        reference_fields = wx.BoxSizer(wx.VERTICAL)
        reference_fields.Add(self.references, 0, wx.EXPAND)
        reference_fields.Add(self.adjacent_button, 0, wx.TOP, 6)
        reference_fields.Add(self.reference_hint, 0, wx.TOP | wx.EXPAND, 6)
        self.spacing = wx.TextCtrl(self.form)
        self.ground_gap = wx.TextCtrl(self.form)
        self.link_hint = wx.StaticText(self.form, label="")
        self.independent_layer = wx.CheckBox(
            self.form, label="Use separate settings for this layer"
        )
        linked_fields = wx.BoxSizer(wx.VERTICAL)
        linked_fields.Add(self.link_hint, 0, wx.EXPAND)
        linked_fields.Add(self.independent_layer, 0, wx.TOP, 4)
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
            ("linked_settings", "Linked settings", linked_fields),
            ("references", "Reference copper layers (B)", reference_fields),
            ("units", "Dimension units", self.units),
            ("width_hint", "Width comparison", self.width_comparison),
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
            # Multi-line width comparisons must top-align or they paint over the
            # spacing and ground-gap rows once the label grows.
            align = (
                wx.ALIGN_TOP | wx.TOP
                if name in ("width_hint", "class_hint", "layer_hint", "references")
                else wx.ALIGN_CENTER_VERTICAL
            )
            top = (
                3
                if name in ("width_hint", "class_hint", "layer_hint", "references")
                else 0
            )
            fields.Add(field_label, 0, align, top)
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
        self.footer = wx.Panel(left_panel)
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
        footer_layout.Add(self.validation_message, 0, wx.ALL | wx.EXPAND, 8)
        self.calculator_link = wx.adv.HyperlinkCtrl(
            self.footer, wx.ID_ANY, CALCULATOR_LINK_LABEL, CALCULATOR_URL
        )
        self.calculator_link.Hide()
        footer_layout.Add(self.calculator_link, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.approve_layer = wx.Button(self.footer, label="Approve image")
        footer_layout.Add(self.approve_layer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.footer.SetSizer(footer_layout)
        left.Add(self.footer, 0, wx.TOP | wx.EXPAND, 8)
        left_panel.SetSizer(left)
        left_panel.SetMinSize((480, -1))
        self.left_panel = left_panel
        content.Add(left_panel, 3, wx.RIGHT | wx.EXPAND, 12)
        from .dialog_preview import WorkbookPreview

        preview_panel = wx.Panel(self)
        preview_layout = wx.BoxSizer(wx.VERTICAL)
        self.image_list = wx.ListCtrl(
            preview_panel, style=wx.LC_REPORT | wx.LC_NO_HEADER | wx.LC_SINGLE_SEL
        )
        self.image_list.InsertColumn(0, "Review", width=35)
        self.image_list.InsertColumn(1, "Workbook image", width=570)
        self.image_list.SetMinSize((1, 146))
        self.image_list.SetName("Workbook image review checklist")
        preview_layout.Add(self.image_list, 0, wx.BOTTOM | wx.EXPAND, 6)
        self.preview_pane = WorkbookPreview(
            preview_panel,
            self._prepared_preview if preview is not None else None,
            verify_current=self._verify_current_snapshot,
            appearance_context=appearance_context,
            appearance_changed=self._invalidate_layer_approvals,
            review_failed=self._invalidate_current_layer_approval,
            image_viewed=self._on_image_viewed,
            approved_images=self._approved_image_ids,
            image_list=self.image_list,
            skip_stub_review=skip_stub_review,
            active=lambda: bool(self) and not self._capture_closed,
        )
        preview_layout.Add(self.preview_pane.window, 1, wx.EXPAND)
        preview_panel.SetSizer(preview_layout)
        preview_panel.SetMinSize((420, -1))
        content.Add(preview_panel, 1, wx.EXPAND)
        root.Add(content, 1, wx.ALL | wx.EXPAND, 12)
        root.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL | wx.EXPAND, 12)
        self.FindWindow(wx.ID_OK).SetLabel("Apply settings")
        self.SetSizer(root)
        self.SetMinSize((1176, 680))
        self.net_class.Bind(wx.EVT_CHOICE, self._on_class)
        self.include_layer.Bind(wx.EVT_CHECKBOX, self._on_include_layer)
        self.independent_layer.Bind(wx.EVT_CHECKBOX, self._on_independent_layer)
        self.approve_layer.Bind(wx.EVT_BUTTON, self._on_approve_layer)
        self.target.Bind(wx.EVT_TEXT, self._on_target)
        self.spacing.Bind(wx.EVT_TEXT, self._on_layer_field)
        self.ground_gap.Bind(wx.EVT_TEXT, self._on_layer_field)
        self.layer.Bind(wx.EVT_CHOICE, self._on_layer)
        self.references.Bind(wx.EVT_CHECKLISTBOX, self._on_references)
        self.adjacent_button.Bind(wx.EVT_BUTTON, self._on_adjacent)
        self.kind.Bind(wx.EVT_CHOICE, self._on_kind)
        self.units.Bind(wx.EVT_CHOICE, self._on_units)
        left_panel.Bind(wx.EVT_SIZE, self._on_left_panel_size)
        self.Bind(wx.EVT_BUTTON, self._on_save, id=wx.ID_OK)
        self.Bind(wx.EVT_BUTTON, self._on_spec_cancel, id=wx.ID_CANCEL)
        self.Bind(wx.EVT_CLOSE, self._on_spec_cancel)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_spec_destroy)
        self.Bind(wx.EVT_SHOW, self._on_show)
        self.Bind(wx.EVT_TIMER, self._on_draft_calc_timer, self._draft_calc_timer)
        self.Bind(wx.EVT_TIMER, self._on_draft_slow_timer, self._draft_slow_timer)
        self.Bind(wx.EVT_TIMER, self._on_draft_retry_timer, self._draft_retry_timer)
        self.Bind(
            wx.EVT_TIMER, self._on_capture_prepare_timer, self._capture_prepare_timer
        )
        self.reference_selection = ReferenceLayerSelection(self.layers, self.layers[0])
        self._populate_references()
        if specification is not None:
            self._load(specification)
        else:
            self._update_controls()
        self._on_kind(None)
        self._update_field_indicators()
        self._rewrap_form_value_texts()
        self.Layout()
        self.CentreOnParent()

    def _form_value_wrap_width(self) -> int:
        """Return a wrap budget for text in the form's value column."""
        client = self.form.GetClientSize()
        width = client.GetWidth() if hasattr(client, "GetWidth") else int(client[0])
        return max(200, width - 175 - 12 - 20)

    def _wrap_form_value_text(self, control: wx.Window) -> None:
        """Wrap one form/footer StaticText to the current value-column width."""
        control.Wrap(self._form_value_wrap_width())

    def _rewrap_form_value_texts(self) -> None:
        """Keep long membership, width, and validation copy inside the left column."""
        for control in (
            self.class_hint,
            self.layer_hint,
            self.approval_hint,
            self.reference_hint,
            self.width_hint,
            self.link_hint,
            self.validation_message,
        ):
            self._wrap_form_value_text(control)

    def _on_left_panel_size(self, event: wx.SizeEvent) -> None:
        """Rewrap form value text when the left column width changes."""
        self._rewrap_form_value_texts()
        event.Skip()

    def _on_show(self, event: wx.ShowEvent) -> None:
        """Resume a preview whose initial queued capture ran before native show."""
        if event.GetEventObject() is self and event.IsShown():
            wx.CallAfter(self.preview_pane.show_selected)
            wx.CallAfter(self._schedule_draft_width_calculation)
            self._schedule_capture_preparation()
        event.Skip()

    def _on_spec_cancel(self, event: wx.Event) -> None:
        """Discard draft calculation before native cancel/close completes."""
        self._cancel_draft_calculation()
        self._stop_capture_preparation()
        event.Skip()

    def _on_spec_destroy(self, event: wx.WindowDestroyEvent) -> None:
        """Stop owned draft calculation if destruction bypasses Cancel."""
        if event.GetId() == self._window_id:
            self._cancel_draft_calculation()
            self._stop_capture_preparation()
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
        """Load dimensions; saved approvals remain pending until post-show capture checks."""
        self._independent_layers = set(specification.independent_layers)
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
            if settings.layer in self.layers
        }
        for layer in specification.excluded_layers:
            if layer not in self.layers:
                continue
            self._layer_drafts[layer] = _LayerDraft(
                adjacent_reference_layers(self.layers, layer),
                included=False,
                confirmed=True,
            )
        self._capture_restore_layers = {
            settings.layer
            for settings in specification.layer_settings
            if settings.layer in self._layer_drafts
            and tracking.find_layer(self.review_tracking, self._spec_id, settings.layer)
            is not None
        }

    def _sections_for_layer(self, layer: str) -> tuple[Section, ...]:
        """Reuse immutable matching geometry across field edits and preparation ticks."""
        spec = self._selection_specification()
        key = (spec.net_class, spec.kind, layer)
        if key not in self._capture_sections:
            self._capture_sections[key] = preview_sections(spec, self.snapshot, layer)
        return self._capture_sections[key]

    def _stop_capture_preparation(self) -> None:
        """Invalidate queued native work before modal close or destruction."""
        self._capture_closed = True
        self._capture_generation += 1
        self._capture_prepare_timer.Stop()
        self.preview_pane.close()
        self._capture_cache.clear()
        self._capture_cache_bytes = 0

    def _schedule_capture_preparation(
        self, delay_ms: int = CAPTURE_PREPARE_INTERVAL_MS
    ) -> None:
        """Prepare one native capture per tick, leaving input and paint turns between."""
        if (
            self
            and not self._capture_closed
            and self.preview_callback is not None
            and self.IsShownOnScreen()
        ):
            self._capture_prepare_timer.StartOnce(
                max(CAPTURE_PREPARE_INTERVAL_MS, delay_ms)
            )

    def _capture_needed(self, section: Section) -> bool:
        """Leave optional covered stubs cold until explicitly selected."""
        return not (
            self.skip_stub_review()
            and section.exclusion_reason == EXCLUSION_COVERED_STUB
        )

    def _on_capture_prepare_timer(self, event: wx.TimerEvent) -> None:
        """Warm current-layer captures first, without recording any display event."""
        if not self or self._capture_closed or not self.IsShownOnScreen():
            return
        generation = self._capture_generation
        # Native plotting and wx bitmaps must stay on the GUI thread. A timer
        # yields between captures, unlike eagerly rendering an entire layer.
        if not self._capture_first_paint:
            if self.preview_pane.sections and not self.preview_pane.failure:
                self._schedule_capture_preparation()
                return
        if (
            self.preview_pane.sections
            and not self.preview_pane.capture_loaded
            and not self.preview_pane.failure
        ):
            self._schedule_capture_preparation()
            return
        layers = [self._active_layer] + [
            layer for layer in self._layer_names if layer != self._active_layer
        ]
        now = time.monotonic()
        retry_due: list[float] = []
        for layer in layers:
            draft = self._layer_drafts.get(layer)
            if (
                draft is None
                or not draft.included
                or layer in self._capture_failed_layers
            ):
                continue
            try:
                sections = self._sections_for_layer(layer)
            except (ValueError, InvalidOperation):
                self._capture_failed_layers.add(layer)
                self._capture_restore_layers.discard(layer)
                continue
            for section in sections:
                if (
                    section == self.preview_pane.selected_section
                    and self.preview_pane.failure
                ):
                    # The preview owns retries for its selected image, including
                    # failures during decoding or actual visible painting.
                    continue
                due = self._capture_retry_at.get(section)
                if due is not None and due > now:
                    retry_due.append(due)
                    continue
                if (
                    self._capture_needed(section) or section in self._capture_failed
                ) and section not in self._capture_hashes:
                    try:
                        self._prepared_preview(
                            section, refresh=section in self._capture_failed
                        )
                    except Exception as error:
                        if (
                            not self
                            or self._capture_closed
                            or generation != self._capture_generation
                        ):
                            return
                        self._capture_failed.add(section)
                        self._capture_errors[section] = str(error)
                        attempt = self._capture_retry_attempts.get(section, 0) + 1
                        self._capture_retry_attempts[section] = attempt
                        self._capture_retry_at[section] = (
                            time.monotonic() + retry_delay_ms(attempt) / 1000
                        )
                        current = self._layer_drafts.get(layer)
                        if current is not None:
                            self._layer_drafts[layer] = replace(
                                current, confirmed=False
                            )
                        self._update_layer_labels()
                        self._update_capture_retry_status()
                    self._schedule_capture_preparation()
                    return
            self._restore_layer_approval(layer, sections)
        if retry_due:
            self._schedule_capture_preparation(
                max(1, round((min(retry_due) - time.monotonic()) * 1000))
            )

    def _prepared_preview(
        self, section: Section, *, refresh: bool = False
    ) -> CapturedImage:
        """Reuse prepared pixels only after checking this editor's board and appearance."""
        if not self or self._capture_closed or self.preview_callback is None:
            raise ValueError("The impedance specification is closed.")
        generation = self._capture_generation
        if refresh:
            previous = self._capture_cache.pop(section, None)
            if previous is not None:
                self._capture_cache_bytes -= len(previous.data)
            self._capture_hashes.pop(section, None)
        capture = self._capture_cache.get(section)
        if capture is None:
            capture = self.preview_callback(section, refresh=refresh)
        if not self or self._capture_closed or generation != self._capture_generation:
            raise ValueError(
                "The capture inputs changed. Select the current image again."
            )
        # Validate returned and cached pixels against this editor's expected
        # snapshot and current palette before installing or accepting them.
        self._verify_current_snapshot()
        self.preview_pane.ensure_appearance_current()
        if not self or self._capture_closed or generation != self._capture_generation:
            raise ValueError(
                "The capture inputs changed. Select the current image again."
            )
        if section not in self._capture_cache:
            while (
                self._capture_cache
                and self._capture_cache_bytes + len(capture.data) > CAPTURE_CACHE_BYTES
            ):
                _, evicted = self._capture_cache.popitem(last=False)
                self._capture_cache_bytes -= len(evicted.data)
            self._capture_cache[section] = capture
            self._capture_cache_bytes += len(capture.data)
        self._capture_cache.move_to_end(section)
        self._capture_hashes[section] = capture.sha256
        if section not in self._capture_display_failed:
            self._capture_failed.discard(section)
            self._capture_errors.pop(section, None)
        self._capture_retry_at.pop(section, None)
        self._capture_retry_attempts.pop(section, None)
        self._restore_layer_approval(
            section.layer, self._sections_for_layer(section.layer)
        )
        self._restore_viewed_captures(
            self._selection_specification(), self._active_layer
        )
        self._update_capture_retry_status()
        self._schedule_capture_preparation()
        return capture

    def _restore_layer_approval(
        self, layer: str, sections: tuple[Section, ...]
    ) -> None:
        """Restore a historical approval only after every required current PNG matches."""
        if (
            not self
            or self._capture_closed
            or layer not in self._capture_restore_layers
        ):
            return
        generation = self._capture_generation
        required = tuple(
            section for section in sections if self._capture_needed(section)
        )
        if any(section not in self._capture_hashes for section in required):
            return
        if any(section in self._capture_display_failed for section in sections):
            return
        self._capture_restore_layers.discard(layer)
        if not required:
            # An all-optional layer can have a real approval with no required
            # PNGs. Verify before reading settings: native callbacks can deliver
            # edits, close the window or replace the current capture context.
            try:
                self._verify_current_snapshot()
                self.preview_pane.ensure_appearance_current()
            except (ValueError, InvalidOperation):
                return
            if (
                not self
                or self._capture_closed
                or generation != self._capture_generation
            ):
                return
        draft = self._layer_drafts.get(layer)
        if draft is None:
            return
        settings, errors = self._validated_fields(layer, draft, selection_errors=False)
        current = False
        if not errors and settings is not None:
            spec = replace(self._selection_specification(), layer_settings=(settings,))
            digests = {
                section.section_id: tracking.capture_fingerprint(
                    self._source_digest, spec, section, self._capture_hashes[section]
                )
                for section in required
            }
            try:
                current = tracking.layer_approval_current(
                    self.review_tracking,
                    self._source_digest,
                    spec,
                    layer,
                    sections,
                    stackup=self.stackup,
                    width_results=self.width_results,
                    capture_digests=digests,
                )
                approved = tracking.find_layer(
                    self.review_tracking, self._spec_id, layer
                )
                current = (
                    current
                    and approved is not None
                    and set(digests) <= dict(approved.captures).keys()
                )
            except (ValueError, InvalidOperation):
                current = False
        if not self or self._capture_closed or generation != self._capture_generation:
            return
        self._layer_drafts[layer] = replace(draft, confirmed=current)
        self._update_layer_labels()

    @property
    def capture_digests(self) -> dict[str, Optional[str]]:
        """Export verified pixel evidence separately from historical review events."""
        spec = self._selection_specification()
        digests: dict[str, Optional[str]] = {
            section.section_id: tracking.capture_fingerprint(
                self._source_digest, spec, section, digest
            )
            for section, digest in self._capture_hashes.items()
        }
        digests.update((section.section_id, None) for section in self._capture_failed)
        return digests

    def _signal_layer(self) -> str:
        """Keep annotated dropdown labels separate from canonical copper-layer names."""
        return self._active_layer

    def _update_capture_retry_status(self) -> None:
        """Keep background recovery visible while another image is selected."""
        if not self or self._capture_closed:
            return
        selected = self.preview_pane.selected_section
        failures = [
            (section, error)
            for section, error in self._capture_errors.items()
            if section != selected
            and section.layer in self._layer_drafts
            and self._layer_drafts[section.layer].included
        ]
        message = ""
        if failures:
            section, error = failures[0]
            state = (
                "Awaiting successful display."
                if section in self._capture_display_failed
                and section in self._capture_hashes
                else "Retrying automatically."
            )
            message = f"Preview on {section.layer} ({', '.join(section.net_names)}): {error} {state}"
            if len(failures) > 1:
                message += f" {len(failures) - 1} other previews are awaiting recovery."
        self.preview_pane.set_background_status(message)

    def _layer_status(self, layer: str) -> str:
        """Describe addressed exclusions distinctly from visual approval."""
        draft = self._layer_drafts[layer]
        if not draft.included:
            return "excluded"
        if draft.included:
            _, errors = self._validated_fields(layer, draft, selection_errors=False)
            if errors:
                return "needs configuration"
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
        self._wrap_form_value_text(self.approval_hint)
        self.form.Layout()
        self.form.FitInside()

    def _on_image_viewed(
        self, section: Section, image_sha256: str, viewed_at_utc: str
    ) -> None:
        """Keep successful display events private until both dialogs are accepted."""
        self._capture_first_paint = True
        self._capture_display_failed.discard(section)
        self._capture_failed.discard(section)
        self._capture_errors.pop(section, None)
        self._capture_retry_at.pop(section, None)
        self._capture_retry_attempts.pop(section, None)
        digest = tracking.capture_fingerprint(
            self._source_digest,
            self._selection_specification(),
            section,
            image_sha256,
        )
        approved = tracking.find_layer(
            self.review_tracking, self._spec_id, section.layer
        )
        if approved is not None:
            previous_digest = dict(approved.captures).get(section.section_id)
            if previous_digest is not None and previous_digest != digest:
                self._invalidate_current_layer_approval(failed=False)
        self.review_tracking = tracking.put_image(
            self.review_tracking,
            tracking.ImageView(
                self._spec_id, section.layer, section.section_id, digest, viewed_at_utc
            ),
        )
        self._restore_layer_approval(
            section.layer, self._sections_for_layer(section.layer)
        )
        self._update_capture_retry_status()

    def _approved_image_ids(self) -> set[str]:
        """Match explicit approvals against current settings and viewed pixels."""
        if not self._active_layer or not self.preview_pane.sections:
            return set()
        settings, errors = self._validated_fields(
            self._active_layer, self._capture_draft()
        )
        if errors or settings is None:
            return set()
        spec = replace(self._selection_specification(), layer_settings=(settings,))
        previous = tracking.find_layer(
            self.review_tracking, self._spec_id, self._active_layer
        )
        if previous is None or previous.settings_digest != tracking.layer_fingerprint(
            self._source_digest,
            spec,
            self._active_layer,
            self.preview_pane.sections,
            stackup=self.stackup,
            width_results=self.width_results,
        ):
            return set()
        approved = dict(previous.captures)
        return {
            section.section_id
            for section in self.preview_pane.sections
            if (image_sha256 := self.preview_pane.image_digest(section)) is not None
            and approved.get(section.section_id)
            == tracking.capture_fingerprint(
                self._source_digest, spec, section, image_sha256
            )
        }

    def _record_image_approval(self, spec: Specification, layer: str) -> None:
        """Approve only the selected image after its successful visible paint."""
        section = self.preview_pane.selected_section
        if section is None or not self.preview_pane.displayed:
            raise ValueError(
                "Wait for this workbook image to display successfully, then choose Approve image."
            )
        image_sha256 = self.preview_pane.image_digest(section)
        if image_sha256 is None:
            raise ValueError("No displayed workbook image is available for approval.")
        digest = tracking.layer_fingerprint(
            self._source_digest,
            spec,
            layer,
            self.preview_pane.sections,
            stackup=self.stackup,
            width_results=self.width_results,
        )
        previous = tracking.find_layer(self.review_tracking, self._spec_id, layer)
        captures = {
            section_id: capture_digest
            for section_id, capture_digest in (
                previous.captures
                if previous is not None and previous.settings_digest == digest
                else ()
            )
            if (
                viewed := tracking.find_image(
                    self.review_tracking, self._spec_id, layer, section_id
                )
            )
            is not None
            and viewed.capture_digest == capture_digest
        }
        captures[section.section_id] = tracking.capture_fingerprint(
            self._source_digest, spec, section, image_sha256
        )
        approved_captures = tuple(sorted(captures.items()))
        if (
            previous is not None
            and previous.settings_digest == digest
            and previous.captures == approved_captures
        ):
            return
        self.review_tracking = tracking.put_layer(
            self.review_tracking,
            tracking.LayerApproval(
                self._spec_id, layer, digest, tracking.utc_now(), approved_captures
            ),
        )

    def _capture_draft(self, confirmed: bool = False) -> _LayerDraft:
        """Keep raw invalid input safely, independently of the newly selected layer."""
        pair, ground = dimension_fields_for_kind(
            KIND_CHOICES[self.kind.GetSelection()][0]
        )
        previous = self._layer_drafts.get(self._active_layer)
        spacing = self.spacing.GetValue()
        ground_gap = self.ground_gap.GetValue()
        if not pair or _is_inactive_dimension(spacing):
            spacing = previous.spacing if previous is not None else ""
            if _is_inactive_dimension(spacing):
                spacing = ""
        if not ground or _is_inactive_dimension(ground_gap):
            ground_gap = previous.ground_gap if previous is not None else ""
            if _is_inactive_dimension(ground_gap):
                ground_gap = ""
        return _LayerDraft(
            self.reference_selection.selected,
            automatic=self.reference_selection.automatic,
            spacing=spacing,
            ground_gap=ground_gap,
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
        self._linked_layer_groups = ()
        self._independent_layers = set()
        self._refresh_class_layers()

    def _class_traces(self) -> tuple[Trace, ...]:
        """Use exact constituent membership and ignore unassigned copper."""
        names = {
            net
            for net, classes in self.snapshot.net_class_memberships
            if net and self._active_class in classes
        }
        return tuple(trace for trace in self.snapshot.traces if trace.net in names)

    def _eligible_link_groups(self) -> tuple[tuple[str, ...], ...]:
        """Find equivalent routed constructions without guessing absent stackup data."""
        from .layer_equivalence import equivalent_layer_groups

        return equivalent_layer_groups(
            self.stackup,
            tuple(layer for layer in self.layers if layer in self._layer_drafts),
            {
                layer: draft.references or adjacent_reference_layers(self.layers, layer)
                for layer, draft in self._layer_drafts.items()
            },
        )

    def _draft_dimensions(self, draft: _LayerDraft) -> tuple[Optional[int], ...]:
        """Compare physical dimensions independently of layer display units."""
        pair, ground = dimension_fields_for_kind(
            KIND_CHOICES[self.kind.GetSelection()][0]
        )
        return tuple(
            self._read_dimension(value, draft.units)
            if required and value.strip()
            else None
            for value, required in ((draft.spacing, pair), (draft.ground_gap, ground))
        )

    def _rebuild_layer_links(self) -> None:
        """Restore eligible links while preserving all existing divergent settings."""
        from .layer_equivalence import corresponding_references

        groups = []
        for eligible in self._eligible_link_groups():
            members = tuple(
                layer
                for layer in eligible
                if layer not in self._independent_layers
                and self._layer_drafts[layer].included
            )
            if len(members) < 2:
                continue
            previously_linked = any(
                set(members) <= set(group) for group in self._linked_layer_groups
            )
            try:
                dimensions = {
                    self._draft_dimensions(self._layer_drafts[layer])
                    for layer in members
                }
            except ValueError:
                dimensions = set()
            source = self._layer_drafts[members[0]]
            references_match = all(
                (not source.references and not self._layer_drafts[layer].references)
                or (
                    bool(source.references)
                    and self.stackup is not None
                    and corresponding_references(
                        self.stackup, members[0], layer, source.references
                    )
                    == self._layer_drafts[layer].references
                )
                for layer in members[1:]
            )
            if references_match and (previously_linked or len(dimensions) == 1):
                groups.append(members)
            else:
                self._independent_layers.update(members)
        self._linked_layer_groups = tuple(groups)

    def _update_link_controls(self) -> None:
        """Explain automatic linkage and offer an explicit per-layer override."""
        group = next(
            (
                group
                for group in self._linked_layer_groups
                if self._active_layer in group
            ),
            (),
        )
        peers = tuple(layer for layer in group if layer != self._active_layer)
        self.link_hint.SetLabel(
            "Settings linked with "
            + ", ".join(peers)
            + ". References map to each layer."
            if peers
            else "Settings apply to this layer only."
        )
        self._wrap_form_value_text(self.link_hint)
        self.independent_layer.SetValue(self._active_layer in self._independent_layers)
        eligible = any(
            self._active_layer in group for group in self._eligible_link_groups()
        )
        self.independent_layer.Enable(
            bool(self._active_layer)
            and (eligible or self._active_layer in self._independent_layers)
        )
        self.independent_layer.SetToolTip(
            "Uncheck to apply this layer's settings to its equivalent layers."
        )

    def _on_independent_layer(self, event: wx.CommandEvent) -> None:
        """Detach one layer, or explicitly rejoin using its current settings."""
        if not self._active_layer:
            return
        self._stash_class_layer()
        if self.independent_layer.GetValue():
            self._independent_layers.add(self._active_layer)
            self._rebuild_layer_links()
        else:
            eligible = next(
                (
                    group
                    for group in self._eligible_link_groups()
                    if self._active_layer in group
                ),
                (),
            )
            self._independent_layers.difference_update(eligible)
            self._linked_layer_groups = tuple(
                group
                for group in self._linked_layer_groups
                if not set(group).intersection(eligible)
            )
            members = tuple(
                layer for layer in eligible if self._layer_drafts[layer].included
            )
            if len(members) > 1:
                self._linked_layer_groups += (members,)
                self._synchronize_layer_settings(None)
        self._update_controls()

    def _synchronize_layer_settings(
        self, previous: Optional[_LayerDraft], *, source_layer: Optional[str] = None
    ) -> None:
        """Copy physical edits, not approvals, to equivalent layer arrangements."""
        from .layer_equivalence import corresponding_references

        source_layer = source_layer or self._active_layer
        source = self._layer_drafts[source_layer]
        group = next(
            (group for group in self._linked_layer_groups if source_layer in group),
            (),
        )
        if not group or self.stackup is None:
            return
        pair, ground = dimension_fields_for_kind(
            KIND_CHOICES[self.kind.GetSelection()][0]
        )
        references_changed = (
            previous is None or previous.references != source.references
        )
        for layer in group:
            if layer == source_layer:
                continue
            peer = self._layer_drafts[layer]
            changes: dict[str, object] = {}
            if references_changed:
                references = (
                    corresponding_references(
                        self.stackup, source_layer, layer, source.references
                    )
                    if source.references
                    else ()
                )
                if references is None:
                    self._independent_layers.update(group)
                    self._rebuild_layer_links()
                    return
                if references != peer.references:
                    changes.update(references=references, automatic=source.automatic)
            for field, required in (("spacing", pair), ("ground_gap", ground)):
                if not required:
                    continue
                value = getattr(source, field).strip()
                try:
                    dimension = (
                        self._read_dimension(value, source.units) if value else None
                    )
                except ValueError:
                    # Keep malformed text private to the edited field until fixed.
                    continue
                old_value = getattr(peer, field).strip()
                try:
                    peer_dimension = (
                        self._read_dimension(old_value, peer.units)
                        if old_value
                        else None
                    )
                except ValueError:
                    peer_dimension = -1
                if dimension != peer_dimension:
                    text = (
                        format_distance(dimension, peer.units)
                        if dimension is not None
                        else ""
                    )
                    if dimension is not None:
                        self._exact_dimensions[text, peer.units] = dimension
                    changes[field] = text
            if changes:
                self._layer_drafts[layer] = replace(peer, **changes, confirmed=False)

    def _refresh_class_layers(self) -> None:
        """Discover routed layers and surface newly encountered layers as unreviewed."""
        traces = self._class_traces()
        routed = {trace.layer for trace in traces}
        # Saved layers without current class copper are not editable signal
        # layers. Keep the original immutable specification until Apply/Save.
        self._layer_drafts = {
            layer: draft
            for layer, draft in self._layer_drafts.items()
            if layer in routed
        }
        self._independent_layers.intersection_update(routed)
        for layer in routed:
            if layer not in self._layer_drafts:
                self._layer_drafts[layer] = _LayerDraft(
                    adjacent_reference_layers(self.layers, layer)
                )
        self._rebuild_layer_links()
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
        self._update_link_controls()
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
        self._wrap_form_value_text(self.class_hint)
        self.layer_hint.SetLabel(summary)
        self._wrap_form_value_text(self.layer_hint)
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
        self._rebuild_layer_links()
        self._update_controls()

    def _on_layer_field(self, event: Optional[wx.CommandEvent]) -> None:
        """Mark genuinely edited class-layer fields unreviewed without reacting to loading."""
        if not self._setting_fields and self._active_layer:
            self._clear_validation()
            previous = self._layer_drafts.get(self._active_layer)
            self._stash_class_layer()
            self._synchronize_layer_settings(previous)
            self._update_controls()
        elif not self._setting_fields:
            self._clear_validation()
            self._update_field_indicators()

    def _validated_fields(
        self,
        layer: str,
        draft: _LayerDraft,
        *,
        allow_incomplete: bool = False,
        selection_errors: bool = True,
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
            if _is_inactive_dimension(value):
                value = ""
            if not value.strip():
                if not allow_incomplete:
                    errors[name] = "Required"
                continue
            try:
                if name == "target":
                    positive_decimal(value.strip(), "desired impedance")
                else:
                    dimensions[name] = self._read_dimension(value, draft.units)
            except (ValueError, InvalidOperation):
                errors[name] = "Invalid"
        if included and not draft.references and not allow_incomplete:
            errors["references"] = "Required"
        if not self._active_class:
            errors["net_class"] = "Required"
        elif not layer:
            errors["layer"] = "Required"
        if selection_errors and self._selection_error_field:
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

    def _draft_settings(
        self, layer: str, draft: _LayerDraft, *, allow_incomplete: bool = False
    ) -> LayerSettings:
        """Report the first actionable form error without changing raw draft values."""
        settings, errors = self._validated_fields(
            layer,
            draft,
            allow_incomplete=allow_incomplete,
            selection_errors=not allow_incomplete,
        )
        if settings is None:
            field = next(iter(errors), "layer")
            if layer != self._active_layer:
                self._active_layer = layer
                self._show_draft(layer, draft)
                self._update_controls()
            self._validation_control = getattr(self, field)
            label = self._field_label_defaults[field][0]
            raise ValueError(
                f"{layer}: {label} — {errors.get(field, 'Required').lower()}."
            )
        return settings

    def _review_current_images(self) -> bool:
        """Present the next image still needing an explicit approval click."""
        self._validation_control = self.preview_pane.checklist
        for index, section in enumerate(self.preview_pane.sections):
            if section in self._capture_display_failed:
                if section != self.preview_pane.selected_section:
                    self.preview_pane.select(index)
                self.validation_message.SetLabel(
                    f"Image {index + 1} must display successfully before this layer can be approved. Failed previews retry automatically."
                )
                self._wrap_form_value_text(self.validation_message)
                self.Layout()
                return False
        if self.preview_pane.advance_to_unreviewed(self._approved_image_ids()):
            self.validation_message.SetLabel(
                "Review this image, then choose Approve image."
            )
            self._wrap_form_value_text(self.validation_message)
            self.Layout()
            return False
        self.preview_pane.ensure_reviewed()
        return True

    def _confirm_current_layer(self, *, approve_image: bool = False) -> bool:
        """Confirm a layer after each required image has an explicit approval."""
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
            selection = replace(
                self._selection_specification(), layer_settings=(settings,)
            )
            if approve_image:
                self._record_image_approval(selection, self._active_layer)
            if not self._review_current_images():
                return False
        draft.confirmed = True
        self._layer_drafts[self._active_layer] = draft
        return True

    def _advance_to_pending_layer(self) -> bool:
        """Navigate to the next unaddressed layer without approving its first image."""
        routed = {trace.layer for trace in self._class_traces()}
        pending = {
            layer
            for layer in self.layers
            if layer in routed
            and self._layer_drafts[layer].included
            and not self._layer_drafts[layer].confirmed
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
        self._wrap_form_value_text(self.validation_message)
        self.Layout()
        return True

    def _on_approve_layer(self, event: wx.CommandEvent) -> None:
        """Approve the displayed image and advance to the next pending image."""
        try:
            self._clear_validation()
            if not self._confirm_current_layer(approve_image=True):
                return
            self._update_controls()
            if not self._advance_to_pending_layer():
                self.validation_message.SetLabel(
                    "All signal layers are approved or excluded. Choose Apply settings to return to the configuration window."
                )
                self._wrap_form_value_text(self.validation_message)
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

    def _update_width_comparison(self, *, schedule: bool = True) -> None:
        """Compare one actual width for the active draft layer with its nominal."""
        if self._closing or not self:
            return
        spec, _digest = self._draft_calculation_input()
        from .width_checks import (
            WidthComparisonRow,
            format_width_nm,
            select_comparison_width_nm,
            width_check,
            width_comparison_rows,
        )

        layer_widths = [
            trace.width_nm
            for trace in self._class_traces()
            if trace.layer == self._active_layer
        ]
        distinct = sorted(set(layer_widths))
        if not distinct:
            self._set_width_comparison(
                message="No routed widths on the selected signal layer."
            )
            self._set_calculator_link_visible(False)
            return
        if not self.include_layer.GetValue():
            self._set_width_comparison(
                message=(
                    "Actual: "
                    + ", ".join(format_width_nm(width) for width in distinct)
                    + "\nExcluded from the workbook and nominal-width calculation."
                )
            )
            self._set_calculator_link_visible(False)
            return
        if spec is None:
            self._set_width_comparison(
                message=(
                    "Actual: "
                    + ", ".join(format_width_nm(width) for width in distinct)
                    + "\nComplete this layer's settings to compare calculated widths."
                )
            )
            self._set_calculator_link_visible(False)
            return
        config = Config(
            specifications=(spec,),
            stackup=self.stackup,
            width_results=self.width_results,
        )
        # Learn the current nominal without publishing one comparison per neckdown.
        probe = width_check(config, spec, self._active_layer, distinct[0])
        target = (
            probe.target_width_nm
            if probe.result_current and probe.target_width_nm is not None
            else None
        )
        actual = select_comparison_width_nm(layer_widths, target)
        check = width_check(config, spec, self._active_layer, actual)
        rows: list[WidthComparisonRow] = []
        messages: list[str] = []
        if (
            check.result_current
            and check.target_width_nm is not None
            and check.delta_nm is not None
        ):
            rows.extend(
                width_comparison_rows(actual, check.target_width_nm, check.delta_nm)
            )
            difference_delta_nm = check.delta_nm
        else:
            messages.append(
                "Actual: "
                + ", ".join(format_width_nm(width) for width in distinct)
                + "\n"
                + check.message
            )
            difference_delta_nm = None
        others = [width for width in distinct if width != actual]
        if others:
            messages.append(
                "Other actual widths on this layer: "
                + ", ".join(format_width_nm(width) for width in others)
                + "."
            )
        if check.model:
            messages.append("Calculation model: " + check.model)
            messages.extend(check.assumptions)
        if self._draft_calc_alert and self._draft_calc_alert not in messages:
            messages.append(self._draft_calc_alert)
        self._set_width_comparison(
            rows=rows,
            message="\n\n".join(messages),
            difference_delta_nm=difference_delta_nm,
            unavailable=(
                check.result_current
                and check.result_status in {"unavailable", "unsupported", "error"}
            ),
        )
        self._set_calculator_link_visible(bool(self._draft_calc_alert))
        if schedule:
            self._schedule_draft_width_calculation()

    def _set_width_comparison(
        self,
        *,
        rows: Optional[list] = None,
        message: str = "",
        difference_delta_nm: Optional[int] = None,
        unavailable: bool = False,
    ) -> None:
        """Publish comparison columns and grow the form so later fields stay visible."""
        from .width_checks import WidthComparisonRow, difference_text_colour

        self.width_rows.Clear(True)
        for row in rows or ():
            if not isinstance(row, WidthComparisonRow):
                continue
            label = wx.StaticText(self.width_comparison, label=row.label)
            millimetres = wx.StaticText(self.width_comparison, label=row.millimetres)
            mils = wx.StaticText(self.width_comparison, label=row.mils)
            if row.label.startswith("Difference") and difference_delta_nm is not None:
                colour = wx.Colour(*difference_text_colour(difference_delta_nm))
                millimetres.SetForegroundColour(colour)
                mils.SetForegroundColour(colour)
            self.width_rows.Add(label, 0, wx.ALIGN_RIGHT | wx.ALIGN_CENTER_VERTICAL)
            self.width_rows.Add(
                millimetres, 0, wx.ALIGN_LEFT | wx.ALIGN_CENTER_VERTICAL
            )
            self.width_rows.Add(mils, 0, wx.ALIGN_LEFT | wx.ALIGN_CENTER_VERTICAL)
        self.width_hint.SetLabel(message)
        colour = self._width_hint_text_colour
        if unavailable:
            # Follow the native text theme; keep warning text readable on dark forms.
            channels = (
                (colour.Red(), colour.Green(), colour.Blue())
                if hasattr(colour, "Red")
                else colour
                if isinstance(colour, tuple)
                else (0, 0, 0)
            )
            colour = wx.Colour(
                *((255, 200, 80) if sum(channels[:3]) > 384 else (150, 90, 0))
            )
        self.width_hint.SetForegroundColour(colour)
        self.width_hint.Show(bool(message))
        self._wrap_form_value_text(self.width_hint)
        if hasattr(self.width_hint, "InvalidateBestSize"):
            self.width_hint.InvalidateBestSize()
        self.width_comparison.Layout()
        self.form.Layout()
        self.form.FitInside()
        self.Layout()

    def _cancel_draft_calculation(self) -> None:
        """Invalidate queued draft-width work without waiting for the worker."""
        self._closing = True
        self._reset_draft_calculation()

    def _reset_draft_calculation(self) -> None:
        """Discard transient work and alerts without closing or touching controls."""
        self._draft_calc_generation += 1
        self._draft_calc_timer.Stop()
        self._draft_slow_timer.Stop()
        self._draft_retry_timer.Stop()
        self._draft_calc_inflight = False
        self._draft_calc_alert = ""
        if self._draft_calc_cancel is not None:
            self._draft_calc_cancel.set()
            self._draft_calc_cancel = None

    def _draft_calculation_input(self) -> tuple[Optional[Specification], str]:
        """Invalidate obsolete work before displaying or calculating current inputs."""
        from .stackup_model import calculation_fingerprint

        spec = None
        digest = ""
        if self._active_layer and self.include_layer.GetValue():
            settings, errors = self._validated_fields(
                self._active_layer, self._capture_draft()
            )
            if not errors and settings is not None:
                spec = replace(
                    self._selection_specification(), layer_settings=(settings,)
                )
                if self.stackup is not None and self.stackup.calculator_id:
                    try:
                        digest = calculation_fingerprint(
                            self.stackup, spec, self._active_layer
                        )
                    except ValueError:
                        digest = ""
        if digest != self._draft_calc_digest:
            self._reset_draft_calculation()
            self._draft_calc_digest = digest
        return spec, digest

    def _set_calculator_link_visible(self, visible: bool) -> None:
        """Show the vendor calculator only when a delay or failure needs an escape hatch."""
        if visible == self.calculator_link.IsShown():
            return
        self.calculator_link.Show(visible)
        self.footer.Layout()
        self.Layout()

    def _schedule_draft_width_calculation(self) -> None:
        """Debounce a quiet solve for the active draft layer when inputs are ready."""
        if self._closing or not self:
            return
        spec, digest = self._draft_calculation_input()
        if spec is None or not digest or self._draft_calc_inflight:
            return
        from .stackup_model import find_width_result

        existing = find_width_result(
            self.width_results, spec.spec_id, self._active_layer, digest
        )
        if existing is not None and existing.status in ("success", "unsupported"):
            return
        self._draft_retry_timer.Stop()
        self._draft_calc_timer.StartOnce(DRAFT_WIDTH_CALC_DEBOUNCE_MS)

    def _on_draft_retry_timer(self, event: wx.TimerEvent) -> None:
        """Continue automatic recovery after a calculator delay or failure."""
        if self:
            self._schedule_draft_width_calculation()

    def _on_draft_slow_timer(self, event: wx.TimerEvent) -> None:
        """Offer the vendor calculator when a draft solve is still outstanding."""
        if not self or not self._draft_calc_inflight:
            return
        elapsed = (
            time.monotonic() - self._draft_calc_started_at
            if self._draft_calc_started_at
            else 0.0
        )
        _LOG.info(
            "JLCPCB width: draft still in flight after %.1fs (layer=%s generation=%s)",
            elapsed,
            self._active_layer,
            self._draft_calc_generation,
        )
        if not self._draft_calc_alert:
            self._draft_calc_alert = (
                "Nominal widths are taking longer than expected from JLCPCB's calculator. "
                "Comparisons will update automatically when results arrive."
            )
            self._set_calculator_link_visible(True)
            self._update_width_comparison(schedule=False)

    def _on_draft_calc_timer(self, event: wx.TimerEvent) -> None:
        """Start one background solve for the current draft fingerprint."""
        if self._closing or not self:
            return
        spec, digest = self._draft_calculation_input()
        if (
            spec is None
            or not digest
            or self.stackup is None
            or self._draft_calc_inflight
        ):
            return
        from .stackup_model import find_width_result

        stackup = self.stackup
        layer = self._active_layer
        existing = find_width_result(self.width_results, spec.spec_id, layer, digest)
        if existing is not None and existing.status in ("success", "unsupported"):
            return
        if self._draft_calc_cancel is not None:
            self._draft_calc_generation += 1
            self._draft_calc_cancel.set()
            self._draft_calc_cancel = None
        self._draft_calc_generation += 1
        generation = self._draft_calc_generation
        cancel = Event()
        self._draft_calc_cancel = cancel
        self._draft_calc_inflight = True
        self._draft_calc_started_at = time.monotonic()
        self._draft_retry_timer.Stop()
        self._draft_slow_timer.StartOnce(WIDTH_REFRESH_SLOW_MS)
        callback = self.calculate_width_callback
        configuration, save_configuration = _calculator_configuration_handoff(
            self.load_calculator_config, self.save_calculator_config, context="draft"
        )
        save_result = self.save_width_result
        owner = ref(self)
        _LOG.info(
            "JLCPCB width: draft start stackup=%s layer=%s kind=%s target=%sΩ generation=%s cached_config=%s",
            stackup.stackup_id,
            layer,
            spec.kind,
            spec.target_ohms,
            generation,
            configuration is not None,
        )

        def work() -> None:
            failure = ""
            result: Optional[WidthResult] = None
            started = time.monotonic()
            try:
                from .jlcpcb_calculator import JlcpcbCalculator

                calculate = (
                    callback
                    if callback is not None
                    else JlcpcbCalculator(
                        configuration=configuration,
                        save_configuration=save_configuration,
                    ).calculate_width
                )
                if not cancel.is_set():
                    result = calculate(stackup, spec, layer, cancel=cancel)
                    # Persist before marshalling to wx: a Cancel or window close
                    # after this point must not lose a completed solve.
                    _persist_width_result(save_result, result, context="draft")
            except Exception as error:
                failure = str(error) or "The nominal-width calculation failed."
            elapsed = time.monotonic() - started
            if failure:
                _LOG.info(
                    "JLCPCB width: draft failed after %.1fs layer=%s: %s",
                    elapsed,
                    layer,
                    failure,
                )
            elif result is not None:
                _LOG.info(
                    "JLCPCB width: draft finished in %.1fs layer=%s status=%s",
                    elapsed,
                    layer,
                    result.status,
                )
            if not cancel.is_set():
                try:
                    wx.CallAfter(
                        _deliver_draft_width_result,
                        owner,
                        generation,
                        digest,
                        result,
                        failure,
                    )
                except RuntimeError:
                    return

        Thread(target=work, name="jlcpcb-impedance-draft-width", daemon=True).start()

    def _finish_draft_calculation(
        self,
        generation: int,
        digest: str,
        result: Optional[WidthResult],
        failure: str,
    ) -> None:
        """Apply one draft-layer result while the editor inputs remain current."""
        if self._closing or not self or generation != self._draft_calc_generation:
            return
        spec, current_digest = self._draft_calculation_input()
        if spec is None or current_digest != digest:
            return
        self._draft_calc_cancel = None
        self._draft_calc_inflight = False
        self._draft_slow_timer.Stop()
        if result is not None and (
            result.input_digest != digest
            or result.spec_id != spec.spec_id
            or result.layer != self._active_layer
        ):
            _LOG.info(
                "JLCPCB width: draft result outdated; rescheduling (layer=%s)",
                result.layer,
            )
            self._schedule_draft_width_calculation()
            return
        if failure or result is None or result.status not in ("success", "unsupported"):
            reason = (
                failure
                or (result.message if result is not None else "")
                or "JLCPCB's calculator did not return a usable result."
            )
            _LOG.info(
                "JLCPCB width: draft apply skipped generation=%s reason=%s",
                generation,
                reason,
            )
            # Keep failure as an input-bound result, just like saved unavailable
            # results. Slow pending work has no failed result to highlight, while
            # retries retain this warning until success or an input change.
            unavailable = result or WidthResult(
                spec.spec_id, self._active_layer, digest, "unavailable", message=reason
            )
            # A failed refresh is advisory. Retain accepted results while an
            # unchanged reviewed layer is current, including during warm-up.
            sections = self._sections_for_layer(self._active_layer)
            preserve_parent_approval = False
            if (
                self._preserve_approved_width_results
                and self.original is not None
                and self.stackup is not None
            ):
                from .stackup_model import calculation_fingerprint

                with suppress(ValueError):
                    preserve_parent_approval = (
                        calculation_fingerprint(
                            self.stackup, self.original, self._active_layer
                        )
                        == digest
                    )
            if not preserve_parent_approval and not tracking.layer_approval_current(
                self.review_tracking,
                self._source_digest,
                spec,
                self._active_layer,
                sections,
                stackup=self.stackup,
                width_results=self.width_results,
                capture_digests=self.capture_digests,
            ):
                self._adopt_draft_width_result(unavailable)
            self._draft_calc_alert = f"Nominal width unavailable: {reason}"
            self._set_calculator_link_visible(True)
            self._update_width_comparison(schedule=False)
            self._draft_retry_timer.StartOnce(DRAFT_WIDTH_CALC_RETRY_MS)
            return
        self._draft_calc_alert = ""
        self._set_calculator_link_visible(False)
        self._draft_retry_timer.Stop()
        self._adopt_draft_width_result(result)
        _LOG.info(
            "JLCPCB width: draft applied layer=%s status=%s",
            result.layer,
            result.status,
        )
        self._update_width_comparison(schedule=True)

    def _adopt_draft_width_result(self, result: WidthResult) -> None:
        """Recheck layer consent whenever accepted nominal inputs change."""
        from .stackup_model import merge_width_results, width_results_fingerprint

        previous = self.width_results
        self.width_results = merge_width_results(previous, (result,))
        if width_results_fingerprint(previous) == width_results_fingerprint(
            self.width_results
        ):
            return
        draft = self._layer_drafts.get(result.layer)
        if draft is not None and draft.included:
            self._layer_drafts[result.layer] = replace(draft, confirmed=False)
            self._capture_restore_layers.add(result.layer)
            self._restore_layer_approval(
                result.layer, self._sections_for_layer(result.layer)
            )
        self._update_layer_labels()
        self.preview_pane.refresh_review_labels()

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
        self._wrap_form_value_text(self.reference_hint)
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
            # Previously inactive fields retain private values. Once relevant
            # again, every linked group must agree before it can be saved.
            for group in self._linked_layer_groups:
                source = self._active_layer if self._active_layer in group else group[0]
                self._synchronize_layer_settings(None, source_layer=source)
        included = bool(self._active_layer) and bool(self.include_layer.GetValue())
        self._setting_fields = True
        try:
            self._set_dimension_field(
                self.spacing, self.spacing_label, pair and included, "spacing"
            )
            self._set_dimension_field(
                self.ground_gap,
                self.ground_gap_label,
                ground and included,
                "ground_gap",
            )
        finally:
            self._setting_fields = False
        if getattr(self, "_target_automatic", False):
            self._setting_target = True
            try:
                self.target.SetValue("90" if pair else "50")
            finally:
                self._setting_target = False
        if event is not None:
            self._update_controls()
        self._update_field_indicators()

    def _set_dimension_field(
        self,
        control: wx.TextCtrl,
        label: wx.StaticText,
        active: bool,
        field: str,
    ) -> None:
        """Show N/A in disabled dimension fields without discarding stored drafts."""
        if active:
            if _is_inactive_dimension(control.GetValue()):
                draft = self._layer_drafts.get(self._active_layer)
                restored = getattr(draft, field, "") if draft is not None else ""
                if _is_inactive_dimension(restored):
                    restored = ""
                control.SetValue(restored)
            control.Enable(True)
            label.Enable(True)
            return
        current = control.GetValue()
        if self._active_layer and not _is_inactive_dimension(current):
            draft = self._layer_drafts[self._active_layer]
            self._layer_drafts[self._active_layer] = replace(draft, **{field: current})
        control.SetValue(INACTIVE_DIMENSION_PLACEHOLDER)
        control.Enable(False)
        label.Enable(False)

    def _selection_specification(self) -> Specification:
        """Describe actual matching intent without inventing missing fabrication dimensions."""
        return Specification(
            self._spec_id,
            self.label.GetValue().strip(),
            self.target.GetValue().strip(),
            KIND_CHOICES[self.kind.GetSelection()][0],
            self._active_class,
        )

    def _restore_viewed_captures(self, spec: Specification, layer: str) -> None:
        """Mark captures viewed when persisted fingerprints still match current pixels."""
        saved_ids = {
            record.section_id
            for record in self.review_tracking.images
            if record.layer == layer
        }
        hashes = {
            section: digest
            for section, digest in self._capture_hashes.items()
            if section.layer == layer
            and section.section_id in saved_ids
            and section not in self._capture_display_failed
        }
        if not hashes:
            return

        def matches(section: Section, image_sha256: str) -> bool:
            record = tracking.find_image(
                self.review_tracking, self._spec_id, layer, section.section_id
            )
            if record is None:
                return False
            digest = tracking.capture_fingerprint(
                self._source_digest, spec, section, image_sha256
            )
            return record.capture_digest == digest

        self.preview_pane.restore_viewed(matches, hashes)

    def _refresh_preview(self) -> None:
        """Show exactly the workbook candidate geometry, independently of missing gap fields."""
        self._update_approval_time()
        self._selection_error = ""
        self._selection_error_field = ""
        context = (
            self._active_class,
            KIND_CHOICES[self.kind.GetSelection()][0],
            tuple(
                layer
                for layer in self._layer_names
                if self._layer_drafts[layer].included
            ),
        )
        if self._capture_context is not None and context != self._capture_context:
            self._capture_generation += 1
            self._capture_prepare_timer.Stop()
            if context[:2] != self._capture_context[:2]:
                self._capture_cache.clear()
                self._capture_cache_bytes = 0
                self._capture_hashes.clear()
                self._capture_failed.clear()
                self._capture_display_failed.clear()
                self._capture_errors.clear()
                self._capture_retry_at.clear()
                self._capture_retry_attempts.clear()
                self._capture_failed_layers.clear()
                self._capture_restore_layers.clear()
                self._capture_first_paint = False
            else:
                # Inclusion changes invalidate work in flight, while retaining
                # useful pixels for the layers that still belong to the draft.
                for section in tuple(self._capture_hashes):
                    if section.layer not in context[2]:
                        self._capture_hashes.pop(section)
                        capture = self._capture_cache.pop(section, None)
                        if capture is not None:
                            self._capture_cache_bytes -= len(capture.data)
                self._capture_restore_layers.intersection_update(context[2])
                for section in tuple(self._capture_retry_at):
                    if section.layer not in context[2]:
                        self._capture_retry_at.pop(section, None)
                        self._capture_retry_attempts.pop(section, None)
                        self._capture_failed.discard(section)
                for section in tuple(self._capture_errors):
                    if section.layer not in context[2]:
                        self._capture_errors.pop(section, None)
                        self._capture_display_failed.discard(section)
                        self._capture_failed.discard(section)
        self._capture_context = context
        self._update_capture_retry_status()
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
            self._schedule_capture_preparation()
            return
        spec: Optional[Specification] = None
        try:
            spec = self._selection_specification()
            sections = self._sections_for_layer(layer)
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
            self._restore_viewed_captures(spec, layer)
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
        self._schedule_capture_preparation()

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
                else self.preview_pane.checklist
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
        self._wrap_form_value_text(self.validation_message)
        if self._validation_control is not None:
            self._validation_control.SetFocus()
        self._update_field_indicators()
        self.Layout()

    def _invalidate_current_layer_approval(self, *, failed: bool = True) -> None:
        """Keep a failed capture's layer pending even if the user navigates away."""
        section = self.preview_pane.selected_section
        if failed and section is not None:
            self._capture_hashes.pop(section, None)
            capture = self._capture_cache.pop(section, None)
            if capture is not None:
                self._capture_cache_bytes -= len(capture.data)
            self._capture_failed.add(section)
            self._capture_display_failed.add(section)
            self._capture_errors[section] = self.preview_pane.failure
            attempt = self._capture_retry_attempts.get(section, 0) + 1
            self._capture_retry_attempts[section] = attempt
            self._capture_retry_at[section] = (
                time.monotonic() + retry_delay_ms(attempt) / 1000
            )
        if failed and (
            tracking.find_layer(self.review_tracking, self._spec_id, self._active_layer)
            is not None
        ):
            self._capture_restore_layers.add(self._active_layer)
        elif not failed:
            self._capture_restore_layers.discard(self._active_layer)
        draft = self._layer_drafts.get(self._active_layer)
        if draft is not None and draft.included and draft.confirmed:
            self._layer_drafts[self._active_layer] = replace(draft, confirmed=False)
            self._update_layer_labels()

        self._update_capture_retry_status()

    def _invalidate_layer_approvals(self) -> None:
        """Require included layers to be reviewed again after visual context changes."""
        self._capture_generation += 1
        self._capture_prepare_timer.Stop()
        self._capture_cache.clear()
        self._capture_cache_bytes = 0
        self._capture_hashes.clear()
        self._capture_retry_at.clear()
        self._capture_retry_attempts.clear()
        self._capture_failed.clear()
        self._capture_display_failed.clear()
        self._capture_errors.clear()
        self._update_capture_retry_status()
        self._capture_restore_layers.clear()
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
                self._validation_control = pane.checklist
            raise ValueError(message) from error

    def _on_units(self, event: wx.CommandEvent) -> None:
        """Convert entered dimensions when the display unit changes."""
        units = self.units.GetStringSelection()
        changes = []
        draft_updates: dict[str, str] = {}
        pair, ground = dimension_fields_for_kind(
            KIND_CHOICES[self.kind.GetSelection()][0]
        )
        controls = (
            (self.spacing, pair, "spacing"),
            (self.ground_gap, ground, "ground_gap"),
        )
        try:
            for control, required, field in controls:
                value = control.GetValue().strip()
                if _is_inactive_dimension(value):
                    previous = self._layer_drafts.get(self._active_layer)
                    draft_value = (
                        getattr(previous, field, "") if previous is not None else ""
                    )
                    if draft_value and not _is_inactive_dimension(draft_value):
                        try:
                            dimension = self._read_dimension(
                                draft_value, self._width_units
                            )
                        except ValueError:
                            continue
                        text = format_distance(dimension, units)
                        draft_updates[field] = text
                        self._exact_dimensions[text, units] = dimension
                    continue
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
        included = bool(self._active_layer) and bool(self.include_layer.GetValue())
        self._setting_fields = True
        try:
            for control, dimension, text in changes:
                control.SetValue(text)
                self._exact_dimensions[text, units] = dimension
            if self._active_layer and draft_updates:
                draft = self._layer_drafts[self._active_layer]
                self._layer_drafts[self._active_layer] = replace(draft, **draft_updates)
            self._set_dimension_field(
                self.spacing, self.spacing_label, pair and included, "spacing"
            )
            self._set_dimension_field(
                self.ground_gap,
                self.ground_gap_label,
                ground and included,
                "ground_gap",
            )
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
        """Apply supplied settings without demanding completed visual review."""
        try:
            self._clear_validation()
            self._verify_current_snapshot()
            self._validation_control = self.target
            text = self.target.GetValue().strip()
            target = (
                format(positive_decimal(text, "desired impedance"), "f") if text else ""
            )
            self._save_class_specification(target)
        except (InvalidOperation, ValueError) as error:
            self.specification = None
            self._show_validation(str(error) or "Enter a valid impedance value.")

    def _save_class_specification(self, target: str) -> None:
        """Accept complete or unfinished intent for current routed signal layers."""
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
            if self._active_class:
                raise ValueError(
                    f"Saved net class {self._active_class!r} is unavailable. Choose an existing class explicitly."
                )
            raise ValueError("Choose an existing net class explicitly.")
        self._stash_class_layer()
        self._refresh_class_layers()
        routed = {trace.layer for trace in self._class_traces()}
        if not routed:
            raise ValueError("No routed named nets belong to this class.")
        settings = tuple(
            self._draft_settings(
                layer, self._layer_drafts[layer], allow_incomplete=True
            )
            for layer in self.layers
            if layer in self._layer_drafts and self._layer_drafts[layer].included
        )
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
            ),
            linked_layer_groups=self._linked_layer_groups,
            independent_layers=tuple(
                layer for layer in self.layers if layer in self._independent_layers
            ),
        )
        validate_storage_config(Config(specifications=(candidate,)), self.layers)
        self._accept_specification(candidate)

    def _accept_specification(self, candidate: Specification) -> None:
        """Retain latest events for accepted rows, not an ever-growing route history."""
        included_layers = {settings.layer for settings in candidate.layer_settings}
        retained_rows: set[tuple[str, str]] = set()
        unresolved: set[str] = set()
        for layer in included_layers:
            try:
                retained_rows.update(
                    (section.layer, section.section_id)
                    for section in preview_sections(candidate, self.snapshot, layer)
                )
            except (ValueError, InvalidOperation):
                # Preserve historical evidence until configuration can be matched.
                # Fingerprints, never the existence of history, authorize review.
                unresolved.add(layer)
        retained_layers = included_layers | set(candidate.excluded_layers)
        self.review_tracking = tracking.ReviewTracking(
            images=tuple(
                record
                for record in self.review_tracking.images
                if (record.layer, record.section_id) in retained_rows
                or record.layer in unresolved
            ),
            layers=tuple(
                record
                for record in self.review_tracking.layers
                if record.layer in retained_layers
            ),
        )
        self.specification = candidate
        self._cancel_draft_calculation()
        self._stop_capture_preparation()
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
        load_calculator_config: Optional[Callable[[], CalculatorConfigCache]] = None,
        save_calculator_config: Optional[
            Callable[[CalculatorConfigCache], None]
        ] = None,
        calculate_width: Optional[Callable[..., WidthResult]] = None,
        fetch_stackup_catalog: Optional[Callable[..., tuple[Stackup, ...]]] = None,
        load_stackup_colors: Optional[
            Callable[[], dict[str, tuple[int, int, int, int]]]
        ] = None,
        save_config: Optional[Callable[[Config], None]] = None,
        load_width_results: Optional[Callable[[], tuple[WidthResult, ...]]] = None,
        save_width_result: Optional[Callable[[WidthResult], None]] = None,
        skip_stub_review: Optional[Callable[[], bool]] = None,
        verify_captures: Optional[
            Callable[[BoardSnapshot, tuple[Section, ...]], CaptureEvidence]
        ] = None,
        prepare_capture: Optional[
            Callable[[BoardSnapshot, Section], CapturedImage]
        ] = None,
    ) -> None:
        super().__init__(
            parent,
            title="JLCPCB controlled impedance",
            size=(1248, 740),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._window_id = self.GetId()
        self.config = config
        self.session = ReviewSession(config, snapshot)
        self.save_width_result = save_width_result
        self.skip_stub_review = skip_stub_review
        self._seed_width_results_from_cache(load_width_results)
        self.preview_callback = preview
        self.verify_snapshot = verify_snapshot
        self.appearance_context = appearance_context
        self.refresh_snapshot = refresh_snapshot
        self.verify_captures = verify_captures
        self.prepare_capture = prepare_capture
        self.save_config = save_config
        self.load_stackup_catalog = load_stackup_catalog
        self.save_stackup_catalog = save_stackup_catalog
        self.load_calculator_config = load_calculator_config
        self.save_calculator_config = save_calculator_config
        self.fetch_stackup_catalog = fetch_stackup_catalog
        self.load_stackup_colors = load_stackup_colors
        self._catalog_controller: Optional[CatalogController] = None
        self._catalog_unsubscribe: Optional[Callable[[], None]] = None
        self._catalog_epoch = 0
        self._catalog_check_scheduled = False
        self.calculate_width_callback = calculate_width
        self._calculation_generation = 0
        self._active_calculation_key: Optional[tuple[object, ...]] = None
        self._calculation_cancel: Optional[Event] = None
        self._width_refresh_alert = ""
        self._width_refresh_started_at = 0.0
        self._width_refresh_timer = wx.Timer(self)
        self._width_slow_timer = wx.Timer(self)
        self._capture_prepare_timer = wx.Timer(self)
        self._capture_prepare_sections: tuple[Section, ...] = ()
        self._main_capture_retry_attempts: dict[Section, int] = {}
        self._main_capture_retry_at: dict[Section, float] = {}
        self._main_capture_retry_context: Optional[tuple[object, ...]] = None
        self._capture_restore_pending = False
        self._capture_expected_key: Optional[tuple[object, ...]] = None
        self._capture_expected: dict[Section, str] = {}
        self._capture_source_digest = ""
        self._main_first_paint = False
        self._specification_rows: Optional[tuple[tuple[object, ...], ...]] = None
        self._closing = False
        self._draft_save_notice_pending = False
        self._child_dialog_open = False
        self._updating_sections = False
        self._workbook_inclusion_refresh_queued = False
        self._row_ids: tuple[str, ...] = ()
        self._rows_error = ""
        root = wx.BoxSizer(wx.VERTICAL)
        stackup_row = wx.BoxSizer(wx.HORIZONTAL)
        self.stackup_summary = wx.StaticText(self, label="")
        self.stackup_summary.Wrap(630)
        stackup_row.Add(self.stackup_summary, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.stackup_button = wx.Button(self, label="Select stackup…")
        self.stackup_button.Bind(wx.EVT_BUTTON, self._on_stackup)
        stackup_row.Add(self.stackup_button)
        root.Add(stackup_row, 0, wx.ALL | wx.EXPAND, 12)
        self.catalog_status = wx.TextCtrl(
            self,
            value="Catalog: waiting for the impedance dialog to open.",
            size=(-1, 36),
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.BORDER_NONE,
        )
        root.Add(self.catalog_status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)
        self.specifications = dv.DataViewListCtrl(
            self,
            style=dv.DV_SINGLE | dv.DV_ROW_LINES | dv.DV_VERT_RULES,
        )
        self.specifications.AssociateModel(_SpecificationListStore())
        for title, width in _SPECIFICATION_COLUMNS:
            self.specifications.AppendTextColumn(
                title,
                width=width,
                mode=dv.DATAVIEW_CELL_INERT,
            )
        self.specifications.SetMinSize((-1, 105))
        self._review_row_labels: list[str] = []
        root.Add(self.specifications, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 12)
        spec_buttons = wx.BoxSizer(wx.HORIZONTAL)
        self._selection_buttons: list[wx.Button] = []
        for label, handler in (
            ("Add…", self._on_add),
            ("Edit / Review…", self._on_edit),
            ("Remove", self._on_remove),
        ):
            button = wx.Button(self, label=label)
            button.Bind(wx.EVT_BUTTON, handler)
            spec_buttons.Add(button, 0, wx.RIGHT, 8)
            if label != "Add…":
                self._selection_buttons.append(button)
        self.specifications.Bind(
            dv.EVT_DATAVIEW_SELECTION_CHANGED, self._on_specification_selection
        )
        root.Add(spec_buttons, 0, wx.ALL | wx.EXPAND, 12)
        content = wx.BoxSizer(wx.HORIZONTAL)
        left = wx.BoxSizer(wx.VERTICAL)
        left.Add(
            wx.StaticText(self, label="Workbook rows — include the rows to export:"),
            0,
            wx.BOTTOM,
            6,
        )
        from .workbook_grid import WorkbookGrid

        self.sections = WorkbookGrid(
            self,
            _WORKBOOK_COLUMNS,
            on_include=self._on_include,
            on_select=self._on_preview,
        )
        self.sections.SetMinSize((720, 160))
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
        content.Add(left, 3, wx.RIGHT | wx.EXPAND, 12)
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
            and not self._child_dialog_open
            and self._main_first_paint,
            state_changed=self._on_main_preview_state,
            skip_stub_review=skip_stub_review,
        )
        content.Add(self.preview_pane.window, 1, wx.EXPAND)
        root.Add(content, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)
        self.status = wx.TextCtrl(
            self,
            value="Add a specification to create workbook rows.",
            size=(-1, 44),
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.BORDER_NONE,
        )
        self.calculator_link = wx.adv.HyperlinkCtrl(
            self, wx.ID_ANY, CALCULATOR_LINK_LABEL, CALCULATOR_URL
        )
        self.calculator_link.Hide()
        status_column = wx.BoxSizer(wx.VERTICAL)
        status_column.Add(self.status, 0, wx.EXPAND)
        status_column.Add(self.calculator_link, 0, wx.TOP, 4)
        root.Add(status_column, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        buttons.AddStretchSpacer()
        buttons.Add(
            self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALIGN_CENTER_VERTICAL
        )
        self.FindWindow(wx.ID_OK).SetLabel("Save draft and close")
        root.Add(buttons, 0, wx.ALL | wx.EXPAND, 12)
        self.Bind(wx.EVT_BUTTON, self._on_save, id=wx.ID_OK)
        self.Bind(wx.EVT_BUTTON, self._on_cancel, id=wx.ID_CANCEL)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_SHOW, self._on_main_show)
        self.Bind(wx.EVT_PAINT, self._on_main_paint)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self.Bind(wx.EVT_TIMER, self._on_width_refresh_timer, self._width_refresh_timer)
        self.Bind(wx.EVT_TIMER, self._on_width_slow_timer, self._width_slow_timer)
        self.Bind(
            wx.EVT_TIMER, self._on_main_capture_timer, self._capture_prepare_timer
        )
        self.Bind(wx.EVT_IDLE, self._on_main_capture_idle)
        self.specifications.Bind(dv.EVT_DATAVIEW_ITEM_ACTIVATED, self._on_edit)
        self.SetSizer(root)
        self.SetMinSize((1176, 680))
        self._refresh_rows(snapshot)
        # Unavailable appearance revokes visual approval while intent stays editable.
        with suppress(ValueError):
            self.preview_pane.ensure_appearance_current()
        self.CentreOnParent()

    def _verify_restored_capture_review(
        self,
        snapshot: Optional[BoardSnapshot] = None,
        sections: Optional[tuple[Section, ...]] = None,
    ) -> None:
        """Verify included saved PNGs without inventing views or approval events."""
        if not self.session.approved or self.session.analysis is None:
            return
        saving = snapshot is not None
        if self.verify_captures is not None and snapshot is None:
            try:
                snapshot = self.refresh_snapshot()
            except Exception as error:
                self._fail_rows(str(error) or "The current board could not be read.")
                return
            if snapshot != self.session.snapshot:
                self._refresh_rows(snapshot)
                if not self.session.approved or self.session.analysis is None:
                    return
        config = self.session.config
        specifications = {spec.spec_id: spec for spec in config.specifications}
        sections = self._review_capture_sections() if sections is None else sections
        source_digest = self._capture_source_digest
        hashes: Optional[dict[Section, Optional[str]]] = None
        if self.verify_captures is not None:
            try:
                evidence = self.verify_captures(
                    snapshot if snapshot is not None else self.session.snapshot,
                    sections,
                )
            except Exception as error:
                _LOG.info("Controlled impedance capture verification failed: %s", error)
                self.session.capture_digests.update(
                    (section.section_id, None) for section in sections
                )
                self.session.invalidate_review()
                self._populate_review_status()
                if saving:
                    raise
                self._error(str(error) or "The current captures could not be verified.")
                return
            if not evidence.current:
                self.session.capture_digests.update(
                    (section.section_id, None) for section in sections
                )
                self.session.invalidate_review()
                self._populate_review_status()
                return
            hashes = evidence.hashes
        for section in sections:
            expected = self._capture_expected.get(section)
            # Older workbook approvals have no recorded pixels to compare.
            # Loading them must not manufacture capture history.
            if expected is None:
                continue
            try:
                if hashes is None:
                    capture = self.preview_callback(section, refresh=False)
                    self._verify_review_snapshot()
                    self.preview_pane.ensure_appearance_current()
                    image_hash = capture.sha256
                else:
                    image_hash = hashes.get(section)
                    if image_hash is None:
                        raise ValueError("The reviewed image could not be verified.")
                digest = tracking.capture_fingerprint(
                    source_digest,
                    specifications[section.spec_id],
                    section,
                    image_hash,
                )
            except Exception:
                self.session.capture_digests[section.section_id] = None
                self.session.invalidate_review()
                break
            self.session.capture_digests[section.section_id] = digest
            if expected != digest:
                self.session.invalidate_review()
                break
        self._populate_review_status()

    def _review_capture_sections(self) -> tuple[Section, ...]:
        """Select saved pixels requiring verification, including layer-only evidence."""
        analysis = self.session.analysis
        if analysis is None:
            return ()
        config = self.session.config
        key = (
            analysis,
            config.specifications,
            config.stackup,
            config.width_results,
            config.review_tracking,
            config.reviewed_captures,
            frozenset(self.session.included),
        )
        if self._capture_expected_key == key:
            return tuple(self._capture_expected)
        specifications = {spec.spec_id: spec for spec in config.specifications}
        source_digest = tracking.source_fingerprint(self.session.snapshot)
        self._capture_source_digest = source_digest
        self._capture_expected = {}
        for section in analysis.sections:
            if section.section_id not in self.session.included:
                continue
            expected = tracking.expected_capture_digest(
                config.review_tracking,
                source_digest,
                specifications[section.spec_id],
                section,
                analysis.sections,
                stackup=config.stackup,
                width_results=config.width_results,
                workbook_captures=config.reviewed_captures,
            )
            if expected is not None:
                self._capture_expected[section] = expected
        self._capture_expected_key = key
        return tuple(self._capture_expected)

    def _reset_main_capture_preparation(self) -> None:
        """Warm current rows incrementally; saved approval waits for known pixels."""
        self._capture_prepare_timer.Stop()
        self.preview_pane.set_background_status("")
        analysis = self.session.analysis
        context = (self.session.snapshot, self.session.config.specifications)
        if context != self._main_capture_retry_context:
            self._main_capture_retry_context = context
            self._main_capture_retry_attempts.clear()
            self._main_capture_retry_at.clear()
        required = self._review_capture_sections() if self.session.approved else ()
        missing = tuple(
            section
            for section in required
            if self.session.capture_digests.get(section.section_id)
            != self._capture_expected[section]
        )
        self._capture_restore_pending = bool(missing)
        others = (
            tuple(
                section
                for section in analysis.sections
                if section not in required
                and (
                    not section.exclusion_reason
                    or self.skip_stub_review is not None
                    and not self.skip_stub_review()
                )
            )
            if analysis is not None
            else ()
        )
        self._capture_prepare_sections = missing + others
        if self._main_first_paint and self._capture_prepare_sections:
            wx.WakeUpIdle()

    def _on_main_paint(self, event: wx.PaintEvent) -> None:
        """Leave native painting before starting any capture or calculation work."""
        event.Skip()
        if self._closing or self._main_first_paint or not self.IsShownOnScreen():
            return
        self._main_first_paint = True
        wx.CallAfter(self._queue_selected_preview)
        wx.CallAfter(self._ensure_width_refresh_timer)
        wx.CallAfter(self._schedule_width_refresh)
        if self._capture_prepare_sections:
            wx.WakeUpIdle()

    def _on_main_preview_state(self) -> None:
        """Resume speculative work after selected display or a settled failure."""
        if self._closing or self._updating_sections:
            return
        self._populate_review_status()
        if self._capture_prepare_sections:
            wx.WakeUpIdle()

    def _on_main_capture_timer(self, event: wx.TimerEvent) -> None:
        """Resume idle work after a failed capture's cooldown."""
        self._capture_prepare_timer.Stop()
        if not self._closing and self:
            wx.WakeUpIdle()

    def _on_main_capture_idle(self, event: wx.IdleEvent) -> None:
        """Prepare one image only after input, queued completion and paint work."""
        event.Skip()
        if self._closing or not self or not self.IsShownOnScreen():
            return
        if (
            self._capture_prepare_timer.IsRunning()
            or self._child_dialog_open
            or (
                self.preview_pane.sections
                and not self.preview_pane.ready
                and not self.preview_pane.failure
            )
        ):
            return
        if not self._capture_prepare_sections:
            return
        # Edits and fresh approvals can replace tracking without rebuilding the
        # row geometry. Compare against the current memo, never a previous review.
        self._review_capture_sections()
        analysis, snapshot = self.session.analysis, self.session.snapshot
        before = self._capture_restore_pending, self.session.approved
        now = time.monotonic()
        index = next(
            (
                index
                for index, section in enumerate(self._capture_prepare_sections)
                if self._main_capture_retry_at.get(section, 0) <= now
            ),
            None,
        )
        if index is None:
            due = min(
                self._main_capture_retry_at[section]
                for section in self._capture_prepare_sections
            )
            self._capture_prepare_timer.StartOnce(max(1, round((due - now) * 1000)))
            return
        section = self._capture_prepare_sections[index]
        self._capture_prepare_sections = (
            self._capture_prepare_sections[:index]
            + self._capture_prepare_sections[index + 1 :]
        )
        failure = ""
        try:
            image_hash = self.preview_pane.image_digest(section)
            if image_hash is None:
                if self.prepare_capture is not None:
                    capture = self.prepare_capture(snapshot, section)
                else:
                    capture = self.preview_callback(section, refresh=False)
                    self._verify_review_snapshot()
                    self.preview_pane.ensure_appearance_current()
                image_hash = capture.sha256
            if self._closing or self.session.analysis is not analysis:
                return
            self._review_capture_sections()
            expected = self._capture_expected.get(section)
            if self.session.approved and expected is not None:
                spec = next(
                    spec
                    for spec in self.session.config.specifications
                    if spec.spec_id == section.spec_id
                )
                digest = tracking.capture_fingerprint(
                    self._capture_source_digest, spec, section, image_hash
                )
                self.session.capture_digests[section.section_id] = digest
                if digest != expected:
                    self.session.invalidate_review()
            self._main_capture_retry_attempts.pop(section, None)
            self._main_capture_retry_at.pop(section, None)
        except Exception as error:
            if self._closing or self.session.analysis is not analysis:
                return
            if self._capture_restore_pending:
                self.session.capture_digests[section.section_id] = None
                self.session.invalidate_review()
            self._capture_prepare_sections += (section,)
            attempt = self._main_capture_retry_attempts.get(section, 0) + 1
            self._main_capture_retry_attempts[section] = attempt
            self._main_capture_retry_at[section] = (
                time.monotonic() + retry_delay_ms(attempt) / 1000
            )
            failure = str(error) or "The current capture could not be prepared."
        pending = bool(
            self.session.approved
            and any(
                section.section_id not in self.session.capture_digests
                for section in self._review_capture_sections()
            )
        )
        if self._capture_restore_pending and not pending and self.session.approved:
            try:
                current = self.refresh_snapshot()
                if current != snapshot:
                    self._refresh_rows(current)
                    return
                self._verify_restored_capture_review(current, ())
            except Exception as error:
                self.session.invalidate_review()
                failure = str(error) or "The current captures could not be verified."
        self._capture_restore_pending = pending and self.session.approved
        if before != (self._capture_restore_pending, self.session.approved):
            self._populate_workbook_status()
        if failure:
            message = failure + " The image will retry automatically."
            self.preview_pane.set_background_status(message)
            self._error(message)
        elif not self._capture_prepare_sections:
            if self.preview_pane.set_background_status(""):
                self._populate_workbook_status()
        if self._capture_prepare_sections:
            now = time.monotonic()
            due = min(
                self._main_capture_retry_at.get(section, now)
                for section in self._capture_prepare_sections
            )
            if due <= now:
                event.RequestMore()
            else:
                self._capture_prepare_timer.StartOnce(max(1, round((due - now) * 1000)))

    def _seed_width_results_from_cache(
        self, load: Optional[Callable[[], tuple[WidthResult, ...]]]
    ) -> None:
        """Adopt persisted solves whose digest matches the current inputs.

        The saved configuration only carries results the user committed with
        "Save and close"; the per-board cache holds every settled solve as it
        arrived. Merging digest-current rows here lets the first paint show
        Differs/matches instead of "waiting for JLCPCB" and lets the show-time
        refresh skip those jobs entirely. Rows for other inputs are ignored so a
        stale or foreign result can never suppress a needed refresh.
        """
        if load is None:
            return
        try:
            cached = tuple(load())
        except Exception as error:
            _LOG.info("JLCPCB width: cache unavailable on open (%s)", error)
            return
        if not cached:
            return
        from .stackup_model import (
            calculation_fingerprint,
            merge_width_results,
            width_results_fingerprint,
        )

        config = self.session.config
        if config.stackup is None:
            return
        wanted: dict[tuple[str, str, str], bool] = {}
        for spec in config.specifications:
            for settings in spec.layer_settings:
                try:
                    digest = calculation_fingerprint(
                        config.stackup, spec, settings.layer
                    )
                except Exception:
                    continue
                wanted[(spec.spec_id, settings.layer, digest)] = True
        # The saved config is authoritative for any digest it already settled:
        # a saved approval must not be revoked by a cache row for the same
        # inputs. The cache only fills digests that are missing or transient.
        settled_saved = {
            result.input_digest
            for result in config.width_results
            if result.status in SETTLED_WIDTH_STATUSES
        }
        adopted = tuple(
            result
            for result in cached
            if result.status in SETTLED_WIDTH_STATUSES
            and result.input_digest not in settled_saved
            and (result.spec_id, result.layer, result.input_digest) in wanted
        )
        if not adopted:
            return
        try:
            merged = merge_width_results(config.width_results, adopted)
            candidate = replace(config, width_results=merged)
            validate_storage_config(candidate, self.session.snapshot.layers)
        except Exception as error:
            _LOG.info("JLCPCB width: cached results not adopted (%s)", error)
            return
        if width_results_fingerprint(config.width_results) == width_results_fingerprint(
            merged
        ):
            return
        _LOG.info("JLCPCB width: seeded %s cached result(s) on open", len(adopted))
        # Same review invalidation the live refresh performs when nominals change.
        self.session.replace_calculation_context(config.stackup, merged)

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

            def usable_current() -> bool:
                """Keep a compatible frozen board construction usable during outages."""
                dialog = owner()
                if dialog is None or dialog._closing or not dialog:
                    return False
                stackup = dialog.session.config.stackup
                return stackup is not None and compatible_stackup(stackup, count)

            controller = CatalogController(
                count,
                load_cache=self.load_stackup_catalog,
                save_cache=self.save_stackup_catalog,
                fetch_catalog=self.fetch_stackup_catalog,
                owner_current=owner_current,
                usable_current=usable_current,
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
                    self._clear_width_refresh_alert()
                    self.session.replace_calculation_context(
                        dialog.stackup, current.width_results
                    )
                else:
                    self.session.config = replace(current, stackup=dialog.stackup)
                refreshed = self._refresh_rows(snapshot)
                if changed and refreshed:
                    self._error(
                        "Stackup changed. Review Target width and approve the updated workbook rows."
                    )
                self._schedule_width_refresh()
            finally:
                self._child_dialog_open = False
                dialog.Destroy()
                self._queue_selected_preview()
        except Exception as error:
            self._error(str(error))

    def _calculation_key(
        self, config: Config, snapshot: BoardSnapshot
    ) -> tuple[object, ...]:
        """Exclude view timestamps while retaining every immutable calculation input."""
        return (snapshot, config.stackup, config.specifications)

    def _verify_calculation_snapshot(self) -> None:
        """Guard async boundaries even when the optional native verifier is absent."""
        try:
            self._verify_review_snapshot()
        except _RowsRefreshed:
            raise
        except Exception as error:
            self._invalidate_visual_review()
            raise ValueError(
                str(error)
                or "The current board could not be verified. Retry when it is available."
            ) from error

    def _width_refresh_jobs(
        self, config: Config
    ) -> Optional[tuple[tuple[Specification, str], ...]]:
        """Return fingerprint-ready jobs, or None when inputs are incomplete."""
        from .stackup_model import calculation_fingerprint

        if config.stackup is None or not config.stackup.calculator_id:
            return None
        jobs = self._complete_width_jobs(config)
        if not jobs:
            return None
        try:
            for spec, layer in jobs:
                calculation_fingerprint(config.stackup, spec, layer)
        except Exception:
            return None
        return jobs

    @staticmethod
    def _complete_width_jobs(config: Config) -> tuple[tuple[Specification, str], ...]:
        """Select complete layer inputs independently of provider availability."""
        return tuple(
            (spec, settings.layer)
            for spec in config.specifications
            for settings in spec.layer_settings
            if _configuration_complete(
                replace(
                    spec,
                    layer_settings=(settings,),
                    excluded_layers=(),
                    linked_layer_groups=(),
                    independent_layers=(),
                )
            )
        )

    def _width_results_current(
        self, config: Config, jobs: tuple[tuple[Specification, str], ...]
    ) -> bool:
        """Return whether every job has a current success or unsupported result."""
        return not self._width_jobs_needing_refresh(config, jobs)

    def _width_jobs_needing_refresh(
        self, config: Config, jobs: tuple[tuple[Specification, str], ...]
    ) -> tuple[tuple[Specification, str], ...]:
        """Return only jobs whose current-digest result is missing or not usable."""
        from .stackup_model import calculation_fingerprint, find_width_result

        if config.stackup is None:
            return jobs
        needing = []
        for spec, layer in jobs:
            digest = calculation_fingerprint(config.stackup, spec, layer)
            result = find_width_result(
                config.width_results, spec.spec_id, layer, digest
            )
            if result is None:
                stale = find_width_result(config.width_results, spec.spec_id, layer)
                reason = "digest_mismatch" if stale is not None else "missing"
                _LOG.info(
                    "JLCPCB width: needs refresh %s/%s reason=%s",
                    spec.spec_id,
                    layer,
                    reason,
                )
                needing.append((spec, layer))
            elif result.status not in ("success", "unsupported"):
                _LOG.info(
                    "JLCPCB width: needs refresh %s/%s reason=status_%s",
                    spec.spec_id,
                    layer,
                    result.status,
                )
                needing.append((spec, layer))
        return tuple(needing)

    def _ensure_width_refresh_timer(self) -> None:
        """Keep a quiet retry timer alive while the dialog remains open."""
        if self._closing or not self:
            return
        if not self._width_refresh_timer.IsRunning():
            self._width_refresh_timer.Start(WIDTH_REFRESH_RETRY_MS)

    def _on_width_refresh_timer(self, event: wx.TimerEvent) -> None:
        """Retry missing or failed nominal widths without user action."""
        if not self._closing and self:
            self._schedule_width_refresh()

    def _on_width_slow_timer(self, event: wx.TimerEvent) -> None:
        """Alert once when a needed refresh has not produced current results."""
        if self._closing or not self:
            return
        jobs = self._width_refresh_jobs(self.session.config)
        elapsed = (
            time.monotonic() - self._width_refresh_started_at
            if self._width_refresh_started_at
            else 0.0
        )
        in_flight = self._calculation_cancel is not None
        if jobs is None or self._width_results_current(self.session.config, jobs):
            _LOG.info(
                "JLCPCB width: slow timer fired after %.1fs but results are current",
                elapsed,
            )
            self._width_refresh_alert = ""
            self._populate_workbook_status()
            return
        pending = ", ".join(f"{spec.spec_id}/{layer}" for spec, layer in jobs)
        _LOG.info(
            "JLCPCB width: still waiting after %.1fs in_flight=%s generation=%s jobs=[%s]",
            elapsed,
            in_flight,
            self._calculation_generation,
            pending,
        )
        if not self._width_refresh_alert:
            self._width_refresh_alert = (
                "Nominal widths are taking longer than expected from JLCPCB's calculator. "
                "Target widths will update automatically when results arrive."
            )
            self._populate_sections()

    def _clear_width_refresh_alert(self) -> bool:
        """Drop a slow-update notice once current results exist or work is cancelled."""
        self._width_slow_timer.Stop()
        changed = bool(self._width_refresh_alert)
        self._width_refresh_alert = ""
        return changed

    def _set_main_calculator_link_visible(self, visible: bool) -> None:
        """Show the vendor calculator when width refresh is slow or failed."""
        if visible == self.calculator_link.IsShown():
            return
        self.calculator_link.Show(visible)
        self.Layout()

    def _schedule_width_refresh(self, *, force: bool = False) -> None:
        """Quietly refresh nominal widths when committed inputs need them."""
        if self._closing or not self:
            return
        attempt_generation = self._calculation_generation
        try:
            config = self.session.config
            jobs = self._width_refresh_jobs(config)
            pending = (
                (jobs if force else self._width_jobs_needing_refresh(config, jobs))
                if jobs
                else ()
            )
            # Width inputs are declared configuration, independent of geometry.
            # Check the board only when starting or owning actual async work.
            if not force and self._calculation_cancel is None and not pending:
                if self._clear_width_refresh_alert():
                    self._populate_workbook_status()
                return
            self._verify_calculation_snapshot()
            snapshot = self.session.snapshot
            key = self._calculation_key(config, snapshot)
            if (
                not force
                and self._calculation_cancel is not None
                and self._active_calculation_key == key
            ):
                return
            if self._calculation_cancel is not None:
                self._cancel_width_batch()
                attempt_generation = self._calculation_generation
            validate_storage_config(config, snapshot.layers)
            if jobs is None:
                if self._clear_width_refresh_alert():
                    self._populate_workbook_status()
                return
            if not pending:
                _LOG.info(
                    "JLCPCB width: refresh skipped; cached results are current (%s jobs)",
                    len(jobs),
                )
                if self._clear_width_refresh_alert():
                    self._populate_workbook_status()
                return
            stackup = config.stackup
            assert stackup is not None
            self._calculation_generation += 1
            generation = self._calculation_generation
            attempt_generation = generation
            cancel = Event()
            self._calculation_cancel = cancel
            self._active_calculation_key = key
            # Drop stale failure text so pending refreshes do not look like a new rejection.
            self._clear_width_refresh_alert()
            callback = self.calculate_width_callback
            configuration, save_configuration = _calculator_configuration_handoff(
                self.load_calculator_config,
                self.save_calculator_config,
                context="batch",
            )
            save_result = self.save_width_result
            owner = ref(self)
            self._width_refresh_started_at = time.monotonic()
            slow_ms = WIDTH_REFRESH_SLOW_MS + max(0, len(pending) - 1) * (
                WIDTH_REFRESH_SLOW_PER_JOB_MS
            )
            self._width_slow_timer.StartOnce(slow_ms)
            job_summary = ", ".join(
                f"{spec.spec_id}/{layer}/{spec.kind}/{spec.target_ohms}Ω"
                for spec, layer in pending
            )
            cached = len(jobs) - len(pending)
            _LOG.info(
                "JLCPCB width: batch start stackup=%s force=%s generation=%s "
                "cached=%s slow_after=%.1fs jobs=[%s]",
                stackup.stackup_id,
                force,
                generation,
                cached,
                slow_ms / 1000.0,
                job_summary,
            )

            def work() -> None:
                """Use only immutable model data; marshal completion back to wx."""
                results: list[WidthResult] = []
                failure = ""
                started = time.monotonic()
                try:
                    from .jlcpcb_calculator import JlcpcbCalculator

                    calculate = (
                        callback
                        if callback is not None
                        else JlcpcbCalculator(
                            configuration=configuration,
                            save_configuration=save_configuration,
                        ).calculate_width
                    )
                    for index, (spec, layer) in enumerate(pending, start=1):
                        if cancel.is_set():
                            _LOG.info(
                                "JLCPCB width: batch cancelled after %s/%s jobs (%.1fs)",
                                index - 1,
                                len(pending),
                                time.monotonic() - started,
                            )
                            return
                        job_started = time.monotonic()
                        _LOG.info(
                            "JLCPCB width: job %s/%s start %s/%s",
                            index,
                            len(pending),
                            spec.spec_id,
                            layer,
                        )
                        result = calculate(stackup, spec, layer, cancel=cancel)
                        results.append(result)
                        # Persist per job, not per batch: a close mid-batch keeps
                        # every solve that already completed.
                        _persist_width_result(save_result, result, context="batch")
                        _LOG.info(
                            "JLCPCB width: job %s/%s done in %.1fs status=%s",
                            index,
                            len(pending),
                            time.monotonic() - job_started,
                            result.status,
                        )
                except Exception as error:
                    failure = str(error) or "The nominal-width calculation failed."
                    _LOG.info(
                        "JLCPCB width: batch failed after %.1fs: %s",
                        time.monotonic() - started,
                        failure,
                    )
                else:
                    _LOG.info(
                        "JLCPCB width: batch worker finished in %.1fs (%s results)",
                        time.monotonic() - started,
                        len(results),
                    )
                if not cancel.is_set():
                    try:
                        wx.CallAfter(
                            _deliver_width_results,
                            owner,
                            generation,
                            key,
                            tuple(results),
                            failure,
                        )
                    except RuntimeError:
                        return

            Thread(target=work, name="jlcpcb-impedance-widths", daemon=True).start()
        except _RowsRefreshed:
            if self._calculation_generation == attempt_generation:
                self._cancel_width_batch()
            wx.CallAfter(self._schedule_width_refresh)
            return
        except Exception as error:
            if self._calculation_generation == attempt_generation:
                self._cancel_width_batch()
            # Incomplete board/config readiness is explained by width comparisons.
            _LOG.info("JLCPCB width: refresh not started (%s)", error)
            self._clear_width_refresh_alert()

    def _finish_calculation(
        self,
        generation: int,
        key: tuple[object, ...],
        results: tuple[WidthResult, ...],
        failure: str,
    ) -> None:
        """Apply current valid results; wholly failed refreshes retain approval."""
        if self._closing or not self or generation != self._calculation_generation:
            _LOG.info(
                "JLCPCB width: ignoring stale batch generation=%s current=%s",
                generation,
                self._calculation_generation if self else None,
            )
            return
        self._calculation_cancel = None
        self._active_calculation_key = None
        elapsed = (
            time.monotonic() - self._width_refresh_started_at
            if self._width_refresh_started_at
            else 0.0
        )
        try:
            self._verify_calculation_snapshot()
            if self._calculation_key(self.session.config, self.session.snapshot) != key:
                _LOG.info(
                    "JLCPCB width: inputs changed during batch (%.1fs); rescheduling",
                    elapsed,
                )
                self._schedule_width_refresh()
                return
            if failure:
                raise ValueError(failure)
            from .stackup_model import (
                calculation_fingerprint,
                merge_width_results,
                width_results_fingerprint,
            )

            config = self.session.config
            if config.stackup is None:
                raise ValueError(
                    "Nominal widths unavailable: the selected stackup changed."
                )
            if not results:
                raise ValueError(
                    "Nominal widths unavailable: JLCPCB returned an incomplete or mismatched batch."
                )
            current_jobs = {
                (spec.spec_id, layer): calculation_fingerprint(
                    config.stackup, spec, layer
                )
                for spec, layer in self._complete_width_jobs(config)
            }
            delivered = {
                (result.spec_id, result.layer): result.input_digest
                for result in results
            }
            if len(delivered) != len(results) or any(
                key not in current_jobs or delivered[key] != current_jobs[key]
                for key in delivered
            ):
                raise ValueError(
                    "Nominal widths unavailable: JLCPCB returned an incomplete or mismatched batch."
                )
            failed = [
                result
                for result in results
                if result.status not in ("success", "unsupported")
            ]
            if failed:
                reason = failed[0].message or "No usable result was returned."
                alert = f"Nominal width unavailable for {failed[0].layer}: {reason}"
                successes = tuple(
                    result
                    for result in results
                    if result.status in ("success", "unsupported")
                )
                if not successes and self.session.approved:
                    # Keep an approved workbook untouched when every job failed.
                    raise ValueError(alert)
                merged = merge_width_results(config.width_results, results)
                candidate = replace(config, width_results=merged)
                validate_storage_config(candidate, self.session.snapshot.layers)
                changed = width_results_fingerprint(
                    config.width_results
                ) != width_results_fingerprint(merged)
                _LOG.info(
                    "JLCPCB width: batch apply retained failure after %.1fs "
                    "changed=%s successes=%s failures=%s: %s",
                    elapsed,
                    changed,
                    len(successes),
                    len(failed),
                    alert,
                )
                # Cache successful layers from a mixed batch so the next open can
                # paint Differs/matches immediately. Fingerprint changes still
                # clear export approval because review digests bind nominals.
                if changed:
                    self.session.replace_calculation_context(config.stackup, merged)
                else:
                    self.session.config = candidate
                self._width_refresh_alert = alert
                if not self._refresh_rows(self.session.snapshot):
                    return
                self._populate_sections()
                return
            merged = merge_width_results(config.width_results, results)
            candidate = replace(config, width_results=merged)
            validate_storage_config(candidate, self.session.snapshot.layers)
            changed = width_results_fingerprint(
                config.width_results
            ) != width_results_fingerprint(merged)
            if changed:
                self.session.replace_calculation_context(config.stackup, merged)
            else:
                self.session.config = candidate
            self._clear_width_refresh_alert()
            _LOG.info(
                "JLCPCB width: batch applied in %.1fs changed=%s results=%s",
                elapsed,
                changed,
                len(results),
            )
            if not self._refresh_rows(self.session.snapshot):
                return
            if changed:
                self._error(
                    "Nominal widths updated. Review Target width and approve the workbook rows."
                )
        except _RowsRefreshed:
            _LOG.info(
                "JLCPCB width: board changed during apply (%.1fs); rescheduling",
                elapsed,
            )
            self._schedule_width_refresh()
        except Exception as error:
            _LOG.info(
                "JLCPCB width: batch apply failed after %.1fs: %s",
                elapsed,
                error,
            )
            self._width_refresh_alert = str(error)
            self._populate_stackup()
            self._populate_sections()

    def _cancel_width_batch(self) -> None:
        """Release only the current batch; leave the periodic retry timer alive."""
        self._calculation_generation += 1
        if self._calculation_cancel is not None:
            self._calculation_cancel.set()
            self._calculation_cancel = None
        self._active_calculation_key = None

    def _cancel_calculation(self) -> None:
        """Invalidate queued callbacks without waiting for a network worker."""
        self._cancel_width_batch()
        self._width_refresh_timer.Stop()
        self._clear_width_refresh_alert()
        if not self._closing:
            self._populate_stackup()
            self._populate_sections()

    def _on_cancel(self, event: wx.CommandEvent) -> None:
        """Discard local calculation and editing state through the normal Cancel event."""
        self._closing = True
        self._capture_prepare_timer.Stop()
        self.preview_pane.close()
        self._stop_catalog()
        self._cancel_calculation()
        event.Skip()

    def _on_close(self, event: wx.CloseEvent) -> None:
        """Prevent worker completion from touching a closing native dialog."""
        self._closing = True
        self._capture_prepare_timer.Stop()
        self.preview_pane.close()
        self._stop_catalog()
        self._cancel_calculation()
        event.Skip()

    def _on_destroy(self, event: wx.WindowDestroyEvent) -> None:
        """Cancel owned work even when native destruction bypasses modal buttons."""
        if event.GetId() == self._window_id:
            self._closing = True
            self._capture_prepare_timer.Stop()
            self.preview_pane.close()
            self._stop_catalog()
            self._cancel_calculation()
        event.Skip()

    def _required_approval_image_ids(self) -> Optional[set[str]]:
        """Require explicit stub approvals when the user includes them in review."""
        if (
            self.session.analysis is None
            or self.skip_stub_review is None
            or self.skip_stub_review()
        ):
            return None
        return {section.section_id for section in self.session.analysis.sections}

    def _populate_specifications(self) -> None:
        """Refresh the specification summary without changing the edit state."""
        from .width_checks import specification_nominal_summary

        selected = self.specifications.GetSelectedRow()
        self._review_row_labels = []
        rows: list[tuple[object, ...]] = []
        kinds = dict(KIND_CHOICES)
        analysis = self.session.analysis
        approved = self.session.approved and not self._capture_restore_pending
        included = self.session.included
        source_digest = (
            tracking.source_fingerprint(self.session.snapshot)
            if analysis is not None
            and not self._capture_restore_pending
            and self.session.config.review_tracking.layers
            else ""
        )
        for specification in self.session.config.specifications:
            layer_review_current = bool(
                analysis is not None
                and not self._capture_restore_pending
                and self.session.config.review_tracking.layers
                and tracking.specification_layer_review_current(
                    self.session.config.review_tracking,
                    source_digest,
                    specification,
                    analysis.sections,
                    stackup=self.session.config.stackup,
                    width_results=self.session.config.width_results,
                    capture_digests=self.session.capture_digests,
                    required_section_ids=self._required_approval_image_ids(),
                )
            )
            review = specification_review_label(
                specification.spec_id,
                approved=approved,
                analysis=analysis,
                included=included,
                layer_review_current=layer_review_current,
            )
            if _needs_configuration(specification, self.session.snapshot):
                review = SPEC_REVIEW_NEEDS_CONFIGURATION
            elif self._capture_restore_pending:
                review = "Verifying saved images"
            self._review_row_labels.append(review)
            rows.append(
                (
                    review,
                    specification.net_class,
                    ", ".join(
                        settings.layer for settings in specification.layer_settings
                    ),
                    specification.target_ohms,
                    kinds.get(specification.kind, specification.kind),
                    specification.label,
                    specification_nominal_summary(self.session.config, specification),
                    "; ".join(
                        f"{settings.layer}: {', '.join(settings.reference_layers)}"
                        for settings in specification.layer_settings
                    ),
                )
            )
        if self._specification_rows == tuple(rows):
            return
        if self._specification_rows is not None and len(
            self._specification_rows
        ) == len(rows):
            for index, (previous, current) in enumerate(
                zip(self._specification_rows, rows)
            ):
                for column, (old, new) in enumerate(zip(previous, current)):
                    if old != new:
                        self.specifications.SetTextValue(str(new), index, column)
            self._specification_rows = tuple(rows)
            self._update_selection_buttons()
            return
        self._specification_rows = tuple(rows)
        self.specifications.DeleteAllItems()
        for row in rows:
            self.specifications.AppendItem(list(row))
        if 0 <= selected < self.specifications.GetItemCount():
            self.specifications.SelectRow(selected)
        self._update_selection_buttons()

    def _specification_selected(self) -> bool:
        """Return whether a specification row is explicitly selected."""
        return self.specifications.GetSelectedRow() != wx.NOT_FOUND

    def _update_selection_buttons(self) -> None:
        """Edit/Remove require a selected specification."""
        enabled = self._specification_selected()
        for button in self._selection_buttons:
            button.Enable(enabled)

    def _on_specification_selection(self, event: dv.DataViewEvent) -> None:
        """Follow list selection changes without waiting for another refresh."""
        self._update_selection_buttons()
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
        self._reset_main_capture_preparation()
        self._populate_stackup()
        self._populate_sections(preferred_spec_id)
        return not self._rows_error

    def _on_retry_rows(self, event: wx.CommandEvent) -> None:
        if self._refresh_rows():
            # Width refresh can re-bind sections (and queue a non-refresh select).
            # Force the failed image last so a fresh row analysis is not treated
            # as evidence that the capture now renders.
            self._schedule_width_refresh()
            self._on_preview(self.sections.GetSelectedRow(), refresh=True)

    def _fit_workbook_columns(self) -> None:
        """Fit current headers and values, including rows outside native samples."""
        if self._closing or not self:
            return
        self.sections.fit_columns()

    def _populate_sections(self, preferred_spec_id: Optional[str] = None) -> None:
        """Present the document while retaining the selected logical row."""
        analysis = self.session.analysis
        old_index = self.sections.GetSelectedRow()
        selected_column = self.sections.GetGridCursorCol()
        scroll = self.sections.GetViewStart()
        selected_id = (
            self._row_ids[old_index] if 0 <= old_index < len(self._row_ids) else None
        )
        specifications = {
            spec.spec_id: spec for spec in self.session.config.specifications
        }
        # Group the display by inclusion without changing analysis/report order
        # or the canonical row numbers used to identify sections during review.
        rows = (
            sorted(
                enumerate(analysis.sections),
                key=lambda item: item[1].section_id not in self.session.included,
            )
            if analysis
            else ()
        )
        # Ignore value/selection notifications while rebuilding the native model;
        # handlers must never read a partially populated table or stale row IDs.
        self._updating_sections = True
        self.sections.Freeze()
        self.sections.BeginBatch()
        try:
            self._row_ids = tuple(section.section_id for _index, section in rows)
            values: list[list[object]] = []
            if analysis:
                from .width_checks import (
                    target_width_cell_text,
                    width_check,
                    width_row_mismatch,
                )

                for index, section in rows:
                    spec = specifications[section.spec_id]
                    check = width_check(
                        self.session.config, spec, section.layer, section.width_nm
                    )
                    cells = workbook_row_cells(
                        section,
                        spec,
                        target_width=target_width_cell_text(check),
                    )
                    mismatch = (
                        not section.exclusion_reason
                        and check.result_current
                        and check.target_width_nm is not None
                        and width_row_mismatch(section.width_nm, check.target_width_nm)
                    )
                    values.append(
                        [
                            section.section_id in self.session.included,
                            str(index + 1),
                            *cells[:6],
                            "!=" if mismatch else "",
                            *cells[6:],
                        ]
                    )
            self.sections.SetItems(values)
            if analysis:
                preferred = next(
                    (
                        section.section_id
                        for _index, section in rows
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
                    self.sections.SelectRow(index, column=selected_column)
                    selected_id = self._row_ids[index]
                self.preview_pane.set_sections(
                    analysis.sections, self.session.snapshot, selected_id
                )
            else:
                self.preview_pane.clear(
                    self._rows_error or "No workbook rows are available."
                )
            self._fit_workbook_columns()
        finally:
            self.sections.EndBatch()
            self.sections.Thaw()
            self._updating_sections = False
        # Native row deletion resets the cursor and viewport. Keep the user's
        # current column and scroll, revealing the selection if grouping moved it.
        self.sections.Scroll(*scroll)
        selected_row = self.sections.GetSelectedRow()
        if selected_row not in (old_index, wx.NOT_FOUND):
            self.sections.MakeCellVisible(
                selected_row, self.sections.GetGridCursorCol()
            )
        self._populate_workbook_status()

    def _populate_workbook_status(self) -> None:
        """Refresh readiness messages without rebuilding native rows or previews."""
        analysis = self.session.analysis
        self.FindWindow(wx.ID_OK).Enable(True)
        if self._width_refresh_alert:
            text = self._width_refresh_alert
        elif self._rows_error:
            text = self._rows_error + " Settings can still be saved as a draft."
        elif self.preview_pane.failure:
            text = self.preview_pane.failure
        elif any(
            _needs_configuration(spec, self.session.snapshot)
            for spec in self.session.config.specifications
        ):
            text = "Some specifications need configuration. Apply their settings when ready, or save this draft and continue later."
        elif not self.session.config.specifications:
            text = "Add a specification to create workbook rows. You can save the stackup and continue later."
        elif not analysis or not analysis.sections:
            text = "No matching workbook rows. Check the selected net classes and signal layers."
        elif self._capture_restore_pending:
            text = "Verifying saved workbook images."
        elif self.session.approved:
            text = "Workbook rows approved. Save and close when ready."
        else:
            exportable = sum(
                1 for section in analysis.sections if not section.exclusion_reason
            )
            excluded = len(analysis.sections) - exportable
            text = (
                f"{exportable} workbook rows found"
                + (
                    f", {excluded} covered stubs (unchecked by default)"
                    if excluded
                    else ""
                )
                + f"; {len(self.session.included)} included. "
                "Check the Review column for net classes that still need work, "
                "then approve the workbook rows below the list."
            )
            if analysis.warnings:
                text += "\n" + "\n".join(str(warning) for warning in analysis.warnings)
        self.status.SetValue(text)
        self._set_main_calculator_link_visible(bool(self._width_refresh_alert))
        self._populate_review_status()
        self.Layout()

    def _populate_review_status(self) -> None:
        """Derive approval controls from the document and shared image component."""
        count = len(self.session.included)
        if not self.session.approved:
            self._capture_restore_pending = False
        rows = f"{count} workbook {'row' if count == 1 else 'rows'}"
        pending = self._capture_restore_pending
        reviewed = self.session.approved and not pending
        self.review_status.SetLabel(
            "Verifying saved images"
            if pending
            else f"✓ {rows} approved"
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
            and not pending
        )
        self.retry_button.Show(bool(self._rows_error or self.preview_pane.failure))
        self.retry_button.Enable(True)
        # Draft saves remain allowed; the label must not imply export readiness.
        save = self.FindWindow(wx.ID_OK)
        if save is not None:
            save.SetLabel(
                "Save and close" if self.session.approved else "Save draft and close"
            )
            save.Enable(True)
        # Keep the Review column in sync when inclusion or approval changes without
        # a full specification-list rebuild from elsewhere.
        self._populate_specifications()

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
                calculate_width=self.calculate_width_callback,
                load_calculator_config=self.load_calculator_config,
                save_calculator_config=self.save_calculator_config,
                save_width_result=self.save_width_result,
                skip_stub_review=self.skip_stub_review,
                preserve_approved_width_results=self.session.approved,
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
            from .stackup_model import merge_width_results

            self.session.config = replace(
                self.session.config,
                review_tracking=tracking.merge_spec(
                    self.session.config.review_tracking,
                    dialog.review_tracking,
                    dialog.specification.spec_id,
                ),
                width_results=merge_width_results(
                    self.session.config.width_results, dialog.width_results
                ),
            )
            self._populate_specifications()
            self._refresh_rows(
                preferred_spec_id=dialog.specification.spec_id if changed else None
            )
            verified = getattr(dialog, "capture_digests", None)
            if verified is not None and self.session.analysis is not None:
                # Applying while restoration is still pending must not let old
                # historical records authorize pixels the child never checked.
                for section in self.session.analysis.sections:
                    if section.spec_id != dialog.specification.spec_id:
                        continue
                    if (
                        section.section_id not in verified
                        and section.section_id not in self.session.included
                        and (self.skip_stub_review is None or self.skip_stub_review())
                        and section.exclusion_reason == EXCLUSION_COVERED_STUB
                    ):
                        # Skipping an unchecked optional image must not erase
                        # historical evidence from an earlier explicit review.
                        self.session.capture_digests.pop(section.section_id, None)
                        continue
                    digest = verified.get(section.section_id)
                    if (
                        section.section_id not in verified
                        and not changed
                        and self.session.approved
                    ):
                        # Pending child warm-up is not failed pixel evidence.
                        # Keep this parent's current proof (or its pending saved
                        # verification) without granting consent to a draft.
                        continue
                    self.session.capture_digests[section.section_id] = digest
                    previous = tracking.find_image(
                        self.session.config.review_tracking,
                        section.spec_id,
                        section.layer,
                        section.section_id,
                    )
                    if digest is None or (
                        section.section_id in self.session.included
                        and (previous is None or previous.capture_digest != digest)
                    ):
                        self.session.invalidate_review()
            if self.session.approve_if_layer_reviews_complete(
                required_section_ids=self._required_approval_image_ids()
            ):
                self._populate_specifications()
                self._populate_review_status()
            self._schedule_width_refresh()
        finally:
            self._child_dialog_open = False
            dialog.Destroy()
            self._queue_selected_preview()

    def _on_add(self, event: wx.CommandEvent) -> None:
        """Add a specification after child-dialog acceptance."""
        self._edit_specification(None)

    def _on_edit(self, event: wx.Event) -> None:
        """Edit the selected specification."""
        index = self.specifications.GetSelectedRow()
        if index != wx.NOT_FOUND:
            self._edit_specification(index)

    def _on_remove(self, event: wx.CommandEvent) -> None:
        """Remove the selected rule and invalidate its scan."""
        index = self.specifications.GetSelectedRow()
        if index != wx.NOT_FOUND:
            specifications = list(self.session.config.specifications)
            del specifications[index]
            self.session.replace_specifications(tuple(specifications))
            self._populate_specifications()
            self._refresh_rows()
            self._schedule_width_refresh()

    def _on_include(self, row: int) -> None:
        """Invalidate approval when a candidate checkbox changes."""
        if (
            self._closing
            or self._updating_sections
            or self.session.analysis is None
            or not 0 <= row < len(self._row_ids)
        ):
            return
        self.session.set_included(
            section_id
            for index, section_id in enumerate(self._row_ids)
            if self.sections.GetToggleValue(index, 0)
        )
        # Commit the logical toggle before another Save/Approve/Refresh action
        # can run. Rebuild native rows after the mouse/key event has returned.
        self._populate_review_status()
        if not self._workbook_inclusion_refresh_queued:
            self._workbook_inclusion_refresh_queued = True
            wx.CallAfter(self._refresh_inclusion_rows)

    def _refresh_inclusion_rows(self) -> None:
        """Refresh inclusion guidance without touching a closing native grid."""
        self._workbook_inclusion_refresh_queued = False
        if self and not self._closing:
            self._populate_sections()

    def _on_preview(self, row: int, *, refresh: bool = False) -> None:
        """Preview the selected logical row after native selection completes."""
        if (
            not self._closing
            and not self._updating_sections
            and 0 <= row < len(self._row_ids)
        ):
            section_id = self._row_ids[row]
            index = next(
                (
                    index
                    for index, section in enumerate(self.preview_pane.sections)
                    if section.section_id == section_id
                ),
                wx.NOT_FOUND,
            )
            self.preview_pane.select(index, refresh=refresh)

    def _on_main_image_viewed(
        self, section: Section, image_sha256: str, viewed_at: str
    ) -> None:
        """Keep successfully displayed image events in the private document."""
        spec = next(
            spec
            for spec in self.session.config.specifications
            if spec.spec_id == section.spec_id
        )
        source_digest = tracking.source_fingerprint(self.session.snapshot)
        digest = tracking.capture_fingerprint(
            source_digest,
            spec,
            section,
            image_sha256,
        )
        expected = tracking.expected_capture_digest(
            self.session.config.review_tracking,
            source_digest,
            spec,
            section,
            self.session.analysis.sections if self.session.analysis is not None else (),
            stackup=self.session.config.stackup,
            width_results=self.session.config.width_results,
            workbook_captures=self.session.config.reviewed_captures,
        )
        if (
            section.section_id in self.session.included
            and expected is not None
            and expected != digest
        ):
            self.session.invalidate_review()
        self.session.capture_digests[section.section_id] = digest
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
        section = self.preview_pane.selected_section
        if section is None or section.section_id in self.session.included:
            self.session.invalidate_review()
        self._populate_review_status()
        self._error(self.preview_pane.failure)

    def _workbook_approval_captures(self) -> dict[str, str]:
        """Bind new workbook consent to current pixels without recording views."""
        analysis, snapshot = self.session.analysis, self.session.snapshot
        if analysis is None:
            raise ValueError("Workbook rows are not available.")
        sections = tuple(
            section
            for section in analysis.sections
            if section.section_id in self.session.included
        )
        specifications = {
            spec.spec_id: spec for spec in self.session.config.specifications
        }
        try:
            if self.verify_captures is not None:
                evidence = self.verify_captures(snapshot, sections)
                if not evidence.current:
                    raise ValueError(
                        "The workbook images changed. Review the current images and try again."
                    )
                hashes = evidence.hashes
            else:
                hashes = {}
                for section in sections:
                    image_hash = self.preview_pane.image_digest(section)
                    if image_hash is None:
                        capture = (
                            self.prepare_capture(snapshot, section)
                            if self.prepare_capture is not None
                            else self.preview_callback(section, refresh=False)
                        )
                        image_hash = capture.sha256
                    hashes[section] = image_hash
            if any(hashes.get(section) is None for section in sections):
                raise ValueError(
                    "A workbook image could not be verified. Retry when its source is available."
                )
            self._verify_review_snapshot()
            self.preview_pane.ensure_appearance_current()
            if not self.preview_pane.ready or self.session.analysis is not analysis:
                raise ValueError(
                    "Workbook images changed. Review the current images and try again."
                )
            source_digest = tracking.source_fingerprint(self.session.snapshot)
            return {
                section.section_id: tracking.capture_fingerprint(
                    source_digest,
                    specifications[section.spec_id],
                    section,
                    hashes[section],
                )
                for section in sections
            }
        except Exception as error:
            raise ValueError(
                str(error) or "The workbook images could not be verified. Try again."
            ) from error

    def _on_approve(self, event: wx.CommandEvent) -> None:
        """Approve the checked candidates explicitly."""
        try:
            if self._capture_restore_pending:
                raise ValueError(
                    "Wait for the saved workbook images to finish verification."
                )
            if not self.preview_pane.ready:
                raise ValueError(
                    "The preview needs to finish successfully before approval. Failed previews retry automatically."
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
            captures = self._workbook_approval_captures()
            self.session.approve(captures)
            self.session.capture_digests.update(captures)
            self._populate_sections()
        except ValueError as error:
            self._error(str(error))

    def _on_save(self, event: wx.CommandEvent) -> None:
        """Commit settings while still open; a failed save retains the working copy."""
        if self._closing:
            return
        started = time.monotonic()
        was_approved = self.session.approved
        try:
            try:
                snapshot = self.refresh_snapshot()
            except Exception:
                self._fail_rows(
                    "The current board could not be read. Retry when it is available."
                )
                raise
            snapshot_elapsed = time.monotonic() - started
            if snapshot != self.session.snapshot:
                self._refresh_rows(snapshot)
            evidence_started = time.monotonic()
            if self.verify_captures is None:
                try:
                    self.preview_pane.ensure_appearance_current()
                except Exception:
                    # An unavailable/changed palette invalidates approval, not intent.
                    self.session.invalidate_review()
            if self._capture_restore_pending:
                # Check source/appearance before waiting on old pixels. A changed
                # revision is a saveable draft, not unfinished current approval.
                self._verify_restored_capture_review(snapshot, ())
                if self.session.approved and self._capture_restore_pending:
                    self._error(
                        "Saved images are still being verified. Save when verification finishes."
                    )
                    return
            self._verify_restored_capture_review(snapshot)
            evidence_elapsed = time.monotonic() - evidence_started
            validation_started = time.monotonic()
            candidate = self.session.save_result(snapshot)
            if self.session.config.reviewed_digest and not candidate.reviewed_digest:
                self._invalidate_visual_review()
            if was_approved and not candidate.reviewed_digest:
                self._draft_save_notice_pending = True
            validation_elapsed = time.monotonic() - validation_started
            persistence_started = time.monotonic()
            if self.save_config is not None:
                self.save_config(candidate)
            persistence_elapsed = time.monotonic() - persistence_started
            # Only successful persistence may publish the result and close. A
            # standalone preview harness can omit persistence and inspect config.
            self.config = candidate
            self.session.config = candidate
            self._closing = True
            self._capture_prepare_timer.Stop()
            self.preview_pane.close()
            self._stop_catalog()
            self._cancel_calculation()
            _LOG.info(
                "Controlled impedance save: snapshot=%.3fs evidence=%.3fs "
                "validation=%.3fs storage=%.3fs total=%.3fs approved=%s",
                snapshot_elapsed,
                evidence_elapsed,
                validation_elapsed,
                persistence_elapsed,
                time.monotonic() - started,
                bool(candidate.reviewed_digest),
            )
            if self._draft_save_notice_pending and not candidate.reviewed_digest:
                try:
                    wx.MessageBox(
                        "The board, appearance, or review evidence changed after approval. "
                        "Your settings have been saved as a draft. Review the current "
                        "board before generating impedance reports.",
                        "Controlled impedance saved as draft",
                        wx.OK | wx.ICON_INFORMATION,
                        self,
                    )
                except RuntimeError:
                    _LOG.exception(
                        "The saved-draft notification could not be displayed"
                    )
            self._draft_save_notice_pending = False
            self.EndModal(wx.ID_OK)
        except Exception as error:
            if was_approved and not self.session.config.reviewed_digest:
                self._draft_save_notice_pending = True
            _LOG.info(
                "Controlled impedance save failed after %.3fs: %s",
                time.monotonic() - started,
                error,
            )
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
        self._main_capture_retry_attempts.clear()
        self._main_capture_retry_at.clear()
        self.preview_pane.clear("Review the updated workbook images.")
        self.FindWindow(wx.ID_OK).Enable(True)
        self._populate_review_status()
