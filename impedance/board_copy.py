"""Snapshot already-filled live geometry without changing the editor's board.

The public BOARD() helper returns null inside KiCad's PCB editor. The raw native
file plugin can allocate a board without that helper's editor/project side
effects. Full-board formatting can update embedded-font caches, so only a
detached settings shadow is formatted that way; live items use const Format().

SWIG 4.4.1 does not balance its type-registry capsule reference count when a
borrowed pointer is promoted with ``thisown = True``. Keep the loader's original
ownership unchanged: explicitly destroy borrowed allocations and let originally
owned wrappers clean themselves up. Never use this owner for the editor's board.
"""

from collections.abc import Callable
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from types import TracebackType
from typing import Any, Optional

from .text_context import apply_text_context, resolve_text_context

_MINIMAL_BOARD = """(kicad_pcb (version 20240108) (generator "pcbnew")
  (general (thickness 1.6)) (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0)))
"""
_TOKENS = re.compile(r'"(?:\\.|[^"\\])*"|[()]|[^\s()"]+')
_REFILL_MESSAGE = (
    "Fill copper zones in KiCad (B), or enable Fill zones and reopen "
    "controlled-impedance review. Image rendering does not refill your board."
)
_ITEM_COLLECTIONS = ("GetFootprints", "GetTracks", "GetDrawings", "Zones")


class BoardCopyError(RuntimeError):
    """Report a snapshot capability, freshness, or native serialization failure."""


class NativeBoardOwner:
    """Own one detached parser allocation, not any editor-owned board.

    ``destroy`` must be the binding's exported BOARD destructor. A borrowed
    wrapper remains borrowed throughout its lifetime. Callers must release all
    dependent native plotters before closing, and must not retain board aliases
    outside the scope. There is deliberately no native finalizer at Python
    shutdown: all users must close explicitly or use a context manager.
    """

    def __init__(self, board: Any, destroy: Callable[[Any], None]) -> None:
        """Retain a newly allocated board with its initial SWIG ownership."""
        if not callable(destroy):
            raise TypeError("A native board destructor is required.")
        self._python_owned = bool(board.thisown)
        self._board: Optional[Any] = board
        self._destroy = destroy

    @property
    def board(self) -> Any:
        """Return the native board only while its allocation is alive."""
        if self._board is None:
            raise RuntimeError("The detached board snapshot is closed.")
        return self._board

    @property
    def closed(self) -> bool:
        """Whether this owner has relinquished its allocation."""
        return self._board is None

    def close(self) -> None:
        """Release exactly once; leave initially owned wrappers to Python."""
        board = self._board
        if board is None:
            return
        # Invalidate before invoking native deletion so even an exception cannot
        # cause a second delete. The raw borrowed wrapper's pointer is not reset
        # by every SWIG build and must never be read after this call.
        self._board = None
        if not self._python_owned:
            self._destroy(board)

    def __enter__(self) -> Any:
        """Expose the native allocation for one bounded scope."""
        return self.board

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        """Release after success or failure without suppressing the failure."""
        self.close()


def _native_io(pcbnew_module: Any) -> Any:
    """Require the raw file plugin rather than stateful global load helpers."""
    factory = getattr(pcbnew_module, "PCB_IO_KICAD_SEXPR", None)
    if factory is None:
        raise BoardCopyError("This KiCad version cannot safely snapshot a board.")
    result = factory()
    if not callable(getattr(result, "LoadBoard", None)):
        raise BoardCopyError("This KiCad version cannot safely snapshot a board.")
    return result


def _load_document(document: str, pcbnew_module: Any) -> NativeBoardOwner:
    """Reparse native geometry with explicit, balanced allocation cleanup."""
    owner = None
    try:
        io = _native_io(pcbnew_module)
        destroy = getattr(
            getattr(pcbnew_module, "BOARD", None), "__swig_destroy__", None
        )
        if not callable(destroy):
            raise BoardCopyError(
                "This KiCad version cannot safely release a board snapshot."
            )
        with TemporaryDirectory(prefix="jlcpcb-impedance-board-") as directory:
            path = Path(directory) / "snapshot.kicad_pcb"
            path.write_text(document, encoding="utf-8")
            # Never use pcbnew.LoadBoard: it loads projects and changes globals.
            board = io.LoadBoard(str(path), None)
            if board is None:
                raise BoardCopyError("KiCad returned an empty board snapshot.")
            # No append target means a newly allocated BOARD. Never promote its
            # wrapper: SWIG 4.4.1 then unbalances the global type registry.
            owner = NativeBoardOwner(board, destroy)
            return owner
    except BaseException as error:
        if owner is not None:
            owner.close()
        if isinstance(error, BoardCopyError) or not isinstance(error, Exception):
            raise
        raise BoardCopyError(
            f"Cannot load a detached board snapshot: {error}"
        ) from error


def empty_board(pcbnew_module: Any) -> NativeBoardOwner:
    """Return an empty detached snapshot owner for use in a context manager."""
    return _load_document(_MINIMAL_BOARD, pcbnew_module)


def _record_tokens(document: str, expected_root: str = "") -> list[str]:
    """Validate one native record, respecting quoted text and escaped delimiters."""
    tokens = []
    position = 0
    for match in _TOKENS.finditer(document):
        if document[position : match.start()].strip():
            raise BoardCopyError("KiCad returned malformed snapshot text.")
        tokens.append(match.group())
        position = match.end()
    if document[position:].strip() or len(tokens) < 3 or tokens[0] != "(":
        raise BoardCopyError("KiCad returned an incomplete snapshot record.")
    if expected_root and tokens[1] != expected_root:
        raise BoardCopyError("KiCad returned an invalid board snapshot root.")
    depth = 0
    for index, token in enumerate(tokens):
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        if depth <= 0 and index != len(tokens) - 1:
            raise BoardCopyError("KiCad returned multiple snapshot roots.")
    if depth or tokens[-1] != ")":
        raise BoardCopyError("KiCad returned an incomplete snapshot record.")
    return tokens


def _check_named_nets(tokens: list[str]) -> None:
    """Reject legacy per-item numeric net mapping rather than mixing mappings."""
    for index in range(len(tokens) - 2):
        if tokens[index : index + 2] == ["(", "net"]:
            if not tokens[index + 2].startswith('"'):
                raise BoardCopyError(
                    "Safe impedance snapshots require KiCad's named-net format "
                    "(KiCad 10 or newer). Numeric net codes are not supported."
                )


def _check_fills(collections: dict[str, tuple[Any, ...]]) -> None:
    """Refuse unfilled/flagged zones; flags cannot certify actual fill freshness."""
    zones = list(collections["Zones"])
    for footprint in collections["GetFootprints"]:
        if hasattr(footprint, "Zones"):
            zones.extend(footprint.Zones())
    for zone in zones:
        try:
            if not zone.IsOnCopperLayer() or zone.GetIsRuleArea():
                continue
            if not zone.IsFilled() or zone.NeedRefill():
                raise BoardCopyError(_REFILL_MESSAGE)
        except AttributeError as error:
            raise BoardCopyError(
                "KiCad cannot read copper-zone fill status. " + _REFILL_MESSAGE
            ) from error


def _copy_settings(source: Any, shadow: Any) -> None:
    """Copy non-geometric board values only into a temporary settings shadow."""
    for suffix in (
        "DesignSettings",
        "EnabledLayers",
        "PageSettings",
        "TitleBlock",
        "Properties",
    ):
        getattr(shadow, "Set" + suffix)(getattr(source, "Get" + suffix)())
    for layer in source.GetEnabledLayers().Seq():
        shadow.SetLayerName(layer, source.GetLayerName(layer))
        shadow.SetLayerType(layer, source.GetLayerType(layer))


def copy_for_render(board: Any, pcbnew_module: Any) -> NativeBoardOwner:
    """Copy current unsaved geometry and native fill caches for read-only plotting.

    This deliberately does not refill. Native fill requires project rules and
    independent netclass state that the SWIG API cannot safely clone in full.
    Plugin review prepares the live board before snapshotting; standalone callers
    must supply filled boards. Unfilled or explicitly flagged copper is an
    actionable error, but these flags do not detect every stale fill cache.
    """
    try:
        collections = {
            method: tuple(getattr(board, method)()) for method in _ITEM_COLLECTIONS
        }
        _check_fills(collections)
        text_context = resolve_text_context(board, pcbnew_module)
        with empty_board(pcbnew_module) as shadow:
            _copy_settings(board, shadow)
            output = pcbnew_module.STRING_FORMATTER()
            _native_io(pcbnew_module).FormatBoardToFormatter(output, shadow)
            header = str(output.GetString()).strip()
        _check_named_nets(_record_tokens(header, "kicad_pcb"))

        # Use a fresh formatter: full-board formatting clears its output pointer.
        formatter = _native_io(pcbnew_module)
        records = []
        for items in collections.values():
            for item in items:
                formatter.Format(item)
                record = str(formatter.GetStringOutput(True)).strip()
                _check_named_nets(_record_tokens(record))
                records.append(record)
        document = header[:-1] + "\n" + "\n".join(records) + "\n)\n"
        owner = _load_document(document, pcbnew_module)
        try:
            detached = owner.board
            for method, items in collections.items():
                if len(tuple(getattr(detached, method)())) != len(items):
                    raise BoardCopyError(
                        "KiCad dropped geometry while parsing the board snapshot."
                    )
            # Restore built-in filename expansion without attaching the source
            # PROJECT (which would share mutable settings and caches).
            detached.SetFileName(str(board.GetFileName()))
            apply_text_context(board, detached, pcbnew_module, text_context)
            return owner
        except BaseException:
            owner.close()
            raise
    except BoardCopyError:
        raise
    except Exception as error:
        raise BoardCopyError(
            f"Cannot safely snapshot the live board: {error}"
        ) from error
