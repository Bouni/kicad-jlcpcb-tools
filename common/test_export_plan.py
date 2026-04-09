"""Tests for export plan abstraction and SWIG export delegation."""

from export_api import ExportPlan, SWIGExportPlan


class _FakeFabrication:
    def __init__(self):
        self.gerber_calls = []
        self.drill_calls = 0

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
