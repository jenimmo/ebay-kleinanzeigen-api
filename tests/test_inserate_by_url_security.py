"""Offline regression tests for the /inserate-by-url SSRF boundary."""

import asyncio

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import scrapers.inserate_by_url as scraper_module
import scrapers.inserate_ultra_optimized as ultra_module
from routers.inserate_by_url import InserateByUrlRequest
from scrapers.inserate_by_url import inject_page
from utils.url_security import (
    UnsafeOutboundUrlError,
    install_safe_page_routing,
    validate_public_https_url,
)


SEARCH_URL = (
    "https://www.kleinanzeigen.de/s-autos/volkswagen/klima/"
    "k0c216+autos.marke_s:volkswagen"
)
PUBLIC_IPV4 = "93.184.216.34"


async def _public_resolver(_hostname: str, port: int):
    assert port == 443
    return [PUBLIC_IPV4]


class FakeRequest:
    def __init__(self, url: str, *, navigation: bool):
        self.url = url
        self._navigation = navigation

    def is_navigation_request(self) -> bool:
        return self._navigation


class FakeRoute:
    def __init__(self):
        self.aborted = False
        self.continued = False
        self.error_code = None

    async def abort(self, error_code: str):
        self.aborted = True
        self.error_code = error_code

    async def continue_(self):
        self.continued = True


class FakePage:
    def __init__(self):
        self.pattern = None
        self.handler = None
        self.web_socket_pattern = None
        self.web_socket_handler = None

    async def route(self, pattern: str, handler):
        self.pattern = pattern
        self.handler = handler

    async def route_web_socket(self, pattern: str, handler):
        self.web_socket_pattern = pattern
        self.web_socket_handler = handler


class FakeWebSocketRoute:
    def __init__(self):
        self.closed = False
        self.code = None
        self.reason = None

    async def close(self, *, code: int, reason: str):
        self.closed = True
        self.code = code
        self.reason = reason


def _run(awaitable):
    return asyncio.run(awaitable)


def test_request_model_preserves_legitimate_search_url_and_limits_pages():
    request = InserateByUrlRequest(url=SEARCH_URL, max_pages=20)

    assert request.url == SEARCH_URL
    assert request.model_dump()["max_pages"] == 20
    assert inject_page(request.url, 2).startswith(
        "https://www.kleinanzeigen.de/s-autos/volkswagen/klima/seite:2/"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://www.kleinanzeigen.de/s-autos",
        "https://evil.example/s-autos",
        "https://www.kleinanzeigen.de.evil.example/s-autos",
        "https://www.kleinanzeigen.de@evil.example/s-autos",
        "https://user:pass@www.kleinanzeigen.de/s-autos",
        "https://www.kleinanzeigen.de:444/s-autos",
        "https://127.0.0.1/s-autos",
        "https://[::1]/s-autos",
        "https://www.kleinanzeigen.de\\@127.0.0.1/s-autos",
        "https://www.kleinanzeigen.de/\ninternal",
        "https://www.kleinanzeigen.de/\u0085internal",
        "https://www.kleinanzeigen.de/\u2028internal",
        "https://www.kleinanzeigen.de/\u202einternal",
    ],
)
def test_request_model_rejects_ssrf_url_forms(url):
    with pytest.raises(ValidationError):
        InserateByUrlRequest(url=url)


def test_request_model_rejects_oversized_url_and_non_positive_legacy_page_count():
    with pytest.raises(ValidationError):
        InserateByUrlRequest(url=SEARCH_URL + ("x" * 4096))

    for invalid_page_count in (0, -1):
        with pytest.raises(ValidationError):
            InserateByUrlRequest(url=SEARCH_URL, max_pages=invalid_page_count)

    # The field is ignored by durable jobs and therefore no longer truncates
    # accepted work at the historical 20-page boundary.
    assert (
        InserateByUrlRequest(url=SEARCH_URL, max_pages=10_000).model_dump()["max_pages"]
        == 10_000
    )


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "192.0.2.1",
        "224.0.0.1",
        "::1",
        "fc00::1",
        "fe80::1",
    ],
)
def test_dns_policy_rejects_non_global_address_classes(address):
    async def resolver(_hostname: str, _port: int):
        return [address]

    with pytest.raises(UnsafeOutboundUrlError):
        _run(
            validate_public_https_url(
                SEARCH_URL,
                require_kleinanzeigen_host=True,
                resolver=resolver,
            )
        )


def test_dns_policy_rejects_mixed_public_private_answer():
    async def resolver(_hostname: str, _port: int):
        return [PUBLIC_IPV4, "127.0.0.1"]

    with pytest.raises(UnsafeOutboundUrlError):
        _run(
            validate_public_https_url(
                SEARCH_URL,
                require_kleinanzeigen_host=True,
                resolver=resolver,
            )
        )


def test_page_guard_allows_legitimate_navigation_and_public_subresource():
    async def scenario():
        page = FakePage()
        await install_safe_page_routing(page, resolver=_public_resolver)

        navigation = FakeRoute()
        await page.handler(navigation, FakeRequest(SEARCH_URL, navigation=True))

        subresource = FakeRoute()
        await page.handler(
            subresource,
            FakeRequest(
                "https://assets.kleinanzeigen.de/assets/app.js", navigation=False
            ),
        )
        return page, navigation, subresource

    page, navigation, subresource = _run(scenario())
    assert page.pattern == "**/*"
    assert navigation.continued and not navigation.aborted
    assert subresource.continued and not subresource.aborted


def test_page_guard_blocks_external_top_level_redirect_before_continue():
    async def scenario():
        page = FakePage()
        await install_safe_page_routing(page, resolver=_public_resolver)

        initial = FakeRoute()
        await page.handler(initial, FakeRequest(SEARCH_URL, navigation=True))

        redirect = FakeRoute()
        await page.handler(
            redirect,
            FakeRequest("https://evil.example/redirect", navigation=True),
        )
        return initial, redirect

    initial, redirect = _run(scenario())
    assert initial.continued and not initial.aborted
    assert redirect.aborted and not redirect.continued
    assert redirect.error_code == "blockedbyclient"


def test_page_guard_blocks_websockets_without_connecting():
    async def scenario():
        page = FakePage()
        await install_safe_page_routing(page, resolver=_public_resolver)
        web_socket = FakeWebSocketRoute()
        await page.web_socket_handler(web_socket)
        return page, web_socket

    page, web_socket = _run(scenario())
    assert page.web_socket_pattern == "**/*"
    assert web_socket.closed
    assert web_socket.code == 1008
    assert web_socket.reason == "Outbound WebSockets are disabled"


def test_page_guard_blocks_public_third_party_and_suffix_confusion_subresources():
    async def scenario():
        page = FakePage()
        await install_safe_page_routing(page, resolver=_public_resolver)

        third_party = FakeRoute()
        await page.handler(
            third_party,
            FakeRequest("https://cdn.example/assets/app.js", navigation=False),
        )

        suffix_confusion = FakeRoute()
        await page.handler(
            suffix_confusion,
            FakeRequest(
                "https://kleinanzeigen.de.evil.example/app.js", navigation=False
            ),
        )
        return third_party, suffix_confusion

    third_party, suffix_confusion = _run(scenario())
    assert third_party.aborted and not third_party.continued
    assert suffix_confusion.aborted and not suffix_confusion.continued


def test_page_guard_rechecks_dns_and_blocks_rebinding_before_continue():
    answers = iter(([PUBLIC_IPV4], ["127.0.0.1"]))

    async def changing_resolver(_hostname: str, _port: int):
        return next(answers)

    async def scenario():
        # The request-level preflight observes a public address.
        await validate_public_https_url(
            SEARCH_URL,
            require_kleinanzeigen_host=True,
            resolver=changing_resolver,
        )

        # The Playwright guard resolves again immediately before continue_().
        page = FakePage()
        await install_safe_page_routing(page, resolver=changing_resolver)
        route = FakeRoute()
        await page.handler(route, FakeRequest(SEARCH_URL, navigation=True))
        return route

    route = _run(scenario())
    assert route.aborted and not route.continued


def test_page_guard_blocks_private_subrequest_and_non_https_request():
    async def private_resolver(_hostname: str, _port: int):
        return ["169.254.169.254"]

    async def scenario():
        page = FakePage()
        await install_safe_page_routing(page, resolver=private_resolver)

        private_request = FakeRoute()
        await page.handler(
            private_request,
            FakeRequest("https://internal.kleinanzeigen.de/latest", navigation=False),
        )

        cleartext_request = FakeRoute()
        await page.handler(
            cleartext_request,
            FakeRequest("http://public.example/app.js", navigation=False),
        )
        return private_request, cleartext_request

    private_request, cleartext_request = _run(scenario())
    assert private_request.aborted and not private_request.continued
    assert cleartext_request.aborted and not cleartext_request.continued


def test_ultra_fetch_installs_guard_before_playwright_goto(
    monkeypatch, outbound_controller
):
    events = []

    class GuardAwarePage:
        guarded = False

        async def goto(self, *_args, **_kwargs):
            assert self.guarded
            events.append("goto")

        async def wait_for_selector(self, *_args, **_kwargs):
            return None

        async def close(self):
            events.append("close")

    page = GuardAwarePage()

    class FakeContext:
        async def new_page(self):
            events.append("new_page")
            return page

    context = FakeContext()

    class FakeBrowserManager:
        async def get_context(self):
            return context

        async def release_context(self, released_context):
            assert released_context is context
            events.append("release")

    async def install_guard(guarded_page):
        assert guarded_page is page
        guarded_page.guarded = True
        events.append("guard")

    async def extract_no_ads(_page):
        return []

    monkeypatch.setattr(ultra_module, "install_safe_page_routing", install_guard)
    scraper = ultra_module.UltraOptimizedScraper.__new__(
        ultra_module.UltraOptimizedScraper
    )
    scraper.browser_manager = FakeBrowserManager()
    scraper.outbound_controller = outbound_controller
    scraper.extract_ads_optimized = extract_no_ads

    results, metrics, extras = _run(
        scraper.ultra_optimized_fetch_page(SEARCH_URL, 1, retry_count=0)
    )

    assert results == []
    assert metrics.success
    assert extras == {}
    assert events == ["new_page", "guard", "goto", "close", "release"]


def test_scrape_rejects_unsafe_dns_before_acquiring_a_browser(
    monkeypatch, outbound_controller
):
    browser_acquired = False

    async def reject_private_dns(*_args, **_kwargs):
        raise UnsafeOutboundUrlError("private address")

    async def create_scraper(_browser_manager, _outbound_controller):
        nonlocal browser_acquired
        browser_acquired = True

    monkeypatch.setattr(scraper_module, "validate_public_https_url", reject_private_dns)
    monkeypatch.setattr(
        scraper_module, "create_ultra_optimized_scraper", create_scraper
    )

    with pytest.raises(HTTPException) as exc_info:
        _run(
            scraper_module.scrape_by_url(
                object(), outbound_controller, SEARCH_URL, max_pages=1
            )
        )

    assert exc_info.value.status_code == 400
    assert not browser_acquired


def test_scrape_has_a_fixed_total_time_budget(monkeypatch, outbound_controller):
    cleanup_finished = False

    async def validate_public(*_args, **_kwargs):
        return SEARCH_URL

    class SlowFakeScraper:
        async def ultra_optimized_fetch_page(self, *_args, **_kwargs):
            await asyncio.sleep(1)

        async def cleanup(self):
            nonlocal cleanup_finished
            cleanup_finished = True

    async def create_scraper(_browser_manager, _outbound_controller):
        return SlowFakeScraper()

    monkeypatch.setattr(scraper_module, "validate_public_https_url", validate_public)
    monkeypatch.setattr(
        scraper_module, "create_ultra_optimized_scraper", create_scraper
    )
    monkeypatch.setattr(scraper_module, "SCRAPE_TOTAL_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(HTTPException) as exc_info:
        _run(
            scraper_module.scrape_by_url(
                object(), outbound_controller, SEARCH_URL, max_pages=1
            )
        )

    assert exc_info.value.status_code == 504
    assert cleanup_finished


def test_direct_scraper_call_cannot_bypass_page_limit(outbound_controller):
    with pytest.raises(HTTPException) as exc_info:
        _run(
            scraper_module.scrape_by_url(
                object(), outbound_controller, SEARCH_URL, max_pages=21
            )
        )

    assert exc_info.value.status_code == 400
