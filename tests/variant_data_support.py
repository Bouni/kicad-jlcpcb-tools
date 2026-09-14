"""Construct native records and real sessions without importing the plugin GUI."""

from dataclasses import replace
import importlib
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Optional

from .variant_native_support import PACKAGE, Board, Snapshot, State, Variant, native

store_module_impl = importlib.import_module(f"{PACKAGE}.variant.store")
session_module = importlib.import_module(f"{PACKAGE}.variant.session")
settings_persistence = importlib.import_module(f"{PACKAGE}.core.settings_persistence")
Assignment = native.ResolvedAssignment


def _component(variant: str = "", lcsc: str = "C100", **changes: Any) -> Any:
    """Make a production component record with concise, consistent defaults."""
    return State(
        changes.pop("component_id", "component-1"),
        changes.pop("reference", "R1"),
        variant,
        lcsc=lcsc,
        assignment=changes.pop(
            "assignment", Assignment("valid" if lcsc else "empty", bool(variant), ())
        ),
        **changes,
    )


def _snapshot(
    store: Any,
    components: Optional[tuple[Any, ...]] = None,
    variants: tuple[str, ...] = ("", "A", "B"),
) -> Any:
    """Create the actual immutable all-variant snapshot, including its index."""
    if components is None:
        components = tuple(
            _component(name, lcsc)
            for name, lcsc in zip(variants, ("C100", "C200", "C300"))
        )
    return replace(Snapshot(components, variants), board_id=store.board_id)


def _parent(project: Path) -> Any:
    """Use a persisted settings document so reopening exercises real values."""
    path = project / "settings.json"
    parent = SimpleNamespace(
        settings=json.loads(path.read_text()) if path.exists() else {}
    )

    def save_settings(variant_patch: Any = None) -> None:
        saved = settings_persistence.save_settings_document(
            project, parent.settings, variant_patch
        )
        parent.settings.clear()
        parent.settings.update(saved)

    parent.save_settings = save_settings
    return parent


class SavedNativeBoard(Board):
    """A stateful board with a real saved path and independent unsaved fields."""

    def __init__(self, project: Path, name: str = "main") -> None:
        super().__init__()
        self.filename = project / f"{name}.kicad_pcb"
        if not self.filename.exists():
            self.filename.write_text("(kicad_pcb)\n", encoding="utf-8")

    def GetFileName(self) -> str:
        """Return the board identity used by the adapter and settings store."""
        return str(self.filename)


def _store(project: Path, name: str = "main") -> Any:
    """Construct the real board-bound settings and supplier store."""
    return store_module_impl.VariantStore(
        _parent(project), str(project), SavedNativeBoard(project, name)
    )


def _native_session(
    project: Path,
    board: Any = None,
) -> tuple[ModuleType, Any, Any, Any, SavedNativeBoard]:
    """Exercise actual native reads, edits, sessions and row adapters together."""
    if board is None:
        board = SavedNativeBoard(project)
        board.parts[0].SetField("LCSC", "C100")
        for name, lcsc in (("A", "C200"), ("B", "C300")):
            board.parts[0].AddVariant(name).SetFieldValue("LCSC", lcsc)
    store = store_module_impl.VariantStore(_parent(project), str(project), board)
    adapter = native.VariantNativeAdapter(
        board, store.board_id, variant_factory=Variant
    )
    session = session_module.VariantSession(adapter, store, get_board=lambda: board)
    return session_module, session, store, adapter, board
