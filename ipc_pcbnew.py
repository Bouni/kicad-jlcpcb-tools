"""A pcbnew-shaped facade over KiCad's IPC API for KiCad 10.99 and newer.

KiCad 10.99 removed the SWIG ``pcbnew`` module and runs plugins in their own
process. The plugin's board logic is written against pcbnew objects, so this
module provides exactly the subset it uses on top of kicad-python (``kipy``),
with the same layer numbers, attribute bits and units as SWIG.

Reads come from a snapshot of the board that is reloaded once it is older
than ``SNAPSHOT_SECONDS``. Edits change that snapshot and are sent to KiCad in
one commit by ``BOARD.commit``, which gives the editor native undo and dirty
state; editing outside a commit is rejected rather than silently lost.
"""

# pyright: reportMissingImports=false
# ruff: noqa: N802, UP045

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
import json
import logging
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Optional
import uuid

from kipy import KiCad
from kipy.board import Board as KipyBoard
from kipy.board_types import BoardRectangle, BoardText, Field
from kipy.errors import ApiError, ConnectionError as KiCadConnectionError
from kipy.proto.board.board_commands_pb2 import BoardOriginType
from kipy.proto.board.board_types_pb2 import BoardLayer, FootprintMountingStyle, PadType
from kipy.proto.common import ApiStatusCode
from kipy.proto.common.commands import editor_commands_pb2
from kipy.proto.common.types.base_types_pb2 import DocumentType

logger = logging.getLogger(__name__)
# pynng logs every pipe event from its own threads; keep them out of the log box.
logging.getLogger("pynng").setLevel(logging.WARNING)

# Board reads within this window share one snapshot; KiCad edits made in the
# editor become visible once it expires (the window refreshes on activation).
SNAPSHOT_SECONDS = 1.0
# KiCad answers AS_BUSY while an interactive tool or zone fill is running.
BUSY_TIMEOUT_SECONDS = 10.0
REQUEST_TIMEOUT_MS = 120_000
CLI_TIMEOUT_SECONDS = 600

# PCB_LAYER_ID values of KiCad 9 and newer: copper layers are even, the rest odd.
F_Cu, F_Mask, B_Cu, B_Mask = 0, 1, 2, 3
F_SilkS, B_SilkS, F_Adhes, B_Adhes, F_Paste, B_Paste = 5, 7, 9, 11, 13, 15
Dwgs_User, Cmts_User, Eco1_User, Eco2_User, Edge_Cuts, Margin = 17, 19, 21, 23, 25, 27
B_CrtYd, F_CrtYd, B_Fab, F_Fab, Rescue = 29, 31, 33, 35, 37
UNDEFINED_LAYER = -1

_LAYER_IDS = {
    "F_Cu": F_Cu,
    "F_Mask": F_Mask,
    "B_Cu": B_Cu,
    "B_Mask": B_Mask,
    "F_SilkS": F_SilkS,
    "B_SilkS": B_SilkS,
    "F_Adhes": F_Adhes,
    "B_Adhes": B_Adhes,
    "F_Paste": F_Paste,
    "B_Paste": B_Paste,
    "Dwgs_User": Dwgs_User,
    "Cmts_User": Cmts_User,
    "Eco1_User": Eco1_User,
    "Eco2_User": Eco2_User,
    "Edge_Cuts": Edge_Cuts,
    "Margin": Margin,
    "B_CrtYd": B_CrtYd,
    "F_CrtYd": F_CrtYd,
    "B_Fab": B_Fab,
    "F_Fab": F_Fab,
    "Rescue": Rescue,
    **{f"In{n}_Cu": 2 * n + 2 for n in range(1, 31)},
    **{f"User_{n}": 37 + 2 * n for n in range(1, 46)},
}
# Expose In1_Cu..In30_Cu and User_1..User_45 as pcbnew does.
globals().update(_LAYER_IDS)
_TO_KIPY = {value: BoardLayer.Value(f"BL_{name}") for name, value in _LAYER_IDS.items()}
_FROM_KIPY = {kipy: value for value, kipy in _TO_KIPY.items()}

# FOOTPRINT_ATTR_T bits.
FP_THROUGH_HOLE = 1
FP_SMD = 2
FP_EXCLUDE_FROM_POS_FILES = 4
FP_EXCLUDE_FROM_BOM = 8
FP_BOARD_ONLY = 16
FP_DNP = 64

# PAD_ATTRIB values.
PAD_ATTRIB_PTH, PAD_ATTRIB_SMD, PAD_ATTRIB_CONN, PAD_ATTRIB_NPTH = 0, 1, 2, 3
_PAD_ATTRIBUTES = {
    PadType.PT_PTH: PAD_ATTRIB_PTH,
    PadType.PT_SMD: PAD_ATTRIB_SMD,
    PadType.PT_EDGE_CONNECTOR: PAD_ATTRIB_CONN,
    PadType.PT_NPTH: PAD_ATTRIB_NPTH,
}

S_SEGMENT, S_RECT = 0, 1
EDA_UNITS_MM = 1
DRILL_MARKS_NO_DRILL_SHAPE = 0
PLOT_FORMAT_GERBER = 1

_session: Optional["_Session"] = None


def ToMM(iu: float) -> float:
    """Convert internal units (nanometres) to millimetres."""
    return float(iu) / 1_000_000


def FromMM(mm: float) -> int:
    """Convert millimetres to internal units, truncating like pcbnew."""
    return int(float(mm) * 1_000_000)


def IsCopperLayer(layer: int) -> bool:
    """Return whether a PCB_LAYER_ID denotes a copper layer."""
    return 0 <= layer < 64 and layer % 2 == 0


def _wx_round(value: float) -> int:
    """Round half away from zero, as wxRound does."""
    return int(math.copysign(math.floor(abs(value) + 0.5), value))


class VECTOR2I:
    """An integer point in internal units."""

    def __init__(self, x: float, y: float) -> None:
        self.x = int(x)
        self.y = int(y)

    def __eq__(self, other: object) -> bool:
        """Compare coordinates."""
        return isinstance(other, VECTOR2I) and (self.x, self.y) == (other.x, other.y)

    def __hash__(self) -> int:
        """Hash coordinates."""
        return hash((self.x, self.y))

    def __repr__(self) -> str:
        """Show coordinates."""
        return f"VECTOR2I({self.x}, {self.y})"


class wxPoint(VECTOR2I):
    """A point built from possibly fractional coordinates."""

    def __init__(self, x: float, y: float) -> None:
        super().__init__(_wx_round(x), _wx_round(y))


class BOX2I:
    """An axis-aligned box in internal units."""

    def __init__(self, x: int, y: int, width: int, height: int) -> None:
        self._x, self._y, self._width, self._height = x, y, width, height

    def Merge(self, other: "BOX2I") -> "BOX2I":
        """Grow this box to also contain another one."""
        left = min(self._x, other._x)
        top = min(self._y, other._y)
        right = max(self._x + self._width, other._x + other._width)
        bottom = max(self._y + self._height, other._y + other._height)
        self._x, self._y = left, top
        self._width, self._height = right - left, bottom - top
        return self

    def GetCenter(self) -> VECTOR2I:
        """Return the centre with BOX2I's integer division."""
        return VECTOR2I(self._x + self._width // 2, self._y + self._height // 2)


class EDA_ANGLE:
    """An angle in degrees."""

    def __init__(self, degrees: float) -> None:
        self._degrees = float(degrees)

    def AsDegrees(self) -> float:
        """Return the angle in degrees."""
        return self._degrees


class KIID:
    """A KiCad identifier exposed as text."""

    def __init__(self, value: str) -> None:
        self._value = value

    def AsString(self) -> str:
        """Return the identifier text."""
        return self._value


class LSET:
    """An ordered set of PCB_LAYER_ID values."""

    def __init__(self, layers: Sequence[int]) -> None:
        self._layers = tuple(sorted(set(layers)))

    def Seq(self) -> tuple[int, ...]:
        """Return the layers in KiCad's order."""
        return self._layers


class LIB_ID:
    """A footprint library identifier."""

    def __init__(self, library: str, name: str) -> None:
        self._library, self._name = library, name

    def GetLibNickname(self) -> str:
        """Return the library nickname."""
        return self._library

    def GetLibItemName(self) -> str:
        """Return the footprint name."""
        return self._name

    def Format(self) -> str:
        """Return ``library:name``, or the name alone without a library."""
        return f"{self._library}:{self._name}" if self._library else self._name


def _to_kipy_layer(layer: int) -> int:
    try:
        return _TO_KIPY[layer]
    except KeyError:
        raise ValueError(f"Unknown KiCad layer id {layer}") from None


def _from_kipy_layer(layer: int) -> int:
    return _FROM_KIPY.get(layer, UNDEFINED_LAYER)


def _point(vector: Any) -> VECTOR2I:
    return VECTOR2I(vector.x, vector.y)


class _Session:
    """One connection to the KiCad instance that launched this process."""

    def __init__(self, kicad: KiCad) -> None:
        self.kicad = kicad
        self.lock = threading.RLock()
        self.version = self.call(kicad.get_version).full_version
        self.board: Optional[BOARD] = None
        self.selection_clear = False
        self.selection_add: dict[str, Any] = {}

    def call(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Serialize API requests and wait out KiCad's transient busy state."""
        deadline = time.monotonic() + BUSY_TIMEOUT_SECONDS
        with self.lock:
            while True:
                try:
                    return function(*args, **kwargs)
                except ApiError as error:
                    if (
                        error.code != ApiStatusCode.AS_BUSY
                        or time.monotonic() >= deadline
                    ):
                        raise
                time.sleep(0.05)

    def current_board(self) -> Optional["BOARD"]:
        """Return the open PCB, keeping one facade per document lifetime."""
        try:
            documents = self.call(
                self.kicad.get_open_documents, DocumentType.DOCTYPE_PCB
            )
        except (ApiError, KiCadConnectionError) as error:
            logger.warning("KiCad did not report an open PCB: %s", error)
            return None
        if not documents:
            return None
        board = self.board
        if board is None or not any(board.is_document(doc) for doc in documents):
            board = BOARD(self, KipyBoard(self.kicad._client, documents[0]))
            self.board = board
        return board

    def flush_selection(self) -> None:
        """Apply pending selection changes in two requests."""
        board = self.board
        clear, add = self.selection_clear, list(self.selection_add.values())
        self.selection_clear, self.selection_add = False, {}
        if board is None or (not clear and not add):
            return
        if clear:
            self.call(board.kipy.clear_selection)
        if add:
            self.call(board.kipy.add_to_selection, add)

    def kicad_cli(self) -> str:
        """Locate the kicad-cli belonging to the running KiCad."""
        with suppress(ApiError, KiCadConnectionError):
            path = self.call(self.kicad.get_kicad_binary_path, "kicad-cli")
            if path and os.path.exists(path):
                return path
        path = shutil.which("kicad-cli")
        if not path:
            raise RuntimeError("Unable to locate kicad-cli for the running KiCad")
        return path


def connect(kicad: Optional[KiCad] = None) -> None:
    """Connect to KiCad; without arguments use the plugin runner's socket and token."""
    global _session  # noqa: PLW0603 -- one connection per plugin process
    if kicad is None:
        kicad = KiCad(client_name="kicad-jlcpcb-tools", timeout_ms=REQUEST_TIMEOUT_MS)
    _session = _Session(kicad)


def _require_session() -> _Session:
    if _session is None:
        raise RuntimeError("JLCPCB Tools is not connected to KiCad")
    return _session


def GetBoard() -> Optional["BOARD"]:
    """Return the board open in the PCB editor, or None."""
    return _require_session().current_board()


def GetBuildVersion() -> str:
    """Return KiCad's full version string."""
    return _require_session().version


def GetMajorMinorVersion() -> str:
    """Return KiCad's ``major.minor`` version."""
    return ".".join(GetBuildVersion().split("-", 1)[0].split(".")[:2])


def GetCurrentSelection() -> list["_SelectedItem"]:
    """Return the editor selection as items that can only be deselected."""
    session = _require_session()
    board = session.current_board()
    if board is None:
        return []
    try:
        selection = session.call(board.kipy.get_selection)
    except ApiError as error:
        # Headless KiCad has no selection; nothing can be selected there.
        logger.debug("Selection unavailable: %s", error)
        return []
    return [_SelectedItem(session) for _ in selection]


def Refresh() -> None:
    """Send pending selection changes; KiCad redraws committed edits itself."""
    _require_session().flush_selection()


def SaveBoard(filename: str, board: "BOARD") -> bool:
    """Save the board through the editor."""
    board.Save(filename)
    return True


def GetSettingsManager() -> "_SettingsManager":
    """Return a settings manager able to identify the board's project file."""
    return _SettingsManager()


def WriteDRCReport(
    board: "BOARD", filename: str, units: int, report_all_track_errors: bool
) -> bool:
    """Run DRC on the board's saved file with kicad-cli, writing a text report."""
    if units != EDA_UNITS_MM:
        raise ValueError("Only millimetre DRC reports are supported")
    command = [
        _require_session().kicad_cli(),
        "pcb",
        "drc",
        "--format",
        "report",
        "--units",
        "mm",
        "--output",
        filename,
    ]
    if report_all_track_errors:
        command.append("--all-track-errors")
    command.append(board.GetFileName())
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        logger.warning(
            "kicad-cli DRC failed (%d): %s",
            completed.returncode,
            completed.stderr.strip() or completed.stdout.strip(),
        )
        return False
    return True


class _SelectedItem:
    """An editor selection entry; deselecting it clears the selection."""

    def __init__(self, session: _Session) -> None:
        self._session = session

    def ClearSelected(self) -> None:
        """Clear the editor selection at the next Refresh."""
        self._session.selection_clear = True


class _SettingsManager:
    """Match project files against the open board's project."""

    def GetProject(self, path: str) -> Optional[str]:
        """Return the board's project token when ``path`` is its project file."""
        board = GetBoard()
        project = board.GetProject() if board is not None else None
        if project is None or not path:
            return None
        if os.path.normcase(os.path.realpath(path)) != project:
            return None
        return project


class _DesignSettings:
    """The board design settings the plugin reads."""

    def __init__(self, board: "BOARD") -> None:
        self._board = board

    def GetAuxOrigin(self) -> VECTOR2I:
        """Return the drill/place file origin."""
        board = self._board
        return _point(
            board.session.call(board.kipy.get_origin, BoardOriginType.BOT_DRILL)
        )


class BOARD:
    """The PCB open in the editor, read through a short-lived snapshot."""

    def __init__(self, session: _Session, kipy_board: KipyBoard) -> None:
        self.session = session
        self.kipy = kipy_board
        document = kipy_board.document
        self._board_filename = document.board_filename
        self._project_path = document.project.path
        project = session.call(kipy_board.get_project)
        directory = project.path or self._project_path
        self._filename = (
            os.path.join(directory, self._board_filename) if directory else ""
        )
        self._project_file: Optional[str] = None
        if directory and project.name:
            self._project_file = os.path.normcase(
                os.path.realpath(os.path.join(directory, f"{project.name}.kicad_pro"))
            )
        self._project = project
        # The API has no board UUID; this identifies the facade's document lifetime.
        self.m_Uuid = KIID(str(uuid.uuid4()))
        self._footprints: Optional[list[FOOTPRINT]] = None
        self._loaded_at = 0.0
        self._pad_boxes: Optional[dict[str, BOX2I]] = None
        self._commit: Any = None
        self._dirty: dict[int, FOOTPRINT] = {}
        self._commits = 0

    def is_document(self, document: Any) -> bool:
        """Return whether an open-document entry is this board."""
        return (
            document.board_filename == self._board_filename
            and document.project.path == self._project_path
        )

    # -- snapshot -------------------------------------------------------------

    def _snapshot(self) -> list["FOOTPRINT"]:
        stale = time.monotonic() - self._loaded_at >= SNAPSHOT_SECONDS
        if self._footprints is None or (stale and self._commit is None):
            footprints = self.session.call(self.kipy.get_footprints)
            self._footprints = [FOOTPRINT(self, item) for item in footprints]
            self._pad_boxes = None
            self._loaded_at = time.monotonic()
        return self._footprints

    def _pad_box(self, pad_id: str) -> Optional[BOX2I]:
        if self._pad_boxes is None:
            pads = [pad for fp in self._snapshot() for pad in fp.kipy.definition.pads]
            self._pad_boxes = self._bounding_boxes(pads)
        return self._pad_boxes.get(pad_id)

    def _bounding_boxes(self, items: Sequence[Any]) -> dict[str, BOX2I]:
        """Fetch item boxes in one request, keyed by item id."""
        if not items:
            return {}
        command = editor_commands_pb2.GetBoundingBox()
        command.header.document.CopyFrom(self.kipy.document)
        command.mode = editor_commands_pb2.BoundingBoxMode.BBM_ITEM_ONLY
        command.items.extend([item.id for item in items])
        response = self.session.call(
            self.kipy.client.send, command, editor_commands_pb2.GetBoundingBoxResponse
        )
        return {
            item.value: BOX2I(
                box.position.x_nm, box.position.y_nm, box.size.x_nm, box.size.y_nm
            )
            for item, box in zip(response.items, response.boxes)
        }

    def mark_dirty(self, footprint: "FOOTPRINT") -> None:
        """Queue a changed footprint for the open commit."""
        if self._commit is None:
            raise RuntimeError(
                "Board edits must run inside a KiCad commit (BOARD.commit)"
            )
        self._dirty[id(footprint)] = footprint

    @contextmanager
    def commit(self, message: str) -> Iterator[None]:
        """Send footprint edits made in the block to KiCad as one undoable change."""
        if self._commit is not None:
            raise RuntimeError("A KiCad board commit is already open")
        self._snapshot()
        self._commit = self.session.call(self.kipy.begin_commit)
        try:
            yield
            changed = [fp.kipy for fp in self._dirty.values()]
            if changed:
                self.session.call(self.kipy.update_items, changed)
                self.session.call(self.kipy.push_commit, self._commit, message)
                self._commits += 1
            else:
                self.session.call(self.kipy.drop_commit, self._commit)
        except BaseException:
            with suppress(ApiError, KiCadConnectionError):
                self.session.call(self.kipy.drop_commit, self._commit)
            raise
        finally:
            self._commit = None
            self._dirty.clear()
            self._footprints = None

    def serialize(self) -> bytes:
        """Return the live board as KiCad would save it."""
        return self.session.call(self.kipy.get_as_string).encode("utf-8")

    def saved_text_variables(self) -> dict[str, str]:
        """Return the text variables stored in the project file."""
        if self._project_file is None or not os.path.isfile(self._project_file):
            return {}
        with open(self._project_file, encoding="utf-8") as project_file:
            variables = json.load(project_file).get("text_variables", {})
        return dict(variables) if isinstance(variables, dict) else {}

    def plot_view(self, variant_name: str) -> "_PlotView":
        """Return this board as plotted in an explicit variant."""
        if variant_name and not self.HasVariant(variant_name):
            raise RuntimeError(f"KiCad could not select plot variant {variant_name!r}")
        return _PlotView(self, variant_name)

    # -- pcbnew BOARD API -----------------------------------------------------

    def GetFileName(self) -> str:
        """Return the board's absolute path."""
        return self._filename

    def GetProject(self) -> Optional[str]:
        """Return a token identifying the board's project file."""
        return self._project_file

    def GetFootprints(self) -> list["FOOTPRINT"]:
        """Return the board's footprints."""
        return list(self._snapshot())

    Footprints = GetFootprints

    def FindFootprintByReference(self, reference: str) -> Optional["FOOTPRINT"]:
        """Return the first footprint with this reference, if any."""
        return next(
            (fp for fp in self._snapshot() if fp.GetReference() == reference), None
        )

    def Drawings(self) -> list[Any]:
        """Return board-level text and graphic shapes."""
        shapes = self.session.call(self.kipy.get_shapes)
        texts = self.session.call(self.kipy.get_text)
        return [PCB_SHAPE(shape) for shape in shapes] + [
            PCB_TEXT(text) for text in texts if isinstance(text, BoardText)
        ]

    def Zones(self) -> list["ZONE"]:
        """Return the board's zones."""
        return [ZONE(zone) for zone in self.session.call(self.kipy.get_zones)]

    def GetDesignSettings(self) -> _DesignSettings:
        """Return the design settings the plugin reads."""
        return _DesignSettings(self)

    def GetCopperLayerCount(self) -> int:
        """Return the number of copper layers."""
        return int(self.session.call(self.kipy.get_copper_layer_count))

    def GetEnabledLayers(self) -> LSET:
        """Return the enabled layers."""
        layers = self.session.call(self.kipy.get_enabled_layers)
        return LSET(
            [
                layer
                for layer in (_from_kipy_layer(item) for item in layers)
                if layer != UNDEFINED_LAYER
            ]
        )

    def GetLayerName(self, layer: int) -> str:
        """Return a layer's user-visible name."""
        return str(self.session.call(self.kipy.get_layer_name, _to_kipy_layer(layer)))

    def _variants(self) -> list[Any]:
        return list(self.session.call(self.kipy.get_variants))

    def GetVariantNamesForUI(self) -> list[str]:
        """Return Default followed by the board's variant names."""
        names = sorted((v.name for v in self._variants()), key=str.casefold)
        return ["Default", *names]

    def HasVariant(self, name: str) -> bool:
        """Return whether a named variant exists."""
        return any(variant.name == name for variant in self._variants())

    def GetVariantDescription(self, name: str) -> str:
        """Return a variant's description."""
        return next((v.description for v in self._variants() if v.name == name), "")

    def GetCurrentVariant(self) -> str:
        """Return the editor's active variant, empty for Default."""
        return self.session.call(self.kipy.get_current_variant) or ""

    def GetTimeStamp(self) -> int:
        """Return a counter of commits made through this facade."""
        return self._commits

    def Save(self, filename: Optional[str] = None) -> bool:
        """Save the board through the editor."""
        if filename and os.path.realpath(filename) != os.path.realpath(self._filename):
            raise ValueError("The board can only be saved to its own file")
        self.session.call(self.kipy.save)
        return True


class _PlotView:
    """A board as plotted in one variant, leaving the editor's variant alone."""

    def __init__(self, board: BOARD, variant_name: str) -> None:
        self.board = board
        self.plot_variant = variant_name

    def GetFileName(self) -> str:
        """Return the board's absolute path."""
        return self.board.GetFileName()

    def GetCopperLayerCount(self) -> int:
        """Return the number of copper layers."""
        return self.board.GetCopperLayerCount()

    def GetEnabledLayers(self) -> LSET:
        """Return the enabled layers."""
        return self.board.GetEnabledLayers()

    def GetLayerName(self, layer: int) -> str:
        """Return a layer's user-visible name."""
        return self.board.GetLayerName(layer)

    def GetDesignSettings(self) -> _DesignSettings:
        """Return the design settings the plugin reads."""
        return self.board.GetDesignSettings()

    def SynchronizeProperties(self) -> None:
        """Project text variables are read live; nothing to synchronize."""

    def GetProperties(self) -> dict[str, str]:
        """Return the project's text variables, live when KiCad has the project open."""
        board = self.board
        try:
            variables = board.session.call(board._project.get_text_variables).variables
        except ApiError as error:
            # Headless KiCad opens boards without their project; use the saved file.
            logger.debug("Project text variables unavailable live: %s", error)
            variables = board.saved_text_variables()
        return {str(k): str(v) for k, v in variables.items()}


class PCB_FIELD:
    """A footprint field."""

    def __init__(self, footprint: "FOOTPRINT", field: Field, name: str) -> None:
        self._footprint, self.kipy, self._name = footprint, field, name

    def GetName(self) -> str:
        """Return the field name."""
        return self._name

    def GetText(self) -> str:
        """Return the field text."""
        return self.kipy.text.value

    def SetText(self, text: str) -> None:
        """Change the field text."""
        self._footprint.changed()
        self.kipy.text.value = text

    def IsVisible(self) -> bool:
        """Return whether the field is shown."""
        return bool(self.kipy.visible)

    def SetVisible(self, visible: bool) -> None:
        """Show or hide the field."""
        self._footprint.changed()
        self.kipy.visible = bool(visible)


class FOOTPRINT_VARIANT:
    """Per-variant footprint overrides."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._dnp = self._bom = self._pos = False
        self._fields: dict[str, str] = {}

    @classmethod
    def from_proto(cls, record: Any) -> "FOOTPRINT_VARIANT":
        """Copy a FootprintVariant protobuf record."""
        variant = cls(record.name)
        variant._dnp = record.do_not_populate
        variant._bom = record.exclude_from_bill_of_materials
        variant._pos = record.exclude_from_position_files
        variant._fields = dict(record.fields)
        return variant

    def GetName(self) -> str:
        """Return the variant name."""
        return self._name

    def GetDNP(self) -> bool:
        """Return the do-not-populate flag."""
        return self._dnp

    def SetDNP(self, value: bool) -> None:
        """Set the do-not-populate flag."""
        self._dnp = bool(value)

    def GetExcludedFromBOM(self) -> bool:
        """Return the exclude-from-BOM flag."""
        return self._bom

    def SetExcludedFromBOM(self, value: bool) -> None:
        """Set the exclude-from-BOM flag."""
        self._bom = bool(value)

    def GetExcludedFromPosFiles(self) -> bool:
        """Return the exclude-from-position-files flag."""
        return self._pos

    def SetExcludedFromPosFiles(self, value: bool) -> None:
        """Set the exclude-from-position-files flag."""
        self._pos = bool(value)

    def GetFields(self) -> dict[str, str]:
        """Return field overrides by name."""
        return dict(self._fields)

    def SetFieldValue(self, name: str, value: str) -> None:
        """Override a field in this variant."""
        self._fields[name] = value


class PAD:
    """A footprint pad."""

    def __init__(self, footprint: "FOOTPRINT", pad: Any) -> None:
        self._footprint, self.kipy = footprint, pad

    def GetAttribute(self) -> int:
        """Return the PAD_ATTRIB value."""
        return _PAD_ATTRIBUTES.get(self.kipy.pad_type, PAD_ATTRIB_SMD)

    def HasHole(self) -> bool:
        """Return whether the pad is drilled."""
        diameter = self.kipy.padstack.drill.diameter
        return diameter.x > 0 and diameter.y > 0

    def GetBoundingBox(self) -> BOX2I:
        """Return the pad's bounding box."""
        box = self._footprint.board._pad_box(self.kipy.id.value)
        if box is None:
            raise RuntimeError("KiCad returned no pad bounding box")
        return BOX2I(box._x, box._y, box._width, box._height)


class FOOTPRINT:
    """A footprint instance from the board snapshot."""

    _MANDATORY = ("Reference", "Value", "Datasheet", "Description")

    def __init__(self, board: BOARD, footprint: Any) -> None:
        self.board, self.kipy = board, footprint
        self.m_Uuid = KIID(footprint.id.value)

    def changed(self) -> None:
        """Queue this footprint for the open commit."""
        self.board.mark_dirty(self)

    # -- identity and placement -------------------------------------------------

    def GetReference(self) -> str:
        """Return the reference designator."""
        return self.kipy.reference_field.text.value

    def GetValue(self) -> str:
        """Return the value text."""
        return self.kipy.value_field.text.value

    def Value(self) -> PCB_FIELD:
        """Return the value field."""
        return PCB_FIELD(self, self.kipy.value_field, "Value")

    def GetFPID(self) -> LIB_ID:
        """Return the footprint library identifier."""
        identifier = self.kipy.definition.id
        return LIB_ID(identifier.library, identifier.name)

    def GetFPIDAsString(self) -> str:
        """Return ``library:name``."""
        return self.GetFPID().Format()

    def GetPath(self) -> KIID:
        """Return the schematic symbol path."""
        path = self.kipy._proto.symbol_path.path
        return KIID("".join(f"/{item.value}" for item in path))

    def GetLayer(self) -> int:
        """Return the footprint side as a PCB_LAYER_ID."""
        return _from_kipy_layer(self.kipy.layer)

    def IsFlipped(self) -> bool:
        """Return whether the footprint is on the bottom side."""
        return self.kipy.layer == BoardLayer.BL_B_Cu

    def GetPosition(self) -> VECTOR2I:
        """Return the anchor position."""
        return _point(self.kipy.position)

    def GetOrientation(self) -> EDA_ANGLE:
        """Return the rotation."""
        return EDA_ANGLE(self.kipy.orientation.degrees)

    def GetOrientationDegrees(self) -> float:
        """Return the rotation in degrees."""
        return float(self.kipy.orientation.degrees)

    def Pads(self) -> list[PAD]:
        """Return the pads."""
        return [PAD(self, pad) for pad in self.kipy.definition.pads]

    # -- attributes ---------------------------------------------------------------

    def GetAttributes(self) -> int:
        """Return FOOTPRINT_ATTR_T bits."""
        attributes = self.kipy.attributes
        style = attributes.mounting_style
        value = {
            FootprintMountingStyle.FMS_THROUGH_HOLE: FP_THROUGH_HOLE,
            FootprintMountingStyle.FMS_SMD: FP_SMD,
        }.get(style, 0)
        for flag, bit in (
            (attributes.exclude_from_position_files, FP_EXCLUDE_FROM_POS_FILES),
            (attributes.exclude_from_bill_of_materials, FP_EXCLUDE_FROM_BOM),
            (attributes.not_in_schematic, FP_BOARD_ONLY),
            (attributes.do_not_populate, FP_DNP),
        ):
            if flag:
                value |= bit
        return value

    def SetAttributes(self, value: int) -> None:
        """Set FOOTPRINT_ATTR_T bits; unrepresented bits are ignored."""
        self.changed()
        attributes = self.kipy.attributes
        style_bits = value & (FP_THROUGH_HOLE | FP_SMD)
        if style_bits != self.GetAttributes() & (FP_THROUGH_HOLE | FP_SMD):
            attributes.mounting_style = {
                FP_THROUGH_HOLE: FootprintMountingStyle.FMS_THROUGH_HOLE,
                FP_SMD: FootprintMountingStyle.FMS_SMD,
            }.get(style_bits, FootprintMountingStyle.FMS_UNSPECIFIED)
        attributes.exclude_from_position_files = bool(value & FP_EXCLUDE_FROM_POS_FILES)
        attributes.exclude_from_bill_of_materials = bool(value & FP_EXCLUDE_FROM_BOM)
        attributes.not_in_schematic = bool(value & FP_BOARD_ONLY)
        attributes.do_not_populate = bool(value & FP_DNP)

    def IsDNP(self) -> bool:
        """Return the do-not-populate flag."""
        return bool(self.kipy.attributes.do_not_populate)

    def SetModified(self) -> None:
        """Edits are tracked by the facade; nothing else to mark."""

    # -- fields -------------------------------------------------------------------

    def _mandatory(self) -> list[PCB_FIELD]:
        kipy = self.kipy
        fields = (
            kipy.reference_field,
            kipy.value_field,
            kipy.datasheet_field,
            kipy.description_field,
        )
        return [PCB_FIELD(self, f, n) for f, n in zip(fields, self._MANDATORY)]

    def _custom(self) -> list[PCB_FIELD]:
        return [
            PCB_FIELD(self, item, item.name)
            for item in self.kipy.definition.items
            if isinstance(item, Field)
        ]

    def GetFields(self) -> list[PCB_FIELD]:
        """Return mandatory and user fields."""
        return self._mandatory() + self._custom()

    def GetField(self, name: str) -> Optional[PCB_FIELD]:
        """Return a field by name."""
        return next((f for f in self.GetFields() if f.GetName() == name), None)

    GetFieldByName = GetField

    def SetField(self, name: str, text: str) -> None:
        """Set a field's text, adding a user field when it does not exist."""
        field = self.GetField(name)
        if field is None:
            self.changed()
            created = Field()
            # Style a new field like the value field; KiCad assigns a fresh id.
            created.proto.text.CopyFrom(self.kipy.value_field.proto.text)
            created.proto.text.ClearField("id")
            created.name = name
            created.visible = True
            definition = self.kipy.definition
            definition.items = [*definition.items, created]
            field = PCB_FIELD(self, created, name)
        field.SetText(text)

    def Remove(self, field: PCB_FIELD) -> None:
        """Remove a user field."""
        definition = self.kipy.definition
        items = [item for item in definition.items if item is not field.kipy]
        if len(items) == len(definition.items):
            raise ValueError(f"Field {field.GetName()!r} is not a removable user field")
        self.changed()
        definition.items = items

    # -- variants -----------------------------------------------------------------

    def _variant_record(self, name: str) -> Any:
        return next((v for v in self.kipy._proto.variants if v.name == name), None)

    def GetVariant(self, name: str) -> Optional[FOOTPRINT_VARIANT]:
        """Return a copy of a variant's overrides, if it has any."""
        record = self._variant_record(name)
        return FOOTPRINT_VARIANT.from_proto(record) if record is not None else None

    def SetVariant(self, variant: FOOTPRINT_VARIANT) -> None:
        """Replace a variant's overrides, keeping its simulation flag."""
        self.changed()
        record = self._variant_record(variant.GetName())
        if record is None:
            record = self.kipy._proto.variants.add()
            record.name = variant.GetName()
        record.do_not_populate = variant.GetDNP()
        record.exclude_from_bill_of_materials = variant.GetExcludedFromBOM()
        record.exclude_from_position_files = variant.GetExcludedFromPosFiles()
        record.fields.clear()
        record.fields.update(variant.GetFields())

    def DeleteVariant(self, name: str) -> None:
        """Remove a variant's overrides."""
        variants = self.kipy._proto.variants
        for index, record in enumerate(variants):
            if record.name == name:
                self.changed()
                del variants[index]
                return

    def GetFieldValueForVariant(self, variant: str, name: str) -> str:
        """Return a field's text in a variant, falling back to the base field."""
        record = self._variant_record(variant)
        if record is not None and name in record.fields:
            return record.fields[name]
        field = self.GetField(name)
        return field.GetText() if field is not None else ""

    def GetDNPForVariant(self, variant: str) -> bool:
        """Return the effective do-not-populate flag in a variant."""
        record = self._variant_record(variant)
        return record.do_not_populate if record is not None else self.IsDNP()

    def GetExcludedFromBOMForVariant(self, variant: str) -> bool:
        """Return the effective exclude-from-BOM flag in a variant."""
        record = self._variant_record(variant)
        if record is not None:
            return record.exclude_from_bill_of_materials
        return bool(self.kipy.attributes.exclude_from_bill_of_materials)

    def GetExcludedFromPosFilesForVariant(self, variant: str) -> bool:
        """Return the effective exclude-from-position-files flag in a variant."""
        record = self._variant_record(variant)
        if record is not None:
            return record.exclude_from_position_files
        return bool(self.kipy.attributes.exclude_from_position_files)

    # -- selection ----------------------------------------------------------------

    def IsSelected(self) -> bool:
        """Report unselected; selection is not tracked per item."""
        return False

    def SetSelected(self) -> None:
        """Select this footprint at the next Refresh."""
        self.board.session.selection_add[self.kipy.id.value] = self.kipy

    def ClearSelected(self) -> None:
        """Clear the editor selection at the next Refresh."""
        self.board.session.selection_clear = True


class _BoardGraphic:
    """Common accessors of board-level drawings."""

    def __init__(self, item: Any) -> None:
        self.kipy = item

    def IsOnLayer(self, layer: int) -> bool:
        """Return whether the item is on a layer."""
        return _TO_KIPY.get(layer) == self.kipy.layer


class PCB_TEXT(_BoardGraphic):
    """Board-level text."""

    def GetText(self) -> str:
        """Return the text."""
        return self.kipy.value

    def GetCenter(self) -> VECTOR2I:
        """Return the text anchor."""
        return _point(self.kipy.position)


class PCB_SHAPE(_BoardGraphic):
    """A board-level graphic shape."""

    def GetShape(self) -> int:
        """Return S_RECT for rectangles."""
        return S_RECT if isinstance(self.kipy, BoardRectangle) else S_SEGMENT

    def IsFilled(self) -> bool:
        """Return whether the shape is filled."""
        return bool(self.kipy.attributes.fill.filled)

    def _corners(self) -> tuple[VECTOR2I, VECTOR2I]:
        return _point(self.kipy.top_left), _point(self.kipy.bottom_right)

    def GetRectCorners(self) -> list[VECTOR2I]:
        """Return a rectangle's four corners."""
        top_left, bottom_right = self._corners()
        return [
            top_left,
            VECTOR2I(bottom_right.x, top_left.y),
            bottom_right,
            VECTOR2I(top_left.x, bottom_right.y),
        ]

    def GetCenter(self) -> VECTOR2I:
        """Return a rectangle's centre."""
        top_left, bottom_right = self._corners()
        return VECTOR2I(
            (top_left.x + bottom_right.x) // 2, (top_left.y + bottom_right.y) // 2
        )


class PCB_VIA:
    """Placeholder: via mask plotting is a board setting in KiCad 9 and newer."""


def _polyline_area(polyline: Any) -> float:
    points = []
    for node in polyline.proto.nodes:
        if node.HasField("point"):
            points.append(node.point)
        else:
            points.extend((node.arc.start, node.arc.mid, node.arc.end))
    twice = sum(
        a.x_nm * b.y_nm - b.x_nm * a.y_nm
        for a, b in zip(points, points[1:] + points[:1])
    )
    return abs(twice) / 2


class SHAPE_POLY_SET:
    """Filled zone polygons on one layer."""

    def __init__(self, polygons: Sequence[Any]) -> None:
        self._polygons = polygons

    def Area(self) -> float:
        """Return the filled area in square internal units."""
        return sum(
            _polyline_area(polygon.outline)
            - sum(_polyline_area(hole) for hole in polygon.holes)
            for polygon in self._polygons
        )


class ZONE:
    """A board zone."""

    def __init__(self, zone: Any) -> None:
        self.kipy = zone

    def GetIsRuleArea(self) -> bool:
        """Return whether this is a rule area (keepout)."""
        return bool(self.kipy.is_rule_area())

    def GetLayerSet(self) -> LSET:
        """Return the zone's layers."""
        return LSET([_from_kipy_layer(layer) for layer in self.kipy.layers])

    def GetFilledPolysList(self, layer: int) -> SHAPE_POLY_SET:
        """Return the filled polygons on a layer."""
        return SHAPE_POLY_SET(self.kipy.filled_polygons.get(_to_kipy_layer(layer), []))

    def GetNetname(self) -> str:
        """Return the zone's net name."""
        net = self.kipy.net
        return net.name if net is not None else ""


class ZONE_FILLER:
    """Refill zones in the editor."""

    def __init__(self, board: BOARD) -> None:
        self._board = board

    def Fill(self, zones: Sequence[ZONE]) -> bool:
        """Refill every zone and reload the given zones' fill data."""
        board = self._board
        board.session.call(board.kipy.refill_zones, block=True, max_poll_seconds=300)
        fresh = {z.id.value: z for z in board.session.call(board.kipy.get_zones)}
        for zone in zones:
            zone.kipy = fresh.get(zone.kipy.id.value, zone.kipy)
        return True


class PCB_PLOT_PARAMS:
    """Plot options, recorded through pcbnew's ``Set*`` methods."""

    def __init__(self) -> None:
        self._values: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Callable[[Any], None]:
        """Accept any ``SetX(value)`` call and remember the value."""
        if not name.startswith("Set"):
            raise AttributeError(name)
        return lambda value: self._values.__setitem__(name[3:], value)

    def value(self, key: str, default: Any) -> Any:
        """Return a recorded option."""
        return self._values.get(key, default)


def _plot_target(board: Any) -> tuple[BOARD, str]:
    if isinstance(board, _PlotView):
        return board.board, board.plot_variant
    return board, ""


def _sanitize_suffix(suffix: str) -> str:
    """Name a plot suffix as KiCad's BuildPlotFileName does."""
    return "".join("_" if c in '\\/:*?"<>|%.' else c for c in suffix.strip())


@contextmanager
def _board_snapshot(board: BOARD, directory: Path) -> Iterator[Path]:
    """Write the live board, with its project settings, for kicad-cli.

    KiCad's export jobs read the saved file, so they would miss unsaved edits
    and a fresh zone fill. pcbnew's plotters use the live board; plotting this
    copy keeps that behaviour.
    """
    with tempfile.TemporaryDirectory(
        prefix=".kicad-snapshot-", dir=directory
    ) as scratch:
        target = Path(scratch) / Path(board.GetFileName()).name
        target.write_bytes(board.serialize())
        project = board.GetProject()
        if project is not None:
            # kicad-cli loads the project named after the board file.
            for suffix in (".kicad_pro", ".kicad_dru"):
                source = Path(project).with_suffix(suffix)
                if source.is_file():
                    shutil.copyfile(source, target.with_suffix(suffix))
        yield target


def _run_cli(session: _Session, arguments: Sequence[str]) -> None:
    completed = subprocess.run(
        [session.kicad_cli(), *arguments],
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"kicad-cli {arguments[1]} failed: {detail}")


class PLOT_CONTROLLER:
    """Plot Gerber layers of the live board with one kicad-cli run.

    ``PlotLayer`` queues a layer under its pcbnew filename; ``ClosePlot`` plots
    every queued layer at once (loading a large board dominates the run time)
    and raises if any file is missing.
    """

    def __init__(self, board: Any) -> None:
        self._board, self._variant = _plot_target(board)
        self._options = PCB_PLOT_PARAMS()
        self._layer: Optional[int] = None
        self._suffix: Optional[str] = None
        self._queued: list[tuple[int, Path]] = []

    def GetPlotOptions(self) -> PCB_PLOT_PARAMS:
        """Return the options used by the plots."""
        return self._options

    def SetLayer(self, layer: int) -> None:
        """Select the layer for the next plot."""
        self._layer = layer

    def OpenPlotfile(self, suffix: str, plot_format: int, _description: str) -> bool:
        """Name the next plot ``<board>-<suffix>.gbr``."""
        if plot_format != PLOT_FORMAT_GERBER:
            return False
        self._suffix = _sanitize_suffix(suffix)
        return True

    def PlotLayer(self) -> bool:
        """Queue the selected layer for ClosePlot."""
        if self._layer is None or self._suffix is None:
            return False
        if self._options.value("UseGerberProtelExtensions", False):
            raise ValueError("Protel Gerber extensions are not supported")
        directory = Path(self._options.value("OutputDirectory", ""))
        stem = Path(self._board.GetFileName()).stem
        self._queued.append((self._layer, directory / f"{stem}-{self._suffix}.gbr"))
        self._suffix = None
        return True

    def _arguments(
        self, layers: Sequence[int], output: Path, source: Path
    ) -> list[str]:
        options = self._options
        if options.value("DrillMarksType", DRILL_MARKS_NO_DRILL_SHAPE) != (
            DRILL_MARKS_NO_DRILL_SHAPE
        ):
            raise ValueError("Only plots without drill marks are supported")
        names = {value: name for name, value in _LAYER_IDS.items()}
        arguments = [
            "pcb",
            "export",
            "gerbers",
            "--layers",
            ",".join(names[layer].replace("_", ".") for layer in layers),
            "--no-protel-ext",
            "--precision",
            "6",
            "--output",
            str(output),
        ]
        for option, default, flag in (
            ("PlotValue", True, "--exclude-value"),
            ("PlotReference", True, "--exclude-refdes"),
            ("UseGerberX2format", True, "--no-x2"),
            ("IncludeGerberNetlistInfo", True, "--no-netlist"),
        ):
            if not options.value(option, default):
                arguments.append(flag)
        for option, flag in (
            ("SketchPadsOnFabLayers", "--sketch-pads-on-fab-layers"),
            ("SubtractMaskFromSilk", "--subtract-soldermask"),
            ("UseAuxOrigin", "--use-drill-file-origin"),
            ("DisableGerberMacros", "--disable-aperture-macros"),
            ("PlotFrameRef", "--include-border-title"),
        ):
            if options.value(option, False):
                arguments.append(flag)
        if self._variant:
            arguments += ["--variant", self._variant]
        return [*arguments, str(source)]

    def ClosePlot(self) -> None:
        """Plot every queued layer and move each to its pcbnew filename."""
        queued, self._queued = self._queued, []
        if not queued:
            return
        board = self._board
        directory = queued[0][1].parent
        with _board_snapshot(board, directory) as source:
            output = source.parent / "plots"
            layers = [layer for layer, _ in queued]
            _run_cli(board.session, self._arguments(layers, output, source))
            for layer, destination in queued:
                # kicad-cli names each plot after the layer's board name.
                name = _sanitize_suffix(board.GetLayerName(layer))
                plotted = output / f"{source.stem}-{name}.gbr"
                if not plotted.is_file():
                    raise RuntimeError(f"kicad-cli did not plot layer {name}")
                os.replace(plotted, destination)


class EXCELLON_WRITER:
    """Write Excellon drill files and maps of the live board with kicad-cli."""

    def __init__(self, board: Any) -> None:
        self._board, _variant = _plot_target(board)
        self._mirror = self._minimal_header = self._merge_npth = False
        self._offset = VECTOR2I(0, 0)
        self._metric = True
        self._route_oval_holes = True

    def SetOptions(
        self, mirror: bool, minimal_header: bool, offset: Any, merge_npth: bool
    ) -> None:
        """Set mirroring, header, origin and PTH/NPTH merging."""
        self._mirror, self._minimal_header = bool(mirror), bool(minimal_header)
        self._offset = VECTOR2I(offset.x, offset.y)
        self._merge_npth = bool(merge_npth)

    def SetFormat(self, metric: bool, *_: Any) -> None:
        """Select metric or imperial decimal output."""
        self._metric = bool(metric)

    def SetRouteModeForOvalHoles(self, route: bool) -> None:
        """Select routed or drilled oval holes."""
        self._route_oval_holes = bool(route)

    def CreateDrillandMapFilesSet(
        self, directory: str, gen_drill: bool, gen_map: bool, _reporter: Any = None
    ) -> bool:
        """Write drill files and PDF drill maps into a directory."""
        if not gen_drill:
            raise ValueError("Drill maps without drill files are not supported")
        board = self._board
        if self._offset == board.GetDesignSettings().GetAuxOrigin():
            origin = "plot"
        elif self._offset == VECTOR2I(0, 0):
            origin = "absolute"
        else:
            raise ValueError("Drill files support only absolute or aux-origin offsets")
        arguments = [
            "pcb",
            "export",
            "drill",
            "--format",
            "excellon",
            "--drill-origin",
            origin,
            "--excellon-zeros-format",
            "decimal",
            "--excellon-oval-format",
            "route" if self._route_oval_holes else "alternate",
            "--excellon-units",
            "mm" if self._metric else "in",
            "--output",
            str(Path(directory)) + os.sep,
        ]
        if self._mirror:
            arguments.append("--excellon-mirror-y")
        if self._minimal_header:
            arguments.append("--excellon-min-header")
        if not self._merge_npth:
            arguments.append("--excellon-separate-th")
        if gen_map:
            arguments += ["--generate-map", "--map-format", "pdf"]
        with _board_snapshot(board, Path(directory)) as source:
            _run_cli(board.session, [*arguments, str(source)])
        return True
