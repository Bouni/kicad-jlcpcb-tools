"""Exercise catalog refresh state and GUI delivery without making HTTP requests."""

from __future__ import annotations

from collections.abc import Callable, Iterator
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


class _Clock:
    """Advance monotonic time explicitly, including across dialog lifetimes."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Later:
    """Retain running/stopped timer state and dispatch only at its due time."""

    def __init__(
        self, gui: _Gui, milliseconds: int, callback: Callable[..., None], *args: object
    ) -> None:
        self.gui = gui
        self.due = gui.clock() + milliseconds / 1000
        self.callback = callback
        self.args = args
        self.running = True

    def Stop(self) -> None:
        self.running = False

    def IsRunning(self) -> bool:
        return self.running


class _Gui:
    """Queue CallAfter and CallLater work rather than calling GUI callbacks inline."""

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.pending: list[tuple[Callable[..., None], tuple[object, ...]]] = []
        self.timers: list[_Later] = []

    def after(self, callback: Callable[..., None], *args: object) -> None:
        self.pending.append((callback, args))

    def later(
        self, milliseconds: int, callback: Callable[..., None], *args: object
    ) -> _Later:
        timer = _Later(self, milliseconds, callback, *args)
        self.timers.append(timer)
        return timer

    def drain(self) -> None:
        for _ in range(50):
            due = [
                timer
                for timer in self.timers
                if timer.running and timer.due <= self.clock()
            ]
            for timer in due:
                timer.running = False
                self.pending.append((timer.callback, timer.args))
            if not self.pending:
                return
            callback, args = self.pending.pop(0)
            callback(*args)
        raise AssertionError("GUI callbacks did not settle")

    def advance(self, seconds: float) -> None:
        self.clock.now += seconds
        self.drain()


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """Import real cache/model/controller code with bounded GUI and worker scheduling."""
    package_name = "_impedance_catalog_controller_tests"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "impedance")]
    monkeypatch.setitem(sys.modules, package_name, package)
    clock = _Clock()
    gui = _Gui(clock)
    wx = ModuleType("wx")
    wx.CallAfter = gui.after
    wx.CallLater = gui.later
    monkeypatch.setitem(sys.modules, "wx", wx)
    transport = importlib.import_module(f"{package_name}.jlcpcb_stackups")
    controller = importlib.import_module(f"{package_name}.catalog_controller")
    model = importlib.import_module(f"{package_name}.stackup_model")
    cache = importlib.import_module(f"{package_name}.catalog_cache")
    workers: list[Any] = []

    class Worker:
        def __init__(
            self,
            *,
            target: Callable[..., None],
            args: tuple[object, ...],
            **_options: object,
        ) -> None:
            self.target = target
            self.args = args
            self.started = False
            workers.append(self)

        def start(self) -> None:
            self.started = True

        def run(self) -> None:
            assert self.started
            self.target(*self.args)
            gui.drain()

    monkeypatch.setattr(controller, "Thread", Worker)
    refresh = importlib.import_module(f"{package_name}.catalog_refresh")
    gate = refresh.CatalogRefreshGate(clock=clock)
    try:
        yield SimpleNamespace(
            controller=controller,
            transport=transport,
            model=model,
            cache=cache,
            clock=clock,
            gui=gui,
            workers=workers,
            gate=gate,
        )
    finally:
        for name in tuple(sys.modules):
            if name.startswith(package_name + "."):
                del sys.modules[name]


def _stackup(api: SimpleNamespace, identifier: str = "saved") -> Any:
    return api.model.Stackup(
        identifier, "Named 4-layer construction", 4, "1.6", "1", "0.5"
    )


def _controller(api: SimpleNamespace, **options: object) -> Any:
    options["refresh_gate"] = api.gate
    return api.controller.CatalogController(4, **options)


def test_transient_refresh_keeps_legacy_cache_usable_without_red_error(
    api: SimpleNamespace,
) -> None:
    """Retain old cache rows and freshness without promoting a transient outage."""
    saved = _stackup(api)
    cache = api.cache.CatalogCache((saved,), "")
    saves: list[object] = []

    def unavailable(*_args: object, **_kwargs: object) -> tuple[Any, ...]:
        raise api.transport.CatalogUnavailable("Temporary network interruption")

    controller = _controller(
        api,
        cache=cache,
        fetch_catalog=unavailable,
        save_cache=lambda *_args: saves.append(_args),
    )
    controller.ensure_started()
    api.workers[0].run()
    assert controller.state.cache == cache
    assert not controller.state.loading
    assert not controller.state.error
    assert "cached" in controller.state.message.lower()
    assert "Catalog check failed" not in controller.state.message
    assert not saves


def test_temporary_outage_retries_in_same_open_dialog_after_cooldown(
    api: SimpleNamespace,
) -> None:
    """Recover automatically while the original dialog remains open."""
    outcomes: list[object] = [
        api.transport.CatalogUnavailable("Unavailable"),
        (_stackup(api, "new"),),
    ]
    saves: list[object] = []

    def fetch(*_args: object, **_kwargs: object) -> tuple[Any, ...]:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    controller = _controller(
        api,
        cache=api.cache.CatalogCache((_stackup(api),), "2001-01-01T00:00:00.000000Z"),
        fetch_catalog=fetch,
        save_cache=lambda *_args: saves.append(_args),
    )
    controller.ensure_started()
    api.workers[0].run()
    api.gui.advance(299)
    assert len(api.workers) == 1
    api.gui.advance(1)
    assert len(api.workers) == 2
    api.workers[1].run()
    assert controller.state.cache.stackups[0].stackup_id == "new"
    assert controller.state.cache.checked_at_utc
    assert not controller.state.error
    assert len(saves) == 1


def test_temporary_outage_without_fallback_has_actionable_error(
    api: SimpleNamespace,
) -> None:
    """Explain an unusable catalog and its automatic recovery attempt."""

    def unavailable(*_args: object, **_kwargs: object) -> tuple[Any, ...]:
        raise api.transport.CatalogUnavailable("Unavailable")

    controller = _controller(api, fetch_catalog=unavailable)
    controller.ensure_started()
    api.workers[0].run()
    assert controller.state.error
    assert "No compatible" in controller.state.message
    assert "automatically" in controller.state.message


def test_saved_compatible_construction_is_a_fallback_when_cache_is_empty(
    api: SimpleNamespace,
) -> None:
    """Allow a frozen compatible selection to remain useful without cached rows."""

    def unavailable(*_args: object, **_kwargs: object) -> tuple[Any, ...]:
        raise api.transport.CatalogUnavailable("Temporary service outage")

    controller = _controller(
        api, fetch_catalog=unavailable, usable_current=lambda: True
    )
    controller.ensure_started()
    api.workers[0].run()
    assert not controller.state.error
    assert "saved construction" in controller.state.message.lower()
    assert controller.state.cache == api.cache.CatalogCache()


def test_fallback_is_evaluated_at_delivery_not_controller_construction(
    api: SimpleNamespace,
) -> None:
    """Use current board intent when deciding whether an outage is actionable."""
    usable = [True]

    def unavailable(*_args: object, **_kwargs: object) -> tuple[Any, ...]:
        raise api.transport.CatalogUnavailable("Temporary service outage")

    controller = _controller(
        api, fetch_catalog=unavailable, usable_current=lambda: usable[0]
    )
    controller.ensure_started()
    usable[0] = False
    api.workers[0].run()
    assert controller.state.error


def test_permanent_catalog_problem_is_red_and_does_not_retry(
    api: SimpleNamespace,
) -> None:
    """Surface schema or configuration failures without a background retry loop."""

    def invalid(*_args: object, **_kwargs: object) -> tuple[Any, ...]:
        raise api.transport.CatalogError("The provider schema changed")

    controller = _controller(
        api, cache=api.cache.CatalogCache((_stackup(api),)), fetch_catalog=invalid
    )
    controller.ensure_started()
    api.workers[0].run()
    assert controller.state.error
    assert "schema changed" in controller.state.message
    api.gui.advance(1000)
    controller.ensure_started()
    assert len(api.workers) == 1
    assert not any(timer.running for timer in api.gui.timers)


def test_reopen_shares_cooldown_and_preserves_previous_timestamp(
    api: SimpleNamespace,
) -> None:
    """Respect a failed check across closing and reopening the dialog."""
    cache = api.cache.CatalogCache((_stackup(api),), "2001-01-01T00:00:00.000000Z")

    def unavailable(*_args: object, **_kwargs: object) -> tuple[Any, ...]:
        raise api.transport.CatalogUnavailable("Temporary service outage")

    first = _controller(api, cache=cache, fetch_catalog=unavailable)
    first.ensure_started()
    api.workers[0].run()
    first.close()
    second = _controller(api, cache=cache, fetch_catalog=unavailable)
    second.ensure_started()
    assert len(api.workers) == 1
    assert not second.state.error
    assert second.state.cache.checked_at_utc == cache.checked_at_utc
    api.gui.advance(300)
    assert len(api.workers) == 2


def test_repeated_opening_or_picker_joins_one_inflight_worker(
    api: SimpleNamespace,
) -> None:
    """Idempotent startup and concurrent owners cannot duplicate active traffic."""
    cache = api.cache.CatalogCache((_stackup(api),))
    first = _controller(
        api,
        cache=cache,
        fetch_catalog=lambda *_args, **_kwargs: (_stackup(api, "fresh"),),
    )
    second = _controller(
        api,
        cache=cache,
        fetch_catalog=lambda *_args, **_kwargs: (_stackup(api, "fresh"),),
    )
    first.ensure_started()
    first.ensure_started()
    second.ensure_started()
    second.ensure_started()
    assert len(api.workers) == 1
    assert second.state.loading
    assert not second.state.error
    api.workers[0].run()
    assert not first.state.loading
    second.close()
    first.close()


def test_retry_after_longer_than_local_cooldown_is_respected_across_reopen(
    api: SimpleNamespace,
) -> None:
    """Honor a longer server delay across dialog lifetimes."""

    def unavailable(*_args: object, **_kwargs: object) -> tuple[Any, ...]:
        raise api.transport.CatalogUnavailable("Rate limited", retry_after=900)

    first = _controller(
        api, cache=api.cache.CatalogCache((_stackup(api),)), fetch_catalog=unavailable
    )
    first.ensure_started()
    api.workers[0].run()
    first.close()
    second = _controller(api, cache=first.state.cache, fetch_catalog=unavailable)
    second.ensure_started()
    api.gui.advance(899)
    assert len(api.workers) == 1
    api.gui.advance(1)
    assert len(api.workers) == 2


def test_joined_dialog_uses_newest_success_when_another_owner_finishes_first(
    api: SimpleNamespace,
) -> None:
    """A delayed GUI observer must not repeat a newer already-completed check."""
    cache = api.cache.CatalogCache((_stackup(api),))
    first = _controller(
        api,
        cache=cache,
        fetch_catalog=lambda *_args, **_kwargs: (_stackup(api, "first"),),
    )
    waiting = _controller(api, cache=cache)
    first.ensure_started()
    waiting.ensure_started()
    api.workers[0].run()
    newest = _controller(
        api,
        cache=cache,
        fetch_catalog=lambda *_args, **_kwargs: (_stackup(api, "newest"),),
    )
    newest.ensure_started()
    api.workers[1].run()
    api.gui.advance(1)
    assert len(api.workers) == 2
    assert waiting.state.cache.stackups[0].stackup_id == "newest"
    assert not waiting.state.loading and not waiting.state.error
    for controller in (first, waiting, newest):
        controller.close()


def test_close_stops_retry_and_queued_callback_cannot_restart(
    api: SimpleNamespace,
) -> None:
    """Invalidated weak callbacks never resurrect closed controller work."""

    def unavailable(*_args: object, **_kwargs: object) -> tuple[Any, ...]:
        raise api.transport.CatalogUnavailable("Unavailable")

    controller = _controller(
        api, cache=api.cache.CatalogCache((_stackup(api),)), fetch_catalog=unavailable
    )
    controller.ensure_started()
    api.workers[0].run()
    timer = next(timer for timer in api.gui.timers if timer.running)
    controller.close()
    assert not timer.running
    timer.callback(*timer.args)
    api.gui.advance(300)
    assert len(api.workers) == 1


def test_closed_worker_releases_reservation_only_when_request_really_finishes(
    api: SimpleNamespace,
) -> None:
    """Cancellation cannot let a new request overlap an old blocking request."""
    saves: list[object] = []
    first = _controller(
        api,
        fetch_catalog=lambda *_args, **_kwargs: (_stackup(api),),
        save_cache=lambda *_args: saves.append(_args),
    )
    first.ensure_started()
    first.close()
    second = _controller(api, fetch_catalog=lambda *_args, **_kwargs: (_stackup(api),))
    second.ensure_started()
    assert len(api.workers) == 1
    api.workers[0].run()
    assert not saves
    api.gui.advance(1)
    assert len(api.workers) == 1
    assert second.state.cache.stackups == (_stackup(api),)
    assert not second.state.error


def test_closed_transient_worker_still_applies_cooldown_before_reopen(
    api: SimpleNamespace,
) -> None:
    """An outage finishing after closure still prevents immediate repeat traffic."""

    def unavailable(*_args: object, **_kwargs: object) -> tuple[Any, ...]:
        raise api.transport.CatalogUnavailable("Unavailable")

    first = _controller(api, fetch_catalog=unavailable)
    first.ensure_started()
    first.close()
    api.workers[0].run()
    second = _controller(
        api, cache=api.cache.CatalogCache((_stackup(api),)), fetch_catalog=unavailable
    )
    second.ensure_started()
    assert len(api.workers) == 1
    assert not second.state.error
    api.gui.advance(300)
    assert len(api.workers) == 2


def test_cancelled_request_does_not_save_publish_error_or_apply_cooldown(
    api: SimpleNamespace,
) -> None:
    """Cancellation is silent and does not imply a provider failure."""
    updates: list[object] = []
    saves: list[object] = []

    def cancelled(*_args: object, **_kwargs: object) -> tuple[Any, ...]:
        raise api.transport.CatalogCancelled("Cancelled")

    first = _controller(
        api, fetch_catalog=cancelled, save_cache=lambda *_args: saves.append(_args)
    )
    first.subscribe(updates.append)
    first.ensure_started()
    first.close()
    published = len(updates)
    api.workers[0].run()
    assert len(updates) == published
    assert not saves
    second = _controller(api, fetch_catalog=lambda *_args, **_kwargs: (_stackup(api),))
    second.ensure_started()
    assert len(api.workers) == 2


def test_queued_delivery_does_not_save_for_closed_or_replaced_owner(
    api: SimpleNamespace,
) -> None:
    """Guard persistence as well as GUI delivery after native owner replacement."""
    current = [True]
    saves: list[object] = []
    updates: list[object] = []
    controller = _controller(
        api,
        fetch_catalog=lambda *_args, **_kwargs: (_stackup(api),),
        save_cache=lambda *_args: saves.append(_args),
        owner_current=lambda: current[0],
    )
    controller.subscribe(updates.append)
    controller.ensure_started()
    api.workers[0].target(*api.workers[0].args)
    current[0] = False
    count = len(updates)
    api.gui.drain()
    assert not saves
    assert len(updates) == count


def test_fresh_cache_does_not_start_request_or_retry_timer(
    api: SimpleNamespace,
) -> None:
    """Reuse successfully checked compatible rows for the existing freshness window."""
    cache = api.cache.CatalogCache((_stackup(api),), api.cache.catalog_checked_at())
    controller = _controller(api, cache=cache)
    controller.ensure_started()
    controller.ensure_started()
    api.gui.advance(1000)
    assert not api.workers
    assert not any(timer.running for timer in api.gui.timers)
    assert not controller.state.error


def test_save_failure_is_exceptional_without_a_retry_loop(api: SimpleNamespace) -> None:
    """Report local persistence failures while keeping successful choices in memory."""

    def broken_save(*_args: object) -> None:
        raise OSError("Disk is unavailable")

    controller = _controller(
        api,
        fetch_catalog=lambda *_args, **_kwargs: (_stackup(api),),
        save_cache=broken_save,
    )
    controller.ensure_started()
    api.workers[0].run()
    assert controller.state.error
    assert "could not be saved" in controller.state.message
    assert controller.state.cache.checked_at_utc
    api.gui.advance(300)
    assert len(api.workers) == 1


def test_invalid_catalog_response_is_exceptional_and_never_replaces_cache(
    api: SimpleNamespace,
) -> None:
    """Reject malformed responses without losing previously accepted choices."""
    cache = api.cache.CatalogCache((_stackup(api),))
    controller = _controller(
        api,
        cache=cache,
        fetch_catalog=lambda *_args, **_kwargs: (_stackup(api), _stackup(api)),
    )
    controller.ensure_started()
    api.workers[0].run()
    assert controller.state.error
    assert controller.state.cache == cache
    assert "duplicate" in controller.state.message
    api.gui.advance(300)
    assert len(api.workers) == 1


def test_successful_empty_catalog_is_only_red_without_usable_saved_construction(
    api: SimpleNamespace,
) -> None:
    """Make a valid empty result actionable only if no saved fallback exists."""
    controller = _controller(
        api, fetch_catalog=lambda *_args, **_kwargs: (), usable_current=lambda: False
    )
    controller.ensure_started()
    api.workers[0].run()
    assert controller.state.error
    assert "no compatible" in controller.state.message.lower()
    assert controller.state.cache.checked_at_utc
    assert not any(timer.running for timer in api.gui.timers)


@pytest.mark.parametrize("during_construction", [False, True])
def test_worker_start_failure_releases_gate_and_is_exceptional(
    api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, during_construction: bool
) -> None:
    """A failed local worker must not strand a reservation or loop HTTP traffic."""

    class BrokenWorker:
        def __init__(self, **_options: object) -> None:
            if during_construction:
                raise RuntimeError("The worker could not be created")

        def start(self) -> None:
            raise RuntimeError("The worker could not start")

    monkeypatch.setattr(api.controller, "Thread", BrokenWorker)
    controller = _controller(api)
    controller.ensure_started()
    assert controller.state.error
    assert not controller.state.loading
    assert not any(timer.running for timer in api.gui.timers)
    reservation = api.gate.reserve(4)
    assert reservation.token is not None
    api.gate.finish(4, reservation.token)


def test_owner_closes_during_loading_publication_before_worker_start(
    api: SimpleNamespace,
) -> None:
    """Reentrant native destruction before HTTP startup releases its token."""
    controller = _controller(api)

    def observe(state: Any) -> None:
        if state.loading:
            controller.close()

    controller.subscribe(observe)
    controller.ensure_started()
    assert not api.workers
    reservation = api.gate.reserve(4)
    assert reservation.token is not None
    api.gate.finish(4, reservation.token)


def test_stale_generation_cannot_replace_cache_or_save(api: SimpleNamespace) -> None:
    """A result from a replaced request generation cannot publish or persist."""
    saves: list[object] = []
    cache = api.cache.CatalogCache((_stackup(api),))
    controller = _controller(
        api, cache=cache, save_cache=lambda *_args: saves.append(_args)
    )
    controller._finish(-1, (_stackup(api, "stale"),), None)
    assert controller.state.cache == cache
    assert not saves


def test_retry_timer_does_not_retain_controller_owner(api: SimpleNamespace) -> None:
    """An abandoned closed dialog can be collected while its callback is queued."""
    import gc
    from weakref import ref

    def unavailable(*_args: object, **_kwargs: object) -> tuple[Any, ...]:
        raise api.transport.CatalogUnavailable("Unavailable")

    controller = _controller(
        api, cache=api.cache.CatalogCache((_stackup(api),)), fetch_catalog=unavailable
    )
    controller.ensure_started()
    api.workers[0].run()
    weak = ref(controller)
    controller.close()
    del controller
    gc.collect()
    assert weak() is None
    api.gui.advance(300)
    assert len(api.workers) == 1


def test_long_provider_cooldown_uses_bounded_gui_timer(api: SimpleNamespace) -> None:
    """Huge retry hints never overflow wx's timer delay or cause early HTTP."""

    def unavailable(*_args: object, **_kwargs: object) -> tuple[Any, ...]:
        raise api.transport.CatalogUnavailable("Rate limited", retry_after=1e308)

    controller = _controller(
        api, cache=api.cache.CatalogCache((_stackup(api),)), fetch_catalog=unavailable
    )
    controller.ensure_started()
    api.workers[0].run()
    timer = next(timer for timer in api.gui.timers if timer.running)
    assert timer.due <= 24 * 60 * 60
    api.gui.advance(24 * 60 * 60)
    assert len(api.workers) == 1
    assert any(timer.running for timer in api.gui.timers)


def test_cache_read_failure_remains_exceptional_even_during_transient_outage(
    api: SimpleNamespace,
) -> None:
    """Failed persisted metadata reads remain visible beside useful in-memory rows."""

    def unreadable(_count: int) -> Any:
        raise ValueError("Invalid saved catalog")

    def unavailable(*_args: object, **_kwargs: object) -> tuple[Any, ...]:
        raise api.transport.CatalogUnavailable("Unavailable")

    cache = api.cache.CatalogCache((_stackup(api),))
    controller = _controller(
        api, cache=cache, load_cache=unreadable, fetch_catalog=unavailable
    )
    controller.ensure_started()
    api.workers[0].run()
    assert controller.state.error
    assert "saved catalog could not be read" in controller.state.message
    assert controller.state.cache == cache


def test_waiting_owner_uses_completed_catalog_instead_of_duplicate_request(
    api: SimpleNamespace,
) -> None:
    """A concurrent observer receives its joined check once it really completes."""
    cache = api.cache.CatalogCache((_stackup(api),))
    fresh = (_stackup(api, "fresh"),)
    first_saves: list[object] = []
    second_saves: list[object] = []
    first = _controller(
        api,
        cache=cache,
        fetch_catalog=lambda *_args, **_kwargs: fresh,
        save_cache=lambda *_args: first_saves.append(_args),
    )
    second = _controller(
        api,
        cache=cache,
        fetch_catalog=lambda *_args, **_kwargs: fresh,
        save_cache=lambda *_args: second_saves.append(_args),
    )
    first.ensure_started()
    second.ensure_started()
    api.workers[0].run()
    api.gui.advance(1)
    assert len(api.workers) == 1
    assert second.state.cache.stackups == fresh
    assert second.state.cache.checked_at_utc
    assert not second.state.loading
    assert not second.state.error
    assert len(first_saves) == len(second_saves) == 1


def test_waiting_owner_recognizes_successful_empty_catalog(
    api: SimpleNamespace,
) -> None:
    """An empty but complete response can satisfy a joined check without HTTP."""
    first = _controller(api, fetch_catalog=lambda *_args, **_kwargs: ())
    second = _controller(
        api, fetch_catalog=lambda *_args, **_kwargs: (), usable_current=lambda: True
    )
    first.ensure_started()
    second.ensure_started()
    api.workers[0].run()
    api.gui.advance(1)
    assert len(api.workers) == 1
    assert second.state.cache.stackups == ()
    assert second.state.cache.checked_at_utc
    assert not second.state.loading
    assert not second.state.error


def test_new_owner_without_joined_request_does_not_reuse_an_unrelated_success(
    api: SimpleNamespace,
) -> None:
    """Only owners that joined the same request can adopt its completed result."""
    first = _controller(api, fetch_catalog=lambda *_args, **_kwargs: (_stackup(api),))
    first.ensure_started()
    api.workers[0].run()
    second = _controller(
        api, fetch_catalog=lambda *_args, **_kwargs: (_stackup(api, "latest"),)
    )
    second.ensure_started()
    assert len(api.workers) == 2
