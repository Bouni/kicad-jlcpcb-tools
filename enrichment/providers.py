"""Generic provider interfaces for part metadata enrichment.

This module intentionally defines a provider contract that can be reused for
future metadata sources (for example, EasyEDA) while keeping only the existing
LCSC-backed implementation for now.
"""

from __future__ import annotations

from collections.abc import Iterable
import contextlib
from typing import Protocol

try:
    from ..lcsc_api import LCSC_API
except ImportError:  # pragma: no cover - test import fallback
    from lcsc_api import LCSC_API


class AssemblyMetadataProvider(Protocol):
    """Provider contract for assembly metadata enrichment by part code."""

    def fetch_many(self, part_codes: Iterable[str]) -> dict[str, dict[str, object]]:
        """Return normalized metadata by part code.

        Normalized payload keys:
        - `assembly_process`: str
        - `component_product_type`: int | None
        - `is_standard_assembly`: bool
        """
        ...


class LCSCAssemblyMetadataProvider:
    """LCSC-backed implementation of `AssemblyMetadataProvider`.

    Converts heterogeneous API payloads into the normalized contract consumed
    by BOM enrichment code and tests.
    """

    def __init__(self, api=None):
        self._api = api or LCSC_API()

    @staticmethod
    def _safe_int(value: object, default: int = 0) -> int:
        """Best-effort integer conversion helper."""
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float, str)):
            with contextlib.suppress(ValueError, TypeError):
                return int(value)
        return default

    def fetch_many(self, part_codes: Iterable[str]) -> dict[str, dict[str, object]]:
        """Fetch assembly metadata values from LCSC API for the given part codes."""
        results = {}
        for code in part_codes:
            assembly_process = ""
            component_product_type = None
            try:
                part_data = self._api.get_part_data(code)
                if part_data.get("success"):
                    payload = part_data.get("data", {}).get("data", {})
                    assembly_process = payload.get("assemblyProcess", "")
                    component_product_type = payload.get("componentProductType")
            except Exception:  # pylint: disable=broad-exception-caught
                assembly_process = ""
                component_product_type = None

            is_standard = False
            with contextlib.suppress(ValueError, TypeError):
                is_standard = self._safe_int(component_product_type) != 0

            results[code] = {
                "assembly_process": assembly_process,
                "component_product_type": component_product_type,
                "is_standard_assembly": is_standard,
            }
        return results


def fetch_assembly_processes(
    lcsc_codes: Iterable[str],
    provider: AssemblyMetadataProvider | None = None,
) -> dict[str, dict[str, object]]:
    """Compatibility helper used by BOM enrichment worker.

    Uses the LCSC provider by default and preserves the legacy return shape
    expected by existing call sites.
    """
    effective_provider = provider or LCSCAssemblyMetadataProvider()
    return effective_provider.fetch_many(lcsc_codes)
