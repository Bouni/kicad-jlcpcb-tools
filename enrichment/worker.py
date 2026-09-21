"""Share background supplier lookups while keeping storage and UI updates local."""

from collections.abc import Callable, Iterable, KeysView
from dataclasses import dataclass
from threading import Thread
from typing import Any, Optional

import wx

from .providers import LCSCAssemblyMetadataProvider


@dataclass(eq=False, frozen=True)
class _Batch:
    """Identify both a request and the dialog/storage lifetime that owns it."""

    generation: object
    codes: tuple[str, ...]


class AssemblyMetadataLookup:
    """Fetch off-thread; own request state and deliver callbacks on the wx thread."""

    def __init__(
        self,
        on_result: Callable[[str, dict[str, Any]], None],
        on_finished: Callable[[], None],
        on_error: Callable[[str], None],
    ) -> None:
        self._on_result = on_result
        self._on_finished = on_finished
        self._on_error = on_error
        self._pending: dict[str, _Batch] = {}
        self._attempted: set[str] = set()
        self.errors: set[str] = set()
        self._generation = object()
        self._closed = False

    @property
    def pending(self) -> KeysView[str]:
        """Expose pending IDs without letting callers change their ownership."""
        return self._pending.keys()

    def request(self, codes: Iterable[str], *, retry: bool = False) -> set[str]:
        """Deduplicate requests, optionally retrying completed missing metadata."""
        if self._closed:
            return set()
        wanted = set(codes)
        if retry:
            self._attempted.difference_update(wanted - self.pending)
        new = wanted - self._attempted
        if not new:
            return set()
        batch = _Batch(self._generation, tuple(sorted(new)))
        self._pending.update(dict.fromkeys(new, batch))
        self._attempted.update(new)
        self.errors.difference_update(new)
        try:
            Thread(target=self._fetch, args=(batch,), daemon=True).start()
        except Exception as error:
            self._finish(batch, str(error))
        return new

    def _fetch(self, batch: _Batch) -> None:
        """Schedule cleanup even when provider construction fails."""
        failure = None
        try:
            provider = LCSCAssemblyMetadataProvider(min_interval_seconds=1.0)
            for code, metadata in provider.fetch_iter(batch.codes):
                wx.CallAfter(self._deliver, batch, code, metadata or {})
        except Exception as error:
            failure = str(error)
        finally:
            wx.CallAfter(self._finish, batch, failure)

    def _deliver(self, batch: _Batch, code: str, metadata: dict[str, Any]) -> None:
        """Apply only results still owned by this request, before notifying the UI."""
        if self._pending.get(code) is not batch:
            return
        del self._pending[code]
        try:
            self._on_result(code, metadata)
        except Exception as error:
            if batch.generation is self._generation:
                if code not in self._pending:
                    self.errors.add(code)
                self._on_error(str(error))

    def _finish(self, batch: _Batch, failure: Optional[str]) -> None:
        """Release only this batch's unfinished IDs; a retry may already own others."""
        if batch.generation is not self._generation:
            return
        remaining = {code for code in batch.codes if self._pending.get(code) is batch}
        for code in remaining:
            del self._pending[code]
        if failure is not None:
            self.errors.update(remaining)
            self._on_error(failure)
        try:
            self._on_finished()
        except Exception as error:
            self._on_error(str(error))

    def invalidate(self) -> None:
        """Ignore callbacks from an obsolete storage lifetime and allow fresh work."""
        self._generation = object()
        self._pending.clear()
        self._attempted.clear()
        self.errors.clear()

    def close(self) -> None:
        """Permanently stop accepting work and ignore already queued callbacks."""
        self._closed = True
        self.invalidate()
