"""Real assignment/preference databases with stateful board and GUI boundaries."""

from collections.abc import Callable, Iterable, Iterator
from contextlib import closing
from itertools import count
import logging
from pathlib import Path
import sqlite3
import types
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from .wx_harness import ROOT, load_siblings, mainwindow_stubs, wx_stubs


@pytest.fixture
def mainwindow(monkeypatch: pytest.MonkeyPatch) -> Iterator[types.ModuleType]:
    """Load real controller, storage and footprint code with GUI imports isolated."""
    package = "_part_preferences_controller_tests"
    ids = count(1)
    stubs = mainwindow_stubs(
        package,
        wx=wx_stubs(
            Frame=type("Frame", (), {}),
            NewIdRef=lambda: next(ids),
            PostEvent=MagicMock(),
            CallAfter=lambda callback: callback(),
        ),
        derive_params={
            "params_for_part": lambda details: details.get("description", "")
        },
    )
    helpers = stubs[f"{package}.helpers"]
    helpers.dict_factory = lambda cursor, row: dict(
        zip([column[0] for column in cursor.description], row)
    )
    helpers.natural_sort_collation = lambda a, b: (a > b) - (a < b)
    stubs[f"{package}.bom_estimation"].__path__ = [str(ROOT / "bom_estimation")]
    for name in (
        "bom_estimation.assembly_mode",
        "footprint_helpers",
        "footprint_metadata",
        "store",
        "library",
    ):
        stubs.pop(f"{package}.{name}", None)
    for name in ("requests", "urllib3"):
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "level", logger.level)
    with load_siblings(package, ("mainwindow",), stubs) as loaded:
        yield loaded["mainwindow"]


class Field:
    """Retain editable KiCad field names, text and visibility."""

    def __init__(self, name: str, text: str) -> None:
        self.name, self.text = name, text
        self.visible = True

    def GetName(self) -> str:
        """Return the native field name."""
        return self.name

    def GetText(self) -> str:
        """Return the current text."""
        return self.text

    def SetVisible(self, visible: bool) -> None:
        """Retain field visibility."""
        self.visible = visible


class Footprint:
    """Represent live native fields and independent assembly exclusion flags."""

    def __init__(
        self,
        reference: str = "R1",
        *,
        value: str = "10k",
        footprint: str = "R_0603",
        lcsc: str = "C100",
        fields: Optional[dict[str, str]] = None,
        dnp: bool = False,
        bom: bool = False,
        pos: bool = False,
    ) -> None:
        self.reference, self.value, self.footprint = reference, value, footprint
        self.fields = {
            name: Field(name, text)
            for name, text in (fields if fields is not None else {"LCSC": lcsc}).items()
        }
        self.dnp = dnp
        self.attributes = (int(bom) << 3) | (int(pos) << 2)
        self.selected = False

    @property
    def field(self) -> Field:
        """Return the conventional LCSC field used by simple workflows."""
        return self.fields["LCSC"]

    def GetReference(self) -> str:
        """Return the board reference."""
        return self.reference

    def GetValue(self) -> str:
        """Return the part value."""
        return self.value

    def GetFPID(self) -> types.SimpleNamespace:
        """Return the footprint library item."""
        return types.SimpleNamespace(GetLibItemName=lambda: self.footprint)

    def GetFields(self) -> list[Field]:
        """Return every editable field."""
        return list(self.fields.values())

    def SetField(self, name: str, value: str) -> None:
        """Change only the named field."""
        self.fields.setdefault(name, Field(name, "")).text = value

    def GetAttributes(self) -> int:
        """Return assembly exclusion bits."""
        return self.attributes

    def SetAttributes(self, attributes: int) -> None:
        """Retain assembly exclusion bits changed by native-style handlers."""
        self.attributes = attributes

    def SetSelected(self) -> None:
        """Retain the native board highlight selection."""
        self.selected = True

    def ClearSelected(self) -> None:
        """Clear the native board highlight selection."""
        self.selected = False

    def IsDNP(self) -> bool:
        """Return the live DNP flag."""
        return self.dnp

    def GetLayer(self) -> int:
        """Return the front layer."""
        return 0


class Board:
    """Resolve references against the current board."""

    def __init__(self, footprints: Iterable[Footprint]) -> None:
        self.footprints = {fp.reference: fp for fp in footprints}

    def GetFootprints(self) -> list[Footprint]:
        """Return current board footprints."""
        return list(self.footprints.values())

    def FindFootprintByReference(self, reference: str) -> Optional[Footprint]:
        """Resolve only references still on the board."""
        return self.footprints.get(reference)


class Toolbar:
    """Retain tool availability through storage failure and recovery."""

    def __init__(self) -> None:
        self.enabled: dict[int, bool] = {}

    def EnableTool(self, tool: int, enabled: bool) -> None:
        """Retain whether this tool is usable."""
        self.enabled[tool] = enabled


def seed_preferences(library: Any, preferences: dict[tuple[str, str], str]) -> None:
    """Install raw legacy rows, including identifiers new writes should reject."""
    with closing(sqlite3.connect(library.part_preferences_db_file)) as db, db:
        db.executemany(
            "INSERT INTO mapping VALUES (?, ?, ?)",
            [(*key, lcsc) for key, lcsc in preferences.items()],
        )


@pytest.fixture
def make_window(mainwindow: types.ModuleType, tmp_path: Path) -> Callable[..., Any]:
    """Construct real databases and stateful GUI boundaries for controller events."""

    def make(
        *,
        footprints: Optional[list[Footprint]] = None,
        settings: Optional[dict[str, Any]] = None,
        part_preferences: Optional[dict[tuple[str, str], str]] = None,
        board: Optional[Board] = None,
        fabrication_initialized: bool = True,
    ) -> Any:
        window = object.__new__(mainwindow.JLCPCBTools)
        window.settings = {} if settings is None else settings
        window.project_path = str(tmp_path)
        board = board or Board(footprints if footprints is not None else [Footprint()])
        window.pcbnew = types.SimpleNamespace(
            GetBoard=lambda: board,
            GetCurrentSelection=lambda: [
                fp for fp in board.GetFootprints() if fp.selected
            ],
            Refresh=MagicMock(),
        )
        window.logger = MagicMock()
        library = window.library = object.__new__(mainwindow.Library)
        library.part_preferences_db_file = str(tmp_path / "mappings.db")
        library.logger = logging.getLogger("part_preferences_test_storage")
        library.create_part_preferences_table()
        library.create_lcsc_metadata_table()
        seed_preferences(library, part_preferences or {})
        library.state = mainwindow.LibraryState.INITIALIZED
        library.save_part_preferences = MagicMock(wraps=library.save_part_preferences)
        library.get_part_preference = MagicMock(wraps=library.get_part_preference)
        library.get_part_details = MagicMock(
            return_value={"type": "Basic", "stock": 27, "description": "10k resistor"}
        )
        library.read_correction_data = MagicMock(
            return_value=types.SimpleNamespace(
                corrections=(),
                state=mainwindow.CorrectionState.READY,
                scope="global",
                db_path=str(tmp_path / "corrections.db"),
            )
        )
        window.correction_status = MagicMock()
        window.project_storage_status = MagicMock()
        window.right_toolbar = MagicMock()
        window.upper_toolbar = Toolbar()
        window.Layout = MagicMock()
        window._project_storage_unavailable = False
        window._part_preferences_applied_on_open = False
        window.assembly_enrichment_generation = 0
        window.pending_assembly_enrichment = set()
        # Assignment-only windows represent startup after fabrication initialization.
        # Recovery tests opt out and exercise the real constructor from init_data.
        if fabrication_initialized:
            window.fabrication = object()
        window.store = mainwindow.Store(window, window.project_path, board)
        model = window.partlist_data_model = MagicMock()
        rows: dict[str, Any] = {}

        def populate() -> None:
            rows.clear()
            for part in window.store.read_all():
                details = window._catalog_get_part_details(part["lcsc"])
                rows[part["reference"]] = {
                    **part,
                    "type": details.get("type", ""),
                    "stock": details.get("stock", ""),
                }
            mainwindow.wx.PostEvent(
                window, mainwindow.BomDataChangedEvent(source="populate_footprint_list")
            )

        model.get_reference.side_effect = lambda ref: ref
        model.get_footprint.side_effect = lambda ref: rows[ref]["footprint"]
        model.get_value.side_effect = lambda ref: rows[ref]["value"]
        model.get_lcsc.side_effect = lambda ref: rows[ref]["lcsc"]
        model.RemoveAll.side_effect = rows.clear
        window.footprint_list = MagicMock()
        window.footprint_list.GetSelections.return_value = list(board.footprints)
        window.populate_footprint_list = MagicMock(side_effect=populate)
        window.start_assembly_enrichment = MagicMock()
        window.recompute_bom_estimate = MagicMock()
        window.hide_bom_parts = window.hide_pos_parts = False
        window.get_correction = MagicMock(return_value="")
        window._get_enrichment_status_label = MagicMock(return_value="")
        populate()
        library.get_part_details.reset_mock()
        mainwindow.wx.PostEvent.reset_mock()
        window.test_rows = rows
        return window

    return make


def act(
    action: str,
    window: Any,
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
    lcsc: str = "C999",
) -> None:
    """Exercise real handlers; clipboard text is set before the handler reads it."""
    if action == "picker":
        window.assign_parts(
            types.SimpleNamespace(
                references=list(window.pcbnew.GetBoard().footprints) + ["REMOVED"],
                lcsc=lcsc,
                type="Basic",
                stock=27,
            )
        )
    elif action == "paste":

        class TextData:
            text = ""

            def GetText(self) -> str:
                """Return the current text."""
                return self.text

        clipboard = MagicMock()
        clipboard.Open.return_value = True

        def read(data: TextData) -> bool:
            data.text = f"https://www.lcsc.com/product-detail/{lcsc}.html"
            return True

        clipboard.GetData.side_effect = read
        monkeypatch.setattr(mainwindow.wx, "TheClipboard", clipboard, raising=False)
        monkeypatch.setattr(mainwindow.wx, "TextDataObject", TextData, raising=False)
        window.paste_part_lcsc()
        clipboard.Close.assert_called_once()
    elif action == "apply":
        window.apply_selected_part_preferences()
    else:
        assert action == "clear"
        window.remove_lcsc_number()


def board_rows(window: Any) -> list[dict[str, Any]]:
    """Read current board state independently of displayed or persisted rows."""
    return sorted(window.store.read_all(), key=lambda part: part["reference"])


def info_messages(window: Any) -> list[str]:
    """Render logged counts and setting pointers as users read them."""
    return [
        str(call.args[0]) % call.args[1:] if len(call.args) > 1 else str(call.args[0])
        for call in window.logger.info.call_args_list
    ]
