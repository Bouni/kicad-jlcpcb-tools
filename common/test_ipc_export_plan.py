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
