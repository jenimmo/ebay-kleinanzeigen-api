"""Offline integration tests for shared outbound control across API paths."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

import httpx
import pytest

import main
import scrapers.inserate_by_url as by_url_scraper
import scrapers.inserate_ultra_optimized as ultra_scraper
from scrapers.inserat import get_inserate_details
from utils.job_manager import JobStorageUnavailable
from utils.outbound_control import (
    OutboundController,
    OutboundSettings,
)


class ManualClock:
    """A monotonic clock advanced explicitly by the test."""

    def __init__(self) -> None:
        self.now = 0.0
        self._sequence = 0
        self._sleepers: list[tuple[float, int, asyncio.Future[None]]] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        if delay <= 0:
            await asyncio.sleep(0)
            return

        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
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
        return min(
            (deadline for deadline, _, future in self._sleepers if not future.done()),
            default=None,
        )

    def advance_to_next_deadline(self) -> None:
        deadline = self.next_deadline
        assert deadline is not None, "No controlled sleep is pending"
        self.now = deadline
        for due_at, _, future in tuple(self._sleepers):
            if due_at <= self.now and not future.done():
                future.set_result(None)


async def _settle_event_loop(turns: int = 12) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


async def _drive_clock_until_done(
    clock: ManualClock,
    tasks: list[asyncio.Task[Any]],
    *,
    max_steps: int = 50,
) -> None:
    for _ in range(max_steps):
        await _settle_event_loop()
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


class NavigationTracker:
    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.starts: list[tuple[str, float]] = []
        self.active = 0
        self.maximum_active = 0

    def enter(self, url: str) -> None:
        self.starts.append((url, self.clock()))
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)

    def leave(self) -> None:
        self.active -= 1
        assert self.active >= 0


class FakePage:
    """Minimal page fake used by the real scraper navigation methods."""

    def __init__(
        self,
        tracker: NavigationTracker,
        *,
        body: str = "",
        final_url: str | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self.tracker = tracker
        self.body = body
        self.final_url = final_url
        self.release = release
        self.started = asyncio.Event()
        self.url = "about:blank"
        self.closed = False
        self.goto_calls: list[str] = []

    async def goto(self, url: str, **_kwargs: Any) -> FakeResponse:
        self.goto_calls.append(url)
        self.url = self.final_url or url
        self.tracker.enter(url)
        self.started.set()
        try:
            if self.release is not None:
                await self.release.wait()
            return FakeResponse()
        finally:
            self.tracker.leave()

    async def title(self) -> str:
        return "Kleinanzeigen"

    def locator(self, selector: str) -> FakeLocator:
        assert selector == "body"
        return FakeLocator(self.body)

    async def wait_for_selector(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def query_selector_all(self, _selector: str) -> list[Any]:
        return []

    async def query_selector(self, _selector: str) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page

    async def new_page(self) -> FakePage:
        return self.page


class FakeBrowserManager:
    def __init__(self, page_factory: Callable[[], FakePage]) -> None:
        self.page_factory = page_factory
        self.max_concurrent = 10
        self.contexts: list[FakeContext] = []
        self.released: list[FakeContext] = []

    async def get_context(self) -> FakeContext:
        context = FakeContext(self.page_factory())
        self.contexts.append(context)
        return context

    async def release_context(self, context: FakeContext) -> None:
        self.released.append(context)

    def get_performance_metrics(self) -> dict[str, int]:
        return {
            "contexts_in_use": len(self.contexts) - len(self.released),
            "contexts_in_pool": 0,
        }


def _controller(
    clock: ManualClock,
    *,
    max_concurrency: int,
    min_interval_seconds: float,
) -> OutboundController:
    return OutboundController(
        OutboundSettings(
            max_concurrency=max_concurrency,
            min_interval_seconds=min_interval_seconds,
            jitter_seconds=0,
            queue_capacity=20,
            queue_timeout_seconds=5,
            navigation_timeout_seconds=5,
            retry_budget=0,
            cooldown_seconds=10,
        ),
        clock=clock,
        sleep=clock.sleep,
        random_value=lambda: 0,
    )


async def _disable_page_routing(_page: FakePage) -> None:
    """Replace the network route guard only because fake pages have no routing API."""


async def test_real_search_and_detail_navigation_share_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real search/detail functions share concurrency and start-rate admission."""

    clock = ManualClock()
    tracker = NavigationTracker(clock)
    search_release = asyncio.Event()
    search_page = FakePage(tracker, release=search_release)
    manager = FakeBrowserManager(lambda: search_page)
    controller = _controller(
        clock,
        max_concurrency=1,
        min_interval_seconds=2.0,
    )
    scraper = ultra_scraper.UltraOptimizedScraper(manager, controller)
    monkeypatch.setattr(
        ultra_scraper,
        "install_safe_page_routing",
        _disable_page_routing,
    )

    detail_page = FakePage(
        tracker,
        final_url="https://www.kleinanzeigen.de/s-bestandsliste.html",
    )

    async def detail_page_factory() -> FakePage:
        return detail_page

    search_task = asyncio.create_task(
        scraper.ultra_optimized_fetch_page(
            "https://www.kleinanzeigen.de/s-suche",
            1,
            retry_count=0,
        )
    )
    await asyncio.wait_for(search_page.started.wait(), timeout=0.5)
    detail_task = asyncio.create_task(
        get_inserate_details(
            "https://www.kleinanzeigen.de/s-anzeige/123456",
            detail_page_factory,
            controller,
        )
    )
    await _settle_event_loop()

    blocked_status = await controller.status()
    assert blocked_status["active"] == 1
    assert blocked_status["queued"] == 1
    assert tracker.maximum_active == 1
    assert tracker.starts == [("https://www.kleinanzeigen.de/s-suche", 0.0)]

    search_release.set()
    await _settle_event_loop()
    assert detail_page.goto_calls == []
    assert clock.next_deadline == pytest.approx(2.0)

    clock.advance_to_next_deadline()
    search_result, detail_result = await asyncio.gather(search_task, detail_task)

    assert search_result[1].success is True
    assert detail_result["status"] == "deleted"
    assert [started_at for _, started_at in tracker.starts] == pytest.approx([0.0, 2.0])
    assert tracker.maximum_active == 1
    status = await controller.status()
    assert status["metrics"]["starts"] == 2
    assert status["metrics"]["max_active"] == 1
    assert manager.released == manager.contexts
    assert search_page.closed is True

    await scraper.cleanup()
    await controller.close()


async def test_parallel_real_by_url_requests_cannot_bypass_spacing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two public by-URL calls execute the real budget loop with one controller."""

    clock = ManualClock()
    tracker = NavigationTracker(clock)
    manager = FakeBrowserManager(lambda: FakePage(tracker))
    controller = _controller(
        clock,
        max_concurrency=4,
        min_interval_seconds=1.5,
    )
    seen_controllers: list[OutboundController] = []
    validated_urls: list[tuple[str, bool]] = []
    real_factory = by_url_scraper.create_ultra_optimized_scraper

    async def recording_factory(
        browser_manager: FakeBrowserManager,
        outbound_controller: OutboundController,
    ) -> ultra_scraper.UltraOptimizedScraper:
        seen_controllers.append(outbound_controller)
        return await real_factory(browser_manager, outbound_controller)

    async def fake_ssrf_validation(
        url: str,
        *,
        require_kleinanzeigen_host: bool = False,
    ) -> None:
        validated_urls.append((url, require_kleinanzeigen_host))

    async def one_result_per_page(
        _self: ultra_scraper.UltraOptimizedScraper,
        page: FakePage,
    ) -> list[dict[str, str]]:
        return [{"adid": page.url}]

    monkeypatch.setattr(
        by_url_scraper,
        "create_ultra_optimized_scraper",
        recording_factory,
    )
    monkeypatch.setattr(
        by_url_scraper,
        "validate_public_https_url",
        fake_ssrf_validation,
    )
    monkeypatch.setattr(
        ultra_scraper,
        "install_safe_page_routing",
        _disable_page_routing,
    )
    monkeypatch.setattr(
        ultra_scraper.UltraOptimizedScraper,
        "extract_ads_optimized",
        one_result_per_page,
    )

    urls = [
        "https://www.kleinanzeigen.de/s-suche?keywords=agent-a",
        "https://www.kleinanzeigen.de/s-suche?keywords=agent-b",
    ]
    tasks = [
        asyncio.create_task(
            by_url_scraper.scrape_by_url(
                manager,
                controller,
                base_url,
                max_pages=2,
            )
        )
        for base_url in urls
    ]
    await _drive_clock_until_done(clock, tasks)
    results = await asyncio.gather(*tasks)

    assert all(result["success"] for result in results)
    assert [result["unique_results"] for result in results] == [2, 2]
    assert seen_controllers == [controller, controller]
    assert validated_urls == [(url, True) for url in urls]
    assert len(tracker.starts) == 4
    start_times = [started_at for _, started_at in tracker.starts]
    assert all(later - earlier >= 1.5 for earlier, later in pairwise(start_times))
    assert all(
        sum(marker in navigated_url for navigated_url, _ in tracker.starts) == 2
        for marker in ("agent-a", "agent-b")
    )
    assert manager.released == manager.contexts
    assert all(context.page.closed for context in manager.contexts)
    assert (await controller.status())["metrics"]["starts"] == 4

    await controller.close()


_ROUTER_CASES = [
    (
        "GET",
        "/inserate?query=test",
        "create_search_job",
        None,
    ),
    (
        "GET",
        "/inserat/123456",
        "create_detail_job",
        None,
    ),
    (
        "POST",
        "/inserate/batch",
        "create_detail_job",
        {"ids": ["123456"], "max_concurrent": 1},
    ),
    (
        "POST",
        "/inserate-by-url",
        "create_search_job",
        {
            "url": "https://www.kleinanzeigen.de/s-suche?keywords=test",
            "max_pages": 1,
        },
    ),
]


@pytest.mark.parametrize(
    (
        "method",
        "path",
        "manager_method",
        "payload",
    ),
    _ROUTER_CASES,
)
async def test_job_adapters_surface_storage_failure_before_acceptance(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    manager_method: str,
    payload: dict[str, Any] | None,
) -> None:
    """Only a pre-acceptance storage failure rejects durable submissions."""

    class RejectingJobManager:
        pass

    async def reject_storage(*_args: Any, **_kwargs: Any) -> None:
        raise JobStorageUnavailable("job storage unavailable")

    manager = RejectingJobManager()
    setattr(manager, manager_method, reject_storage)
    monkeypatch.setattr(main.app.state, "job_manager", manager, raising=False)

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://offline.test",
    ) as client:
        response = await client.request(method, path, json=payload)

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "error": "job_storage_unavailable",
            "message": "job storage unavailable",
        }
    }


async def test_outbound_status_endpoint_exposes_no_navigation_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock()
    tracker = NavigationTracker(clock)
    controller = _controller(
        clock,
        max_concurrency=2,
        min_interval_seconds=0,
    )
    secret_url = (
        "https://www.kleinanzeigen.de/s-suche"
        "?token=top-secret-token&email=owner@example.invalid"
    )
    secret_body = "cookie=session-secret; private listing owner"
    await controller.navigate(
        FakePage(tracker, body=secret_body),
        secret_url,
    )
    monkeypatch.setattr(
        main.app.state,
        "outbound_controller",
        controller,
        raising=False,
    )

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://offline.test",
    ) as client:
        response = await client.get("/outbound-status")

    assert response.status_code == 200
    payload = response.json()
    serialized = json.dumps(payload, sort_keys=True).casefold()
    assert set(payload) == {
        "limits",
        "active",
        "queued",
        "closed",
        "circuit",
        "metrics",
    }
    assert payload["metrics"]["starts"] == 1
    for secret in (
        "top-secret-token",
        "owner@example.invalid",
        "session-secret",
        "private listing owner",
    ):
        assert secret not in serialized

    await controller.close()


async def test_outbound_status_maps_job_storage_failure_to_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenJobManager:
        async def status(self):
            raise JobStorageUnavailable("job storage unavailable")

    controller = OutboundController(
        OutboundSettings(
            max_concurrency=1,
            min_interval_seconds=0,
            jitter_seconds=0,
            queue_capacity=1,
            queue_timeout_seconds=0.1,
            navigation_timeout_seconds=1,
            retry_budget=0,
            cooldown_seconds=1,
        )
    )
    monkeypatch.setattr(
        main.app.state, "outbound_controller", controller, raising=False
    )
    monkeypatch.setattr(
        main.app.state, "job_manager", BrokenJobManager(), raising=False
    )

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://offline.test",
    ) as client:
        response = await client.get("/outbound-status")

    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "job_storage_unavailable"
    await controller.close()
