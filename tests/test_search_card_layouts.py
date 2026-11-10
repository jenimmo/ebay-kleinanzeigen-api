"""Exercise the actual DOM queries on synthetic legacy and current search cards."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright

from scrapers.inserate_ultra_optimized import UltraOptimizedScraper


@pytest.mark.asyncio
async def test_both_card_layouts_and_organic_date(outbound_controller):
    modern = """<li data-clickable="card">
        <article data-adid="3000000001" data-href="/s-anzeige/test/3000000001-239-2457">
            <div><svg data-title="locationOutline"></svg><span>38126 Braunschweig</span>
                <span>(3 km)</span></div>
            {date}
            <div><h3>{title}</h3>
                <p>Fenster und Rahmen</p><div><p class="text-title3"> VB</p>
                    <p class="text-title3 line-through">150 €</p></div></div>
        </article></li>"""
    legacy = """<li class="ad-listitem"><article data-adid="3000000002"
            data-href="/s-anzeige/alt/3000000002-239-2457">
        <span class="aditem-main--top--left">38112 Braunschweig</span>
        <span class="aditem-main--top--right">09.11.2026</span>
        <h2 class="text-module-begin"><a class="ellipsis">Fensterputzer</a></h2>
        <p class="aditem-main--middle--description">Privat und Gewerbe</p>
        <p class="aditem-main--middle--price-shipping--price">1.200 € VB</p>
        </article></li>"""
    async with async_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("Install Playwright Chromium for the offline DOM fixture")
        browser = await playwright.chromium.launch(channel="chromium")
        scraper = UltraOptimizedScraper(
            SimpleNamespace(max_concurrent=1), outbound_controller
        )
        try:
            page = await browser.new_page()
            await page.set_content(
                "<ul id='srchrslt-adtable'>"
                + modern.format(
                    date="",
                    title='<a href="/s-anzeige/test/3000000001-239-2457">Glasreinigung</a>',
                )
                + legacy
                + modern.format(
                    date='<div><svg data-title="calendarOutline"></svg><span>10.11.2026</span></div>',
                    title='<span data-url="/s-anzeige/test/3000000001-239-2457">Glasreinigung</span>',
                )
                + "</ul><aside><article data-adid='99'>Empfehlung</article></aside>"
            )
            assert [
                row["adid"] for row in await scraper.extract_ads_optimized(page)
            ] == ["3000000001", "3000000002", "3000000001"]
            rows = await scraper.extract_ads_for_job(page)
        finally:
            await scraper.cleanup()
            await browser.close()

    assert rows == [
        {
            "adid": "3000000001",
            "url": "https://www.kleinanzeigen.de/s-anzeige/test/3000000001-239-2457",
            "title": "Glasreinigung",
            "location": "38126 Braunschweig",
            "description": "Fenster und Rahmen",
            "price": "VB",
            "published_at": "2026-11-10T00:00:00",
        },
        {
            "adid": "3000000002",
            "url": "https://www.kleinanzeigen.de/s-anzeige/alt/3000000002-239-2457",
            "title": "Fensterputzer",
            "location": "38112 Braunschweig",
            "description": "Privat und Gewerbe",
            "price": "1200",
            "published_at": "2026-11-09T00:00:00",
        },
    ]
