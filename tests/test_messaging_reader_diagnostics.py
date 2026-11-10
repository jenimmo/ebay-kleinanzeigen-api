"""Reader errors expose shapes only, including at the private REST boundary."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
import httpx
import pytest

from messaging.contracts import MessagingError
from messaging.safari import SafariBridge, _reader_structure
from routers.messaging import router
from utils.http_security import MessagingAccessMiddleware


def diagnostic(**overrides):
    return {
        "operation": "list",
        "stage": "list_timestamp",
        "reason": "item_timestamp",
        "root_types": {"conversations": "array", "_meta": "object"},
        "meta_types": {"numFound": "number", "pageNum": "number", "pageSize": "number"},
        "meta_values": {"numFound": 21, "pageNum": 0, "pageSize": 10},
        "requested_page": 0,
        "requested_size": 10,
        "conversation_count": 10,
        "conversation_count_limited": False,
        "item_index": 2,
        "item_types": {"id": "string", "userIdBuyer": "number", "receivedDate": "string"},
        "timestamp_format": "iso_offset_compact",
        "timestamp_parseable": True,
        "timestamp_iso_zoned": False,
        **overrides,
    }


def test_reader_sanitizer_keeps_known_shapes_and_discards_all_content():
    value = diagnostic(
        body="PRIVATE MESSAGE", Authorization="PRIVATE TOKEN",
        identity="PRIVATE USER", url="https://private.invalid/secret",
    )
    value["root_types"]["PRIVATE-KEY"] = "string"
    value["item_types"]["adTitle"] = "PRIVATE TITLE"
    value["item_types"]["PRIVATE-KEY"] = "object"
    value["meta_values"]["userId"] = 111
    result = _reader_structure(value)
    assert result == diagnostic()
    assert "PRIVATE" not in json.dumps(result)
    assert "private.invalid" not in json.dumps(result)
    assert value["Authorization"] == "PRIVATE TOKEN"  # Input was not mutated.


@pytest.mark.parametrize("value", [None, [], "PRIVATE", {}, {"operation": "get"}, diagnostic(stage=[]), diagnostic(reason={}), diagnostic(stage="PRIVATE")])
def test_unknown_or_malformed_reader_diagnostics_are_not_exported(value):
    assert _reader_structure(value) is None


def test_reader_sanitizer_rejects_boolean_numbers_invalid_types_and_out_of_bounds():
    result = _reader_structure(diagnostic(
        requested_page=True, requested_size=201, conversation_count=1001, item_index=-1,
        http_status=600, json_mime="PRIVATE", timestamp_parseable=1,
        timestamp_format="2026-11-04T12:34:56Z", body_type=[],
        meta_values={"numFound": 1000001, "pageNum": False, "pageSize": 2.5},
        item_types={"id": [], "unread": "PRIVATE"},
    ))
    for field in ("requested_page", "requested_size", "conversation_count", "item_index", "http_status", "json_mime", "timestamp_parseable", "timestamp_format", "body_type"):
        assert field not in result
    assert result["meta_values"] == {}
    assert result["item_types"] == {}
    assert result["timestamp_iso_zoned"] is False
    assert "PRIVATE" not in json.dumps(result)


async def test_native_result_details_are_sanitized_before_reaching_service(tmp_path):
    bridge = SafariBridge(tmp_path)
    account = {"account_id": "own", "user_id": "111"}
    bridge._bindings["own"] = {"profile_id": "default", "user_id": "111", "tab_id": 1}
    task = asyncio.create_task(bridge.request(account, "list_conversations", {"limit": 10}))
    command = None
    for _ in range(100):
        answer = await bridge.exchange({"version": 1, "profile_id": "default", "action": "poll"})
        command = answer["command"]
        if command:
            break
        await asyncio.sleep(0)
    assert command is not None
    await bridge.exchange({
        "version": 1, "profile_id": "default", "action": "result", "command_id": command["id"],
        "error": {"code": "ui_unsupported", "message": "PRIVATE MESSAGE", "body": "PRIVATE BODY",
                  "reader_structure": diagnostic(secret="PRIVATE TOKEN")},
    })
    with pytest.raises(MessagingError) as caught:
        await task
    assert caught.value.details == {"reader_structure": diagnostic()}
    assert "PRIVATE" not in json.dumps(caught.value.to_dict())


@pytest.mark.parametrize("with_reader", [True, False])
async def test_rest_exposes_only_resanitized_reader_shapes_not_other_error_details(with_reader):
    details = {
        "Authorization": "PRIVATE TOKEN", "message_content": "PRIVATE MESSAGE",
        "identity_structure": {"text": "PRIVATE"}, "traceback": "PRIVATE PATH",
    }
    if with_reader:
        details["reader_structure"] = diagnostic(
            secret="PRIVATE TOKEN", message="PRIVATE MESSAGE", arbitrary_key="PRIVATE USER",
        )
    service = SimpleNamespace(list_conversations=AsyncMock(side_effect=MessagingError(
        "ui_unsupported", "Die Lesestruktur ist noch nicht unterstützt.", 409, details,
    )))
    app = FastAPI()
    app.state.messaging_service = service
    app.include_router(router)
    app.add_middleware(MessagingAccessMiddleware)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        reply = await client.get("/messaging/accounts/own/conversations?limit=10")
    assert reply.status_code == 409
    expected = {"code": "ui_unsupported", "message": "Die Lesestruktur ist noch nicht unterstützt."}
    if with_reader:
        expected["reader_structure"] = diagnostic()
    assert reply.json() == {"detail": expected}
    assert "PRIVATE" not in reply.text
    assert "Authorization" not in reply.text
    service.list_conversations.assert_awaited_once()


async def test_nonobject_error_details_remain_private_without_breaking_http_error():
    from fastapi import HTTPException
    from routers.messaging import _call

    failed = AsyncMock(side_effect=MessagingError("ui_unsupported", "Feste Meldung", 409, ["PRIVATE"]))
    with pytest.raises(HTTPException) as caught:
        await _call(failed())
    assert caught.value.status_code == 409
    assert caught.value.detail == {"code": "ui_unsupported", "message": "Feste Meldung"}
