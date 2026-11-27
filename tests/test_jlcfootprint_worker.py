"""Tests for the fetch worker: buckets, the two queues, chunking, priority, breaker, stopping."""

import threading
from types import SimpleNamespace

from jlcfootprint.worker import (
    BATCH_SIZE,
    COALESCE_S,
    DOCUMENT_BURST,
    DOCUMENT_INTERVAL_S,
    FOOTPRINT,
    LOOKUP_BURST,
    LOOKUP_INTERVAL_S,
    MAX_CONSECUTIVE_FAILURES,
    SYMBOL,
    Buckets,
    FetchWorker,
    TokenBucket,
    estimate_seconds,
)

from .jlcfootprint_fetch_support import Clock


def _bucket(clock, **kwargs):
    kwargs.setdefault("jitter", lambda: 0.0)
    return TokenBucket(clock=clock, wait=clock.wait, **kwargs)


def test_token_bucket_allows_a_burst_then_one_per_interval():
    """Ten tokens are free; the eleventh waits for the next refill; refills accrue while idle."""
    clock = Clock()
    bucket = _bucket(clock)
    for _ in range(DOCUMENT_BURST):
        assert bucket.try_take()
    assert not bucket.try_take()
    assert bucket.seconds_until_token() == DOCUMENT_INTERVAL_S
    assert bucket.take()
    assert clock.waits == [DOCUMENT_INTERVAL_S]
    clock.now += 3 * DOCUMENT_INTERVAL_S
    assert bucket.seconds_until_token() == 0.0
    for _ in range(3):
        assert bucket.try_take()
    assert not bucket.try_take()


def test_token_bucket_jitter_and_stop():
    """Jitter stretches the spacing; a stop request during the wait gives no token."""
    clock = Clock()
    bucket = TokenBucket(
        clock=clock,
        wait=clock.wait,
        jitter=lambda: 0.5,
        burst=1,
        interval=2.0,
        jitter_s=1.0,
    )
    assert bucket.take()
    assert bucket.take()
    assert clock.waits == [2.5]
    stopping = TokenBucket(
        clock=clock, wait=lambda seconds: True, jitter=lambda: 0.0, burst=1
    )
    assert stopping.take()
    assert not stopping.take()


def test_default_paces_chunk_size_and_estimate():
    """Lookups: burst 2 then one per 2 s; documents: burst 10 then one per second; one chunk is 200."""
    clock = Clock()
    buckets = Buckets.default(clock=clock, jitter=lambda: 0.0)
    assert (buckets.lookups.burst, buckets.lookups.interval) == (LOOKUP_BURST, 2.0)
    assert (buckets.documents.burst, buckets.documents.interval) == (
        DOCUMENT_BURST,
        1.0,
    )
    assert BATCH_SIZE == 200
    assert estimate_seconds(0, 0) == 0.0
    # A queued code is its chunk plus up to two documents.
    assert estimate_seconds(1, 25) == LOOKUP_INTERVAL_S + 27 * DOCUMENT_INTERVAL_S
    assert estimate_seconds(201, 0) == 2 * LOOKUP_INTERVAL_S + 402 * DOCUMENT_INTERVAL_S


def _worker(lookup=None, fetch=None, on_lookup=None, on_document=None, on_tripped=None):
    clock = Clock()
    log = {"lookups": [], "documents": []}
    worker = FetchWorker(
        lookup or (lambda codes: SimpleNamespace(devices=list(codes), transient=False)),
        fetch
        or (lambda kind, uuid: SimpleNamespace(record=(kind, uuid), transient=False)),
        on_lookup or (lambda codes, result: log["lookups"].append(list(codes))),
        on_document
        or (lambda kind, uuid, result: log["documents"].append((kind, uuid))),
        on_tripped=on_tripped,
        clock=clock,
        jitter=lambda: 0.0,
    )
    worker.wait = clock.wait
    return worker, log, clock


def test_lookups_settle_then_go_out_in_chunks():
    """Codes wait half a second for company, then leave 200 at a time; duplicates cost nothing."""
    worker, log, clock = _worker()
    codes = [f"C{i}" for i in range(250)]
    assert worker.enqueue_lookups(codes + ["C1", ""]) == 250
    assert worker.enqueue_lookups(["C2"]) == 0
    assert worker.counts() == {"lookups": 250, "footprints": 0, "symbols": 0}
    assert (
        worker.estimate_seconds() == 2 * LOOKUP_INTERVAL_S + 500 * DOCUMENT_INTERVAL_S
    )
    assert worker.pending_lookups() == set(codes)
    assert worker.run_pending() == 2
    assert clock.waits == [COALESCE_S]
    assert [len(chunk) for chunk in log["lookups"]] == [200, 50]
    assert log["lookups"][0][0] == "C0" and log["lookups"][1][-1] == "C249"
    assert worker.pending_lookups() == set()


def test_documents_go_footprints_first_and_are_deduplicated():
    """Symbols queued before footprints still wait for them; a queued pair is never queued twice."""
    worker, log, _ = _worker()
    assert (
        worker.enqueue_documents(
            [
                (SYMBOL, "s1"),
                (FOOTPRINT, "f1"),
                (SYMBOL, "s1"),
                ("other", "x"),
                (FOOTPRINT, ""),
            ]
        )
        == 2
    )
    assert worker.pending_documents() == {(SYMBOL, "s1"), (FOOTPRINT, "f1")}
    assert worker.counts() == {"lookups": 0, "footprints": 1, "symbols": 1}
    assert worker.run_pending() == 2
    assert log["documents"] == [(FOOTPRINT, "f1"), (SYMBOL, "s1")]
    assert worker.pending_documents() == set()


def test_lookups_go_before_documents_once_settled():
    """With both queues ready the lookup goes first; during the settling window a document goes."""
    worker, log, clock = _worker()
    order = []
    worker.on_lookup = lambda codes, result: order.append("lookup")
    worker.on_document = lambda kind, uuid, result: order.append(kind)
    worker.enqueue_documents([(FOOTPRINT, "f1")])
    worker.enqueue_lookups(["C1"])
    clock.now += COALESCE_S
    worker.enqueue_documents([(FOOTPRINT, "f2")])
    assert worker.run_pending() == 3
    assert order == ["lookup", FOOTPRINT, FOOTPRINT]
    assert clock.waits == []

    worker, log, clock = _worker()
    worker.on_lookup = lambda codes, result: order.append("lookup")
    worker.on_document = lambda kind, uuid, result: order.append(kind)
    order.clear()
    worker.enqueue_lookups(["C1"])
    worker.enqueue_documents([(SYMBOL, "s1")])
    assert worker.run_pending() == 2
    assert order == [SYMBOL, "lookup"]
    assert clock.waits == [COALESCE_S]


def test_the_worker_waits_for_the_earliest_refill():
    """Past the burst, the next document waits one interval; an idle worker returns at once."""
    worker, log, clock = _worker()
    worker.enqueue_documents([(FOOTPRINT, f"f{i}") for i in range(DOCUMENT_BURST + 1)])
    assert worker.run_pending() == DOCUMENT_BURST + 1
    assert clock.waits == [DOCUMENT_INTERVAL_S]
    assert worker.run_pending() == 0


def test_breaker_trips_after_three_transient_failures_across_both_queues():
    """Three failures in a row trip it whichever queue they come from; later work stays queued."""
    messages = []
    worker, log, clock = _worker(
        lookup=lambda codes: SimpleNamespace(devices=None, transient=True),
        fetch=lambda kind, uuid: SimpleNamespace(record=uuid, transient=uuid != "ok"),
        on_tripped=messages.append,
    )
    worker.enqueue_lookups(["C1"])
    clock.now += COALESCE_S
    worker.enqueue_documents([(FOOTPRINT, "ok"), (FOOTPRINT, "f1"), (SYMBOL, "s1")])
    worker.run_pending()
    assert [len(chunk) for chunk in log["lookups"]] == [1]
    assert log["documents"] == [(FOOTPRINT, "ok"), (FOOTPRINT, "f1"), (SYMBOL, "s1")]
    assert not worker.tripped, "the successful footprint reset the count"
    assert worker.consecutive_failures == 2
    worker.enqueue_documents([(SYMBOL, "s2"), (SYMBOL, "s3")])
    worker.run_pending()
    assert worker.tripped, "s2 was the third failure in a row"
    assert worker.pending_documents() == {(SYMBOL, "s3")}
    assert len(messages) == 1
    assert "3 fetches from EasyEDA failed in a row" in messages[0]
    assert "the 1 part lookup(s) and drawing(s) still queued" in messages[0]
    assert worker.consecutive_failures == MAX_CONSECUTIVE_FAILURES
    assert worker.enqueue_lookups(["C7"]) == 1
    assert worker.run_pending() == 0


def test_final_failures_trip_the_breaker_and_a_missing_part_does_not():
    """A run of final errors stops the queue too; "EasyEDA has nothing" resets the count."""
    messages = []
    worker, _, _ = _worker(
        fetch=lambda kind, uuid: SimpleNamespace(
            record=uuid, transient=False, failed=uuid != "none"
        ),
        on_tripped=messages.append,
    )
    worker.enqueue_documents(
        [(FOOTPRINT, "f1"), (FOOTPRINT, "none"), (FOOTPRINT, "f2"), (FOOTPRINT, "f3")]
    )
    worker.run_pending()
    assert not worker.tripped, "the part EasyEDA has nothing for reset the count"
    assert worker.consecutive_failures == 2
    worker.enqueue_documents([(SYMBOL, "s1"), (SYMBOL, "s2")])
    worker.run_pending()
    assert worker.tripped
    assert worker.pending_documents() == {(SYMBOL, "s2")}
    assert len(messages) == 1


def test_a_result_arriving_while_stopping_is_dropped():
    """A fetch cut short by a stop says nothing about EasyEDA: no handler, no count, no trip."""
    messages = []
    held = {}

    def fetch(kind, uuid):
        if uuid == "f3":
            held["worker"].stop_event.set()
        return SimpleNamespace(record=uuid, transient=True)

    worker, log, _ = _worker(fetch=fetch, on_tripped=messages.append)
    held["worker"] = worker
    worker.enqueue_documents([(FOOTPRINT, "f1"), (FOOTPRINT, "f2"), (FOOTPRINT, "f3")])
    worker.run_pending()
    assert log["documents"] == [(FOOTPRINT, "f1"), (FOOTPRINT, "f2")]
    assert worker.consecutive_failures == 2
    assert not worker.tripped
    assert messages == []


def test_exceptions_are_isolated():
    """A fetch that raises counts as a transient failure; a callback that raises is swallowed."""
    calls = []

    def fetch(kind, uuid):
        calls.append(uuid)
        if uuid == "f1":
            raise RuntimeError("boom")
        return SimpleNamespace(record=uuid, transient=False)

    def on_document(kind, uuid, result):
        if uuid == "f2":
            raise RuntimeError("handler")

    worker, _, _ = _worker(fetch=fetch, on_document=on_document)
    worker.enqueue_documents([(FOOTPRINT, "f1"), (FOOTPRINT, "f2"), (FOOTPRINT, "f3")])
    assert worker.run_pending() == 3
    assert calls == ["f1", "f2", "f3"]
    assert worker.consecutive_failures == 0
    worker, _, _ = _worker(lookup=lambda codes: 1 / 0)
    worker.enqueue_lookups(["C1"])
    assert worker.run_pending() == 1
    assert worker.consecutive_failures == 1


def test_acquire_pays_once_for_the_first_attempt_and_per_retry_after_that():
    """The scheduler's token covers the first request; each retry inside the job takes another."""
    seen = []

    def fetch(kind, uuid):
        seen.append((worker.acquire(), worker.buckets.documents.tokens))
        seen.append((worker.acquire(), worker.buckets.documents.tokens))
        return SimpleNamespace(record=uuid, transient=False)

    worker, _, _ = _worker(fetch=fetch)
    worker.enqueue_documents([(FOOTPRINT, "f1")])
    worker.run_pending()
    assert seen == [(True, DOCUMENT_BURST - 1), (True, DOCUMENT_BURST - 2)]
    assert worker.acquire() and worker.buckets.documents.tokens == DOCUMENT_BURST - 3


def test_a_job_leaves_the_in_flight_slot_before_its_handler_runs():
    """A handler that queues the key it is handling is heard, not dropped as in flight."""
    worker, log, _ = _worker()
    seen = []

    def on_document(kind, uuid, result):
        log["documents"].append((kind, uuid))
        if len(log["documents"]) == 1:
            seen.append((worker.in_flight, worker.active()))
            seen.append(worker.enqueue_documents([(kind, uuid)]))

    worker.on_document = on_document
    worker.enqueue_documents([(FOOTPRINT, "f1")])
    assert worker.run_pending() == 2
    assert seen == [(None, (0, 0.0)), 1]
    assert log["documents"] == [(FOOTPRINT, "f1"), (FOOTPRINT, "f1")]


def test_active_reports_the_job_in_flight_and_its_backoff():
    """While a fetch runs one job is active; a backoff it sleeps through is time still to wait."""
    seen = []

    def fetch(kind, uuid):
        seen.append(worker.active())
        worker.wait_for(30.0)
        seen.append(worker.active())
        return SimpleNamespace(record=uuid, transient=False)

    worker, _, clock = _worker(fetch=fetch)
    worker.wait = lambda seconds: (seen.append(worker.active()), clock.wait(seconds))[1]
    assert worker.active() == (0, 0.0)
    worker.enqueue_documents([(FOOTPRINT, "f1")])
    assert worker.run_pending() == 1
    assert seen == [(1, 0.0), (1, 30.0), (1, 0.0)]
    assert clock.waits == [30.0]
    assert worker.active() == (0, 0.0)


def test_thread_processes_and_stops():
    """The daemon thread drains the queue and joins on stop; stop interrupts a wait."""
    processed = threading.Event()
    worker = FetchWorker(
        lambda codes: SimpleNamespace(devices=[], transient=False),
        lambda kind, uuid: SimpleNamespace(record=uuid, transient=False),
        lambda codes, result: None,
        lambda kind, uuid, result: processed.set(),
        jitter=lambda: 0.0,
    )
    worker.start()
    worker.enqueue_documents([(FOOTPRINT, "f1")])
    assert processed.wait(5.0)
    assert not worker.wait_for(0.01)
    worker.stop()
    assert not worker.is_running()
    assert worker.wait_for(0.0)
    assert not worker.acquire()


def test_shared_buckets_wait_on_the_asking_worker():
    """Two workers share one budget; a token wait goes through the asking worker's own wait."""
    clock = Clock()
    buckets = Buckets.default(clock=clock, jitter=lambda: 0.0)
    first = FetchWorker(
        lambda c: None,
        lambda k, u: None,
        lambda c, r: None,
        lambda k, u, r: None,
        buckets=buckets,
    )
    second = FetchWorker(
        lambda c: None,
        lambda k, u: None,
        lambda c, r: None,
        lambda k, u, r: None,
        buckets=buckets,
    )
    assert first.buckets is second.buckets
    for _ in range(DOCUMENT_BURST):
        assert buckets.documents.try_take()
    asked = []

    def stopping(name):
        # A wait that reports a stop request, as a stopping worker's does.
        return lambda seconds: (asked.append(name), True)[1]

    buckets.documents.wait = stopping("bucket")
    first.wait = stopping("first")
    second.wait = stopping("second")
    assert not second.acquire()
    assert not first.acquire()
    assert asked == ["second", "first"]
