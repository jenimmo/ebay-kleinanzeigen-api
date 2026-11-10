"""Exact raster proof and pre-click persistence handshake; entirely offline."""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
import httpx
import pytest

from messaging.contracts import MessagingError
from messaging.image_evidence import image_preview, pixel_proof, receipt_evidence
from messaging.safari import SafariBridge
from routers.messaging import router
from utils.http_security import MessagingAccessMiddleware


PIXELS = {"algorithm": "srgb-rgba8-v1", "width": 16, "height": 16, "sha256": "a" * 64}
PREVIEW = {"preview_sha256": "b" * 64, "image_pixels": PIXELS}


@pytest.mark.parametrize(
    "changes",
    [
        {"algorithm": "approximate"},
        {"width": True},
        {"height": 1.0},
        {"width": 0},
        {"height": -1},
        {"width": 4000001, "height": 1},
        {"width": 2001, "height": 2000},
        {"sha256": "A" * 64},
        {"sha256": "a" * 63},
        {"url": "PRIVATE"},
    ],
)
def test_pixel_proof_rejects_nonexact_malformed_and_oversized_rasters(changes):
    with pytest.raises(ValueError):
        pixel_proof({**PIXELS, **changes})


def test_pixel_proof_copies_the_complete_bounded_contract():
    assert pixel_proof(PIXELS) == PIXELS
    assert pixel_proof(PIXELS) is not PIXELS
    assert pixel_proof({**PIXELS, "width": 2000, "height": 2000})["width"] == 2000
    assert image_preview(PREVIEW) == PREVIEW
    assert image_preview(PREVIEW)["image_pixels"] is not PIXELS
    with pytest.raises(ValueError):
        image_preview({**PREVIEW, "token": "PRIVATE"})
    with pytest.raises(ValueError):
        image_preview({"preview_sha256": "b" * 64, "image_pixels": None})


def test_byte_receipt_stays_compatible_and_pixel_receipt_requires_exact_prior_proof():
    byte_evidence = {"source": "fresh_conversation_dom", "sha256": "b" * 64}
    assert receipt_evidence(byte_evidence, "b" * 64) == byte_evidence
    assert receipt_evidence(byte_evidence, "b" * 64, PIXELS) == byte_evidence
    evidence = {
        "source": "fresh_conversation_dom",
        "sha256": "c" * 64,
        "comparison": "exact_pixels",
        "pixels": PIXELS,
    }
    assert receipt_evidence(evidence, "b" * 64, PIXELS) == evidence
    assert receipt_evidence(evidence, "b" * 64, PIXELS)["pixels"] is not PIXELS
    for changed, prior in [
        (evidence, None),
        ({**evidence, "sha256": "b" * 64}, PIXELS),
        ({**evidence, "pixels": {**PIXELS, "width": 17}}, PIXELS),
        ({**evidence, "pixels": {**PIXELS, "sha256": "d" * 64}}, PIXELS),
        ({**evidence, "comparison": "perceptual"}, PIXELS),
        ({**evidence, "tolerance": 1}, PIXELS),
    ]:
        with pytest.raises(ValueError):
            receipt_evidence(changed, "b" * 64, prior)


async def delivered_image(bridge, callback):
    account = {"account_id": "own", "user_id": "111"}
    bridge._bindings["own"] = {"profile_id": "default", "user_id": "111", "tab_id": 1}
    task = asyncio.create_task(
        bridge.request(
            account,
            "send_step",
            {"kind": "image"},
            image={"data": b"upload"},
            on_image_preview=callback,
        )
    )
    async with asyncio.timeout(2):
        while True:
            reply = await bridge.exchange(
                {"version": 1, "profile_id": "default", "action": "poll"}
            )
            if reply["command"]:
                return task, reply["command"]
            await asyncio.sleep(0)


def preview_request(command, **changes):
    return {
        "version": 1,
        "profile_id": "default",
        "action": "image_preview",
        "command_id": command["id"],
        **deepcopy(PREVIEW),
        **changes,
    }


async def test_preview_ack_waits_for_commit_and_cannot_replace_an_existing_proof(
    tmp_path,
):
    bridge = SafariBridge(tmp_path)
    entered, commit = asyncio.Event(), asyncio.Event()
    observed = []

    async def save(preview):
        observed.append(deepcopy(preview))
        entered.set()
        await commit.wait()

    task, command = await delivered_image(bridge, save)
    ack = asyncio.create_task(bridge.exchange(preview_request(command)))
    try:
        await entered.wait()
        assert not ack.done()
        assert bridge._commands[command["id"]].preview is None
        commit.set()
        assert await ack == {"accepted": True}
        assert observed == [PREVIEW]
        assert await bridge.exchange(preview_request(command)) == {"accepted": True}
        assert observed == [PREVIEW]  # Identical duplicate does not re-commit or click.
        with pytest.raises(MessagingError) as caught:
            await bridge.exchange(preview_request(command, preview_sha256="d" * 64))
        assert caught.value.code == "outcome_unknown"
        assert bridge._commands[command["id"]].preview == PREVIEW
    finally:
        commit.set()
        await bridge.close()
        await asyncio.gather(task, ack, return_exceptions=True)


@pytest.mark.parametrize(
    "changes",
    [
        {"profile_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
        {"command_id": "other-command"},
        {"url": "PRIVATE"},
        {"image_pixels": {**PIXELS, "width": True}},
        {"preview_sha256": "B" * 64},
        {"image_pixels": None},
    ],
)
async def test_preview_handshake_rejects_wrong_command_profile_or_payload_before_commit(
    tmp_path, changes
):
    bridge = SafariBridge(tmp_path)
    saved = []

    async def save(value):
        saved.append(value)

    task, command = await delivered_image(bridge, save)
    try:
        with pytest.raises(MessagingError):
            await bridge.exchange(preview_request(command, **changes))
        assert saved == []
    finally:
        await bridge.close()
        await asyncio.gather(task, return_exceptions=True)


async def test_preview_storage_failure_or_expired_command_never_acknowledges_send(
    tmp_path,
):
    bridge = SafariBridge(tmp_path, command_timeout=0.1)
    entered, release = asyncio.Event(), asyncio.Event()

    async def save(value):
        entered.set()
        await release.wait()

    task, command = await delivered_image(bridge, save)
    ack = asyncio.create_task(bridge.exchange(preview_request(command)))
    try:
        await entered.wait()
        with pytest.raises(MessagingError) as caught:
            await task
        assert caught.value.code == "safari_timeout"
        release.set()
        with pytest.raises(MessagingError) as late:
            await ack
        assert late.value.code == "safari_command_gone"
        assert not bridge._commands
    finally:
        release.set()
        await bridge.close()
        await asyncio.gather(task, ack, return_exceptions=True)


async def test_preview_commit_failure_returns_no_ack_and_retains_no_proof(tmp_path):
    bridge = SafariBridge(tmp_path)

    async def save(value):
        raise MessagingError("unsafe_storage", "Speichern fehlgeschlagen.", 503)

    task, command = await delivered_image(bridge, save)
    try:
        with pytest.raises(MessagingError) as caught:
            await bridge.exchange(preview_request(command))
        assert caught.value.code == "unsafe_storage"
        assert bridge._commands[command["id"]].preview is None
    finally:
        await bridge.close()
        await asyncio.gather(task, return_exceptions=True)


async def test_native_preview_capability_is_profile_scoped_and_body_cannot_assert_it(
    tmp_path,
):
    bridge = SafariBridge(tmp_path)
    other_profile = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    bridge._bindings = {
        "own": {"profile_id": "default", "user_id": "111", "tab_id": 1},
        "second": {"profile_id": other_profile, "user_id": "222", "tab_id": 2},
    }
    poll = {
        "version": 1,
        "profile_id": "default",
        "action": "poll",
        "image_preview_capable": True,
    }
    assert not any(
        a["image_preview_capable"] for a in (await bridge.status())["accounts"]
    )
    await bridge.exchange(poll)  # Old native host can pass arbitrary JS body fields.
    assert not any(
        a["image_preview_capable"] for a in (await bridge.status())["accounts"]
    )
    await bridge.exchange(poll, native_image_preview_capable=True)
    accounts = {a["account_id"]: a for a in (await bridge.status())["accounts"]}
    assert accounts["own"]["image_preview_capable"] is True
    assert accounts["second"]["image_preview_capable"] is False
    assert accounts["own"]["last_native_poll_at"]
    serialized = json.dumps(await bridge.status())
    assert other_profile not in serialized
    assert '"user_id"' not in serialized and '"profile_id"' not in serialized
    await bridge.exchange(poll, native_image_preview_capable=1)
    assert (await bridge.status())["accounts"][0]["image_preview_capable"] is False
    await bridge.exchange(poll, native_image_preview_capable=True)
    await bridge.exchange(poll)  # A later old host poll must clear the claim.
    assert (await bridge.status())["accounts"][0]["image_preview_capable"] is False


@pytest.mark.parametrize(
    "headers,expected",
    [
        ([], False),
        ([("X-Kleinanzeigen-Native-Image-Preview", "1")], True),
        ([("X-Kleinanzeigen-Native-Image-Preview", "true")], False),
        (
            [
                ("X-Kleinanzeigen-Native-Image-Preview", "1"),
                ("X-Kleinanzeigen-Native-Image-Preview", "1"),
            ],
            False,
        ),
    ],
)
async def test_authenticated_native_header_is_separate_from_untrusted_body(
    headers, expected
):
    app = FastAPI()
    bridge = SimpleNamespace(
        authenticate=lambda token: token == "test-only",
        exchange=AsyncMock(return_value={"command": None}),
    )
    app.state.safari_bridge = bridge
    app.include_router(router)
    app.add_middleware(MessagingAccessMiddleware)
    body = {
        "version": 1,
        "action": "poll",
        "profile_id": "default",
        "image_preview_capable": True,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        result = await client.post(
            "/messaging/safari-bridge/exchange",
            json=body,
            headers=[("Authorization", "Bearer test-only"), *headers],
        )
    assert result.status_code == 200
    bridge.exchange.assert_awaited_once_with(
        body, native_image_preview_capable=expected
    )
