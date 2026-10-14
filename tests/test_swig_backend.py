"""The SWIG backend's reads, over SWIG-shaped stand-ins for KiCad's objects."""

from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from .swig_doubles import Board, Field, Footprint, Pad, editor
from .wx_harness import load_siblings


@pytest.fixture
def swig() -> Iterator[SimpleNamespace]:
    """Load the SWIG backend under a scoped name; no pcbnew import is needed."""
    with load_siblings(
        "_swig_backend_tests",
        (
            "kicad_backend",
            "kicad_backend.records",
            "kicad_backend.swig",
            "kicad_backend.swig.read",
        ),
        {},
    ) as modules:
        yield SimpleNamespace(
            api=modules["kicad_backend"],
            backend=modules["kicad_backend.swig"].SwigBackend,
            read=modules["kicad_backend.swig.read"],
            records=modules["kicad_backend.records"],
        )


def test_snapshot_reads_each_footprint_in_board_order(swig: SimpleNamespace) -> None:
    """Fields, the exclusion bits, DNP, the side and the pads become plain records."""
    pads = [Pad("1", "smd"), Pad("2", "pth"), Pad("", "npth"), Pad("3", "conn")]
    connector = Footprint(
        "J1",
        value="Header",
        footprint="Connector:Header_1x02",
        fields={"LCSC": "C1"},
        attributes=0x01 | 0x02 | 0x04,
        pads=pads,
        path="/root/symbol",
    )
    chip = Footprint("U1", attributes=0x08 | 0x10, dnp=True, flipped=True)
    board = Board([connector, chip])

    snapshot = swig.backend(editor(board)).snapshot()

    first, second = snapshot.components
    assert (first.uuid, first.reference, first.footprint_lib_id) == (
        connector.uuid,
        "J1",
        "Connector:Header_1x02",
    )
    assert [(f.name, f.text) for f in first.default.fields.values()] == [
        ("Reference", "J1"),
        ("Value", "Header"),
        ("Datasheet", ""),
        ("Description", ""),
        ("LCSC", "C1"),
    ]
    assert (first.schematic_path, first.side) == ("/root/symbol", "top")
    assert (first.default.exclude_from_bom, first.default.exclude_from_pos) == (
        False,
        True,
    )
    assert [(p.kind, p.has_hole) for p in first.pads] == [
        ("smd", False),
        ("pth", True),
        ("npth", True),
        ("conn", False),
    ]
    assert second.side == "bottom"
    assert (
        second.default.dnp,
        second.default.exclude_from_bom,
        second.default.exclude_from_pos,
    ) == (True, True, False)
    assert [variant.name for variant in snapshot.variants] == [""]


@pytest.mark.parametrize(
    "variants, label", [(None, "Default"), (["< Default >", "A"], "< Default >")]
)
def test_default_label_follows_kicads_variant_api(
    swig: SimpleNamespace, variants: object, label: str
) -> None:
    """KiCad 10 names Default in its own list; KiCad 9 has no variant API."""
    backend = swig.backend(editor(Board([Footprint()], variants=variants)))

    assert backend.snapshot().variants[0].label == label


def test_a_snapshot_names_its_board_and_file(swig: SimpleNamespace) -> None:
    """The token is the board's identity; saving under another name changes it."""
    board = Board()
    info = swig.backend(editor(board)).snapshot().info

    assert info.file_name == "/project/board.kicad_pcb"
    assert info.board_token == swig.read.board_identity(board)
    board.filename = "/project/saved-as.kicad_pcb"
    assert swig.backend(editor(board)).snapshot().info.board_token != info.board_token


def test_two_fields_with_one_name_read_as_kicad_reopens_the_board(
    swig: SimpleNamespace,
) -> None:
    """Every field is read; the state keeps the later one and records the other."""
    footprint = Footprint("R2", fields={"LCSC": "C1"})
    footprint.fields["Second LCSC"] = Field("LCSC", "C2")

    snapshot = swig.backend(editor(Board([Footprint("R1"), footprint]))).snapshot()

    state = snapshot.components[1].default
    assert state.fields["LCSC"] == swig.records.FieldRecord("LCSC", "C2")
    assert state.replaced_fields == (swig.records.FieldRecord("LCSC", "C1"),)
    assert state.duplicate_names == ("LCSC",)


def test_footprints_sharing_a_uuid_are_read_apart(swig: SimpleNamespace) -> None:
    """Both keep the UUID; the later one in board order is keyed "#2"."""
    board = Board([Footprint("R1", uuid="shared"), Footprint("R7", uuid="shared")])

    snapshot = swig.backend(editor(board)).snapshot()

    assert [(c.reference, c.uuid, c.key) for c in snapshot.components] == [
        ("R1", "shared", "shared"),
        ("R7", "shared", "shared#2"),
    ]


def test_a_closed_editor_raises_board_changed(swig: SimpleNamespace) -> None:
    """With no board open a read returns none."""
    backend = swig.backend(editor(None))

    with pytest.raises(swig.api.BoardChanged, match="no longer has an open board"):
        backend.snapshot()


class _KiCad7Footprint(Footprint):
    """A KiCad 7 footprint: a property map instead of fields, and no DNP flag."""

    def __init__(self, properties: dict[str, str]) -> None:
        super().__init__("R7", value="4k7")
        self.properties = properties

    def GetFields(self) -> list:
        """Fail as KiCad 7's footprints do."""
        raise AttributeError("GetFields")

    def GetProperties(self) -> dict[str, str]:
        """Return the property map."""
        return dict(self.properties)

    def __getattribute__(self, name: str) -> object:
        """Hide IsDNP, which KiCad 7 lacks."""
        if name == "IsDNP":
            raise AttributeError(name)
        return super().__getattribute__(name)


def test_kicad7_footprints_read_their_property_maps(swig: SimpleNamespace) -> None:
    """Reference and Value stand first, then the property map; no DNP."""
    footprint = _KiCad7Footprint({"LCSC": " c25804 ", "MPN": "RC0603"})

    (record,) = swig.backend(editor(Board([footprint]), "7.0.11")).snapshot().components

    assert [(f.name, f.text) for f in record.default.fields.values()] == [
        ("Reference", "R7"),
        ("Value", "4k7"),
        ("LCSC", " c25804 "),
        ("MPN", "RC0603"),
    ]
    assert record.default.dnp is False
