"""Tests for export plan abstraction and SWIG export delegation."""

from export_api import IPCExportPlan, ExportPlan, SWIGExportPlan, create_export_plan


class _FakeFabrication:
    def __init__(self, version=(11, 0, 0)):
        self.gerber_calls = []
        self.drill_calls = 0
        self.kicad = type("Kicad", (), {"version": version})()

    def _generate_gerber_impl(self, layer_count=None):
        self.gerber_calls.append(layer_count)

    def _generate_excellon_impl(self):
        self.drill_calls += 1


def test_swig_export_plan_implements_export_plan():
    """SWIGExportPlan should satisfy the ExportPlan contract."""
    plan = SWIGExportPlan(_FakeFabrication())

    assert isinstance(plan, ExportPlan)


def test_swig_export_plan_delegates_to_fabrication_impls():
    """SWIG export plan should delegate gerber/drill generation to fabrication internals."""
    fake = _FakeFabrication()
    plan = SWIGExportPlan(fake)

    plan.generate_gerbers(layer_count=4)
    plan.generate_gerbers()
    plan.generate_drill_files()

    assert fake.gerber_calls == [4, None]
    assert fake.drill_calls == 1


def test_create_export_plan_defaults_to_swig_without_ipc_context(monkeypatch):
    """Default selection should remain SWIG when IPC launch context is absent."""
    monkeypatch.delenv("KICAD_API_SOCKET", raising=False)
    monkeypatch.delenv("KICAD_API_TOKEN", raising=False)
    monkeypatch.delenv("KICAD_IPC_SOCKET", raising=False)

    plan = create_export_plan(_FakeFabrication(version=(11, 0, 0)))

    assert isinstance(plan, SWIGExportPlan)


def test_create_export_plan_selects_ipc_in_ipc_context_with_supported_version(
    monkeypatch,
):
    """IPC launch context on supported versions should select IPCExportPlan."""
    monkeypatch.setenv("KICAD_API_SOCKET", "/tmp/kicad-api.sock")

    plan = create_export_plan(_FakeFabrication(version=(11, 0, 0)))

    assert isinstance(plan, IPCExportPlan)


def test_create_export_plan_falls_back_to_swig_on_unsupported_version(monkeypatch):
    """IPC context on unsupported versions should fall back to SWIG plan."""
    monkeypatch.setenv("KICAD_API_SOCKET", "/tmp/kicad-api.sock")

    plan = create_export_plan(_FakeFabrication(version=(10, 0, 0)))

    assert isinstance(plan, SWIGExportPlan)
