"""Explicit reconnect of an existing Safari profile; no browser or live HTTP."""

import asyncio
import copy
import json

import pytest

from messaging.contracts import MessagingError
from messaging.safari import SafariBridge, SafariMessagingAdapter


A = {"account_id": "own_a", "user_id": "111", "label": "Eigenes Konto"}
B = {"account_id": "own_b", "user_id": "222"}
OTHER_PROFILE = "faed3981-0a75-48c9-a13a-baf6c01be529"


class Controller:
    def __init__(self):
        self.calls = 0

    async def run_interaction(self, operation, **kwargs):
        self.calls += 1
        return await operation()


def setup(tmp_path):
    bridge = SafariBridge(tmp_path)
    bridge._bindings = {
        "own_a": {"profile_id": "default", "tab_id": 1, "user_id": "111"},
        "own_b": {"profile_id": OTHER_PROFILE, "tab_id": 2, "user_id": "222"},
    }
    bridge._save_bindings()
    controller = Controller()
    return bridge, controller, SafariMessagingAdapter(tmp_path, controller, bridge)


async def exchange(bridge, action, profile="default", **body):
    return await bridge.exchange(
        {"version": 1, "profile_id": profile, "action": action, **body}
    )


async def next_command(bridge, profile="default"):
    for _ in range(100):
        reply = await exchange(bridge, "poll", profile)
        if reply["command"]:
            return reply["command"]
        await asyncio.sleep(0)
    raise AssertionError("No queued command")


def proof(**overrides):
    return {
        "user_id": "111",
        "account_user_id": "111",
        "identity_page_path": "/m-meine-anzeigen.html",
        "recovered_tab_id": 91,
        "capabilities": {"text_status": "unverified"},
        "_upstream_reached": True,
        **overrides,
    }


async def test_connect_recovers_only_same_native_profile_and_persists_tab(tmp_path):
    bridge, controller, adapter = setup(tmp_path)
    before = copy.deepcopy(bridge._bindings)
    task = asyncio.create_task(adapter.connect(A))
    await asyncio.sleep(0)
    assert (await exchange(bridge, "poll", OTHER_PROFILE))["command"] is None
    command = await next_command(bridge)
    assert command["method"] == "recover_tab"
    assert command["user_id"] == "111"
    assert command["args"] == {"tab_id": 1}
    assert bridge._pending == {}
    with pytest.raises(MessagingError):
        await exchange(
            bridge, "result", OTHER_PROFILE, command_id=command["id"], result=proof()
        )
    assert bridge._bindings == before
    await exchange(bridge, "result", command_id=command["id"], result=proof())
    identity = await task
    assert identity == {
        "user_id": "111",
        "identity_page_path": "/m-meine-anzeigen.html",
        "capabilities": {"text_status": "unverified"},
    }
    assert controller.calls == 1
    assert bridge.binding(A) == {**before["own_a"], "tab_id": 91}
    assert bridge.binding(B) == before["own_b"]
    assert SafariBridge(tmp_path).binding(A) == bridge.binding(A)
    assert not bridge._commands and not bridge._pending


@pytest.mark.parametrize(
    "error_code", ["safari_tab_missing", "safari_tab_ambiguous", "safari_recovery_unavailable"]
)
async def test_unavailable_recovery_keeps_binding_and_waits_for_manual_pair(tmp_path, error_code):
    bridge, controller, adapter = setup(tmp_path)
    before = copy.deepcopy(bridge._bindings)
    task = asyncio.create_task(adapter.connect(A))
    command = await next_command(bridge)
    await exchange(bridge, "result", command_id=command["id"], error={"code": error_code})
    for _ in range(100):
        if "own_a" in bridge._pending:
            break
        await asyncio.sleep(0)
    assert "own_a" in bridge._pending
    assert bridge._bindings == before
    assert controller.calls == 1
    await exchange(bridge, "pair", account_id="own_a", tab_id=92)
    identity_command = await next_command(bridge)
    assert identity_command["method"] == "identify"
    await exchange(bridge, "result", command_id=identity_command["id"], result={"user_id": "111"})
    assert (await task)["user_id"] == "111"
    assert bridge.binding(A)["tab_id"] == 92


@pytest.mark.parametrize(
    "values",
    [
        {"user_id": "999"},
        {"account_user_id": "999"},
        {"recovered_tab_id": True},
        {"recovered_tab_id": -1},
        {"identity_page_path": "/s-bestandsliste.html"},
    ],
)
async def test_invalid_identity_never_changes_existing_binding(tmp_path, values):
    bridge, _, adapter = setup(tmp_path)
    before = copy.deepcopy(bridge._bindings)
    task = asyncio.create_task(adapter.connect(A))
    command = await next_command(bridge)
    await exchange(bridge, "result", command_id=command["id"], result=proof(**values))
    with pytest.raises(MessagingError):
        await task
    assert bridge._bindings == before
    assert json.loads((tmp_path / "safari-bindings.json").read_text()) == before
    assert not bridge._pending


@pytest.mark.parametrize("change", ["disconnect", "replace", "close"])
def test_compare_and_swap_rejects_removed_or_replaced_binding(tmp_path, change):
    bridge, _, _ = setup(tmp_path)
    previous = dict(bridge.binding(A))
    if change == "disconnect":
        bridge._bindings.pop("own_a")
    elif change == "replace":
        bridge._bindings["own_a"] = {**previous, "tab_id": 80}
    else:
        bridge._closed = True
    current = copy.deepcopy(bridge._bindings)
    with pytest.raises(MessagingError) as denied:
        bridge.restore_tab(A, previous, proof())
    assert denied.value.code == "safari_binding_changed"
    assert bridge._bindings == current


def test_failed_durable_commit_does_not_change_memory_or_disk(tmp_path, monkeypatch):
    bridge, _, _ = setup(tmp_path)
    previous = dict(bridge.binding(A))
    before = (tmp_path / "safari-bindings.json").read_bytes()

    def fail_write(*args):
        raise OSError("offline test disk unavailable")

    monkeypatch.setattr("messaging.safari._private_write", fail_write)
    with pytest.raises(OSError):
        bridge.restore_tab(A, previous, proof())
    assert bridge.binding(A) == previous
    assert (tmp_path / "safari-bindings.json").read_bytes() == before


async def test_read_error_does_not_recover_or_retry_implicitly(tmp_path):
    bridge, controller, adapter = setup(tmp_path)
    task = asyncio.create_task(adapter.list_conversations(A))
    command = await next_command(bridge)
    assert command["method"] == "list_conversations"
    await exchange(bridge, "result", command_id=command["id"], error={"code": "safari_tab_missing"})
    with pytest.raises(MessagingError) as denied:
        await task
    assert denied.value.code == "safari_tab_missing"
    assert controller.calls == 1
    assert not bridge._commands and not bridge._pending
    assert (await exchange(bridge, "poll"))["command"] is None


async def test_new_or_disconnected_account_still_requires_manual_pairing(tmp_path):
    bridge, controller, adapter = setup(tmp_path)
    await bridge.disconnect(A)
    task = asyncio.create_task(adapter.connect(A))
    await asyncio.sleep(0)
    reply = await exchange(bridge, "poll")
    assert reply["command"] is None
    assert reply["pending_accounts"] == [{"account_id": "own_a", "label": "Eigenes Konto"}]
    assert controller.calls == 0
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_service_reconnect_completes_without_manual_pair_or_changed_uid(tmp_path):
    from messaging.contracts import MessagingSettings
    from messaging.service import MessagingService

    bridge, controller, adapter = setup(tmp_path)
    service = MessagingService(
        MessagingSettings(root=tmp_path, browser_channel="safari"), adapter
    )
    await service.start()
    try:
        await service.store.save_account({**A, "state": "connected"})
        operation = await service.connect_account("own_a")
        assert operation["state"] == "waiting_for_login"
        command = await next_command(bridge)
        assert command["method"] == "recover_tab"
        await exchange(bridge, "result", command_id=command["id"], result=proof())
        await service._connect_tasks["own_a"][1]
        completed = await service.get_operation("own_a", operation["operation_id"])
        assert completed["state"] == "connected"
        assert completed["user_id"] == "111"
        assert (await service.store.account("own_a"))["state"] == "connected"
        assert bridge.binding(A)["tab_id"] == 91
        assert controller.calls == 1
        assert not bridge._pending
    finally:
        await service.close()
