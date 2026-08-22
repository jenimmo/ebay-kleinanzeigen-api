"""Offline-Vertragstests für den HTTP-Client des MCP-Adapters."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from kleinanzeigen_mcp.client import (
    KleinanzeigenAPIError,
    KleinanzeigenJobClient,
    KleinanzeigenProtocolError,
    KleinanzeigenTransportError,
)


JOB_ID = "123e4567-e89b-12d3-a456-426614174000"


def _accepted(kind: str = "search", state: str = "queued") -> dict[str, Any]:
    return {
        "job_id": JOB_ID,
        "type": kind,
        "state": state,
        "idempotent_replay": False,
        "status_url": f"/jobs/{JOB_ID}",
        "results_url": f"/jobs/{JOB_ID}/results",
    }


def _status(state: str = "running") -> dict[str, Any]:
    return {
        "job_id": JOB_ID,
        "type": "search",
        "state": state,
        "progress": {"completed": 1, "total": 2},
        "results": {"released": 1},
        "continuation": {"required": False},
        "resume": {"required": False},
        "cooldown": {"active": False},
        "errors": {"open": 0, "last": None},
    }


@pytest.mark.asyncio
async def test_client_maps_every_job_operation_to_the_persistent_api() -> None:
    """Der Adapter bleibt eine dünne, vorhersagbare Schicht über `/jobs`."""

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        method = request.method

        if (method, path) == ("POST", "/jobs/search"):
            return httpx.Response(202, json=_accepted(), request=request)
        if (method, path) == ("POST", "/jobs/details"):
            return httpx.Response(
                202,
                json=_accepted(kind="details"),
                request=request,
            )
        if (method, path) == ("GET", f"/jobs/{JOB_ID}"):
            return httpx.Response(200, json=_status(), request=request)
        if (method, path) == ("GET", f"/jobs/{JOB_ID}/results"):
            return httpx.Response(
                200,
                json={
                    "job_id": JOB_ID,
                    "type": "search",
                    "items": [
                        {
                            "sequence": 1,
                            "listing_id": "111",
                            "status": "complete",
                            "data": {"title": "Rad"},
                        }
                    ],
                    "next_cursor": "cursor-2",
                    "has_more": True,
                },
                request=request,
            )
        if (method, path) == ("POST", f"/jobs/{JOB_ID}/continue"):
            return httpx.Response(
                202,
                json=_accepted(state="queued"),
                request=request,
            )
        if (method, path) == ("POST", f"/jobs/{JOB_ID}/resume"):
            return httpx.Response(
                202,
                json=_accepted(state="queued"),
                request=request,
            )
        if (method, path) == ("DELETE", f"/jobs/{JOB_ID}"):
            return httpx.Response(
                200,
                json=_accepted(state="cancelled"),
                request=request,
            )
        raise AssertionError(f"unerwarteter Request: {method} {request.url}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = KleinanzeigenJobClient(
            "https://backend.example/",
            headers={"X-Adapter": "contract-test"},
            client=http_client,
        )

        search = await client.create_search_job(
            {"query": "Fahrrad", "location": "Berlin", "radius": 10},
            idempotency_key="agent.search-1",
        )
        details = await client.create_detail_job(
            listing_ids=["111", "222"],
            idempotency_key="agent.details-1",
        )
        status = await client.get_job(JOB_ID)
        page = await client.get_results(JOB_ID, cursor="cursor-1", limit=37)
        continued = await client.continue_job(
            JOB_ID,
            "continue-token",
            after_cursor="cursor-2",
        )
        resumed = await client.resume_job(JOB_ID, "resume-token")
        cancelled = await client.cancel_job(JOB_ID)

    assert search.job_id == JOB_ID
    assert details.type == "details"
    assert status.state == "running"
    assert page.items[0].listing_id == "111"
    assert page.next_cursor == "cursor-2"
    assert continued.state == "queued"
    assert resumed.state == "queued"
    assert cancelled.state == "cancelled"

    assert [(request.method, request.url.path) for request in seen] == [
        ("POST", "/jobs/search"),
        ("POST", "/jobs/details"),
        ("GET", f"/jobs/{JOB_ID}"),
        ("GET", f"/jobs/{JOB_ID}/results"),
        ("POST", f"/jobs/{JOB_ID}/continue"),
        ("POST", f"/jobs/{JOB_ID}/resume"),
        ("DELETE", f"/jobs/{JOB_ID}"),
    ]
    assert json.loads(seen[0].content) == {
        "query": "Fahrrad",
        "location": "Berlin",
        "radius": 10,
    }
    assert seen[0].headers["Idempotency-Key"] == "agent.search-1"
    assert seen[0].headers["X-Adapter"] == "contract-test"
    assert json.loads(seen[1].content) == {"listing_ids": ["111", "222"]}
    assert seen[1].headers["Idempotency-Key"] == "agent.details-1"
    assert dict(seen[3].url.params) == {"cursor": "cursor-1", "limit": "37"}
    assert json.loads(seen[4].content) == {
        "continuation_token": "continue-token",
        "after_cursor": "cursor-2",
    }
    assert json.loads(seen[5].content) == {"resume_token": "resume-token"}


@pytest.mark.asyncio
async def test_continue_keeps_legacy_body_when_after_cursor_is_omitted() -> None:
    seen_body: dict[str, Any] | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_body
        seen_body = json.loads(request.content)
        return httpx.Response(
            202,
            json=_accepted(state="queued"),
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KleinanzeigenJobClient("https://backend.example", client=http)
        await client.continue_job(JOB_ID, "continue-token")

    assert seen_body == {"continuation_token": "continue-token"}


@pytest.mark.asyncio
async def test_backend_error_preserves_safe_machine_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            headers={"Retry-After": "17"},
            json={
                "detail": {
                    "error": "continuation_required",
                    "message": "Continuation must be authorized explicitly",
                }
            },
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KleinanzeigenJobClient("https://backend.example", client=http)
        with pytest.raises(KleinanzeigenAPIError) as raised:
            await client.continue_job(JOB_ID, "stale-token")

    error = raised.value
    assert error.status_code == 409
    assert error.code == "continuation_required"
    assert error.message == "Continuation must be authorized explicitly"
    assert error.retry_after_seconds == 17
    assert error.to_dict() == {
        "code": "continuation_required",
        "message": "Continuation must be authorized explicitly",
        "retryable": error.retryable,
        "retry_after_seconds": 17,
        "status_code": 409,
    }


@pytest.mark.asyncio
async def test_transport_failure_is_wrapped_without_leaking_request_secrets() -> None:
    secret = "agent-secret-idempotency-key"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KleinanzeigenJobClient("https://backend.example", client=http)
        with pytest.raises(KleinanzeigenTransportError) as raised:
            await client.create_search_job(
                {"query": "Fahrrad"},
                idempotency_key=secret,
            )

    error = raised.value
    assert error.retryable is True
    assert error.status_code is None
    assert secret not in str(error)
    assert secret not in json.dumps(error.to_dict())


@pytest.mark.asyncio
async def test_lazy_base_url_provider_recovers_once_after_connection_failure() -> None:
    provider_calls: list[bool] = []
    requests: list[str] = []

    def provider(refresh: bool) -> str:
        provider_calls.append(refresh)
        return "http://127.0.0.1:41002" if refresh else "http://127.0.0.1:41001"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.port == 41001:
            raise httpx.ConnectError("backend exited", request=request)
        return httpx.Response(200, json=_status(), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KleinanzeigenJobClient(
            client=http,
            base_url_provider=provider,
        )
        assert provider_calls == []
        status = await client.get_job(JOB_ID)

    assert status.job_id == JOB_ID
    assert provider_calls == [False, True]
    assert requests == [
        f"http://127.0.0.1:41001/jobs/{JOB_ID}",
        f"http://127.0.0.1:41002/jobs/{JOB_ID}",
    ]


@pytest.mark.asyncio
async def test_lazy_provider_retries_transport_only_once() -> None:
    provider_calls: list[bool] = []
    request_count = 0

    def provider(refresh: bool) -> str:
        provider_calls.append(refresh)
        return "http://127.0.0.1:41001"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        raise httpx.ConnectError("still unavailable", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KleinanzeigenJobClient(client=http, base_url_provider=provider)
        with pytest.raises(KleinanzeigenTransportError) as raised:
            await client.get_job(JOB_ID)

    assert raised.value.code == "transport_error"
    assert provider_calls == [False, True]
    assert request_count == 2


@pytest.mark.asyncio
async def test_lazy_provider_does_not_retry_an_api_response() -> None:
    provider_calls: list[bool] = []

    def provider(refresh: bool) -> str:
        provider_calls.append(refresh)
        return "http://127.0.0.1:41001"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={"detail": {"error": "service_unavailable"}},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KleinanzeigenJobClient(client=http, base_url_provider=provider)
        with pytest.raises(KleinanzeigenAPIError):
            await client.get_job(JOB_ID)

    assert provider_calls == [False]


@pytest.mark.asyncio
async def test_job_creation_without_idempotency_key_is_not_retried() -> None:
    provider_calls: list[bool] = []
    request_count = 0

    def provider(refresh: bool) -> str:
        provider_calls.append(refresh)
        return "http://127.0.0.1:41001"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        raise httpx.ReadTimeout("outcome unknown", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KleinanzeigenJobClient(client=http, base_url_provider=provider)
        with pytest.raises(KleinanzeigenTransportError):
            await client.create_search_job({"query": "Fahrrad"})

    assert provider_calls == [False, True]
    assert request_count == 1


@pytest.mark.asyncio
async def test_lazy_provider_retries_idempotent_job_creation_once() -> None:
    provider_calls: list[bool] = []
    request_count = 0

    def provider(refresh: bool) -> str:
        provider_calls.append(refresh)
        return "http://127.0.0.1:41002" if refresh else "http://127.0.0.1:41001"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request.url.port == 41001:
            raise httpx.ReadTimeout("outcome unknown", request=request)
        return httpx.Response(
            202,
            json=_accepted(),
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KleinanzeigenJobClient(client=http, base_url_provider=provider)
        accepted = await client.create_search_job(
            {"query": "Fahrrad"},
            idempotency_key="autostart-retry-safe",
        )

    assert accepted.job_id == JOB_ID
    assert provider_calls == [False, True]
    assert request_count == 2


@pytest.mark.asyncio
async def test_backend_error_redacts_urls_headers_and_stacktraces() -> None:
    messages = [
        "backend http://10.0.0.4:9000/jobs failed Authorization: Bearer secret-token",
        "Traceback (most recent call last): File /srv/private.py, line 1",
    ]

    for message in messages:

        def handler(request: httpx.Request, raw: str = message) -> httpx.Response:
            return httpx.Response(
                503,
                json={"detail": {"error": "service_unavailable", "message": raw}},
                request=request,
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = KleinanzeigenJobClient("https://backend.example", client=http)
            with pytest.raises(KleinanzeigenAPIError) as raised:
                await client.get_job(JOB_ID)

        serialized = json.dumps(raised.value.to_dict())
        assert "10.0.0.4" not in serialized
        assert "secret-token" not in serialized
        assert "private.py" not in serialized


@pytest.mark.asyncio
async def test_success_with_invalid_json_is_a_protocol_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html>not json</html>",
            headers={"Content-Type": "text/html"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KleinanzeigenJobClient("https://backend.example", client=http)
        with pytest.raises(KleinanzeigenProtocolError) as raised:
            await client.get_job(JOB_ID)

    assert raised.value.status_code == 200
    assert "<html>" not in str(raised.value)
