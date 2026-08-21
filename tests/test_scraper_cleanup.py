"""Offline regression tests for scraper resource cleanup."""

import asyncio

import pytest

import scrapers.inserat as inserat_scraper
import scrapers.inserate as listings_scraper
import scrapers.inserate_ultra_optimized as ultra_scraper
from utils.outbound_control import OutboundControlError, UpstreamRateLimited


class FakePage:
    def __init__(self, *, fail_close: bool):
        self.fail_close = fail_close
        self.close_calls = 0

    async def close(self):
        self.close_calls += 1
        if self.fail_close:
            raise RuntimeError("page close failed")


class FakeContext:
    def __init__(self, page: FakePage):
        self.page = page

    async def new_page(self):
        return self.page


class FakeBrowserManager:
    max_concurrent = 1

    def __init__(self, page: FakePage, *, fail_release: bool = False):
        self.context = FakeContext(page)
        self.fail_release = fail_release
        self.release_calls = 0

    async def get_context(self):
        return self.context

    async def release_context(self, context):
        assert context is self.context
        self.release_calls += 1
        if self.fail_release:
            raise RuntimeError("context release failed")

    async def execute_with_semaphore(self, coroutine):
        return await coroutine

    def get_performance_metrics(self):
        return {"contexts_in_use": 0, "contexts_in_pool": 1}


def _run_detail_fetch(monkeypatch, outbound_controller, *, fail_close: bool):
    async def fake_details(_url, page_factory, _outbound_controller, **_kwargs):
        await page_factory()
        return {"id": "3399373623", "status": "active"}

    monkeypatch.setattr(inserat_scraper, "get_inserate_details", fake_details)
    page = FakePage(fail_close=fail_close)
    manager = FakeBrowserManager(page)
    result = asyncio.run(
        inserat_scraper.get_inserate_details_optimized(
            manager,
            outbound_controller,
            "3399373623",
            retry_count=0,
        )
    )
    return result, manager, page


def test_detail_scraper_releases_context_when_page_close_fails(
    monkeypatch, outbound_controller
):
    result, manager, page = _run_detail_fetch(
        monkeypatch, outbound_controller, fail_close=True
    )

    assert result["success"] is False
    assert page.close_calls == 1
    assert manager.release_calls == 1


def test_detail_scraper_success_path_still_closes_and_releases(
    monkeypatch, outbound_controller
):
    result, manager, page = _run_detail_fetch(
        monkeypatch, outbound_controller, fail_close=False
    )

    assert result["success"] is True
    assert result["data"]["id"] == "3399373623"
    assert page.close_calls == 1
    assert manager.release_calls == 1


@pytest.mark.parametrize(
    "primary_error",
    [
        UpstreamRateLimited("upstream rate limited", retry_after=42),
        OutboundControlError("outbound controller rejected navigation"),
    ],
    ids=["upstream-rate-limited", "generic-outbound-control"],
)
def test_detail_cleanup_errors_do_not_mask_outbound_error(
    monkeypatch, outbound_controller, primary_error
):
    async def fake_details(_url, page_factory, _outbound_controller, **_kwargs):
        await page_factory()
        raise primary_error

    monkeypatch.setattr(inserat_scraper, "get_inserate_details", fake_details)
    page = FakePage(fail_close=True)
    manager = FakeBrowserManager(page, fail_release=True)

    with pytest.raises(type(primary_error)) as caught:
        asyncio.run(
            inserat_scraper.get_inserate_details_optimized(
                manager,
                outbound_controller,
                "3399373623",
                retry_count=0,
            )
        )

    assert caught.value is primary_error
    assert page.close_calls == 1
    assert manager.release_calls == 1


class FakeSearchPage(FakePage):
    async def goto(self, *_args, **_kwargs):
        return None

    async def wait_for_load_state(self, *_args, **_kwargs):
        return None

    async def wait_for_selector(self, *_args, **_kwargs):
        return None


def test_legacy_search_scraper_releases_context_when_page_close_fails(
    monkeypatch, outbound_controller
):
    async def fake_ads(_page):
        return []

    monkeypatch.setattr(listings_scraper, "get_ads", fake_ads)
    page = FakeSearchPage(fail_close=True)
    manager = FakeBrowserManager(page)

    results, metrics = asyncio.run(
        listings_scraper.optimized_fetch_page(
            manager,
            outbound_controller,
            "https://www.kleinanzeigen.de/s-test",
            page_num=1,
            retry_count=0,
        )
    )

    assert results == []
    assert metrics.success is False
    assert page.close_calls == 1
    assert manager.release_calls == 1


def test_ultra_search_scraper_releases_context_when_page_close_fails(
    monkeypatch, outbound_controller
):
    async def fake_install_safe_page_routing(_page):
        return None

    async def scenario():
        page = FakeSearchPage(fail_close=True)
        manager = FakeBrowserManager(page)
        scraper = ultra_scraper.UltraOptimizedScraper(manager, outbound_controller)

        async def fake_extract_ads(_page):
            return []

        scraper.extract_ads_optimized = fake_extract_ads
        monkeypatch.setattr(
            ultra_scraper,
            "install_safe_page_routing",
            fake_install_safe_page_routing,
        )
        try:
            with pytest.raises(RuntimeError, match="page close failed"):
                await scraper.ultra_optimized_fetch_page(
                    "https://www.kleinanzeigen.de/s-test",
                    page_num=1,
                    retry_count=0,
                )
        finally:
            await scraper.cleanup()
        return manager, page

    manager, page = asyncio.run(scenario())

    assert page.close_calls == 1
    assert manager.release_calls == 1
