"""Offline regression tests for the process-wide outbound controller."""

from __future__ import annotations

import ast
import asyncio
import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from utils.outbound_control import (
    OutboundCircuitOpen,
    OutboundController,
    OutboundNavigationTimeout,
    OutboundQueueFull,
    OutboundQueueTimeout,
    OutboundSettings,
    UpstreamBlocked,
    UpstreamRateLimited,
)


class ManualClock:
    """Monotonic clock whose sleeps advance only when the test asks them to."""

    def __init__(self, initial: float = 0.0) -> None:
        self.now = initial
        self.sleep_calls: list[float] = []
        self._sequence = 0
        self._sleepers: list[tuple[float, int, asyncio.Future[None]]] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleep_calls.append(delay)
        if delay <= 0:
            await asyncio.sleep(0)
            return

        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        self._sequence += 1
        sleeper = (self.now + delay, self._sequence, future)
        self._sleepers.append(sleeper)
        try:
            await future
        finally:
            if sleeper in self._sleepers:
                self._sleepers.remove(sleeper)

    @property
    def next_deadline(self) -> float | None:
        pending = [
            deadline for deadline, _, future in self._sleepers if not future.done()
        ]
        return min(pending, default=None)

    def advance(self, seconds: float) -> None:
        assert seconds >= 0
        self.now += seconds
        for deadline, _, future in tuple(self._sleepers):
            if deadline <= self.now and not future.done():
                future.set_result(None)

    def advance_to_next_deadline(self) -> None:
        deadline = self.next_deadline
        assert deadline is not None, "No controlled sleep is pending"
        self.advance(max(0.0, deadline - self.now))


async def settle_event_loop(turns: int = 8) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


async def drive_clock_until_done(
    clock: ManualClock, tasks: list[asyncio.Task[Any]], *, max_steps: int = 100
) -> None:
    """Run tasks by advancing only the sleeps registered with ``ManualClock``."""

    for _ in range(max_steps):
        await settle_event_loop()
        if all(task.done() for task in tasks):
            return
        if clock.next_deadline is not None:
            clock.advance_to_next_deadline()
            continue
        await asyncio.sleep(0)
    raise AssertionError("Tasks did not finish under controlled time")


@dataclass(slots=True)
class FakeResponse:
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)


class FakeLocator:
    def __init__(self, text: str) -> None:
        self.text = text

    async def inner_text(self, *, timeout: int) -> str:
        assert timeout > 0
        return self.text


class ActivityTracker:
    def __init__(self, target: int) -> None:
        self.target = target
        self.active = 0
        self.maximum = 0
        self.reached_target = asyncio.Event()

    def enter(self) -> None:
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        if self.active >= self.target:
            self.reached_target.set()

    def leave(self) -> None:
        self.active -= 1
        assert self.active >= 0


class FakePage:
    """Small Playwright page fake with controllable navigation completion."""

    def __init__(
        self,
        *,
        response: FakeResponse | None = None,
        title: str = "Kleinanzeigen",
        body: str = "",
        final_url: str | None = None,
        clock: ManualClock | None = None,
        label: str = "page",
        starts: list[tuple[str, float]] | None = None,
        entered: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
        tracker: ActivityTracker | None = None,
        goto_error: BaseException | None = None,
        selectors: set[str] | None = None,
    ) -> None:
        self.response = response or FakeResponse()
        self.page_title = title
        self.body = body
        self.final_url = final_url
        self.clock = clock
        self.label = label
        self.starts = starts
        self.entered = entered
        self.release = release
        self.tracker = tracker
        self.goto_error = goto_error
        self.selectors = selectors or set()
        self.url = "about:blank"
        self.goto_calls: list[tuple[str, dict[str, Any]]] = []

    async def goto(self, url: str, **kwargs: Any) -> FakeResponse:
        self.goto_calls.append((url, kwargs))
        self.url = self.final_url or url
        if self.starts is not None:
            assert self.clock is not None
            self.starts.append((self.label, self.clock()))
        if self.tracker is not None:
            self.tracker.enter()
        if self.entered is not None:
            self.entered.set()
        try:
            if self.release is not None:
                await self.release.wait()
            if self.goto_error is not None:
                raise self.goto_error
            return self.response
        finally:
            if self.tracker is not None:
                self.tracker.leave()

    async def title(self) -> str:
        return self.page_title

    def locator(self, selector: str) -> FakeLocator:
        assert selector == "body"
        return FakeLocator(self.body)

    async def query_selector(self, selector: str) -> object | None:
        return object() if selector in self.selectors else None


def make_settings(**overrides: Any) -> OutboundSettings:
    values: dict[str, Any] = {
        "max_concurrency": 2,
        "min_interval_seconds": 0.0,
        "jitter_seconds": 0.0,
        "queue_capacity": 20,
        "queue_timeout_seconds": 1.0,
        "navigation_timeout_seconds": 1.0,
        "retry_budget": 1,
        "cooldown_seconds": 10.0,
    }
    values.update(overrides)
    return OutboundSettings(**values)


async def test_many_agents_never_exceed_global_concurrency() -> None:
    limit = 3
    release = asyncio.Event()
    tracker = ActivityTracker(target=limit)
    controller = OutboundController(
        make_settings(max_concurrency=limit, queue_capacity=40)
    )
    pages = [FakePage(release=release, tracker=tracker) for _ in range(30)]
    tasks = [
        asyncio.create_task(
            controller.navigate(page, f"https://www.kleinanzeigen.de/s-agent-{index}")
        )
        for index, page in enumerate(pages)
    ]

    await asyncio.wait_for(tracker.reached_target.wait(), timeout=0.5)
    await settle_event_loop()
    status_while_blocked = await controller.status()

    assert tracker.active == limit
    assert tracker.maximum == limit
    assert status_while_blocked["active"] == limit
    assert status_while_blocked["queued"] == len(tasks) - limit

    release.set()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=1.0)
    status = await controller.status()
    assert tracker.maximum == limit
    assert status["active"] == 0
    assert status["queued"] == 0
    assert status["metrics"]["max_active"] == limit
    assert status["metrics"]["starts"] == len(tasks)
    await controller.close()


async def test_fifo_and_minimum_start_interval_use_one_global_clock() -> None:
    clock = ManualClock()
    starts: list[tuple[str, float]] = []
    controller = OutboundController(
        make_settings(
            max_concurrency=3,
            min_interval_seconds=2.0,
            jitter_seconds=1.0,
        ),
        clock=clock,
        sleep=clock.sleep,
        random_value=lambda: 0.5,
    )
    labels = ["/inserate", "/inserat/1", "/inserate-detailed"]
    tasks = [
        asyncio.create_task(
            controller.navigate(
                FakePage(clock=clock, label=label, starts=starts),
                f"https://www.kleinanzeigen.de{label}",
            )
        )
        for label in labels
    ]

    await settle_event_loop()
    assert starts == [(labels[0], 0.0)]
    assert clock.next_deadline == pytest.approx(2.5)

    clock.advance(2.49)
    await settle_event_loop()
    assert len(starts) == 1

    clock.advance(0.01)
    await settle_event_loop()
    assert starts == [(labels[0], 0.0), (labels[1], 2.5)]

    clock.advance(2.5)
    await asyncio.gather(*tasks)
    assert [label for label, _ in starts] == labels
    assert [started for _, started in starts] == pytest.approx([0.0, 2.5, 5.0])
    await controller.close()


async def test_slow_state_persistence_cannot_shorten_real_start_gap() -> None:
    starts: list[float] = []
    callback_calls = 0

    class TimestampPage(FakePage):
        async def goto(self, url: str, **kwargs: Any) -> FakeResponse:
            starts.append(time.monotonic())
            return await super().goto(url, **kwargs)

    async def persist_state(_snapshot: dict[str, Any]) -> None:
        nonlocal callback_calls
        callback_calls += 1
        if callback_calls == 1:
            await asyncio.sleep(0.08)

    controller = OutboundController(
        make_settings(max_concurrency=2, min_interval_seconds=0.03),
        state_change_callback=persist_state,
    )

    await asyncio.wait_for(
        asyncio.gather(
            controller.navigate(
                TimestampPage(), "https://www.kleinanzeigen.de/s-first"
            ),
            controller.navigate(
                TimestampPage(), "https://www.kleinanzeigen.de/s-second"
            ),
        ),
        timeout=0.5,
    )

    assert len(starts) == 2
    assert starts[1] - starts[0] >= 0.03
    await controller.close()


async def test_parallel_by_url_requests_cannot_bypass_global_spacing() -> None:
    clock = ManualClock()
    starts: list[tuple[str, float]] = []
    controller = OutboundController(
        make_settings(max_concurrency=4, min_interval_seconds=1.25),
        clock=clock,
        sleep=clock.sleep,
    )

    async def by_url_request(request_id: str) -> None:
        for item in range(3):
            label = f"{request_id}:{item}"
            await controller.navigate(
                FakePage(clock=clock, label=label, starts=starts),
                f"https://www.kleinanzeigen.de/s-anzeige/{label}",
            )

    tasks = [
        asyncio.create_task(by_url_request("request-a")),
        asyncio.create_task(by_url_request("request-b")),
        asyncio.create_task(by_url_request("request-c")),
    ]
    await drive_clock_until_done(clock, tasks)
    await asyncio.gather(*tasks)

    assert len(starts) == 9
    assert {label.split(":", 1)[0] for label, _ in starts} == {
        "request-a",
        "request-b",
        "request-c",
    }
    start_times = [started for _, started in starts]
    assert all(later - earlier >= 1.25 for earlier, later in pairwise(start_times))
    await controller.close()


async def test_search_and_detail_calls_share_the_same_controller() -> None:
    clock = ManualClock()
    starts: list[tuple[str, float]] = []
    controller = OutboundController(
        make_settings(max_concurrency=2, min_interval_seconds=3.0),
        clock=clock,
        sleep=clock.sleep,
    )
    search = asyncio.create_task(
        controller.navigate(
            FakePage(clock=clock, label="search", starts=starts),
            "https://www.kleinanzeigen.de/s-suche",
        )
    )
    detail = asyncio.create_task(
        controller.navigate(
            FakePage(clock=clock, label="detail", starts=starts),
            "https://www.kleinanzeigen.de/s-anzeige/123",
        )
    )

    await settle_event_loop()
    assert starts == [("search", 0.0)]
    clock.advance(3.0)
    await asyncio.gather(search, detail)
    assert starts == [("search", 0.0), ("detail", 3.0)]
    await controller.close()


async def test_every_retry_is_readmitted_and_obeys_spacing() -> None:
    clock = ManualClock()
    starts: list[tuple[str, float]] = []
    controller = OutboundController(
        make_settings(min_interval_seconds=4.0, retry_budget=1),
        clock=clock,
        sleep=clock.sleep,
    )
    failed_attempt = FakePage(
        clock=clock,
        label="attempt-1",
        starts=starts,
        goto_error=RuntimeError("transient navigation failure"),
    )

    with pytest.raises(RuntimeError, match="transient navigation failure"):
        await controller.navigate(
            failed_attempt, "https://www.kleinanzeigen.de/s-retry"
        )

    retry = asyncio.create_task(
        controller.navigate(
            FakePage(clock=clock, label="attempt-2", starts=starts),
            "https://www.kleinanzeigen.de/s-retry",
            is_retry=True,
        )
    )
    await settle_event_loop()
    assert starts == [("attempt-1", 0.0)]
    clock.advance(4.0)
    await retry

    status = await controller.status()
    assert starts == [("attempt-1", 0.0), ("attempt-2", 4.0)]
    assert status["metrics"]["starts"] == 2
    assert status["metrics"]["retries"] == 1
    assert status["metrics"]["failed"] == 1
    assert status["metrics"]["completed"] == 1
    assert controller.bounded_retry_count(99) == 1
    await controller.close()


async def test_queue_full_fails_fast_with_controlled_429() -> None:
    release = asyncio.Event()
    entered = asyncio.Event()
    controller = OutboundController(make_settings(max_concurrency=1, queue_capacity=1))
    active = asyncio.create_task(
        controller.navigate(
            FakePage(entered=entered, release=release),
            "https://www.kleinanzeigen.de/s-active",
        )
    )
    await entered.wait()
    queued = asyncio.create_task(
        controller.navigate(FakePage(), "https://www.kleinanzeigen.de/s-queued")
    )
    await settle_event_loop()
    assert (await controller.status())["queued"] == 1

    with pytest.raises(OutboundQueueFull) as caught:
        await controller.navigate(FakePage(), "https://www.kleinanzeigen.de/s-full")

    assert caught.value.http_status == 429
    assert caught.value.error_code == "outbound_queue_full"
    assert caught.value.retry_after_seconds is not None
    release.set()
    await asyncio.gather(active, queued)
    assert (await controller.status())["metrics"]["queue_full"] == 1
    await controller.close()


async def test_queue_overflow_happens_before_page_or_context_allocation() -> None:
    release = asyncio.Event()
    entered = asyncio.Event()
    controller = OutboundController(make_settings(max_concurrency=1, queue_capacity=1))
    factory_calls: list[str] = []

    async def page_factory(label: str, page: FakePage) -> FakePage:
        factory_calls.append(label)
        return page

    active_page = FakePage(entered=entered, release=release)
    active = asyncio.create_task(
        controller.navigate_with_page(
            lambda: page_factory("active", active_page),
            "https://www.kleinanzeigen.de/s-active",
        )
    )
    await entered.wait()
    queued = asyncio.create_task(
        controller.navigate_with_page(
            lambda: page_factory("queued", FakePage()),
            "https://www.kleinanzeigen.de/s-queued",
        )
    )
    await settle_event_loop()

    with pytest.raises(OutboundQueueFull):
        await controller.navigate_with_page(
            lambda: page_factory("overflow", FakePage()),
            "https://www.kleinanzeigen.de/s-overflow",
        )

    assert factory_calls == ["active"]
    release.set()
    await asyncio.gather(active, queued)
    assert factory_calls == ["active", "queued"]
    await controller.close()


async def test_queue_timeout_withdraws_waiter_and_recovers() -> None:
    release = asyncio.Event()
    entered = asyncio.Event()
    controller = OutboundController(
        make_settings(
            max_concurrency=1,
            queue_capacity=1,
            queue_timeout_seconds=0.01,
        )
    )
    active = asyncio.create_task(
        controller.navigate(
            FakePage(entered=entered, release=release),
            "https://www.kleinanzeigen.de/s-active",
        )
    )
    await entered.wait()

    with pytest.raises(OutboundQueueTimeout) as caught:
        await asyncio.wait_for(
            controller.navigate(FakePage(), "https://www.kleinanzeigen.de/s-times-out"),
            timeout=0.5,
        )

    assert caught.value.http_status == 503
    status = await controller.status()
    assert status["active"] == 1
    assert status["queued"] == 0
    assert status["metrics"]["queue_timeouts"] == 1

    release.set()
    await active
    await controller.navigate(FakePage(), "https://www.kleinanzeigen.de/s-recovered")
    assert (await controller.status())["active"] == 0
    await controller.close()


async def test_cancelling_a_queued_navigation_removes_its_waiter() -> None:
    release = asyncio.Event()
    entered = asyncio.Event()
    controller = OutboundController(make_settings(max_concurrency=1))
    active = asyncio.create_task(
        controller.navigate(
            FakePage(entered=entered, release=release),
            "https://www.kleinanzeigen.de/s-active",
        )
    )
    await entered.wait()
    queued = asyncio.create_task(
        controller.navigate(FakePage(), "https://www.kleinanzeigen.de/s-cancelled")
    )
    await settle_event_loop()
    assert (await controller.status())["queued"] == 1

    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued

    status = await controller.status()
    assert status["active"] == 1
    assert status["queued"] == 0
    assert status["metrics"]["cancellations"] == 1

    release.set()
    await active
    await controller.navigate(FakePage(), "https://www.kleinanzeigen.de/s-recovered")
    await controller.close()


async def test_repeated_cancellation_while_withdrawing_queue_waiter_does_not_leak() -> (
    None
):
    release = asyncio.Event()
    entered = asyncio.Event()
    withdrawal_started = asyncio.Event()
    controller = OutboundController(make_settings(max_concurrency=1))
    original_withdraw = controller._withdraw_waiter

    async def observed_withdraw(*args: Any, **kwargs: Any) -> bool:
        withdrawal_started.set()
        return await original_withdraw(*args, **kwargs)

    controller._withdraw_waiter = observed_withdraw  # type: ignore[method-assign]
    active = asyncio.create_task(
        controller.navigate(
            FakePage(entered=entered, release=release),
            "https://www.kleinanzeigen.de/s-active",
        )
    )
    await entered.wait()
    queued = asyncio.create_task(
        controller.navigate(FakePage(), "https://www.kleinanzeigen.de/s-queued")
    )
    await settle_event_loop()

    await controller._lock.acquire()
    try:
        queued.cancel()
        await asyncio.wait_for(withdrawal_started.wait(), timeout=0.5)
        queued.cancel()
    finally:
        controller._lock.release()

    with pytest.raises(asyncio.CancelledError):
        await queued
    status = await controller.status()
    assert status["active"] == 1
    assert status["queued"] == 0
    assert status["metrics"]["cancellations"] == 1

    release.set()
    await active
    await controller.navigate(FakePage(), "https://www.kleinanzeigen.de/s-recovered")
    await controller.close()


async def test_cancelling_an_active_navigation_releases_its_slot() -> None:
    entered = asyncio.Event()
    controller = OutboundController(make_settings(max_concurrency=1))
    active = asyncio.create_task(
        controller.navigate(
            FakePage(entered=entered, release=asyncio.Event()),
            "https://www.kleinanzeigen.de/s-active",
        )
    )
    await entered.wait()

    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active

    status = await controller.status()
    assert status["active"] == 0
    assert status["metrics"]["cancellations"] == 1
    assert status["metrics"]["failed"] == 1

    await controller.navigate(FakePage(), "https://www.kleinanzeigen.de/s-recovered")
    assert (await controller.status())["active"] == 0
    await controller.close()


async def test_cancellation_during_start_handshake_stops_child_navigation() -> None:
    child_finished = asyncio.Event()
    never_finishes = asyncio.Event()
    owner: asyncio.Task[Any] | None = None

    class CancellingPage(FakePage):
        async def goto(self, url: str, **kwargs: Any) -> FakeResponse:
            self.goto_calls.append((url, kwargs))
            assert owner is not None
            owner.cancel()
            try:
                await never_finishes.wait()
            finally:
                child_finished.set()
            raise AssertionError("unreachable")

    controller = OutboundController(make_settings(max_concurrency=1))

    async def request() -> None:
        nonlocal owner
        owner = asyncio.current_task()
        await controller.navigate(
            CancellingPage(),
            "https://www.kleinanzeigen.de/s-cancel-during-start",
        )

    active = asyncio.create_task(request())
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(active, timeout=0.5)

    assert child_finished.is_set()
    status = await controller.status()
    assert status["active"] == 0
    assert status["metrics"]["cancellations"] == 1
    await controller.close()


async def test_repeated_cancellation_while_release_waits_does_not_leak_slot() -> None:
    entered = asyncio.Event()
    release_started = asyncio.Event()
    controller = OutboundController(make_settings(max_concurrency=1))
    original_release = controller._release

    async def observed_release(*args: Any, **kwargs: Any) -> None:
        release_started.set()
        await original_release(*args, **kwargs)

    controller._release = observed_release  # type: ignore[method-assign]
    active = asyncio.create_task(
        controller.navigate(
            FakePage(entered=entered, release=asyncio.Event()),
            "https://www.kleinanzeigen.de/s-active",
        )
    )
    await entered.wait()

    await controller._lock.acquire()
    try:
        active.cancel()
        await asyncio.wait_for(release_started.wait(), timeout=0.5)
        active.cancel()
    finally:
        controller._lock.release()

    with pytest.raises(asyncio.CancelledError):
        await active
    await settle_event_loop()

    status = await controller.status()
    assert status["active"] == 0
    assert status["metrics"]["cancellations"] == 1
    await controller.navigate(FakePage(), "https://www.kleinanzeigen.de/s-recovered")
    await controller.close()


async def test_navigation_timeout_releases_the_active_slot() -> None:
    entered = asyncio.Event()
    controller = OutboundController(
        make_settings(max_concurrency=1, navigation_timeout_seconds=0.01)
    )

    with pytest.raises(OutboundNavigationTimeout):
        await asyncio.wait_for(
            controller.navigate(
                FakePage(entered=entered, release=asyncio.Event()),
                "https://www.kleinanzeigen.de/s-timeout",
            ),
            timeout=0.5,
        )

    assert entered.is_set()
    assert (await controller.status())["active"] == 0
    await controller.navigate(FakePage(), "https://www.kleinanzeigen.de/s-recovered")
    await controller.close()


async def test_hanging_page_factory_times_out_and_releases_slot_and_start_lock() -> (
    None
):
    factory_started = asyncio.Event()
    factory_cancelled = asyncio.Event()
    never_finishes = asyncio.Event()
    controller = OutboundController(
        make_settings(max_concurrency=1, navigation_timeout_seconds=0.01)
    )

    async def hanging_page_factory() -> FakePage:
        factory_started.set()
        try:
            await never_finishes.wait()
        finally:
            factory_cancelled.set()
        raise AssertionError("unreachable")

    with pytest.raises(OutboundNavigationTimeout):
        await asyncio.wait_for(
            controller.navigate_with_page(
                hanging_page_factory,
                "https://www.kleinanzeigen.de/s-factory-timeout",
            ),
            timeout=0.5,
        )

    assert factory_started.is_set()
    assert factory_cancelled.is_set()
    assert (await controller.status())["active"] == 0

    recovered = FakePage()
    await asyncio.wait_for(
        controller.navigate(
            recovered, "https://www.kleinanzeigen.de/s-after-factory-timeout"
        ),
        timeout=0.5,
    )
    assert len(recovered.goto_calls) == 1
    await controller.close()


async def test_playwright_timeout_is_normalized_to_navigation_timeout() -> None:
    fake_timeout_type = type(
        "TimeoutError",
        (Exception,),
        {"__module__": "playwright.async_api"},
    )
    playwright_timeout = fake_timeout_type("page.goto timed out")
    controller = OutboundController(make_settings(max_concurrency=1))

    with pytest.raises(OutboundNavigationTimeout) as caught:
        await controller.navigate(
            FakePage(goto_error=playwright_timeout),
            "https://www.kleinanzeigen.de/s-playwright-timeout",
        )

    assert caught.value.__cause__ is playwright_timeout
    assert (await controller.status())["active"] == 0
    await controller.navigate(FakePage(), "https://www.kleinanzeigen.de/s-recovered")
    await controller.close()


async def test_429_retry_after_opens_circuit_and_stops_navigation() -> None:
    clock = ManualClock(initial=100.0)
    controller = OutboundController(
        make_settings(cooldown_seconds=10.0),
        clock=clock,
        sleep=clock.sleep,
    )
    rate_limited = FakePage(
        response=FakeResponse(status=429, headers={"Retry-After": "30"})
    )

    with pytest.raises(UpstreamRateLimited) as caught:
        await controller.navigate(
            rate_limited, "https://www.kleinanzeigen.de/s-rate-limited"
        )

    assert caught.value.http_status == 429
    assert caught.value.retry_after_seconds == 30
    status = await controller.status()
    assert status["circuit"] == {
        "state": "open",
        "reason": "http_429",
        "cooldown_remaining_seconds": 30.0,
    }
    assert status["metrics"]["upstream_429"] == 1

    rejected_page = FakePage()
    with pytest.raises(OutboundCircuitOpen) as rejected:
        await controller.navigate(
            rejected_page, "https://www.kleinanzeigen.de/s-must-not-run"
        )

    assert rejected.value.retry_after_seconds == 30
    assert rejected_page.goto_calls == []
    assert len(rate_limited.goto_calls) == 1
    await controller.close()


async def test_later_short_retry_after_reports_existing_longer_cooldown() -> None:
    clock = ManualClock(initial=100.0)
    controller = OutboundController(
        make_settings(max_concurrency=2, cooldown_seconds=1.0),
        clock=clock,
        sleep=clock.sleep,
    )
    long_entered = asyncio.Event()
    short_entered = asyncio.Event()
    long_release = asyncio.Event()
    short_release = asyncio.Event()

    long_trip = asyncio.create_task(
        controller.navigate(
            FakePage(
                response=FakeResponse(status=429, headers={"Retry-After": "60"}),
                entered=long_entered,
                release=long_release,
            ),
            "https://www.kleinanzeigen.de/s-long-cooldown",
        )
    )
    short_trip = asyncio.create_task(
        controller.navigate(
            FakePage(
                response=FakeResponse(status=429, headers={"Retry-After": "2"}),
                entered=short_entered,
                release=short_release,
            ),
            "https://www.kleinanzeigen.de/s-short-cooldown",
        )
    )
    await asyncio.gather(long_entered.wait(), short_entered.wait())

    long_release.set()
    with pytest.raises(UpstreamRateLimited) as first:
        await long_trip
    assert first.value.retry_after_seconds == 60

    short_release.set()
    with pytest.raises(UpstreamRateLimited) as second:
        await short_trip

    assert second.value.retry_after_seconds == 60
    status = await controller.status()
    assert status["circuit"]["cooldown_remaining_seconds"] == pytest.approx(60.0)
    assert status["metrics"]["circuit_trips"] == 2
    await controller.close()


async def test_429_rejects_already_queued_navigation_before_goto() -> None:
    release = asyncio.Event()
    entered = asyncio.Event()
    controller = OutboundController(make_settings(max_concurrency=1))
    triggering_page = FakePage(
        response=FakeResponse(status=429), entered=entered, release=release
    )
    triggering = asyncio.create_task(
        controller.navigate(
            triggering_page, "https://www.kleinanzeigen.de/s-rate-limited"
        )
    )
    await entered.wait()

    waiting_page = FakePage()
    waiting = asyncio.create_task(
        controller.navigate(waiting_page, "https://www.kleinanzeigen.de/s-waiting")
    )
    await settle_event_loop()
    assert (await controller.status())["queued"] == 1

    release.set()
    with pytest.raises(UpstreamRateLimited):
        await triggering
    with pytest.raises(OutboundCircuitOpen):
        await waiting
    assert waiting_page.goto_calls == []
    await controller.close()


async def test_cancellation_cannot_discard_an_already_detected_circuit_trip() -> None:
    navigation_entered = asyncio.Event()
    navigation_release = asyncio.Event()
    trip_started = asyncio.Event()
    controller = OutboundController(make_settings(max_concurrency=1))
    original_trip = controller._trip_circuit

    async def observed_trip(*args: Any, **kwargs: Any) -> float:
        trip_started.set()
        return await original_trip(*args, **kwargs)

    controller._trip_circuit = observed_trip  # type: ignore[method-assign]
    navigation = asyncio.create_task(
        controller.navigate(
            FakePage(
                response=FakeResponse(status=429),
                entered=navigation_entered,
                release=navigation_release,
            ),
            "https://www.kleinanzeigen.de/s-rate-limited",
        )
    )
    await navigation_entered.wait()

    await controller._lock.acquire()
    try:
        navigation_release.set()
        await asyncio.wait_for(trip_started.wait(), timeout=0.5)
        navigation.cancel()
        navigation.cancel()
    finally:
        controller._lock.release()

    with pytest.raises(asyncio.CancelledError):
        await navigation

    status = await controller.status()
    assert status["active"] == 0
    assert status["circuit"]["state"] == "open"
    assert status["circuit"]["reason"] == "http_429"
    assert status["metrics"]["upstream_429"] == 1
    await controller.close()


async def test_cancellation_at_429_classification_still_trips_circuit() -> None:
    owner: asyncio.Task[Any] | None = None

    class CancellingResponse:
        status = 429

        @property
        def headers(self) -> dict[str, str]:
            assert owner is not None
            owner.cancel()
            return {"Retry-After": "19"}

    controller = OutboundController(
        make_settings(max_concurrency=1, cooldown_seconds=1.0)
    )

    async def request() -> None:
        nonlocal owner
        owner = asyncio.current_task()
        await controller.navigate(
            FakePage(response=CancellingResponse()),  # type: ignore[arg-type]
            "https://www.kleinanzeigen.de/s-cancel-on-429",
        )

    navigation = asyncio.create_task(request())
    with pytest.raises(asyncio.CancelledError):
        await navigation

    status = await controller.status()
    assert status["active"] == 0
    assert status["circuit"]["state"] == "open"
    assert status["circuit"]["reason"] == "http_429"
    assert status["circuit"]["cooldown_remaining_seconds"] > 18
    assert status["metrics"]["upstream_429"] == 1
    await controller.close()


async def test_retry_after_http_date_is_honored() -> None:
    monotonic = ManualClock(initial=50.0)
    wall_now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    retry_at = format_datetime(wall_now + timedelta(seconds=45), usegmt=True)
    controller = OutboundController(
        make_settings(cooldown_seconds=5.0),
        clock=monotonic,
        sleep=monotonic.sleep,
        utcnow=lambda: wall_now,
    )

    with pytest.raises(UpstreamRateLimited) as caught:
        await controller.navigate(
            FakePage(
                response=FakeResponse(status=429, headers={"retry-after": retry_at})
            ),
            "https://www.kleinanzeigen.de/s-rate-limited",
        )

    assert caught.value.retry_after_seconds == 45
    assert (await controller.status())["circuit"][
        "cooldown_remaining_seconds"
    ] == pytest.approx(45.0)
    await controller.close()


async def test_huge_retry_after_remains_finite_and_bounded() -> None:
    clock = ManualClock()
    controller = OutboundController(
        make_settings(cooldown_seconds=5.0),
        clock=clock,
        sleep=clock.sleep,
    )

    with pytest.raises(UpstreamRateLimited) as caught:
        await controller.navigate(
            FakePage(
                response=FakeResponse(
                    status=429,
                    headers={"Retry-After": "9" * 10_000},
                )
            ),
            "https://www.kleinanzeigen.de/s-rate-limited",
        )

    retry_after_seconds = caught.value.retry_after_seconds
    remaining = (await controller.status())["circuit"]["cooldown_remaining_seconds"]
    assert retry_after_seconds is not None
    assert 1 <= retry_after_seconds <= 86_400
    assert math.isfinite(remaining)
    assert 1.0 <= remaining <= 86_400.0
    await controller.close()


async def test_half_open_allows_one_probe_then_controlled_recovery() -> None:
    clock = ManualClock()
    controller = OutboundController(
        make_settings(max_concurrency=4, cooldown_seconds=5.0),
        clock=clock,
        sleep=clock.sleep,
    )
    with pytest.raises(UpstreamRateLimited):
        await controller.navigate(
            FakePage(response=FakeResponse(status=429)),
            "https://www.kleinanzeigen.de/s-rate-limited",
        )

    clock.advance(5.0)
    probe_entered = asyncio.Event()
    probe_release = asyncio.Event()
    probe_page = FakePage(entered=probe_entered, release=probe_release)
    probe = asyncio.create_task(
        controller.navigate(probe_page, "https://www.kleinanzeigen.de/s-probe")
    )
    await probe_entered.wait()

    competing_page = FakePage()
    with pytest.raises(OutboundCircuitOpen):
        await controller.navigate(
            competing_page, "https://www.kleinanzeigen.de/s-second-probe"
        )
    assert competing_page.goto_calls == []
    assert (await controller.status())["circuit"]["state"] == "half_open"

    probe_release.set()
    await probe
    recovered_page = FakePage()
    await controller.navigate(
        recovered_page, "https://www.kleinanzeigen.de/s-after-cooldown"
    )

    status = await controller.status()
    assert status["circuit"]["state"] == "closed"
    assert status["circuit"]["reason"] is None
    assert status["metrics"]["circuit_recoveries"] == 1
    assert len(recovered_page.goto_calls) == 1
    await controller.close()


async def test_stale_successful_probe_cannot_close_a_newer_circuit_trip() -> None:
    clock = ManualClock()
    controller = OutboundController(
        make_settings(max_concurrency=3, cooldown_seconds=5.0),
        clock=clock,
        sleep=clock.sleep,
    )
    old_entered = asyncio.Event()
    old_release = asyncio.Event()
    old_navigation = asyncio.create_task(
        controller.navigate(
            FakePage(
                response=FakeResponse(status=429),
                entered=old_entered,
                release=old_release,
            ),
            "https://www.kleinanzeigen.de/s-old-in-flight",
        )
    )
    await old_entered.wait()

    with pytest.raises(UpstreamRateLimited):
        await controller.navigate(
            FakePage(response=FakeResponse(status=429)),
            "https://www.kleinanzeigen.de/s-first-trip",
        )

    clock.advance(5.0)
    probe_entered = asyncio.Event()
    probe_release = asyncio.Event()
    probe = asyncio.create_task(
        controller.navigate(
            FakePage(entered=probe_entered, release=probe_release),
            "https://www.kleinanzeigen.de/s-half-open-probe",
        )
    )
    await probe_entered.wait()

    old_release.set()
    with pytest.raises(UpstreamRateLimited):
        await old_navigation

    probe_release.set()
    await probe

    status = await controller.status()
    assert status["circuit"] == {
        "state": "open",
        "reason": "http_429",
        "cooldown_remaining_seconds": 5.0,
    }
    assert status["metrics"]["circuit_trips"] == 2
    assert status["metrics"]["circuit_recoveries"] == 0

    rejected_page = FakePage()
    with pytest.raises(OutboundCircuitOpen):
        await controller.navigate(
            rejected_page, "https://www.kleinanzeigen.de/s-still-blocked"
        )
    assert rejected_page.goto_calls == []
    await controller.close()


async def test_http_403_is_not_treated_as_an_empty_success() -> None:
    controller = OutboundController(make_settings(cooldown_seconds=7.0))
    forbidden = FakePage(response=FakeResponse(status=403), body="")

    with pytest.raises(UpstreamBlocked) as caught:
        await controller.navigate(
            forbidden, "https://www.kleinanzeigen.de/s-empty-looking"
        )

    assert caught.value.retry_after_seconds == 7
    status = await controller.status()
    assert status["circuit"]["state"] == "open"
    assert status["circuit"]["reason"] == "http_403"
    assert status["metrics"]["upstream_403"] == 1
    assert status["metrics"]["completed"] == 0
    await controller.close()


@pytest.mark.parametrize(
    ("title", "body", "final_url"),
    [
        ("Security check", "Please verify you are human", None),
        ("Kleinanzeigen", "Ich bin kein Roboter", None),
        ("Kleinanzeigen", "", "https://www.kleinanzeigen.de/challenge/start"),
    ],
)
async def test_captcha_challenge_and_block_pages_are_rejected(
    title: str, body: str, final_url: str | None
) -> None:
    controller = OutboundController(make_settings())
    page = FakePage(title=title, body=body, final_url=final_url)

    with pytest.raises(UpstreamBlocked, match="captcha, challenge, or block"):
        await controller.navigate(page, "https://www.kleinanzeigen.de/s-search")

    status = await controller.status()
    assert status["circuit"]["reason"] == "block_page"
    assert status["metrics"]["block_pages"] == 1
    assert status["metrics"]["completed"] == 0
    await controller.close()


async def test_legitimate_empty_search_page_remains_a_success() -> None:
    controller = OutboundController(make_settings())
    response = FakeResponse(status=200)
    empty_search = FakePage(
        response=response,
        title="Kleinanzeigen | Suchergebnisse",
        body="Keine Anzeigen gefunden. Bitte passe deine Suchkriterien an.",
    )

    returned = await controller.navigate(
        empty_search, "https://www.kleinanzeigen.de/s-kein-treffer"
    )

    assert returned is response
    status = await controller.status()
    assert status["circuit"]["state"] == "closed"
    assert status["metrics"]["completed"] == 1
    assert status["metrics"]["block_pages"] == 0
    await controller.close()


@pytest.mark.parametrize("expected_selector", [".ad-listitem", "#viewad-title"])
async def test_expected_kleinanzeigen_dom_allows_user_generated_block_words(
    expected_selector: str,
) -> None:
    controller = OutboundController(make_settings())
    legitimate_page = FakePage(
        response=FakeResponse(status=200),
        title="Access denied in einem Computerspiel – Kleinanzeigen",
        body=(
            "Im Anzeigentext beschreibt der Nutzer ein Captcha und die Meldung "
            "Zugriff verweigert."
        ),
        selectors={expected_selector},
    )

    response = await controller.navigate(
        legitimate_page, "https://www.kleinanzeigen.de/s-legitimate-listing"
    )

    assert response.status == 200
    status = await controller.status()
    assert status["circuit"]["state"] == "closed"
    assert status["metrics"]["block_pages"] == 0
    assert status["metrics"]["completed"] == 1
    await controller.close()


async def test_challenge_dom_signature_remains_authoritative() -> None:
    controller = OutboundController(make_settings())
    challenge = FakePage(
        response=FakeResponse(status=200),
        title="Kleinanzeigen",
        body="",
        selectors={".ad-listitem", "#challenge-form"},
    )

    with pytest.raises(UpstreamBlocked):
        await controller.navigate(
            challenge, "https://www.kleinanzeigen.de/s-challenge-dom"
        )

    status = await controller.status()
    assert status["circuit"]["reason"] == "block_page"
    assert status["metrics"]["block_pages"] == 1
    await controller.close()


async def test_durable_navigation_cleanup_error_preserves_upstream_block() -> None:
    controller = OutboundController(make_settings(max_concurrency=1))
    page = FakePage(response=FakeResponse(status=429))
    cleanup_called = asyncio.Event()

    async def page_factory() -> FakePage:
        return page

    async def operation(_page: FakePage, _response: FakeResponse) -> None:
        raise AssertionError("blocked response must not reach the parser")

    async def failing_cleanup(_page: FakePage | None) -> None:
        cleanup_called.set()
        raise RuntimeError("cleanup also failed")

    with pytest.raises(UpstreamRateLimited):
        await controller.run_navigation(
            page_factory,
            "https://www.kleinanzeigen.de/s-rate-limited",
            operation,
            cleanup=failing_cleanup,
        )

    assert cleanup_called.is_set()
    assert (await controller.status())["active"] == 0
    await controller.close()


async def test_durable_navigation_cleanup_error_preserves_cancellation() -> None:
    controller = OutboundController(make_settings(max_concurrency=1))
    page = FakePage()
    operation_entered = asyncio.Event()
    never_finishes = asyncio.Event()
    cleanup_called = asyncio.Event()

    async def page_factory() -> FakePage:
        return page

    async def operation(_page: FakePage, _response: FakeResponse) -> None:
        operation_entered.set()
        await never_finishes.wait()

    async def failing_cleanup(_page: FakePage | None) -> None:
        cleanup_called.set()
        raise RuntimeError("cleanup also failed")

    task = asyncio.create_task(
        controller.run_navigation(
            page_factory,
            "https://www.kleinanzeigen.de/s-cancelled",
            operation,
            cleanup=failing_cleanup,
        )
    )
    await operation_entered.wait()
    task.cancel()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert cleanup_called.is_set()
    status = await controller.status()
    assert status["active"] == 0
    assert status["metrics"]["cancellations"] == 1
    await controller.close()


async def test_durable_navigation_spacing_is_outside_goto_timeout() -> None:
    controller = OutboundController(
        make_settings(
            max_concurrency=1,
            min_interval_seconds=0.04,
            navigation_timeout_seconds=0.01,
        )
    )

    async def operation(_page: FakePage, response: FakeResponse) -> FakeResponse:
        return response

    async def run(page: FakePage) -> FakeResponse:
        async def page_factory() -> FakePage:
            return page

        return await controller.run_navigation(
            page_factory,
            "https://www.kleinanzeigen.de/s-durable-spacing",
            operation,
        )

    await run(FakePage())
    # This waits around eight times longer than the configured goto timeout;
    # only the actual immediate goto is subject to that deadline.
    await asyncio.wait_for(run(FakePage()), timeout=0.5)
    assert (await controller.status())["metrics"]["completed"] == 2
    await controller.close()


async def test_durable_navigation_waits_unbounded_and_holds_slot_through_cleanup() -> (
    None
):
    controller = OutboundController(
        make_settings(max_concurrency=1, queue_capacity=1, queue_timeout_seconds=0.01)
    )
    cleanup_entered = asyncio.Event()
    cleanup_release = asyncio.Event()
    later_starts = [asyncio.Event(), asyncio.Event()]

    async def operation(_page: FakePage, _response: FakeResponse) -> None:
        return None

    async def first_cleanup(_page: FakePage | None) -> None:
        cleanup_entered.set()
        await cleanup_release.wait()

    async def run(page: FakePage, cleanup=None) -> None:
        async def page_factory() -> FakePage:
            return page

        await controller.run_navigation(
            page_factory,
            "https://www.kleinanzeigen.de/s-durable-queue",
            operation,
            cleanup=cleanup,
        )

    first = asyncio.create_task(run(FakePage(), first_cleanup))
    await cleanup_entered.wait()
    second = asyncio.create_task(run(FakePage(entered=later_starts[0])))
    third = asyncio.create_task(run(FakePage(entered=later_starts[1])))
    await settle_event_loop()

    status = await controller.status()
    assert status["active"] == 1
    assert status["queued"] == 2  # exceeds the legacy queue capacity without rejection
    assert not any(event.is_set() for event in later_starts)

    cleanup_release.set()
    await asyncio.wait_for(asyncio.gather(first, second, third), timeout=0.5)
    assert all(event.is_set() for event in later_starts)
    assert (await controller.status())["active"] == 0
    await controller.close()


async def test_successful_half_open_navigation_closes_before_parser_failure() -> None:
    clock = ManualClock()
    controller = OutboundController(
        make_settings(max_concurrency=1, cooldown_seconds=5.0),
        clock=clock,
        sleep=clock.sleep,
    )
    with pytest.raises(UpstreamRateLimited):
        await controller.navigate(
            FakePage(response=FakeResponse(status=429)),
            "https://www.kleinanzeigen.de/s-initial-block",
        )
    clock.advance(5.0)

    async def page_factory() -> FakePage:
        return FakePage(response=FakeResponse(status=200))

    async def failing_parser(_page: FakePage, _response: FakeResponse) -> None:
        raise RuntimeError("permanent parser failure")

    with pytest.raises(RuntimeError, match="parser failure"):
        await controller.run_navigation(
            page_factory,
            "https://www.kleinanzeigen.de/s-half-open-parser",
            failing_parser,
        )

    status = await controller.status()
    assert status["circuit"]["state"] == "closed"
    assert status["active"] == 0
    await controller.close()


async def test_prepared_navigation_rechecks_circuit_before_real_start() -> None:
    controller = OutboundController(make_settings(max_concurrency=2))
    release_blocking_response = asyncio.Event()
    factory_entered = asyncio.Event()
    release_factory = asyncio.Event()
    blocking_page = FakePage(
        response=FakeResponse(status=429), release=release_blocking_response
    )
    prepared_page = FakePage(response=FakeResponse(status=200))

    async def delayed_page_factory() -> FakePage:
        factory_entered.set()
        await release_factory.wait()
        return prepared_page

    first = asyncio.create_task(
        controller.navigate(blocking_page, "https://www.kleinanzeigen.de/s-first-block")
    )
    await settle_event_loop()
    second = asyncio.create_task(
        controller.navigate_with_page(
            delayed_page_factory, "https://www.kleinanzeigen.de/s-prepared"
        )
    )
    await factory_entered.wait()

    release_blocking_response.set()
    with pytest.raises(UpstreamRateLimited):
        await first
    assert (await controller.status())["circuit"]["state"] == "open"

    release_factory.set()
    with pytest.raises(OutboundCircuitOpen):
        await second

    assert prepared_page.goto_calls == []
    assert (await controller.status())["active"] == 0
    await controller.close()


async def test_block_inspection_is_bounded_by_navigation_attempt_timeout() -> None:
    never_returns = asyncio.Event()

    class HangingInspectionPage(FakePage):
        async def query_selector(self, _selector: str):
            await never_returns.wait()

    controller = OutboundController(
        make_settings(max_concurrency=1, navigation_timeout_seconds=0.01)
    )

    async def page_factory() -> FakePage:
        return HangingInspectionPage(response=FakeResponse(status=200))

    async def operation(_page: FakePage, _response: FakeResponse) -> None:
        raise AssertionError("inspection timeout must happen before extraction")

    with pytest.raises(OutboundNavigationTimeout):
        await asyncio.wait_for(
            controller.run_navigation(
                page_factory,
                "https://www.kleinanzeigen.de/s-hanging-inspection",
                operation,
            ),
            timeout=0.2,
        )

    assert (await controller.status())["active"] == 0
    await controller.close()


async def test_detected_429_survives_slow_circuit_persistence() -> None:
    async def persist_state(snapshot: dict[str, Any]) -> None:
        if snapshot["circuit_reason"] == "http_429":
            # Circuit persistence deliberately exceeds the browser-attempt
            # deadline after the 429 was already observed.
            await asyncio.sleep(0.05)

    controller = OutboundController(
        make_settings(
            max_concurrency=1,
            navigation_timeout_seconds=0.01,
            cooldown_seconds=1.0,
        ),
        state_change_callback=persist_state,
    )

    with pytest.raises(UpstreamRateLimited) as caught:
        await asyncio.wait_for(
            controller.navigate(
                FakePage(
                    response=FakeResponse(
                        status=429,
                        headers={"Retry-After": "17"},
                    )
                ),
                "https://www.kleinanzeigen.de/s-slow-circuit-persistence",
            ),
            timeout=0.5,
        )

    assert caught.value.retry_after_seconds == 17
    status = await controller.status()
    assert status["circuit"]["state"] == "open"
    assert status["circuit"]["reason"] == "http_429"
    assert status["metrics"]["upstream_429"] == 1
    assert status["active"] == 0
    await controller.close()


async def test_repeated_half_open_block_uses_bounded_exponential_cooldown() -> None:
    clock = ManualClock()
    controller = OutboundController(
        make_settings(
            max_concurrency=1,
            cooldown_seconds=5.0,
            max_cooldown_seconds=20.0,
        ),
        clock=clock,
        sleep=clock.sleep,
    )

    for expected in (5.0, 10.0, 20.0, 20.0):
        with pytest.raises(UpstreamRateLimited):
            await controller.navigate(
                FakePage(response=FakeResponse(status=429)),
                "https://www.kleinanzeigen.de/s-repeated-block",
            )
        status = await controller.status()
        assert status["circuit"]["cooldown_remaining_seconds"] == expected
        clock.advance(expected)

    await controller.close()


async def test_status_metrics_do_not_leak_navigation_data() -> None:
    controller = OutboundController(make_settings())
    secret_url = (
        "https://www.kleinanzeigen.de/s-anzeige/private-id?token=super-secret-value"
    )
    page = FakePage(body="listing-owner@example.invalid cookie=session-secret")
    await controller.navigate(page, secret_url)

    status = await controller.status()
    serialized = json.dumps(status, sort_keys=True).casefold()
    assert "private-id" not in serialized
    assert "super-secret-value" not in serialized
    assert "listing-owner" not in serialized
    assert "session-secret" not in serialized
    assert "cookie" not in serialized
    assert "header" not in serialized
    assert set(status) == {"limits", "active", "queued", "closed", "circuit", "metrics"}
    assert status["active"] == 0
    assert status["metrics"]["starts"] == 1
    await controller.close()


def test_environment_configuration_is_parsed_and_validated() -> None:
    settings = OutboundSettings.from_env(
        {
            "KLEINANZEIGEN_OUTBOUND_MAX_CONCURRENCY": "4",
            "KLEINANZEIGEN_OUTBOUND_MIN_INTERVAL_SECONDS": "1.75",
            "KLEINANZEIGEN_OUTBOUND_JITTER_SECONDS": "0.25",
            "KLEINANZEIGEN_OUTBOUND_QUEUE_CAPACITY": "33",
            "KLEINANZEIGEN_OUTBOUND_QUEUE_TIMEOUT_SECONDS": "12.5",
            "KLEINANZEIGEN_OUTBOUND_NAVIGATION_TIMEOUT_SECONDS": "45",
            "KLEINANZEIGEN_OUTBOUND_RETRY_BUDGET": "2",
            "KLEINANZEIGEN_OUTBOUND_COOLDOWN_SECONDS": "180",
            "KLEINANZEIGEN_OUTBOUND_MAX_COOLDOWN_SECONDS": "900",
        }
    )

    assert settings == OutboundSettings(
        max_concurrency=4,
        min_interval_seconds=1.75,
        jitter_seconds=0.25,
        queue_capacity=33,
        queue_timeout_seconds=12.5,
        navigation_timeout_seconds=45.0,
        retry_budget=2,
        cooldown_seconds=180.0,
        max_cooldown_seconds=900.0,
    )


@pytest.mark.parametrize(
    ("environment_name", "value", "setting_name"),
    [
        ("KLEINANZEIGEN_OUTBOUND_MAX_CONCURRENCY", "0", "max_concurrency"),
        ("KLEINANZEIGEN_OUTBOUND_MAX_CONCURRENCY", "many", "MAX_CONCURRENCY"),
        (
            "KLEINANZEIGEN_OUTBOUND_MIN_INTERVAL_SECONDS",
            "-0.1",
            "min_interval_seconds",
        ),
        (
            "KLEINANZEIGEN_OUTBOUND_JITTER_SECONDS",
            "nan",
            "JITTER_SECONDS",
        ),
        ("KLEINANZEIGEN_OUTBOUND_QUEUE_CAPACITY", "0", "queue_capacity"),
        (
            "KLEINANZEIGEN_OUTBOUND_QUEUE_TIMEOUT_SECONDS",
            "0",
            "queue_timeout_seconds",
        ),
        (
            "KLEINANZEIGEN_OUTBOUND_NAVIGATION_TIMEOUT_SECONDS",
            "infinity",
            "NAVIGATION_TIMEOUT_SECONDS",
        ),
        ("KLEINANZEIGEN_OUTBOUND_RETRY_BUDGET", "-1", "retry_budget"),
        ("KLEINANZEIGEN_OUTBOUND_COOLDOWN_SECONDS", "0", "cooldown_seconds"),
        (
            "KLEINANZEIGEN_OUTBOUND_MAX_COOLDOWN_SECONDS",
            "0",
            "max_cooldown_seconds",
        ),
    ],
)
def test_invalid_environment_values_fail_clearly(
    environment_name: str, value: str, setting_name: str
) -> None:
    with pytest.raises(ValueError) as caught:
        OutboundSettings.from_env({environment_name: value})

    assert setting_name.casefold() in str(caught.value).casefold()


def test_production_goto_calls_exist_only_in_outbound_controller() -> None:
    project_root = Path(__file__).resolve().parents[1]
    production_files = [project_root / "main.py"]
    for directory in ("libs", "routers", "scrapers", "utils"):
        production_files.extend((project_root / directory).rglob("*.py"))

    goto_calls: list[tuple[Path, int]] = []
    for source_path in production_files:
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=source_path)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "goto"
            ):
                goto_calls.append((source_path.relative_to(project_root), node.lineno))

    assert goto_calls
    assert {path.as_posix() for path, _ in goto_calls} == {"utils/outbound_control.py"}
    assert len(goto_calls) == 1
