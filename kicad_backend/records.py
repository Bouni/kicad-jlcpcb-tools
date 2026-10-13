"""Immutable facts about the open board, the same whichever backend read them.

Records hold KiCad's raw facts only, and only the facts the plugin reads:
field text, the assembly flags, the side and the pads' kinds. Interpreting
them (assignment aliases, LCSC numbers) is the job of shared code.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType


@dataclass(frozen=True)
class BoardInfo:
    """The open board."""

    board_token: str  # identity of this open board; changes when another opens
    file_name: str  # absolute .kicad_pcb path


@dataclass(frozen=True)
class FieldRecord:
    """One footprint field."""

    name: str
    text: str


@dataclass(frozen=True)
class PadRecord:
    """One footprint pad."""

    kind: str  # "pth" | "smd" | "conn" | "npth"
    has_hole: bool


@dataclass(frozen=True)
class ComponentState:
    """A footprint's fields and assembly flags in one variant.

    A footprint can hold two fields with one name: KiCad's footprint dialog
    accepts a second, and the footprint keeps both until the board is
    reopened. Reopening keeps one field of each name, in the first one's
    place, with the last one's text. ``fields`` holds the fields as reopening
    leaves them; ``replaced_fields`` holds the ones it replaces, in the order
    it replaces them.

    Build states with ``component_state``.
    """

    fields: Mapping[str, FieldRecord]  # by name, as reopening the board leaves them
    dnp: bool
    exclude_from_bom: bool
    exclude_from_pos: bool
    replaced_fields: tuple[FieldRecord, ...] = ()

    def __post_init__(self) -> None:
        """Freeze the fields."""
        fields = dict(self.fields)
        if any(name != record.name for name, record in fields.items()):
            raise ValueError("Component fields must be keyed by their names")
        replaced = tuple(self.replaced_fields)
        if any(record.name not in fields for record in replaced):
            raise ValueError("A replaced field needs a later field of its name")
        object.__setattr__(self, "fields", MappingProxyType(fields))
        object.__setattr__(self, "replaced_fields", replaced)

    @property
    def duplicate_names(self) -> tuple[str, ...]:
        """Return each name more than one field holds, in the order each first repeats."""
        return tuple(dict.fromkeys(record.name for record in self.replaced_fields))


def component_state(
    fields: Iterable[FieldRecord],
    *,
    dnp: bool = False,
    exclude_from_bom: bool = False,
    exclude_from_pos: bool = False,
) -> ComponentState:
    """Build a state from fields in KiCad's order.

    A name that repeats resolves as KiCad resolves it on reopening the
    board: the later field replaces the earlier one in its place. The
    replaced fields are kept, so the state shows the name was repeated.
    """
    by_name: dict[str, FieldRecord] = {}
    replaced: list[FieldRecord] = []
    for record in fields:
        if record.name in by_name:
            replaced.append(by_name[record.name])
        by_name[record.name] = record
    return ComponentState(
        by_name,
        bool(dnp),
        bool(exclude_from_bom),
        bool(exclude_from_pos),
        tuple(replaced),
    )


@dataclass(frozen=True)
class ComponentRecord:
    """One footprint: identity, side, pads and its state in each variant."""

    uuid: str
    reference: str
    footprint_lib_id: str  # "Lib:Name"
    schematic_path: str  # the symbol's KIID path as KiCad reports it
    side: str  # "top" | "bottom"
    pads: tuple[PadRecord, ...]
    states: Mapping[str, ComponentState]  # "" is Default; one entry per variant
    key: str = ""  # unique in its snapshot; see footprint_keys

    def __post_init__(self) -> None:
        """Freeze the pads and states; a footprint's key is its UUID by default."""
        object.__setattr__(self, "pads", tuple(self.pads))
        object.__setattr__(self, "states", MappingProxyType(dict(self.states)))
        if not self.key:
            object.__setattr__(self, "key", self.uuid)

    @property
    def default(self) -> ComponentState:
        """Return the footprint's own state, which every variant starts from."""
        return self.states[""]


def footprint_keys(uuids: Iterable[str]) -> list[str]:
    """Key footprints given in board order by their UUIDs, keeping each apart.

    KiCad gives every footprint its own UUID, but a board file can hold two
    footprints with one, and KiCad keeps both when it opens the file. The
    first footprint with a UUID is keyed by it; the nth is keyed by it with
    "#n". A UUID holds no "#", so no key is another footprint's UUID.
    """
    counts: dict[str, int] = {}
    keys = []
    for uuid in uuids:
        counts[uuid] = counts.get(uuid, 0) + 1
        keys.append(uuid if counts[uuid] == 1 else f"{uuid}#{counts[uuid]}")
    return keys


@dataclass(frozen=True)
class VariantDefinition:
    """Native canonical name plus its independent presentation strings."""

    name: str
    label: str
    description: str = ""


@dataclass(frozen=True)
class BoardSnapshot:
    """Every footprint of the open board, in every variant the snapshot holds.

    Components keep the order the backend reports them in. For KiCad that is
    the board's own footprint order, the order its reference lookups search.
    Each is keyed as ``footprint_keys`` keys it in that order.
    """

    info: BoardInfo
    variants: tuple[VariantDefinition, ...]  # Default ("") first
    components: tuple[ComponentRecord, ...]

    _by_key: Mapping[str, ComponentRecord] = field(
        init=False, repr=False, compare=False
    )
    _by_reference: Mapping[str, tuple[ComponentRecord, ...]] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        """Reject an incomplete or ambiguous inventory, then index it."""
        variants = tuple(self.variants)
        components = tuple(self.components)
        names = [variant.name for variant in variants]
        if not names or names[0] != "" or len(set(names)) != len(names):
            raise ValueError("A snapshot lists Default first and each variant once")
        by_key: dict[str, ComponentRecord] = {}
        by_reference: dict[str, list[ComponentRecord]] = {}
        keys = footprint_keys(component.uuid for component in components)
        for key, component in zip(keys, components):
            if not component.uuid:
                raise ValueError("A snapshot requires a UUID for every footprint")
            if set(component.states) != set(names):
                raise ValueError(
                    f"Footprint {component.uuid} needs one state per variant"
                )
            if component.key != key:
                component = replace(component, key=key)
            by_key[key] = component
            by_reference.setdefault(component.reference, []).append(component)
        object.__setattr__(self, "variants", variants)
        object.__setattr__(self, "components", tuple(by_key.values()))
        object.__setattr__(self, "_by_key", MappingProxyType(by_key))
        object.__setattr__(
            self,
            "_by_reference",
            MappingProxyType(
                {ref: tuple(found) for ref, found in by_reference.items()}
            ),
        )

    def get(self, key: str) -> ComponentRecord:
        """Return the footprint with this key; KeyError when there is none."""
        return self._by_key[key]

    def by_reference(self, reference: str) -> tuple[ComponentRecord, ...]:
        """Return the footprints with this reference, in board order."""
        return self._by_reference.get(reference, ())
