"""Assembly-mode policy decisions for BOM estimation."""

# ruff: noqa: D102, D105, UP045
# pyupgrade: disable

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class AssemblyModeDecision:
    """Derive the modeled assembly mode from immutable board facts.

    JLCPCB documents Economic PCBA as supporting 2-50 boards and placement on
    one side, while Standard supports higher quantities and both sides:
    https://jlcpcb.com/capabilities/pcb-assembly-capabilities

    Standard Only parts require Standard service or must remain unplaced:
    https://jlcpcb.com/help/article/common-bom-and-cpl-matching-issues-and-explanations

    ``manual_enabled`` is a local plugin override, not a JLCPCB criterion.
    """

    board_count: int
    manual_enabled: bool = False
    top_refs: frozenset[str] = field(default_factory=frozenset)
    bottom_refs: frozenset[str] = field(default_factory=frozenset)
    smt_populated_side_count: int = 0
    standard_only_refs: frozenset[str] = field(default_factory=frozenset)
    economic_only_refs: frozenset[str] = field(default_factory=frozenset)
    classification_missing_refs: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self):
        for name in ("top_refs", "bottom_refs"):
            object.__setattr__(self, name, frozenset(getattr(self, name)))
        populated_refs = self.top_refs | self.bottom_refs
        for name in (
            "standard_only_refs",
            "economic_only_refs",
            "classification_missing_refs",
        ):
            object.__setattr__(
                self, name, frozenset(getattr(self, name)) & populated_refs
            )

    @property
    def applicable(self) -> bool:
        return bool(self.top_refs or self.bottom_refs)

    @property
    def quantity_over_50(self) -> bool:
        return self.applicable and self.board_count > 50

    @property
    def both_sides_populated(self) -> bool:
        return bool(self.top_refs and self.bottom_refs)

    @property
    def board_standard(self) -> Optional[bool]:
        if not self.applicable:
            return None
        if (
            self.manual_enabled
            or self.quantity_over_50
            or self.both_sides_populated
            or self.standard_only_refs
        ):
            return True
        return None if self.classification_missing_refs else False

    @property
    def economic_only_conflict_refs(self) -> frozenset[str]:
        return self.economic_only_refs if self.board_standard is True else frozenset()


def classify_component_product_type(value: object) -> Optional[int]:
    """Normalize the observed JLC assembly classification to 0, 1, or 2.

    The numeric mapping is observed API behavior, not a published JLCPCB API
    contract: 0 is Economic and Standard, 1 is Economic Only, and 2 is
    Standard Only. Unknown or malformed values deliberately remain unknown.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return normalized if normalized in {0, 1, 2} else None
