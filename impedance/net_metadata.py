"""Copy live native net classes and differential pairs into immutable metadata.

Effective classes, including label/pattern assignments and inherited Default,
come from KiCad, not a parallel implementation of its matching rules. Calling
its resolver may populate KiCad's own memoization cache; this module never
clears caches, changes ownership, sets native values, or reads guessed projects.

BOARD.DpCoupledNet supplies the board's pairing rules. This module does not
guess suffixes, alter nets, or treat name-based pairing as electrical validation.
Class and pairing diagnostics remain independent so single-ended matching can
ignore unavailable differential-pair metadata.
"""

from collections.abc import Iterable
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Optional

_PCB_PARAMETERS = (
    "Clearance",
    "TrackWidth",
    "ViaDiameter",
    "ViaDrill",
    "uViaDiameter",
    "uViaDrill",
    "DiffPairWidth",
    "DiffPairGap",
    "DiffPairViaGap",
)
_MAX_NAME_CANDIDATES = 4096


class NetClassContextError(RuntimeError):
    """Report metadata that cannot safely support class-based matching."""


@dataclass(frozen=True)
class NetClassContext:
    """Contain only immutable Python values, never borrowed native objects."""

    classes: tuple[str, ...] = ()
    memberships: tuple[tuple[str, tuple[str, ...]], ...] = ()
    digest: str = ""
    error: str = ""


def _call(value: Any, name: str, *arguments: Any) -> Any:
    """Require a documented native getter rather than inventing a fallback."""
    getter = getattr(value, name, None)
    if not callable(getter):
        raise NetClassContextError(f"KiCad does not expose {name} for net classes.")
    return getter(*arguments)


def _name(value: Any, label: str, allow_empty: bool = False) -> str:
    """Reject opaque pointer strings and malformed names; preserve exact spelling."""
    if not isinstance(value, str) or "\x00" in value or (not value and not allow_empty):
        raise NetClassContextError(f"KiCad returned an invalid {label}.")
    return value


def _integer(value: Any, label: str) -> int:
    """Native integer quantities must not become strings or truncated floats."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise NetClassContextError(f"KiCad returned an invalid {label}.")
    return value


def _definition(native: Any) -> tuple[Any, ...]:
    """Fingerprint current PCB constraints, including unset/inherited values."""
    values = []
    for parameter in _PCB_PARAMETERS:
        present = _call(native, "Has" + parameter)
        # KiCad 10's header declares HasViaDrill as int, unlike the other
        # optional-presence predicates. SWIG faithfully exposes its 0/1 result.
        if parameter == "ViaDrill" and type(present) is int and present in (0, 1):
            present = bool(present)
        if not isinstance(present, bool):
            raise NetClassContextError(f"KiCad returned invalid {parameter} presence.")
        value = (
            _integer(_call(native, "Get" + parameter), parameter) if present else None
        )
        values.append((parameter, value))
    profile_getter = getattr(native, "GetTuningProfile", None)
    profile = (
        _name(profile_getter(), "tuning-profile name", allow_empty=True)
        if callable(profile_getter)
        else None
    )
    return (
        _integer(_call(native, "GetPriority"), "net-class priority"),
        tuple(values),
        profile,
    )


def _map_values(value: Any, label: str) -> tuple[Any, ...]:
    """Read typed values, avoiding SWIG's opaque nested wxString map keys.

    Installed KiCad 10's ``map.items()`` creates owned opaque wxString key
    wrappers. Its value iterator returns typed NETCLASS/NETINFO_ITEM objects;
    their direct name getters have the correct Unicode typemap.
    """
    values = getattr(value, "values", None)
    if not callable(values):
        raise NetClassContextError(f"KiCad cannot enumerate {label}.")
    return tuple(values())


def _opaque_constituent_names(native: Any) -> tuple[str, ...]:
    """Verify exact names natively when SWIG exposes an opaque vector.

    KiCad 10.0.6 NETCLASS::GetName joins constituent names with commas, while
    ContainsNetclassWithName checks each constituent with wxString::Matches.
    Generate every contiguous comma-delimited span, then ask the native
    predicate whether that exact span is a constituent. Merely appearing in
    the composite string never establishes membership. This also distinguishes
    a literal class named ``USB,RF`` from separate USB and RF classes.

    wxString's predicate gives '*' and '?' wildcard semantics, so those names
    cannot safely use this fallback. No pointer casts or ownership changes are
    made to access an unwrapped std::vector.
    """
    combined = _name(_call(native, "GetName"), "effective net-class name")
    if "*" in combined or "?" in combined:
        raise NetClassContextError(
            "This KiCad Python API cannot resolve literal '*' or '?' in an "
            "assigned net-class name safely. Use manual layer/width matching "
            "or rename that net class."
        )
    pieces = combined.split(",")
    if len(pieces) * (len(pieces) + 1) // 2 > _MAX_NAME_CANDIDATES:
        raise NetClassContextError(
            "The effective net-class name is too complex for this KiCad Python API."
        )
    candidates = {
        ",".join(pieces[start:end])
        for start in range(len(pieces))
        for end in range(start + 1, len(pieces) + 1)
        if ",".join(pieces[start:end])
    }
    names = []
    for candidate in sorted(candidates):
        accepted = _call(native, "ContainsNetclassWithName", candidate)
        if not isinstance(accepted, bool):
            raise NetClassContextError("KiCad returned an invalid class membership.")
        if accepted:
            names.append(candidate)
    if not names:
        raise NetClassContextError(
            "KiCad returned no effective net-class constituents."
        )
    return tuple(names)


def _constituents(native: Any) -> tuple[tuple[str, ...], tuple[Any, ...]]:
    """Prefer exact typed constituents, falling back only for opaque vectors."""
    result = _call(native, "GetConstituentNetclasses")
    try:
        values = tuple(result)
    except TypeError:
        return _opaque_constituent_names(native), ()
    names = tuple(_name(_call(value, "GetName"), "net-class name") for value in values)
    if not names or len(names) != len(set(names)):
        raise NetClassContextError("KiCad returned invalid net-class constituents.")
    return tuple(sorted(names)), values


def _read_context(board: Any, routed_nets: Iterable[str]) -> NetClassContext:
    """Resolve every named board net, retaining configured unrouted classes."""
    settings = getattr(_call(board, "GetDesignSettings"), "m_NetSettings", None)
    configured_classes = _map_values(
        _call(settings, "GetNetclasses"), "configured net classes"
    )
    definitions: dict[str, tuple[Any, ...]] = {}
    default = _call(settings, "GetDefaultNetclass")
    default_name = _name(_call(default, "GetName"), "default net-class name")
    if default_name != "Default":
        raise NetClassContextError("KiCad returned an invalid Default net class.")
    definitions[default_name] = _definition(default)
    for native in configured_classes:
        name = _name(_call(native, "GetName"), "configured net-class name")
        definition = _definition(native)
        if name in definitions:
            raise NetClassContextError(
                "KiCad returned duplicate configured net classes."
            )
        definitions[name] = definition

    catalog = set(definitions)
    memberships = {}
    effective_definitions = {}
    net_codes: set[int] = set()
    for net in _map_values(_call(board, "GetNetsByName"), "board nets"):
        name = _name(_call(net, "GetNetname"), "net name", allow_empty=True)
        code = _integer(_call(net, "GetNetCode"), "net code")
        if not name or code == 0:
            continue
        if name in memberships or code in net_codes or code < 0:
            raise NetClassContextError(
                "KiCad returned duplicate or invalid board nets."
            )
        net_codes.add(code)
        native = _call(settings, "GetEffectiveNetClass", name)
        names, constituents = _constituents(native)
        memberships[name] = names
        effective_definitions[name] = _definition(native)
        catalog.update(names)
        for constituent in constituents:
            class_name = _name(_call(constituent, "GetName"), "net-class name")
            definition = _definition(constituent)
            if class_name in definitions and definitions[class_name] != definition:
                raise NetClassContextError(
                    "KiCad returned conflicting net-class settings."
                )
            definitions[class_name] = definition

    for name in routed_nets:
        name = _name(name, "routed net name", allow_empty=True)
        if name and name not in memberships:
            raise NetClassContextError(f"KiCad cannot resolve the routed net '{name}'.")
    classes = tuple(sorted(catalog))
    records = tuple(sorted(memberships.items()))
    payload = {
        "version": 1,
        "classes": classes,
        "memberships": records,
        "definitions": definitions,
        "effective_definitions": effective_definitions,
    }
    digest = hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")
        ).encode("ascii")
    ).hexdigest()
    return NetClassContext(classes, records, digest)


def netclass_context(board: Any, routed_nets: Iterable[str] = ()) -> NetClassContext:
    """Return complete class metadata or a diagnostic, preserving legacy mode."""
    try:
        return _read_context(board, routed_nets)
    except Exception as error:
        return NetClassContext(
            error=f"Cannot read native net-class matching metadata: {error}"
        )


class PairingContextError(RuntimeError):
    """Report unavailable or inconsistent native differential-pair metadata."""


@dataclass(frozen=True)
class PairingContext:
    """Contain canonical unordered pairs, without retaining native references."""

    pairs: tuple[tuple[str, str], ...] = ()
    error: str = ""


def _pair_getter(value: Any, method: str) -> Any:
    """Require a typed native accessor; never reinterpret an opaque pointer."""
    getter = getattr(value, method, None)
    if not callable(getter):
        raise PairingContextError(
            f"KiCad cannot expose {method} for differential pairs."
        )
    return getter


def _pair_name(value: Any) -> str:
    """Accept direct native Unicode results with exact spelling and case."""
    if not isinstance(value, str) or "\x00" in value:
        raise PairingContextError(
            "KiCad returned an invalid differential-pair net name."
        )
    return value


def _pair_identity(native: Any) -> tuple[str, int]:
    """Copy the native net identity and reject malformed or unassigned partners."""
    name = _pair_name(_pair_getter(native, "GetNetname")())
    code = _pair_getter(native, "GetNetCode")()
    if isinstance(code, bool) or not isinstance(code, int) or code < 0:
        raise PairingContextError(
            "KiCad returned an invalid differential-pair net code."
        )
    if bool(name) != (code > 0):
        raise PairingContextError(
            "KiCad returned inconsistent differential-pair net identity."
        )
    return name, code


def _read_pairing(board: Any, routed_nets: Iterable[str]) -> PairingContext:
    """Resolve known nets once each, then verify that pairing is reciprocal."""
    coupled_net = getattr(board, "DpCoupledNet", None)
    if not callable(coupled_net):
        raise PairingContextError(
            "KiCad does not expose native differential-pair recognition (DpCoupledNet)."
        )
    native_map = _pair_getter(board, "GetNetsByName")()
    # Installed SWIG map.items() produces opaque owned wxString keys; values()
    # exposes typed NETINFO_ITEM wrappers whose direct getters return strings.
    native_values = tuple(_pair_getter(native_map, "values")())
    nets = {}
    codes = {}
    seen_names: set[str] = set()
    seen_codes: set[int] = set()
    for native in native_values:
        name, code = _pair_identity(native)
        if name in seen_names or code in seen_codes:
            raise PairingContextError(
                "KiCad returned duplicate differential-pair net identities."
            )
        seen_names.add(name)
        seen_codes.add(code)
        if not name:
            continue
        nets[name] = native
        codes[name] = code
    for routed_name in routed_nets:
        name = _pair_name(routed_name)
        if name and name not in nets:
            raise PairingContextError(
                f"KiCad cannot identify the routed net '{name}' for pairing."
            )

    partners: dict[str, Optional[str]] = {}
    for name, native in nets.items():
        # The installed borrowed NETINFO_ITEM result is Python None for null.
        # No ownership promotion, mutation or secondary local suffix rule.
        mate = coupled_net(native)
        if mate is None:
            partners[name] = None
            continue
        mate_name, mate_code = _pair_identity(mate)
        if not mate_name or mate_name not in codes or codes[mate_name] != mate_code:
            raise PairingContextError(
                f"KiCad returned an unknown partner for net '{name}'."
            )
        if mate_name == name:
            raise PairingContextError(f"KiCad paired net '{name}' with itself.")
        partners[name] = mate_name

    pairs = set()
    for name, mate_name in partners.items():
        if mate_name is None:
            continue
        if partners.get(mate_name) != name:
            raise PairingContextError(
                f"KiCad's differential-pair mapping for '{name}' and '{mate_name}' is not reciprocal."
            )
        pairs.add((min(name, mate_name), max(name, mate_name)))
    return PairingContext(tuple(sorted(pairs)))


def pairing_context(board: Any, routed_nets: Iterable[str] = ()) -> PairingContext:
    """Return complete native pairs or an error that single-ended mode can ignore."""
    try:
        return _read_pairing(board, routed_nets)
    except Exception as error:
        return PairingContext(
            error=f"Cannot read KiCad differential-pair metadata: {error}"
        )
