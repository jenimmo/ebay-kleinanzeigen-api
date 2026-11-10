"""Platform events are archived separately and cannot impersonate user messages."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from messaging.contracts import MessagingSettings
from messaging.service import MessagingService
from messaging.store import MessagingStore


@pytest.fixture
async def service(tmp_path):
    instance = MessagingService(
        MessagingSettings(root=tmp_path / "archive"),
        SimpleNamespace(close=AsyncMock(), get_conversation=AsyncMock()),
    )
    await instance.start()
    await instance.store.save_account({"account_id": "a", "user_id": "111"})
    yield instance
    await instance.close()


def conversation(events):
    return {
        "conversation_id": "chat-1", "recipient_user_id": "222",
        "messages": [{"message_id": "msg-1", "text": "Hallo", "images": []}],
        "system_events": events, "has_more": False, "next_cursor": None,
    }


def event(text="Die Anzeige ist nicht mehr verfügbar."):
    return {"event_id": "event-1", "event_type": "AdDeleted",
            "created_at": "2026-11-04T12:00:00Z", "text": text}


async def test_event_history_survives_pages_without_events_and_restart(service):
    live = await service._archive_result("a", conversation([event()]))
    assert live["system_events"][0]["created_at"].endswith("+00:00")
    assert "sender_user_id" not in live["system_events"][0]
    assert len(live["messages"]) == 1
    await service._archive_result("a", conversation([event("Anzeige gelöscht")]))
    await service._archive_result("a", conversation([]))
    archived = await service.get_conversation("a", "chat-1", source="archive")
    assert archived["system_events"][0]["text"] == "Anzeige gelöscht"
    assert archived["system_events"][0]["previous_texts"] == [event()["text"]]
    assert len(archived["messages"]) == 1
    service.adapter.get_conversation.assert_not_awaited()
    assert await service.store.archived_system_events("b", "chat-1") == []
    await service.store.close()
    reopened = MessagingStore(service.settings.root)
    await reopened.start()
    try:
        assert await reopened.archived_system_events("a", "chat-1") == archived["system_events"]
    finally:
        await reopened.close()


async def test_unknown_raw_fields_and_fake_sender_are_not_stored(service):
    value = {**event(), "sender_user_id": "222", "token": "never-save-me", "raw": {"csrf": "private"}}
    live = await service._archive_result("a", conversation([value]))
    assert set(live["system_events"][0]) == {"event_id", "event_type", "created_at", "text"}
    archived = await service.get_conversation("a", "chat-1", source="archive")
    assert "never-save-me" not in json.dumps(archived)
    assert "csrf" not in json.dumps(archived)


@pytest.mark.parametrize("event_type", ["_STATUS", "1:STATUS", "event.status-v2"])
async def test_reader_event_type_tokens_are_preserved_without_inventing_meaning(service, event_type):
    live = await service._archive_result("a", conversation([{**event(), "event_type": event_type}]))
    assert live["system_events"][0]["event_type"] == event_type


@pytest.mark.parametrize("events", [
    [{**event(), "event_id": "../bad"}],
    [{**event(), "event_type": {"bad": "shape"}}],
    [{**event(), "created_at": "2026-11-04"}],
    [{**event(), "text": "x" * 20001}],
    [event(), event()],
    [event()] * 201,
])
async def test_malformed_event_does_not_destroy_messages_or_prior_events(service, events):
    await service._archive_result("a", conversation([event()]))
    result = await service._archive_result("a", conversation(events))
    assert result["system_events_error"]["code"] == "system_events_invalid"
    archived = await service.get_conversation("a", "chat-1", source="archive")
    assert archived["messages"][0]["text"] == "Hallo"
    assert len(archived["system_events"]) == 1


async def test_conflicting_event_type_is_rejected_without_replacing_history(service):
    await service._archive_result("a", conversation([event()]))
    conflicting = {**event(), "event_type": "UserMessage", "text": "False text"}
    result = await service._archive_result("a", conversation([conflicting]))
    assert result["system_events_error"]["code"] == "system_events_invalid"
    events = await service.store.archived_system_events("a", "chat-1")
    assert events[0]["event_type"] == "AdDeleted"
    assert "False text" not in json.dumps(events)
