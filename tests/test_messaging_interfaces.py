"""Offline contracts for private messaging REST and both MCP client paths."""

from __future__ import annotations

import base64
import json

from fastapi import FastAPI
import httpx
from mcp import Client
import pytest

from kleinanzeigen_mcp.client import (
    KleinanzeigenClientError,
    KleinanzeigenJobClient,
    KleinanzeigenTransportError,
)
from kleinanzeigen_mcp.messaging import (
    MESSAGING_TOOL_NAMES,
    KleinanzeigenMessagingClient,
    _assert_messaging_path,
    _read_selected_image,
)
from kleinanzeigen_mcp.server import asgi_client_factory, create_mcp_server
from messaging.contracts import MessagingError
from routers.messaging import router


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aHqoAAAAASUVORK5CYII="
)


class FakeMessagingService:
    def __init__(self):
        self.calls = []

    async def list_accounts(self):
        return {
            "accounts": [
                {"account_id": "own-a", "user_id": "100", "state": "connected"}
            ]
        }

    async def connect_account(self, account_id, label=None):
        self.calls.append(("connect", account_id, label))
        return {
            "account_id": account_id,
            "operation_id": "login-1",
            "state": "waiting_for_login",
        }

    async def disconnect_account(self, account_id):
        self.calls.append(("disconnect", account_id))
        return {"account_id": account_id, "state": "disconnected"}

    async def list_conversations(self, account_id, **kwargs):
        self.calls.append(("list", account_id, kwargs))
        return {
            "account_id": account_id,
            "source": kwargs["source"],
            "items": [{"conversation_id": "chat-1"}],
            "next_cursor": None,
        }

    async def get_conversation(self, account_id, conversation_id, **kwargs):
        self.calls.append(("conversation", account_id, conversation_id, kwargs))
        if account_id != "own-a":
            raise MessagingError("not_found", "Gespräch nicht gefunden", 404)
        return {
            "account_id": account_id,
            "conversation_id": conversation_id,
            "source": kwargs["source"],
            "messages": [{"message_id": "msg-1", "text": "Grüße\nÖl und Käse"}],
        }

    async def get_message_image(self, account_id, image_id):
        self.calls.append(("image", account_id, image_id))
        return {"data": PNG, "mime_type": "image/png"}

    async def upload_image(self, account_id, data, filename):
        self.calls.append(("upload", account_id, data, filename))
        if data != PNG:
            raise MessagingError("invalid_image", "Ungültiges Bild", 415)
        return {"attachment_id": "image-1", "mime_type": "image/png"}

    async def submit_send(self, account_id, **kwargs):
        self.calls.append(("send", account_id, kwargs))
        return {"account_id": account_id, "operation_id": "send-1", "state": "queued"}

    async def get_operation(self, account_id, operation_id):
        self.calls.append(("operation", account_id, operation_id))
        return {
            "account_id": account_id,
            "operation_id": operation_id,
            "state": "outcome_unknown",
            "steps": [{"state": "outcome_unknown"}],
        }


@pytest.fixture
def service():
    return FakeMessagingService()


@pytest.fixture
def app(service):
    app = FastAPI()
    app.state.messaging_service = service
    app.include_router(router)
    return app


@pytest.fixture
async def http_client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        yield client


async def test_account_and_read_routes_keep_scope_and_source(http_client, service):
    result = await http_client.get("/messaging/accounts")
    assert result.json()["accounts"][0]["account_id"] == "own-a"
    result = await http_client.post(
        "/messaging/accounts/own-a/connect", json={"label": "Privat"}
    )
    assert result.status_code == 202
    assert result.json()["state"] == "waiting_for_login"
    result = await http_client.get(
        "/messaging/accounts/own-a/conversations",
        params={
            "source": "archive",
            "cursor": "opaque",
            "limit": 2,
            "unread_only": "true",
        },
    )
    assert result.status_code == 200
    assert service.calls[-1] == (
        "list",
        "own-a",
        {"source": "archive", "cursor": "opaque", "limit": 2, "unread_only": True},
    )
    result = await http_client.get(
        "/messaging/accounts/own-a/conversations/chat-1", params={"source": "archive"}
    )
    assert result.json()["messages"][0]["text"] == "Grüße\nÖl und Käse"
    result = await http_client.get("/messaging/accounts/own-b/conversations/chat-1")
    assert result.status_code == 404
    assert result.json()["detail"]["code"] == "not_found"
    result = await http_client.post("/messaging/accounts/own-a/disconnect")
    assert result.json()["state"] == "disconnected"


@pytest.mark.parametrize(
    "payload",
    [
        {"conversation_id": "chat-1", "text": "Hallo"},
        {"conversation_id": "chat-1", "text": "  ", "idempotency_key": "same-key"},
        {
            "conversation_id": "chat-1",
            "text": "Hallo",
            "idempotency_key": "same-key",
            "account_id": "other",
        },
        {"conversation_id": "../other", "text": "Hallo", "idempotency_key": "same-key"},
        {"conversation_id": 17, "text": "Hallo", "idempotency_key": "same-key"},
        {
            "conversation_id": "chat-1",
            "attachment_ids": ["a", "a"],
            "idempotency_key": "same-key",
        },
    ],
)
async def test_send_requires_strict_target_content_and_stable_key(
    http_client, service, payload
):
    response = await http_client.post(
        "/messaging/accounts/own-a/send-message", json=payload
    )
    assert response.status_code == 422
    assert not service.calls


async def test_start_and_reply_forward_exact_send_identity(http_client, service):
    payload = {
        "listing_id": "1234567890-123-1234",
        "text": "Ist Öl dabei?\nDanke!",
        "attachment_ids": ["image-1"],
        "idempotency_key": "stable-new-chat",
    }
    response = await http_client.post(
        "/messaging/accounts/own-a/start-conversation", json=payload
    )
    assert response.status_code == 202
    assert service.calls[-1] == (
        "send",
        "own-a",
        {
            "target_kind": "listing",
            "target_id": payload["listing_id"],
            "text": payload["text"],
            "attachment_ids": ["image-1"],
            "idempotency_key": payload["idempotency_key"],
        },
    )
    reply = {
        "conversation_id": "chat-1",
        "attachment_ids": ["image-1"],
        "idempotency_key": "stable-image-reply",
    }
    response = await http_client.post(
        "/messaging/accounts/own-a/send-message", json=reply
    )
    assert response.status_code == 202
    assert service.calls[-1][2]["target_kind"] == "conversation"
    response = await http_client.get("/messaging/accounts/own-a/operations/send-1")
    assert response.json()["state"] == "outcome_unknown"
    assert service.calls[-1] == ("operation", "own-a", "send-1")


async def test_upload_uses_image_bytes_never_backend_paths(http_client, service):
    response = await http_client.post(
        "/messaging/accounts/own-a/attachments",
        content=PNG,
        headers={"Content-Type": "image/png", "X-Filename": "test.png"},
    )
    assert response.status_code == 201
    assert response.json()["attachment_id"] == "image-1"
    assert service.calls[-1] == ("upload", "own-a", PNG, "test.png")
    response = await http_client.post(
        "/messaging/accounts/own-a/attachments", json={"path": "/etc/passwd"}
    )
    assert response.status_code == 415
    response = await http_client.post(
        "/messaging/accounts/own-a/attachments",
        content=PNG,
        headers={"Content-Type": "image/png", "X-Filename": "../test.png"},
    )
    assert response.status_code == 422
    response = await http_client.get("/messaging/accounts/own-a/images/image-1")
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"


async def test_upload_stream_limit_applies_without_global_middleware(
    http_client, monkeypatch, service
):
    monkeypatch.setattr("routers.messaging.MAX_IMAGE_BYTES", 4)

    async def chunks():
        yield b"123"
        yield b"45"

    response = await http_client.post(
        "/messaging/accounts/own-a/attachments",
        content=chunks(),
        headers={"Content-Type": "image/png"},
    )
    assert response.status_code == 413
    assert not service.calls


@pytest.mark.parametrize(
    "path",
    [
        "/jobs/123",
        "https://evil.test/messaging/accounts",
        "/messaging/accounts/../secrets",
        "/messaging/accounts/a/conversations/a%2Fb",
        "/messaging/accounts?a=b",
        "/messaging/accounts/a/images/x#fragment",
    ],
)
def test_messaging_client_path_allowlist(path):
    with pytest.raises(ValueError):
        _assert_messaging_path(path)


async def test_client_recovers_same_send_payload_only_after_backend_refresh():
    seen = []
    resolutions = []

    async def handler(request):
        seen.append((request.url.host, json.loads(request.content)))
        if len(seen) == 1:
            raise httpx.ConnectError("private URL must not leak", request=request)
        return httpx.Response(
            202, json={"operation_id": "send-1", "idempotent_replay": True}
        )

    def provider(refresh):
        resolutions.append(refresh)
        return "http://127.0.0.1:8001" if refresh else "http://127.0.0.1:8000"

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        backend = KleinanzeigenJobClient(client=transport, base_url_provider=provider)
        client = KleinanzeigenMessagingClient(backend)
        payload = {
            "text": "Grüße",
            "conversation_id": "chat-1",
            "idempotency_key": "same-key",
        }
        result = await client.json(
            "POST", "/messaging/accounts/own-a/send-message", payload=payload
        )
    assert result["idempotent_replay"] is True
    assert seen[0][1] == seen[1][1] == payload
    assert resolutions == [False, True]


async def test_connect_failure_is_not_automatically_replayed():
    seen = []

    async def handler(request):
        seen.append(request)
        raise httpx.ConnectError("sensitive input", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        backend = KleinanzeigenJobClient(
            client=transport, base_url_provider=lambda _: "http://127.0.0.1:8000"
        )
        with pytest.raises(KleinanzeigenTransportError) as error:
            await KleinanzeigenMessagingClient(backend).json(
                "POST", "/messaging/accounts/own-a/connect", payload={"label": "Privat"}
            )
    assert len(seen) == 1
    assert error.value.retryable is False
    assert "sensitive" not in str(error.value)


async def test_private_messaging_never_uses_remote_backend_or_redirect():
    seen = []

    async def handler(request):
        seen.append(request)
        return httpx.Response(
            307, headers={"Location": "https://remote.invalid/messages"}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as transport:
        remote = KleinanzeigenMessagingClient(
            KleinanzeigenJobClient("https://remote.invalid", client=transport)
        )
        with pytest.raises(KleinanzeigenClientError) as error:
            await remote.json("GET", "/messaging/accounts")
        assert error.value.code == "local_backend_required"
        assert not seen
        local = KleinanzeigenMessagingClient(
            KleinanzeigenJobClient("http://127.0.0.1", client=transport)
        )
        with pytest.raises(KleinanzeigenClientError):
            await local.json(
                "POST",
                "/messaging/accounts/own-a/send-message",
                payload={"text": "Privat", "idempotency_key": "stable-key"},
            )
    assert len(seen) == 1
    assert seen[0].url.host == "127.0.0.1"


def test_selected_image_requires_absolute_regular_non_symlink_file(tmp_path):
    path = tmp_path / "image.png"
    path.write_bytes(PNG)
    assert _read_selected_image(str(path))[0] == PNG
    with pytest.raises(ValueError):
        _read_selected_image("relative.png")
    with pytest.raises((OSError, ValueError)):
        _read_selected_image(str(tmp_path))
    link = tmp_path / "link.png"
    link.symlink_to(path)
    with pytest.raises(OSError):
        _read_selected_image(str(link))


async def test_mcp_catalog_arguments_image_and_send_contracts(app, service, tmp_path):
    server = create_mcp_server(asgi_client_factory(app))
    async with Client(server, raise_exceptions=True) as client:
        tools = (await client.list_tools()).tools
        by_name = {tool.name: tool for tool in tools}
        assert MESSAGING_TOOL_NAMES <= by_name.keys()
        for name in MESSAGING_TOOL_NAMES:
            assert by_name[name].output_schema is not None
            assert by_name[name].description
            for parameter in by_name[name].input_schema.get("properties", {}).values():
                assert parameter.get("description")
        assert by_name["get_conversation"].annotations.read_only_hint is False
        assert by_name["send_message"].annotations.read_only_hint is False
        assert by_name["send_message"].annotations.idempotent_hint is True
        accounts = await client.call_tool("list_accounts", {})
        assert accounts.structured_content["accounts"][0]["account_id"] == "own-a"
        history = await client.call_tool(
            "get_conversation",
            {"account_id": "own-a", "conversation_id": "chat-1", "source": "archive"},
        )
        assert history.structured_content["messages"][0]["text"] == "Grüße\nÖl und Käse"
        image = await client.call_tool(
            "get_message_image", {"account_id": "own-a", "image_id": "image-1"}
        )
        assert image.content[1].type == "image"
        assert base64.b64decode(image.content[1].data) == PNG
        path = tmp_path / "Bild.png"
        path.write_bytes(PNG)
        upload = await client.call_tool(
            "upload_message_images", {"account_id": "own-a", "image_paths": [str(path)]}
        )
        assert upload.structured_content["attachment_ids"] == ["image-1"]
        result = await client.call_tool(
            "send_message",
            {
                "account_id": "own-a",
                "conversation_id": "chat-1",
                "text": "Grüße",
                "attachment_ids": ["image-1"],
                "idempotency_key": "never-change",
            },
        )
        assert result.structured_content["operation_id"] == "send-1"
        assert service.calls[-1][2]["idempotency_key"] == "never-change"


async def test_mcp_argument_errors_do_not_echo_private_content(app):
    server = create_mcp_server(asgi_client_factory(app))
    async with Client(server, raise_exceptions=False) as client:
        result = await client.call_tool(
            "send_message",
            {
                "account_id": "own-a",
                "conversation_id": "chat-1",
                "text": "PRIVATER TEXT",
            },
        )
    assert result.is_error
    assert result.structured_content["error"]["code"] == "invalid_argument"
    assert "PRIVATER TEXT" not in str(result)


async def test_mcp_operation_unknown_does_not_submit_again(app, service):
    server = create_mcp_server(asgi_client_factory(app))
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "get_message_operation", {"account_id": "own-a", "operation_id": "send-1"}
        )
    assert result.structured_content["state"] == "outcome_unknown"
    assert service.calls == [("operation", "own-a", "send-1")]
