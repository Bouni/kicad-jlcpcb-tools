"""Bound catalog refresh traffic across dialog lifetimes without persisting failures."""

from collections.abc import Callable
from dataclasses import dataclass
import math
from threading import Lock
import time
from typing import Optional

from .stackup_model import Stackup

CATALOG_RETRY_SECONDS = 5 * 60.0


@dataclass(frozen=True)
class RefreshReservation:
    """Either own a request token or wait for an active request/cooldown."""

    token: Optional[int]
    retry_in: float = 0.0
    cooling_down: bool = False
    waiting_for: Optional[int] = None


class CatalogRefreshGate:
    """Permit one worker per copper count and remember only transient cooldowns."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = Lock()
        self._next_token = 0
        self._active: dict[int, int] = {}
        self._retry_at: dict[int, float] = {}
        self._success: dict[int, tuple[int, tuple[Stackup, ...]]] = {}

    def reserve(self, layer_count: int) -> RefreshReservation:
        """Atomically reserve eligible traffic; GUI observers may retry later."""
        self._validate_count(layer_count)
        with self._lock:
            if layer_count in self._active:
                return RefreshReservation(
                    None, retry_in=1.0, waiting_for=self._active[layer_count]
                )
            remaining = self._retry_at.get(layer_count, 0.0) - self._clock()
            if remaining > 0:
                return RefreshReservation(None, remaining, cooling_down=True)
            self._retry_at.pop(layer_count, None)
            self._next_token += 1
            token = self._next_token
            self._active[layer_count] = token
            return RefreshReservation(token)

    def finish(
        self,
        layer_count: int,
        token: int,
        *,
        retry_after: Optional[float] = None,
        catalog: Optional[tuple[Stackup, ...]] = None,
    ) -> None:
        """Release the completing worker, respecting a provider's longer delay.

        Cancellation alone does not release its token: the request must actually
        exit first. Stale completions cannot release a replacement worker.
        """
        self._validate_count(layer_count)
        with self._lock:
            if self._active.get(layer_count) != token:
                return
            self._active.pop(layer_count)
            if retry_after is None:
                self._retry_at.pop(layer_count, None)
                if catalog is not None:
                    self._success[layer_count] = (token, catalog)
            else:
                delay = (
                    retry_after
                    if math.isfinite(retry_after) and retry_after >= 0
                    else 0.0
                )
                self._retry_at[layer_count] = self._clock() + max(
                    CATALOG_RETRY_SECONDS, delay
                )

    def completed(self, layer_count: int, token: int) -> Optional[tuple[Stackup, ...]]:
        """Supply a joined request's result or a newer complete successful catalog."""
        self._validate_count(layer_count)
        with self._lock:
            result = self._success.get(layer_count)
            return result[1] if result is not None and result[0] >= token else None

    @staticmethod
    def _validate_count(layer_count: int) -> None:
        """Keep the process-wide state bounded by KiCad's copper-layer range."""
        if type(layer_count) is not int or not 2 <= layer_count <= 64:
            raise ValueError("Catalog refresh requires 2–64 copper layers.")


DEFAULT_REFRESH_GATE = CatalogRefreshGate()
