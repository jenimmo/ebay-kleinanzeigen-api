import asyncio

import pytest

from utils.browser import (
    OptimizedPlaywrightManager,
    PlaywrightManager,
    close_page_and_release_context,
)
from utils.outbound_control import OutboundController, OutboundSettings


class FakePage:
    def __init__(self, *, fail_close=False):
        self.closed = False
        self.fail_close = fail_close

    async def close(self):
        self.closed = True
        if self.fail_close:
            raise RuntimeError("page close failed")


class FakeContext:
    def __init__(
        self,
        *,
        fail_clear=False,
        fail_close=False,
        clear_started=None,
        clear_continue=None,
    ):
        self.pages = []
        self.closed = False
        self.close_calls = 0
        self.clear_calls = 0
        self.fail_clear = fail_clear
        self.fail_close = fail_close
        self.clear_started = clear_started
        self.clear_continue = clear_continue

    async def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page

    async def clear_cookies(self):
        self.clear_calls += 1
        if self.clear_started is not None:
            self.clear_started.set()
        if self.clear_continue is not None:
            await self.clear_continue.wait()
        if self.fail_clear:
            raise RuntimeError("clear failed")

    async def close(self):
        self.close_calls += 1
        self.closed = True
        if self.fail_close:
            raise RuntimeError("context close failed")


class FakeBrowser:
    def __init__(self, *, failures=0, fail_close=False, context_factory=None):
        self.failures = failures
        self.fail_close = fail_close
        self.close_calls = 0
        self.context_factory = context_factory or FakeContext
        self.new_context_calls = []
        self.contexts = []

    async def new_context(self, **kwargs):
        self.new_context_calls.append(kwargs)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("new_context failed")
        context = self.context_factory()
        self.contexts.append(context)
        return context

    async def close(self):
        self.close_calls += 1
        if self.fail_close:
            raise RuntimeError("browser close failed")


class FakePlaywright:
    def __init__(self, *, fail_stop=False):
        self.fail_stop = fail_stop
        self.stop_calls = 0

    async def stop(self):
        self.stop_calls += 1
        if self.fail_stop:
            raise RuntimeError("driver stop failed")


def run(coro):
    return asyncio.run(coro)


def test_waiter_progresses_after_a_context_is_released():
    async def scenario():
        browser = FakeBrowser()
        manager = OptimizedPlaywrightManager(
            max_contexts=2, max_concurrent=2, context_acquire_timeout=0.5
        )
        manager._browser = browser

        first = await manager.get_context()
        second = await manager.get_context()
        waiter = asyncio.create_task(manager.get_context())
        await asyncio.sleep(0)
        assert not waiter.done()

        await manager.release_context(first)
        acquired = await asyncio.wait_for(waiter, timeout=0.2)

        assert acquired is first
        assert manager.get_performance_metrics()["contexts_in_use"] == 2
        await manager.release_context(acquired)
        await manager.release_context(second)

    run(scenario())


def test_context_wait_times_out_and_does_not_consume_a_slot():
    async def scenario():
        browser = FakeBrowser()
        manager = OptimizedPlaywrightManager(
            max_contexts=1, max_concurrent=1, context_acquire_timeout=0.01
        )
        manager._browser = browser
        first = await manager.get_context()

        with pytest.raises(TimeoutError, match="Timed out waiting"):
            await manager.get_context()

        await manager.release_context(first)
        recovered = await asyncio.wait_for(manager.get_context(), timeout=0.2)
        await manager.release_context(recovered)

    run(scenario())


def test_cancelled_waiter_does_not_consume_a_slot():
    async def scenario():
        browser = FakeBrowser()
        manager = OptimizedPlaywrightManager(
            max_contexts=1, max_concurrent=1, context_acquire_timeout=0.5
        )
        manager._browser = browser
        first = await manager.get_context()

        waiter = asyncio.create_task(manager.get_context())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        await manager.release_context(first)
        recovered = await asyncio.wait_for(manager.get_context(), timeout=0.2)
        await manager.release_context(recovered)

    run(scenario())


def test_failed_context_creation_returns_its_slot():
    async def scenario():
        browser = FakeBrowser(failures=1)
        manager = OptimizedPlaywrightManager(
            max_contexts=1, max_concurrent=1, context_acquire_timeout=0.1
        )
        manager._browser = browser

        with pytest.raises(RuntimeError, match="new_context failed"):
            await manager.get_context()

        context = await asyncio.wait_for(manager.get_context(), timeout=0.2)
        await manager.release_context(context)

    run(scenario())


def test_failed_release_closes_context_and_returns_its_slot():
    async def scenario():
        browser = FakeBrowser(context_factory=lambda: FakeContext(fail_clear=True))
        manager = OptimizedPlaywrightManager(
            max_contexts=1, max_concurrent=1, context_acquire_timeout=0.1
        )
        manager._browser = browser
        context = await manager.get_context()

        with pytest.raises(RuntimeError, match="clear failed"):
            await manager.release_context(context)

        assert context.closed
        browser.context_factory = FakeContext
        recovered = await asyncio.wait_for(manager.get_context(), timeout=0.2)
        await manager.release_context(recovered)

    run(scenario())


def test_cancelled_release_finishes_cleanup_before_propagating_cancellation():
    async def scenario():
        clear_started = asyncio.Event()
        clear_continue = asyncio.Event()
        context = FakeContext(
            clear_started=clear_started, clear_continue=clear_continue
        )
        browser = FakeBrowser(context_factory=lambda: context)
        manager = OptimizedPlaywrightManager(
            max_contexts=2, max_concurrent=2, context_acquire_timeout=0.2
        )
        manager._browser = browser
        checked_out = await manager.get_context()

        release = asyncio.create_task(manager.release_context(checked_out))
        await clear_started.wait()
        release.cancel()
        clear_continue.set()
        with pytest.raises(asyncio.CancelledError):
            await release

        reused = await asyncio.wait_for(manager.get_context(), timeout=0.2)
        assert reused is checked_out
        await manager.release_context(reused)

    run(scenario())


def test_timed_out_release_discards_context_and_returns_its_slot():
    async def scenario():
        close_started = asyncio.Event()
        never_close = asyncio.Event()

        class HangingPage(FakePage):
            async def close(self):
                self.closed = True
                close_started.set()
                await never_close.wait()

        hanging_context = FakeContext()
        hanging_context.pages = [HangingPage()]
        browser = FakeBrowser(context_factory=lambda: hanging_context)
        manager = OptimizedPlaywrightManager(
            max_contexts=1, max_concurrent=1, context_acquire_timeout=0.1
        )
        manager._browser = browser
        checked_out = await manager.get_context()

        with pytest.raises(TimeoutError, match="Timed out cleaning"):
            await manager.release_context(checked_out, timeout_seconds=0.01)

        assert close_started.is_set()
        assert manager.get_performance_metrics()["contexts_in_use"] == 0
        browser.context_factory = FakeContext
        recovered = await asyncio.wait_for(manager.get_context(), timeout=0.2)
        assert recovered is not checked_out
        await manager.release_context(recovered)

    run(scenario())


def test_cleanup_deadline_releases_browser_and_outbound_slots():
    async def scenario():
        never_close = asyncio.Event()

        class NavigableHangingPage(FakePage):
            def __init__(self):
                super().__init__()
                self.url = "about:blank"

            async def goto(self, url, **_kwargs):
                self.url = url
                return type("FakeResponse", (), {"status": 200, "headers": {}})()

            async def query_selector(self, _selector):
                return None

            async def close(self):
                self.closed = True
                await never_close.wait()

        page = NavigableHangingPage()
        context = FakeContext()
        context.pages = [page]
        browser = FakeBrowser(context_factory=lambda: context)
        manager = OptimizedPlaywrightManager(
            max_contexts=1, max_concurrent=1, context_acquire_timeout=0.1
        )
        manager._browser = browser
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
        checked_out = None

        async def page_factory():
            nonlocal checked_out
            checked_out = await manager.get_context()
            return page

        async def operation(_page, _response):
            return None

        async def cleanup(navigated_page):
            await close_page_and_release_context(
                navigated_page,
                checked_out,
                manager,
                timeout_seconds=0.01,
            )

        with pytest.raises(TimeoutError, match="Timed out cleaning"):
            await asyncio.wait_for(
                controller.run_navigation(
                    page_factory,
                    "https://www.kleinanzeigen.de/s-test",
                    operation,
                    cleanup=cleanup,
                ),
                timeout=0.2,
            )

        assert (await controller.status())["active"] == 0
        assert manager.get_performance_metrics()["contexts_in_use"] == 0
        await controller.close()

    run(scenario())


def test_context_options_and_configured_concurrency_are_stable():
    async def scenario():
        browser = FakeBrowser()
        manager = OptimizedPlaywrightManager(max_contexts=1, max_concurrent=3)
        manager._browser = browser

        assert manager.max_concurrent == 3
        context = await manager.get_context()
        assert browser.new_context_calls[-1]["service_workers"] == "block"
        assert "user_agent" in browser.new_context_calls[-1]
        await manager.release_context(context)

        simple_manager = PlaywrightManager()
        simple_manager._browser = browser
        simple_page = await simple_manager.new_context_page()
        simple_context = browser.contexts[-1]
        assert browser.new_context_calls[-1]["service_workers"] == "block"
        await simple_manager.close_page(simple_page)
        assert simple_page.closed
        assert simple_context.closed

    run(scenario())


def test_simple_manager_closes_context_when_page_close_fails():
    async def scenario():
        manager = PlaywrightManager()
        page = FakePage(fail_close=True)
        context = FakeContext()
        page._context_ref = context

        with pytest.raises(RuntimeError, match="page close failed"):
            await manager.close_page(page)

        assert context.closed

    run(scenario())


def test_optimized_manager_shutdown_is_best_effort_for_dead_driver():
    async def scenario():
        pooled = FakeContext(fail_close=True)
        checked_out = FakeContext()
        browser = FakeBrowser(fail_close=True)
        playwright = FakePlaywright(fail_stop=True)
        manager = OptimizedPlaywrightManager(max_contexts=2, max_concurrent=1)
        manager._context_pool = [pooled]
        manager._context_in_use = [checked_out]
        manager._browser = browser
        manager._playwright = playwright

        await manager.close()

        assert pooled.close_calls == 1
        assert checked_out.close_calls == 1
        assert browser.close_calls == 1
        assert playwright.stop_calls == 1
        assert manager._context_pool == []
        assert manager._context_in_use == []
        assert manager._browser is None
        assert manager._playwright is None

    run(scenario())
