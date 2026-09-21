"""Coordinate native variant edits and coherent in-memory assembly views."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .native import (
        BoardVariantSnapshot,
        VariantEdit,
        VariantNativeAdapter,
        VariantTarget,
    )
    from .store import VariantStore


class VariantSessionError(RuntimeError):
    """An operation no longer has a valid, unambiguous native target."""


@dataclass(frozen=True)
class AssignmentSession:
    """Immutable destinations captured when the assignment window is activated."""

    serial: int
    targets: tuple[VariantTarget, ...]


class VariantSession:
    """Own one board lifetime, coherent projection and independent output target."""

    def __init__(
        self,
        adapter: VariantNativeAdapter,
        cache: VariantStore,
        get_board: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.adapter = adapter
        self.cache = cache
        self.get_board = get_board
        self.snapshot: BoardVariantSnapshot
        self.reliable = False
        self.generating = False
        self._generation_snapshot: Optional[BoardVariantSnapshot] = None
        self._assignment: Optional[AssignmentSession] = None
        self._serial = 0
        self.refresh()
        remembered = cache.get_output_variant()
        self.output_variant = (
            self.snapshot.native_variant_name if remembered is None else remembered
        )

    def _check_board(self) -> None:
        board = self.get_board() if self.get_board is not None else self.adapter.board
        if not self.adapter.is_current_board(board):
            self.reliable = False
            self._assignment = None
            raise VariantSessionError(
                "The PCB was replaced or saved under another name. Reopen JLCPCB Tools."
            )
        self.cache.ensure_current_board()

    @staticmethod
    def _same_assembly(
        current: BoardVariantSnapshot, captured: BoardVariantSnapshot
    ) -> bool:
        """Ignore KiCad's bookkeeping timestamp and editor's active variant."""
        return (current.board_token, current.variants, current.components) == (
            captured.board_token,
            captured.variants,
            captured.components,
        )

    def refresh(self) -> BoardVariantSnapshot:
        """Publish one native snapshot, retaining failure as an explicit state."""
        self._check_board()
        try:
            if self.adapter.unreliable:
                raise VariantSessionError(
                    "A native edit could not be fully restored. Inspect the PCB and reopen JLCPCB Tools before continuing."
                )
            snapshot = self.adapter.snapshot()
        except Exception:
            self.reliable = False
            raise
        self.snapshot = snapshot
        self.reliable = True
        return snapshot

    def require_editable(self) -> None:
        """Reject unsafe editing, including reentrant actions during generation."""
        self._check_board()
        if self.generating:
            raise VariantSessionError("Finish generation before editing variants.")
        if not self.reliable:
            raise VariantSessionError(
                "Variant data is unavailable. Refresh before editing or generating."
            )

    def apply(self, edits: Sequence[VariantEdit]) -> BoardVariantSnapshot:
        """Apply native edits first and recover projection failures by rereading."""
        self.require_editable()
        try:
            snapshot = self.adapter.apply_edits(tuple(edits))
        except Exception:
            # Compensation belongs to the adapter. A reread establishes whether
            # recovery succeeded; never replay a cached assignment to the board.
            try:
                self.refresh()
            except Exception:
                self.reliable = False
            raise
        self.snapshot = snapshot
        return snapshot

    def begin_assignment(self, targets: Sequence[VariantTarget]) -> AssignmentSession:
        """Capture an explicit one-variant batch independently of grid focus."""
        self.require_editable()
        if not targets or len({target.variant_name for target in targets}) != 1:
            raise VariantSessionError(
                "Select components within one variant for assignment."
            )
        self._serial += 1
        self._assignment = AssignmentSession(self._serial, tuple(targets))
        return self._assignment

    def accept_assignment(self, context: Any) -> AssignmentSession:
        """Reject queued events from a replaced or unrelated assignment window."""
        self.require_editable()
        if not isinstance(context, AssignmentSession) or context != self._assignment:
            raise VariantSessionError(
                "This assignment session was replaced. Select the part again."
            )
        return context

    def set_output_variant(self, name: str) -> None:
        """Persist only a user's explicit, validated output choice."""
        self.require_editable()
        if name not in {variant.name for variant in self.snapshot.variants}:
            raise VariantSessionError(
                "Output variant is unavailable. Choose an existing variant."
            )
        self.cache.set_output_variant(name)
        self.output_variant = name

    def begin_generation(self) -> tuple[BoardVariantSnapshot, str]:
        """Capture one source for every output artifact and freeze mutations."""
        self.require_editable()
        self.refresh()
        if self.output_variant not in {
            variant.name for variant in self.snapshot.variants
        }:
            raise VariantSessionError(
                "Output variant is unavailable. Choose an existing variant."
            )
        self._generation_snapshot = self.snapshot
        self.generating = True
        return self.snapshot, self.output_variant

    def validate_generation(self) -> None:
        """Stop publication if a hook or native edit changed the captured source."""
        self._check_board()
        if not self.generating or self._generation_snapshot is None:
            raise VariantSessionError("No variant generation is active.")
        current = self.adapter.snapshot()
        captured = self._generation_snapshot
        # KiCad's monotonically increasing timestamp also changes for legitimate
        # zone preparation. Compare complete assembly state; Fabrication guards
        # auxiliary origin, pad geometry, and serialized plot content separately.
        if not self._same_assembly(current, captured):
            raise VariantSessionError(
                "The PCB or its variants changed during generation. Generate again from the refreshed board."
            )

    def end_generation(self) -> None:
        """Release the operation lock without changing output or edit focus."""
        self.generating = False
        self._generation_snapshot = None
