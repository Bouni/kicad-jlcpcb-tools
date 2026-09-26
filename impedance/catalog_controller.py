"""One GUI-owned catalog check shared by the impedance dialog and its picker."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re
from threading import Event, Thread
from typing import Optional
from weakref import ReferenceType, WeakMethod, ref

import wx

from .catalog_cache import CatalogCache, catalog_check_due, catalog_checked_at
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
    error: str,
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
) -> None:
    """Keep HTTP and parsing off wx; no repository or widget is accessed here."""
    catalog: Optional[tuple[Stackup, ...]] = None
    error = ""
    try:
        if fetch_catalog is None:
            from .jlcpcb_stackups import fetch_stackups

            fetch_catalog = fetch_stackups
        catalog = accepted_catalog(
            fetch_catalog(layer_count, cancel=cancel.is_set), layer_count
        )
    except Exception as failure:
        error = str(failure)[:500] or "The JLCPCB catalog could not be retrieved."
    if cancel.is_set():
        return
    try:
        wx.CallAfter(_deliver_catalog, owner, generation, catalog, error)
    except RuntimeError:
        # The application can disappear while a bounded HTTP request finishes.
        return


class CatalogController:
    """Own one idempotent catalog check for one dialog lifetime and copper count."""

    def __init__(
        self,
        layer_count: int,
        cache: CatalogCache = CatalogCache(),
        *,
        load_cache: Optional[Callable[[int], CatalogCache]] = None,
        save_cache: Optional[Callable[[int, CatalogCache], None]] = None,
        fetch_catalog: Optional[Callable[..., tuple[Stackup, ...]]] = None,
        owner_current: Optional[Callable[[], bool]] = None,
    ) -> None:
        if type(layer_count) is not int or not 2 <= layer_count <= 64:
            raise ValueError("Choose a board with 2–64 enabled copper layers.")
        self.layer_count = layer_count
        self.state = CatalogState(self._valid_cache(cache))
        self._load_cache = load_cache
        self._save_cache = save_cache
        self._fetch_catalog = fetch_catalog
        self._owner_current = owner_current
        self._generation = 0
        self._started = False
        self._closed = False
        self._cancel: Optional[Event] = None
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
        """Load/check once; picker opening during a request simply joins its state."""
        if not self._current() or self._started:
            return
        self._started = True
        cache = self.state.cache
        notice = ""
        if self._load_cache is not None:
            try:
                cache = self._valid_cache(self._load_cache(self.layer_count))
            except Exception as failure:
                notice = f"The saved catalog could not be read: {str(failure)[:300]}. "
        if not self._current():
            return
        if not catalog_check_due(cache):
            self._publish(
                CatalogState(
                    cache,
                    False,
                    notice
                    + "Using the cached catalog; its last successful check was within 24 hours.",
                    bool(notice),
                )
            )
            return
        self._generation += 1
        generation = self._generation
        cancel = Event()
        self._cancel = cancel
        self._publish(
            CatalogState(
                cache,
                True,
                notice + "Checking JLCPCB's catalog… Cached choices remain available.",
                bool(notice),
            )
        )
        if not self._current():
            return
        worker = Thread(
            target=_fetch_catalog_worker,
            args=(ref(self), self._fetch_catalog, self.layer_count, cancel, generation),
            name="jlcpcb-stackup-catalog",
            daemon=True,
        )
        try:
            worker.start()
        except RuntimeError as failure:
            self._finish(generation, None, str(failure))

    def _finish(
        self, generation: int, catalog: Optional[tuple[Stackup, ...]], error: str
    ) -> None:
        """Persist a complete success before supplying its choices to subscribers."""
        if not self._current() or generation != self._generation:
            return
        self._cancel = None
        checked: Optional[CatalogCache] = None
        if not error:
            try:
                checked = CatalogCache(
                    accepted_catalog(catalog, self.layer_count), catalog_checked_at()
                )
            except (TypeError, ValueError) as failure:
                error = str(failure) or "JLCPCB returned an invalid catalog."
        if error or checked is None:
            self._publish(
                CatalogState(
                    self.state.cache,
                    False,
                    f"Catalog check failed: {error} Cached choices and the saved stackup are unchanged. "
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
        save_error = False
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
        if self._cancel is not None:
            self._cancel.set()
            self._cancel = None
        self._listeners.clear()
