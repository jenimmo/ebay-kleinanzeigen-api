"""File-input accept rules must be checked before any potentially sending upload."""

from __future__ import annotations

import base64
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from messaging.browser import MessagingBrowserAdapter, _validate_image_accept
from messaging.contracts import MessagingError


@pytest.mark.parametrize(
    ("mime", "name", "accept"),
    [
        ("image/png", "image.png", ["image/*"]),
        ("image/gif", "image.gif", [" IMAGE/* "]),
        ("image/jpeg", "image.jpg", ["image/png", " image/JPEG "]),
        ("image/webp", "image.webp", ["image/webp"]),
        ("image/png", "image.PNG", [" .png "]),
        ("image/jpeg", "image.jpg", [".JPG"]),
        ("image/jpeg", "image.jpeg", [".jpeg"]),
        ("image/gif", "image.gif", [".png", ".GIF"]),
        ("image/png", "image.png", ["invalid-token", "image/png"]),
    ],
)
def test_accept_allows_matching_mime_extension_or_image_wildcard(mime, name, accept):
    payload = {"mimeType": mime, "name": name, "buffer": b"unchanged"}
    original = dict(payload)
    _validate_image_accept(payload, accept)
    assert payload == original


@pytest.mark.parametrize(
    ("mime", "name", "accept"),
    [
        ("image/gif", "image.gif", ["image/png", "image/jpeg"]),
        ("image/gif", "image.gif", [".png", ".jpg", ".jpeg"]),
        ("image/webp", "image.webp", ["image/png"]),
        ("image/jpeg", "image.jpg", [".jpeg"]),
        ("image/png", "image.png", ["*/*"]),
        ("image/png", "image.png", ["application/octet-stream"]),
        ("image/png", "image.png", ["image/png; charset=utf-8"]),
        ("image/png", "image.png", ["image / png", "../png", "png"]),
    ],
)
def test_accept_rejects_unlisted_formats_and_does_not_guess_aliases(mime, name, accept):
    with pytest.raises(MessagingError) as error:
        _validate_image_accept({"mimeType": mime, "name": name}, accept)
    assert error.value.code == "image_type_unsupported"
    assert error.value.status_code == 422


@pytest.mark.parametrize("accept", [None, [], ["", " \t "], "image/png", [None]])
def test_missing_empty_or_malformed_accept_is_unverified(accept):
    with pytest.raises(MessagingError) as error:
        _validate_image_accept({"mimeType": "image/png", "name": "image.png"}, accept)
    assert error.value.code == "image_type_unverified"


@pytest.mark.parametrize(
    "accept", [["image/png", "image/jpeg"], [], None, [".gif"], ["image/*"]]
)
async def test_real_send_step_checks_current_accept_before_marker_or_upload(
    tmp_path, accept
):
    # A valid 1×1 GIF with matching persisted hash is locally acceptable. The
    # platform form may still reject GIF, which must stop BEFORE file selection.
    data = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
    path = tmp_path / "staged.gif"
    path.write_bytes(data)
    image = {
        "path": str(path),
        "attachment_id": "img_1",
        "mime_type": "image/gif",
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    account = {"account_id": "own-a", "user_id": "101"}
    target = {
        "target_kind": "conversation",
        "target_id": "chat-1",
        "conversation_id": "chat-1",
        "recipient_user_id": "202",
        "account_user_id": "101",
    }
    events = []

    async def marker():
        events.append("marker")

    async def upload(payload):
        assert payload["buffer"] == data
        events.append("upload")

    input_file = SimpleNamespace(set_input_files=AsyncMock(side_effect=upload))
    previews = SimpleNamespace(
        first=SimpleNamespace(wait_for=AsyncMock()),
        evaluate_all=AsyncMock(return_value=["remote-attachment-1"]),
    )
    button = SimpleNamespace(click=AsyncMock())
    composer = SimpleNamespace(
        locator=lambda selector: (
            input_file if selector == 'input[type="file"]' else previews
        ),
        get_by_role=lambda *a, **kw: button,
    )
    page = SimpleNamespace(
        evaluate=AsyncMock(return_value={"messages": []}),
        on=lambda *a: None,
        remove_listener=lambda *a: None,
    )
    adapter = MessagingBrowserAdapter(tmp_path, SimpleNamespace())
    adapter._prepare = AsyncMock(
        return_value={
            **target,
            "capabilities": {
                "can_send_images": True,
                "image_send_mode": "auto_send",
                "accepted_image_types": accept,
            },
        }
    )
    adapter._composer = AsyncMock(return_value=composer)
    adapter._assert_composer_clean = AsyncMock()
    adapter._observed_follow_target = AsyncMock(return_value=None)
    adapter._inspect = AsyncMock()
    adapter._receipt = AsyncMock(
        return_value={
            "message_id": "sent-1",
            "conversation_id": "chat-1",
            "recipient_user_id": "202",
            "confirmed": True,
        }
    )

    async def navigate(account, url, operation):
        return await operation(page, None)

    adapter._navigate = navigate
    allowed = accept in ([".gif"], ["image/*"])
    if allowed:
        receipt = await adapter.send_step(
            account, target, "image", image=image, before_dispatch=marker
        )
        assert receipt["confirmed"] is True
        assert events == ["marker", "upload"]
        input_file.set_input_files.assert_awaited_once()
    else:
        with pytest.raises(MessagingError) as error:
            await adapter.send_step(
                account, target, "image", image=image, before_dispatch=marker
            )
        assert error.value.code in {"image_type_unsupported", "image_type_unverified"}
        assert events == []
        input_file.set_input_files.assert_not_awaited()
    button.click.assert_not_awaited()
