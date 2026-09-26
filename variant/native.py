"""Explicit, immutable assembly views over KiCad's native variant records.

The adapter never selects a global editor variant or reads assignments from a
database. Its mutation boundary validates captured targets, preserves unrelated
native state, verifies read-back, and compensates partial failures. ``SetModified``
marks native items; editor undo/save lifecycle remains the action plugin's job.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Optional, Union

from ..footprint_metadata import get_footprint_pad_metadata
from ..lcsc import is_lcsc_part, normalize_lcsc

FieldValue = Union[str, bool]
EDITABLE_FIELDS = frozenset(("value", "lcsc", "bom", "pos", "pop"))
# KiCad footprint attributes: EXCLUDE_FROM_BOM, EXCLUDE_FROM_POS_FILES, DNP.
# Bit 32 is FP_JUST_ADDED, not population state.
FLAG_MASKS = {"bom": 8, "pos": 4, "pop": 64}


class NativeVariantError(RuntimeError):
    """Native state could not be read or changed safely."""


class StaleVariantTarget(NativeVariantError):
    """A captured board, component, or source revision is no longer current."""


@dataclass(frozen=True)
class VariantDefinition:
    """Native canonical name plus its independent presentation strings."""

    name: str
    label: str
    description: str = ""


@dataclass(frozen=True)
class AssignmentAlias:
    """One raw native assignment field and whether its value is inherited."""

    name: str
    text: str
    inherited: bool


@dataclass(frozen=True)
class ResolvedAssignment:
    """Assignment provenance without collapsing absence, blanks, or conflicts."""

    status: str
    inherited: bool
    aliases: tuple[AssignmentAlias, ...]


@dataclass(frozen=True)
class VariantTarget:
    """Captured component/variant identity and its native source revision."""

    board_id: str
    board_token: str
    component_id: str
    variant_name: str
    source_revision: str


@dataclass(frozen=True)
class VariantEdit:
    """A finite explicit field update or request to restore field inheritance."""

    target: VariantTarget
    changes: tuple[tuple[str, FieldValue], ...] = ()
    use_base: tuple[str, ...] = ()


@dataclass(frozen=True)
class ComponentVariantState:
    """Effective assembly data and shared placement for one physical component."""

    component_id: str
    reference: str
    variant_name: str
    value: str
    lcsc: str
    assignment: ResolvedAssignment
    bom: bool
    pos: bool
    pop: bool
    footprint: str
    side: str
    pcb_angle: float
    x_mm: float
    y_mm: float
    pad_count: int
    has_tht: bool
    attributes: int
    source_revision: str
    footprint_field: str


@dataclass(frozen=True)
class BoardVariantSnapshot:
    """One complete, immutable board inventory across all native variants."""

    board_id: str
    board_token: str
    source_token: str
    variants: tuple[VariantDefinition, ...]
    components: tuple[ComponentVariantState, ...]
    inventory: tuple[str, ...]
    native_variant_name: str

    _index: Mapping[tuple[str, str], ComponentVariantState] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        """Index a complete native inventory once, rejecting ambiguous targets."""
        names = tuple(variant.name for variant in self.variants)
        if not names or names[0] != "" or len(set(names)) != len(names):
            raise NativeVariantError(
                "The complete native variant inventory is required"
            )
        if len(set(self.inventory)) != len(self.inventory) or not all(self.inventory):
            raise NativeVariantError("The native component inventory is ambiguous")
        index = {(s.component_id, s.variant_name): s for s in self.components}
        if len(index) != len(self.components) or set(index) != {
            (component, name) for component in self.inventory for name in names
        }:
            raise NativeVariantError("Read every component and variant together")
        references = {(s.variant_name, s.reference) for s in self.components}
        if len(references) != len(self.components) or any(
            not s.reference or not s.source_revision for s in self.components
        ):
            raise NativeVariantError("Native references or revisions are ambiguous")
        object.__setattr__(self, "_index", MappingProxyType(index))

    def get(self, component_id: str, variant_name: str) -> ComponentVariantState:
        """Return an exact target, rejecting missing or renamed components."""
        try:
            return self._index[(component_id, variant_name)]
        except KeyError:
            raise StaleVariantTarget(
                f"Component/variant is unavailable: {component_id}/{variant_name}"
            ) from None

    def for_variant(self, variant_name: str) -> tuple[ComponentVariantState, ...]:
        """Return one variant's complete physical inventory, including DNPs."""
        if variant_name not in {variant.name for variant in self.variants}:
            raise StaleVariantTarget(f"Variant is unavailable: {variant_name}")
        return tuple(
            self._index[(component, variant_name)] for component in self.inventory
        )

    def target(self, component_id: str, variant_name: str) -> VariantTarget:
        """Capture an edit target without relying on a visible row index."""
        state = self.get(component_id, variant_name)
        return VariantTarget(
            self.board_id,
            self.board_token,
            component_id,
            variant_name,
            state.source_revision,
        )


@dataclass(frozen=True)
class _Record:
    """Raw fields and exclusion bits, retaining all attributes for Default."""

    fields: tuple[tuple[str, str], ...]
    attributes: int


@dataclass(frozen=True)
class _Write:
    component_id: str
    variant_name: str
    before: Optional[_Record]
    after: Optional[_Record]


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _uuid(item: Any) -> str:
    value = getattr(item, "m_Uuid", None)
    if value is None or not callable(getattr(value, "AsString", None)):
        raise NativeVariantError("KiCad component UUID access is unavailable")
    result = str(value.AsString())
    if not result:
        raise NativeVariantError("KiCad returned an empty component UUID")
    return result


def native_board_identity(board: Any) -> str:
    """Identify a board lifetime, including duplicate SWIG proxies of one board."""
    pointer = board
    seen: set[int] = set()
    while hasattr(pointer, "this") and id(pointer) not in seen:
        seen.add(id(pointer))
        pointer = pointer.this
    try:
        address = int(pointer)
    except (TypeError, ValueError):
        # Stateful test doubles and non-SWIG wrappers retain ordinary object identity.
        address = id(board)
    return _digest((address, _uuid(board), str(board.GetFileName())))


def _required(item: Any, name: str, *args: Any) -> Any:
    method = getattr(item, name, None)
    if not callable(method):
        raise NativeVariantError(f"Required KiCad API is unavailable: {name}")
    return method(*args)


def _fields(fp: Any) -> dict[str, str]:
    return {str(f.GetName()): str(f.GetText()) for f in _required(fp, "GetFields")}


def _record(fp: Any, name: str) -> Optional[_Record]:
    """Capture raw Default or named state; None means no named record exists."""
    if not name:
        return _Record(tuple(sorted(_fields(fp).items())), int(fp.GetAttributes()))
    variant = _required(fp, "GetVariant", name)
    if variant is None:
        return None
    fields = tuple(sorted((str(k), str(v)) for k, v in variant.GetFields().items()))
    return _Record(
        fields,
        FLAG_MASKS["pop"] * bool(variant.GetDNP())
        | FLAG_MASKS["bom"] * bool(variant.GetExcludedFromBOM())
        | FLAG_MASKS["pos"] * bool(variant.GetExcludedFromPosFiles()),
    )


def _assignment_name(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", name.casefold())
    return bool(
        re.fullmatch(
            r"(?:lcsc|jlcpcb|jlc)(?:part(?:number|num|no)?|pn|number|code|id)?",
            normalized,
        )
    )


def _assignment_names(fields: dict[str, str], overrides: dict[str, str]) -> set[str]:
    # Prefixes alone also match unrelated metadata such as "JLCPCB Rotation".
    # Recognition must depend on names, never current values: an assignment
    # remains an assignment after both base and override are cleared/reloaded.
    return {name for name in (*fields, *overrides) if _assignment_name(name)}


def _alias_order(name: str) -> tuple[bool, str, str]:
    return name.casefold() != "lcsc", name.casefold(), name


def resolve_assignment(
    fields: dict[str, str], overrides: dict[str, str], variant_name: str
) -> tuple[ResolvedAssignment, str]:
    """Resolve aliases once for reading and editing, retaining explicit blanks.

    A named variant's explicit assignment aliases shadow its inherited aliases.
    Multiple occupied aliases with different values are a conflict, never a
    first-match catalog lookup. A present canonical empty field is intentional.
    Supported names are LCSC, JLC, or JLCPCB with an optional Part, Part Number,
    Part Num, Part No, PN, Number, Code, or ID suffix, ignoring punctuation,
    whitespace, and case. Other prefixed metadata is never an assignment.
    """
    names = sorted(_assignment_names(fields, overrides), key=_alias_order)
    aliases = tuple(
        AssignmentAlias(
            n,
            overrides.get(n, fields.get(n, "")),
            bool(variant_name and n not in overrides),
        )
        for n in names
    )
    if not aliases:
        return ResolvedAssignment("missing", bool(variant_name), ()), ""
    explicit = tuple(a for a in aliases if a.name in overrides) if variant_name else ()
    active = explicit or aliases
    first = active[0]
    normalized = tuple(normalize_lcsc(a.text) for a in active)
    occupied = {text for text in normalized if text}
    if len(occupied) > 1 or (first.text == "" and occupied):
        status, lcsc = "conflict", ""
    elif first.text == "":
        status, lcsc = "empty", ""
    elif is_lcsc_part(normalized[0]):
        status, lcsc = "valid", normalized[0]
    else:
        status, lcsc = "invalid", ""
    return ResolvedAssignment(status, first.inherited, aliases), lcsc


class VariantNativeAdapter:
    """Operate on captured component/variant targets against one live board."""

    def __init__(
        self,
        board: Any,
        board_id: str,
        variant_factory: Optional[Callable[[str], Any]] = None,
    ) -> None:
        if not board_id:
            raise NativeVariantError("A runtime board identity is required")
        self.board = board
        self.board_id = board_id
        self.board_token = native_board_identity(board)
        self.variant_factory = variant_factory
        self.unreliable = False

    def is_current_board(self, board: Any) -> bool:
        """Check whether a board handle denotes this adapter's board lifetime."""
        return native_board_identity(board) == self.board_token

    def variants(self) -> tuple[VariantDefinition, ...]:
        """Read native definitions with the translated Default entry mapped to empty."""
        names = [str(n) for n in _required(self.board, "GetVariantNamesForUI")]
        if not names:
            raise NativeVariantError("KiCad returned no Default variant entry")
        # The UI list is sorted with the translated Default sentinel first. Check
        # native membership, never parse the translated display label.
        if _required(self.board, "HasVariant", names[0]):
            raise NativeVariantError(
                "KiCad's Default variant entry could not be identified"
            )
        result = [VariantDefinition("", names[0])]
        seen: set[str] = set()
        for name in names[1:]:
            if (
                not name
                or name.casefold() in seen
                or not _required(self.board, "HasVariant", name)
            ):
                raise NativeVariantError(
                    f"Invalid or duplicate native variant definition: {name}"
                )
            seen.add(name.casefold())
            desc = str(_required(self.board, "GetVariantDescription", name))
            result.append(VariantDefinition(name, name, desc))
        return tuple(result)

    def _footprints(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for fp in _required(self.board, "GetFootprints"):
            component_id = _uuid(fp)
            if component_id in result:
                raise NativeVariantError(
                    f"Duplicate native component UUID: {component_id}"
                )
            result[component_id] = fp
        return result

    def _state(
        self,
        fp: Any,
        component_id: str,
        name: str,
        fields: dict[str, str],
        attributes: int,
        geometry: tuple[Any, ...],
    ) -> ComponentVariantState:
        record = _record(fp, name) if name else None
        overrides = dict(record.fields) if record else {}
        assignment, lcsc = resolve_assignment(fields, overrides, name)
        value_name = str(fp.Value().GetName())
        if name:
            value = str(_required(fp, "GetFieldValueForVariant", name, value_name))
            # Read every assignment alias through the effective native getter too.
            for alias in assignment.aliases:
                actual = str(_required(fp, "GetFieldValueForVariant", name, alias.name))
                if actual != alias.text:
                    raise NativeVariantError(
                        "Native field and override read-back disagree"
                    )
            dnp = bool(_required(fp, "GetDNPForVariant", name))
            bom = not bool(_required(fp, "GetExcludedFromBOMForVariant", name))
            pos = not bool(_required(fp, "GetExcludedFromPosFilesForVariant", name))
        else:
            value = str(fp.GetValue())
            dnp = bool(fp.IsDNP())
            bom = not bool(attributes & FLAG_MASKS["bom"])
            pos = not bool(attributes & FLAG_MASKS["pos"])
        revision = _digest(
            (
                str(fp.GetReference()),
                fields,
                attributes,
                asdict(record) if record else None,
                geometry,
            )
        )
        return ComponentVariantState(
            component_id,
            str(fp.GetReference()),
            name,
            value,
            lcsc,
            assignment,
            bom,
            pos,
            not dnp,
            *geometry,
            attributes,
            revision,
            overrides.get("Footprint", fields.get("Footprint", geometry[0])),
        )

    def snapshot(self) -> BoardVariantSnapshot:
        """Read all physical components and every native variant without mutation."""
        try:
            if not self.is_current_board(self.board):
                raise StaleVariantTarget(
                    "The board identity or filename changed; reopen the plugin"
                )
            definitions = self.variants()
            footprints = self._footprints()
            inventory = tuple(sorted(footprints))
            states_list = []
            for component_id in inventory:
                fp = footprints[component_id]
                fields = _fields(fp)
                attributes = int(fp.GetAttributes())
                point = fp.GetPosition()
                geometry = (
                    str(fp.GetFPIDAsString()),
                    "BOT" if fp.IsFlipped() else "TOP",
                    float(fp.GetOrientationDegrees()),
                    float(point.x) / 1_000_000,
                    float(point.y) / 1_000_000,
                    *get_footprint_pad_metadata(fp),
                )
                states_list.extend(
                    self._state(
                        fp, component_id, variant.name, fields, attributes, geometry
                    )
                    for variant in definitions
                )
            states = tuple(states_list)
            current = str(self.board.GetCurrentVariant())
            timestamp = int(self.board.GetTimeStamp())
            source_token = _digest(
                (
                    self.board_token,
                    [asdict(v) for v in definitions],
                    [
                        (s.component_id, s.variant_name, s.source_revision)
                        for s in states
                    ],
                    timestamp,
                )
            )
            if definitions != self.variants() or inventory != tuple(
                sorted(self._footprints())
            ):
                raise StaleVariantTarget(
                    "Native variant/component inventory changed during refresh"
                )
            return BoardVariantSnapshot(
                self.board_id,
                self.board_token,
                source_token,
                definitions,
                states,
                inventory,
                current,
            )
        except NativeVariantError:
            raise
        except Exception as exc:
            raise NativeVariantError(
                f"Unable to read native assembly state: {exc}"
            ) from exc

    def _prepare(
        self, edit: VariantEdit, snapshot: BoardVariantSnapshot, fp: Any
    ) -> _Write:
        target = edit.target
        if (target.board_id, target.board_token) != (self.board_id, self.board_token):
            raise StaleVariantTarget("The edit belongs to a different board lifetime")
        state = snapshot.get(target.component_id, target.variant_name)
        if state.source_revision != target.source_revision:
            raise StaleVariantTarget(
                f"{state.reference}/{target.variant_name}: source changed; refresh"
            )
        changes = dict(edit.changes)
        resets = set(edit.use_base)
        if len(changes) != len(edit.changes) or len(resets) != len(edit.use_base):
            raise NativeVariantError("An edit contains duplicate field keys")
        if (set(changes) | resets) - EDITABLE_FIELDS or set(changes) & resets:
            raise NativeVariantError("Unknown or conflicting editable field keys")
        if resets and (not target.variant_name or resets - {"value", "lcsc"}):
            raise NativeVariantError(
                "Use base is supported only for named-variant Value/LCSC fields"
            )
        for key, value in changes.items():
            if key in FLAG_MASKS:
                if type(value) is not bool:
                    raise NativeVariantError(
                        f"{key} requires a boolean inclusion value"
                    )
            elif not isinstance(value, str):
                raise NativeVariantError(f"{key} requires text")
            elif "\x00" in value:
                raise NativeVariantError(f"{key} cannot contain a NUL character")
            elif key == "lcsc" and value and not is_lcsc_part(value):
                raise NativeVariantError("LCSC must be empty or a C followed by digits")
        base = _record(fp, "")
        assert base is not None  # Only named records may be absent.
        before = _record(fp, target.variant_name) if target.variant_name else base
        fields = dict(before.fields) if before else {}
        # A new named record snapshots just the three effective exclusion flags;
        # Default and existing named records retain their captured raw attributes.
        attributes = (
            before.attributes if before else base.attributes & sum(FLAG_MASKS.values())
        )
        value_name = str(fp.Value().GetName())
        aliases = _assignment_names(dict(base.fields), fields)
        for key in resets:
            for name in aliases if key == "lcsc" else (value_name,):
                fields.pop(name, None)
        for key, value in changes.items():
            if key in FLAG_MASKS:
                mask = FLAG_MASKS[key]
                attributes = attributes & ~mask if value else attributes | mask
            else:
                names = set(aliases) if key == "lcsc" else {value_name}
                if key == "lcsc" and not names:
                    names.add("LCSC")
                for name in names:
                    fields[name] = normalize_lcsc(value) if key == "lcsc" else value
        after = (
            _Record(tuple(sorted(fields.items())), attributes)
            if before is not None or changes
            else None
        )
        return _Write(
            target.component_id,
            target.variant_name,
            before,
            after,
        )

    def _make_variant(self, name: str, record: _Record) -> Any:
        factory = self.variant_factory
        if factory is None:
            import pcbnew  # pylint: disable=import-error,import-outside-toplevel

            factory = pcbnew.FOOTPRINT_VARIANT
        variant = factory(name)
        variant.SetDNP(bool(record.attributes & FLAG_MASKS["pop"]))
        variant.SetExcludedFromBOM(bool(record.attributes & FLAG_MASKS["bom"]))
        variant.SetExcludedFromPosFiles(bool(record.attributes & FLAG_MASKS["pos"]))
        for key, value in record.fields:
            variant.SetFieldValue(key, value)
        return variant

    def _write(self, fp: Any, write: _Write, restore: bool = False) -> None:
        record = write.before if restore else write.after
        if write.variant_name:
            if record is None:
                if _record(fp, write.variant_name) is not None:
                    _required(fp, "DeleteVariant", write.variant_name)
            else:
                _required(
                    fp, "SetVariant", self._make_variant(write.variant_name, record)
                )
            return
        assert record is not None  # Default always has fields and attributes.
        desired = dict(record.fields)
        current = _fields(fp)
        for key in current.keys() - desired.keys():
            _required(fp, "Remove", _required(fp, "GetField", key))
        for key, value in desired.items():
            if key not in current or current[key] != value:
                _required(fp, "SetField", key, value)
                if key not in current:
                    field = _required(fp, "GetField", key)
                    _required(field, "SetVisible", False)
        if int(fp.GetAttributes()) != record.attributes:
            _required(fp, "SetAttributes", record.attributes)

    def _matches(self, fp: Any, write: _Write, restore: bool = False) -> bool:
        return _record(fp, write.variant_name) == (
            write.before if restore else write.after
        )

    def apply_edits(self, edits: Sequence[VariantEdit]) -> BoardVariantSnapshot:
        """Validate the whole batch, write native state first, then verify it."""
        if self.unreliable:
            raise NativeVariantError(
                "An earlier native rollback failed; reopen and inspect the board"
            )
        before = self.snapshot()
        footprints = self._footprints()
        prepared: list[_Write] = []
        seen: set[tuple[str, str]] = set()
        for edit in edits:
            key = (edit.target.component_id, edit.target.variant_name)
            if key in seen:
                raise NativeVariantError(
                    "Combine changes for each component/variant into one edit"
                )
            seen.add(key)
            if edit.target.component_id not in footprints:
                raise StaleVariantTarget("The target component no longer exists")
            write = self._prepare(edit, before, footprints[edit.target.component_id])
            if write.before != write.after:
                prepared.append(write)
        applied: list[_Write] = []
        try:
            for write in prepared:
                applied.append(write)  # A native setter may mutate and then fail.
                fp = footprints[write.component_id]
                self._write(fp, write)
                if not self._matches(fp, write):
                    raise NativeVariantError(
                        "Native setter did not preserve the requested state"
                    )
            if prepared:
                for component_id in {w.component_id for w in prepared}:
                    _required(footprints[component_id], "SetModified")
            after = self.snapshot()
            for edit in edits:
                state = after.get(edit.target.component_id, edit.target.variant_name)
                expected_fields = dict(edit.changes)
                if edit.use_base:
                    base = after.get(edit.target.component_id, "")
                    expected_fields.update(
                        (key, getattr(base, key)) for key in edit.use_base
                    )
                for key, expected in expected_fields.items():
                    if key == "lcsc":
                        expected = normalize_lcsc(expected)
                    if getattr(state, key) != expected:
                        raise NativeVariantError(
                            f"{state.reference}/{state.variant_name or 'base'}: "
                            f"effective {key} read-back did not preserve the requested value"
                        )
            return after
        except Exception as exc:
            errors: list[str] = []
            for write in reversed(applied):
                try:
                    fp = footprints[write.component_id]
                    self._write(fp, write, restore=True)
                    if not self._matches(fp, write, restore=True):
                        raise NativeVariantError("restored state failed read-back")
                except Exception as rollback_exc:
                    errors.append(
                        f"{write.component_id}/{write.variant_name}: {rollback_exc}"
                    )
            self.unreliable = bool(errors)
            if errors:
                raise NativeVariantError(
                    "Native edit failed and recovery is incomplete; generation disabled. "
                    + "; ".join(errors)
                ) from exc
            raise NativeVariantError(
                f"Native edit failed; original native state restored: {exc}"
            ) from exc
