"""Safari integration keeps the existing local HTTP and JSON boundaries."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
import httpx
import pytest

from messaging.contracts import MessagingSettings, validate_safari_endpoint
from messaging.safari_config import export_safari_pairing
from routers.messaging import router
from utils.http_security import MessagingAccessMiddleware, RequestBodyLimitMiddleware


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:8000/messaging/safari-bridge/exchange",
        "http://example.org:8000/messaging/safari-bridge/exchange",
        "http://127.0.0.1.example.org:8000/messaging/safari-bridge/exchange",
        "http://user@127.0.0.1:8000/messaging/safari-bridge/exchange",
        "http://127.0.0.1/messaging/safari-bridge/exchange",
        "http://127.0.0.1:8000/messaging/accounts",
        "http://127.0.0.1:8000/messaging/safari-bridge/exchange?secret=1",
        "http://127.0.0.1:8000/messaging/safari-bridge/exchange#fragment",
        "http://127.0.0.1:8000/messaging/safari-bridge/exchange\n",
    ],
)
def test_safari_config_rejects_nonlocal_or_ambiguous_destinations(endpoint):
    with pytest.raises(ValueError):
        validate_safari_endpoint(endpoint)


def test_pairing_document_is_private_and_rejects_symlink(tmp_path):
    config = {
        "version": 1,
        "endpoint": MessagingSettings().safari_endpoint,
        "token": "test-only",
    }
    path = export_safari_pairing(tmp_path, config)
    assert json.loads(path.read_text()) == config
    assert path.stat().st_mode & 0o777 == 0o600
    path.unlink()
    other = tmp_path / "untouched"
    other.write_text("preserve")
    path.symlink_to(other)
    with pytest.raises(ValueError):
        export_safari_pairing(tmp_path, config)
    assert other.read_text() == "preserve"


@pytest.fixture
def bridge_app():
    app = FastAPI()
    app.include_router(router)
    app.add_middleware(RequestBodyLimitMiddleware)
    app.add_middleware(MessagingAccessMiddleware)
    bridge = SimpleNamespace(
        authenticate=lambda token: token == "private-test-token",
        exchange=AsyncMock(return_value={"command": None}),
    )
    app.state.safari_bridge = bridge
    return app, bridge


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer incorrect"},
        {"Authorization": "Bearer private-test-token", "Origin": "http://127.0.0.1"},
        {
            "Authorization": "Bearer private-test-token",
            "Origin": "safari-web-extension://foreign",
        },
        {"Authorization": "Bearer private-test-token", "Sec-Fetch-Site": "same-origin"},
        [
            ("Authorization", "Bearer private-test-token"),
            ("Authorization", "Bearer private-test-token"),
        ],
    ],
)
async def test_bridge_requires_native_authenticated_client(bridge_app, headers):
    app, bridge = bridge_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.post(
            "/messaging/safari-bridge/exchange",
            headers=headers,
            json={"action": "poll"},
        )
    assert response.status_code == 403
    bridge.exchange.assert_not_awaited()


async def test_native_bridge_retains_json_limit(bridge_app):
    app, bridge = bridge_app
    headers = {"Authorization": "Bearer private-test-token"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        accepted = await client.post(
            "/messaging/safari-bridge/exchange",
            headers=headers,
            json={"action": "poll"},
        )
        rejected = await client.post(
            "/messaging/safari-bridge/exchange",
            headers=headers,
            json={"data": "x" * 65536},
        )
    assert accepted.status_code == 200
    assert rejected.status_code == 413
    assert bridge.exchange.await_count == 1


async def test_remote_peer_cannot_use_valid_bridge_token(bridge_app):
    app, bridge = bridge_app
    transport = httpx.ASGITransport(app=app, client=("192.0.2.9", 1234))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1"
    ) as client:
        response = await client.post(
            "/messaging/safari-bridge/exchange",
            headers={"Authorization": "Bearer private-test-token"},
            json={"action": "poll"},
        )
    assert response.status_code == 403
    bridge.exchange.assert_not_awaited()


async def test_bridge_diagnostics_are_local_read_only_and_not_cached(bridge_app):
    app, bridge = bridge_app
    bridge.status = AsyncMock(return_value={"enabled": True, "profile_count": 1})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.get("/messaging/safari-bridge/status")
        foreign = await client.get(
            "/messaging/safari-bridge/status", headers={"Origin": "https://example.org"}
        )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"enabled": True, "profile_count": 1}
    assert foreign.status_code == 403
    bridge.status.assert_awaited_once()
    bridge.exchange.assert_not_awaited()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.9", 1234)),
        base_url="http://127.0.0.1",
    ) as client:
        remote = await client.get("/messaging/safari-bridge/status")
    assert remote.status_code == 403


async def test_ui_structure_diagnostics_require_local_account_route(bridge_app):
    app, _ = bridge_app
    inspect = AsyncMock(
        return_value={"account_id": "own-a", "source": "live", "nodes": []}
    )
    app.state.messaging_service = SimpleNamespace(inspect_safari_ui=inspect)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        good = await client.get("/messaging/accounts/own-a/safari-ui")
        invalid = await client.get(
            "/messaging/accounts/own-a/safari-ui?view=execute&script=PRIVATE"
        )
        foreign = await client.get(
            "/messaging/accounts/own-a/safari-ui",
            headers={"Origin": "https://example.org"},
        )
        schema = await client.get("/openapi.json")
    assert good.status_code == 200
    assert good.headers["cache-control"] == "no-store"
    inspect.assert_awaited_once_with("own-a", view="current", listing_id=None)
    assert invalid.status_code == 422
    assert "PRIVATE" not in invalid.text
    assert foreign.status_code == 403
    assert "/messaging/accounts/{account_id}/safari-ui" not in schema.json()["paths"]


async def test_application_selects_safari_without_launching_account_chrome(
    monkeypatch, tmp_path
):
    import main
    from messaging.safari import SafariMessagingAdapter

    class PublicBrowser:
        def __init__(self, **_kwargs):
            pass

        async def start(self):
            pass

        async def close(self):
            pass

    def unexpected_chrome(*_args, **_kwargs):
        raise AssertionError("Safari account must not start Chrome")

    monkeypatch.setenv("KLEINANZEIGEN_MESSAGING_BROWSER_CHANNEL", "safari")
    monkeypatch.setenv(
        "KLEINANZEIGEN_JOB_DATABASE_PATH", str(tmp_path / "jobs.sqlite3")
    )
    monkeypatch.setenv("KLEINANZEIGEN_JOB_MIN_FREE_DISK_BYTES", "0")
    monkeypatch.setattr(main, "OptimizedPlaywrightManager", PublicBrowser)
    monkeypatch.setattr(main, "MessagingBrowserAdapter", unexpected_chrome)
    server = main.create_mcp_server(main.asgi_client_factory(main.app))
    main.create_streamable_http_app(server)
    monkeypatch.setattr(main, "mcp_http_server", server)
    async with main.lifespan(main.app):
        service = main.app.state.messaging_service
        assert isinstance(service.adapter, SafariMessagingAdapter)
        config = json.loads((service.settings.root / "safari-pairing.json").read_text())
        assert main.app.state.safari_bridge.authenticate(config["token"])
        operation = await service.connect_account("own-a")
        assert operation["state"] == "waiting_for_login"
    assert main.app.state.safari_bridge is None
