"""Tests for IPCExportPlan scaffold behavior."""

import pytest

from export_api import IPCExportPlan


class _FakeBoard:
    @staticmethod
    def GetFileName() -> str:
        return "/tmp/fixture.kicad_pcb"


class _FakeFabrication:
    def __init__(self, version):
        self.kicad = type("Kicad", (), {"version": version})()
        self.board = _FakeBoard()
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


def test_ipc_export_plan_requires_minimum_version():
    """IPC export plan should fail fast on unsupported KiCad versions."""
    plan = IPCExportPlan(_FakeFabrication((10, 0, 0)))

    with pytest.raises(RuntimeError, match="KiCad >= 11.0.0"):
        plan.generate_gerbers()


def test_ipc_export_plan_uses_kicad_cli_fallback_by_default():
    """Without direct IPC export implementation, fallback should call kicad-cli."""
    calls = []

    def _runner(cmd, **kwargs):
        calls.append((cmd, kwargs))

    plan = IPCExportPlan(_FakeFabrication((11, 0, 0)), command_runner=_runner)
    plan.generate_gerbers()
    plan.generate_drill_files()

    assert calls[0][0] == [
        "kicad-cli",
        "pcb",
        "export",
        "gerbers",
        "--output",
        "/tmp/gerber-output",
        "/tmp/fixture.kicad_pcb",
    ]
    assert calls[1][0] == [
        "kicad-cli",
        "pcb",
        "export",
        "drill",
        "--output",
        "/tmp/gerber-output",
        "/tmp/fixture.kicad_pcb",
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

    assert calls[0][0][:4] == ["kicad-cli", "pcb", "export", "gerbers"]


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
                "board_file": "/tmp/fixture.kicad_pcb",
                "output_dir": "/tmp/gerber-output",
            },
        ),
        (
            "export_drill",
            {
                "board_file": "/tmp/fixture.kicad_pcb",
                "output_dir": "/tmp/gerber-output",
            },
        ),
    ]
