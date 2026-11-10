"""Identical image contents retain their distinct platform attachment slots."""

import io

from PIL import Image
import pytest

from messaging.store import MessagingStore


@pytest.fixture
async def store(tmp_path):
    instance = MessagingStore(tmp_path / "archive")
    await instance.start()
    await instance.save_account({"account_id": "a", "user_id": "111"})
    await instance.archive_conversations("a", [{"conversation_id": "chat-1", "recipient_user_id": "222"}])
    yield instance
    await instance.close()


async def test_equal_bytes_keep_all_real_slots_across_repeat_missing_read_and_restart(store):
    output = io.BytesIO()
    Image.new("RGB", (2, 2), (47, 160, 96)).save(output, "PNG")
    image = await store.save_image("a", output.getvalue())
    references = [{"image_id": image["image_id"], "remote_id": f"m1:{index}"} for index in range(5)]
    message = {"message_id": "m1", "text": "Fünf gleiche Bilder", "images": references}
    await store.archive_messages("a", "chat-1", [message])
    await store.archive_messages("a", "chat-1", [message])
    await store.archive_messages("a", "chat-1", [{**message, "images": [
        {"remote_id": f"m1:{index}", "unavailable": True} for index in range(5)
    ]}])
    _, messages = await store.archived_conversation("a", "chat-1")
    assert messages[0]["images"] == references
    assert len(list((store.root / "images/a").iterdir())) == 1
    await store.close()
    reopened = MessagingStore(store.root)
    await reopened.start()
    try:
        _, restored = await reopened.archived_conversation("a", "chat-1")
        assert restored[0]["images"] == references
    finally:
        await reopened.close()


async def test_repeat_of_same_slot_is_deduplicated_and_legacy_hash_references_stay_compatible(store):
    image_id = "img_" + "a" * 64
    references = [
        {"image_id": image_id, "remote_id": "m1:0"},
        {"image_id": image_id, "remote_id": "m1:1"},
        {"image_id": image_id},
    ]
    message = {"message_id": "m1", "images": references}
    await store.archive_messages("a", "chat-1", [message])
    await store.archive_messages("a", "chat-1", [{**message, "images": [*references, *references]}])
    _, messages = await store.archived_conversation("a", "chat-1")
    assert messages[0]["images"] == references
