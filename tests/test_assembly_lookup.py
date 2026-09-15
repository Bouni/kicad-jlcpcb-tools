"""Exercise shared background lookup ownership without threads or a GUI loop."""

from collections.abc import Callable, Iterator
from functools import partial
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from .wx_harness import load_siblings, module


def _drain(queue: list[Callable[[], None]]) -> None:
    """Deliver queued work in order, including work queued by callbacks."""
    while queue:
        queue.pop(0)()


@pytest.fixture
def lookup(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Separate worker execution from main-thread delivery for controlled races."""
    jobs, delivery, results, errors = [], [], [], []

    def call_after(callback: Callable[..., None], *args: Any) -> None:
        delivery.append(partial(callback, *args))

    def thread(
        *, target: Callable[..., None], args: tuple = (), daemon: bool = False
    ) -> Mock:
        return Mock(start=lambda: jobs.append(partial(target, *args)))

    with load_siblings(
        "_assembly_lookup",
        ["enrichment.worker"],
        {"wx": module("wx", CallAfter=call_after)},
    ) as loaded:
        worker = loaded["enrichment.worker"]
        provider = Mock()
        provider.fetch_iter.side_effect = lambda codes: iter(
            (code, {"component_product_type": 0}) for code in sorted(codes)
        )
        monkeypatch.setattr(worker, "Thread", Mock(side_effect=thread))
        monkeypatch.setattr(
            worker, "LCSCAssemblyMetadataProvider", Mock(return_value=provider)
        )
        finished = Mock()
        service = worker.AssemblyMetadataLookup(
            lambda code, data: results.append((code, data)), finished, errors.append
        )
        yield SimpleNamespace(
            service=service,
            worker=worker,
            provider=provider,
            jobs=jobs,
            delivery=delivery,
            results=results,
            errors=errors,
            finished=finished,
        )


def test_overlapping_batches_and_duplicate_demand_complete_independently(
    lookup: Any,
) -> None:
    """A new disjoint batch must neither invalidate nor duplicate existing requests."""
    service = lookup.service
    assert service.request({"C1", "C2"}) == {"C1", "C2"}
    assert service.request({"C2", "C3"}) == {"C3"}
    assert service.request({"C1", "C3"}, retry=True) == set()
    lookup.jobs.pop()()
    assert not lookup.results  # Worker results must wait for main-thread delivery.
    _drain(lookup.delivery)
    assert service.pending == {"C1", "C2"}
    _drain(lookup.jobs)
    _drain(lookup.delivery)
    assert sorted(code for code, _ in lookup.results) == ["C1", "C2", "C3"]
    assert not service.pending and not service.errors
    assert lookup.finished.call_count == 2


@pytest.mark.parametrize("failure", ["construct", "iterate", "start"])
def test_failures_release_pending_and_allow_explicit_retry(
    lookup: Any, failure: str
) -> None:
    """Setup and streaming failures release owned requests and keep earlier results."""

    def partial_failure(codes: set[str]) -> Iterator[tuple[str, dict]]:
        yield "C1", {"component_product_type": 0}
        raise RuntimeError("lookup failed")

    if failure == "construct":
        lookup.worker.LCSCAssemblyMetadataProvider.side_effect = RuntimeError(
            "lookup failed"
        )
    elif failure == "iterate":
        lookup.provider.fetch_iter.side_effect = partial_failure
    else:
        lookup.worker.Thread.side_effect = None
        lookup.worker.Thread.return_value.start.side_effect = RuntimeError(
            "lookup failed"
        )
    lookup.service.request({"C1", "C2"})
    _drain(lookup.jobs)
    _drain(lookup.delivery)
    assert not lookup.service.pending
    assert lookup.finished.call_count == 1
    assert lookup.errors and "lookup failed" in lookup.errors[0]
    assert [code for code, _ in lookup.results] == (
        ["C1"] if failure == "iterate" else []
    )
    assert lookup.service.errors == ({"C2"} if failure == "iterate" else {"C1", "C2"})
    assert lookup.service.request({"C1", "C2"}) == set()
    assert lookup.service.request({"C2"}, retry=True) == {"C2"}


@pytest.mark.parametrize("results", [(), (("C1", {"component_product_type": None}),)])
def test_missing_classification_is_retried_only_explicitly(
    lookup: Any, results: tuple
) -> None:
    """Automatic redraws do not keep requesting a completed classification miss."""
    lookup.provider.fetch_iter.return_value = iter(results)
    lookup.provider.fetch_iter.side_effect = None
    lookup.service.request({"C1"})
    _drain(lookup.jobs)
    _drain(lookup.delivery)
    assert not lookup.service.pending
    assert lookup.service.request({"C1"}) == set()
    assert lookup.service.request({"C1"}, retry=True) == {"C1"}


@pytest.mark.parametrize("reset", ["invalidate", "close"])
def test_old_deliveries_cannot_update_or_finish_a_new_lifetime(
    lookup: Any, reset: str
) -> None:
    """Queued results and completion from an obsolete owner have no side effects."""
    service = lookup.service
    service.request({"C1"})
    _drain(lookup.jobs)
    getattr(service, reset)()
    assert not service.pending and not service.errors
    assert service.request({"C1"}) == ({"C1"} if reset == "invalidate" else set())
    _drain(lookup.delivery)
    assert not lookup.results and not lookup.finished.called
    assert service.pending == ({"C1"} if reset == "invalidate" else set())
    _drain(lookup.jobs)
    _drain(lookup.delivery)
    assert len(lookup.results) == (1 if reset == "invalidate" else 0)
    assert not service.pending


def test_old_batch_completion_preserves_a_new_request_for_its_finished_code(
    lookup: Any,
) -> None:
    """Retry between one result and batch completion retains its own pending slot."""
    service = lookup.service
    service.request({"C1"})
    _drain(lookup.jobs)
    lookup.delivery.pop(0)()
    assert [code for code, _ in lookup.results] == ["C1"]
    assert service.request({"C1"}, retry=True) == {"C1"}
    _drain(lookup.delivery)
    assert service.pending == {"C1"}
    _drain(lookup.jobs)
    _drain(lookup.delivery)
    assert [code for code, _ in lookup.results] == ["C1", "C1"]
    assert not service.pending
    assert lookup.finished.call_count == 2


def test_result_application_failure_does_not_strand_batch_completion(
    lookup: Any,
) -> None:
    """One consumer failure still permits subsequent results and a final refresh."""

    def accept(code: str, data: dict) -> None:
        if code == "C1":
            raise RuntimeError("application failed")
        lookup.results.append((code, data))

    service = lookup.worker.AssemblyMetadataLookup(
        accept, lookup.finished, lookup.errors.append
    )
    service.request({"C1", "C2"})
    _drain(lookup.jobs)
    _drain(lookup.delivery)
    assert [code for code, _ in lookup.results] == ["C2"]
    assert not service.pending
    assert service.errors == {"C1"}
    assert lookup.finished.call_count == 1
    assert lookup.errors and "application failed" in lookup.errors[0]
