"""The board records and the in-memory backend."""

from collections.abc import Iterator
from types import ModuleType, SimpleNamespace

import pytest

from .wx_harness import load_siblings


@pytest.fixture
def backend() -> Iterator[SimpleNamespace]:
    """Load the backend package under a scoped name, without KiCad or wx."""
    names = ("kicad_backend", "records", "fake")
    with load_siblings(
        "_kicad_backend_tests",
        ("kicad_backend", *(f"kicad_backend.{name}" for name in names[1:])),
        {},
    ) as modules:
        yield SimpleNamespace(
            api=modules["kicad_backend"],
            **{name: modules[f"kicad_backend.{name}"] for name in names[1:]},
        )


def _snapshot(backend: SimpleNamespace, *components: object) -> object:
    """Read a fake board holding ``components``."""
    return backend.fake.FakeBackend(components).snapshot()


def test_components_keep_board_order_and_references_may_repeat(
    backend: SimpleNamespace,
) -> None:
    """Lookups by reference return every match in the order the board lists them."""
    fake = backend.fake
    second = fake.component("R1", uuid="b-second", value="22k")
    first = fake.component("R1", uuid="a-first", value="10k")
    other = fake.component("C1")
    snapshot = _snapshot(backend, second, first, other)

    assert snapshot.components == (second, first, other)
    assert snapshot.by_reference("R1") == (second, first)
    assert snapshot.by_reference("R9") == ()
    assert snapshot.get("a-first") is first
    with pytest.raises(KeyError):
        snapshot.get("missing")


@pytest.mark.parametrize(
    "problem, message",
    [
        ("empty uuid", "a UUID for every footprint"),
        ("default not first", "Default first"),
        ("duplicate variant", "Default first"),
        ("missing state", "one state per variant"),
    ],
)
def test_an_incomplete_or_ambiguous_snapshot_is_rejected(
    backend: SimpleNamespace, problem: str, message: str
) -> None:
    """A snapshot lists Default first and gives every footprint a UUID and every variant."""
    records, fake = backend.records, backend.fake
    default = records.VariantDefinition("", "Default")
    variants = (default,)
    components = [fake.component("R1", uuid="u1"), fake.component("R2", uuid="u2")]
    if problem == "empty uuid":
        components[1] = fake.component("R2", uuid="")
    elif problem == "default not first":
        variants = (records.VariantDefinition("A", "A"), default)
    elif problem == "duplicate variant":
        variants = (default, default)
    else:
        variants = (default, records.VariantDefinition("A", "A"))
    with pytest.raises(ValueError, match=message):
        records.BoardSnapshot(fake.board_info(), variants, tuple(components))


def test_states_are_frozen(backend: SimpleNamespace) -> None:
    """Field maps cannot be edited through a record."""
    records = backend.records
    state = records.component_state([records.FieldRecord("LCSC", "C1")], dnp=True)
    with pytest.raises(TypeError):
        state.fields["LCSC"] = records.FieldRecord("LCSC", "C2")  # type: ignore[index]
    assert (state.dnp, state.exclude_from_bom, state.exclude_from_pos) == (
        True,
        False,
        False,
    )
    assert (state.replaced_fields, state.duplicate_names) == ((), ())


def test_a_repeated_field_name_resolves_as_kicad_reopens_the_board(
    backend: SimpleNamespace,
) -> None:
    """The later field takes the earlier one's place; the earlier one is kept apart.

    KiCad's footprint dialog accepts a second field with a name the
    footprint already has. On reopening the board, KiCad sets the first
    field of that name to each later one's text in turn.
    """
    field = backend.records.FieldRecord
    state = backend.records.component_state(
        [
            field("A", "a1"),
            field("LCSC", "C1"),
            field("MPN", "X"),
            field("LCSC", "C2"),
            field("A", "a2"),
            field("LCSC", "C3"),
        ]
    )

    assert list(state.fields.values()) == [
        field("A", "a2"),
        field("LCSC", "C3"),
        field("MPN", "X"),
    ]
    assert state.replaced_fields == (
        field("LCSC", "C1"),
        field("A", "a1"),
        field("LCSC", "C2"),
    )
    assert state.duplicate_names == ("LCSC", "A")


def test_a_replaced_field_needs_a_field_of_its_name(
    backend: SimpleNamespace,
) -> None:
    """A state built by hand cannot record a replaced field with no successor."""
    records = backend.records
    field = records.FieldRecord("LCSC", "C1")
    with pytest.raises(ValueError, match="later field of its name"):
        records.ComponentState(
            {"LCSC": field}, False, False, False, (records.FieldRecord("MPN", "X"),)
        )


def test_a_state_keys_fields_by_name(backend: SimpleNamespace) -> None:
    """A state built without component_state cannot mislabel a field."""
    records = backend.records
    field = records.FieldRecord("LCSC", "C1")
    with pytest.raises(ValueError, match="keyed by their names"):
        records.ComponentState({"MPN": field}, False, False, False)


def test_fake_components_list_fields_as_kicad_does(backend: SimpleNamespace) -> None:
    """Reference and Value, then Datasheet and Description, then user fields."""
    record = backend.fake.component(
        "R1", value="10k", fields={"LCSC": "C1", "MPN": "X"}
    )
    assert [(field.name, field.text) for field in record.default.fields.values()] == [
        ("Reference", "R1"),
        ("Value", "10k"),
        ("Datasheet", ""),
        ("Description", ""),
        ("LCSC", "C1"),
        ("MPN", "X"),
    ]


def test_fake_components_can_repeat_a_field_name(backend: SimpleNamespace) -> None:
    """A name given again adds a second field after the others, as KiCad's dialog can."""
    record = backend.fake.component(
        "R1", fields=(("LCSC", "C1"), ("MPN", "X"), ("LCSC", "C2"))
    )
    state = record.default
    assert [(field.name, field.text) for field in state.fields.values()][4:] == [
        ("LCSC", "C2"),
        ("MPN", "X"),
    ]
    assert state.replaced_fields == (backend.records.FieldRecord("LCSC", "C1"),)
    assert state.duplicate_names == ("LCSC",)


def test_a_records_states_cannot_change_after_it_is_made(
    backend: SimpleNamespace,
) -> None:
    """A snapshot indexes its records, so their states and pads stay as read."""
    fake = backend.fake
    record = fake.component("R1", pads=[fake.pad()])

    with pytest.raises(TypeError):
        record.states["Alternative"] = record.default  # type: ignore[index]
    assert record.pads == (fake.pad(),)


def test_fake_pad_holes_follow_their_kind(backend: SimpleNamespace) -> None:
    """Plated through and bare holes have holes; SMD and connector pads do not."""
    fake = backend.fake
    assert fake.pad() == backend.records.PadRecord("smd", False)
    assert fake.pad("pth").has_hole and fake.pad("npth").has_hole
    assert not fake.pad("conn").has_hole
    assert fake.pad("smd", has_hole=True).has_hole


def test_a_closed_fake_board_raises_board_changed(backend: SimpleNamespace) -> None:
    """Closing the board fails reads the way a real backend does."""
    board = backend.fake.FakeBackend([backend.fake.component("R1")])
    assert board.snapshot().info is board.board
    assert board.reads == 1
    board.board = None
    with pytest.raises(backend.api.BoardChanged):
        board.snapshot()
    assert issubclass(backend.api.BoardChanged, backend.api.BackendError)
    assert issubclass(backend.api.BackendError, RuntimeError)


def test_footprints_sharing_a_uuid_keep_it_and_are_keyed_apart(
    backend: SimpleNamespace,
) -> None:
    """Each keeps its UUID; the second with one is keyed "#2", in board order.

    KiCad gives every footprint its own UUID, but a board file can hold two
    with one, and KiCad keeps both when it opens the file.
    """
    fake = backend.fake
    first, other, second = (
        fake.component("R1", uuid="same"),
        fake.component("C1", uuid="c1"),
        fake.component("R2", uuid="same"),
    )
    snapshot = fake.FakeBackend([first, other, second]).snapshot()

    assert [(c.reference, c.uuid, c.key) for c in snapshot.components] == [
        ("R1", "same", "same"),
        ("C1", "c1", "c1"),
        ("R2", "same", "same#2"),
    ]
    assert snapshot.get("same") is first
    assert snapshot.get("same#2").reference == "R2"
    assert snapshot.by_reference("R2") == (snapshot.get("same#2"),)


def test_footprint_keys_count_each_uuid_in_board_order(
    backend: SimpleNamespace,
) -> None:
    """The nth footprint with a UUID gets "#n"; the first keeps the bare UUID."""
    assert backend.records.footprint_keys(["a", "b", "a", "a", "b"]) == [
        "a",
        "b",
        "a#2",
        "a#3",
        "b#2",
    ]


def test_variant_definitions_have_one_home(backend: SimpleNamespace) -> None:
    """The variant adapter reuses the record type instead of declaring its own."""
    with load_siblings(
        "_kicad_backend_variant_tests",
        ("variant.native", "kicad_backend.records"),
        {},
    ) as loaded:
        native: ModuleType = loaded["variant.native"]
        records: ModuleType = loaded["kicad_backend.records"]
        assert native.VariantDefinition is records.VariantDefinition
