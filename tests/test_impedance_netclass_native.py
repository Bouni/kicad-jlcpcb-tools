"""Copy exact live net-class membership without changing native board settings."""

from copy import deepcopy
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from impedance.matching import analyze, validate_review
from impedance.model import Config, LayerSettings, Specification, ValidationError
from impedance.pcbnew_adapter import snapshot_board
from tests.test_impedance_render import FakeBoard, FakeTrack, pcbnew
from tests.test_impedance_text_context import NativeText

PCB_PARAMETERS = (
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


class NativeClass:
    """Retain mutable optional native constraints and exact constituent objects."""

    def __init__(
        self, name: str, constituents: Optional[tuple[Any, ...]] = None
    ) -> None:
        self.name = name
        self.constituents = (self,) if constituents is None else constituents
        self.priority = 0
        self.parameters = dict.fromkeys(PCB_PARAMETERS, 200_000)
        self.profile = ""

    def GetName(self) -> str:
        """Return the literal class name."""
        return self.name

    def GetConstituentNetclasses(self) -> tuple[Any, ...]:
        """Return the native constituent records, including inherited Default."""
        return self.constituents

    def GetPriority(self) -> int:
        """Read current priority without saving the project."""
        return self.priority

    def GetTuningProfile(self) -> str:
        """Read the assigned profile name."""
        return self.profile

    def __getattr__(self, name: str) -> Any:
        """Expose mutable native-style Has/Get optional constraint accessors."""
        if name.startswith("Has") and name[3:] in self.parameters:
            # KiCad's header declares this particular predicate as int.
            if name == "HasViaDrill":
                return lambda: int(self.parameters[name[3:]] is not None)
            return lambda: self.parameters[name[3:]] is not None
        if name.startswith("Get") and name[3:] in self.parameters:

            def value() -> int:
                result = self.parameters[name[3:]]
                assert result is not None, "Do not read absent native optional values"
                return result

            return value
        raise AttributeError(name)

    def ContainsNetclassWithName(self, name: str) -> bool:
        """Reject unnecessary wildcard matching when typed constituents exist."""
        raise AssertionError("Native membership helper uses wildcards, not exact names")


@dataclass
class NativeNet:
    """Keep identity independent of routed-track geometry."""

    name: str
    code: int

    def GetNetname(self) -> str:
        """Return the board's exact net name."""
        return self.name

    def GetNetCode(self) -> int:
        """Return zero only for unassigned copper."""
        return self.code


class OpaqueKeyMap:
    """Model opaque wxString map keys but typed native value wrappers."""

    def __init__(self, values: tuple[Any, ...]) -> None:
        self.entries = values
        self.items_calls = 0

    def items(self) -> tuple[tuple[Any, Any], ...]:
        """Expose non-string keys, as observed in the installed nested typemap."""
        self.items_calls += 1
        return tuple((object(), value) for value in self.entries)

    def values(self) -> tuple[Any, ...]:
        """Keep the native records available through the map's value iterator."""
        return self.entries


class NativeSettings:
    """Let native resolution provide label and pattern results independently."""

    def __init__(self) -> None:
        self.default = NativeClass("Default")
        self.classes = {"USB": NativeClass("USB"), "Unused": NativeClass("Unused")}
        self.implicit = NativeClass("Label only")
        self.effective = {
            "USB_D+": NativeClass(
                "Opaque composite display", (self.classes["USB"], self.default)
            ),
            "USB_D-": NativeClass(
                "Different display", (self.implicit, self.classes["USB"])
            ),
            "POWER": self.default,
        }
        self.resolved = []

    def GetNetclasses(self) -> dict[str, NativeClass]:
        """Expose configured classes, excluding Default and implicit classes."""
        return self.classes

    def GetDefaultNetclass(self) -> NativeClass:
        """Expose KiCad's separately stored Default class."""
        return self.default

    def GetEffectiveNetClass(self, name: str) -> NativeClass:
        """Resolve from live native state instead of static track defaults."""
        self.resolved.append(name)
        return self.effective[name]

    def GetNetClassByName(self, name: str) -> NativeClass:
        """Reject a misleading native API that cannot validate existence."""
        raise AssertionError("Unknown native class lookup silently returns Default")

    def ClearAllCaches(self) -> None:
        """Reject plugin mutation of shared native caches."""
        raise AssertionError("Reading classes must not clear shared native caches")


class OpaqueVectorClass(NativeClass):
    """Model the installed SWIG pointer and native exact non-wildcard predicate."""

    def __init__(self, constituents: tuple[NativeClass, ...]) -> None:
        super().__init__(",".join(item.name for item in constituents), constituents)
        self.queries: list[str] = []

    def GetConstituentNetclasses(self) -> Any:
        """Expose the opaque pointer returned by installed SWIG bindings."""
        return object()

    def ContainsNetclassWithName(self, name: str) -> bool:
        """Preserve exact native whole-string behavior for non-wildcard names."""
        assert not any(character in name for character in "*?\x00")
        self.queries.append(name)
        return name in {item.name for item in self.constituents}


class NativeBoard(FakeBoard):
    """Expose ordinary snapshot geometry plus stateful native net settings."""

    def __init__(self) -> None:
        super().__init__((FakeTrack(net="USB_D+"),))
        self.settings = NativeSettings()
        self.nets = {
            name: NativeNet(name, index)
            for index, name in enumerate(("", "USB_D+", "USB_D-", "POWER"))
        }

    def GetDesignSettings(self) -> Any:
        """Return the live settings holder without a detached board allocation."""
        return SimpleNamespace(m_NetSettings=self.settings)

    def GetNetsByName(self) -> dict[str, NativeNet]:
        """Enumerate routed and unrouted board nets."""
        return self.nets


def context(board: Any, routed_nets: tuple[str, ...] = ()) -> Any:
    """Use the production adapter without requiring native KiCad in myenv."""
    from impedance.net_metadata import netclass_context

    return netclass_context(board, routed_nets)


def test_snapshot_includes_native_classes_and_exact_constituents() -> None:
    """Keep configured, label-only, composite and Default memberships distinct."""
    board = NativeBoard()
    snapshot = snapshot_board(board, pcbnew())
    assert snapshot.net_classes == ("Default", "Label only", "USB", "Unused")
    assert snapshot.net_class_memberships == (
        ("POWER", ("Default",)),
        ("USB_D+", ("Default", "USB")),
        ("USB_D-", ("Label only", "USB")),
    )
    assert len(snapshot.net_class_context_digest) == 64
    assert snapshot.net_class_error == ""
    assert "" not in board.settings.resolved


def _detached_with_default_netclasses(source: NativeBoard) -> NativeBoard:
    """Model a geometry clone whose project assignment leaves default net settings."""
    detached = deepcopy(source)
    detached.settings.classes.clear()
    detached.settings.effective = dict.fromkeys(
        detached.settings.effective, detached.settings.default
    )
    return detached


def test_detached_snapshot_reads_source_netclasses_without_mutation() -> None:
    """Use live class metadata while leaving detached default settings independent."""
    source = NativeBoard()
    detached = _detached_with_default_netclasses(source)
    default_snapshot = snapshot_board(detached, pcbnew())
    source_snapshot = snapshot_board(source, pcbnew())
    assert default_snapshot.net_classes == ("Default",)
    assert (
        default_snapshot.net_class_context_digest
        != source_snapshot.net_class_context_digest
    )

    snapshot = snapshot_board(detached, pcbnew(), netclass_source=source)
    assert snapshot == source_snapshot
    assert detached.settings is not source.settings
    assert snapshot_board(detached, pcbnew()) == default_snapshot

    source.settings.classes["USB"].parameters["TrackWidth"] += 1
    changed = snapshot_board(detached, pcbnew(), netclass_source=source)
    assert changed.net_class_context_digest != snapshot.net_class_context_digest
    assert changed.traces == snapshot.traces
    assert changed.context_digest == snapshot.context_digest
    assert snapshot_board(detached, pcbnew()) == default_snapshot


def test_netclass_source_does_not_replace_clone_geometry_text_or_pairing() -> None:
    """Clone tracks, shown text and pair recognition remain the snapshot authority."""
    source = NativeBoard()
    detached = _detached_with_default_netclasses(source)
    text = NativeText("variant-label", "${VERSION}")
    text.serialized = '(gr_text "${VERSION}")'
    detached.context = (text,)
    detached.tracks[0].width += 10

    def coupled_net(native: NativeNet) -> Optional[NativeNet]:
        """Give only the clone a reciprocal differential pair."""
        mate = {"USB_D+": "USB_D-", "USB_D-": "USB_D+"}.get(native.name)
        return detached.nets[mate] if mate else None

    detached.DpCoupledNet = coupled_net
    snapshot = snapshot_board(detached, pcbnew(), netclass_source=source)
    source_snapshot = snapshot_board(source, pcbnew())
    assert snapshot.traces != source_snapshot.traces
    assert snapshot.context_digest != source_snapshot.context_digest
    assert snapshot.differential_pairs == (("USB_D+", "USB_D-"),)
    assert snapshot.differential_pair_error == ""
    assert snapshot.net_class_context_digest == source_snapshot.net_class_context_digest

    detached.tracks[0].width += 1
    geometry_changed = snapshot_board(detached, pcbnew(), netclass_source=source)
    assert geometry_changed.traces != snapshot.traces
    assert (
        geometry_changed.net_class_context_digest == snapshot.net_class_context_digest
    )
    text.context["VERSION"] = "variant C"
    text_changed = snapshot_board(detached, pcbnew(), netclass_source=source)
    assert text_changed.context_digest != geometry_changed.context_digest
    assert text_changed.traces == geometry_changed.traces
    assert text_changed.net_class_context_digest == snapshot.net_class_context_digest


def test_netclass_source_validates_routed_nets_from_detached_geometry() -> None:
    """A clone route absent from the live class catalog must prevent validation."""
    source = NativeBoard()
    detached = _detached_with_default_netclasses(source)
    detached.tracks[0].net = "CLONE_ONLY"
    snapshot = snapshot_board(detached, pcbnew(), netclass_source=source)
    assert snapshot.traces[0].net == "CLONE_ONLY"
    assert "cannot resolve the routed net 'CLONE_ONLY'" in snapshot.net_class_error
    assert snapshot.net_class_context_digest == ""


def test_class_and_net_names_are_literal_not_composite_strings_or_wildcards() -> None:
    """Typed native records safely preserve comma and wildcard class names."""
    board = NativeBoard()
    literal = NativeClass("USB,RF*?")
    board.settings.classes[literal.name] = literal
    board.settings.effective["USB_D+"] = NativeClass("USB,RF*?,Default", (literal,))
    result = context(board)
    assert dict(result.memberships)["USB_D+"] == ("USB,RF*?",)
    assert "RF" not in result.classes
    assert not result.error


def test_live_edit_and_reopen_are_detected_without_board_file_writes() -> None:
    """Snapshots detach old membership and reflect a reopened edited board."""
    board = NativeBoard()
    original = context(board)
    board.settings.effective["USB_D+"] = board.settings.default
    changed = context(board)
    reopened = context(deepcopy(board))
    assert changed.digest != original.digest
    assert changed == reopened
    assert dict(original.memberships)["USB_D+"] == ("Default", "USB")
    assert dict(changed.memberships)["USB_D+"] == ("Default",)


@pytest.mark.parametrize("parameter", PCB_PARAMETERS)
def test_unsaved_native_constraint_edits_invalidate_context(parameter: str) -> None:
    """Each relevant PCB constraint is part of the live review fingerprint."""
    board = NativeBoard()
    initial = context(board)
    board.settings.classes["USB"].parameters[parameter] += 1
    assert context(board).digest != initial.digest


def test_optional_constraint_inheritance_is_distinct_from_same_explicit_value() -> None:
    """An unset optional value is meaningful and must not be dereferenced."""
    board = NativeBoard()
    initial = context(board)
    board.settings.classes["USB"].parameters["TrackWidth"] = None
    inherited = context(board)
    assert not inherited.error
    assert inherited.digest != initial.digest


@pytest.mark.parametrize(
    "attribute,value", [("priority", 3), ("profile", "USB 90 ohm")]
)
def test_priority_and_tuning_profile_name_invalidate_live_context(
    attribute: str, value: Any
) -> None:
    """Changed class priority or profile invalidates an unchanged routed board."""
    board = NativeBoard()
    initial = context(board)
    setattr(board.settings.classes["USB"], attribute, value)
    assert context(board).digest != initial.digest


def test_catalog_and_iteration_order_do_not_change_membership_or_digest() -> None:
    """Native container iteration order is not a design change."""
    board = NativeBoard()
    initial = context(board)
    board.nets = dict(reversed(tuple(board.nets.items())))
    board.settings.classes = dict(reversed(tuple(board.settings.classes.items())))
    board.settings.effective["USB_D+"].constituents = tuple(
        reversed(board.settings.effective["USB_D+"].constituents)
    )
    assert context(board) == initial


def test_unrouted_configured_class_add_and_remove_invalidate_catalog() -> None:
    """Engineers can choose configured classes before routing them."""
    board = NativeBoard()
    initial = context(board)
    board.settings.classes["Future RF"] = NativeClass("Future RF")
    added = context(board)
    assert "Future RF" in added.classes
    assert added.digest != initial.digest
    del board.settings.classes["Future RF"]
    assert context(board) == initial


@pytest.mark.parametrize(
    "corruption",
    [
        "missing_settings",
        "missing_nets",
        "opaque_constituents",
        "empty_constituents",
        "null_constituent",
        "bad_name",
        "duplicate_constituent",
        "duplicate_class_name",
        "net_mismatch",
        "missing_routed_net",
        "numeric_constraint",
        "native_exception",
    ],
)
def test_unavailable_or_partial_metadata_fails_closed(corruption: str) -> None:
    """Unavailable APIs and inconsistent native records never broaden selection."""
    board = NativeBoard()
    routed = ("USB_D+",)
    if corruption == "missing_settings":
        board.GetDesignSettings = None
    elif corruption == "missing_nets":
        board.GetNetsByName = None
    elif corruption == "opaque_constituents":
        board.settings.effective["USB_D+"].constituents = object()
    elif corruption == "empty_constituents":
        board.settings.effective["USB_D+"].constituents = ()
    elif corruption == "null_constituent":
        board.settings.effective["USB_D+"].constituents = (None,)
    elif corruption == "bad_name":
        board.settings.classes["USB"].name = ""
    elif corruption == "duplicate_constituent":
        native = board.settings.classes["USB"]
        board.settings.effective["USB_D+"].constituents = (native, native)
    elif corruption == "duplicate_class_name":
        board.settings.classes["Second USB"] = NativeClass("USB")
    elif corruption == "net_mismatch":
        board.nets["USB_D+"].name = "Different net"
    elif corruption == "missing_routed_net":
        routed = ("USB_D+", "Orphaned route")
    elif corruption == "numeric_constraint":
        board.settings.classes["USB"].parameters["TrackWidth"] = "wide"
    elif corruption == "native_exception":
        del board.settings.effective["USB_D+"]
    result = context(board, routed)
    assert result.error
    assert result.classes == ()
    assert result.memberships == ()
    assert result.digest == ""


def test_snapshot_metadata_failure_preserves_geometry_and_context() -> None:
    """Metadata errors retain geometry for diagnostics without authorizing export."""
    board = NativeBoard()
    valid = snapshot_board(board, pcbnew())
    board.GetNetsByName = None
    unavailable = snapshot_board(board, pcbnew())
    assert unavailable.net_class_error
    assert unavailable.traces == valid.traces
    assert unavailable.context_digest == valid.context_digest


def test_constituent_with_conflicting_same_name_definition_is_rejected() -> None:
    """Reject stale or inconsistent native records sharing a class name."""
    board = NativeBoard()
    conflicting = NativeClass("USB")
    conflicting.parameters["TrackWidth"] = 900_000
    board.settings.effective["USB_D+"].constituents = (conflicting,)
    assert context(board).error


@pytest.mark.parametrize(
    "names",
    [
        ("USB", "Default"),
        ("USB,RF",),
        ("USB,RF", "USB", "RF"),
        (" leading ", " trailing "),
        ("RF[2]", r"RF\path", "rf{2}"),
        (",", "A,,B"),
    ],
)
def test_opaque_swig_vector_membership_is_proven_by_native_literal_predicate(
    names: tuple[str, ...],
) -> None:
    """Verify spans natively instead of inferring membership from delimiters."""
    board = NativeBoard()
    native = OpaqueVectorClass(tuple(NativeClass(name) for name in names))
    board.settings.effective["USB_D+"] = native
    result = context(board)
    assert not result.error
    assert dict(result.memberships)["USB_D+"] == tuple(sorted(names))
    assert set(names).issubset(result.classes)
    assert native.queries


def test_identical_composite_name_does_not_imply_identical_constituents() -> None:
    """Never cache effective membership by a possibly colliding joined name."""
    board = NativeBoard()
    board.settings.effective["USB_D+"] = OpaqueVectorClass((NativeClass("A,B"),))
    board.settings.effective["USB_D-"] = OpaqueVectorClass(
        (NativeClass("A"), NativeClass("B"))
    )
    result = context(board)
    assert not result.error
    assert dict(result.memberships)["USB_D+"] == ("A,B",)
    assert dict(result.memberships)["USB_D-"] == ("A", "B")


@pytest.mark.parametrize("name", ["RF*", "USB?", "USB\x00RF"])
def test_opaque_native_wildcard_or_nul_name_never_widens_membership(name: str) -> None:
    """Unsupported literal names fail before any native wildcard query."""
    board = NativeBoard()
    native = OpaqueVectorClass((NativeClass(name),))
    board.settings.effective["USB_D+"] = native
    result = context(board)
    assert result.error
    assert result.classes == result.memberships == ()
    assert not native.queries


def test_opaque_composite_candidate_count_is_bounded_before_native_queries() -> None:
    """Limit quadratic candidate construction before querying KiCad."""
    board = NativeBoard()
    native = OpaqueVectorClass(tuple(NativeClass(str(index)) for index in range(100)))
    board.settings.effective["USB_D+"] = native
    result = context(board)
    assert "too complex" in result.error
    assert not native.queries


def test_unused_configured_wildcard_class_does_not_disable_other_classes() -> None:
    """Unassigned unusual class names need no ambiguous native resolution."""
    board = NativeBoard()
    board.settings.classes["Future*"] = NativeClass("Future*")
    board.settings.effective["USB_D+"] = OpaqueVectorClass(
        (board.settings.classes["USB"],)
    )
    result = context(board)
    assert not result.error
    assert "Future*" in result.classes
    assert dict(result.memberships)["USB_D+"] == ("USB",)


def test_opaque_implicit_and_configured_class_constraints_invalidate_context() -> None:
    """Effective constraints remain fingerprinted even with opaque constituents."""
    board = NativeBoard()
    effective = OpaqueVectorClass(
        (board.settings.classes["USB"], board.settings.implicit)
    )
    board.settings.effective["USB_D+"] = effective
    original = context(board)
    effective.parameters["DiffPairGap"] = 123_000
    assert context(board).digest != original.digest


def test_duplicate_native_net_codes_are_rejected() -> None:
    """Inconsistent net identity cannot silently establish class membership."""
    board = NativeBoard()
    board.nets["USB_D-"].code = board.nets["USB_D+"].code
    assert context(board).error


@pytest.mark.parametrize("present", [0, 1])
def test_native_integer_via_drill_presence_is_accepted(present: int) -> None:
    """Match the actual SWIG int contract of NETCLASS.HasViaDrill."""
    board = NativeBoard()
    board.settings.classes["USB"].parameters["ViaDrill"] = 300_000 if present else None
    assert board.settings.classes["USB"].HasViaDrill() == present
    result = context(board)
    assert not result.error
    assert "USB" in result.classes


@pytest.mark.parametrize("value", [-1, 2, 1.0, "1", None])
def test_invalid_native_integer_via_drill_presence_fails_closed(value: Any) -> None:
    """Accept only documented binary integer presence, never loose truthiness."""
    board = NativeBoard()
    board.settings.classes["USB"].HasViaDrill = lambda: value
    assert context(board).error


def test_empty_board_still_exposes_configured_class_catalog_and_digest() -> None:
    """Allow new specifications before any nets or tracks have been created."""
    board = NativeBoard()
    board.nets = {"": NativeNet("", 0)}
    board.tracks = ()
    snapshot = snapshot_board(board, pcbnew())
    assert snapshot.net_classes == ("Default", "USB", "Unused")
    assert snapshot.net_class_memberships == ()
    assert len(snapshot.net_class_context_digest) == 64
    assert not snapshot.net_class_error


@pytest.mark.parametrize("change", ["constraint", "profile", "membership", "geometry"])
def test_native_snapshot_review_persistence_requires_renewal_after_live_edits(
    change: str,
) -> None:
    """Trace actual constructors through native snapshot, approval, reopen and edit."""
    board = NativeBoard()
    specification = Specification(
        "usb",
        "USB-class single-ended example",
        "50",
        "single_ended",
        net_class="USB",
        layer_settings=(LayerSettings("F.Cu", ("B.Cu",)),),
    )
    configuration = Config(True, (specification,))
    first = analyze(configuration, snapshot_board(board, pcbnew()))
    approved = replace(
        configuration,
        reviewed_digest=first.digest,
        included_section_ids=tuple(section.section_id for section in first.sections),
    )
    reopened = Config.from_dict(approved.to_dict())
    validate_review(reopened, analyze(reopened, snapshot_board(board, pcbnew())))
    if change == "constraint":
        board.settings.classes["USB"].parameters["TrackWidth"] += 1
    elif change == "profile":
        board.settings.classes["USB"].profile = "New profile"
    elif change == "membership":
        board.settings.effective["USB_D+"] = NativeClass(
            "USB,Label only", (board.settings.classes["USB"], board.settings.implicit)
        )
    else:
        board.tracks[0].width = 100_000
    with pytest.raises(ValidationError, match="changed"):
        validate_review(reopened, analyze(reopened, snapshot_board(board, pcbnew())))


def test_netclass_review_requires_native_class_capability_after_reopening() -> None:
    """Missing live membership cannot silently reuse a previously approved selection."""
    board = NativeBoard()
    specification = Specification(
        "usb",
        "USB",
        "50",
        "single_ended",
        "USB",
        (LayerSettings("F.Cu", ("B.Cu",)),),
    )
    configuration = Config(True, (specification,))
    first = analyze(configuration, snapshot_board(board, pcbnew()))
    approved = replace(
        configuration,
        reviewed_digest=first.digest,
        included_section_ids=tuple(section.section_id for section in first.sections),
    )
    board.GetNetsByName = None
    reopened = Config.from_dict(approved.to_dict())
    with pytest.raises(ValidationError, match="net.class|metadata|unavailable"):
        validate_review(reopened, analyze(reopened, snapshot_board(board, pcbnew())))


@pytest.mark.parametrize("opaque_maps", ["classes", "nets", "both"])
def test_opaque_native_map_keys_do_not_hide_typed_class_and_net_names(
    opaque_maps: str,
) -> None:
    """Native map key typemaps must not block direct typed name accessors."""
    board = NativeBoard()
    original = snapshot_board(board, pcbnew())
    if opaque_maps in ("classes", "both"):
        classes = OpaqueKeyMap(tuple(board.settings.classes.values()))
        board.settings.GetNetclasses = lambda: classes
    if opaque_maps in ("nets", "both"):
        nets = OpaqueKeyMap(tuple(board.nets.values()))
        board.GetNetsByName = lambda: nets
    assert snapshot_board(board, pcbnew()) == original
    if opaque_maps in ("classes", "both"):
        assert classes.items_calls == 0
    if opaque_maps in ("nets", "both"):
        assert nets.items_calls == 0
