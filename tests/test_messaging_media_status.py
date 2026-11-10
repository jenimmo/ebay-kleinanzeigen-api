"""HTTP media blocks must open the shared circuit instead of becoming placeholders."""

from __future__ import annotations

import base64
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import messaging.browser as browser
from messaging.browser import MessagingBrowserAdapter
from messaging.contracts import MessagingError
from utils.outbound_control import (
    OutboundController,
    OutboundSettings,
    UpstreamBlocked,
    UpstreamRateLimited,
)


ACCOUNT = {"account_id": "own-a", "user_id": "111"}
IMAGE = {
    "url": "https://img.kleinanzeigen.de/private-image?token=DO_NOT_LOG",
    "remote_id": "remote-1",
}


def page_with_response(response):
    return SimpleNamespace(
        url=browser.INBOX_URL,
        evaluate=AsyncMock(return_value=response),
        query_selector=AsyncMock(return_value=None),
        title=AsyncMock(return_value="Kleinanzeigen Nachrichten"),
        locator=lambda selector: SimpleNamespace(
            inner_text=AsyncMock(return_value="Normaler Nachrichtenverlauf")
        ),
    )


@pytest.mark.parametrize(
    ("status", "error_type", "reason", "metric"),
    [
        (403, UpstreamBlocked, "http_403", "upstream_403"),
        (429, UpstreamRateLimited, "http_429", "upstream_429"),
    ],
)
async def test_media_http_block_opens_circuit_with_retry_after(
    tmp_path, monkeypatch, caplog, status, error_type, reason, metric
):
    monkeypatch.setattr(browser, "validate_public_https_url", AsyncMock())
    controller = OutboundController(OutboundSettings(cooldown_seconds=1))
    adapter = MessagingBrowserAdapter(tmp_path, controller)
    page = page_with_response(
        {"status": status, "retry_after": "73", "error": "image_unavailable"}
    )
    try:
        with pytest.raises(error_type) as caught:
            await adapter._download_image(page, IMAGE)
        assert caught.value.retry_after_seconds == 73
        state = await controller.status()
        assert state["circuit"]["state"] == "open"
        assert state["circuit"]["reason"] == reason
        assert state["metrics"][metric] == 1
        page.evaluate.assert_awaited_once()
        assert "DO_NOT_LOG" not in caplog.text
        assert IMAGE["url"] not in str(caught.value)
    finally:
        await controller.close()


@pytest.mark.parametrize(
    ("status", "error_type"), [(403, UpstreamBlocked), (429, UpstreamRateLimited)]
)
async def test_get_conversation_never_turns_http_blocks_into_missing_images(
    tmp_path, monkeypatch, status, error_type
):
    monkeypatch.setattr(browser, "validate_public_https_url", AsyncMock())
    controller = OutboundController(OutboundSettings(cooldown_seconds=1))
    adapter = MessagingBrowserAdapter(tmp_path, controller)
    page = page_with_response(
        {"status": status, "retry_after": "45", "error": "image_unavailable"}
    )
    adapter._identity = AsyncMock(return_value={"user_id": "111"})
    adapter._open_conversation = AsyncMock()
    adapter._capabilities = AsyncMock()
    selected = [
        {
            "message_id": "m1",
            "sender_user_id": "222",
            "text": "Text bleibt nur bei einzelnen Medienfehlern verfügbar.",
            "images": [IMAGE],
        }
    ]
    adapter._read_page = AsyncMock(
        return_value=(
            {"recipient_user_id": "222", "more": False},
            deepcopy(selected),
            None,
            False,
        )
    )

    async def navigate(account, url, operation, **kwargs):
        return await operation(page, None)

    adapter._navigate = navigate
    try:
        with pytest.raises(error_type):
            await adapter.get_conversation(ACCOUNT, "conversation-1")
        assert (await controller.status())["circuit"]["state"] == "open"
        adapter._identity.assert_awaited_once()
        adapter._capabilities.assert_not_awaited()
    finally:
        await controller.close()


async def test_missing_media_404_still_preserves_chat_without_opening_circuit(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(browser, "validate_public_https_url", AsyncMock())
    controller = OutboundController(OutboundSettings(cooldown_seconds=1))
    adapter = MessagingBrowserAdapter(tmp_path, controller)
    page = page_with_response(
        {"status": 404, "retry_after": "", "error": "image_unavailable"}
    )
    adapter._identity = AsyncMock(return_value={"user_id": "111"})
    adapter._open_conversation = AsyncMock()
    adapter._capabilities = AsyncMock(return_value={})
    selected = [
        {
            "message_id": "m1",
            "sender_user_id": "222",
            "text": "Grüße bleiben lesbar.",
            "images": [IMAGE],
        }
    ]
    adapter._read_page = AsyncMock(
        return_value=(
            {"recipient_user_id": "222", "more": False},
            deepcopy(selected),
            None,
            False,
        )
    )

    async def navigate(account, url, operation, **kwargs):
        return await operation(page, None)

    adapter._navigate = navigate
    try:
        result = await adapter.get_conversation(ACCOUNT, "conversation-1")
        assert result["messages"][0]["text"] == selected[0]["text"]
        assert result["messages"][0]["images"][0]["unavailable"] is True
        assert (await controller.status())["circuit"]["state"] == "closed"
        assert adapter._identity.await_count == 2
    finally:
        await controller.close()


async def test_successful_media_returns_only_image_content_not_response_headers(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(browser, "validate_public_https_url", AsyncMock())
    controller = OutboundController(OutboundSettings(cooldown_seconds=1))
    adapter = MessagingBrowserAdapter(tmp_path, controller)
    page = page_with_response(
        {
            "status": 200,
            "retry_after": "",
            "data": base64.b64encode(b"image-bytes").decode(),
            "mime_type": "image/png",
        }
    )
    try:
        result = await adapter._download_image(page, IMAGE)
        assert result == {
            "data": b"image-bytes",
            "mime_type": "image/png",
            "remote_id": "remote-1",
        }
        assert (await controller.status())["circuit"]["state"] == "closed"
    finally:
        await controller.close()


@pytest.mark.parametrize("status", [403, 429])
async def test_custom_noop_inspector_cannot_silence_media_block(
    tmp_path, monkeypatch, status
):
    monkeypatch.setattr(browser, "validate_public_https_url", AsyncMock())
    controller = SimpleNamespace(inspect_interaction_response=AsyncMock())
    adapter = MessagingBrowserAdapter(tmp_path, controller)
    with pytest.raises(MessagingError) as caught:
        await adapter._download_image(
            page_with_response({"status": status, "error": "image_unavailable"}), IMAGE
        )
    assert caught.value.code in {"upstream_blocked", "upstream_rate_limited"}
    inspected = controller.inspect_interaction_response.call_args.args[1]
    assert inspected.status == status
    assert inspected.headers == {}
