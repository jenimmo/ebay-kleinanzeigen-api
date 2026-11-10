"""Offline routing of observed login frames and verification subresources."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from messaging.browser import install_messaging_page_routing
import utils.url_security as url_security


async def _routes(callback=None):
    handlers = {}

    async def register_http(pattern, handler):
        handlers["http"] = handler

    async def register_websocket(pattern, handler):
        handlers["websocket"] = handler

    main = SimpleNamespace(parent_frame=None)
    page = SimpleNamespace(
        main_frame=main, route=register_http, route_web_socket=register_websocket
    )
    await install_messaging_page_routing(page, on_blocked=callback)
    return page, handlers


async def _request(page, handlers, url, resource_type, navigation, subframe=False):
    frame = (
        SimpleNamespace(parent_frame=page.main_frame) if subframe else page.main_frame
    )
    request = SimpleNamespace(
        url=url,
        resource_type=resource_type,
        is_navigation_request=lambda: navigation,
        frame=frame,
    )
    route = SimpleNamespace(continue_=AsyncMock(), abort=AsyncMock())
    await handlers["http"](route, request)
    return route


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    resolver = AsyncMock(return_value=["8.8.8.8"])
    monkeypatch.setattr(url_security, "resolve_host_addresses", resolver)
    return resolver


@pytest.mark.parametrize(
    "url,resource_type,navigation,subframe",
    [
        ("https://kleinanzeigen.de/", "document", True, False),
        ("https://www.kleinanzeigen.de/", "document", True, False),
        ("https://login.kleinanzeigen.de/u/login", "document", True, False),
        ("https://dfme.kleinanzeigen.de/verification", "document", True, True),
        ("https://another.kleinanzeigen.de/frame", "document", True, True),
        ("https://h.online-metrix.net/frame", "document", True, True),
        ("https://h.online-metrix.net/pixel", "image", False, True),
        ("https://h64.online-metrix.net/script", "script", False, False),
        ("https://session.e.aa.online-metrix.net/pixel", "image", False, True),
        ("https://" + "a" * 63 + ".e.aa.online-metrix.net/pixel", "image", False, True),
    ],
)
async def test_observed_resources_are_allowed_only_through_https_public_dns(
    url, resource_type, navigation, subframe, public_dns
):
    page, handlers = await _routes()
    route = await _request(page, handlers, url, resource_type, navigation, subframe)
    route.continue_.assert_awaited_once()
    route.abort.assert_not_awaited()
    public_dns.assert_awaited_once()


@pytest.mark.parametrize(
    "url,resource_type,navigation,subframe",
    [
        ("https://dfme.kleinanzeigen.de/frame", "document", True, False),
        ("https://h.online-metrix.net/frame", "document", True, False),
        ("https://h64.online-metrix.net/script", "document", True, False),
        ("https://session.e.aa.online-metrix.net/pixel", "document", True, False),
        ("https://h64.online-metrix.net/frame", "document", True, True),
        ("https://session.e.aa.online-metrix.net/frame", "document", True, True),
        ("https://h.online-metrix.net/script", "script", False, True),
        ("https://h.online-metrix.net/fetch", "fetch", False, True),
        ("https://h64.online-metrix.net/fetch", "fetch", False, True),
        ("https://h64.online-metrix.net/image", "image", False, True),
        ("https://session.e.aa.online-metrix.net/script", "script", False, True),
        ("https://session.e.aa.online-metrix.net/fetch", "fetch", False, True),
        ("https://other.online-metrix.net/script", "script", False, True),
        ("https://one.two.e.aa.online-metrix.net/pixel", "image", False, True),
        ("https://h.online-metrix.net.evil.test/frame", "document", True, True),
        ("https://h.online-metrix.net.evil.test/pixel", "image", False, True),
        ("https://h64.online-metrix.net.evil.test/script", "script", False, True),
        ("https://h64.online-metrix-net/script", "script", False, True),
        (
            "https://session.e.aa.online-metrix.net.evil.test/pixel",
            "image",
            False,
            True,
        ),
        ("https://dfme.kleinanzeigen.de.evil.test/frame", "document", True, True),
        ("http://h.online-metrix.net/frame", "document", True, True),
        ("http://h.online-metrix.net/pixel", "image", False, True),
        ("https://h.online-metrix.net:8443/pixel", "image", False, True),
        ("https://user:secret@h.online-metrix.net/pixel", "image", False, True),
        ("https://h64.online-metrix.net:8443/script", "script", False, True),
        ("https://user:secret@h64.online-metrix.net/script", "script", False, True),
    ],
)
async def test_exceptions_never_enable_top_navigation_unobserved_types_or_lookalikes(
    url, resource_type, navigation, subframe
):
    page, handlers = await _routes()
    route = await _request(page, handlers, url, resource_type, navigation, subframe)
    route.abort.assert_awaited_once_with("blockedbyclient")
    route.continue_.assert_not_awaited()


@pytest.mark.parametrize(
    "addresses", [["127.0.0.1"], ["::1"], ["8.8.8.8", "10.0.0.1"], []]
)
@pytest.mark.parametrize(
    "url,resource_type,navigation",
    [
        ("https://dfme.kleinanzeigen.de/frame", "document", True),
        ("https://h.online-metrix.net/frame", "document", True),
        ("https://h.online-metrix.net/pixel", "image", False),
        ("https://h64.online-metrix.net/script", "script", False),
        ("https://session.e.aa.online-metrix.net/pixel", "image", False),
    ],
)
async def test_verification_exceptions_reject_private_mixed_or_missing_dns(
    addresses, url, resource_type, navigation, public_dns
):
    public_dns.return_value = addresses
    page, handlers = await _routes()
    route = await _request(
        page, handlers, url, resource_type, navigation, subframe=True
    )
    route.abort.assert_awaited_once()
    route.continue_.assert_not_awaited()


async def test_missing_frame_identity_does_not_grant_subframe_privileges():
    _, handlers = await _routes()
    request = SimpleNamespace(
        url="https://h.online-metrix.net/frame",
        resource_type="document",
        is_navigation_request=lambda: True,
    )
    route = SimpleNamespace(continue_=AsyncMock(), abort=AsyncMock())
    await handlers["http"](route, request)
    route.abort.assert_awaited_once()
    route.continue_.assert_not_awaited()


@pytest.mark.parametrize(
    "host,expected",
    [
        ("short-session.e.aa.online-metrix.net", "*.online-metrix.net"),
        ("a" * 40 + ".e.aa.online-metrix.net", "*.online-metrix.net"),
        ("b" * 40 + ".sub.external.test", "*.external.test"),
        ("c" * 40 + ".test", "<dynamic-host>"),
        ("ordinary.external.test", "ordinary.external.test"),
    ],
)
async def test_diagnostics_mask_dynamic_labels_without_publishing_session_data(
    host, expected
):
    callback = Mock()
    page, handlers = await _routes(callback)
    route = await _request(
        page,
        handlers,
        f"https://user:private-secret@{host}/private/path?session=private-token",
        "fetch",
        False,
    )
    route.abort.assert_awaited_once()
    callback.assert_called_once_with(expected, "fetch")
    assert "private" not in repr(callback.call_args_list)


async def test_verification_exceptions_do_not_authorize_websockets():
    _, handlers = await _routes()
    socket = SimpleNamespace(
        url="wss://h.online-metrix.net/socket",
        close=AsyncMock(),
        connect_to_server=Mock(),
    )
    await handlers["websocket"](socket)
    socket.close.assert_awaited_once()
    socket.connect_to_server.assert_not_called()
