"""Offline crash, isolation and archive acceptance tests; never contact Kleinanzeigen."""

import asyncio
from dataclasses import replace
import io
from pathlib import Path

from PIL import Image
import pytest

from messaging.contracts import MessagingError, MessagingSettings
from messaging.service import MessagingService
from messaging.store import MessagingStore
from utils.outbound_control import UpstreamBlocked


def png():
    output = io.BytesIO()
    Image.new("RGB", (3, 3), (20, 100, 40)).save(output, "PNG")
    return output.getvalue()


class Adapter:
    def __init__(self):
        self.clicks = []
        self.fail = None
        self.closed = False
        self.image_capability = True
        self.recipient = "222"
        self.after_click = asyncio.Event()
        self.hold_after_click = False
        self.reauth = False

    async def connect(self, account):
        return {"user_id": "111" if account["account_id"] == "a" else "222"}

    async def disconnect(self, account):
        pass

    async def close(self):
        self.closed = True

    async def prepare_send(self, account, target_kind, target_id):
        if self.reauth:
            raise MessagingError("reauth_required", "Bitte erneut anmelden.", 409)
        return {
            "account_user_id": account["user_id"],
            "recipient_user_id": self.recipient,
            "target_kind": target_kind,
            "target_id": target_id,
            "listing_id": target_id if target_kind == "listing" else None,
            "conversation_id": target_id if target_kind == "conversation" else None,
            "capabilities": {
                "can_send_text": True,
                "can_send_images": self.image_capability
                if target_kind == "conversation"
                else False,
            },
        }

    async def send_step(
        self, account, target, kind, text=None, image=None, before_dispatch=None
    ):
        if self.fail == "before":
            raise RuntimeError("private token and message must not escape")
        await before_dispatch()
        self.clicks.append(
            {"account_id": account["account_id"], "kind": kind, "text": text}
        )
        self.after_click.set()
        if self.hold_after_click:
            await asyncio.Future()
        if self.fail == "after":
            raise TimeoutError("private data")
        return {
            "message_id": f"message-{len(self.clicks)}",
            "conversation_id": "conversation-1",
            "recipient_user_id": self.recipient,
        }

    async def list_conversations(self, account, **kwargs):
        return {
            "conversations": [
                {
                    "conversation_id": "conversation-1",
                    "recipient_user_id": self.recipient,
                    "unread": True,
                }
            ],
            "next_cursor": None,
            "has_more": False,
        }

    async def get_conversation(self, account, conversation_id, **kwargs):
        return {
            "conversation_id": conversation_id,
            "recipient_user_id": self.recipient,
            "messages": [
                {
                    "message_id": "incoming-1",
                    "sender_user_id": self.recipient,
                    "text": "Grüße\nÖl & Äpfel",
                    "created_at": "2026-11-04T11:00:00+00:00",
                    "images": [
                        {
                            "data": png(),
                            "mime_type": "image/png",
                            "remote_id": "remote-1",
                        }
                    ],
                }
            ],
            "next_cursor": None,
            "has_more": False,
        }


@pytest.fixture
async def service(tmp_path):
    instance = MessagingService(
        MessagingSettings(root=tmp_path / "messaging"), Adapter()
    )
    await instance.start()
    for account_id, user_id in (("a", "111"), ("b", "222")):
        await instance.store.save_account(
            {"account_id": account_id, "user_id": user_id, "state": "connected"}
        )
    yield instance
    await instance.close()


async def wait(instance, operation):
    async with asyncio.timeout(5):
        while True:
            result = await instance.get_operation(
                operation["account_id"], operation["operation_id"]
            )
            if result["state"] not in {"queued", "running", "waiting_for_login"}:
                return result
            await asyncio.sleep(0.005)


async def send(instance, **overrides):
    return await instance.submit_send(
        **{
            "account_id": "a",
            "target_kind": "listing",
            "target_id": "1234567890",
            "text": "Grüße!\nIst der Artikel verfügbar?",
            "idempotency_key": "stable-1",
            **overrides,
        }
    )


async def test_first_contact_then_image_and_content_bound_idempotency(service):
    image = await service.upload_image("a", png(), "private-original-name.png")
    args = {"attachment_ids": [image["attachment_id"]]}
    first, replay = await asyncio.gather(send(service, **args), send(service, **args))
    assert first["operation_id"] == replay["operation_id"]
    result = await wait(service, first)
    assert result["state"] == "sent"
    assert [x["kind"] for x in service.adapter.clicks] == ["text", "image"]
    assert result["conversation_id"] == "conversation-1"
    assert "payload" not in result and "target" not in result
    assert (await send(service, **args))["idempotent_replay"]
    with pytest.raises(MessagingError, match="anderen Nachrichtenauftrag"):
        await send(service, text="anderer Text", **args)


async def test_failure_after_click_never_resends_even_after_restart(service):
    service.adapter.fail = "after"
    operation = await send(service)
    result = await wait(service, operation)
    assert result["state"] == "outcome_unknown"
    assert len(service.adapter.clicks) == 1
    settings = service.settings
    await service.close()
    other = MessagingService(settings, Adapter())
    await other.start()
    try:
        replay = await send(other)
        assert replay["operation_id"] == operation["operation_id"]
        assert replay["state"] == "outcome_unknown"
        assert other.adapter.clicks == []
    finally:
        await other.close()


async def test_shutdown_during_dispatch_records_unknown(service):
    service.adapter.hold_after_click = True
    operation = await send(service)
    await asyncio.wait_for(service.adapter.after_click.wait(), 5)
    await service.close()
    store = MessagingStore(service.settings.root)
    await store.start()
    try:
        result = await store.operation("a", operation["operation_id"])
        assert result["state"] == "outcome_unknown"
        assert result["steps"][0]["state"] == "outcome_unknown"
    finally:
        await store.close()


async def test_failure_before_dispatch_has_no_click_and_no_private_error(service):
    service.adapter.fail = "before"
    result = await wait(service, await send(service))
    assert result["state"] == "failed"
    assert service.adapter.clicks == []
    assert "private" not in str(result)


async def test_text_success_image_unsupported_reports_partial_without_repeating(
    service,
):
    service.adapter.image_capability = False
    image = await service.upload_image("a", png())
    operation = await send(service, attachment_ids=[image["attachment_id"]])
    result = await wait(service, operation)
    assert result["state"] == "partial"
    assert [s["state"] for s in result["steps"]] == ["sent", "pending"]
    assert result["error"]["code"] == "images_unsupported"
    await send(service, attachment_ids=[image["attachment_id"]])
    assert len(service.adapter.clicks) == 1


async def test_expired_login_pauses_and_connect_resumes(service):
    service.adapter.reauth = True
    operation = await send(service)
    assert (await wait(service, operation))["state"] == "reauth_required"
    service.adapter.reauth = False
    login = await service.connect_account("a")
    assert (await wait(service, login))["state"] == "connected"
    assert (await wait(service, operation))["state"] == "sent"
    assert len(service.adapter.clicks) == 1


async def test_archive_survives_disconnect_and_restart_and_is_account_scoped(service):
    live = await service.get_conversation("a", "conversation-1")
    image_id = live["messages"][0]["images"][0]["image_id"]
    assert (await service.get_message_image("a", image_id))["data"] == png()
    assert "data" not in live["messages"][0]["images"][0]
    with pytest.raises(MessagingError):
        await service.get_message_image("b", image_id)
    with pytest.raises(MessagingError):
        await send(service, account_id="b", attachment_ids=[image_id])
    await service.disconnect_account("a")
    archived = await service.get_conversation("a", "conversation-1", source="archive")
    assert archived["messages"][0]["text"] == "Grüße\nÖl & Äpfel"
    await service.close()
    other = MessagingService(service.settings, Adapter())
    await other.start()
    try:
        assert (await other.get_conversation("a", "conversation-1", source="archive"))[
            "messages"
        ] == archived["messages"]
        assert (await other.get_message_image("a", image_id))["data"] == png()
    finally:
        await other.close()


async def test_operations_cannot_be_read_through_other_account(service):
    operation = await send(service)
    with pytest.raises(MessagingError) as error:
        await service.get_operation("b", operation["operation_id"])
    assert error.value.code == "operation_not_found"


@pytest.mark.parametrize(
    "recipient,accounts,users,listings,expected",
    [
        ("222", {"a", "b"}, {"111", "222"}, {"1234567890"}, "sent"),
        ("333", {"a", "b"}, {"111", "222"}, {"1234567890"}, "failed"),
        ("222", set(), set(), set(), "failed"),
        ("222", {"a", "b"}, {"111", "222"}, {"999"}, "failed"),
        ("111", {"a", "b"}, {"111", "222"}, {"1234567890"}, "failed"),
    ],
)
async def test_server_test_policy_blocks_non_owned_targets(
    service, recipient, accounts, users, listings, expected
):
    service.settings = replace(
        service.settings,
        test_mode=True,
        test_account_ids=frozenset(accounts),
        test_user_ids=frozenset(users),
        test_listing_ids=frozenset(listings),
    )
    service.adapter.recipient = recipient
    result = await wait(service, await send(service))
    assert result["state"] == expected
    assert len(service.adapter.clicks) == (1 if expected == "sent" else 0)


async def test_invalid_images_and_modified_staged_file_never_send(service):
    with pytest.raises(MessagingError) as error:
        await service.upload_image("a", b"this is not a PNG", "pretend.png")
    assert error.value.code == "invalid_image"
    image = await service.upload_image("a", png())
    stored = await service.store.image("a", image["attachment_id"])
    Path(stored["path"]).write_bytes(b"modified")
    result = await wait(
        service,
        await send(
            service,
            target_kind="conversation",
            target_id="conversation-1",
            text=None,
            attachment_ids=[image["attachment_id"]],
        ),
    )
    assert result["state"] == "failed"
    assert result["error"]["code"] == "image_changed"
    assert not service.adapter.clicks


async def test_crash_recovery_of_dispatching_step_never_queues_it(tmp_path):
    settings = MessagingSettings(root=tmp_path / "messaging")
    store = MessagingStore(settings.root)
    await store.start()
    await store.save_account(
        {"account_id": "a", "user_id": "111", "state": "connected"}
    )
    await store.insert_operation(
        {
            "operation_id": "op-1",
            "account_id": "a",
            "kind": "send",
            "state": "running",
            "steps": [{"kind": "text", "state": "dispatching"}],
            "payload": {
                "target_kind": "listing",
                "target_id": "1234567890",
                "text": "Hallo",
                "attachment_ids": [],
            },
        },
        "key",
        "hash",
    )
    await store.close()
    instance = MessagingService(settings, Adapter())
    await instance.start()
    try:
        assert (await instance.get_operation("a", "op-1"))["state"] == "outcome_unknown"
        assert not instance.adapter.clicks
    finally:
        await instance.close()


async def test_private_storage_and_exclusive_owner(service):
    root = service.settings.root
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / "messaging.sqlite3").stat().st_mode & 0o777 == 0o600
    other = MessagingStore(root)
    with pytest.raises(MessagingError) as error:
        await other.start()
    assert error.value.code == "storage_in_use"


async def test_wrong_conversation_is_rejected_before_any_dispatch(service):
    original = service.adapter.prepare_send

    async def wrong(*args):
        target = await original(*args)
        target["conversation_id"] = "wrong-conversation"
        return target

    service.adapter.prepare_send = wrong
    result = await wait(
        service,
        await send(service, target_kind="conversation", target_id="conversation-1"),
    )
    assert result["error"]["code"] == "target_mismatch"
    assert not service.adapter.clicks


async def test_existing_archive_content_survives_unavailable_remote_content(service):
    live = await service.get_conversation("a", "conversation-1")
    old = live["messages"][0]
    await service.store.archive_messages(
        "a",
        "conversation-1",
        [
            {
                "message_id": old["message_id"],
                "text": None,
                "images": [{"remote_id": "remote-1", "unavailable": True}],
            }
        ],
    )
    result = await service.get_conversation("a", "conversation-1", source="archive")
    assert result["messages"][0]["text"] == old["text"]
    assert (
        result["messages"][0]["images"][0]["image_id"] == old["images"][0]["image_id"]
    )


async def test_failed_error_persistence_recovers_without_second_send(service):
    original = service.store.save_operation
    failures = []

    async def fail_once(operation):
        if operation["state"] == "outcome_unknown" and not failures:
            failures.append(True)
            raise OSError("disk temporarily unavailable")
        return await original(operation)

    service.store.save_operation = fail_once
    service.adapter.fail = "after"
    result = await wait(service, await send(service))
    assert result["state"] == "outcome_unknown"
    assert len(service.adapter.clicks) == 1
    assert service._worker is not None and not service._worker.done()


@pytest.mark.parametrize("wrapped", [False, True])
async def test_login_block_is_distinct_from_expired_auth_and_does_not_retry(
    service, wrapped
):
    calls = []

    async def blocked(account):
        calls.append(account["account_id"])
        if wrapped:
            raise MessagingError(
                "upstream_blocked",
                "Zugriff eingeschränkt",
                503,
                {"retry_after_seconds": 120},
            )
        raise UpstreamBlocked("Block detected", retry_after=120)

    service.adapter.connect = blocked
    result = await wait(service, await service.connect_account("a"))
    assert result["state"] == "blocked"
    assert result["error"]["code"] == "upstream_blocked"
    assert result["error"]["retry_after_seconds"] == 120
    assert "Keine automatischen" in result["next_action"]
    assert (await service.store.account("a"))["state"] == "blocked"
    with pytest.raises(MessagingError) as exc:
        await service.list_conversations("a")
    assert exc.value.code == "access_blocked"
    assert calls == ["a"]
    assert not service.adapter.clicks


async def test_recovery_of_last_receipt_needs_no_browser_and_repairs_archive(tmp_path):
    settings = MessagingSettings(root=tmp_path / "messaging")
    store = MessagingStore(settings.root)
    await store.start()
    await store.save_account(
        {"account_id": "a", "user_id": "111", "state": "disconnected"}
    )
    await store.insert_operation(
        {
            "operation_id": "op-final",
            "account_id": "a",
            "kind": "send",
            "state": "running",
            "conversation_id": "conversation-1",
            "steps": [
                {
                    "kind": "text",
                    "state": "sent",
                    "message_id": "confirmed-message",
                    "sent_at": "2026-11-04T12:00:00Z",
                }
            ],
            "target": {"recipient_user_id": "222"},
            "payload": {
                "text": "Bereits bestätigt",
                "target_kind": "conversation",
                "target_id": "conversation-1",
                "attachment_ids": [],
            },
        },
        "stable-key",
        "fingerprint",
    )
    await store.close()
    instance = MessagingService(settings, Adapter())
    await instance.start()
    try:
        assert (await instance.get_operation("a", "op-final"))["state"] == "sent"
        assert (
            await instance.get_conversation("a", "conversation-1", source="archive")
        )["messages"][0]["text"] == "Bereits bestätigt"
        assert not instance.adapter.clicks
    finally:
        await instance.close()
