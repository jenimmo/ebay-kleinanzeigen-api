from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from messaging.safari import SafariMessagingAdapter
from utils.outbound_control import OutboundCircuitOpen, OutboundController, OutboundSettings


@pytest.mark.parametrize("signal", ["upstream_blocked", "upstream_rate_limited"])
async def test_provider_restriction_preserves_chat_and_blocks_next_access(tmp_path, signal):
    controller = OutboundController(OutboundSettings(min_interval_seconds=0, jitter_seconds=0))
    account = {"account_id": "own", "user_id": "111"}
    bridge = SimpleNamespace(
        binding=lambda _: {}, command_timeout=45,
        request=AsyncMock(return_value={
            "account_user_id": "111", "conversation_id": "c1", "recipient_user_id": "222",
            "messages": [{"message_id": "m1", "sender_user_id": "222", "text": "Bereits gelesen"}],
            "provider_context": {"upstream_error": signal}, "_upstream_reached": True,
        }),
    )
    adapter = SafariMessagingAdapter(tmp_path, controller, bridge)
    try:
        result = await adapter.get_conversation(account, "c1")
        assert result["messages"][0]["text"] == "Bereits gelesen"
        assert result["access_warning"]["code"] == signal
        with pytest.raises(OutboundCircuitOpen):
            await adapter.get_conversation(account, "c1")
        assert bridge.request.await_count == 1
    finally:
        await controller.close()


@pytest.mark.parametrize("signal", [None, [], {}, "untrusted_exception"])
async def test_unrecognized_signal_cannot_break_archive_or_inject_error(tmp_path, signal):
    controller = SimpleNamespace(
        report_interaction_block=AsyncMock(),
    )

    async def run(operation, **kwargs):
        return await operation()

    controller.run_interaction = run
    bridge = SimpleNamespace(binding=lambda _: {}, command_timeout=45, request=AsyncMock(return_value={
        "account_user_id": "111", "conversation_id": "c1", "recipient_user_id": "222",
        "messages": [], "provider_context": {"upstream_error": signal},
    }))
    adapter = SafariMessagingAdapter(tmp_path, controller, bridge)
    result = await adapter.get_conversation({"account_id": "own", "user_id": "111"}, "c1")
    assert "access_warning" not in result
    controller.report_interaction_block.assert_not_awaited()
