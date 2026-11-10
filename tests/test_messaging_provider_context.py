"""Provider history is bound to a chat and survives missing/deleted pages."""

from copy import deepcopy
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from messaging.contracts import MessagingError, MessagingSettings
from messaging.service import MessagingService
from messaging.store import MessagingStore


FIRST = "2026-11-04T10:00:00+00:00"
SECOND = "2026-11-04T12:00:00+00:00"


def observation(text="Müller GmbH\nImpressum: Straße 4, Jena"):
    return {
        "schema_version": 1,
        "recipient_user_id": "222",
        "listing_id": "333",
        "sections": [{
            "kind": "listing_imprint", "status": "captured",
            "source_url": "https://www.kleinanzeigen.de/s-anzeige/fenster/333-84-3771",
            "captured_at": FIRST, "user_id": "222", "listing_id": "333",
            "title": "Rechtliche Angaben", "text": text, "truncated": False,
        }],
    }


def conversation(context=None):
    result = {
        "conversation_id": "conversation-1", "recipient_user_id": "222",
        "listing_id": "333", "messages": [{
            "message_id": "message-1", "sender_user_id": "222",
            "text": "Grüße aus Thüringen", "images": [], "created_at": FIRST,
        }], "has_more": False, "next_cursor": None,
    }
    if context is not None:
        result["provider_context"] = context
    return result


@pytest.fixture
async def service(tmp_path):
    adapter = SimpleNamespace(close=AsyncMock(), get_conversation=AsyncMock())
    instance = MessagingService(MessagingSettings(root=tmp_path / "archive"), adapter)
    await instance.start()
    await instance.store.save_account({"account_id": "a", "user_id": "111", "state": "connected"})
    yield instance
    await instance.close()


async def test_snapshots_deduplicate_preserve_changes_and_survive_unavailable_pages(service):
    first = await service._archive_result("a", conversation(observation()))
    snapshot = first["provider_context"]["snapshots"][0]
    assert snapshot["text"].startswith("Müller")
    assert snapshot["captured_at"] == FIRST
    again = observation()
    again["sections"][0]["captured_at"] = SECOND
    replay = await service._archive_result("a", conversation(again))
    assert replay["provider_context"]["snapshots"] == [snapshot]
    assert replay["provider_context"]["latest_checks"][0]["captured_at"] == SECOND

    changed = observation("Müller GmbH\nNeue Straße 8")
    changed["sections"][0]["captured_at"] = SECOND
    updated = await service._archive_result("a", conversation(changed))
    assert len(updated["provider_context"]["snapshots"]) == 2
    unavailable = observation("discard unbound page text")
    unavailable["sections"][0]["status"] = "unavailable"
    unavailable["sections"][0]["error_code"] = "page_unavailable"
    lost = await service._archive_result("a", conversation(unavailable))
    assert lost["provider_context"]["snapshots"] == updated["provider_context"]["snapshots"]
    assert lost["provider_context"]["latest_checks"][0]["status"] == "unavailable"
    assert "discard unbound" not in json.dumps(lost)

    await service.store.archive_conversations("a", [{"conversation_id": "conversation-1", "title": "Gelöschte Anzeige"}])
    archived = await service.get_conversation("a", "conversation-1", source="archive")
    assert archived["provider_context"] == lost["provider_context"]
    assert archived["messages"][0]["text"] == "Grüße aus Thüringen"
    assert archived["recipient_user_id"] == "222"
    service.adapter.get_conversation.assert_not_awaited()


async def test_existing_archive_migrates_and_provider_history_survives_restart(tmp_path):
    root = tmp_path / "archive"
    root.mkdir()
    db = sqlite3.connect(root / "messaging.sqlite3")
    db.execute("CREATE TABLE conversations(account_id TEXT,conversation_id TEXT,data TEXT,PRIMARY KEY(account_id,conversation_id))")
    original = conversation()
    original.pop("messages")
    db.execute("INSERT INTO conversations VALUES(?,?,?)", ("a", "conversation-1", json.dumps(original)))
    db.execute("PRAGMA user_version=1")
    db.commit()
    db.close()
    store = MessagingStore(root)
    await store.start()
    await store.archive_provider_context("a", "conversation-1", observation())
    before = await store.archived_provider_context("a", "conversation-1")
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == 2
    await store.close()
    reopened = MessagingStore(root)
    await reopened.start()
    try:
        meta, _ = await reopened.archived_conversation("a", "conversation-1")
        assert meta["provider_context"] == before
        assert (root / "messaging.sqlite3").stat().st_mode & 0o777 == 0o600
    finally:
        await reopened.close()


@pytest.mark.parametrize("mutation", [
    lambda x: x.update(recipient_user_id="999"),
    lambda x: x.update(listing_id="444"),
    lambda x: x.update(schema_version=2),
    lambda x: x.update(schema_version=True),
    lambda x: x["sections"][0].update(kind={"unexpected": "shape"}),
    lambda x: x["sections"][0].update(status=[]),
    lambda x: x["sections"][0].update(user_id="999"),
    lambda x: x["sections"][0].update(listing_id="444"),
    lambda x: x["sections"][0].update(source_url="https://private.example/profile"),
    lambda x: x["sections"][0].update(source_url="https://www.kleinanzeigen.de/s-anzeige/fenster/333-84-3771?token=private"),
    lambda x: x["sections"][0].update(source_url="https://www.kleinanzeigen.de/s-anzeige/other/444-84-3771"),
    lambda x: x["sections"][0].update(captured_at="today"),
    lambda x: x["sections"][0].update(text="x" * 20001),
    lambda x: x["sections"].append(deepcopy(x["sections"][0])),
])
async def test_invalid_context_is_not_archived_and_does_not_destroy_chat(service, mutation):
    original = await service._archive_result("a", conversation(observation()))
    bad = observation("This must never be saved")
    mutation(bad)
    result = await service._archive_result("a", conversation(bad))
    assert result["provider_context_error"]["code"] == "provider_context_invalid"
    assert result["provider_context"] == original["provider_context"]
    assert result["messages"][0]["text"] == "Grüße aus Thüringen"
    assert "must never" not in json.dumps(result)


async def test_account_isolation_and_existing_conversation_binding(service):
    await service._archive_result("a", conversation(observation()))
    await service.store.save_account({"account_id": "b", "user_id": "222"})
    assert await service.store.archived_provider_context("b", "conversation-1") is None
    with pytest.raises(MessagingError, match="Gespräch nicht im Archiv"):
        await service.store.archive_provider_context("b", "conversation-1", observation())
    for field in ("recipient_user_id", "listing_id"):
        with pytest.raises(MessagingError) as exc:
            await service.store.archive_conversations("a", [{"conversation_id": "conversation-1", field: "999"}])
        assert exc.value.code == "conversation_mismatch"


@pytest.mark.parametrize("status", ["not_present", "unavailable", "identity_mismatch", "not_counterpart"])
async def test_failed_or_own_listing_checks_cannot_archive_foreign_text(service, status):
    value = observation("Sensitive content of another account")
    value["sections"][0].update(status=status, user_id="111", error_code="unsafe secret")
    result = await service._archive_result("a", conversation(value))
    context = result["provider_context"]
    assert context["snapshots"] == []
    assert context["latest_checks"][0]["status"] == status
    assert "Sensitive" not in json.dumps(context)
    assert "unsafe secret" not in json.dumps(context)


@pytest.mark.parametrize("source", [
    "https://www.kleinanzeigen.de/s-bestandsliste.html?userId=222",
    "https://www.kleinanzeigen.de/s-anzeigen-des-nutzers.html?userId=222",
    "https://www.kleinanzeigen.de/pro/mueller-immobilien",
    "https://www.kleinanzeigen.de/pro/mueller.immo~jena/",
    "https://www.kleinanzeigen.de/s-anzeigen-des-nutzers/222/",
])
async def test_public_profile_text_is_preserved_with_source(service, source):
    value = observation("Müller\nAktiv seit 2016\nFreundlich")
    value["sections"][0].update(kind="profile", source_url=source, title="Profil")
    result = await service._archive_result("a", conversation(value))
    snapshot = result["provider_context"]["snapshots"][0]
    assert snapshot["kind"] == "profile"
    assert snapshot["source_url"] == source
    assert snapshot["user_id"] == "222"


async def test_listing_trailing_slash_keeps_exact_listing_binding(service):
    value = observation()
    value["sections"][0]["source_url"] += "/"
    result = await service._archive_result("a", conversation(value))
    assert len(result["provider_context"]["snapshots"]) == 1
    value["sections"][0]["source_url"] = "https://www.kleinanzeigen.de/s-anzeige/fenster/444-84-3771/"
    rejected = await service._archive_result("a", conversation(value))
    assert rejected["provider_context_error"]["code"] == "provider_context_invalid"


async def test_failed_capture_diagnostics_expose_only_bounded_counts(service):
    value = observation()
    value["sections"][0].update(status="unavailable", structure={
        "provider_nodes": 2, "profile_links": True, "imprint_nodes": -1,
        "body_bytes": 2000001, "imprint_text_chars": 296,
        "html": "PRIVATE", "csrf": "PRIVATE", "provider_text_chars": "PRIVATE",
    })
    result = await service._archive_result("a", conversation(value))
    check = result["provider_context"]["latest_checks"][0]
    assert check["structure"] == {"provider_nodes": 2, "imprint_text_chars": 296}
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("guard", [
    "listing_url", "provider_root", "conflicting_numeric_id",
    "no_numeric_or_business_link", "pro_url", "pro_identity_components",
])
async def test_provider_guard_station_survives_archiving_without_page_content(service, guard):
    value = observation("PRIVATE UNBOUND PAGE CONTENT")
    value["sections"][0].update(status="identity_mismatch", provider_guard=guard)
    result = await service._archive_result("a", conversation(value))
    check = result["provider_context"]["latest_checks"][0]
    assert check["provider_guard"] == guard
    assert "PRIVATE" not in json.dumps(result)
    archived = await service.get_conversation("a", "conversation-1", source="archive")
    assert archived["provider_context"]["latest_checks"][0]["provider_guard"] == guard


@pytest.mark.parametrize("guard", [None, True, 1, [], {}, "PRIVATE", "listing_url:PRIVATE"])
async def test_unknown_or_nonstring_provider_guard_is_discarded(service, guard):
    value = observation("PRIVATE UNBOUND PAGE CONTENT")
    value["sections"][0].update(status="identity_mismatch", provider_guard=guard)
    result = await service._archive_result("a", conversation(value))
    assert "provider_guard" not in result["provider_context"]["latest_checks"][0]
    assert "PRIVATE" not in json.dumps(result)


async def test_successful_provider_snapshots_never_acquire_failure_guards(service):
    value = observation()
    value["sections"][0]["provider_guard"] = "pro_identity_components"
    result = await service._archive_result("a", conversation(value))
    assert "provider_guard" not in json.dumps(result["provider_context"])
