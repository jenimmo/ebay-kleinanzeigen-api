"""Offline Safari bridge tests: no browser, site access, uploads or sends."""

import asyncio
import base64
import hashlib
import json
import stat
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from messaging.contracts import MessagingError
from messaging.safari import (
    CHUNK_BYTES,
    SafariBridge,
    SafariMessagingAdapter,
    _content_revision,
    _identity_structure,
    _sanitize_structure,
)
from utils.outbound_control import (
    OutboundCircuitOpen,
    OutboundController,
    OutboundSettings,
    UpstreamBlocked,
)


A = {"account_id": "own_a", "user_id": "111", "label": "Eigenes Konto A"}
B = {"account_id": "own_b", "user_id": "222"}
PROFILE_B = "faed3981-0a75-48c9-a13a-baf6c01be529"


class Controller:
    def __init__(self):
        self.calls = 0
        self.signals = []

    async def run_interaction(self, operation, **kwargs):
        self.calls += 1
        return await operation()

    async def report_interaction_block(self, code):
        self.signals.append(code)
        raise UpstreamBlocked("Blocked")


def bound(tmp_path, **kwargs):
    bridge = SafariBridge(tmp_path, **kwargs)
    bridge._bindings[A["account_id"]] = {
        "profile_id": "default",
        "tab_id": 1,
        "user_id": "111",
    }
    bridge._bindings[B["account_id"]] = {
        "profile_id": PROFILE_B,
        "tab_id": 2,
        "user_id": "222",
    }
    return bridge


async def exchange(bridge, action, profile="default", **kwargs):
    return await bridge.exchange(
        {"version": 1, "profile_id": profile, "action": action, **kwargs}
    )


async def command_for(bridge, profile="default"):
    for _ in range(50):
        result = await exchange(bridge, "poll", profile)
        if result["command"]:
            return result["command"]
        await asyncio.sleep(0)
    raise AssertionError("No queued command")


async def respond(bridge, command, result, profile="default"):
    return await exchange(
        bridge, "result", profile, command_id=command["id"], result=result
    )


def test_token_is_persistent_private_and_constant_time_comparable(tmp_path):
    first = SafariBridge(tmp_path)
    config = first.pairing_config(
        "http://127.0.0.1:8000/messaging/safari-bridge/exchange"
    )
    assert config["version"] == 1
    assert first.authenticate(config["token"])
    assert not first.authenticate("wrong")
    assert not first.authenticate(None)
    assert SafariBridge(tmp_path).authenticate(config["token"])
    assert stat.S_IMODE((tmp_path / "safari-token").stat().st_mode) == 0o600


async def test_manual_pairing_has_long_wait_without_extending_command_ttl(tmp_path):
    bridge = SafariBridge(tmp_path)
    assert bridge.connect_timeout == 1800
    assert bridge.command_timeout == 45
    bridge.connect_timeout = 0.01
    controller = Controller()
    adapter = SafariMessagingAdapter(tmp_path, controller, bridge)
    with pytest.raises(MessagingError) as error:
        await adapter.connect({"account_id": "own", "user_id": None})
    assert error.value.code == "safari_pairing_timeout"
    assert "Diesen Tab verbinden" in error.value.message
    assert controller.calls == 0
    assert not bridge._commands
    assert not bridge._pending
    assert bridge.command_timeout == 45


def test_token_symlink_is_rejected_without_modifying_destination(tmp_path):
    target = tmp_path / "secret"
    target.write_text("x" * 43)
    (tmp_path / "safari-token").symlink_to(target)
    with pytest.raises(MessagingError, match="Konfiguration"):
        SafariBridge(tmp_path)
    assert target.read_text() == "x" * 43


async def test_pairing_requires_pending_request_and_verified_dom_identity(tmp_path):
    bridge = SafariBridge(tmp_path)
    adapter = SafariMessagingAdapter(tmp_path, Controller(), bridge)
    with pytest.raises(MessagingError) as denied:
        await exchange(bridge, "pair", account_id="own_a", tab_id=1, user_id="999")
    assert denied.value.code == "safari_pairing_not_pending"
    task = asyncio.create_task(adapter.connect({**A, "user_id": None}))
    await asyncio.sleep(0)
    pending = await exchange(bridge, "poll")
    assert pending["pending_accounts"] == [{"account_id": "own_a", "label": A["label"]}]
    await exchange(bridge, "pair", account_id="own_a", tab_id=1, user_id="999")
    command = await command_for(bridge)
    assert command["method"] == "identify"
    assert command["user_id"] is None
    await respond(bridge, command, {"user_id": "111"})
    assert await task == {"user_id": "111"}
    assert bridge.binding(A)["user_id"] == "111"
    assert SafariBridge(tmp_path).binding(A)["profile_id"] == "default"


async def test_safari_connect_exposes_capabilities_through_service(tmp_path):
    from messaging.contracts import MessagingSettings
    from messaging.service import MessagingService

    bridge = SafariBridge(tmp_path)
    adapter = SafariMessagingAdapter(tmp_path, Controller(), bridge)
    service = MessagingService(
        MessagingSettings(root=tmp_path, browser_channel="safari"), adapter
    )
    await service.start()
    try:
        operation = await service.connect_account("own_a")
        for _ in range(50):
            if "own_a" in bridge._pending:
                break
            await asyncio.sleep(0)
        await exchange(bridge, "pair", account_id="own_a", tab_id=1)
        command = await command_for(bridge)
        await respond(
            bridge,
            command,
            {
                "user_id": "111",
                "capabilities": {
                    "scope": "current_page",
                    "text_status": "supported",
                    "can_send_text": True,
                    "images_status": "unverified",
                    "can_send_images": False,
                },
            },
        )
        await service._connect_tasks["own_a"][1]
        result = await service.get_operation("own_a", operation["operation_id"])
        assert result["state"] == "connected"
        assert result["capabilities"]["can_send_text"] is True
        assert result["capabilities"]["can_send_images"] is None
        assert (await service.store.account("own_a"))["capabilities"] == result[
            "capabilities"
        ]
    finally:
        await service.close()


async def test_identity_error_diagnostics_contain_only_schema_metadata(tmp_path):
    bridge = bound(tmp_path)
    task = asyncio.create_task(bridge.request(A, "identify", {}))
    command = await command_for(bridge)
    await exchange(
        bridge,
        "result",
        command_id=command["id"],
        error={
            "code": "identity_unverified",
            "content_revision": "1.0.6",
            "message": "PRIVATE",
            "identity_structure": {
                "page_path": "/portal/anzeigen",
                "astro_present": True,
                "astro_valid_json": True,
                "current_user_marker_count": 0,
                "legacy_proof_stage": "seller",
                "legacy_proof_counts": {
                    "own_edit_links": 1,
                    "own_logout_links": 2,
                    "matching_containers": 1,
                    "PRIVATE": 1,
                },
                "legacy_proof_flags": {
                    "edit_text_matches": True,
                    "owned_link_proven": True,
                    "listing_page_present": False,
                    "recheck_proven": False,
                    "PRIVATE": True,
                },
                "own_header_data_count": 0,
                "own_header_link_count": 3,
                "candidate_id_count": 2,
                "next_present": True,
                "next_valid_json": True,
                "next_current_user": {
                    "type": "object",
                    "keys": ["id", "userId", "PRIVATE"],
                    "value": "PRIVATE",
                },
                "json_scripts": [
                    {"name": "astroSharedData", "type": "other", "text": "PRIVATE"},
                    {
                        "name": "__NEXT_DATA__",
                        "type": "application/json",
                        "value": "PRIVATE",
                    },
                    {"name": "PRIVATE", "type": "application/json"},
                ],
                "html": "PRIVATE",
                "cookie": "PRIVATE",
                "sources": [
                    {
                        "name": "user",
                        "type": "object",
                        "keys": ["id", "userId", "PRIVATE"],
                        "value": "PRIVATE",
                    },
                    {"name": "PRIVATE", "type": "object", "keys": ["id"]},
                ],
            },
        },
    )
    with pytest.raises(MessagingError) as error:
        await task
    metadata = error.value.details["identity_structure"]
    assert error.value.details["content_revision"] == "1.0.6"
    assert metadata == {
        "page_path": "/portal/anzeigen",
        "astro_present": True,
        "astro_valid_json": True,
        "current_user_marker_count": 0,
        "legacy_proof_stage": "seller",
        "legacy_proof_counts": {
            "own_edit_links": 1,
            "own_logout_links": 2,
            "matching_containers": 1,
        },
        "legacy_proof_flags": {
            "edit_text_matches": True,
            "owned_link_proven": True,
            "listing_page_present": False,
            "recheck_proven": False,
        },
        "own_header_data_count": 0,
        "own_header_link_count": 3,
        "candidate_id_count": 2,
        "next_present": True,
        "next_valid_json": True,
        "next_current_user": {"type": "object", "keys": ["id", "userId"]},
        "json_scripts": [
            {"name": "astroSharedData", "type": "other"},
            {"name": "__NEXT_DATA__", "type": "application/json"},
        ],
        "sources": [{"name": "user", "type": "object", "keys": ["id", "userId"]}],
    }
    assert "PRIVATE" not in str(error.value.to_dict())


def test_legacy_identity_diagnostics_reject_unknown_stages_counts_flags_and_values():
    result = _identity_structure(
        {
            "legacy_proof_stage": "PRIVATE",
            "legacy_proof_counts": {
                "own_edit_links": True,
                "own_logout_links": -1,
                "matching_containers": 101,
                "html": "PRIVATE",
            },
            "legacy_proof_flags": {
                "edit_text_matches": 1,
                "owned_link_proven": "PRIVATE",
                "listing_page_present": [],
                "recheck_proven": None,
            },
        }
    )
    assert result == {
        "legacy_proof_counts": {},
        "legacy_proof_flags": {},
        "sources": [],
    }


@pytest.mark.parametrize(
    "value",
    [
        "1.0.6?PRIVATE",
        "1.0.6\n",
        "v1.0.6",
        "1.0",
        "1.0.6.1",
        "1000.1.1",
        "PRIVATE",
        106,
        ["1.0.6"],
        None,
    ],
)
def test_content_revision_rejects_values_outside_bounded_version_format(value):
    assert _content_revision(value) is None
    assert "content_revision" not in _sanitize_structure(
        {"nodes": [], "content_revision": value}
    )


async def test_content_revision_is_available_for_nonidentity_content_errors(tmp_path):
    bridge = bound(tmp_path)
    task = asyncio.create_task(bridge.request(A, "list_conversations", {}))
    command = await command_for(bridge)
    await exchange(
        bridge,
        "result",
        command_id=command["id"],
        error={
            "code": "ui_unsupported",
            "content_revision": "1.0.6",
            "message": "PRIVATE",
            "identity_structure": {"legacy_proof_stage": "seller"},
        },
    )
    with pytest.raises(MessagingError) as error:
        await task
    assert error.value.details == {
        "content_revision": "1.0.6",
        "identity_structure": {"legacy_proof_stage": "seller", "sources": []},
    }
    assert "PRIVATE" not in str(error.value.to_dict())
    assert (
        _sanitize_structure({"nodes": [], "content_revision": "1.0.6"})[
            "content_revision"
        ]
        == "1.0.6"
    )


@pytest.mark.parametrize(
    "diagnostic",
    [
        {
            "own_header_data_count": True,
            "own_header_link_count": 101,
            "candidate_id_count": 3,
        },
        {
            "own_header_data_count": -1,
            "own_header_link_count": 1.5,
            "candidate_id_count": "1",
        },
        {"next_present": 1, "next_valid_json": "PRIVATE"},
        {"next_current_user": {"type": ["object"], "keys": ["id"]}},
        {"next_current_user": {"type": "PRIVATE", "keys": ["id"]}},
        {"json_scripts": "PRIVATE"},
        {"json_scripts": [{"name": ["__NEXT_DATA__"], "type": "application/json"}]},
        {"json_scripts": [{"name": "__NEXT_DATA__", "type": "PRIVATE"}]},
    ],
)
def test_identity_structure_rejects_invalid_new_metadata(diagnostic):
    assert _identity_structure(diagnostic) in (
        {"sources": []},
        {"json_scripts": [], "sources": []},
    )


def test_identity_structure_never_returns_current_user_values_or_arbitrary_script_names():
    diagnostic = _identity_structure(
        {
            "next_current_user": {
                "type": "object",
                "keys": ["PRIVATE", "email"],
                "id": "PRIVATE",
            },
            "json_scripts": [
                {"name": "PRIVATE", "type": "application/json"},
                {"name": "__NEXT_DATA__", "type": "missing", "text": "PRIVATE"},
            ],
        }
    )
    assert diagnostic == {
        "next_current_user": {"type": "object", "keys": []},
        "json_scripts": [{"name": "__NEXT_DATA__", "type": "missing"}],
        "sources": [],
    }
    assert "PRIVATE" not in str(diagnostic)


async def test_navigation_timeout_preserves_only_fixed_boolean_diagnostics(tmp_path):
    from messaging.service import MessagingService

    bridge = bound(tmp_path)
    task = asyncio.create_task(bridge.request(A, "identify", {}))
    command = await command_for(bridge)
    await exchange(
        bridge,
        "result",
        command_id=command["id"],
        error={
            "code": "safari_navigation_timeout",
            "message": "PRIVATE",
            "navigation_state": {
                "tab_complete": False,
                "target_matched": True,
                "marker_received": True,
                "marker_ready": "PRIVATE",
                "fresh_document": 1,
                "url": "https://example.org/?token=PRIVATE",
                "text": "PRIVATE",
            },
        },
    )
    with pytest.raises(MessagingError) as error:
        await task
    expected = {"tab_complete": False, "target_matched": True, "marker_received": True}
    assert error.value.details == {"navigation_state": expected}
    assert MessagingService._error(error.value)["navigation_state"] == expected
    assert "PRIVATE" not in str(MessagingService._error(error.value))


async def test_navigation_diagnostics_are_not_accepted_for_other_errors(tmp_path):
    bridge = bound(tmp_path)
    task = asyncio.create_task(bridge.request(A, "identify", {}))
    command = await command_for(bridge)
    await exchange(
        bridge,
        "result",
        command_id=command["id"],
        error={
            "code": "ui_unsupported",
            "navigation_state": {"tab_complete": True},
        },
    )
    with pytest.raises(MessagingError) as error:
        await task
    assert error.value.details == {}


@pytest.mark.parametrize(
    "delivery",
    [
        "received",
        "no_reply",
        "send_rejected",
        "handler_error",
        "sender_rejected",
        "invalid_reply",
        "PRIVATE RAW ERROR",
        {"token": "PRIVATE"},
    ],
)
async def test_navigation_marker_delivery_accepts_only_fixed_enum(tmp_path, delivery):
    bridge = bound(tmp_path)
    task = asyncio.create_task(bridge.request(A, "identify", {}))
    command = await command_for(bridge)
    await exchange(
        bridge,
        "result",
        command_id=command["id"],
        error={
            "code": "safari_navigation_timeout",
            "marker_delivery": delivery,
        },
    )
    with pytest.raises(MessagingError) as error:
        await task
    if isinstance(delivery, str) and delivery != "PRIVATE RAW ERROR":
        assert error.value.details == {"marker_delivery": delivery}
    else:
        assert error.value.details == {}
    assert "PRIVATE" not in str(error.value.to_dict())


@pytest.mark.parametrize(
    "delivery,stage",
    [
        ("received", "bootstrap"),
        ("no_reply", "unavailable"),
        ("send_rejected", "content_started"),
        ("sender_rejected", "initialized"),
        ("invalid_reply", "handler_ready"),
    ],
)
async def test_content_bootstrap_accepts_only_enum_and_boolean_schema(
    tmp_path, delivery, stage
):
    bridge = bound(tmp_path)
    task = asyncio.create_task(bridge.request(A, "identify", {}))
    command = await command_for(bridge)
    expected = {
        "delivery": delivery,
        "stage": stage,
        "protocol_ready": True,
        "secure_random_ready": False,
        "handler_ready": False,
    }
    await exchange(
        bridge,
        "result",
        command_id=command["id"],
        error={
            "code": "safari_navigation_timeout",
            "content_bootstrap": {**expected, "url": "PRIVATE", "message": "PRIVATE"},
        },
    )
    with pytest.raises(MessagingError) as error:
        await task
    assert error.value.details == {"content_bootstrap": expected}
    assert "PRIVATE" not in str(error.value.to_dict())


@pytest.mark.parametrize(
    "override",
    [
        {"delivery": "PRIVATE"},
        {"stage": "PRIVATE"},
        {"stage": {"token": "PRIVATE"}},
        {"protocol_ready": "PRIVATE"},
        {"secure_random_ready": 1},
        {"handler_ready": None},
    ],
)
async def test_content_bootstrap_rejects_malformed_diagnostics(tmp_path, override):
    bridge = bound(tmp_path)
    task = asyncio.create_task(bridge.request(A, "identify", {}))
    command = await command_for(bridge)
    await exchange(
        bridge,
        "result",
        command_id=command["id"],
        error={
            "code": "safari_navigation_timeout",
            "content_bootstrap": {
                "delivery": "received",
                "stage": "handler_ready",
                "protocol_ready": True,
                "secure_random_ready": True,
                "handler_ready": True,
                **override,
            },
        },
    )
    with pytest.raises(MessagingError) as error:
        await task
    assert error.value.details == {}


async def test_bridge_status_reports_only_counts_and_pending_account_ids(tmp_path):
    bridge = bound(tmp_path)
    before = await bridge.status()
    assert before["last_native_poll_at"] is None
    assert before["profile_count"] == 0
    task = asyncio.create_task(
        bridge.request(A, "send_step", {"text": "PRIVATE MESSAGE"})
    )
    await asyncio.sleep(0)
    assert (await bridge.status())["command_counts"] == {"queued": 1, "delivered": 0}
    command = await command_for(bridge)
    with pytest.raises(MessagingError):
        await exchange(bridge, "pair", account_id="not-pending", tab_id=1)
    status = await bridge.status()
    assert status["last_native_poll_at"] is not None
    assert status["profile_count"] == 1
    assert status["exchange_counts"]["poll"] >= 1
    assert status["error_counts"] == {"safari_pairing_not_pending": 1}
    assert status["command_counts"] == {"queued": 0, "delivered": 1}
    serialized = json.dumps(status)
    assert "PRIVATE MESSAGE" not in serialized
    assert bridge.pairing_config("ignored")["token"] not in serialized
    assert PROFILE_B not in serialized
    await respond(bridge, command, {})
    await task


async def test_account_diagnostics_distinguish_current_profile_heartbeats_and_commands(
    tmp_path,
):
    bridge = bound(tmp_path)
    initial = {item["account_id"]: item for item in (await bridge.status())["accounts"]}
    assert initial["own_a"]["last_native_poll_at"] is None
    assert initial["own_b"]["last_native_poll_at"] is None
    await exchange(bridge, "poll")
    after_a = {item["account_id"]: item for item in (await bridge.status())["accounts"]}
    assert after_a["own_a"]["last_native_poll_at"] is not None
    assert after_a["own_b"]["last_native_poll_at"] is None
    task = asyncio.create_task(bridge.request(B, "inspect_ui", {}))
    await asyncio.sleep(0)
    queued = {item["account_id"]: item for item in (await bridge.status())["accounts"]}
    assert queued["own_b"]["command_counts"] == {"queued": 1, "delivered": 0}
    assert queued["own_a"]["command_counts"] == {"queued": 0, "delivered": 0}
    command = await command_for(bridge, PROFILE_B)
    delivered = {
        item["account_id"]: item for item in (await bridge.status())["accounts"]
    }
    assert delivered["own_b"]["last_native_poll_at"] is not None
    assert (
        delivered["own_a"]["last_native_poll_at"]
        == after_a["own_a"]["last_native_poll_at"]
    )
    assert delivered["own_b"]["command_counts"] == {"queued": 0, "delivered": 1}
    encoded = json.dumps(delivered)
    assert PROFILE_B not in encoded
    assert "user_id" not in encoded
    assert "profile_id" not in encoded
    assert "tab_id" not in encoded
    await respond(bridge, command, {}, PROFILE_B)
    await task
    bridge._save_bindings()
    restarted = SafariBridge(tmp_path)
    assert all(
        item["last_native_poll_at"] is None
        for item in (await restarted.status())["accounts"]
    )


async def test_pairing_refuses_second_account_in_same_profile(tmp_path):
    bridge = bound(tmp_path)
    task = asyncio.create_task(
        bridge.connect(B, AsyncMock(return_value={"user_id": "222"}))
    )
    await asyncio.sleep(0)
    with pytest.raises(MessagingError) as denied:
        await exchange(bridge, "pair", account_id="own_b", tab_id=9)
    assert denied.value.code == "safari_profile_in_use"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_reconnecting_cannot_replace_bound_user_id(tmp_path):
    bridge = bound(tmp_path)
    task = asyncio.create_task(
        bridge.connect(A, AsyncMock(return_value={"user_id": "999"}))
    )
    await asyncio.sleep(0)
    await exchange(bridge, "pair", account_id="own_a", tab_id=4)
    with pytest.raises(MessagingError) as denied:
        await task
    assert denied.value.code == "account_mismatch"
    assert bridge.binding(A)["user_id"] == "111"


async def test_command_delivered_only_once_even_with_parallel_polls(tmp_path):
    bridge = bound(tmp_path)
    task = asyncio.create_task(bridge.request(A, "inspect_ui", {}))
    await asyncio.sleep(0)
    polls = await asyncio.gather(*(exchange(bridge, "poll") for _ in range(5)))
    commands = [item["command"] for item in polls if item["command"]]
    assert len(commands) == 1
    await respond(bridge, commands[0], {"account_user_id": "111"})
    await task
    with pytest.raises(MessagingError) as gone:
        await respond(bridge, commands[0], {"account_user_id": "111"})
    assert gone.value.code == "safari_command_gone"


async def test_other_profile_cannot_claim_or_complete_command(tmp_path):
    bridge = bound(tmp_path)
    task = asyncio.create_task(bridge.request(A, "inspect_ui", {}))
    await asyncio.sleep(0)
    assert (await exchange(bridge, "poll", PROFILE_B))["command"] is None
    command = await command_for(bridge)
    with pytest.raises(MessagingError) as denied:
        await respond(bridge, command, {}, PROFILE_B)
    assert denied.value.code == "safari_profile_mismatch"
    await respond(bridge, command, {})
    await task


@pytest.mark.parametrize("delivered", [False, True])
async def test_timeout_never_redelivers_and_late_result_is_rejected(
    tmp_path, delivered
):
    bridge = bound(tmp_path, command_timeout=0.02)
    task = asyncio.create_task(bridge.request(A, "send_step", {}))
    command = await command_for(bridge) if delivered else None
    with pytest.raises(MessagingError) as timeout:
        await task
    assert timeout.value.code == "safari_timeout"
    assert (await exchange(bridge, "poll"))["command"] is None
    if command:
        with pytest.raises(MessagingError):
            await respond(bridge, command, {})


async def test_cancellation_removes_pending_command(tmp_path):
    bridge = bound(tmp_path)
    task = asyncio.create_task(bridge.request(A, "send_step", {}))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await exchange(bridge, "poll"))["command"] is None
    assert not bridge._commands


async def test_dispatch_journal_precedes_any_deliverable_send(tmp_path):
    bridge = bound(tmp_path)
    adapter = SafariMessagingAdapter(tmp_path, Controller(), bridge)
    recorded = []

    async def durable_before_dispatch():
        assert not bridge._commands
        recorded.append("durable")

    task = asyncio.create_task(
        adapter.send_step(
            A, {}, "text", text="Grüße ÄÖÜ", before_dispatch=durable_before_dispatch
        )
    )
    command = await command_for(bridge)
    assert recorded == ["durable"]
    assert command["args"]["text"] == "Grüße ÄÖÜ"
    assert datetime.fromisoformat(command["expires_at"]) > datetime.now(timezone.utc)
    await respond(bridge, command, {"account_user_id": "111", "message_id": "m1"})
    assert (await task)["message_id"] == "m1"


async def test_failed_journal_or_missing_binding_prevents_dispatch(tmp_path):
    bridge = bound(tmp_path)
    adapter = SafariMessagingAdapter(tmp_path, Controller(), bridge)
    before = AsyncMock(side_effect=RuntimeError("disk full"))
    with pytest.raises(RuntimeError):
        await adapter.send_step(A, {}, "text", text="test", before_dispatch=before)
    assert not bridge._commands
    bridge._bindings.pop("own_a")
    before.reset_mock()
    with pytest.raises(MessagingError):
        await adapter.send_step(A, {}, "text", text="test", before_dispatch=before)
    before.assert_not_called()


async def test_adapter_rejects_wrong_account_and_sanitizes_remote_errors(tmp_path):
    bridge = bound(tmp_path)
    adapter = SafariMessagingAdapter(tmp_path, Controller(), bridge)
    task = asyncio.create_task(adapter.inspect_ui(A))
    command = await command_for(bridge)
    await respond(bridge, command, {"account_user_id": "222"})
    with pytest.raises(MessagingError) as mismatch:
        await task
    assert mismatch.value.code == "account_mismatch"
    for remote_code, expected in (
        ("SECRET COOKIE", "ui_unsupported"),
        ("safari_content_timeout", "safari_content_timeout"),
    ):
        task = asyncio.create_task(adapter.inspect_ui(A))
        command = await command_for(bridge)
        await exchange(
            bridge,
            "result",
            command_id=command["id"],
            error={"code": remote_code, "message": "PRIVATE MESSAGE"},
        )
        with pytest.raises(MessagingError) as sanitized:
            await task
        assert sanitized.value.code == expected
        assert "PRIVATE" not in str(sanitized.value)


async def test_legacy_safari_block_code_trips_shared_controller(tmp_path):
    bridge = bound(tmp_path)
    controller = Controller()
    adapter = SafariMessagingAdapter(tmp_path, controller, bridge)
    task = asyncio.create_task(adapter.inspect_ui(A))
    command = await command_for(bridge)
    await exchange(
        bridge, "result", command_id=command["id"], error={"code": "access_blocked"}
    )
    with pytest.raises(UpstreamBlocked):
        await task
    assert controller.signals == ["upstream_blocked"]


async def test_oversized_utf8_text_rejected_before_dispatch_journal(tmp_path):
    bridge = bound(tmp_path)
    adapter = SafariMessagingAdapter(tmp_path, Controller(), bridge)
    before = AsyncMock()
    with pytest.raises(MessagingError) as oversized:
        await adapter.send_step(
            A, {}, "text", text="😀" * 20000, before_dispatch=before
        )
    assert oversized.value.code == "response_too_large"
    before.assert_not_called()
    assert not bridge._commands


def test_structure_diagnostics_omit_message_html_values_and_url_queries():
    result = _sanitize_structure(
        {
            "page": "/m-nachrichten.html?token=PRIVATE",
            "messages": [{"text": "PRIVATE MESSAGE"}],
            "token": "PRIVATE TOKEN",
            "nodes": [
                {
                    "tag": "textarea",
                    "id": "nachricht",
                    "data-testid": "reply-editor",
                    "visible": True,
                    "disabled": False,
                    "maxlength": "20000",
                    "value": "PRIVATE DRAFT",
                    "text": "PRIVATE MESSAGE",
                    "outerHTML": "PRIVATE HTML",
                    "attributes": ["value", "data-testid", "onclick"],
                    "link_path": "/m-nachrichten.html?token=PRIVATE",
                },
                {
                    "tag": "a",
                    "link_path": "/s-anzeige/PRIVATER-TITEL/1234500001-84-3771",
                    "userId": "12345001",
                },
            ],
        }
    )
    assert "PRIVATE" not in json.dumps(result)
    assert result["page"] is None
    assert result["nodes"][0] == {
        "tag": "textarea",
        "id": "nachricht",
        "data-testid": "reply-editor",
        "visible": True,
        "disabled": False,
        "maxlength": "20000",
        "attributes": ["value", "data-testid"],
    }
    assert result["nodes"][1]["link_path"] == "/s-anzeige/1234500001-84-3771"


def test_structure_diagnostics_are_bounded_and_reject_nonstructural_result():
    result = _sanitize_structure(
        {
            "page": "/m-nachrichten.html",
            "nodes": [{"tag": "button", "attributes": []}] * 101,
        }
    )
    assert len(result["nodes"]) == 100
    assert result["truncated"] is True
    with pytest.raises(MessagingError):
        _sanitize_structure({"messages": []})


def test_structure_diagnostics_preserve_only_known_empty_frame_and_shadow_metadata():
    result = _sanitize_structure(
        {
            "page": "/portal/nachrichten",
            "empty_heading_in_main": False,
            "empty_text_in_main": False,
            "empty_heading_in_document": True,
            "empty_text_in_document": True,
            "open_shadow_host_count": 1,
            "heading": "PRIVATE",
            "nodes": [
                {"tag": "h1", "has_direct_text": True, "text": "PRIVATE"},
                {
                    "tag": "iframe",
                    "frame_path": "/portal/nachrichten",
                    "src": "https://www.kleinanzeigen.de/portal/nachrichten?token=PRIVATE",
                },
                {
                    "tag": "message-app",
                    "shadow_child_count": 100,
                    "shadow_html": "PRIVATE",
                },
            ],
        }
    )
    assert result["empty_heading_in_main"] is False
    assert result["empty_text_in_main"] is False
    assert result["empty_heading_in_document"] is True
    assert result["empty_text_in_document"] is True
    assert result["open_shadow_host_count"] == 1
    assert result["nodes"] == [
        {"tag": "h1", "has_direct_text": True, "attributes": []},
        {"tag": "iframe", "frame_path": "/portal/nachrichten", "attributes": []},
        {"tag": "message-app", "shadow_child_count": 100, "attributes": []},
    ]
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("invalid_count", [True, -1, 101, 1.5, "1", None])
def test_structure_diagnostics_reject_invalid_shadow_counts_and_nonboolean_empty_flags(
    invalid_count,
):
    result = _sanitize_structure(
        {
            "nodes": [{"tag": "div", "shadow_child_count": invalid_count}],
            "open_shadow_host_count": invalid_count,
            "empty_heading_in_main": 1,
            "empty_text_in_main": "PRIVATE",
            "empty_heading_in_document": [],
            "empty_text_in_document": None,
        }
    )
    assert "open_shadow_host_count" not in result
    assert "shadow_child_count" not in result["nodes"][0]
    assert not any(name.startswith("empty_") for name in result)


@pytest.mark.parametrize(
    "path",
    [
        "/portal/nachrichten?token=PRIVATE",
        "https://www.kleinanzeigen.de/portal/nachrichten",
        "https://evil.test/portal/nachrichten",
        "/s-anzeige/1234500001",
        "/m-mein-konto.html",
        ["/portal/nachrichten"],
    ],
)
def test_structure_diagnostics_reject_unknown_or_query_bearing_frame_paths(path):
    result = _sanitize_structure({"nodes": [{"tag": "iframe", "frame_path": path}]})
    assert result["nodes"] == [{"tag": "iframe", "attributes": []}]


async def test_inspection_preserves_fixed_view_and_never_accepts_arbitrary_urls(
    tmp_path,
):
    bridge = bound(tmp_path)
    controller = Controller()
    adapter = SafariMessagingAdapter(tmp_path, controller, bridge)
    task = asyncio.create_task(adapter.inspect_ui(A))
    command = await command_for(bridge)
    assert command["args"] == {"view": "current", "tab_id": 1}
    await respond(
        bridge,
        command,
        {"account_user_id": "111", "page": "/m-nachrichten.html", "nodes": []},
    )
    assert (await task)["nodes"] == []
    assert controller.calls == 1
    for args in (
        {"view": "https://example.org"},
        {"view": "listing"},
        {"view": "current", "listing_id": "1234500001"},
        {"view": "listing", "listing_id": "https://example.org/?token=PRIVATE"},
    ):
        with pytest.raises(MessagingError):
            await adapter.inspect_ui(A, **args)
    assert controller.calls == 1


@pytest.mark.parametrize(
    "state,user_id",
    [("reauth_required", "111"), ("blocked", "111"), ("connected", None)],
)
async def test_inspection_requires_connected_verified_account(tmp_path, state, user_id):
    from types import SimpleNamespace
    from messaging.contracts import MessagingSettings
    from messaging.service import MessagingService

    inspector = AsyncMock()
    service = MessagingService(
        MessagingSettings(root=tmp_path),
        SimpleNamespace(inspect_ui=inspector),
        store=SimpleNamespace(
            account=AsyncMock(return_value={**A, "state": state, "user_id": user_id})
        ),
    )
    with pytest.raises(MessagingError):
        await service.inspect_safari_ui("own_a")
    inspector.assert_not_awaited()


async def test_image_download_is_chunked_and_command_scoped(tmp_path):
    bridge = bound(tmp_path)
    data = b"x" * (CHUNK_BYTES + 10)
    image = {
        "data": data,
        "sha256": hashlib.sha256(data).hexdigest(),
        "mime_type": "image/png",
        "filename": "test.png",
    }
    task = asyncio.create_task(bridge.request(A, "send_step", {}, image=image))
    command = await command_for(bridge)
    chunks = []
    for offset in (0, CHUNK_BYTES):
        part = await exchange(
            bridge, "image_chunk", command_id=command["id"], offset=offset
        )
        assert len(json.dumps(part)) < 64 * 1024
        chunks.append(base64.b64decode(part["data_base64"]))
    assert b"".join(chunks) == data
    with pytest.raises(MessagingError):
        await exchange(
            bridge, "image_chunk", PROFILE_B, command_id=command["id"], offset=0
        )
    with pytest.raises(MessagingError):
        await exchange(bridge, "image_chunk", command_id=command["id"], offset=1)
    await respond(bridge, command, {})
    await task
    with pytest.raises(MessagingError):
        await exchange(bridge, "image_chunk", command_id=command["id"], offset=0)


@pytest.mark.parametrize(
    "complete,correct_digest", [(True, True), (False, True), (True, False)]
)
async def test_archive_image_chunks_are_verified_and_missing_data_explicit(
    tmp_path, complete, correct_digest
):
    bridge = bound(tmp_path)
    task = asyncio.create_task(bridge.request(A, "get_conversation", {}))
    command = await command_for(bridge)
    data = b"media"
    await exchange(
        bridge,
        "archive_image_chunk",
        command_id=command["id"],
        transfer_id="image-1",
        offset=0,
        total_bytes=len(data) if complete else 10,
        sha256=hashlib.sha256(data if correct_digest else b"other").hexdigest(),
        data_base64=base64.b64encode(data).decode(),
    )
    await respond(
        bridge,
        command,
        {
            "messages": [
                {"images": [{"transfer_id": "image-1", "remote_id": "remote-1"}]}
            ]
        },
    )
    image = (await task)["messages"][0]["images"][0]
    if complete and correct_digest:
        assert image["data"] == data
    else:
        assert image["unavailable"] is True
        assert "data" not in image
    assert "transfer_id" not in image
    assert not bridge._commands


async def test_archive_transfer_rejects_oversize_out_of_order_and_wrong_command(
    tmp_path,
):
    bridge = bound(tmp_path)
    task = asyncio.create_task(bridge.request(A, "get_conversation", {}))
    command = await command_for(bridge)
    values = {
        "command_id": command["id"],
        "transfer_id": "i",
        "offset": 0,
        "total_bytes": 2,
        "sha256": "0" * 64,
        "data_base64": "eA==",
    }
    with pytest.raises(MessagingError):
        await exchange(bridge, "archive_image_chunk", **{**values, "offset": 1})
    with pytest.raises(MessagingError):
        await exchange(
            bridge, "archive_image_chunk", **{**values, "total_bytes": 21 * 1024 * 1024}
        )
    await exchange(bridge, "archive_image_chunk", **values)
    with pytest.raises(MessagingError):
        await exchange(bridge, "archive_image_chunk", **values)
    await respond(bridge, command, {"messages": []})
    await task


async def test_disconnect_forgets_only_local_binding_not_safari_login(tmp_path):
    bridge = bound(tmp_path)
    await bridge.disconnect(A)
    with pytest.raises(MessagingError):
        bridge.binding(A)
    assert bridge.binding(B)["user_id"] == "222"
    assert not bridge._commands  # No website logout or cookie-clearing command.


async def test_shared_controller_serializes_safari_operations_and_records_blocks():
    controller = OutboundController(
        OutboundSettings(min_interval_seconds=0, jitter_seconds=0)
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    order = []

    async def first():
        order.append("first")
        entered.set()
        await release.wait()

    async def second():
        order.append("second")

    one = asyncio.create_task(controller.run_interaction(first))
    await entered.wait()
    two = asyncio.create_task(controller.run_interaction(second))
    await asyncio.sleep(0)
    assert order == ["first"]
    release.set()
    await asyncio.gather(one, two)
    assert order == ["first", "second"]
    with pytest.raises(UpstreamBlocked):
        await controller.run_interaction(
            lambda: controller.report_interaction_block("upstream_blocked")
        )
    with pytest.raises(OutboundCircuitOpen):
        await controller.run_interaction(second)
    assert order == ["first", "second"]
    await controller.close()


async def test_local_dom_read_does_not_clear_existing_circuit():
    controller = OutboundController(
        OutboundSettings(min_interval_seconds=0, jitter_seconds=0)
    )
    await controller.restore_circuit_snapshot(
        {
            "version": 1,
            "state": "open",
            "reason": "block_page",
            "open_until": (
                datetime.now(timezone.utc) - timedelta(seconds=2)
            ).isoformat(),
            "block_streak": 1,
        }
    )
    # Set a recoverable existing circuit independently of snapshot wire format.
    controller._circuit_reason = "block_page"
    controller._circuit_open_until = controller._clock() - 1
    assert await controller.run_interaction(
        AsyncMock(return_value={"local": True})
    ) == {"local": True}
    assert controller._circuit_reason == "block_page"
    await controller.run_interaction(
        AsyncMock(return_value={"verified": True}),
        upstream_verified=lambda result: result["verified"],
    )
    assert controller._circuit_reason is None
    await controller.close()


async def test_safari_lifespan_uses_real_shared_controller_and_releases_resources(
    tmp_path, monkeypatch
):
    import main

    class BrowserPool:
        def __init__(self, **kwargs):
            self.closed = False

        async def start(self):
            pass

        async def close(self):
            self.closed = True

    monkeypatch.setenv("KLEINANZEIGEN_MESSAGING_BROWSER_CHANNEL", "safari")
    monkeypatch.setenv(
        "KLEINANZEIGEN_JOB_DATABASE_PATH", str(tmp_path / "jobs.sqlite3")
    )
    monkeypatch.setenv("KLEINANZEIGEN_JOB_MIN_FREE_DISK_BYTES", "0")
    monkeypatch.setattr(main, "OptimizedPlaywrightManager", BrowserPool)
    server = main.create_mcp_server(main.asgi_client_factory(main.app))
    main.create_streamable_http_app(server)
    monkeypatch.setattr(main, "mcp_http_server", server)
    async with main.lifespan(main.app):
        service = main.app.state.messaging_service
        bridge = main.app.state.safari_bridge
        controller = main.app.state.outbound_controller
        pool = main.app.state.browser_manager
        assert isinstance(service.adapter, SafariMessagingAdapter)
        assert service.adapter.controller is controller
        assert service.adapter.bridge is bridge
        pairing = json.loads(
            (tmp_path / "messaging" / "safari-pairing.json").read_text()
        )
        assert bridge.authenticate(pairing["token"])
        assert (
            await bridge.exchange(
                {"version": 1, "action": "poll", "profile_id": "default"}
            )
        )["command"] is None
    assert pool.closed
    assert bridge._closed
    assert main.app.state.safari_bridge is None
    assert main.app.state.messaging_service is None
