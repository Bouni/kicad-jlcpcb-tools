"""Background fetch worker: one daemon thread, two paced queues and a breaker.

The lookup queue holds LCSC codes and is served in chunks of up to 200 through the
batch endpoint; the document queue holds footprint and symbol uuids, footprints
first, served one per request.  Each queue has its own token bucket: lookups get a
burst of two, then one every two seconds; documents a burst of ten, then one every
1 to 1.2 seconds.  That is well under the 2.5 document requests per second the Pro
host has answered over long runs through EasyEDA's library without a refusal.

Each pass serves the first queue, lookups then documents, that is non-empty and
holds a token; when neither does, the thread waits for the earliest refill.  A
lookup waits half a second after the newest code arrived, so a burst of
assignments shares one call.  Three consecutive failures, transient or not, trip
the worker's breaker: it serves neither queue again, what is queued stays pending
and one message is reported.  Each job is processed in isolation, so nothing raises
out of the thread.

The queues and the in-flight slot are guarded by one lock, so the enqueue and
query methods are safe from any thread; a job and its result handler run on the
thread draining the queues, outside the lock.  Stdlib only.
"""

from __future__ import annotations

import collections
from collections.abc import Callable, Iterable
from dataclasses import dataclass
import logging
import math
import random
import threading
import time
from typing import Any, Optional

from .easyeda_client import BATCH_SIZE, FOOTPRINT, SYMBOL

logger = logging.getLogger(__name__)

LOOKUP_BURST = 2
LOOKUP_INTERVAL_S = 2.0
LOOKUP_JITTER_S = 0.0
DOCUMENT_BURST = 10
DOCUMENT_INTERVAL_S = 1.0
DOCUMENT_JITTER_S = 0.2
COALESCE_S = 0.5
MAX_CONSECUTIVE_FAILURES = 3
LOOKUP = "lookup"
BREAKER_MESSAGE = (
    "{failures} fetches from EasyEDA failed in a row; the {pending} part lookup(s) and "
    "drawing(s) still queued wait until the plugin is reopened."
)


class TokenBucket:
    """``burst`` tokens to start, one more every interval plus jitter.

    ``take`` waits for a token; ``try_take`` spends one only when it is there;
    ``seconds_until_token`` says how long ``take`` would wait.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        wait: Optional[Callable[[float], bool]] = None,
        jitter: Callable[[], float] = random.random,
        burst: int = DOCUMENT_BURST,
        interval: float = DOCUMENT_INTERVAL_S,
        jitter_s: float = DOCUMENT_JITTER_S,
    ) -> None:
        self.clock = clock
        # wait(seconds) returns True when the caller should stop instead of waiting.
        self.wait = wait or (lambda seconds: (time.sleep(seconds), False)[1])
        self.jitter = jitter
        self.burst = burst
        self.interval = interval
        self.jitter_s = jitter_s
        self.tokens = float(burst)
        self.next_refill = clock() + self._spacing()
        self.lock = threading.Lock()

    def _spacing(self) -> float:
        return self.interval + self.jitter() * self.jitter_s

    def _refill(self) -> None:
        now = self.clock()
        while now >= self.next_refill and self.tokens < self.burst:
            self.tokens += 1
            self.next_refill += self._spacing()
        if self.tokens >= self.burst:
            self.next_refill = max(self.next_refill, now + self._spacing())

    def try_take(self) -> bool:
        """Spend a token when one is available; never waits."""
        with self.lock:
            self._refill()
            if self.tokens >= 1:
                self.tokens -= 1
                return True
            return False

    def seconds_until_token(self) -> float:
        """Return how long the next token takes, 0 when one is available now."""
        with self.lock:
            self._refill()
            if self.tokens >= 1:
                return 0.0
            return max(0.0, self.next_refill - self.clock())

    def take(self, wait: Optional[Callable[[float], bool]] = None) -> bool:
        """Block until a token is available and spend it; False when asked to stop.

        ``wait`` is the caller's stop-aware sleep, so a bucket shared by successive
        workers waits on the worker that is asking, not on the one that created it.
        """
        wait = wait or self.wait
        while True:
            if self.try_take():
                return True
            if wait(self.seconds_until_token()):
                return False


@dataclass
class Buckets:
    """The two request budgets, one per queue.

    Successive workers may share one pair, so starting a new worker does not reset
    the pace.
    """

    lookups: TokenBucket
    documents: TokenBucket

    @classmethod
    def default(
        cls,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ) -> Buckets:
        """Return the two queues' buckets at the default paces (the module's constants)."""
        return cls(
            lookups=TokenBucket(
                clock=clock,
                jitter=jitter,
                burst=LOOKUP_BURST,
                interval=LOOKUP_INTERVAL_S,
                jitter_s=LOOKUP_JITTER_S,
            ),
            documents=TokenBucket(
                clock=clock,
                jitter=jitter,
                burst=DOCUMENT_BURST,
                interval=DOCUMENT_INTERVAL_S,
                jitter_s=DOCUMENT_JITTER_S,
            ),
        )


def estimate_seconds(lookups: int, documents: int) -> float:
    """Return roughly how long the queued requests take at the buckets' paces.

    A queued code counts as its lookup chunk plus the two documents it may need,
    so the estimate right after a scan covers the fetch, not just the lookups.
    """
    return (
        math.ceil(lookups / BATCH_SIZE) * LOOKUP_INTERVAL_S
        + (documents + 2 * lookups) * DOCUMENT_INTERVAL_S
    )


class FetchWorker:
    """Drain the two queues through ``lookup`` and ``fetch`` on a daemon thread."""

    def __init__(
        self,
        lookup: Callable[[list[str]], Any],
        fetch: Callable[[str, str], Any],
        on_lookup: Callable[[list[str], Any], None],
        on_document: Callable[[str, str, Any], None],
        on_tripped: Optional[Callable[[str], None]] = None,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
        buckets: Optional[Buckets] = None,
    ) -> None:
        self.lookup = lookup
        self.fetch = fetch
        self.on_lookup = on_lookup
        self.on_document = on_document
        self.on_tripped = on_tripped
        self.clock = clock
        self.stop_event = threading.Event()
        # The stop-aware sleep every wait goes through; tests replace it with a fake clock's.
        self.wait = self.stop_event.wait
        self.buckets = buckets or Buckets.default(clock=clock, jitter=jitter)
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.lookups: collections.deque = collections.deque()
        self.queued_lookups: set[str] = set()
        self.lookup_added_at = clock()
        self.footprints: collections.deque = collections.deque()
        self.symbols: collections.deque = collections.deque()
        self.queued_documents: set[tuple[str, str]] = set()
        self.in_flight: Optional[tuple[str, Any]] = None
        self.current_bucket: Optional[TokenBucket] = None
        self.first_attempt_paid = False
        # The end of a backoff the job in flight is sleeping through (clock units).
        self.backoff_until: Optional[float] = None
        self.consecutive_failures = 0
        self.tripped = False
        self.thread = threading.Thread(
            target=self._run, name="jlcfootprint-fetch", daemon=True
        )

    # ------------------------------------------------------------------
    # Queues
    # ------------------------------------------------------------------

    def enqueue_lookups(self, codes: Iterable[str]) -> int:
        """Queue codes not already queued or in flight; return how many were added."""
        added = 0
        with self.lock:
            in_flight = self._in_flight_lookup()
            for code in codes:
                if code and code not in self.queued_lookups and code not in in_flight:
                    self.lookups.append(code)
                    self.queued_lookups.add(code)
                    added += 1
            if added:
                self.lookup_added_at = self.clock()
        if added:
            self.wake.set()
        return added

    def enqueue_documents(self, items: Iterable[tuple[str, str]]) -> int:
        """Queue (kind, uuid) pairs not already queued or in flight; return how many were added."""
        added = 0
        with self.lock:
            for kind, uuid in items:
                item = (kind, uuid)
                if (
                    not uuid
                    or kind not in (FOOTPRINT, SYMBOL)
                    or item in self.queued_documents
                    or item == self.in_flight
                ):
                    continue
                (self.footprints if kind == FOOTPRINT else self.symbols).append(uuid)
                self.queued_documents.add(item)
                added += 1
        if added:
            self.wake.set()
        return added

    def _in_flight_lookup(self) -> set[str]:
        if self.in_flight is not None and self.in_flight[0] == LOOKUP:
            return set(self.in_flight[1])
        return set()

    def pending_lookups(self) -> set[str]:
        """Return the codes still queued or in flight."""
        with self.lock:
            return set(self.queued_lookups) | self._in_flight_lookup()

    def pending_documents(self) -> set[tuple[str, str]]:
        """Return the (kind, uuid) pairs still queued or in flight."""
        with self.lock:
            pending = set(self.queued_documents)
            if self.in_flight is not None and self.in_flight[0] != LOOKUP:
                pending.add(self.in_flight)
            return pending

    def counts(self) -> dict[str, int]:
        """Return how many codes, footprints and symbols are queued (in flight excluded)."""
        with self.lock:
            return {
                "lookups": len(self.lookups),
                "footprints": len(self.footprints),
                "symbols": len(self.symbols),
            }

    def estimate_seconds(self) -> float:
        """Return roughly how long the queued requests take."""
        counts = self.counts()
        return estimate_seconds(
            counts["lookups"], counts["footprints"] + counts["symbols"]
        )

    def wait_for(self, seconds: float) -> bool:
        """Wait up to ``seconds`` (the client's backoff); True when the worker was asked to stop."""
        with self.lock:
            self.backoff_until = self.clock() + seconds
        try:
            return self.wait(seconds)
        finally:
            with self.lock:
                self.backoff_until = None

    def active(self) -> tuple[int, float]:
        """Return the jobs in flight (0 or 1) and the seconds left of a backoff one sleeps through."""
        with self.lock:
            jobs = 0 if self.in_flight is None else 1
            remaining = (
                0.0
                if self.backoff_until is None
                else max(0.0, self.backoff_until - self.clock())
            )
        return jobs, remaining

    def acquire(self) -> bool:
        """Spend a request token for the job in flight; False when stopping.

        The scheduler already paid for the first attempt of the job it dispatched;
        the client's retries pay as they go.
        """
        if self.stop_event.is_set():
            return False
        if self.first_attempt_paid:
            self.first_attempt_paid = False
            return True
        bucket = self.current_bucket or self.buckets.documents
        return bucket.take(self.wait)

    # ------------------------------------------------------------------
    # Thread
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the daemon thread once."""
        if not self.thread.is_alive():
            self.thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the thread to stop (interrupting any wait or backoff) and join it."""
        self.stop_event.set()
        self.wake.set()
        if self.thread.is_alive():
            self.thread.join(timeout)

    def is_running(self) -> bool:
        """Return True while the thread is alive."""
        return self.thread.is_alive()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            self.wake.wait()
            self.wake.clear()
            self.run_pending()

    def _choose(self) -> tuple[str, Any, float]:
        """Pick the next job under the lock: (kind, payload, pause).

        ``kind`` is ``lookup``, ``footprint`` or ``symbol`` with the payload taken off
        its queue and the bucket charged, ``wait`` with the seconds to pause, or
        ``idle`` when every queue is empty.
        """
        with self.lock:
            now = self.clock()
            settling = (
                COALESCE_S - (now - self.lookup_added_at) if self.lookups else 0.0
            )
            if self.lookups and settling <= 0 and self.buckets.lookups.try_take():
                codes = [
                    self.lookups.popleft()
                    for _ in range(min(BATCH_SIZE, len(self.lookups)))
                ]
                self.queued_lookups.difference_update(codes)
                self.in_flight = (LOOKUP, codes)
                self.current_bucket = self.buckets.lookups
                return LOOKUP, codes, 0.0
            if (self.footprints or self.symbols) and self.buckets.documents.try_take():
                kind = FOOTPRINT if self.footprints else SYMBOL
                uuid = (self.footprints if self.footprints else self.symbols).popleft()
                self.queued_documents.discard((kind, uuid))
                self.in_flight = (kind, uuid)
                self.current_bucket = self.buckets.documents
                return kind, uuid, 0.0
            pauses = []
            if self.lookups:
                pauses.append(max(settling, self.buckets.lookups.seconds_until_token()))
            if self.footprints or self.symbols:
                pauses.append(self.buckets.documents.seconds_until_token())
            if not pauses:
                return "idle", None, 0.0
            return "wait", None, max(min(pauses), 0.01)

    def run_pending(self) -> int:
        """Process the queues on the calling thread until they are empty, tripped or stopped."""
        done = 0
        while not self.stop_event.is_set() and not self.tripped:
            kind, payload, pause = self._choose()
            if kind == "idle":
                return done
            if kind == "wait":
                if self.wait(pause):
                    return done
                continue
            self.first_attempt_paid = True
            try:
                self._process(kind, payload)
            finally:
                self._release()
            done += 1
        return done

    def _release(self) -> None:
        """Clear the in-flight slot; harmless when it is already clear."""
        with self.lock:
            self.in_flight = None
            self.current_bucket = None
            self.first_attempt_paid = False

    def _process(self, kind: str, payload: Any) -> None:
        """Run one job and report it; a raised exception counts as a transient failure."""
        try:
            if kind == LOOKUP:
                fetched = self.lookup(payload)
            else:
                fetched = self.fetch(kind, payload)
        except Exception as error:
            logger.exception("jlcfootprint: %s %s raised", kind, payload)
            fetched = _Failure(str(error))
        # The request is over before its handler runs, so a handler, or a scan on
        # the main thread right after it, that queues this key again is heard
        # instead of being dropped as in flight.
        self._release()
        # A result that arrives once the worker is stopping is dropped: a request
        # cut short then says nothing about EasyEDA or the part, and a handler
        # would otherwise run after stop() has given up waiting for the thread.
        # The part is asked for again the next time it is checked, as anything
        # left queued is.
        if self.stop_event.is_set():
            return
        # Every failure counts, transient or not: a run of final refusals (an API
        # change, a block) must stop the queue as surely as a run of 503s.  A
        # part EasyEDA has nothing for is an answer, not a failure.
        if getattr(fetched, "transient", False) or getattr(fetched, "failed", False):
            self.consecutive_failures += 1
        else:
            self.consecutive_failures = 0
        try:
            if kind == LOOKUP:
                self.on_lookup(payload, fetched)
            else:
                self.on_document(kind, payload, fetched)
        except Exception:
            logger.exception(
                "jlcfootprint: result handler raised for %s %s", kind, payload
            )
        if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES and not self.tripped:
            self.tripped = True
            counts = self.counts()
            message = BREAKER_MESSAGE.format(
                failures=self.consecutive_failures, pending=sum(counts.values())
            )
            logger.warning("jlcfootprint: %s", message)
            if self.on_tripped is not None:
                try:
                    self.on_tripped(message)
                except Exception:
                    logger.exception("jlcfootprint: breaker handler raised")


class _Failure:
    """A result standing in for a lookup or fetch function that raised.

    It is a failed, transient answer with nothing in it: ``record`` and
    ``devices`` are None, so a handler checks ``failed`` or ``transient`` before
    reading them.
    """

    failed = True
    transient = True

    def __init__(self, error: str) -> None:
        self.error = error
        self.record = None
        self.devices = None
