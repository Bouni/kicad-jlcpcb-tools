"""Tests for IPC-backed adapter implementations."""

from types import SimpleNamespace

from ipc_impl import IPCBoardAdapter, IPCFootprintAdapter, IPCPoint, IPCUtilityAdapter


class FakeText:
    def __init__(self, value):
        self.value = value


class FakeField:
    def __init__(self, name, value):
        self.name = name
        self.text = FakeText(value)


class FakePad:
    def __init__(self, ident, number, x, y):
        self.id = ident
        self.number = number
        self.position = SimpleNamespace(x=x, y=y)


class FakeFootprint:
    def __init__(self, ident, ref, value, package, layer, orientation_deg, x, y):
        self.id = ident
        self.reference_field = SimpleNamespace(text=FakeText(ref))
        self.value_field = SimpleNamespace(text=FakeText(value))
        self.layer = layer
        self.orientation = SimpleNamespace(degrees=orientation_deg)
        self.position = SimpleNamespace(x=x, y=y)
        self.attributes = SimpleNamespace(
            exclude_from_position_files=False,
            exclude_from_bill_of_materials=False,
            do_not_populate=False,
        )
        self._fields = [FakeField("LCSC", "C1234")]
        self.definition = SimpleNamespace(
            id=SimpleNamespace(name=package),
            pads=[FakePad(f"{ident}-pad", "1", x + 1, y + 1)],
            add_item=lambda item: self._fields.append(item),
        )

    @property
    def texts_and_fields(self):
        return self._fields


class FakeBoard:
    def __init__(self):
        self.document = SimpleNamespace(board_filename="/example/test-board.kicad_pcb")
        self.fp_u1 = FakeFootprint("fp-2", "U1", "MCU", "QFN-32", 0, 90.0, 1200, 3400)
        self.fp_r1 = FakeFootprint("fp-1", "R1", "10k", "0402", 1, 180.0, 100, 200)
        self.fp_r1.attributes.exclude_from_bill_of_materials = True
        self.fp_r1.attributes.do_not_populate = True
        self._selection = [self.fp_r1]
        self.updated_items = []
        self.added_selection = []
        self.removed_selection = []
        self.refill_calls = 0

    def get_copper_layer_count(self):
        return 4

    def get_footprints(self):
        return [self.fp_u1, self.fp_r1]

    def get_enabled_layers(self):
        return [0, 1, 9]

    def get_layer_name(self, layer_id):
        return {9: "Edge_Cuts", 0: "F_Cu", 1: "B_Cu"}.get(layer_id, str(layer_id))

    def get_selection(self):
        return list(self._selection)

    def get_origin(self, _origin_type):
        return SimpleNamespace(x=11, y=22)

    def update_items(self, items):
        self.updated_items.append(list(items))

    def add_to_selection(self, items):
        self.added_selection.append(list(items))

    def remove_from_selection(self, items):
        self.removed_selection.append(list(items))

    def refill_zones(self):
        self.refill_calls += 1


class FakeIPCClient:
    """Very small fake IPC client for adapter tests."""

    def __init__(self):
        self._board = FakeBoard()

    def board(self):
        return self._board


def test_ipc_board_adapter_maps_board_calls():
    """Board adapter should expose board metadata and list payloads."""
    client = FakeIPCClient()
    adapter = IPCBoardAdapter(client)

    assert adapter.get_board_filename() == "/example/test-board.kicad_pcb"
    assert adapter.get_copper_layer_count() == 4
    assert [fp["reference"] for fp in adapter.get_all_footprints()] == ["R1", "U1"]
    assert [fp["reference"] for fp in adapter.get_footprints()] == ["U1", "R1"]
    assert adapter.get_enabled_layers() == [0, 1, 9]
    assert adapter.get_layer_name(9) == "Edge_Cuts"
    assert adapter.get_current_selection()[0]["id"] == "fp-1"
    assert adapter.get_aux_origin().x == 11
    assert adapter.get_aux_origin().y == 22
    assert adapter.get_drawings() == []
    assert adapter.get_design_settings() == {}
    assert adapter.get_footprint_by_reference("R1")["id"] == "fp-1"


def test_ipc_footprint_adapter_reads_metadata_and_fields():
    """Footprint adapter should expose metadata from payload dicts."""
    client = FakeIPCClient()
    board_adapter = IPCBoardAdapter(client)
    adapter = IPCFootprintAdapter(client)
    footprint = board_adapter.get_footprints()[0]

    assert adapter.get_reference(footprint) == "U1"
    assert adapter.get_value(footprint) == "MCU"
    assert adapter.get_fpid_name(footprint) == "QFN-32"
    assert adapter.get_layer(footprint) == 0
    assert adapter.get_orientation(footprint) == 90.0
    assert adapter.get_position(footprint) == (1200.0, 3400.0)
    assert adapter.get_lcsc_value(footprint) == "C1234"
    assert adapter.get_exclude_from_pos(footprint) is False
    assert adapter.get_exclude_from_bom(footprint) is False
    assert adapter.get_is_dnp(footprint) is False
    assert len(adapter.get_pads(footprint)) == 1


def test_ipc_footprint_adapter_mutations_update_board_state():
    """Footprint mutation helpers should update live board items."""
    client = FakeIPCClient()
    board_adapter = IPCBoardAdapter(client)
    adapter = IPCFootprintAdapter(client)
    footprint = board_adapter.get_footprints()[1]

    adapter.set_lcsc_value(footprint, "C777")
    assert adapter.get_lcsc_value(footprint) == "C777"

    assert adapter.toggle_exclude_from_pos(footprint) is True
    assert adapter.toggle_exclude_from_bom(footprint) is False
    adapter.set_selected(footprint)
    adapter.clear_selected(footprint)

    board = client.board()
    assert board.updated_items, "Expected board.update_items calls"
    assert board.added_selection, "Expected board.add_to_selection call"
    assert board.removed_selection, "Expected board.remove_from_selection call"


def test_ipc_utility_adapter_helpers_are_stable():
    """Utility adapter should provide deterministic helpers and point wrappers."""
    client = FakeIPCClient()
    adapter = IPCUtilityAdapter(client)

    assert adapter.from_mm(1.25) == 1250000
    assert adapter.to_mm(2500000) == 2.5
    assert adapter.get_layer_constants()["F_Cu"] == 0
    assert adapter.get_plot_format_gerber() == 1

    point = adapter.create_vector2i(10, 20)
    assert isinstance(point, IPCPoint)
    assert (point.x, point.y) == (10, 20)

    adapter.refill_zones({"id": "board-1"})
    assert client.board().refill_calls == 1
