"""Backend-neutral export plan abstraction.

This module introduces an explicit export strategy boundary so fabrication export
logic can transition from SWIG-specific behavior to IPC-backed implementations
without changing call sites.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from fabrication import Fabrication


class ExportPlan(ABC):
    """Abstract export strategy for Gerber and drill generation."""

    @abstractmethod
    def generate_gerbers(self, layer_count: Optional[int] = None) -> None:
        """Generate Gerber output files."""

    @abstractmethod
    def generate_drill_files(self) -> None:
        """Generate drill output files."""


class SWIGExportPlan(ExportPlan):
    """Current SWIG-backed export strategy.

    This is intentionally a mechanical extraction with no behavior change. It
    delegates to Fabrication's existing implementation internals.
    """

    def __init__(self, fabrication: Fabrication):
        self.fabrication = fabrication

    def generate_gerbers(self, layer_count: Optional[int] = None) -> None:
        """Generate Gerber files via existing SWIG-backed implementation."""
        self.fabrication._generate_gerber_impl(layer_count)

    def generate_drill_files(self) -> None:
        """Generate drill files via existing SWIG-backed implementation."""
        self.fabrication._generate_excellon_impl()
