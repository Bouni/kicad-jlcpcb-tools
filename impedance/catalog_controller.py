"""One GUI-owned catalog check shared by the impedance dialog and its picker."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import math
import re
from threading import Event, Thread
from typing import Optional
from weakref import ReferenceType, WeakMethod, ref

import wx

from .catalog_cache import CatalogCache, catalog_check_due, catalog_checked_at
from .catalog_refresh import DEFAULT_REFRESH_GATE, CatalogRefreshGate
from .stackup_model import Stackup, validate_stackup

_NO_REQUIREMENT = re.compile(r"\bno\s+requirement\b|无要求|不指定", re.IGNORECASE)


def compatible_stackup(stackup: Stackup, layer_count: int) -> bool:
    """Keep named constructions for the full enabled copper count only."""
    validate_stackup(stackup)
    return stackup.layer_count == layer_count and not _NO_REQUIREMENT.search(
        stackup.name
    )


def accepted_catalog(catalog: object, layer_count: int) -> tuple[Stackup, ...]:
    """Require a complete, bounded, unambiguous provider response."""
    if not isinstance(catalog, tuple) or len(catalog) > 2_000:
        raise ValueError("JLCPCB returned an invalid or oversized stackup catalog.")
    seen: set[str] = set()
    for item in catalog:
        if not compatible_stackup(item, layer_count):
            raise ValueError("JLCPCB returned an unnamed or incompatible stackup.")
        if item.stackup_id in seen:
            raise ValueError("JLCPCB returned duplicate stackup identifiers.")
        seen.add(item.stackup_id)
    return catalog


@dataclass(frozen=True)
class CatalogState:
    """Shared current choices and check status, never a board's selection."""

    cache: CatalogCache = CatalogCache()
    loading: bool = False
    message: str = "The catalog will be checked when this dialog opens."
    error: bool = False


def _deliver_catalog(
    owner: ReferenceType[CatalogController],
    generation: int,
    catalog: Optional[tuple[Stackup, ...]],
    error: Optional[Exception],
) -> None:
    """Resolve the weak owner only on the GUI thread before persistence or UI work."""
    controller = owner()
    if controller is not None:
        controller._finish(generation, catalog, error)


def _fetch_catalog_worker(
    owner: ReferenceType[CatalogController],
    fetch_catalog: Optional[Callable[..., tuple[Stackup, ...]]],
    layer_count: int,
    cancel: Event,
    generation: int,
    refresh_gate: CatalogRefreshGate,
    reservation: int,
) -> None:
    """Keep HTTP and parsing off wx; no repository or widget is accessed here."""
    catalog: Optional[tuple[Stackup, ...]] = None
    error: Optional[Exception] = None
    retry_after: Optional[float] = None
    try:
        from .jlcpcb_stackups import CatalogUnavailable

        try:
            if fetch_catalog is None:
                from .jlcpcb_stackups import fetch_stackups

                fetch_catalog = fetch_stackups
            catalog = accepted_catalog(
                fetch_catalog(layer_count, cancel=cancel.is_set), layer_count
            )
        except CatalogUnavailable as failure:
            retry_after = failure.retry_after
            raise
    except Exception as failure:
        error = failure
    finally:
        # The worker owns this token until the bounded request actually exits,
        # even if its dialog has already closed. A late outage still cools down
        # newly opened dialogs without updating the destroyed owner's cache.
        refresh_gate.finish(
            layer_count,
            reservation,
            retry_after=retry_after,
            catalog=catalog if error is None else None,
        )
    if cancel.is_set():
        return
    try:
        wx.CallAfter(_deliver_catalog, owner, generation, catalog, error)
    except RuntimeError:
        # The application can disappear while a bounded HTTP request finishes.
        return


def _resume_catalog(owner: ReferenceType[CatalogController], generation: int) -> None:
    """Resolve a scheduled retry weakly, after its native owner's validity check."""
    controller = owner()
    if (
        controller is not None
        and controller._current()
        and generation == controller._generation
    ):
        controller._retry_timer = None
        controller.ensure_started()


class CatalogController:
    """Share safe choices while checking and recovering from temporary outages."""

    def __init__(
        self,
        layer_count: int,
        cache: CatalogCache = CatalogCache(),
        *,
        load_cache: Optional[Callable[[int], CatalogCache]] = None,
        save_cache: Optional[Callable[[int, CatalogCache], None]] = None,
        fetch_catalog: Optional[Callable[..., tuple[Stackup, ...]]] = None,
        owner_current: Optional[Callable[[], bool]] = None,
        usable_current: Optional[Callable[[], bool]] = None,
        refresh_gate: CatalogRefreshGate = DEFAULT_REFRESH_GATE,
    ) -> None:
        if type(layer_count) is not int or not 2 <= layer_count <= 64:
            raise ValueError("Choose a board with 2–64 enabled copper layers.")
        self.layer_count = layer_count
        self.state = CatalogState(self._valid_cache(cache))
        self._load_cache = load_cache
        self._save_cache = save_cache
        self._fetch_catalog = fetch_catalog
        self._owner_current = owner_current
        self._usable_current = usable_current
        self._refresh_gate = refresh_gate
        self._generation = 0
        self._started = False
        self._completed = False
        self._notice = ""
        self._closed = False
        self._cancel: Optional[Event] = None
        self._waiting_for: Optional[int] = None
        self._retry_timer: Optional[wx.CallLater] = None
        self._next_listener = 0
        self._listeners: dict[
            int, Callable[[], Optional[Callable[[CatalogState], None]]]
        ] = {}

    def _valid_cache(self, cache: CatalogCache) -> CatalogCache:
        """Do not let incompatible or malformed cache rows suppress a check."""
        if not isinstance(cache, CatalogCache):
            # Preserve the same validation exception as malformed catalog rows.
            raise ValueError("The saved catalog is not a supported cache record.")  # noqa: TRY004
        return CatalogCache(
            accepted_catalog(cache.stackups, self.layer_count), cache.checked_at_utc
        )

    def _current(self) -> bool:
        """Reject delivery for a closed or replaced native owner before saving."""
        if self._closed:
            return False
        if self._owner_current is not None:
            try:
                if not self._owner_current():
                    self.close()
                    return False
            except RuntimeError:
                self.close()
                return False
        return True

    def subscribe(self, listener: Callable[[CatalogState], None]) -> Callable[[], None]:
        """Immediately supply shared state and return an independent unsubscribe."""
        if self._closed:
            return lambda: None
        token = self._next_listener
        self._next_listener += 1
        try:
            weak_listener = WeakMethod(listener)
        except TypeError:
            # Plain functions have no widget owner; retain them until unsubscribe.
            self._listeners[token] = lambda: listener
        else:
            self._listeners[token] = weak_listener
        listener(self.state)
        owner = ref(self)

        def unsubscribe() -> None:
            """Detach one picker without changing the parent-owned request."""
            controller = owner()
            if controller is not None:
                controller._listeners.pop(token, None)

        return unsubscribe

    def _publish(self, state: CatalogState) -> None:
        """Notify live observers after updating the single authoritative state."""
        if not self._current():
            return
        self.state = state
        for token, weak_listener in tuple(self._listeners.items()):
            if not self._current():
                return
            if token not in self._listeners:
                continue
            listener = weak_listener()
            if listener is None:
                self._listeners.pop(token, None)
                continue
            try:
                listener(state)
            except (ReferenceError, RuntimeError):
                # Native windows may be destroyed while other observers remain.
                self._listeners.pop(token, None)

    def ensure_started(self) -> None:
        """Join active work, or quietly retry a due catalog after its cooldown."""
        if (
            not self._current()
            or self._completed
            or self._cancel is not None
            or self._retry_timer is not None
        ):
            return
        cache = self.state.cache
        if not self._started:
            self._started = True
            if self._load_cache is not None:
                try:
                    cache = self._valid_cache(self._load_cache(self.layer_count))
                except Exception as failure:
                    self._notice = (
                        f"The saved catalog could not be read: {str(failure)[:300]}. "
                    )
        if not self._current():
            return
        if self._waiting_for is not None:
            shared = self._refresh_gate.completed(self.layer_count, self._waiting_for)
            if shared is not None:
                self._waiting_for = None
                self._finish(self._generation, shared, None)
                return
        if not catalog_check_due(cache):
            self._completed = True
            self._publish(
                CatalogState(
                    cache,
                    False,
                    self._notice
                    + "Using the cached catalog; its last successful check was within 24 hours.",
                    bool(self._notice),
                )
            )
            return
        reservation = self._refresh_gate.reserve(self.layer_count)
        self._waiting_for = reservation.waiting_for
        if reservation.token is None:
            if reservation.cooling_down:
                self._publish_unavailable(cache)
            else:
                self._publish(
                    CatalogState(
                        cache,
                        True,
                        self._notice
                        + "A catalog check is already running. "
                        + self._fallback_message(cache),
                        bool(self._notice),
                    )
                )
            if self._current():
                self._schedule_retry(reservation.retry_in)
            return
        self._generation += 1
        generation = self._generation
        cancel = Event()
        self._cancel = cancel
        self._publish(
            CatalogState(
                cache,
                True,
                self._notice
                + "Checking JLCPCB's catalog… "
                + self._fallback_message(cache),
                bool(self._notice),
            )
        )
        if not self._current():
            self._refresh_gate.finish(self.layer_count, reservation.token)
            return
        try:
            worker = Thread(
                target=_fetch_catalog_worker,
                args=(
                    ref(self),
                    self._fetch_catalog,
                    self.layer_count,
                    cancel,
                    generation,
                    self._refresh_gate,
                    reservation.token,
                ),
                name="jlcpcb-stackup-catalog",
                daemon=True,
            )
            worker.start()
        except RuntimeError as failure:
            self._refresh_gate.finish(self.layer_count, reservation.token)
            self._finish(generation, None, failure)

    def _has_fallback(self, cache: CatalogCache) -> bool:
        """Count cached choices or the current compatible saved construction."""
        if cache.stackups:
            return True
        if self._usable_current is None:
            return False
        try:
            return bool(self._usable_current())
        except (ReferenceError, RuntimeError, ValueError):
            return False

    def _fallback_message(self, cache: CatalogCache) -> str:
        """Explain the work that remains available without changing selections."""
        if cache.stackups:
            return "Cached choices and the saved construction remain available."
        if self._has_fallback(cache):
            return "Your saved construction remains available."
        return "Compatible stackup choices will appear when the check succeeds."

    def _publish_unavailable(self, cache: CatalogCache) -> None:
        """Temporary failures are quiet when existing choices remain usable."""
        usable = self._has_fallback(cache)
        message = (
            "JLCPCB's catalog is temporarily unavailable. "
            + self._fallback_message(cache)
            if usable
            else "JLCPCB's catalog is temporarily unavailable. No compatible stackup is available; check the network connection or try again later."
        )
        self._publish(
            CatalogState(
                cache,
                False,
                self._notice
                + message
                + " The catalog will retry automatically after a short cooldown.",
                not usable or bool(self._notice),
            )
        )

    def _schedule_retry(self, seconds: float) -> None:
        """Schedule one weak callback; large server delays are rechecked safely."""
        milliseconds = max(1, math.ceil(min(24 * 60 * 60.0, seconds) * 1000))
        self._retry_timer = wx.CallLater(
            milliseconds, _resume_catalog, ref(self), self._generation
        )

    def _finish(
        self,
        generation: int,
        catalog: Optional[tuple[Stackup, ...]],
        error: Optional[Exception],
    ) -> None:
        """Persist a complete success before supplying its choices to subscribers."""
        if not self._current() or generation != self._generation:
            return
        self._cancel = None
        if error is not None:
            try:
                from .jlcpcb_stackups import CatalogCancelled, CatalogUnavailable
            except Exception:
                pass  # A missing transport uses the existing generic failure below.
            else:
                if isinstance(error, CatalogUnavailable):
                    # The worker has already released the token and installed cooldown.
                    self.ensure_started()
                    return
                if isinstance(error, CatalogCancelled):
                    self._completed = True
                    self._publish(
                        CatalogState(
                            self.state.cache,
                            False,
                            "The catalog check was cancelled. "
                            + self._fallback_message(self.state.cache),
                        )
                    )
                    return
        self._completed = True
        checked: Optional[CatalogCache] = None
        if error is None:
            try:
                checked = CatalogCache(
                    accepted_catalog(catalog, self.layer_count), catalog_checked_at()
                )
            except (TypeError, ValueError) as failure:
                error = failure
        if error is not None or checked is None:
            self._publish(
                CatalogState(
                    self.state.cache,
                    False,
                    f"Catalog check failed: {str(error)[:500] or 'The JLCPCB catalog could not be retrieved.'} Cached choices and the saved stackup are unchanged. "
                    "The catalog will be checked again when the impedance dialog is reopened.",
                    True,
                )
            )
            return
        message = (
            f"Catalog checked: {len(checked.stackups)} compatible stackups. Select a stackup to change the board's saved construction."
            if checked.stackups
            else f"JLCPCB returned no compatible named {self.layer_count}-layer stackups."
        )
        save_error = not checked.stackups and not self._has_fallback(checked)
        if self._save_cache is not None:
            if not self._current() or generation != self._generation:
                return
            try:
                self._save_cache(self.layer_count, checked)
            except Exception as failure:
                message += (
                    f" The catalog and its check time could not be saved: {str(failure)[:300]}. "
                    "New choices are available in this dialog only; the persisted check time is unchanged."
                )
                save_error = True
        if not self._current() or generation != self._generation:
            return
        self._publish(CatalogState(checked, False, message, save_error))

    def close(self) -> None:
        """Invalidate queued results before canceling; never erase cached metadata."""
        self._closed = True
        self._generation += 1
        if self._retry_timer is not None:
            self._retry_timer.Stop()
            self._retry_timer = None
        if self._cancel is not None:
            self._cancel.set()
            self._cancel = None
        self._listeners.clear()
