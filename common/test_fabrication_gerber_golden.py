"""Golden-file parity tests for normalized Gerber/Drill content."""

from pathlib import Path
import re

import pytest

from fabrication import Fabrication


GERBER_VOLATILE_PATTERNS = [
    re.compile(r"^%TF\.CreationDate,.*\*%$"),
    re.compile(r"^%TF\.GenerationSoftware,.*\*%$"),
    re.compile(r"^G04 Created by KiCad.*\*$"),
    re.compile(r"^G04 #@\$ CreationDate,.*\*$"),
]

DRILL_VOLATILE_PATTERNS = [
    re.compile(r"^; DRILL file \{.*\} date .*$"),
    re.compile(r"^; #@! TF\.CreationDate,.*$"),
    re.compile(r"^; #@! TF\.GenerationSoftware,.*$"),
]


class _Point:
    def __init__(self, x: int, y: int):
        self.x = x
        self.y = y

    def __iter__(self):
        yield self.x
        yield self.y


class _PathBoard:
    def __init__(self, board_path: Path):
        self._path = board_path

    def GetFileName(self) -> str:
        return str(self._path)


class _FakeParent:
    def __init__(self):
        self.settings = {
            "gerber": {
                "fill_zones": True,
                "tented_vias": True,
                "plot_values": False,
                "plot_references": False,
            }
        }


class _BoardAdapter:
    def __init__(self, layer_count: int):
        self._layer_count = layer_count

    def get_copper_layer_count(self) -> int:
        return self._layer_count

    @staticmethod
    def get_enabled_layers():
        return [0, 1, 2, 3, 4, 5, 7, 8, 9, 42]

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
            42: "JLC_Assy",
        }
        return names[layer_id]

    @staticmethod
    def get_aux_origin():
        return _Point(0, 0)


class _UtilityAdapter:
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
    def get_no_drill_shape() -> int:
        return 99

    @staticmethod
    def get_plot_format_gerber() -> int:
        return 1

    @staticmethod
    def get_inner_cu_layer(layer: int) -> int:
        return 100 + layer

    @staticmethod
    def refill_zones(_board) -> None:
        return None


class _GoldenGerberBase:
    def __init__(self):
        self.output_directory = None
        self._current_filename = None

    @staticmethod
    def create_plot_controller(_board):
        return object()

    @staticmethod
    def get_plot_options(_plot_controller):
        return object()

    def set_output_directory(self, _plot_options, directory: str) -> None:
        self.output_directory = directory

    @staticmethod
    def set_format(_plot_options, _format_id: int) -> None:
        return None

    @staticmethod
    def set_plot_component_values(_plot_options, _value: bool) -> None:
        return None

    @staticmethod
    def set_plot_reference_designators(_plot_options, _value: bool) -> None:
        return None

    @staticmethod
    def set_sketch_pads_on_mask_layers(_plot_options, _value: bool) -> None:
        return None

    @staticmethod
    def set_use_protel_extensions(_plot_options, _value: bool) -> None:
        return None

    @staticmethod
    def set_create_job_file(_plot_options, _value: bool) -> None:
        return None

    @staticmethod
    def set_mask_color(_plot_options, _value: bool) -> None:
        return None

    @staticmethod
    def set_use_auxiliary_origin(_plot_options, _value: bool) -> None:
        return None

    @staticmethod
    def set_plot_vias_on_mask(_plot_options, _value: bool) -> None:
        return None

    @staticmethod
    def set_use_x2_format(_plot_options, _value: bool) -> None:
        return None

    @staticmethod
    def set_include_netlist_attributes(_plot_options, _value: bool) -> None:
        return None

    @staticmethod
    def set_disable_macros(_plot_options, _value: bool) -> None:
        return None

    @staticmethod
    def set_drill_marks(_plot_options, _mark_type: int) -> None:
        return None

    @staticmethod
    def set_plot_frame_ref(_plot_options, _value: bool) -> None:
        return None

    @staticmethod
    def set_skip_plot_npth_pads(_plot_options, _value: bool) -> None:
        return None

    @staticmethod
    def set_layer(_plot_controller, _layer_id: int) -> None:
        return None

    def open_plot_file(
        self, _plot_controller, filename: str, _extension: str, _plot_title: str
    ) -> None:
        self._current_filename = filename

    def plot_layer(self, _plot_controller) -> bool:
        assert self.output_directory is not None
        assert self._current_filename is not None
        Path(self.output_directory).mkdir(parents=True, exist_ok=True)
        path = Path(self.output_directory) / f"{self._current_filename}.gbr"
        lines = [
            "%TF.CreationDate,2026-04-08T12:00:00+00:00*%",
            "%TF.GenerationSoftware,KiCad,Pcbnew,9.0.0*%",
            "G04 Created by KiCad*",
            "G04 #@$ CreationDate,2026-04-08*",
            f"G04 LAYER:{self._current_filename}*",
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True

    @staticmethod
    def close_plot(_plot_controller) -> None:
        return None

    @staticmethod
    def create_excellon_writer(_board):
        return object()

    @staticmethod
    def set_drill_options(_writer, **kwargs) -> None:
        return None

    @staticmethod
    def set_drill_format(_writer, _metric: bool) -> None:
        return None

    def generate_drill_files(self, _writer, output_directory: str) -> None:
        Path(output_directory).mkdir(parents=True, exist_ok=True)
        lines = [
            "; DRILL file {fixture} date 2026-04-08",
            "; #@! TF.CreationDate,2026-04-08T12:00:00+00:00",
            "; #@! TF.GenerationSoftware,KiCad,Pcbnew,9.0.0",
            "M48",
            "T01C0.300",
            "%",
        ]
        (Path(output_directory) / "drill.drl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )


class _SWIGGoldenGerber(_GoldenGerberBase):
    pass


class _IPCGoldenGerber(_GoldenGerberBase):
    pass


class _AdapterSet:
    def __init__(self, gerber, layer_count: int):
        self.board = _BoardAdapter(layer_count)
        self.utility = _UtilityAdapter()
        self.gerber = gerber


def _normalize_gerber_text(text: str) -> str:
    lines = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if any(pattern.match(stripped) for pattern in GERBER_VOLATILE_PATTERNS):
            continue
        lines.append(stripped)
    return "\n".join(lines).strip() + "\n"


def _normalize_drill_text(text: str) -> str:
    lines = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if any(pattern.match(stripped) for pattern in DRILL_VOLATILE_PATTERNS):
            continue
        lines.append(stripped)
    return "\n".join(lines).strip() + "\n"


def _normalize_output_file(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".drl":
        return _normalize_drill_text(text)
    return _normalize_gerber_text(text)


def _generate_output(tmp_path: Path, backend: str, layer_count: int):
    board_path = tmp_path / backend / "fixture.kicad_pcb"
    board_path.parent.mkdir(parents=True, exist_ok=True)
    board_path.write_text("", encoding="utf-8")

    parent = _FakeParent()
    board = _PathBoard(board_path)
    gerber = _SWIGGoldenGerber() if backend == "swig" else _IPCGoldenGerber()

    fab = Fabrication(parent, board, adapter_set=_AdapterSet(gerber, layer_count))
    fab.fill_zones()
    fab.generate_geber(layer_count=layer_count)
    fab.generate_excellon()

    files = sorted(
        [
            p
            for p in Path(fab.gerberdir).iterdir()
            if p.is_file() and p.suffix.lower() in {".gbr", ".drl"}
        ],
        key=lambda p: p.name,
    )
    return {p.name: _normalize_output_file(p) for p in files}


def _read_golden_outputs(profile: str) -> dict[str, str]:
    root = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "gerber_golden" / profile
    files = sorted(
        [p for p in root.iterdir() if p.is_file() and p.suffix.lower() in {".gbr", ".drl"}],
        key=lambda p: p.name,
    )
    normalized = {}
    for p in files:
        text = p.read_text(encoding="utf-8")
        if p.suffix.lower() == ".drl":
            normalized[p.name] = _normalize_drill_text(text)
        else:
            normalized[p.name] = _normalize_gerber_text(text)
    return normalized


@pytest.mark.parametrize(
    "profile, layer_count",
    [
        ("2layer", 2),
        ("4layer", 4),
    ],
)
def test_normalized_gerber_drill_match_goldens_for_swig_and_ipc(
    tmp_path, profile, layer_count
):
    """Normalized Gerber/Drill outputs should match checked-in golden files for both backends."""
    golden = _read_golden_outputs(profile)
    swig = _generate_output(tmp_path, "swig", layer_count)
    ipc = _generate_output(tmp_path, "ipc", layer_count)

    assert swig.keys() == golden.keys()
    assert ipc.keys() == golden.keys()
    assert swig == golden
    assert ipc == golden
