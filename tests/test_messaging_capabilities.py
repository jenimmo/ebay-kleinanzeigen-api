"""Capabilities expose observed evidence, never untested features as absent."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from messaging.contracts import MessagingSettings
from messaging.service import MessagingService, _public_capabilities


def test_unverified_and_missing_capabilities_are_not_false():
    for raw in (
        None,
        {},
        {"can_send_text": False, "can_send_images": False},
        {"text_status": "supported", "can_send_text": False},
    ):
        result = _public_capabilities(raw)
        assert result["text_status"] == "unverified"
        assert result["can_send_text"] is None
        assert result["can_send_images"] is None


def test_capabilities_whitelist_preserves_explicit_evidence_and_rejects_raw_data():
    result = _public_capabilities(
        {
            "scope": "PRIVATE",
            "text_status": "supported",
            "can_send_text": True,
            "images_status": "unsupported",
            "can_send_images": False,
            "first_contact_images_status": "unverified",
            "first_contact_images": False,
            "max_text_length": 4000,
            "text_limit_source": "form",
            "accepted_image_types": [
                "image/*",
                "image/png",
                ".jpg",
                "image/png",
                "PRIVATE",
            ],
            "image_send_mode": "explicit_submit",
            "max_images": 20,
            "message": "PRIVATE",
            "cookies": "PRIVATE",
            "image_limitation": "PRIVATE",
        }
    )
    assert result["scope"] == "current_page"
    assert result["can_send_text"] is True
    assert result["can_send_images"] is False
    assert result["first_contact_images"] is None
    assert result["max_text_length"] == 4000
    assert result["accepted_image_types"] == ["image/*", "image/png", ".jpg"]
    assert "image_send_mode" not in result
    assert "max_images" not in result
    assert "PRIVATE" not in str(result)


def test_malformed_capabilities_do_not_break_login():
    result = _public_capabilities(
        {
            "text_status": "supported",
            "can_send_text": True,
            "max_text_length": True,
            "text_limit_source": [],
            "images_status": "supported",
            "can_send_images": True,
            "image_send_mode": {},
            "accepted_image_types": [{"token": "secret"}],
        }
    )
    assert "max_text_length" not in result
    assert "image_send_mode" not in result
    assert result["accepted_image_types"] == []


@pytest.mark.parametrize("source", ["form", "website"])
def test_observed_chat_image_limit_is_preserved_separately(source):
    result = _public_capabilities(
        {
            "images_status": "supported",
            "can_send_images": True,
            "max_images_per_message": 5,
            "image_limit_source": source,
        }
    )
    assert result["max_images_per_message"] == 5
    assert result["image_limit_source"] == source


@pytest.mark.parametrize(
    "changes",
    [
        {"images_status": "unverified"},
        {"can_send_images": False},
        {"max_images_per_message": True},
        {"max_images_per_message": 0},
        {"max_images_per_message": 21},
        {"image_limit_source": "listing_images"},
        {"image_limit_source": []},
    ],
)
def test_unverified_or_malformed_image_limits_are_not_published(changes):
    result = _public_capabilities(
        {
            "images_status": "supported",
            "can_send_images": True,
            "max_images_per_message": 5,
            "image_limit_source": "website",
            **changes,
        }
    )
    assert "max_images_per_message" not in result
    assert "image_limit_source" not in result


async def test_connect_persists_sanitized_capabilities_in_account_and_operation(
    tmp_path,
):
    identity = {
        "user_id": "111",
        "capabilities": {
            "text_status": "supported",
            "can_send_text": True,
            "images_status": "unverified",
            "can_send_images": False,
            "token": "PRIVATE",
        },
    }
    adapter = SimpleNamespace(
        connect=AsyncMock(return_value=identity), close=AsyncMock()
    )
    service = MessagingService(MessagingSettings(root=tmp_path), adapter)
    await service.start()
    try:
        operation = await service.connect_account("own")
        async with asyncio.timeout(2):
            while True:
                finished = await service.get_operation("own", operation["operation_id"])
                if finished["state"] != "waiting_for_login":
                    break
                await asyncio.sleep(0)
        assert finished["state"] == "connected"
        assert finished["capabilities"]["can_send_text"] is True
        assert finished["capabilities"]["can_send_images"] is None
        account = (await service.list_accounts())["accounts"][0]
        assert account["capabilities"] == finished["capabilities"]
        assert "PRIVATE" not in str(account)
        assert "PRIVATE" not in str(finished)
    finally:
        await service.close()


@pytest.mark.parametrize(
    "caps", [None, {"text_status": "unverified", "can_send_text": False}]
)
async def test_reconnect_replaces_stale_capability_claims(tmp_path, caps):
    adapter = SimpleNamespace(
        connect=AsyncMock(return_value={"user_id": "111", "capabilities": caps}),
        close=AsyncMock(),
    )
    service = MessagingService(MessagingSettings(root=tmp_path), adapter)
    await service.start()
    try:
        await service.store.save_account(
            {
                "account_id": "own",
                "user_id": "111",
                "state": "connected",
                "capabilities": {"can_send_text": True},
            }
        )
        operation = await service.connect_account("own")
        await service._connect_tasks["own"][1]
        finished = await service.get_operation("own", operation["operation_id"])
        assert finished["capabilities"]["can_send_text"] is None
        assert (await service.store.account("own"))["capabilities"] == finished[
            "capabilities"
        ]
    finally:
        await service.close()


async def test_conversation_capabilities_remain_unverified_live_and_after_restart(
    tmp_path,
):
    raw = {
        "conversation_id": "own-chat",
        "recipient_user_id": "222",
        "messages": [],
        "capabilities": {
            "text_status": "unverified",
            "can_send_text": False,
            "images_status": "supported",
            "can_send_images": True,
            "token": "PRIVATE",
        },
    }
    adapter = SimpleNamespace(
        get_conversation=AsyncMock(return_value=raw), close=AsyncMock()
    )
    service = MessagingService(MessagingSettings(root=tmp_path), adapter)
    await service.start()
    try:
        await service.store.save_account(
            {"account_id": "own", "user_id": "111", "state": "connected"}
        )
        live = await service.get_conversation("own", "own-chat")
        assert live["capabilities"]["can_send_text"] is None
        assert live["capabilities"]["can_send_images"] is True
        assert "PRIVATE" not in str(live)
        assert raw["capabilities"]["can_send_text"] is False
    finally:
        await service.close()

    service = MessagingService(MessagingSettings(root=tmp_path), adapter)
    await service.start()
    try:
        archived = await service.get_conversation("own", "own-chat", source="archive")
        assert archived["capabilities"] == live["capabilities"]
        # Old archives from before this normalization receive the same public
        # semantics without rewriting the user's archived messages.
        await service.store.archive_conversations("own", [raw])
        legacy = await service.get_conversation("own", "own-chat", source="archive")
        summary = await service.list_conversations("own", source="archive")
        assert legacy["capabilities"] == live["capabilities"]
        assert summary["conversations"][0]["capabilities"] == live["capabilities"]
        assert "PRIVATE" not in str(legacy) + str(summary)
        adapter.get_conversation.assert_awaited_once()
    finally:
        await service.close()
