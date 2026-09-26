"""Pure row mapping, comparison, and clipboard planning for assembly variants."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
import math
import re
from typing import Any, Optional, Union

from ..lcsc import is_lcsc_part, normalize_lcsc
from .native import BoardVariantSnapshot, ComponentVariantState, VariantEdit

EDITABLE_FIELDS = ("value", "lcsc", "bom", "pos", "pop")
FLAG_FIELDS = ("bom", "pos", "pop")
SHARED_COLUMN_COUNT = 5
_UNKNOWN_ASSIGNMENTS = {"invalid", "conflict", "unknown", "error", "unavailable"}
_BAD_CELL_STATUSES = {
    "pending",
    "missing",
    "unknown",
    "error",
    "invalid",
    "conflict",
    "unavailable",
}


@dataclass(frozen=True)
class ColumnSpec:
    """Describe one physical matrix column independently of the view toolkit."""

    key: str
    label: str
    variant: Optional[str]
    editable: bool = False
    tooltip: str = ""


_SHARED_COLUMNS = (
    ColumnSpec("ref", "Ref", None, tooltip="Physical component reference"),
    ColumnSpec("footprint", "Footprint", None, tooltip="Placed footprint geometry"),
    ColumnSpec("side", "Side", None, tooltip="Physical board side: T top, B bottom"),
    ColumnSpec("pcb_angle", "PCB°", None, tooltip="Raw physical PCB orientation"),
    ColumnSpec(
        "correction",
        "Corr.",
        None,
        tooltip=(
            "Rotation and XY placement correction for the selected Output variant; "
            "its LCSC rule overrides shared Default reference, Value and package rules"
        ),
    ),
)
_VARIANT_COLUMNS = (
    ("value", "Value", "Effective native Value"),
    ("params", "Params", "LCSC parameters"),
    ("lcsc", "LCSC", "Effective LCSC assignment; bent arrow: inherited from Default"),
    ("bom", "BOM", "Include in bill of materials"),
    ("pos", "POS", "Include in position file"),
    ("pop", "POP", "Populate component; disabled means DNP"),
    (
        "type",
        "Type",
        "Catalog part type: B Basic, P Preferred, E Extended, - unavailable",
    ),
    ("standard", "Std", "Standard-only assembly requirement"),
    (
        "stock",
        "Stock",
        "Stock reserve: more than 10× required quantity; hover for counts",
    ),
    (
        "price",
        "Price",
        "Price compared with the cheapest known variant; hover for amounts",
    ),
)


@dataclass(frozen=True)
class CatalogMetadata:
    """A captured catalog projection; missing values remain unknown."""

    params: str = ""
    type: str = ""
    standard: Optional[bool] = None
    stock: Optional[int] = None
    price: Optional[Union[str, float, Decimal]] = None
    status: str = "missing"
    lcsc: str = ""
    package: str = ""
    price_label: str = ""
    description: str = ""


@dataclass(frozen=True)
class StockCheck:
    """Compare catalog inventory with a variant's complete populated BOM demand."""

    sufficient: bool
    available: Optional[int]
    required: int
    threshold: int
    per_board: int
    board_count: int


@dataclass(frozen=True)
class PriceComparison:
    """Compare known costs for one physical Ref without display rounding."""

    direction: str
    amount: Optional[Decimal]
    minimum: Optional[Decimal]
    available_count: int
    variant_count: int
    premium_ratio: float = 0.0
    intensity_ratio: float = 0.0


@dataclass(frozen=True)
class CorrectionState:
    """A validated output correction with its prepared physical placement angle."""

    rotation: int = 0
    offset_x: float = 0.0
    offset_y: float = 0.0
    source: str = ""
    final_angle: float = 0.0
    status: str = "complete"

    def signature(self) -> tuple[int, float, float]:
        """Return full-precision values instead of rounded display text."""
        return self.rotation, self.offset_x, self.offset_y


_MISSING_METADATA = CatalogMetadata()
_NO_CORRECTION = CorrectionState(status="pending")
_UNAVAILABLE_CORRECTION = CorrectionState(status="unavailable")


@dataclass(frozen=True)
class CellStyle:
    """Expose independent equality, difference, provenance, and error cues."""

    matches: tuple[str, ...] = ()
    different: bool = False
    status: str = "known"
    provenance: str = ""
    warning: str = ""
    symbol: str = ""
    variant_different: bool = False


@dataclass(frozen=True)
class RowComparison:
    """Keep engineering equality separate from known field changes to Default."""

    matches: Mapping[str, tuple[str, ...]]
    changed_fields: Mapping[str, frozenset[str]]
    engineering_different: bool


@dataclass(frozen=True)
class ClipboardRow:
    """Capture a source component's exact ordered field values at copy time."""

    component_id: str
    values: tuple[tuple[str, Union[str, bool]], ...]


@dataclass(frozen=True)
class ClipboardPayload:
    """Trusted captured native values; only text and a nonce leave the controller."""

    board_id: str
    board_token: str
    rows: tuple[ClipboardRow, ...]
    cell_field: Optional[str] = None

    @property
    def plain_text(self) -> str:
        """Render the same immutable values for ordinary clipboard consumers."""
        return "\n".join(
            "\t".join(
                str(value).lower() if isinstance(value, bool) else value
                for _, value in row.values
            )
            for row in self.rows
        )


class ClipboardError(ValueError):
    """Explain invalid clipboard shapes, values, or destination identities."""


def _natural_key(value: object) -> tuple[tuple[int, object], ...]:
    """Sort references naturally while preserving deterministic mixed types."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"([0-9]+)", str(value))
    )


def _parse_external_text(field: str, value: str) -> Union[str, bool]:
    """Interpret one external cell without weakening native clipboard values."""
    if field not in EDITABLE_FIELDS:
        raise ClipboardError(f"{field!r} is a read-only paste destination")
    if "\0" in value:
        raise ClipboardError(f"{field} cannot contain a NUL character")
    if any(char in value for char in "\r\n\t"):
        raise ClipboardError(
            "External text must contain one value without tabs or line breaks"
        )
    if field in FLAG_FIELDS:
        tokens = {"true": True, "1": True, "false": False, "0": False}
        if value.strip().casefold() in tokens:
            return tokens[value.strip().casefold()]
        raise ClipboardError(f"{field.upper()} requires true/false or 1/0")
    if field == "lcsc":
        if value.strip() and not is_lcsc_part(value):
            raise ClipboardError(
                "LCSC requires a C-prefixed part number or an explicit empty value"
            )
        return normalize_lcsc(value)
    return value


def _catalog_number(value: object) -> Optional[Decimal]:
    """Reject invalid supplier numbers before display or comparison."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() and number >= 0 else None


def _stock_quantity(value: object) -> Optional[int]:
    """Inventory additionally requires a whole quantity."""
    number = _catalog_number(value)
    return (
        int(number)
        if number is not None and number == number.to_integral_value()
        else None
    )


class MatrixModel:
    """Resolve a native snapshot into aligned, sortable whole-component rows."""

    def __init__(
        self,
        snapshot: BoardVariantSnapshot,
        enrichment: Optional[Mapping[tuple[str, str], CatalogMetadata]] = None,
        corrections: Optional[Mapping[str, CorrectionState]] = None,
        *,
        board_count: int = 1,
        show_footprint_library: bool = False,
        correction_variant: str = "",
    ) -> None:
        if type(board_count) is not int or board_count < 1:
            raise ValueError("Board count must be a positive integer")
        self.board_count = board_count
        self.show_footprint_library = show_footprint_library
        self.correction_variant = correction_variant
        self.snapshot = snapshot
        self.variants = tuple(definition.name for definition in snapshot.variants)
        self.variant_order = self.variants
        self.columns = _SHARED_COLUMNS + tuple(
            ColumnSpec(key, label, variant, key in EDITABLE_FIELDS, tooltip)
            for variant in self.variants
            for key, label, tooltip in _VARIANT_COLUMNS
        )
        self._default_columns = {
            column.key: column for column in self.columns if column.variant == ""
        }
        self._all_rows = tuple(
            snapshot.get(component_id, "") for component_id in snapshot.inventory
        )
        self._metadata = {}
        for key, metadata in (enrichment or {}).items():
            state = snapshot.get(*key)
            if not state.lcsc or normalize_lcsc(metadata.lcsc) != normalize_lcsc(
                state.lcsc
            ):
                metadata = CatalogMetadata(
                    status="pending" if state.lcsc else "missing", lcsc=state.lcsc
                )
            self._metadata[key] = replace(
                metadata,
                stock=_stock_quantity(metadata.stock),
                price=_catalog_number(metadata.price),
            )
        self._stock_counts: dict[tuple[str, str], int] = {}
        for state in snapshot.components:
            if state.bom is True and state.pop is True and state.lcsc:
                key = (state.variant_name, normalize_lcsc(state.lcsc))
                self._stock_counts[key] = self._stock_counts.get(key, 0) + 1
        self._price_comparisons: dict[str, dict[str, PriceComparison]] = {}
        self._corrections = dict(corrections or {})
        self._comparisons = {
            row.component_id: self._compare_row(row) for row in self._all_rows
        }
        self._filter = (False, False, False, False)
        self._sort = (0, False)
        self.rows = self._all_rows
        self._rebuild_rows()

    def _metadata_for(self, component_id: str, variant: str) -> CatalogMetadata:
        """Read facts already joined to this model's captured native assignments."""
        return self._metadata.get((component_id, variant), _MISSING_METADATA)

    def _correction_for(self, component_id: str) -> CorrectionState:
        """Read prepared output results without substituting a missing variant."""
        if self.correction_variant not in self.variants:
            return _UNAVAILABLE_CORRECTION
        return self._corrections.get(component_id, _NO_CORRECTION)

    def _correction_status(self, component_id: str) -> str:
        """Keep unavailable or invalid placement data visibly distinct from zero."""
        correction = self._correction_for(component_id)
        if correction.status != "complete":
            return correction.status
        return "known" if math.isfinite(correction.final_angle) else "invalid"

    def _engineering_signature(
        self, component_id: str, variant: str
    ) -> Optional[tuple[Any, ...]]:
        """Compare normalized native fields; invalid assignments cannot confirm matches."""
        state = self.snapshot.get(component_id, variant)
        if state.assignment.status in _UNKNOWN_ASSIGNMENTS:
            return None
        return (
            state.value,
            normalize_lcsc(state.lcsc),
            state.bom,
            state.pos,
            state.pop,
        )

    def _compare_row(self, row: ComponentVariantState) -> RowComparison:
        """Calculate named engineering matches and per-variant changed fields once."""
        signatures = {
            name: self._engineering_signature(row.component_id, name)
            for name in self.variants
        }
        matches = {
            name: tuple(
                other
                for other, candidate in signatures.items()
                if candidate == signature
            )
            if signature is not None
            else ()
            for name, signature in signatures.items()
        }
        changed = {name: set() for name in self.variants}
        for column in self.columns:
            if column.variant is not None and self._field_differs(row, column):
                changed[column.variant].add(column.key)
        return RowComparison(
            matches,
            {name: frozenset(keys) for name, keys in changed.items()},
            None in signatures.values() or len(set(signatures.values())) > 1,
        )

    def _field_differs(self, row: ComponentVariantState, column: ColumnSpec) -> bool:
        """Compare known values with Default; Std requires population on both sides."""
        if not column.variant or self._cell_status(row, column) != "known":
            return False
        if column.key == "standard" and any(
            self.snapshot.get(row.component_id, variant).pop is not True
            for variant in ("", column.variant)
        ):
            return False
        default = self._default_columns[column.key]
        if self._cell_status(row, default) != "known":
            return False
        current = self._value(row, column)
        base = self._value(row, default)
        return current != base

    def row_for_component(self, component_id: str) -> Optional[int]:
        """Find a visible row by stable identity after sorting or filtering."""
        return next(
            (
                index
                for index, row in enumerate(self.rows)
                if row.component_id == component_id
            ),
            None,
        )

    def column_for(self, variant: Optional[str], key: str) -> int:
        """Resolve columns by explicit variant and stable semantic field key."""
        return next(
            index
            for index, column in enumerate(self.columns)
            if column.variant == variant and column.key == key
        )

    def variant_label(self, variant: Optional[str]) -> str:
        """Preserve native UI labels independently of canonical variant identity."""
        if variant is None:
            return "Shared"
        return {item.name: item.label for item in self.snapshot.variants}[variant]

    def set_variant_order(self, order: object) -> bool:
        """Move complete presentation groups while retaining native identities."""
        names: list[str] = []
        if isinstance(order, (list, tuple)):
            for name in order:
                if (
                    isinstance(name, str)
                    and name in self.variants
                    and name not in names
                ):
                    names.append(name)
        names.extend(name for name in self.variants if name not in names)
        normalized = tuple(names)
        if normalized == self.variant_order:
            return False

        sort_column, descending = self._sort
        sorted_field = self.columns[sort_column]
        groups: dict[str, list[ColumnSpec]] = {name: [] for name in self.variants}
        for column in self.columns[SHARED_COLUMN_COUNT:]:
            groups[column.variant].append(column)
        self.columns = self.columns[:SHARED_COLUMN_COUNT] + tuple(
            column for name in normalized for column in groups[name]
        )
        self.variant_order = normalized
        self._sort = (
            self.column_for(sorted_field.variant, sorted_field.key),
            descending,
        )
        self._rebuild_rows()
        return True

    def set_filter(
        self,
        *,
        require_bom: bool = False,
        require_pos: bool = False,
        require_pop: bool = False,
        differences_only: bool = False,
    ) -> None:
        """Retain rows when one variant satisfies all requested predicates."""
        self._filter = (require_bom, require_pos, require_pop, differences_only)
        self._rebuild_rows()

    def sort_by(self, column: int, descending: bool = False) -> None:
        """Sort entire physical rows using one shared mapping for all groups."""
        self.columns[column]
        self._sort = (column, descending)
        self._rebuild_rows()

    def _rebuild_rows(self) -> None:
        """Reapply whole-row visibility and stable sorting without native reads."""
        bom, pos, pop, differences = self._filter
        requirements = tuple(
            field for field, needed in zip(FLAG_FIELDS, (bom, pos, pop)) if needed
        )
        selected = [
            row
            for row in self._all_rows
            if (
                not differences
                or self._comparisons[row.component_id].engineering_different
            )
            and any(
                all(
                    getattr(self.snapshot.get(row.component_id, variant), field) is True
                    for field in requirements
                )
                for variant in self.variants
            )
        ]
        column, descending = self._sort
        spec = self.columns[column]

        def sort_key(row: ComponentVariantState) -> tuple[Any, ...]:
            value = self._value(row, spec)
            if spec.key == "footprint" and not self.show_footprint_library:
                value = self._display_text(value, spec.key)
            if isinstance(value, CorrectionState):
                value = value.signature()
            if isinstance(value, (bool, int, float, Decimal)):
                comparable = (0, value)
            elif isinstance(value, tuple):
                comparable = (1, value)
            else:
                comparable = (2, _natural_key(value))
            return (
                value is None,
                comparable,
                _natural_key(row.reference),
                row.component_id,
            )

        if spec.key == "correction":
            # Unknown physical placement follows known values in either direction.
            known = [
                row
                for row in selected
                if self._correction_status(row.component_id) == "known"
            ]
            unavailable = [
                row
                for row in selected
                if self._correction_status(row.component_id) != "known"
            ]
            self.rows = tuple(
                sorted(known, key=sort_key, reverse=descending)
                + sorted(
                    unavailable,
                    key=lambda row: (_natural_key(row.reference), row.component_id),
                )
            )
        else:
            self.rows = tuple(sorted(selected, key=sort_key, reverse=descending))

    def _value(self, row: ComponentVariantState, column: ColumnSpec) -> object:
        """Read captured native values and independent catalog projections."""
        if column.key == "correction":
            return self._correction_for(row.component_id)
        if column.variant is None:
            return row.reference if column.key == "ref" else getattr(row, column.key)
        state = self.snapshot.get(row.component_id, column.variant)
        if column.key in EDITABLE_FIELDS:
            return getattr(state, column.key)
        metadata = self._metadata_for(row.component_id, column.variant)
        return getattr(metadata, column.key)

    def get_value(self, row: int, column: int) -> object:
        """Return the exact captured cell value before display formatting."""
        return self._value(self.rows[row], self.columns[column])

    def _display_text(self, value: object, key: str) -> str:
        """Keep empty text and unknown placeholders consistent in cells and sizing."""
        if key == "footprint" and not self.show_footprint_library and value is not None:
            value = str(value).rsplit(":", 1)[-1]
        if value is None or value == "":
            return "" if key in ("value", "lcsc") else "—"
        return str(value)

    def column_display_samples(self, column: int) -> tuple[str, ...]:
        """Sample each physical row once, independent of filtering and sorting.

        Only the three compactable text fields participate. Catalog text goes
        through the same assignment-validity checks as the displayed cell, and
        repeated strings remain repeated so statistics retain row weighting.
        """
        spec = self.columns[column]
        if spec.key not in ("footprint", "value", "params"):
            raise ValueError("Column does not support compact text statistics")
        return tuple(
            self._display_text(self._value(row, spec), spec.key)
            for row in self._all_rows
        )

    def column_display_markers(self, column: int) -> tuple[str, ...]:
        """Reserve only actual markers, including those on currently hidden rows."""
        spec = self.columns[column]
        if spec.key not in ("footprint", "value", "params"):
            raise ValueError("Column does not support compact text statistics")
        return tuple(self._cell_style(row, spec).symbol for row in self._all_rows)

    def stock_check(self, row: int, column: int) -> StockCheck:
        """Include hidden same-LCSC rows; reject stale or invalid stock quantities."""
        physical, spec = self.rows[row], self.columns[column]
        state = self.snapshot.get(physical.component_id, spec.variant)
        per_board = self._stock_counts.get(
            (spec.variant, normalize_lcsc(state.lcsc)), 0
        )
        required = self.board_count * per_board
        available = self._metadata_for(physical.component_id, spec.variant).stock
        return StockCheck(
            available is not None and available > 10 * required,
            available,
            required,
            10 * required,
            per_board,
            self.board_count,
        )

    def price_comparison(self, row: int, column: int) -> PriceComparison:
        """Rank against priced variants; never turn unavailable costs into zero."""
        component_id = self.rows[row].component_id
        if component_id not in self._price_comparisons:
            amounts = {
                name: self._metadata_for(component_id, name).price
                for name in self.variants
            }
            known = [amount for amount in amounts.values() if amount is not None]
            minimum = min(known) if known else None
            positive = [amount for amount in known if amount > 0]
            minimum_positive = min(positive) if positive else None
            all_equal = len(known) >= 2 and minimum == max(known)
            comparisons = {}
            for name, amount in amounts.items():
                premium = 0.0
                intensity = 0.0
                if amount is None or len(known) < 2:
                    direction = "unknown"
                elif all_equal:
                    direction = "same"
                elif amount == minimum:
                    direction = "cheapest"
                else:
                    direction = "higher"
                    premium = (
                        float((amount - minimum) / minimum) if minimum else math.inf
                    )
                    # Percent premiums are undefined above zero; still show
                    # different red intensities using the cheapest paid option.
                    intensity = premium if minimum else float(amount / minimum_positive)
                comparisons[name] = PriceComparison(
                    direction,
                    amount,
                    minimum,
                    len(known),
                    len(self.variants),
                    premium,
                    intensity,
                )
            self._price_comparisons[component_id] = comparisons
        return self._price_comparisons[component_id][self.columns[column].variant]

    def get_display(self, row: int, column: int) -> str:
        """Format values without changing comparison precision or native data."""
        spec = self.columns[column]
        value = self.get_value(row, column)
        if spec.key in FLAG_FIELDS:
            return "1" if value is True else "0" if value is False else "?"
        if spec.key == "stock":
            return "✓" if self.stock_check(row, column).sufficient else "⚠"
        if spec.key == "price":
            return {"cheapest": "↓", "higher": "↑", "same": "-", "unknown": "?"}[
                self.price_comparison(row, column).direction
            ]
        if spec.key == "type":
            catalog_type = str(value or "").strip()
            return {
                "basic": "B",
                "preferred": "P",
                "extended": "E",
                "": "-",
                "—": "-",
                "-": "-",
            }.get(catalog_type.casefold(), catalog_type)
        if spec.key == "side":
            return {
                "top": "T",
                "front": "T",
                "t": "T",
                "bottom": "B",
                "back": "B",
                "bot": "B",
                "b": "B",
            }.get(str(value).casefold(), str(value))
        if value is None or value == "":
            return self._display_text(value, spec.key)
        if spec.key == "pcb_angle":
            return f"{value:g}°"
        if isinstance(value, CorrectionState):
            if self._correction_status(self.rows[row].component_id) != "known":
                return "Unavailable"
            return f"{value.rotation:g}°, {value.offset_x:g}/{value.offset_y:g}"
        if spec.key == "standard":
            return "✓" if value is True else "—"
        return self._display_text(value, spec.key)

    def _cell_status(self, row: ComponentVariantState, column: ColumnSpec) -> str:
        """Keep unknown values neutral instead of treating placeholders as matches."""
        if column.key == "correction":
            return self._correction_status(row.component_id)
        if column.variant is None:
            return "known"
        state = self.snapshot.get(row.component_id, column.variant)
        if column.key in EDITABLE_FIELDS:
            if column.key == "lcsc" and state.assignment.status in _UNKNOWN_ASSIGNMENTS:
                return state.assignment.status
            return "known"
        metadata = self._metadata_for(row.component_id, column.variant)
        if column.key == "standard" and metadata.status in _BAD_CELL_STATUSES:
            return metadata.status
        value = self._value(row, column)
        if value is None or value == "":
            return (
                metadata.status if metadata.status in _BAD_CELL_STATUSES else "missing"
            )
        return "known"

    def _warning(self, row: ComponentVariantState, variant: str) -> str:
        """Surface native footprint-field metadata mismatches with placed geometry."""
        state = self.snapshot.get(row.component_id, variant)
        field = getattr(state, "footprint_field", "")
        if field and field != row.footprint:
            return f"Variant Footprint field {field!r} differs from placed geometry {row.footprint!r}"
        return ""

    def cell_style(self, row: int, column: int) -> CellStyle:
        """Return independent field and block differences, status, and provenance."""
        return self._cell_style(self.rows[row], self.columns[column])

    def _cell_style(
        self, physical: ComponentVariantState, spec: ColumnSpec
    ) -> CellStyle:
        """Read styling by physical identity for visible cells and hidden samples."""
        status = self._cell_status(physical, spec)
        if spec.variant is None:
            return CellStyle(status=status, symbol="?" if status != "known" else "")
        comparison = self._comparisons[physical.component_id]
        changed_fields = comparison.changed_fields[spec.variant]
        different = spec.key in changed_fields
        provenance = ""
        if spec.key == "lcsc":
            native = self.snapshot.get(physical.component_id, spec.variant)
            provenance = (
                "base"
                if not spec.variant
                else "inherited"
                if native.assignment.inherited
                else "explicit"
            )
        return CellStyle(
            comparison.matches[spec.variant] if status == "known" else (),
            different,
            status,
            provenance,
            self._warning(physical, spec.variant),
            "≠" if different else "?" if status != "known" else "",
            bool(changed_fields),
        )

    def cell_tooltip(self, row: int, column: int) -> str:
        """Expose qualified footprint hover text only when the library is hidden."""
        if self.columns[column].key == "footprint" and self.show_footprint_library:
            return ""
        return self.cell_details(row, column)

    def cell_details(self, row: int, column: int) -> str:
        """Provide full values, provenance, and placement details accessibly."""
        physical = self.rows[row]
        spec = self.columns[column]
        style = self.cell_style(row, column)
        value = self.get_value(row, column)
        variant = self.variant_label(spec.variant)
        details = [f"{physical.reference} · {variant} · {spec.label}", str(value)]
        if spec.key == "lcsc" and value:
            description = self._metadata_for(
                physical.component_id, spec.variant
            ).description
            details.append(
                description
                if isinstance(description, str) and description.strip()
                else "Description unavailable"
            )
        elif spec.key == "stock":
            check = self.stock_check(row, column)
            details = [
                details[0],
                f"Available stock: {check.available}"
                if check.available is not None
                else "Available stock: unknown or invalid",
                f"Required: {check.per_board} per board × {check.board_count} boards = {check.required}",
                f"Green check requires more than {check.threshold} available (10× required)",
                "Counts all populated, BOM-included uses of this LCSC number in this variant; display filters and POS do not change demand.",
            ]
            if check.required == 0:
                details.append("No populated BOM demand for this part in this variant")
        elif spec.key == "price":
            comparison = self.price_comparison(row, column)
            details = [
                details[0],
                f"BOM price (USD, current board count): ${comparison.amount:f}"
                if comparison.amount is not None
                else "BOM price unavailable",
            ]
            metadata = self._metadata_for(physical.component_id, spec.variant)
            if metadata.price_label:
                details.append(f"Estimator label: {metadata.price_label}")
            if comparison.direction == "higher":
                premium = (
                    f"{comparison.premium_ratio * 100:.2f}% above the cheapest known price"
                    if math.isfinite(comparison.premium_ratio)
                    else "Higher than the zero-priced minimum"
                )
                details.append(premium)
                if comparison.minimum == 0:
                    details.append(f"Price increase: ${comparison.amount:f}")
            elif comparison.direction == "cheapest":
                details.append("Cheapest known variant price (including ties)")
            elif comparison.direction == "same":
                details.append("All known variant prices are equal")
            elif comparison.amount is not None:
                details.append("No other known variant price to compare")
            if comparison.minimum is not None:
                details.append(f"Cheapest known price: ${comparison.minimum:f}")
            details.append(
                f"Compared {comparison.available_count} priced variants; "
                f"{comparison.variant_count - comparison.available_count} unavailable"
            )
        elif isinstance(value, CorrectionState):
            output_label = (
                self.variant_label(self.correction_variant)
                if self.correction_variant in self.variants
                else self.correction_variant
            )
            details = [
                f"{physical.reference} · Output: {output_label} · {spec.label}",
                f"Placement correction for selected Output variant: {output_label}.",
                "An exact LCSC rule for this variant overrides shared pattern rules.",
                f"Pattern fallback uses {self.variant_label('')} reference, Value and package.",
            ]
            if style.status == "known":
                details.extend(
                    (
                        f"Rotation {value.rotation!r}°; X {value.offset_x!r} mm; Y {value.offset_y!r} mm",
                        f"Rule source: {value.source or 'none'}; final CPL angle: {value.final_angle!r}°",
                    )
                )
            else:
                details.append(
                    "Correction unavailable; no valid placement angle can be derived."
                )
        if style.provenance == "inherited":
            details.append("Inherited from Default")
        elif style.provenance:
            details.append(f"LCSC: {style.provenance}")
        if style.matches:
            if len(style.matches) > 1:
                details.append(
                    "Matches: " + ", ".join(map(self.variant_label, style.matches))
                )
                details.append("Same Value, LCSC, BOM/POS/POP.")
            else:
                details.append("Matches: no other confirmed variants")
        if style.different:
            details.append("Different from Default")
        if style.status != "known":
            details.append(f"Status: {style.status}")
        if style.warning:
            details.append(style.warning)
        if spec.variant is not None:
            metadata = self._metadata_for(physical.component_id, spec.variant)
            if metadata.package:
                details.append(f"Catalog package: {metadata.package}")
        return "\n".join(details)

    def copy_cell(self, row: int, column: int) -> ClipboardPayload:
        """Copy one exact editable value or readable text for a derived cell."""
        physical, spec = self.rows[row], self.columns[column]
        if spec.editable:
            payload = self.copy_block(
                (physical.component_id,), spec.variant, fields=(spec.key,)
            )
            return replace(payload, cell_field=spec.key)
        text = (
            str(self.get_value(row, column) or "")
            if spec.key == "footprint"
            else self.get_display(row, column)
        )
        captured = ClipboardRow(physical.component_id, ((spec.key, text),))
        return ClipboardPayload(
            self.snapshot.board_id,
            self.snapshot.board_token,
            (captured,),
            spec.key,
        )

    def copy_block(
        self,
        component_ids: Sequence[str],
        source_variant: str,
        fields: Sequence[str] = EDITABLE_FIELDS,
    ) -> ClipboardPayload:
        """Capture GUI-selected fields from normalized native state, not live links."""
        captured = []
        for component_id in component_ids:
            native = self.snapshot.get(component_id, source_variant)
            if "lcsc" in fields and native.assignment.status in _UNKNOWN_ASSIGNMENTS:
                raise ClipboardError(
                    f"LCSC is {native.assignment.status}; resolve the unavailable assignment before copying"
                )
            values = tuple((field, getattr(native, field)) for field in fields)
            captured.append(ClipboardRow(component_id, values))
        return ClipboardPayload(
            self.snapshot.board_id,
            self.snapshot.board_token,
            tuple(captured),
        )

    def _validate_payload(self, payload: ClipboardPayload) -> None:
        """Check whether captured internal values still belong to this board."""
        if (
            payload.board_id != self.snapshot.board_id
            or payload.board_token != self.snapshot.board_token
        ):
            raise ClipboardError(
                "Clipboard belongs to a different board or board lifetime"
            )
        if payload.cell_field is not None and payload.cell_field not in EDITABLE_FIELDS:
            raise ClipboardError(
                "Read-only cells can be copied as text but cannot be pasted into native fields"
            )

    def _edits(
        self, destinations: tuple[tuple[str, ClipboardRow, str], ...]
    ) -> tuple[VariantEdit, ...]:
        """Resolve the complete native target batch before any mutation begins."""
        return tuple(
            VariantEdit(self.snapshot.target(component, variant), source.values)
            for component, source, variant in destinations
        )

    def plan_paste(
        self,
        payload: ClipboardPayload,
        component_ids: Sequence[str],
        destination_variant: str,
        destination_field: Optional[str] = None,
    ) -> tuple[VariantEdit, ...]:
        """Map a cell or a defined block shape without positional row guessing."""
        self._validate_payload(payload)
        ids = tuple(component_ids)
        if destination_field is not None and payload.cell_field != destination_field:
            raise ClipboardError("Source and destination fields are incompatible")
        if len(ids) != len(payload.rows):
            raise ClipboardError("Clipboard and destination row counts do not match")
        if len(ids) == 1:
            sources = ((ids[0], payload.rows[0], destination_variant),)
        else:
            by_id = {row.component_id: row for row in payload.rows}
            if set(ids) != set(by_id):
                raise ClipboardError(
                    "Multi-row paste requires matching component identities; use an explicit single-row copy"
                )
            sources = tuple(
                (component_id, by_id[component_id], destination_variant)
                for component_id in ids
            )
        return self._edits(sources)

    def plan_copy_to_variants(
        self,
        payload: ClipboardPayload,
        destination_variants: Sequence[str],
    ) -> tuple[VariantEdit, ...]:
        """Copy explicit source identities to the exact chosen variant product."""
        self._validate_payload(payload)
        if not destination_variants:
            raise ClipboardError("Select at least one destination variant")
        destinations = tuple(
            (row.component_id, row, variant)
            for row in payload.rows
            for variant in destination_variants
        )
        return self._edits(destinations)

    def plan_text_paste(
        self,
        text: str,
        component_id: str,
        variant: str,
        field: str,
    ) -> tuple[VariantEdit, ...]:
        """Validate external single-cell text against its explicit destination."""
        value = _parse_external_text(field, text)
        return (
            VariantEdit(self.snapshot.target(component_id, variant), ((field, value),)),
        )
