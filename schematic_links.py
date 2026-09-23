"""Index native PCB symbol links through the actual schematic hierarchy."""

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
import os
from types import MappingProxyType
from typing import Optional
import unicodedata

from .part_assignments import ResolvedAssignment, resolve_assignment
from .schematic_fields import _arguments, _children, _Form, _forms

TargetKey = tuple[str, str]


@dataclass(frozen=True)
class SymbolTarget:
    """One physical placed symbol and all its actual hierarchy instance paths."""

    file_path: str
    symbol_uuid: str
    reference: str
    fields: Mapping[str, str]
    assignment: ResolvedAssignment
    lcsc: str
    in_bom: Optional[bool]  # noqa: UP045
    multi_unit: bool = False
    instance_paths: tuple[str, ...] = ()

    @property
    def key(self) -> TargetKey:
        """Return the physical identity shared by repeated sheet instances."""
        return self.file_path, self.symbol_uuid


@dataclass(frozen=True)
class SymbolInstance:
    """One physical symbol's effective annotation in an actual hierarchy context."""

    path: str
    target: SymbolTarget
    reference: str
    unit: int


@dataclass(frozen=True)
class ComponentResolution:
    """All placed units authenticated by one PCB anchor, or preservation reasons."""

    members: tuple[SymbolInstance, ...]
    issues: tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        """Require a nonempty, fully authenticated placed-unit closure."""
        return bool(self.members) and not self.issues

    @property
    def key(self) -> tuple[str, ...]:
        """Identify the same component regardless of which unit the PCB links to."""
        return tuple(member.path for member in self.members)


@dataclass(frozen=True)
class _Annotation:
    """A stored annotation; project labels never select its sheet context."""

    sheet_path: str
    reference: str
    unit: int


@dataclass(frozen=True)
class _SymbolDetails:
    """Native package metadata kept separate from physical assignment fields."""

    library_id: str
    library_name: str
    unit_count: int
    default_unit: int
    annotations: tuple[_Annotation, ...]
    has_instances: bool
    issues: tuple[str, ...]

    @property
    def family(self) -> str:
        """Allow edited cache definitions while retaining original library identity."""
        return self.library_id or self.library_name


@dataclass(frozen=True)
class _Occurrence:
    """Retain even ambiguous placed records so closure cannot silently omit them."""

    path: str
    target: SymbolTarget
    details: _SymbolDetails


@dataclass(frozen=True)
class _InstanceState:
    """Resolved annotations and any obstacle to using them as package identity."""

    member: SymbolInstance
    details: _SymbolDetails
    scope: str
    issues: tuple[str, ...]
    unit_issues: tuple[str, ...]
    multi_unit: bool


@dataclass(frozen=True)
class _Sheet:
    """One placed sheet; its UUID differs from the child file's root UUID."""

    uuid: str
    filename: str
    ambiguous: bool


@dataclass(frozen=True)
class _Document:
    """Read-only parsed fields needed for a hierarchy walk."""

    root_uuid: str
    symbols: tuple[SymbolTarget, ...]
    details: tuple[_SymbolDetails, ...]
    sheets: tuple[_Sheet, ...]
    ambiguous_uuids: frozenset[str]


def _single_argument(text: str, forms: Iterable[_Form], name: str) -> str:
    """Read an unambiguous scalar property, refusing repeated declarations."""
    values = [_arguments(text, form) for form in forms if form.name == name]
    return values[0][0].text if len(values) == 1 and len(values[0]) == 1 else ""


def _fields(text: str, forms: Iterable[_Form]) -> dict[str, str]:
    """Read direct fields without treating variant or library fields as base."""
    fields: dict[str, str] = {}
    for form in forms:
        args = _arguments(text, form)
        if form.name == "property" and len(args) >= 2:
            name, value = args[0].text, args[1].text
            if name in fields:
                raise ValueError(f"Duplicate schematic property '{name}'")
            fields[name] = value
    return fields


def _placed_library_name(text: str, forms: Iterable[_Form]) -> str:
    """Use KiCad's local cache binding, falling back only when lib_name is absent."""
    bindings: dict[str, str] = {}
    for form in forms:
        if form.name not in {"lib_name", "lib_id"}:
            continue
        arguments = _arguments(text, form)
        if form.name in bindings or len(arguments) != 1 or not arguments[0].text:
            raise ValueError("Ambiguous or invalid placed-symbol library binding")
        bindings[form.name] = arguments[0].text
    return bindings.get("lib_name", bindings.get("lib_id", ""))


def _multi_unit_libraries(text: str, forms: Iterable[_Form]) -> set[str]:
    """Identify libraries that can share one footprint across multiple units."""
    multi_unit: set[str] = set()
    for form in forms:
        if form.name == "lib_symbols":
            for library in _children(text, form):
                name = _arguments(text, library)
                if library.name != "symbol" or not name:
                    continue
                for unit in _children(text, library):
                    args = _arguments(text, unit)
                    if unit.name == "symbol" and args:
                        suffix = args[0].text.rsplit("_", 2)
                        if (
                            len(suffix) == 3
                            and suffix[1].isdigit()
                            and int(suffix[1]) > 1
                        ):
                            multi_unit.add(name[0].text)
        elif form.name == "symbol":
            members = tuple(_children(text, form))
            unit_number = _single_argument(text, members, "unit")
            if unit_number.isdigit() and int(unit_number) > 1:
                multi_unit.add(_placed_library_name(text, members))
    return multi_unit


def _cached_unit_counts(text: str, forms: Iterable[_Form]) -> dict[str, int]:
    """Read each cached definition's unit count; zero means ambiguous or unknown."""
    counts: dict[str, int] = {}
    for form in forms:
        if form.name != "lib_symbols":
            continue
        for library in _children(text, form):
            names = _arguments(text, library)
            if library.name != "symbol" or len(names) != 1:
                continue
            name = names[0].text
            if name in counts:
                counts[name] = 0
                continue
            numbers = []
            for unit in _children(text, library):
                args = _arguments(text, unit)
                if unit.name == "symbol" and len(args) == 1:
                    suffix = args[0].text.rsplit("_", 2)
                    if len(suffix) == 3 and suffix[1].isdigit():
                        numbers.append(int(suffix[1]))
            counts[name] = max(numbers, default=0)
    return counts


def _positive_unit(value: str) -> int:
    """Return a unit number without turning malformed annotations into unit one."""
    return int(value) if value.isdigit() and int(value) > 0 else 0


def _symbol_details(
    text: str,
    members: tuple[_Form, ...],
    counts: Mapping[str, int],
) -> _SymbolDetails:
    """Read direct annotation records without trusting the serialized default view."""
    name = _placed_library_name(text, members)
    instance_forms = [form for form in members if form.name == "instances"]
    annotations = []
    issues = []
    for instances in instance_forms:
        for project in _children(text, instances):
            if project.name != "project":
                continue
            for path in _children(text, project):
                if path.name != "path":
                    continue
                args = _arguments(text, path)
                if len(args) != 1 or not args[0].text.startswith("/"):
                    issues.append("ambiguous stored instance path")
                    continue
                fields = tuple(_children(text, path))
                annotations.append(
                    _Annotation(
                        args[0].text,
                        _single_argument(text, fields, "reference"),
                        _positive_unit(_single_argument(text, fields, "unit")),
                    )
                )
    default_unit = (
        _positive_unit(_single_argument(text, members, "unit"))
        if any(form.name == "unit" for form in members)
        else 1
    )
    return _SymbolDetails(
        _single_argument(text, members, "lib_id"),
        name,
        counts.get(name, 0),
        default_unit,
        tuple(annotations),
        bool(instance_forms),
        tuple(issues),
    )


def _parse(text: str, file_path: str) -> _Document:
    """Validate one whole document and retain only direct placed symbols/sheets."""
    # UTF-8 BOMs are legal at the start of a file, but not S-expression tokens.
    start = 1 if text.startswith("\ufeff") else 0
    roots = tuple(_forms(text, start, len(text)))
    if len(roots) != 1 or roots[0].name != "kicad_sch":
        raise ValueError(f"Expected one complete kicad_sch document: {file_path}")
    children = tuple(_children(text, roots[0]))
    root_uuid = _single_argument(text, children, "uuid")
    if not root_uuid:
        raise ValueError(f"Schematic root UUID is absent or ambiguous: {file_path}")
    multi_unit = _multi_unit_libraries(text, children)
    counts = _cached_unit_counts(text, children)
    placed = [form for form in children if form.name in {"symbol", "sheet"}]
    uuids = [_single_argument(text, _children(text, form), "uuid") for form in placed]
    ambiguous = frozenset(uuid for uuid, count in Counter(uuids).items() if count > 1)
    symbols: list[SymbolTarget] = []
    details: list[_SymbolDetails] = []
    sheets: list[_Sheet] = []
    for form, uuid in zip(placed, uuids):
        members = tuple(_children(text, form))
        fields = _fields(text, members)
        if form.name == "sheet":
            filenames = [
                value.strip()
                for name, value in fields.items()
                if name.casefold() in {"sheetfile", "sheet file"}
            ]
            if len(filenames) != 1 or not filenames[0]:
                raise ValueError(f"Sheet file is absent or ambiguous in: {file_path}")
            sheets.append(_Sheet(uuid, filenames[0], not uuid or uuid in ambiguous))
        elif any(member.name in {"lib_id", "lib_name"} for member in members):
            assignment, lcsc = resolve_assignment(fields, {}, "")
            bom = _single_argument(text, members, "in_bom")
            symbols.append(
                SymbolTarget(
                    file_path,
                    uuid,
                    fields.get("Reference", ""),
                    MappingProxyType(fields),
                    assignment,
                    lcsc,
                    {"yes": True, "no": False}.get(bom),
                    _placed_library_name(text, members) in multi_unit,
                )
            )
            details.append(_symbol_details(text, members, counts))
    return _Document(
        root_uuid, tuple(symbols), tuple(details), tuple(sheets), ambiguous
    )


def _annotated_reference(reference: str) -> bool:
    """Require an annotation, rather than a prefix or an unannotated question mark."""
    return bool(reference) and reference[-1].isdigit() and "?" not in reference


def _instance_state(
    occurrence: _Occurrence,
    targets: Mapping[TargetKey, SymbolTarget],
    links: Mapping[str, SymbolTarget],
    shared_project: bool,
) -> _InstanceState:
    """Read GetRef/GetUnitSelection evidence for this exact rooted sheet path."""
    target = targets.get(occurrence.target.key, occurrence.target)
    details = occurrence.details
    sheet_path = occurrence.path.rsplit("/", 1)[0]
    current = {
        (record.reference.casefold(), record.unit): record
        for record in details.annotations
        if record.sheet_path == sheet_path
    }
    issues = list(details.issues)
    unit_issues = []
    if details.default_unit <= 0 or (
        details.unit_count > 0 and details.default_unit > details.unit_count
    ):
        unit_issues.append("invalid placed unit")
    current_units = {record.unit for record in current.values()}
    if len(current_units) > 1:
        unit_issues.append("conflicting current instance units")
    if any(
        number <= 0 or (details.unit_count > 0 and number > details.unit_count)
        for number in current_units
    ):
        unit_issues.append("invalid current instance unit")
    if len(current) == 1:
        record = next(iter(current.values()))
        reference, unit = record.reference, record.unit
    elif current:
        reference, unit = "", 0
        issues.append("conflicting annotation records for the current sheet path")
    elif not details.has_instances and len(target.instance_paths) == 1:
        # Older/simple documents can have no instance table. The serialized
        # default is safe only when this physical symbol has one actual context.
        reference, unit = target.reference, details.default_unit
    else:
        reference, unit = "", 0
        issues.append("no annotation record for the current sheet path")
    if not _annotated_reference(reference):
        issues.append("missing or incomplete instance reference")
    if unit <= 0:
        issues.append("missing or invalid instance unit")
    if occurrence.path not in links:
        issues.append("ambiguous or missing symbol identity in the hierarchy")
    return _InstanceState(
        SymbolInstance(occurrence.path, target, reference, unit),
        details,
        "" if shared_project else occurrence.path.split("/", 2)[1],
        tuple(issues + unit_issues),
        tuple(unit_issues),
        target.multi_unit or unit > 1 or any(number > 1 for number in current_units),
    )


def _component_resolutions(
    occurrences: Iterable[_Occurrence],
    targets: Mapping[TargetKey, SymbolTarget],
    links: Mapping[str, SymbolTarget],
    shared_project: bool,
) -> dict[str, ComponentResolution]:
    """Authenticate native packages before any caller considers physical writes.

    KiCad collects units by effective reference across the project hierarchy.
    Edited units may use different cache names; their original library identity
    and cached unit counts must still agree. Missing annotation in a compatible
    family prevents proving the placed-unit closure and preserves that family.
    """
    unique = {(item.path, item.target.key, item.details): item for item in occurrences}
    states = [
        _instance_state(item, targets, links, shared_project)
        for item in unique.values()
    ]
    by_reference: dict[tuple[str, str], list[_InstanceState]] = defaultdict(list)
    unknown: dict[tuple[str, str], list[_InstanceState]] = defaultdict(list)
    multi_unit_families = {
        (state.scope, state.details.family) for state in states if state.multi_unit
    }
    for state in states:
        if _annotated_reference(state.member.reference):
            by_reference[(state.scope, state.member.reference.casefold())].append(state)
        else:
            unknown[(state.scope, state.details.family)].append(state)

    resolutions = {}
    for anchor in states:
        if anchor.member.path not in links:
            continue
        group = by_reference.get(
            (anchor.scope, anchor.member.reference.casefold()), [anchor]
        )
        candidates = list(group)
        for state in group:
            candidates.extend(unknown[(state.scope, state.details.family)])
        candidates = list({id(state): state for state in candidates}.values())
        multi_unit = any(
            state.multi_unit
            or (
                state.details.unit_count == 0
                and (state.scope, state.details.family) in multi_unit_families
            )
            for state in candidates
        )
        if not multi_unit:
            # Single-unit UUID identity requires no reference-based expansion.
            resolutions[anchor.member.path] = ComponentResolution(
                (anchor.member,),
                anchor.unit_issues,
            )
            continue
        candidates.sort(key=lambda state: state.member.path)
        issues = [
            f"{state.member.path}: {issue}"
            for state in candidates
            for issue in state.issues
        ]
        families = {state.details.family for state in candidates}
        counts = {state.details.unit_count for state in candidates}
        if len(families) != 1 or "" in families:
            issues.append("component units have incompatible library identities")
        if len(counts) != 1 or 0 in counts:
            issues.append(
                "component units have unknown or incompatible cached unit counts"
            )
        units = [state.member.unit for state in candidates]
        if len(units) != len(set(units)):
            issues.append("component has duplicate effective unit identities")
        if any(
            not 1 <= state.member.unit <= state.details.unit_count
            for state in candidates
        ):
            issues.append("component unit is outside its cached library definition")
        resolutions[anchor.member.path] = ComponentResolution(
            tuple(state.member for state in candidates), tuple(dict.fromkeys(issues))
        )
    return resolutions


@dataclass(frozen=True)
class SchematicIndex:
    """A complete immutable hierarchy snapshot keyed by physical symbol identity.

    Canonical links contain the top-level file's root UUID, followed by placed
    sheet UUIDs and the placed symbol UUID. KiCad 7–9 rootless links resolve only
    when they identify one canonical instance. Child files' root UUIDs and saved
    project names never select links. Component expansion separately authenticates
    effective references using exact current sheet-instance paths. Ambiguous
    identities never resolve, even if one candidate shares a reference.
    """

    targets: Mapping[TargetKey, SymbolTarget]
    texts: Mapping[str, str]
    file_paths: Mapping[str, str]
    encountered_paths: tuple[str, ...]
    issues: tuple[str, ...]
    _links: Mapping[str, SymbolTarget]
    _canonical_links: Mapping[str, str]
    _components: Mapping[str, ComponentResolution]

    @classmethod
    def from_paths(
        cls,
        paths: Iterable[str],
        *,
        shared_project: bool = False,
        root_uuids: Optional[Mapping[str, str]] = None,
    ) -> "SchematicIndex":
        """Read every root and child before exposing any usable symbol targets.

        Opened aliases are retained for lock checks. Relative sheet files are
        resolved beside the opened path, matching KiCad, while physical paths
        group writes. Missing files, malformed documents, and hierarchy cycles
        raise before callers can prepare or write a partial hierarchy.

        ``shared_project`` authenticates the selected paths as top-level roots
        with a common reference namespace, even when also used as child sheets.
        Arbitrarily selected paths are reduced to independent hierarchy roots.
        ``root_uuids`` supplies authenticated project-declared root IDs, which
        KiCad 10 applies instead of those stored in the corresponding files.
        """
        return _IndexBuilder().build(
            paths,
            shared_project=shared_project,
            root_uuids=root_uuids or {},
        )

    def resolve(self, path: str) -> Optional[SymbolTarget]:  # noqa: UP045
        """Resolve an exact native footprint path without reference fallbacks."""
        canonical = self.canonical_link(path)
        return self._links.get(canonical) if canonical is not None else None

    def canonical_link(self, path: str) -> Optional[str]:  # noqa: UP045
        """Identify one rooted instance for either native path generation."""
        return self._canonical_links.get(path)

    def resolve_component(self, path: str) -> ComponentResolution:
        """Expand an exact native footprint link into all authenticated placed units."""
        canonical = self.canonical_link(path)
        if canonical is None:
            return ComponentResolution(
                (), ("schematic link is missing, stale or ambiguous",)
            )
        return self._components[canonical]


class _IndexBuilder:
    """Collect mutable walk state before publishing an immutable index."""

    def __init__(self) -> None:
        self.texts: dict[str, str] = {}
        self.file_paths: dict[str, str] = {}
        self.canonical_paths: dict[str, str] = {}
        self.directory_names: dict[str, tuple[str, ...]] = {}
        self.documents: dict[str, _Document] = {}
        self.encountered: dict[str, None] = {}
        self.issues: list[str] = []
        self.targets: dict[TargetKey, SymbolTarget] = {}
        self.instances: dict[TargetKey, set[str]] = defaultdict(set)
        self.links: dict[str, set[TargetKey]] = defaultdict(set)
        self.blocked: set[str] = set()
        self.ancestors: set[tuple[str, str]] = set()
        self.descendants: dict[str, frozenset[str]] = {}
        self.occurrences: list[_Occurrence] = []

    def _canonical_path(self, opened: str) -> str:
        """Resolve spelling aliases while keeping separate hardlink entries apart.

        realpath alone retains caller casing on case-insensitive filesystems.
        Inode identity alone instead merges hardlinks, which atomic replacement
        writes independently. Resolve each existing directory entry's spelling;
        an exact entry always wins, including Foo/foo on case-sensitive volumes.
        """
        real = os.path.realpath(opened)
        if real in self.canonical_paths:
            return self.canonical_paths[real]
        drive, remainder = os.path.splitdrive(real)
        current = drive + os.sep
        for part in remainder.strip(os.sep).split(os.sep):
            if not part:
                continue
            if current not in self.directory_names:
                with os.scandir(current) as entries:
                    self.directory_names[current] = tuple(
                        entry.name for entry in entries
                    )
            names = self.directory_names[current]
            exact = os.path.join(current, part)
            if part not in names:
                folded = unicodedata.normalize("NFC", part).casefold()
                candidates = [
                    name
                    for name in names
                    if unicodedata.normalize("NFC", name).casefold() == folded
                    and os.path.samefile(os.path.join(current, name), exact)
                ]
                if len(candidates) > 1:
                    raise ValueError(f"Ambiguous schematic path spelling: {opened}")
                if candidates:
                    part = candidates[0]
            current = os.path.join(current, part)
        self.canonical_paths[real] = current
        return current

    def _read(self, opened: str) -> tuple[str, _Document]:
        """Read each physical file once while recording every opened lock name."""
        self.encountered[opened] = None
        real = self._canonical_path(opened)
        self.file_paths[opened] = real
        if real not in self.documents:
            try:
                with open(opened, encoding="utf-8", newline="") as source:
                    text = source.read()
            except UnicodeDecodeError as error:
                raise ValueError(
                    f"Sheet file '{os.path.basename(opened)}' is not UTF-8 text: "
                    f"{error.reason} at byte {error.start}"
                ) from error
            self.documents[real] = _parse(text, real)
            self.texts[real] = text
        return real, self.documents[real]

    def _discover(self, opened: str) -> frozenset[str]:
        """Find child files before choosing which selected files are real roots."""
        real, document = self._read(opened)
        ancestor = (real, self._canonical_path(os.path.dirname(opened)))
        if ancestor in self.ancestors:
            raise ValueError(f"Cyclic schematic hierarchy: {opened}")
        if opened not in self.descendants:
            self.ancestors.add(ancestor)
            children: set[str] = set()
            for sheet in document.sheets:
                child = os.path.normpath(
                    os.path.join(os.path.dirname(opened), sheet.filename)
                )
                try:
                    child_real, _ = self._read(child)
                except FileNotFoundError as error:
                    raise FileNotFoundError(
                        f"Sheet file '{sheet.filename}' used in "
                        f"'{os.path.basename(opened)}' does not exist: {child}"
                    ) from error
                children.add(child_real)
                children.update(self._discover(child))
            self.ancestors.remove(ancestor)
            self.descendants[opened] = frozenset(children)
        return self.descendants[opened]

    def _walk(self, opened: str, prefix: str, blocked: bool = False) -> None:
        """Expand every sheet instance; never substitute a child's root UUID."""
        real, document = self._read(opened)
        ancestor = (real, self._canonical_path(os.path.dirname(opened)))
        if ancestor in self.ancestors:
            raise ValueError(f"Cyclic schematic hierarchy: {opened}")
        self.ancestors.add(ancestor)
        for target, details in zip(document.symbols, document.details):
            path = f"{prefix}/{target.symbol_uuid}"
            self.occurrences.append(_Occurrence(path, target, details))
            if not target.symbol_uuid or target.symbol_uuid in document.ambiguous_uuids:
                self.issues.append(
                    f"Ambiguous or missing symbol UUID in {opened}: {target.reference}"
                )
                self.blocked.add(path)
                continue
            self.targets[target.key] = target
            self.instances[target.key].add(path)
            self.links[path].add(target.key)
            if blocked:
                self.blocked.add(path)
        for sheet in document.sheets:
            child = os.path.normpath(
                os.path.join(os.path.dirname(opened), sheet.filename)
            )
            if sheet.ambiguous:
                self.issues.append(
                    f"Ambiguous or missing sheet UUID in {opened}: {sheet.filename}"
                )
            self._walk(child, f"{prefix}/{sheet.uuid}", blocked or sheet.ambiguous)
        self.ancestors.remove(ancestor)

    def build(
        self,
        paths: Iterable[str],
        *,
        shared_project: bool,
        root_uuids: Mapping[str, str],
    ) -> SchematicIndex:
        """Finish all instance paths and remove ambiguous links before publishing."""
        opened_paths = tuple(
            dict.fromkeys(os.path.abspath(os.path.normpath(path)) for path in paths)
        )
        roots = [(opened, *self._read(opened)) for opened in opened_paths]
        descendants = set().union(*(self._discover(opened) for opened in opened_paths))
        roots = [
            (opened, real, doc)
            for opened, real, doc in roots
            if shared_project or real not in descendants
        ]
        declared_ids: dict[str, str] = {}
        for path, uuid in root_uuids.items():
            if not isinstance(uuid, str) or not uuid:
                raise ValueError(f"Invalid declared project root UUID: {path}")
            real = self._canonical_path(os.path.abspath(os.path.normpath(path)))
            if real in declared_ids and declared_ids[real] != uuid:
                raise ValueError(f"Conflicting declared project root UUIDs: {path}")
            declared_ids[real] = uuid
        root_files: dict[str, set[tuple[str, str]]] = defaultdict(set)
        for opened, real, document in roots:
            root_uuid = declared_ids.get(real, document.root_uuid)
            root_files[root_uuid].add(
                (real, self._canonical_path(os.path.dirname(opened)))
            )
        # Root discovery above must not reorder the depth-first lock inventory.
        self.encountered.clear()
        for opened, real, document in roots:
            root_uuid = declared_ids.get(real, document.root_uuid)
            duplicate = len(root_files[root_uuid]) > 1
            if duplicate:
                self.issues.append(f"Ambiguous root UUID {root_uuid}: {opened}")
            self._walk(opened, f"/{root_uuid}", duplicate)
        for opened in opened_paths:
            self.encountered[opened] = None
        targets = {
            key: replace(target, instance_paths=tuple(sorted(self.instances[key])))
            for key, target in self.targets.items()
        }
        links: dict[str, SymbolTarget] = {}
        for path, keys in self.links.items():
            if len(keys) != 1:
                self.issues.append(f"Ambiguous schematic symbol path: {path}")
            elif path not in self.blocked:
                links[path] = targets[next(iter(keys))]
        spellings: dict[str, set[str]] = defaultdict(set)
        for path in set(self.links) | self.blocked:
            spellings[path].add(path)
            # KiCad 7–9 SCH_SHEET_PATH::PathAsString skips the real root sheet.
            # KiCad 10 skips its new virtual root and includes the real root.
            rootless = "/" + path.split("/", 2)[2]
            spellings[rootless].add(path)
        canonical_links = {
            spelling: next(iter(candidates))
            for spelling, candidates in spellings.items()
            if len(candidates) == 1 and next(iter(candidates)) in links
        }
        return SchematicIndex(
            MappingProxyType(targets),
            MappingProxyType(self.texts),
            MappingProxyType(self.file_paths),
            tuple(self.encountered),
            tuple(dict.fromkeys(self.issues)),
            MappingProxyType(links),
            MappingProxyType(canonical_links),
            MappingProxyType(
                _component_resolutions(
                    self.occurrences,
                    targets,
                    links,
                    shared_project,
                )
            ),
        )
