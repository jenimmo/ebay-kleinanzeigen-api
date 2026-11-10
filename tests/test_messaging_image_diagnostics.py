"""Image diagnostics reveal failed guards, never private image or message values."""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
import httpx
import pytest

from messaging.contracts import MessagingError, MessagingSettings
from messaging.diagnostics import image_structure
from messaging.safari import SafariBridge
from messaging.service import MessagingService
from routers.messaging import router
from utils.http_security import MessagingAccessMiddleware


ACCOUNT = {"account_id": "own", "user_id": "111", "state": "connected"}


def diagnostic(**overrides):
    return {
        "stage": "source_encoding",
        "fresh_document": True,
        "descriptor_present": True,
        "remote_id_matches": True,
        "sender_matches": True,
        "direction_matches": True,
        "view_verified": True,
        "attachment_count": 1,
        "message_count": 1,
        "container_count": 1,
        "image_count": 1,
        "visible": True,
        "complete": True,
        "dimensions_valid": True,
        "within_pixel_limit": True,
        "readiness_timeout": True,
        "source_kind": "blob",
        "data_mime": "missing",
        "expected_mime": "jpeg",
        **overrides,
    }


def private_diagnostic(**overrides):
    return diagnostic(
        url="https://private.invalid/PRIVATE",
        token="PRIVATE TOKEN",
        text="PRIVATE MESSAGE",
        bytes="PRIVATE BASE64",
        sha256="PRIVATE HASH",
        user_id="PRIVATE USER",
        nested={"stage": "PRIVATE"},
        **overrides,
    )


def app_for(service):
    app = FastAPI()
    app.state.messaging_service = service
    app.include_router(router)
    app.add_middleware(MessagingAccessMiddleware)
    return app


def test_image_diagnostics_copy_only_known_flags_counts_and_enums():
    value = private_diagnostic(magic_type="webp", preview_digest_matches=False)
    assert image_structure(value) == diagnostic(
        magic_type="webp", preview_digest_matches=False
    )
    assert "PRIVATE" not in json.dumps(image_structure(value))
    assert value["token"] == "PRIVATE TOKEN"  # Does not mutate the error source.


@pytest.mark.parametrize("guard", [
    "existing_modal", "opener_control", "preopen_binding", "message_changed",
    "modal_changed", "modal_structure", "thumbnail_count", "thumbnail_visibility",
    "thumbnail_order", "active_thumbnail", "nested_images", "image_containment",
    "source_match", "thumbnails_changed", "images_changed", "close_control",
    "modal_ownership", "post_identity_binding",
])
def test_viewer_guard_diagnostics_allow_only_fixed_names(guard):
    value = private_diagnostic(stage="viewer_binding", viewer_guard=guard)
    expected = diagnostic(stage="viewer_binding", viewer_guard=guard)
    assert image_structure(value) == expected
    assert "PRIVATE" not in json.dumps(image_structure(value))


@pytest.mark.parametrize("guard", [
    "PRIVATE", "active_thumbnail:PRIVATE", "https://private.invalid/PRIVATE",
    "", True, 0, None, [], {"value": "active_thumbnail"},
])
def test_viewer_guard_diagnostics_strip_unknown_values_and_types(guard):
    result = image_structure(private_diagnostic(stage="viewer_binding", viewer_guard=guard))
    assert result == diagnostic(stage="viewer_binding")
    assert "viewer_guard" not in result
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("stage", [
    "pixel_decode", "pixel_compare", "preview_persistence", "viewer_binding", "viewer_ready",
])
def test_pixel_and_viewer_diagnostics_remain_fixed_stages_and_boolean_proof(stage):
    result = image_structure({
        "stage": stage, "pixel_space_supported": True, "pixel_format_supported": True,
        "pixel_dimensions_match": False, "pixel_digest_matches": False,
        "view_verified": True, "image_count": 1,
        "pixels": "PRIVATE", "sha256": "PRIVATE", "width": 16,
    })
    assert result == {
        "stage": stage, "pixel_space_supported": True, "pixel_format_supported": True,
        "pixel_dimensions_match": False, "pixel_digest_matches": False,
        "view_verified": True, "image_count": 1,
    }
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize(
    "value",
    [None, [], "PRIVATE", True, {}, {"stage": []}, {"stage": "PRIVATE"}],
)
def test_image_diagnostics_require_a_known_fixed_guard(value):
    assert image_structure(value) is None


def test_image_diagnostics_reject_free_values_boolean_counts_and_bad_types():
    result = image_structure(
        diagnostic(
            source_kind="https://private.invalid/PRIVATE",
            data_mime="data:image/jpeg;base64,PRIVATE",
            expected_mime={},
            magic_type=[],
            message_count=True,
            image_count=3,
            container_count=-1,
            attachment_count=1.0,
            complete=1,
            dimensions_valid="true",
            preview_digest_matches="PRIVATE HASH",
            readiness_timeout=None,
        )
    )
    for key in (
        "source_kind", "data_mime", "expected_mime", "magic_type",
        "message_count", "image_count", "container_count", "attachment_count",
        "complete", "dimensions_valid", "preview_digest_matches", "readiness_timeout",
    ):
        assert key not in result
    assert result["fresh_document"] is True
    assert "PRIVATE" not in json.dumps(result)
    assert image_structure({"stage": "image_node", "image_count": 2}) == {
        "stage": "image_node", "image_count": 2
    }


async def queued_command(bridge):
    async with asyncio.timeout(2):
        while True:
            reply = await bridge.exchange({
                "version": 1, "profile_id": "default", "action": "poll"
            })
            if reply["command"]:
                return reply["command"]
            await asyncio.sleep(0)


def bound_bridge(root):
    bridge = SafariBridge(root)
    bridge._bindings["own"] = {
        "profile_id": "default", "user_id": "111", "tab_id": 1
    }
    return bridge


async def test_native_confirmation_error_is_sanitized_before_operation_persistence(tmp_path):
    bridge = bound_bridge(tmp_path)
    task = asyncio.create_task(bridge.request(ACCOUNT, "confirm_send", {}))
    command = await queued_command(bridge)
    await bridge.exchange({
        "version": 1, "profile_id": "default", "action": "result",
        "command_id": command["id"],
        "error": {
            "code": "outcome_unknown", "body": "PRIVATE", "message": "PRIVATE",
            "image_structure": private_diagnostic(
                stage="preview_digest", preview_digest_matches=False
            ),
        },
    })
    with pytest.raises(MessagingError) as caught:
        await task
    expected = diagnostic(stage="preview_digest", preview_digest_matches=False)
    assert caught.value.code == "outcome_unknown"
    assert caught.value.details == {"image_structure": expected}
    operation_error = MessagingService._error(caught.value)
    assert operation_error["code"] == "outcome_unknown"
    assert operation_error["image_structure"] == expected
    assert "PRIVATE" not in json.dumps(operation_error)
    # Service persistence independently sanitizes adapters other than this bridge.
    independently_sanitized = MessagingService._error(MessagingError(
        "outcome_unknown", "Feste Meldung", 409,
        {"image_structure": private_diagnostic()},
    ))
    assert independently_sanitized["image_structure"] == diagnostic()
    assert "PRIVATE" not in json.dumps(independently_sanitized)
    assert not bridge._commands


@pytest.mark.parametrize("valid", [True, False])
async def test_rest_error_resanitizes_image_structure_without_arbitrary_details(valid):
    supplied = private_diagnostic() if valid else {"stage": "PRIVATE"}
    service = SimpleNamespace(get_conversation=AsyncMock(side_effect=MessagingError(
        "image_unavailable", "Bild nicht verfügbar.", 409,
        {"image_structure": supplied, "Authorization": "PRIVATE", "trace": "PRIVATE"},
    )))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(service)), base_url="http://127.0.0.1"
    ) as client:
        response = await client.get("/messaging/accounts/own/conversations/conversation-1")
    assert response.status_code == 409
    expected = {"code": "image_unavailable", "message": "Bild nicht verfügbar."}
    if valid:
        expected["image_structure"] = diagnostic()
    assert response.json() == {"detail": expected}
    assert "PRIVATE" not in response.text


async def test_unavailable_image_diagnostics_cross_bridge_rest_and_durable_archive(tmp_path):
    bridge = bound_bridge(tmp_path / "bridge")

    async def read(account, conversation_id, **kwargs):
        return await bridge.request(account, "get_conversation", {
            "conversation_id": conversation_id, **kwargs
        })

    adapter = SimpleNamespace(get_conversation=read, close=AsyncMock())
    settings = MessagingSettings(root=tmp_path / "archive")
    service = MessagingService(settings, adapter)
    await service.start()
    try:
        await service.store.save_account(ACCOUNT)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_for(service)),
            base_url="http://127.0.0.1",
        ) as client:
            request = asyncio.create_task(client.get(
                "/messaging/accounts/own/conversations/conversation-1"
            ))
            command = await queued_command(bridge)
            await bridge.exchange({
                "version": 1, "profile_id": "default", "action": "result",
                "command_id": command["id"],
                "result": {
                    "account_user_id": "111", "conversation_id": "conversation-1",
                    "recipient_user_id": "222", "has_more": False, "next_cursor": None,
                    "messages": [{
                        "message_id": "message-1", "sender_user_id": "222",
                        "text": "Grüße bleiben erhalten.",
                        "images": [{
                            "remote_id": "message-1:0", "unavailable": True,
                            "error": {
                                "code": "PRIVATE", "message": "PRIVATE",
                                "image_structure": private_diagnostic(),
                            },
                        }],
                    }],
                },
            })
            response = await request
        assert response.status_code == 200
        live = response.json()
        expected = {
            "unavailable": True, "remote_id": "message-1:0",
            "error": {
                "code": "image_unavailable",
                "message": "Dieses Bild konnte nicht geladen oder archiviert werden.",
                "image_structure": diagnostic(),
            },
        }
        assert live["messages"][0]["images"] == [expected]
        assert live["messages"][0]["text"] == "Grüße bleiben erhalten."
        assert "PRIVATE" not in json.dumps(live)
        assert live["source"] == "live"
    finally:
        await service.close()

    # A fresh service reads only SQLite; no Safari or second request is involved.
    restored_adapter = SimpleNamespace(get_conversation=AsyncMock(), close=AsyncMock())
    restored = MessagingService(settings, restored_adapter)
    await restored.start()
    try:
        archived = await restored.get_conversation(
            "own", "conversation-1", source="archive"
        )
        assert archived["source"] == "archive"
        assert archived["messages"][0]["images"] == [expected]
        assert "PRIVATE" not in json.dumps(archived)
        restored_adapter.get_conversation.assert_not_awaited()
    finally:
        await restored.close()


async def test_archive_boundary_sanitizes_diagnostics_even_without_safari_bridge(tmp_path):
    raw_image = {"remote_id": "message-1:0", "error": {
        "code": "image_unavailable", "image_structure": private_diagnostic(),
        "body": "PRIVATE", "message": "PRIVATE",
    }}
    result = {
        "conversation_id": "conversation-1", "recipient_user_id": "222",
        "messages": [{"message_id": "message-1", "text": "Erhalten", "images": [raw_image]}],
    }
    adapter = SimpleNamespace(get_conversation=AsyncMock(return_value=deepcopy(result)), close=AsyncMock())
    service = MessagingService(MessagingSettings(root=tmp_path), adapter)
    await service.start()
    try:
        await service.store.save_account(ACCOUNT)
        live = await service.get_conversation("own", "conversation-1")
        assert live["messages"][0]["images"][0]["error"]["image_structure"] == diagnostic()
        assert "PRIVATE" not in json.dumps(live)
    finally:
        await service.close()
