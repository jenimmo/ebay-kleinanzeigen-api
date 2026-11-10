"""Offline adversarial checks for private messaging HTTP boundaries."""

from __future__ import annotations

import json

from fastapi import FastAPI
import httpx
import pytest

from routers.messaging import router
from utils.http_security import MessagingAccessMiddleware, RequestBodyLimitMiddleware


UPLOAD_PATH = "/messaging/accounts/own-a/attachments"
JSON_LIMIT = 64 * 1024
IMAGE_LIMIT = 20 * 1024 * 1024


class BodyReader:
    def __init__(self):
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1
        total = 0
        while True:
            message = await receive()
            total += len(message.get("body", b""))
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send(
            {
                "type": "http.response.body",
                "body": json.dumps({"bytes": total}).encode(),
            }
        )


async def invoke(
    *,
    path="/messaging/accounts",
    method="GET",
    headers=None,
    peer=("127.0.0.1", 1234),
    chunks=(),
    guard=True,
    scheme="http",
):
    reader = BodyReader()
    app = RequestBodyLimitMiddleware(
        reader, max_body_size=JSON_LIMIT, messaging_upload_max_size=IMAGE_LIMIT
    )
    if guard:
        app = MessagingAccessMiddleware(app)
    messages = []
    body = iter(chunks)
    receive_count = 0

    async def receive():
        nonlocal receive_count
        receive_count += 1
        try:
            chunk = next(body)
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def send(message):
        messages.append(message)

    await app(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": scheme,
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "client": peer,
            "server": ("127.0.0.1", 8000),
            "headers": headers
            if headers is not None
            else [(b"host", b"127.0.0.1:8000")],
        },
        receive,
        send,
    )
    status = next(
        message["status"]
        for message in messages
        if message["type"] == "http.response.start"
    )
    response = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return status, json.loads(response), reader.calls, receive_count


@pytest.mark.parametrize(
    "peer",
    [
        ("203.0.113.8", 1234),
        ("192.168.1.10", 1234),
        ("2001:db8::1", 1234),
        None,
        ("localhost", 1234),
    ],
)
async def test_private_routes_reject_remote_or_unverifiable_peers_before_body_read(
    peer,
):
    status, body, calls, reads = await invoke(
        peer=peer,
        headers=[(b"host", b"127.0.0.1:8000"), (b"x-forwarded-for", b"127.0.0.1")],
    )
    assert status == 403
    assert body["detail"]["code"] == "messaging_forbidden"
    assert calls == reads == 0


@pytest.mark.parametrize(
    ("peer", "host"),
    [
        (("127.0.0.1", 1), b"127.0.0.1:8000"),
        (("::1", 1), b"[::1]:8000"),
        (("::ffff:127.0.0.1", 1), b"localhost:8000"),
    ],
)
async def test_local_clients_with_exact_same_origin_are_allowed(peer, host):
    status, _, calls, _ = await invoke(
        peer=peer, headers=[(b"host", host), (b"origin", b"http://" + host)]
    )
    assert status == 200
    assert calls == 1


@pytest.mark.parametrize(
    "origin",
    [
        b"null",
        b"",
        b"https://evil.example",
        b"http://127.0.0.1:9000",
        b"http://localhost:8000",
        b"https://127.0.0.1:8000",
        b"http://127.0.0.1:8000/",
        b"http://127.0.0.1:8000?x=1",
        b"http://127.0.0.1:8000#fragment",
        b"http://user@127.0.0.1:8000",
    ],
)
async def test_opaque_empty_foreign_or_malformed_origins_are_rejected(origin):
    status, _, calls, reads = await invoke(
        headers=[(b"host", b"127.0.0.1:8000"), (b"origin", origin)]
    )
    assert status == 403
    assert calls == reads == 0


@pytest.mark.parametrize(
    "host",
    [
        b"evil.example:8000",
        b"127.0.0.1.evil.example",
        b"user@127.0.0.1",
        b"127.0.0.1/extra",
        b"127.0.0.1?x=1",
        b"127.0.0.1#fragment",
        b"127.0.0.1:invalid",
        b"[::1",
        b"",
        b"127.0.0.1\\evil.example",
    ],
)
async def test_host_rebinding_and_malformed_authorities_are_rejected(host):
    status, _, calls, reads = await invoke(headers=[(b"host", host)])
    assert status == 403
    assert calls == reads == 0


@pytest.mark.parametrize(
    "extra",
    [
        [(b"host", b"evil.example")],
        [(b"host", b"127.0.0.1:8000")],
        [(b"origin", b"https://evil.example"), (b"origin", b"http://127.0.0.1:8000")],
        [(b"origin", b"http://127.0.0.1:8000"), (b"origin", b"https://evil.example")],
        [(b"sec-fetch-site", b"cross-site")],
    ],
)
async def test_ambiguous_headers_and_cross_site_fetch_are_rejected(extra):
    status, _, calls, _ = await invoke(headers=[(b"host", b"127.0.0.1:8000"), *extra])
    assert status == 403
    assert calls == 0


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"host", b"evil.example"), (b"host", b"127.0.0.1:8000")],
        [
            (b"host", b"127.0.0.1:8000"),
            (b"content-type", b"text/plain"),
            (b"content-type", b"application/json"),
        ],
    ],
)
async def test_missing_host_and_duplicate_trust_headers_fail_closed(headers):
    status, _, calls, reads = await invoke(method="POST", headers=headers)
    assert status == 403
    assert calls == reads == 0


@pytest.mark.parametrize(
    "content_type",
    [
        None,
        b"text/plain",
        b"application/x-www-form-urlencoded",
        b"multipart/form-data; boundary=a",
    ],
)
async def test_account_actions_require_json(content_type):
    headers = [(b"host", b"127.0.0.1:8000")]
    if content_type:
        headers.append((b"content-type", content_type))
    status, body, calls, _ = await invoke(
        path="/messaging/accounts/own-a/disconnect",
        method="POST",
        headers=headers,
        chunks=[b"{}"],
    )
    assert status == 415
    assert body["detail"]["code"] == "json_required"
    assert calls == 0


async def test_disconnect_accepts_empty_json_and_guard_does_not_change_public_routes():
    status, body, _, _ = await invoke(
        path="/messaging/accounts/own-a/disconnect",
        method="POST",
        headers=[
            (b"host", b"127.0.0.1:8000"),
            (b"content-type", b"application/json; charset=utf-8"),
        ],
        chunks=[b"{}"],
    )
    assert status == 200
    assert body["bytes"] == 2
    status, _, calls, _ = await invoke(
        path="/jobs/search",
        method="POST",
        peer=("203.0.113.1", 1),
        headers=[(b"host", b"outside.example")],
    )
    assert status == 200
    assert calls == 1


@pytest.mark.parametrize("size", [JSON_LIMIT, JSON_LIMIT + 1])
async def test_json_64k_boundary_with_declared_body(size):
    status, body, calls, reads = await invoke(
        path="/messaging/accounts/own-a/send-message",
        method="POST",
        headers=[
            (b"host", b"127.0.0.1:8000"),
            (b"content-type", b"application/json"),
            (b"content-length", str(size).encode()),
        ],
        chunks=[b" " * size],
    )
    assert status == (200 if size == JSON_LIMIT else 413)
    if size > JSON_LIMIT:
        assert calls == reads == 0
    else:
        assert body["bytes"] == size


@pytest.mark.parametrize("declared_length", [None, b"1", b"invalid", b"-1"])
async def test_chunked_json_never_exceeds_64k_even_with_false_length(declared_length):
    headers = [(b"host", b"127.0.0.1:8000"), (b"content-type", b"application/json")]
    if declared_length is not None:
        headers.append((b"content-length", declared_length))
    status, _, _, reads = await invoke(
        path="/messaging/accounts/own-a/send-message",
        method="POST",
        headers=headers,
        chunks=[b"x" * JSON_LIMIT, b"x"],
    )
    assert status == 413
    assert reads == 2


@pytest.mark.parametrize("extra_byte", [False, True])
async def test_image_upload_20m_boundary_streamed(extra_byte):
    chunks = [b"x" * JSON_LIMIT] * (IMAGE_LIMIT // JSON_LIMIT)
    if extra_byte:
        chunks.append(b"x")
    status, body, _, _ = await invoke(
        path=UPLOAD_PATH,
        method="POST",
        headers=[(b"host", b"127.0.0.1:8000"), (b"content-type", b"image/png")],
        chunks=chunks,
    )
    assert status == (413 if extra_byte else 200)
    if not extra_byte:
        assert body["bytes"] == IMAGE_LIMIT


async def test_oversized_declared_image_is_rejected_before_stream_read():
    status, _, calls, reads = await invoke(
        path=UPLOAD_PATH,
        method="POST",
        headers=[
            (b"host", b"127.0.0.1:8000"),
            (b"content-type", b"image/jpeg"),
            (b"content-length", str(IMAGE_LIMIT + 1).encode()),
        ],
    )
    assert status == 413
    assert calls == reads == 0


@pytest.mark.parametrize(
    ("method", "path", "mime"),
    [
        ("GET", UPLOAD_PATH, b"image/png"),
        ("PUT", UPLOAD_PATH, b"image/png"),
        ("POST", UPLOAD_PATH + "/", b"image/png"),
        ("POST", "/messaging/accounts/invalid.id/attachments", b"image/png"),
        ("POST", "/messaging/other/attachments", b"image/png"),
        ("POST", UPLOAD_PATH, b"application/json"),
        ("POST", UPLOAD_PATH, b"application/octet-stream"),
    ],
)
async def test_large_body_exception_is_only_exact_image_post(method, path, mime):
    status, _, _, _ = await invoke(
        path=path,
        method=method,
        guard=False,
        headers=[(b"content-type", mime)],
        chunks=[b"x" * (JSON_LIMIT + 1)],
    )
    assert status == 413


@pytest.mark.parametrize("maximum", [0, -1, True, 1.5])
def test_upload_limit_requires_a_positive_integer(maximum):
    with pytest.raises(ValueError, match="positive integer"):
        RequestBodyLimitMiddleware(BodyReader(), messaging_upload_max_size=maximum)


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"conversation_id": "chat-1", "text": "PRIVATER NACHRICHTENTEXT"},
        {
            "conversation_id": "chat-1",
            "text": "PRIVATER NACHRICHTENTEXT",
            "attachment_ids": ["img-1", "img-1"],
            "idempotency_key": "same-key",
        },
        {
            "conversation_id": "chat-1",
            "text": "x",
            "idempotency_key": "same-key",
            "private_path": "/Users/private/secret.png",
        },
    ],
)
async def test_messaging_validation_error_does_not_echo_body(bad_payload):
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.post(
            "/messaging/accounts/own-a/send-message", json=bad_payload
        )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_argument"
    assert "PRIVATER" not in response.text
    assert "/Users/private" not in response.text
    assert '"input"' not in response.text


async def test_private_validation_handler_is_local_to_messaging_routes():
    app = FastAPI()
    app.include_router(router)

    @app.get("/public")
    async def public(value: int):
        return {"value": value}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.get("/public", params={"value": "invalid"})
        invalid_json = await client.post(
            "/messaging/accounts/own-a/send-message",
            content=b'{"text":"PRIVATER NACHRICHTENTEXT",',
            headers={"Content-Type": "application/json"},
        )
    assert isinstance(response.json()["detail"], list)
    assert invalid_json.status_code == 422
    assert invalid_json.json()["detail"]["code"] == "invalid_argument"
    assert "PRIVATER" not in invalid_json.text
