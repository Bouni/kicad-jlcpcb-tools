"""Tests for export plan abstraction and SWIG export delegation."""

from export_api import IPCExportPlan, ExportPlan, SWIGExportPlan, create_export_plan


class _FakeFabrication:
    def __init__(self, tmp_path=None, version=(11, 0, 0)):
        self.logger = type(
            "Logger",
            (),
            {
                "info": staticmethod(lambda *_args, **_kwargs: None),
                "error": staticmethod(lambda *_args, **_kwargs: None),
            },
        )()
        self.parent = type(
            "Parent",
            (),
            {
                "settings": {
                    "gerber": {
                        "plot_values": True,
                        "plot_references": True,
                        "tented_vias": True,
                    }
                }
            },
        )()
        self.board = object()
        self.gerberdir = str(tmp_path) if tmp_path is not None else "."

        class _FakeGerber:
            def __init__(self):
                self.calls = []

            @staticmethod
            def create_plot_controller(_board):
                return object()

            @staticmethod
            def get_plot_options(_plot_controller):
                return object()

            def __getattr__(self, name):
                if name in {
                    "set_output_directory",
                    "set_format",
                    "set_plot_component_values",
                    "set_plot_reference_designators",
                    "set_sketch_pads_on_mask_layers",
                    "set_use_protel_extensions",
                    "set_create_job_file",
                    "set_mask_color",
                    "set_use_auxiliary_origin",
                    "set_plot_vias_on_mask",
                    "set_use_x2_format",
                    "set_include_netlist_attributes",
                    "set_disable_macros",
                    "set_drill_marks",
                    "set_plot_frame_ref",
                    "set_skip_plot_npth_pads",
                    "set_layer",
                    "open_plot_file",
                    "close_plot",
                    "set_drill_options",
                    "set_drill_format",
                    "generate_drill_files",
                }:
                    return lambda *args, **kwargs: self.calls.append(
                        (name, args, kwargs)
                    )
                raise AttributeError(name)

            @staticmethod
            def plot_layer(_plot_controller):
                return True

            @staticmethod
            def create_excellon_writer(_board):
                return object()

        class _FakeUtility:
            @staticmethod
            def get_layer_constants():
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
            def get_no_drill_shape():
                return 0

            @staticmethod
            def get_plot_format_gerber():
                return 1

            @staticmethod
            def get_inner_cu_layer(layer):
                return 100 + layer

        class _FakeBoard:
            @staticmethod
            def get_copper_layer_count():
                return 2

            @staticmethod
            def get_enabled_layers():
                return [0, 1, 2, 3, 4, 5, 7, 8, 9]

            @staticmethod
            def get_layer_name(layer_id):
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
                }
                return names[layer_id]

            @staticmethod
            def get_aux_origin():
                return (0, 0)

        self.kicad = type(
            "Kicad",
            (),
            {
                "version": version,
                "gerber": _FakeGerber(),
                "utility": _FakeUtility(),
                "board": _FakeBoard(),
            },
        )()


def test_swig_export_plan_implements_export_plan():
    """SWIGExportPlan should satisfy the ExportPlan contract."""
    plan = SWIGExportPlan(_FakeFabrication())

    assert isinstance(plan, ExportPlan)


def test_swig_export_plan_runs_export_flow(tmp_path):
    """SWIG export plan should own Gerber/Drill generation behavior."""
    fake = _FakeFabrication(tmp_path=tmp_path)
    plan = SWIGExportPlan(fake)

    plan.generate_gerbers(layer_count=4)
    plan.generate_drill_files()

    method_names = [call[0] for call in fake.kicad.gerber.calls]
    assert "set_format" in method_names
    assert "open_plot_file" in method_names
    assert "generate_drill_files" in method_names


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


def test_create_export_plan_selects_ipc_in_context_on_older_versions(monkeypatch):
    """IPC context on older versions should still select IPCExportPlan."""
    monkeypatch.setenv("KICAD_API_SOCKET", "/tmp/kicad-api.sock")

    plan = create_export_plan(_FakeFabrication(version=(10, 0, 0)))

    assert isinstance(plan, IPCExportPlan)
