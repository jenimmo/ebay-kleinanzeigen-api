"""Individual media failures preserve readable chats without hiding auth failures."""

from __future__ import annotations

from copy import deepcopy
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image
from playwright.async_api import Error as PlaywrightError
import pytest

from messaging.browser import MessagingBrowserAdapter
from messaging.contracts import MessagingError, MessagingSettings
from messaging.service import MessagingService
from utils.outbound_control import UpstreamBlocked
from utils.url_security import UnsafeOutboundUrlError


ACCOUNT = {"account_id": "own-a", "user_id": "111", "state": "connected"}


def _png():
    output = io.BytesIO()
    Image.new("RGB", (3, 3), (10, 90, 30)).save(output, "PNG")
    return output.getvalue()


def _adapter(tmp_path, download):
    adapter = MessagingBrowserAdapter(tmp_path, SimpleNamespace())
    adapter._identity = AsyncMock(return_value={"user_id": "111"})
    adapter._inspect = AsyncMock()
    adapter._open_conversation = AsyncMock()
    adapter._capabilities = AsyncMock(return_value={"can_send_text": True})
    adapter._download_image = AsyncMock(side_effect=download)
    selected = [
        {
            "message_id": "m1",
            "sender_user_id": "222",
            "text": "Grüße mit verfügbarem und fehlendem Bild.",
            "images": [
                {"url": "https://img.kleinanzeigen.de/good", "remote_id": "good"},
                {"url": "https://img.kleinanzeigen.de/missing", "remote_id": "missing"},
            ],
        },
        {
            "message_id": "m2",
            "sender_user_id": "222",
            "text": "Dieser zweite Text bleibt ebenfalls erhalten.",
            "images": [],
        },
    ]

    async def read_page(*args, **kwargs):
        return (
            {"recipient_user_id": "222", "more": False},
            deepcopy(selected),
            None,
            False,
        )

    async def navigate(account, url, operation, **kwargs):
        return await operation(object(), None)

    adapter._read_page = read_page
    adapter._navigate = navigate
    return adapter


async def test_download_failure_preserves_text_and_good_image_in_durable_archive(
    tmp_path,
):
    good_bytes = _png()

    async def download(page, image):
        if image["remote_id"] == "missing":
            raise PlaywrightError(
                "Page.evaluate: TypeError: Failed to fetch https://private.invalid/token"
            )
        return {
            "remote_id": image["remote_id"],
            "data": good_bytes,
            "mime_type": "image/png",
        }

    adapter = _adapter(tmp_path, download)
    service = MessagingService(MessagingSettings(root=tmp_path / "archive"), adapter)
    await service.start()
    try:
        await service.store.save_account(ACCOUNT)
        live = await service.get_conversation("own-a", "conversation-1")
        assert len(live["messages"]) == 2
        images = live["messages"][0]["images"]
        assert images[0]["image_id"]
        assert images[1] == {
            "unavailable": True,
            "remote_id": "missing",
            "error": {
                "code": "image_unavailable",
                "message": "Dieses Bild konnte nicht geladen oder archiviert werden.",
            },
        }
        assert "private.invalid" not in json.dumps(live)
        stored = await service.get_conversation(
            "own-a", "conversation-1", source="archive"
        )
        assert [message["text"] for message in stored["messages"]] == [
            message["text"] for message in live["messages"]
        ]
        assert stored["messages"][0]["images"] == images
        restored = await service.get_message_image("own-a", images[0]["image_id"])
        assert restored["data"] == good_bytes
        assert (
            adapter._identity.await_count == 2
        )  # initial identity + after fetch failure
        adapter._inspect.assert_awaited_once()
    finally:
        await service.close()


@pytest.mark.parametrize(
    "failure,expected_code",
    [
        (UnsafeOutboundUrlError("Private URL detail"), "image_url_unavailable"),
        (PlaywrightError("Page.evaluate: Error: image_too_large"), "image_too_large"),
        (PlaywrightError("Page.evaluate: Error: invalid_image"), "invalid_image"),
        (
            PlaywrightError("Page.evaluate: AbortError: The operation was aborted"),
            "image_unavailable",
        ),
        (MessagingError("unsupported_image", "private detail"), "unsupported_image"),
    ],
)
async def test_only_known_individual_media_failures_become_unavailable(
    tmp_path, failure, expected_code
):
    async def download(page, image):
        if image["remote_id"] == "missing":
            raise failure
        return {
            "remote_id": image["remote_id"],
            "data": _png(),
            "mime_type": "image/png",
        }

    adapter = _adapter(tmp_path, download)
    result = await adapter.get_conversation(ACCOUNT, "conversation-1")
    missing = result["messages"][0]["images"][1]
    assert missing["unavailable"] is True
    assert missing["error"]["code"] == expected_code
    assert "private" not in missing["error"]["message"].lower()


@pytest.mark.parametrize(
    "failure",
    [
        MessagingError("reauth_required", "private detail", 409),
        MessagingError("account_mismatch", "private detail", 409),
        MessagingError("upstream_blocked", "private detail", 403),
        UpstreamBlocked("private detail"),
        PlaywrightError(
            "Page.evaluate: Target page, context or browser has been closed"
        ),
        PlaywrightError("Page.evaluate: Execution context was destroyed"),
        RuntimeError("Unrelated programming failure"),
    ],
)
async def test_auth_block_account_and_unknown_failures_propagate_unchanged(
    tmp_path, failure
):
    adapter = _adapter(tmp_path, AsyncMock(side_effect=failure))
    with pytest.raises(type(failure)) as caught:
        await adapter.get_conversation(ACCOUNT, "conversation-1")
    assert caught.value is failure


@pytest.mark.parametrize("failure_stage", ["inspect", "identity"])
async def test_media_fallback_rechecks_access_and_identity(tmp_path, failure_stage):
    failure = (
        UpstreamBlocked("access blocked")
        if failure_stage == "inspect"
        else MessagingError("account_mismatch", "different account", 409)
    )
    adapter = _adapter(
        tmp_path,
        AsyncMock(
            side_effect=PlaywrightError("Page.evaluate: TypeError: Failed to fetch")
        ),
    )
    if failure_stage == "inspect":
        adapter._inspect = AsyncMock(side_effect=failure)
    else:
        adapter._identity = AsyncMock(side_effect=[{"user_id": "111"}, failure])
    with pytest.raises(type(failure)) as caught:
        await adapter.get_conversation(ACCOUNT, "conversation-1")
    assert caught.value is failure


async def test_invalid_downloaded_image_does_not_discard_readable_messages(tmp_path):
    async def download(page, image):
        return {
            "remote_id": image["remote_id"],
            "data": _png() if image["remote_id"] == "good" else b"not an image",
            "mime_type": "image/png",
        }

    service = MessagingService(
        MessagingSettings(root=tmp_path / "archive"), _adapter(tmp_path, download)
    )
    await service.start()
    try:
        await service.store.save_account(ACCOUNT)
        result = await service.get_conversation("own-a", "conversation-1")
        assert len(result["messages"]) == 2
        assert result["messages"][0]["images"][0]["image_id"]
        missing = result["messages"][0]["images"][1]
        assert missing["remote_id"] == "missing"
        assert missing["unavailable"] is True
        assert missing["error"]["code"] == "invalid_image"
        archived = await service.get_conversation(
            "own-a", "conversation-1", source="archive"
        )
        assert archived["messages"][0]["images"][1] == missing
        assert archived["messages"][1]["text"] == result["messages"][1]["text"]
    finally:
        await service.close()


@pytest.mark.parametrize(
    "failure",
    [
        MessagingError("account_not_found", "private detail", 404),
        MessagingError("unsafe_storage", "private detail", 503),
        OSError("disk failure"),
    ],
)
async def test_archival_storage_and_account_failures_are_not_media_placeholders(
    tmp_path, failure
):
    async def download(page, image):
        return {
            "remote_id": image["remote_id"],
            "data": _png(),
            "mime_type": "image/png",
        }

    service = MessagingService(
        MessagingSettings(root=tmp_path / "archive"), _adapter(tmp_path, download)
    )
    await service.start()
    try:
        await service.store.save_account(ACCOUNT)
        service.store.save_image = AsyncMock(side_effect=failure)
        with pytest.raises(type(failure)) as caught:
            await service.get_conversation("own-a", "conversation-1")
        assert caught.value is failure
    finally:
        await service.close()
