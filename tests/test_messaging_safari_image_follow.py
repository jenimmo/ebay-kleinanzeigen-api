"""Image receipts bind immutable uploads to fresh, independently read DOM bytes."""

import asyncio
from datetime import datetime, timezone
import hashlib
import io
from unittest.mock import AsyncMock

from PIL import Image
import pytest

from messaging.contracts import MessagingError, MessagingSettings
from messaging.safari import SafariBridge, SafariMessagingAdapter
from messaging.service import MessagingService


ACCOUNT = {"account_id": "own-a", "user_id": "111", "state": "connected"}
TARGET = {
    "target_kind": "conversation",
    "target_id": "conversation-1",
    "conversation_id": "conversation-1",
    "listing_id": "1234567890",
    "account_user_id": "111",
    "recipient_user_id": "222",
    "capabilities": {"can_send_text": True, "can_send_images": True},
}
PREVIEW_HASH = hashlib.sha256(b"transcoded JPEG preview").hexdigest()


def png():
    buffer = io.BytesIO()
    Image.new("RGB", (3, 3), (20, 100, 40)).save(buffer, "PNG")
    return buffer.getvalue()


def observation(**changes):
    return {
        "kind": "image",
        "conversation_id": "conversation-1",
        "message_id": "image-message-1",
        "image_sha256": PREVIEW_HASH,
        **changes,
    }


def receipt(**changes):
    return {
        "confirmed": True,
        "kind": "image",
        "conversation_id": "conversation-1",
        "message_id": "image-message-1",
        "recipient_user_id": "222",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "image_evidence": {
            "source": "fresh_conversation_dom",
            "sha256": PREVIEW_HASH,
        },
        **changes,
    }


def setup_adapter(tmp_path, *, follow_changes=None, receipt_changes=None, direct=False, preview=None):
    path = tmp_path / "upload.png"
    path.write_bytes(png())
    image = {
        "path": str(path),
        "image_id": "image-1",
        "mime_type": "image/png",
        "sha256": hashlib.sha256(png()).hexdigest(),
    }
    adapter = SafariMessagingAdapter(tmp_path, None, SafariBridge(tmp_path))
    calls = []
    observed = AsyncMock()
    before = AsyncMock()

    async def call(account, method, args, **kwargs):
        calls.append((method, args))
        if method == "send_step":
            assert kwargs["image"]["sha256"] == image["sha256"]
            await kwargs["before_dispatch"]()
            if preview is not None:
                await kwargs["on_image_preview"](preview)
            if direct:
                return receipt()
            return {
                "_follow": observation(
                    dispatched_at=datetime.now(timezone.utc).isoformat(),
                    **(follow_changes or {}),
                )
            }
        assert method == "confirm_send"
        assert "before_dispatch" not in kwargs
        observed.assert_awaited_once()
        return receipt(**(receipt_changes or {}))

    adapter._call = call
    return adapter, image, calls, observed, before


async def send(adapter, image, observed, before, **kwargs):
    return await adapter.send_step(
        ACCOUNT,
        TARGET,
        "image",
        image=image,
        before_dispatch=before,
        on_conversation_observed=observed,
        **kwargs,
    )


async def test_image_follow_is_journaled_before_separate_confirmation(tmp_path):
    adapter, image, calls, observed, before = setup_adapter(tmp_path)
    result = await send(adapter, image, observed, before)
    assert image["sha256"] != PREVIEW_HASH  # Real UI transcoding is expected.
    before.assert_awaited_once()
    hint = observed.await_args.args[0]
    assert hint["kind"] == "image"
    assert hint["image_sha256"] == PREVIEW_HASH
    assert [method for method, _ in calls] == ["send_step", "confirm_send"]
    assert calls[1][1] == {
        "kind": "image",
        "target": TARGET,
        "conversation_id": "conversation-1",
        "expected_message_id": "image-message-1",
        "expected_image_sha256": PREVIEW_HASH,
        "not_before": hint["not_before"],
        "text": "",
        "baseline_message_ids": [],
    }
    assert result["image_evidence"] == {
        "source": "fresh_conversation_dom",
        "sha256": PREVIEW_HASH,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "text"},
        {"message_id": None},
        {"message_id": ""},
        {"conversation_id": "conversation-other"},
        {"image_sha256": None},
        {"image_sha256": PREVIEW_HASH.upper()},
        {"image_sha256": "a" * 63},
        {"image_sha256": {"secret": "not a hash"}},
        {"url": "https://example.org/private"},
    ],
)
async def test_invalid_image_follow_is_not_committed_or_confirmed(tmp_path, changes):
    adapter, image, calls, observed, before = setup_adapter(
        tmp_path, follow_changes=changes
    )
    with pytest.raises(MessagingError, match="Versand") as error:
        await send(adapter, image, observed, before)
    assert error.value.code == "outcome_unknown"
    assert len(calls) == 1
    observed.assert_not_awaited()
    before.assert_awaited_once()


async def test_image_never_accepts_direct_receipt_without_fresh_read(tmp_path):
    adapter, image, calls, observed, before = setup_adapter(tmp_path, direct=True)
    with pytest.raises(MessagingError) as error:
        await send(adapter, image, observed, before)
    assert error.value.code == "outcome_unknown"
    observed.assert_not_awaited()
    assert len(calls) == 1


async def test_failed_image_observation_commit_prevents_confirmation(tmp_path):
    adapter, image, calls, observed, before = setup_adapter(tmp_path)
    observed.side_effect = RuntimeError("storage unavailable")
    with pytest.raises(RuntimeError):
        await send(adapter, image, observed, before)
    assert len(calls) == 1
    before.assert_awaited_once()


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": None},
        {"kind": "text"},
        {"confirmed": False},
        {"recipient_user_id": "333"},
        {"conversation_id": "conversation-other"},
        {"message_id": "message-other"},
        {"_follow": {}},
        {"image_evidence": None},
        {"image_evidence": {"source": "preview", "sha256": PREVIEW_HASH}},
        {"image_evidence": {"source": "fresh_conversation_dom", "sha256": "a" * 64}},
        {
            "image_evidence": {
                "source": "fresh_conversation_dom",
                "sha256": PREVIEW_HASH,
                "url": "private",
            }
        },
        {"created_at": None},
        {"created_at": "2020-01-01T00:00:00Z"},
        {"created_at": "2099-01-01T00:00:00Z"},
        {"created_at": "2026-11-04T12:00:00"},
    ],
)
async def test_wrong_image_receipt_stays_unknown_and_never_repeats_send(
    tmp_path, changes
):
    adapter, image, calls, observed, before = setup_adapter(
        tmp_path, receipt_changes=changes
    )
    with pytest.raises(MessagingError) as error:
        await send(adapter, image, observed, before)
    assert error.value.code == "outcome_unknown"
    assert [method for method, _ in calls] == ["send_step", "confirm_send"]
    before.assert_awaited_once()
    observed.assert_awaited_once()


class ObservingAdapter:
    supports_conversation_observation = True

    def __init__(self, *, timeout=False, bad_receipt=None, second_observation=None):
        self.timeout = timeout
        self.bad_receipt = bad_receipt or {}
        self.second_observation = second_observation
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
        image,
        **kwargs,
    ):
        await before_dispatch()
        self.sends += 1
        await on_conversation_observed(observation())
        if self.second_observation:
            await on_conversation_observed(observation(**self.second_observation))
        stored = (await self.service.store.operations({"running"}))[0]
        step = stored["steps"][0]
        assert step["state"] == "dispatching"
        assert step["observed_image_sha256"] == PREVIEW_HASH
        assert step["observed_attachment_sha256"] == image["sha256"]
        assert step["observed_message_id"] == "image-message-1"
        assert "image_evidence" not in step
        if self.timeout:
            raise TimeoutError("lost read result")
        return receipt(**self.bad_receipt)

    async def get_conversation(self, account, conversation_id, **kwargs):
        self.reads.append(conversation_id)
        return {
            "conversation_id": conversation_id,
            "recipient_user_id": "222",
            "messages": [],
        }

    async def close(self):
        pass


async def service_with_adapter(tmp_path, adapter):
    service = MessagingService(MessagingSettings(root=tmp_path), adapter)
    adapter.service = service
    await service.start()
    await service.store.save_account(ACCOUNT)
    return service


async def submit_image(service):
    image = await service.upload_image("own-a", png())
    operation = await service.submit_send(
        "own-a",
        "conversation",
        "conversation-1",
        attachment_ids=[image["attachment_id"]],
        idempotency_key="one-image",
    )
    return image, operation


async def finished(service, operation):
    async with asyncio.timeout(2):
        while True:
            stored = await service.store.operation("own-a", operation["operation_id"])
            if stored["state"] not in {"queued", "running"}:
                return stored
            await asyncio.sleep(0)


async def test_image_observation_survives_restart_and_is_never_a_send_receipt(tmp_path):
    adapter = ObservingAdapter(timeout=True)
    service = await service_with_adapter(tmp_path, adapter)
    try:
        image, operation = await submit_image(service)
        stored = await finished(service, operation)
        assert stored["state"] == "outcome_unknown"
        assert stored["steps"][0]["observed_image_sha256"] == PREVIEW_HASH
        assert (
            stored["steps"][0]["observed_attachment_sha256"]
            == hashlib.sha256(png()).hexdigest()
        )
        assert adapter.sends == 1
    finally:
        await service.close()
    after_restart = ObservingAdapter()
    service = await service_with_adapter(tmp_path, after_restart)
    try:
        current = await service.get_operation("own-a", operation["operation_id"])
        assert current["state"] == "outcome_unknown"
        assert current["reconciliation"]["automatic_resend"] is False
        assert "image_evidence" not in current["steps"][0]
        replay = await service.submit_send(
            "own-a",
            "conversation",
            "conversation-1",
            attachment_ids=[image["attachment_id"]],
            idempotency_key="one-image",
        )
        assert replay["operation_id"] == operation["operation_id"]
        assert replay["state"] == "outcome_unknown"
        assert after_restart.sends == 0
        assert after_restart.reads == ["conversation-1"]
    finally:
        await service.close()


async def test_confirmed_image_evidence_is_durable_before_archive_failure(tmp_path):
    adapter = ObservingAdapter()
    service = await service_with_adapter(tmp_path, adapter)
    service.store.archive_messages = AsyncMock(
        side_effect=RuntimeError("archive failure")
    )
    try:
        _, operation = await submit_image(service)
        stored = await finished(service, operation)
        assert stored["state"] == "sent"
        assert "archive_warning" in stored
        evidence = stored["steps"][0]["image_evidence"]
        assert evidence["source"] == "fresh_conversation_dom"
        assert evidence["sha256"] == PREVIEW_HASH
        assert evidence["attachment_sha256"] == hashlib.sha256(png()).hexdigest()
        assert evidence["message_created_at"]
        assert evidence["confirmed_at"]
    finally:
        await service.close()
    after_restart = ObservingAdapter()
    service = await service_with_adapter(tmp_path, after_restart)
    try:
        stored = await service.store.operation("own-a", operation["operation_id"])
        assert stored["state"] == "sent"
        assert stored["steps"][0]["image_evidence"] == evidence
        assert after_restart.sends == 0
        assert after_restart.reads == []
    finally:
        await service.close()


async def test_first_contact_text_is_not_duplicated_after_unknown_image(tmp_path):
    class FirstContactAdapter(ObservingAdapter):
        async def prepare_send(self, account, kind, target_id):
            if kind == "listing":
                return {
                    **TARGET,
                    "target_kind": kind,
                    "target_id": target_id,
                    "conversation_id": None,
                    "capabilities": {"can_send_text": True, "can_send_images": False},
                }
            return dict(TARGET)

        async def send_step(self, account, target, kind, **kwargs):
            if kind == "text":
                await kwargs["before_dispatch"]()
                self.sends += 1
                await kwargs["on_conversation_observed"](
                    {
                        "conversation_id": "conversation-1",
                        "message_id": "text-message-1",
                    }
                )
                return {
                    "message_id": "text-message-1",
                    "conversation_id": "conversation-1",
                    "recipient_user_id": "222",
                }
            await kwargs["before_dispatch"]()
            self.sends += 1
            await kwargs["on_conversation_observed"](observation())
            raise TimeoutError("image confirmation lost")

    adapter = FirstContactAdapter()
    service = await service_with_adapter(tmp_path, adapter)
    try:
        image = await service.upload_image("own-a", png())
        options = {
            "text": "Grüße mit Bild",
            "attachment_ids": [image["attachment_id"]],
            "idempotency_key": "one-first-contact-with-image",
        }
        operation = await service.submit_send(
            "own-a", "listing", "1234567890", **options
        )
        stored = await finished(service, operation)
        assert stored["state"] == "outcome_unknown"
        assert [step["state"] for step in stored["steps"]] == [
            "sent",
            "outcome_unknown",
        ]
        assert stored["steps"][0]["message_id"] == "text-message-1"
        assert stored["steps"][1]["observed_message_id"] == "image-message-1"
        assert adapter.sends == 2
        replay = await service.submit_send("own-a", "listing", "1234567890", **options)
        assert replay["operation_id"] == operation["operation_id"]
        assert replay["state"] == "outcome_unknown"
        assert adapter.sends == 2
    finally:
        await service.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"image_evidence": None},
        {"message_id": "wrong-message"},
        {
            "image_evidence": {
                "source": "fresh_conversation_dom",
                "sha256": hashlib.sha256(png()).hexdigest(),
            }
        },
    ],
)
async def test_service_cannot_confirm_a_different_image_or_message(tmp_path, changes):
    adapter = ObservingAdapter(bad_receipt=changes)
    service = await service_with_adapter(tmp_path, adapter)
    try:
        _, operation = await submit_image(service)
        stored = await finished(service, operation)
        assert stored["state"] == "outcome_unknown"
        assert "image_evidence" not in stored["steps"][0]
        assert adapter.sends == 1
    finally:
        await service.close()


@pytest.mark.parametrize(
    "changes",
    [{"message_id": "other-message"}, {"image_sha256": "b" * 64}],
)
async def test_conflicting_image_observation_never_replaces_durable_hint(
    tmp_path, changes
):
    adapter = ObservingAdapter(second_observation=changes)
    service = await service_with_adapter(tmp_path, adapter)
    try:
        _, operation = await submit_image(service)
        stored = await finished(service, operation)
        assert stored["state"] == "outcome_unknown"
        step = stored["steps"][0]
        assert step["observed_image_sha256"] == PREVIEW_HASH
        assert step["observed_message_id"] == "image-message-1"
        assert "image_evidence" not in step
        assert adapter.sends == 1
    finally:
        await service.close()


PIXELS = {"algorithm": "srgb-rgba8-v1", "width": 16, "height": 16, "sha256": "c" * 64}
RECEIVED_HASH = "d" * 64


def pixel_evidence(**changes):
    return {
        "source": "fresh_conversation_dom", "sha256": RECEIVED_HASH,
        "comparison": "exact_pixels", "pixels": dict(PIXELS), **changes,
    }


async def test_exact_pixel_adapter_commits_before_send_follow_and_separate_confirmation(tmp_path):
    prepared = AsyncMock()
    adapter, image, calls, observed, before = setup_adapter(
        tmp_path,
        preview={"preview_sha256": PREVIEW_HASH, "image_pixels": PIXELS},
        follow_changes={"image_pixels": PIXELS},
        receipt_changes={"image_evidence": pixel_evidence()},
    )

    async def observe(value):
        prepared.assert_awaited_once()
        assert value["image_pixels"] == PIXELS

    observed.side_effect = observe
    result = await send(adapter, image, observed, before, on_image_prepared=prepared)
    assert prepared.await_args.args[0] == {"preview_sha256": PREVIEW_HASH, "image_pixels": PIXELS}
    assert calls[1][1]["expected_image_pixels"] == PIXELS
    assert result["image_evidence"] == pixel_evidence()
    assert result["image_evidence"]["sha256"] != PREVIEW_HASH != image["sha256"]
    assert [method for method, _ in calls] == ["send_step", "confirm_send"]
    before.assert_awaited_once()


async def test_pixel_follow_without_acknowledged_preclick_proof_never_confirms(tmp_path):
    adapter, image, calls, observed, before = setup_adapter(
        tmp_path, follow_changes={"image_pixels": PIXELS},
        receipt_changes={"image_evidence": pixel_evidence()},
    )
    with pytest.raises(MessagingError) as caught:
        await send(adapter, image, observed, before)
    assert caught.value.code == "outcome_unknown"
    assert len(calls) == 1
    observed.assert_not_awaited()


@pytest.mark.parametrize("changed", [
    {**PIXELS, "width": 17}, {**PIXELS, "sha256": "a" * 64}, None,
])
async def test_follow_cannot_replace_or_drop_committed_preview_pixels(tmp_path, changed):
    adapter, image, calls, observed, before = setup_adapter(
        tmp_path, preview={"preview_sha256": PREVIEW_HASH, "image_pixels": PIXELS},
        follow_changes={"image_pixels": changed},
    )
    prepared = AsyncMock()
    with pytest.raises(MessagingError) as caught:
        await send(adapter, image, observed, before, on_image_prepared=prepared)
    assert caught.value.code == "outcome_unknown"
    prepared.assert_awaited_once()
    observed.assert_not_awaited()
    assert len(calls) == 1


@pytest.mark.parametrize("changed", [
    {"pixels": {**PIXELS, "width": 17}},
    {"pixels": {**PIXELS, "sha256": "a" * 64}},
    {"comparison": "similar_pixels"},
    {"sha256": PREVIEW_HASH},
    {"url": "PRIVATE"},
])
async def test_pixel_receipt_requires_exact_prior_raster_without_tolerance(tmp_path, changed):
    adapter, image, calls, observed, before = setup_adapter(
        tmp_path, preview={"preview_sha256": PREVIEW_HASH, "image_pixels": PIXELS},
        follow_changes={"image_pixels": PIXELS},
        receipt_changes={"image_evidence": pixel_evidence(**changed)},
    )
    with pytest.raises(MessagingError) as caught:
        await send(adapter, image, observed, before, on_image_prepared=AsyncMock())
    assert caught.value.code == "outcome_unknown"
    assert [method for method, _ in calls] == ["send_step", "confirm_send"]
    before.assert_awaited_once()


class PixelAdapter(ObservingAdapter):
    supports_image_preparation = True

    def __init__(self, *, conflict=None, **kwargs):
        super().__init__(**kwargs)
        self.conflict = conflict

    async def send_step(self, account, target, kind, *, before_dispatch,
                        on_image_prepared, on_conversation_observed, image, **kwargs):
        await before_dispatch()
        preview = {"preview_sha256": PREVIEW_HASH, "image_pixels": dict(PIXELS)}
        await on_image_prepared(preview)
        before_click = (await self.service.store.operations({"running"}))[0]["steps"][0]
        assert before_click["state"] == "dispatching"
        assert before_click["prepared_image_sha256"] == PREVIEW_HASH
        assert before_click["prepared_image_pixels"] == PIXELS
        assert before_click["prepared_attachment_sha256"] == image["sha256"]
        assert before_click["image_prepared_at"]
        assert "observed_image_pixels" not in before_click
        if self.conflict:
            await on_image_prepared({**preview, **self.conflict})
        self.sends += 1  # The simulated single click is strictly after durable ACK.
        await on_conversation_observed(observation(image_pixels=dict(PIXELS)))
        before_read = (await self.service.store.operations({"running"}))[0]["steps"][0]
        assert before_read["observed_image_pixels"] == PIXELS
        assert before_read["observed_image_sha256"] == PREVIEW_HASH
        assert "image_evidence" not in before_read
        if self.timeout:
            raise TimeoutError("lost pixel confirmation")
        return receipt(image_evidence=pixel_evidence(), **self.bad_receipt)


async def test_pixel_evidence_is_durable_and_separate_from_upload_preview_and_received_bytes(tmp_path):
    adapter = PixelAdapter()
    service = await service_with_adapter(tmp_path, adapter)
    service.store.archive_messages = AsyncMock(side_effect=RuntimeError("archive failure"))
    try:
        _, operation = await submit_image(service)
        stored = await finished(service, operation)
        assert stored["state"] == "sent"
        step = stored["steps"][0]
        assert step["prepared_image_pixels"] == step["observed_image_pixels"] == PIXELS
        assert step["prepared_image_sha256"] == step["observed_image_sha256"] == PREVIEW_HASH
        assert step["prepared_attachment_sha256"] == hashlib.sha256(png()).hexdigest()
        evidence = step["image_evidence"]
        assert evidence["comparison"] == "exact_pixels"
        assert evidence["sha256"] == RECEIVED_HASH
        assert evidence["pixels"] == PIXELS
        assert evidence["preview_sha256"] == PREVIEW_HASH
        assert evidence["attachment_sha256"] == hashlib.sha256(png()).hexdigest()
        assert evidence["message_created_at"] and evidence["confirmed_at"]
        assert adapter.sends == 1
        assert "archive_warning" in stored
    finally:
        await service.close()
    restarted = PixelAdapter()
    service = await service_with_adapter(tmp_path, restarted)
    try:
        restored = await service.store.operation("own-a", operation["operation_id"])
        assert restored["state"] == "sent"
        assert restored["steps"][0]["image_evidence"] == evidence
        assert restarted.sends == 0
    finally:
        await service.close()


async def test_unknown_pixel_send_survives_restart_without_resend_or_retrospective_promotion(tmp_path):
    adapter = PixelAdapter(timeout=True)
    service = await service_with_adapter(tmp_path, adapter)
    try:
        image, operation = await submit_image(service)
        stored = await finished(service, operation)
        assert stored["state"] == "outcome_unknown"
        assert stored["steps"][0]["prepared_image_pixels"] == PIXELS
        assert "image_evidence" not in stored["steps"][0]
        assert adapter.sends == 1
    finally:
        await service.close()
    restarted = PixelAdapter()
    service = await service_with_adapter(tmp_path, restarted)
    try:
        current = await service.get_operation("own-a", operation["operation_id"])
        assert current["state"] == "outcome_unknown"
        assert "image_evidence" not in current["steps"][0]
        replay = await service.submit_send(
            "own-a", "conversation", "conversation-1",
            attachment_ids=[image["attachment_id"]], idempotency_key="one-image",
        )
        assert replay["operation_id"] == operation["operation_id"]
        assert replay["state"] == "outcome_unknown"
        assert restarted.sends == 0
        assert restarted.reads == ["conversation-1"]
    finally:
        await service.close()


@pytest.mark.parametrize("changed", [
    {"preview_sha256": "a" * 64},
    {"image_pixels": {**PIXELS, "height": 17}},
    {"image_pixels": {**PIXELS, "sha256": "a" * 64}},
])
async def test_conflicting_preclick_preview_never_replaces_the_durable_proof(tmp_path, changed):
    adapter = PixelAdapter(conflict=changed)
    service = await service_with_adapter(tmp_path, adapter)
    try:
        _, operation = await submit_image(service)
        stored = await finished(service, operation)
        assert stored["state"] == "outcome_unknown"
        step = stored["steps"][0]
        assert step["prepared_image_sha256"] == PREVIEW_HASH
        assert step["prepared_image_pixels"] == PIXELS
        assert "image_evidence" not in step
        assert adapter.sends == 0
    finally:
        await service.close()


@pytest.mark.parametrize("reconstruct", [False, True])
async def test_sent_image_archive_keeps_upload_original_separate_from_received_attachment(tmp_path, reconstruct):
    received_file = io.BytesIO()
    Image.new("RGB", (3, 3), (20, 100, 40)).save(received_file, "JPEG")
    received_bytes = received_file.getvalue()

    class ReadingPixelAdapter(PixelAdapter):
        async def get_conversation(self, account, conversation_id, **kwargs):
            self.reads.append(conversation_id)
            return {
                "conversation_id": conversation_id, "recipient_user_id": "222",
                "messages": [{
                    "message_id": "image-message-1", "sender_user_id": "111", "text": "",
                    "images": [{"remote_id": "image-message-1:0", "data": received_bytes}],
                }],
            }

    adapter = ReadingPixelAdapter()
    service = await service_with_adapter(tmp_path, adapter)
    if reconstruct:
        service.store.archive_messages = AsyncMock(side_effect=RuntimeError("crash before message archive"))
    try:
        upload, operation = await submit_image(service)
        stored = await finished(service, operation)
        assert stored["state"] == "sent"
        if reconstruct:
            # Durable state immediately after receipt commit, before the worker
            # committed its final operation state or the message archive.
            stored["state"] = "running"
            await service.store.save_operation(stored)
    finally:
        await service.close()

    restarted = ReadingPixelAdapter()
    service = await service_with_adapter(tmp_path, restarted)
    try:
        before = await service.get_conversation("own-a", "conversation-1", source="archive")
        message = before["messages"][0]
        assert len(message["images"]) == 1
        assert message["images"][0]["unavailable"] is True
        assert message["images"][0]["remote_id"] == "image-message-1:0"
        assert len(message["upload_originals"]) == 1
        original = message["upload_originals"][0]
        assert original["source"] == "upload_original"
        assert original["image_id"] == upload["attachment_id"]
        assert "path" not in original and "data" not in original
        assert restarted.sends == 0 and restarted.reads == []

        live = await service.get_conversation("own-a", "conversation-1")
        assert len(live["messages"][0]["images"]) == 1
        received = live["messages"][0]["images"][0]
        assert received["mime_type"] == "image/jpeg"
        assert received["image_id"] != original["image_id"]
        archived = await service.get_conversation("own-a", "conversation-1", source="archive")
        assert archived["messages"][0]["images"] == [received]
        assert archived["messages"][0]["upload_originals"] == [original]
        assert (await service.get_message_image("own-a", original["image_id"]))["data"] == png()
        assert (await service.get_message_image("own-a", received["image_id"]))["data"] == received_bytes

        # Rebuilding a known receipt again cannot overwrite a retrieved image
        # with the placeholder or duplicate the local source reference.
        saved = await service.store.operation("own-a", operation["operation_id"])
        await service._archive_confirmed_steps(saved)
        again = await service.get_conversation("own-a", "conversation-1", source="archive")
        assert again["messages"][0]["images"] == [received]
        assert again["messages"][0]["upload_originals"] == [original]
        assert restarted.sends == 0
    finally:
        await service.close()
