"""First-contact receipt follow-up is read-only and survives uncertain outcomes."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from messaging.contracts import MessagingError, MessagingSettings
from messaging.safari import SafariBridge, SafariMessagingAdapter
from messaging.service import MessagingService


ACCOUNT = {"account_id": "own-a", "user_id": "111", "state": "connected"}
TARGET = {
    "target_kind": "listing",
    "target_id": "1234567890",
    "listing_id": "1234567890",
    "account_user_id": "111",
    "recipient_user_id": "222",
    "capabilities": {"can_send_text": True, "can_send_images": False},
}


class Controller:
    def __init__(self):
        self.active = 0
        self.calls = 0

    async def run_interaction(self, operation, **kwargs):
        assert self.active == 0
        self.calls += 1
        self.active += 1
        try:
            return await operation()
        finally:
            self.active -= 1


def setup(tmp_path, timeout=1):
    bridge = SafariBridge(tmp_path, command_timeout=timeout)
    bridge._bindings["own-a"] = {"user_id": "111", "profile_id": "default", "tab_id": 1}
    controller = Controller()
    return bridge, controller, SafariMessagingAdapter(tmp_path, controller, bridge)


async def exchange(bridge, action, **kwargs):
    return await bridge.exchange(
        {"version": 1, "profile_id": "default", "action": action, **kwargs}
    )


async def poll(bridge):
    for _ in range(100):
        value = (await exchange(bridge, "poll"))["command"]
        if value:
            return value
        await asyncio.sleep(0)
    raise AssertionError("No command")


async def result(bridge, command, value):
    return await exchange(
        bridge,
        "result",
        command_id=command["id"],
        result={"account_user_id": "111", **value},
    )


def follow(**changes):
    return {
        "conversation_id": "conversation-1",
        "message_id": "message-1",
        "dispatched_at": datetime.now(timezone.utc).isoformat(),
        **changes,
    }


async def test_first_contact_journals_follow_before_separate_read_permit(tmp_path):
    bridge, controller, adapter = setup(tmp_path)
    before = AsyncMock()
    observations = []

    async def durable_observation(value):
        assert controller.active == 0
        assert not bridge._commands
        observations.append(value)

    task = asyncio.create_task(
        adapter.send_step(
            ACCOUNT,
            TARGET,
            "text",
            text="Grüße!",
            before_dispatch=before,
            on_conversation_observed=durable_observation,
        )
    )
    sending = await poll(bridge)
    assert sending["method"] == "send_step"
    before.assert_awaited_once()
    await result(bridge, sending, {"_follow": follow()})
    reading = await poll(bridge)
    assert reading["method"] == "confirm_send"
    assert len(observations) == 1
    assert reading["args"]["conversation_id"] == observations[0]["conversation_id"]
    assert reading["args"]["expected_message_id"] == "message-1"
    assert reading["args"]["text"] == "Grüße!"
    assert reading["args"]["baseline_message_ids"] == []
    before.assert_awaited_once()
    assert controller.calls == 2
    await result(
        bridge,
        reading,
        {
            "confirmed": True,
            "message_id": "message-1",
            "conversation_id": "conversation-1",
            "recipient_user_id": "222",
        },
    )
    receipt = await task
    assert receipt["message_id"] == "message-1"
    assert "_follow" not in receipt
    assert (await exchange(bridge, "poll"))["command"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"conversation_id": "https://example.org/?token=private"},
        {"message_id": {"secret": "private"}},
        {"dispatched_at": "2020-01-01T00:00:00+00:00"},
        {"dispatched_at": "2099-01-01T00:00:00+00:00"},
        {"dispatched_at": "2026-01-01T00:00:00"},
    ],
)
async def test_untrusted_follow_hint_never_starts_another_operation(tmp_path, changes):
    bridge, controller, adapter = setup(tmp_path)
    observed = AsyncMock()
    before = AsyncMock()
    task = asyncio.create_task(
        adapter.send_step(
            ACCOUNT,
            TARGET,
            "text",
            text="test",
            before_dispatch=before,
            on_conversation_observed=observed,
        )
    )
    sending = await poll(bridge)
    await result(bridge, sending, {"_follow": follow(**changes)})
    with pytest.raises(MessagingError) as error:
        await task
    assert error.value.code == "outcome_unknown"
    before.assert_awaited_once()
    observed.assert_not_awaited()
    assert controller.calls == 1


async def test_failed_observation_commit_prevents_followup_read(tmp_path):
    bridge, controller, adapter = setup(tmp_path)
    task = asyncio.create_task(
        adapter.send_step(
            ACCOUNT,
            TARGET,
            "text",
            text="test",
            before_dispatch=AsyncMock(),
            on_conversation_observed=AsyncMock(
                side_effect=RuntimeError("disk unavailable")
            ),
        )
    )
    sending = await poll(bridge)
    await result(bridge, sending, {"_follow": follow()})
    with pytest.raises(RuntimeError):
        await task
    assert controller.calls == 1
    assert (await exchange(bridge, "poll"))["command"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"recipient_user_id": "333"},
        {"conversation_id": "another-conversation"},
        {"message_id": "another-message"},
        {"confirmed": False},
        {"_follow": {"conversation_id": "new-hop"}},
    ],
)
async def test_wrong_or_recursive_receipt_stays_unknown_and_does_not_resend(
    tmp_path, changes
):
    bridge, controller, adapter = setup(tmp_path)
    before = AsyncMock()
    task = asyncio.create_task(
        adapter.send_step(
            ACCOUNT,
            TARGET,
            "text",
            text="test",
            before_dispatch=before,
            on_conversation_observed=AsyncMock(),
        )
    )
    sending = await poll(bridge)
    await result(bridge, sending, {"_follow": follow()})
    reading = await poll(bridge)
    await result(
        bridge,
        reading,
        {
            "confirmed": True,
            "message_id": "message-1",
            "conversation_id": "conversation-1",
            "recipient_user_id": "222",
            **changes,
        },
    )
    with pytest.raises(MessagingError) as error:
        await task
    assert error.value.code == "outcome_unknown"
    before.assert_awaited_once()
    assert controller.calls == 2
    assert (await exchange(bridge, "poll"))["command"] is None


def test_follow_not_before_never_precedes_durable_dispatch_boundary():
    boundary = datetime.now(timezone.utc)
    observation = SafariMessagingAdapter._follow_observation(
        follow(dispatched_at=(boundary - timedelta(seconds=1)).isoformat()), boundary
    )
    assert datetime.fromisoformat(observation["not_before"]) == boundary


async def test_observed_conversation_survives_restart_without_becoming_a_receipt(
    tmp_path,
):
    class Adapter:
        supports_conversation_observation = True

        def __init__(self):
            self.sends = 0
            self.reads = []

        async def prepare_send(self, account, kind, target_id):
            return dict(TARGET)

        async def send_step(
            self,
            account,
            target,
            kind,
            *,
            before_dispatch,
            on_conversation_observed,
            **kwargs,
        ):
            await before_dispatch()
            self.sends += 1
            await on_conversation_observed(
                {"conversation_id": "conversation-1", "message_id": "message-1"}
            )
            raise TimeoutError("lost confirmation")

        async def get_conversation(self, account, conversation_id, **kwargs):
            self.reads.append(conversation_id)
            return {
                "conversation_id": conversation_id,
                "recipient_user_id": "222",
                "messages": [],
            }

        async def close(self):
            pass

    settings = MessagingSettings(
        root=tmp_path,
        test_mode=True,
        test_account_ids=frozenset({"own-a", "own-b"}),
        test_user_ids=frozenset({"111", "222"}),
        test_listing_ids=frozenset({"1234567890"}),
    )
    adapter = Adapter()
    service = MessagingService(settings, adapter)
    await service.start()
    try:
        await service.store.save_account(ACCOUNT)
        await service.store.save_account(
            {"account_id": "own-b", "user_id": "222", "state": "connected"}
        )
        operation = await service.submit_send(
            "own-a",
            "listing",
            "1234567890",
            text="test",
            idempotency_key="one-first-contact",
        )
        async with asyncio.timeout(2):
            while True:
                stored = await service.store.operation(
                    "own-a", operation["operation_id"]
                )
                if stored["state"] == "outcome_unknown":
                    break
                await asyncio.sleep(0)
        assert stored["observed_conversation_id"] == "conversation-1"
        assert stored["steps"][0]["observed_message_id"] == "message-1"
        assert stored["conversation_id"] is None
        assert adapter.sends == 1
    finally:
        await service.close()
    after_restart = Adapter()
    service = MessagingService(settings, after_restart)
    await service.start()
    try:
        current = await service.get_operation("own-a", operation["operation_id"])
        assert current["state"] == "outcome_unknown"
        assert current["conversation_id"] is None
        assert current["reconciliation"]["recipient_matches"] is True
        assert current["reconciliation"]["automatic_resend"] is False
        assert after_restart.reads == ["conversation-1"]
        assert after_restart.sends == 0
        replay = await service.submit_send(
            "own-a",
            "listing",
            "1234567890",
            text="test",
            idempotency_key="one-first-contact",
        )
        assert replay["operation_id"] == operation["operation_id"]
        assert replay["state"] == "outcome_unknown"
        assert after_restart.sends == 0
    finally:
        await service.close()
