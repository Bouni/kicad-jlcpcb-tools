"""Require reciprocal native differential pairs without guessing net-name rules."""

from copy import deepcopy
from dataclasses import replace
from typing import Any, Optional

import pytest

from impedance.matching import analyze, validate_review
from impedance.model import Config, LayerSettings, Specification, ValidationError
from impedance.pcbnew_adapter import snapshot_board

from .test_impedance_netclass_native import NativeBoard, NativeNet, OpaqueKeyMap
from .test_impedance_render import FakeTrack, pcbnew


class PairBoard(NativeBoard):
    """Provide mutable native pair results independent of suffix heuristics."""

    def __init__(
        self, pairs: tuple[tuple[str, str], ...] = (("USB_D+", "USB_D-"),)
    ) -> None:
        super().__init__()
        names = sorted({"POWER", *(name for pair in pairs for name in pair)})
        self.nets = {"": NativeNet("", 0)}
        self.nets.update(
            {name: NativeNet(name, index + 1) for index, name in enumerate(names)}
        )
        self.partners = dict.fromkeys(self.nets)
        for first, second in pairs:
            self.partners[first] = self.nets[second]
            self.partners[second] = self.nets[first]
        self.pair_calls: list[str] = []

    def DpCoupledNet(self, native: NativeNet) -> Optional[NativeNet]:
        """Resolve the given native object without changing board or net identity."""
        assert any(native is item for item in self.nets.values())
        assert native.code > 0 and native.name
        self.pair_calls.append(native.name)
        return self.partners[native.name]


def context(board: Any, routed_nets: tuple[str, ...] = ()) -> Any:
    """Exercise only public production helper behavior in the repository myenv."""
    from impedance.net_metadata import pairing_context

    return pairing_context(board, routed_nets)


def test_snapshot_copies_canonical_native_pair_metadata() -> None:
    """Keep one immutable pair, not two directional records or native handles."""
    board = PairBoard()
    original = snapshot_board(board, pcbnew())
    assert original.differential_pairs == (("USB_D+", "USB_D-"),)
    assert not original.differential_pair_error
    assert "" not in board.pair_calls
    board.partners["USB_D+"] = board.partners["USB_D-"] = None
    assert original.differential_pairs == (("USB_D+", "USB_D-"),)
    assert not snapshot_board(board, pcbnew()).differential_pairs


@pytest.mark.parametrize(
    "pair",
    [
        ("USB_D+", "USB_D-"),
        ("RF_P_123", "RF_N_123"),
        ("/hierarchy/CLK_P12__", "/hierarchy/CLK_N12__"),
        ("USB+_007", "USB-_007"),
        ("left member", "right member"),
    ],
)
def test_native_pair_result_is_authoritative_regardless_of_local_name_assumptions(
    pair: tuple[str, str],
) -> None:
    """Names with digits/hierarchy and future native rules pass through unchanged."""
    board = PairBoard((pair,))
    result = context(board)
    assert result.pairs == (tuple(sorted(pair)),)
    assert not result.error
    assert set(board.pair_calls) == set(board.nets) - {""}


def test_pair_looking_names_are_not_inferred_when_native_returns_no_mate() -> None:
    """A null native result never becomes an invented pair from local suffixes."""
    board = PairBoard()
    board.partners["USB_D+"] = board.partners["USB_D-"] = None
    result = context(board)
    assert not result.pairs
    assert not result.error


def test_multiple_pairs_are_deterministic_under_map_and_pair_order_changes() -> None:
    """Canonical pairs are stable across native iteration ordering and reopening."""
    pairs = (("D2_P", "D2_N"), ("D1+", "D1-"))
    board = PairBoard(pairs)
    first = context(board)
    board.nets = dict(reversed(tuple(board.nets.items())))
    assert context(board) == first == context(deepcopy(board))
    assert first.pairs == (("D1+", "D1-"), ("D2_N", "D2_P"))


def test_fresh_native_wrappers_can_identify_the_same_known_net() -> None:
    """SWIG may return distinct Python wrappers for the same native net identity."""
    board = PairBoard()
    for name, partner in board.partners.items():
        if partner is not None:
            board.partners[name] = NativeNet(partner.name, partner.code)
    assert context(board).pairs == (("USB_D+", "USB_D-"),)


def test_native_pairing_uses_map_values_without_creating_opaque_keys() -> None:
    """Use canonical direct net getters, never str() on opaque wxString keys."""
    board = PairBoard()
    values = OpaqueKeyMap(tuple(board.nets.values()))
    board.GetNetsByName = lambda: values
    assert context(board).pairs == (("USB_D+", "USB_D-"),)
    assert values.items_calls == 0


@pytest.mark.parametrize(
    "corruption",
    [
        "missing_api",
        "missing_map",
        "opaque_map",
        "missing_routed_net",
        "duplicate_name",
        "duplicate_code",
        "invalid_name",
        "invalid_code",
        "named_zero_code",
        "unnamed_positive_code",
        "unknown_partner",
        "wrong_partner_code",
        "unassigned_partner",
        "self_partner",
        "one_way_pair",
        "three_way_pair",
        "opaque_partner",
        "native_exception",
    ],
)
def test_incomplete_or_inconsistent_native_pair_metadata_fails_closed(
    corruption: str,
) -> None:
    """Return no partial pair set when native results cannot be trusted."""
    board = PairBoard()
    routed = ("USB_D+",)
    if corruption == "missing_api":
        board.DpCoupledNet = None
    elif corruption == "missing_map":
        board.GetNetsByName = None
    elif corruption == "opaque_map":
        board.GetNetsByName = lambda: object()
    elif corruption == "missing_routed_net":
        routed = ("Orphaned route",)
    elif corruption == "duplicate_name":
        board.nets["duplicate"] = NativeNet("USB_D+", 99)
    elif corruption == "duplicate_code":
        board.nets["USB_D-"].code = board.nets["USB_D+"].code
    elif corruption == "invalid_name":
        board.nets["USB_D+"].name = None
    elif corruption == "invalid_code":
        board.nets["USB_D+"].code = True
    elif corruption == "named_zero_code":
        board.nets["USB_D+"].code = 0
    elif corruption == "unnamed_positive_code":
        board.nets["USB_D+"].name = ""
    elif corruption == "unknown_partner":
        board.partners["USB_D+"] = NativeNet("Unknown", 99)
    elif corruption == "wrong_partner_code":
        board.partners["USB_D+"] = NativeNet("USB_D-", 99)
    elif corruption == "unassigned_partner":
        board.partners["USB_D+"] = board.nets[""]
    elif corruption == "self_partner":
        board.partners["USB_D+"] = board.nets["USB_D+"]
    elif corruption == "one_way_pair":
        board.partners["USB_D-"] = None
    elif corruption == "three_way_pair":
        board.partners["USB_D-"] = board.nets["POWER"]
        board.partners["POWER"] = board.nets["USB_D+"]
    elif corruption == "opaque_partner":
        board.partners["USB_D+"] = object()
    elif corruption == "native_exception":
        del board.partners["USB_D+"]
    result = context(board, routed)
    assert result.error
    assert result.pairs == ()


def test_unrouted_native_pair_mate_is_preserved_for_later_copper_validation() -> None:
    """Native pair identity and selected routed geometry are separate concerns."""
    board = PairBoard()
    board.tracks = (FakeTrack(net="USB_D+"),)
    snapshot = snapshot_board(board, pcbnew())
    assert len(snapshot.traces) == 1
    assert snapshot.differential_pairs == (("USB_D+", "USB_D-"),)


def test_single_ended_review_remains_valid_when_pair_api_becomes_unavailable() -> None:
    """Pairing capability must not regress unrelated single-ended exports."""
    board = PairBoard()
    specification = Specification(
        "single",
        "Single-ended",
        "50",
        "single_ended",
        "USB",
        (LayerSettings("F.Cu", ("B.Cu",)),),
    )
    config = Config(True, (specification,))
    analysis = analyze(config, snapshot_board(board, pcbnew()))
    approved = replace(
        config,
        reviewed_digest=analysis.digest,
        included_section_ids=tuple(section.section_id for section in analysis.sections),
    )
    board.DpCoupledNet = None
    reopened = Config.from_dict(approved.to_dict())
    snapshot = snapshot_board(board, pcbnew())
    assert snapshot.differential_pair_error
    validate_review(reopened, analyze(reopened, snapshot))


def test_empty_board_has_empty_pair_metadata_without_native_calls() -> None:
    """There is no error merely because no pair or named net exists yet."""
    board = PairBoard(())
    board.nets = {"": NativeNet("", 0)}
    result = context(board)
    assert not result.error
    assert result.pairs == ()
    assert not board.pair_calls


@pytest.mark.parametrize("change", ["unavailable", "lost_pair", "nonreciprocal"])
def test_native_snapshot_to_differential_approval_rejects_changed_pairing(
    change: str,
) -> None:
    """Exercise real snapshot, automatic pairing, persisted approval and invalidation."""
    board = PairBoard()
    board.tracks = (
        FakeTrack(net="USB_D+", uuid="positive"),
        FakeTrack(
            net="USB_D-", uuid="negative", start=(0, 400_000), end=(10_000_000, 400_000)
        ),
    )
    specification = Specification(
        "usb",
        "USB differential",
        "90",
        "differential",
        "USB",
        (LayerSettings("F.Cu", ("B.Cu",), spacing_nm=200_000),),
    )
    configuration = Config(True, (specification,))
    first = analyze(configuration, snapshot_board(board, pcbnew()))
    assert len(first.sections) == 1
    assert first.sections[0].net_names == ("USB_D+", "USB_D-")
    approved = replace(
        configuration,
        reviewed_digest=first.digest,
        included_section_ids=(first.sections[0].section_id,),
    )
    reopened = Config.from_dict(approved.to_dict())
    validate_review(reopened, analyze(reopened, snapshot_board(board, pcbnew())))
    if change == "unavailable":
        board.DpCoupledNet = None
    elif change == "lost_pair":
        board.partners["USB_D+"] = board.partners["USB_D-"] = None
    else:
        board.partners["USB_D+"] = None
    with pytest.raises(ValidationError):
        validate_review(reopened, analyze(reopened, snapshot_board(board, pcbnew())))
