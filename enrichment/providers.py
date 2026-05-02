"""Generic provider interfaces for part metadata enrichment.

This module intentionally defines a provider contract that can be reused for
future metadata sources (for example, EasyEDA) while keeping only the existing
LCSC-backed implementation for now.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
import contextlib
import time
from typing import Protocol

try:
    from ..lcsc_api import LCSC_API
except ImportError:  # pragma: no cover - test import fallback
    from lcsc_api import LCSC_API


class AssemblyMetadataProvider(Protocol):
    """Provider contract for assembly metadata enrichment by part code."""

    def fetch_iter(
        self, part_codes: Iterable[str]
    ) -> Iterator[tuple[str, dict[str, object]]]:
        """Yield ``(code, metadata)`` one at a time, honouring any rate limit.

        This is the preferred interface for streaming progress back to callers
        without waiting for the full batch to complete.
        """
        ...

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

    def __init__(self, api=None, min_interval_seconds: float = 0.0):
        self._api = api or LCSC_API()
        self.min_interval_seconds = min_interval_seconds

    @staticmethod
    def _safe_int(value: object, default: int = 0) -> int:
        """Best-effort integer conversion helper."""
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float, str)):
            with contextlib.suppress(ValueError, TypeError):
                return int(value)
        return default

    def _normalize(self, code: str) -> dict[str, object]:
        """Fetch and normalize a single part code's metadata."""
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

        return {
            "assembly_process": assembly_process,
            "component_product_type": component_product_type,
            "is_standard_assembly": is_standard,
        }

    def fetch_iter(
        self, part_codes: Iterable[str]
    ) -> Iterator[tuple[str, dict[str, object]]]:
        """Yield ``(code, metadata)`` one at a time, sleeping between requests.

        The caller receives each result as soon as it is ready rather than
        waiting for the entire batch to finish.
        """
        next_allowed: float = 0.0
        for code in part_codes:
            delay = max(0.0, next_allowed - time.monotonic())
            if delay > 0:
                time.sleep(delay)
            metadata = self._normalize(code)
            next_allowed = time.monotonic() + self.min_interval_seconds
            yield code, metadata

    def fetch_many(self, part_codes: Iterable[str]) -> dict[str, dict[str, object]]:
        """Fetch assembly metadata values from LCSC API for the given part codes."""
        return dict(self.fetch_iter(part_codes))


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
