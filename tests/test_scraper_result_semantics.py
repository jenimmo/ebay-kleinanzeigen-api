"""Offline regression tests for scraper success/failure semantics."""

import asyncio
from datetime import datetime

import pytest

import scrapers.inserate_by_url as by_url_scraper
from scrapers.inserate_ultra_optimized import (
    SearchExtractionError,
    UltraOptimizedScraper,
    _parse_breadcrumb_summary,
    build_search_page_url,
)
from utils.performance import PageMetrics


class _FakeBrowserManager:
    max_concurrent = 1

    def get_performance_metrics(self):
        return {"contexts_in_use": 0, "contexts_in_pool": 1}


class _TextElement:
    def __init__(self, text: str) -> None:
        self.text = text

    async def inner_text(self) -> str:
        return self.text


class _SearchArticle:
    def __init__(self, listing_id: str, *, href: str | None = None) -> None:
        self.listing_id = listing_id
        self.href = href

    async def get_attribute(self, name: str) -> str | None:
        if name == "data-adid":
            return self.listing_id
        if name == "data-href":
            return self.href
        return None

    async def query_selector(self, selector: str):
        values = {
            "h2.text-module-begin a.ellipsis": f"Anzeige {self.listing_id}",
            "p.aditem-main--middle--price-shipping--price": "10 €",
            "p.aditem-main--middle--description": "Beschreibung",
            ".aditem-main--top--right": "",
        }
        return _TextElement(values[selector])

    async def evaluate(self, _script: str) -> str:
        return "10115 Berlin"


class _SearchResultsPage:
    def __init__(self, articles: list[_SearchArticle]) -> None:
        self.articles = articles
        self.selectors: list[str] = []

    async def query_selector_all(self, selector: str):
        self.selectors.append(selector)
        return self.articles


def _page_metrics(*, success: bool) -> PageMetrics:
    return PageMetrics(
        page_number=1,
        url="https://www.kleinanzeigen.de/s-test",
        start_time=0.0,
        end_time=0.01,
        success=success,
        retry_count=0,
        error_message=None if success else "browser unavailable",
        results_count=0,
    )


@pytest.mark.parametrize("page_success", [False, True])
def test_ultra_scraper_distinguishes_failed_from_empty_page(
    page_success, outbound_controller
):
    async def scenario():
        scraper = UltraOptimizedScraper(_FakeBrowserManager(), outbound_controller)

        async def fake_fetch(*_args, **_kwargs):
            return [], _page_metrics(success=page_success), {}

        scraper.ultra_optimized_fetch_page = fake_fetch
        try:
            return await scraper.ultra_optimized_scrape(page_count=1)
        finally:
            await scraper.cleanup()

    response = asyncio.run(scenario())

    assert response["success"] is page_success
    assert response["unique_results"] == 0
    assert response["performance_metrics"]["pages_successful"] == int(page_success)


@pytest.mark.parametrize("page_success", [False, True])
def test_url_scraper_distinguishes_failed_from_empty_page(
    monkeypatch, page_success, outbound_controller
):
    class FakeScraper:
        def __init__(self):
            self.cleanup_calls = 0

        async def ultra_optimized_fetch_page(self, *_args, **_kwargs):
            return [], _page_metrics(success=page_success), {}

        async def cleanup(self):
            self.cleanup_calls += 1

    fake_scraper = FakeScraper()

    async def fake_factory(_browser_manager, _outbound_controller):
        return fake_scraper

    monkeypatch.setattr(
        by_url_scraper,
        "create_ultra_optimized_scraper",
        fake_factory,
    )

    response = asyncio.run(
        by_url_scraper._scrape_by_url_within_budget(
            browser_manager=_FakeBrowserManager(),
            outbound_controller=outbound_controller,
            base_url="https://www.kleinanzeigen.de/s-test",
            max_pages=1,
        )
    )

    assert response["success"] is page_success
    assert response["unique_results"] == 0
    assert response["performance_metrics"]["pages_successful"] == int(page_success)
    assert fake_scraper.cleanup_calls == 1


def test_job_extraction_includes_promoted_cards_and_deduplicates_by_id(
    outbound_controller,
):
    async def scenario():
        scraper = UltraOptimizedScraper(_FakeBrowserManager(), outbound_controller)
        page = _SearchResultsPage(
            [
                _SearchArticle("3000000001", href="/s-anzeige/a/3000000001"),
                _SearchArticle("3000000002", href="/s-anzeige/b/3000000002"),
                _SearchArticle("3000000001", href="/s-anzeige/a/3000000001"),
            ]
        )
        try:
            return await scraper.extract_ads_for_job(page), page.selectors
        finally:
            await scraper.cleanup()

    listings, selectors = asyncio.run(scenario())

    assert selectors == [".ad-listitem article[data-adid]"]
    assert ":not(.is-topad)" not in selectors[0]
    assert "badge-hint-pro" not in selectors[0]
    assert [listing["adid"] for listing in listings] == [
        "3000000001",
        "3000000002",
    ]


def test_job_extraction_never_silently_drops_a_malformed_card(outbound_controller):
    async def scenario():
        scraper = UltraOptimizedScraper(_FakeBrowserManager(), outbound_controller)
        page = _SearchResultsPage([_SearchArticle("3000000001", href=None)])
        try:
            await scraper.extract_ads_for_job(page)
        finally:
            await scraper.cleanup()

    with pytest.raises(SearchExtractionError, match="missing its canonical ID or URL"):
        asyncio.run(scenario())


def test_job_search_url_and_breadcrumb_helpers_preserve_pagination_metadata():
    url = build_search_page_url(
        {
            "query": "Fahrrad",
            "location": "Berlin",
            "radius": 5,
            "min_price": 10,
            "max_price": 500,
        },
        2,
    )

    assert "/preis:10:500/s-seite:2" in url
    assert "keywords=Fahrrad" in url
    assert "locationStr=Berlin" in url
    assert "radius=5" in url
    assert _parse_breadcrumb_summary("1 - 25 von 60 Anzeigen") == (60, 3, 25)


def test_job_search_stops_pagination_after_publish_date_cutoff(
    monkeypatch, outbound_controller
):
    class FakePage:
        async def goto(self, _url, **_kwargs):
            return type("FakeResponse", (), {"status": 200, "headers": {}})()

        async def query_selector(self, selector):
            if selector == ".breadcrump-summary":
                return _TextElement("1 - 25 von 100 Anzeigen")
            return None

    class FakeContext:
        async def new_page(self):
            return FakePage()

    class FakeJobBrowserManager(_FakeBrowserManager):
        async def get_context(self):
            return FakeContext()

    async def no_op_page_routing(_page):
        return None

    async def no_op_cleanup(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "scrapers.inserate_ultra_optimized.install_safe_page_routing",
        no_op_page_routing,
    )
    monkeypatch.setattr(
        "scrapers.inserate_ultra_optimized.close_page_and_release_context",
        no_op_cleanup,
    )

    async def scenario():
        scraper = UltraOptimizedScraper(FakeJobBrowserManager(), outbound_controller)

        async def fake_extract(_page):
            return [
                {
                    "adid": "3000000001",
                    "published_at": "2026-01-01T00:00:00",
                }
            ]

        scraper.extract_ads_for_job = fake_extract
        try:
            return await scraper.execute_search_page(
                search={"min_publish_date": datetime(2099, 1, 1)},
                page_number=1,
            )
        finally:
            await scraper.cleanup()

    result = asyncio.run(scenario())

    assert result.listings == ()
    assert result.total_results == 100
    assert result.has_next_page is False
