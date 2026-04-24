"""Tests for IPCExportPlan scaffold behavior."""

import os

from export_api import IPCExportPlan


_EXISTING_BOARD_FILE = __file__


class _FakeBoard:
    @staticmethod
    def GetFileName() -> str:
        return _EXISTING_BOARD_FILE

    @staticmethod
    def get_copper_layer_count() -> int:
        return 2

    @staticmethod
    def get_enabled_layers() -> list[int]:
        return [0, 1, 2, 3, 4, 5, 7, 8, 9]

    @staticmethod
    def get_layer_name(layer_id: int) -> str:
        names = {
            0: "F_Cu",
            1: "B_Cu",
            2: "F_SilkS",
            3: "B_SilkS",
            4: "F_Mask",
            5: "B_Mask",
            7: "F_Paste",
            8: "B_Paste",
            9: "Edge_Cuts",
            101: "In2_Cu",
            102: "In3_Cu",
        }
        return names[layer_id]


class _FakeUtility:
    @staticmethod
    def get_layer_constants() -> dict[str, int]:
        return {
            "F_Cu": 0,
            "B_Cu": 1,
            "F_SilkS": 2,
            "B_SilkS": 3,
            "F_Mask": 4,
            "B_Mask": 5,
            "F_Paste": 7,
            "B_Paste": 8,
            "Edge_Cuts": 9,
        }

    @staticmethod
    def get_inner_cu_layer(layer: int) -> int:
        return 100 + layer


class _FakeFabrication:
    def __init__(self, version):
        self.board = _FakeBoard()
        self.kicad = type(
            "Kicad",
            (),
            {"version": version, "board": self.board, "utility": _FakeUtility()},
        )()
        self.gerberdir = "/tmp/gerber-output"


class _FakeIPCClient:
    def __init__(self, available=True):
        self._available = available
        self.calls = []

    def is_available(self):
        return self._available

    def export_gerbers(self, board_file, output_dir):
        self.calls.append(("export_gerbers", {"board_file": board_file, "output_dir": output_dir}))

    def export_drill(self, board_file, output_dir):
        self.calls.append(("export_drill", {"board_file": board_file, "output_dir": output_dir}))


def test_ipc_export_plan_uses_cli_fallback_on_older_kicad_versions():
    """Older KiCad versions should skip direct IPC export and use CLI fallback."""
    calls = []

    def _runner(cmd, **kwargs):
        calls.append((cmd, kwargs))

    plan = IPCExportPlan(_FakeFabrication((10, 0, 0)), command_runner=_runner)
    plan.generate_gerbers()

    assert os.path.basename(calls[0][0][0]) == "kicad-cli"
    assert calls[0][0][1:4] == ["pcb", "export", "gerbers"]


def test_ipc_export_plan_uses_kicad_cli_fallback_by_default():
    """Without direct IPC export implementation, fallback should call kicad-cli."""
    calls = []

    def _runner(cmd, **kwargs):
        calls.append((cmd, kwargs))

    plan = IPCExportPlan(_FakeFabrication((11, 0, 0)), command_runner=_runner)
    plan.generate_gerbers()
    plan.generate_drill_files()

    assert os.path.basename(calls[0][0][0]) == "kicad-cli"
    assert calls[0][0][1:] == [
        "pcb",
        "export",
        "gerbers",
        "--output",
        "/tmp/gerber-output",
        "--layers",
        "F.Cu,F.SilkS,F.Mask,F.Paste,B.Cu,B.SilkS,B.Mask,Edge.Cuts,B.Paste",
        "--no-protel-ext",
        "--use-drill-file-origin",
        _EXISTING_BOARD_FILE,
    ]
    assert os.path.basename(calls[1][0][0]) == "kicad-cli"
    assert calls[1][0][1:] == [
        "pcb",
        "export",
        "drill",
        "--output",
        "/tmp/gerber-output",
        _EXISTING_BOARD_FILE,
    ]
    assert calls[0][1]["check"] is True
    assert calls[1][1]["check"] is True


def test_ipc_export_plan_falls_back_when_direct_ipc_export_fails():
    """When direct IPC export path raises, plan should fall back to kicad-cli."""

    class _DirectIPCFailingPlan(IPCExportPlan):
        def _ipc_export_available(self) -> bool:
            return True

        def _run_ipc_gerber_export(self, _layer_count=None) -> None:
            raise RuntimeError("ipc unavailable")

    calls = []

    def _runner(cmd, **kwargs):
        calls.append((cmd, kwargs))

    plan = _DirectIPCFailingPlan(_FakeFabrication((11, 0, 0)), command_runner=_runner)
    plan.generate_gerbers(layer_count=4)

    assert os.path.basename(calls[0][0][0]) == "kicad-cli"
    assert calls[0][0][1:] == [
        "pcb",
        "export",
        "gerbers",
        "--output",
        "/tmp/gerber-output",
        "--layers",
        "F.Cu,F.SilkS,F.Mask,F.Paste,In1.Cu,In2.Cu,B.Cu,B.SilkS,B.Mask,Edge.Cuts,B.Paste",
        "--no-protel-ext",
        "--use-drill-file-origin",
        _EXISTING_BOARD_FILE,
    ]


def test_ipc_export_plan_uses_one_layer_plot_plan_for_single_layer_board():
    """Single-layer boards should use the same reduced layer set as SWIG export."""
    calls = []

    def _runner(cmd, **kwargs):
        calls.append((cmd, kwargs))

    fabrication = _FakeFabrication((10, 0, 0))
    fabrication.kicad.board.get_copper_layer_count = staticmethod(lambda: 1)
    plan = IPCExportPlan(fabrication, command_runner=_runner)

    plan.generate_gerbers(layer_count=1)

    assert calls[0][0][6:] == [
        "--layers",
        "F.Cu,F.SilkS,F.Mask,F.Paste,Edge.Cuts,B.Paste",
        "--no-protel-ext",
        "--use-drill-file-origin",
        _EXISTING_BOARD_FILE,
    ]


def test_ipc_export_plan_uses_direct_ipc_when_available():
    """When IPC transport is available, direct IPC export should be used."""
    cli_calls = []

    def _runner(cmd, **kwargs):
        cli_calls.append((cmd, kwargs))

    ipc_client = _FakeIPCClient(available=True)
    plan = IPCExportPlan(
        _FakeFabrication((11, 0, 0)),
        command_runner=_runner,
        ipc_client=ipc_client,
    )

    plan.generate_gerbers(layer_count=4)
    plan.generate_drill_files()

    assert cli_calls == []
    assert ipc_client.calls == [
        (
            "export_gerbers",
            {
                "board_file": _EXISTING_BOARD_FILE,
                "output_dir": "/tmp/gerber-output",
            },
        ),
        (
            "export_drill",
            {
                "board_file": _EXISTING_BOARD_FILE,
                "output_dir": "/tmp/gerber-output",
            },
        ),
    ]
