"""Offline safety/receipt tests; never connect to Kleinanzeigen or send mail."""

from pathlib import Path
import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import messaging.browser as browser
from messaging.browser import MessagingBrowserAdapter, _page_slice
from messaging.contracts import MessagingError
from utils.outbound_control import OutboundController, OutboundSettings, UpstreamBlocked
import utils.url_security as url_security


ACCOUNT = {"account_id": "account-a", "user_id": "101"}
TARGET = {
    "target_kind": "conversation",
    "target_id": "conversation-1",
    "conversation_id": "conversation-1",
    "recipient_user_id": "202",
    "account_user_id": "101",
}


class Page:
    def __init__(self, value):
        self.value = value
        self.url = browser.INBOX_URL
        self.handlers = {}
        self.is_closed = Mock(return_value=False)

    async def evaluate(self, script, *args):
        return self.value

    def on(self, name, callback):
        self.handlers[name] = callback

    def remove_listener(self, name, callback):
        self.handlers.pop(name, None)


@pytest.fixture
def adapter(tmp_path):
    value = MessagingBrowserAdapter(
        tmp_path, SimpleNamespace(inspect_interaction_response=AsyncMock())
    )
    value.dom_timeout_seconds = 0.02
    return value


async def test_identity_never_substitutes_another_logged_in_account(adapter):
    page = Page({"ids": ["202"], "login_form": False})
    with pytest.raises(MessagingError) as exc:
        await adapter._identity(page, ACCOUNT)
    assert exc.value.code == "account_mismatch"


async def test_unbound_account_cannot_read_or_send(adapter):
    page = Page({"ids": ["101"], "login_form": False})
    with pytest.raises(MessagingError) as exc:
        await adapter._identity(page, {"account_id": "new"})
    assert exc.value.code == "reauth_required"
    assert (await adapter._identity(page, {"account_id": "new"}, connecting=True))[
        "user_id"
    ] == "101"


async def test_auth0_redirect_requests_reauthentication(adapter):
    page = Page({"ids": [], "login_form": False})
    page.url = "https://login.kleinanzeigen.de/u/login/identifier"
    with pytest.raises(MessagingError) as exc:
        await adapter._identity(page, ACCOUNT)
    assert exc.value.code == "reauth_required"


async def test_unknown_empty_ui_is_not_successful_empty_inbox(adapter):
    page = Page({"conversations": [], "known_container": False, "empty": False})
    with pytest.raises(MessagingError) as exc:
        await adapter._inbox(page)
    assert exc.value.code == "ui_unsupported"


async def test_conversation_requires_exact_remote_identity(adapter):
    page = Page(
        {
            "conversation_id": "conversation-1",
            "recipients": ["101", "202", "303"],
            "messages": [],
            "empty": True,
        }
    )
    with pytest.raises(MessagingError) as exc:
        await adapter._conversation(page, ACCOUNT, "conversation-1")
    assert exc.value.code == "ui_unsupported"


async def test_receipt_requires_new_unambiguous_outgoing_message(adapter):
    message = {
        "message_id": "m1",
        "sender_user_id": "101",
        "text": "Grüße\nÄpfel",
        "images": [],
    }
    snapshot = {
        "conversation_id": "conversation-1",
        "recipient_user_id": "202",
        "messages": [message],
    }
    adapter._conversation = AsyncMock(return_value=snapshot)
    assert (
        await adapter._receipt(
            None, ACCOUNT, TARGET, {"m1"}, "text", message["text"], set()
        )
        is None
    )
    receipt = await adapter._receipt(
        None, ACCOUNT, TARGET, set(), "text", message["text"], set()
    )
    assert receipt["message_id"] == "m1"
    snapshot["messages"].append({**message, "message_id": "m2"})
    assert (
        await adapter._receipt(
            None, ACCOUNT, TARGET, set(), "text", message["text"], set()
        )
        is None
    )


async def test_image_receipt_requires_matching_attachment_identity(adapter):
    adapter._conversation = AsyncMock(
        return_value={
            "conversation_id": "conversation-1",
            "recipient_user_id": "202",
            "messages": [
                {
                    "message_id": "m1",
                    "sender_user_id": "101",
                    "text": "",
                    "images": [{"remote_id": "upload-1"}],
                }
            ],
        }
    )
    assert (
        await adapter._receipt(None, ACCOUNT, TARGET, set(), "image", None, set())
        is None
    )
    assert (
        await adapter._receipt(None, ACCOUNT, TARGET, set(), "image", None, {"other"})
        is None
    )
    assert (
        await adapter._receipt(
            None, ACCOUNT, TARGET, set(), "image", None, {"upload-1"}
        )
    )["confirmed"]


class Composer:
    def __init__(self):
        self.button = SimpleNamespace(
            is_enabled=AsyncMock(return_value=True), click=AsyncMock()
        )
        self.editor = SimpleNamespace(fill=AsyncMock())
        self.evaluate = AsyncMock(
            return_value={
                "texts": [],
                "attachment_ids": [],
                "filenames": [],
                "unknown_previews": False,
            }
        )

    def get_by_role(self, *args, **kwargs):
        return self.button

    def locator(self, *args):
        return self.editor


def setup_send(adapter):
    composer = Composer()
    page = Page({"messages": []})
    target = {
        **TARGET,
        "capabilities": {
            "can_send_text": True,
            "can_send_images": False,
            "max_text_length": 20000,
        },
    }
    adapter._prepare = AsyncMock(return_value=target)
    adapter._composer = AsyncMock(return_value=composer)

    async def navigate(account, url, operation, **kwargs):
        return await operation(page, None)

    adapter._navigate = navigate
    return composer, page, target


async def test_durable_marker_failure_prevents_send_click(adapter):
    composer, page, target = setup_send(adapter)
    marker = AsyncMock(side_effect=RuntimeError("database unavailable"))
    with pytest.raises(RuntimeError):
        await adapter.send_step(
            ACCOUNT, target, "text", text="Grüße", before_dispatch=marker
        )
    composer.button.click.assert_not_awaited()
    assert not page.handlers


async def test_post_click_failure_is_unknown_and_not_retried(adapter):
    composer, page, target = setup_send(adapter)
    marker = AsyncMock()
    adapter._receipt = AsyncMock(side_effect=RuntimeError("lost browser"))
    with pytest.raises(MessagingError) as exc:
        await adapter.send_step(
            ACCOUNT, target, "text", text="Grüße", before_dispatch=marker
        )
    assert exc.value.code == "outcome_unknown"
    marker.assert_awaited_once()
    composer.button.click.assert_awaited_once()
    assert not page.handlers


async def test_changed_target_never_crosses_dispatch_boundary(adapter):
    composer, _, target = setup_send(adapter)
    adapter._prepare.return_value = {**target, "conversation_id": "other"}
    marker = AsyncMock()
    with pytest.raises(MessagingError) as exc:
        await adapter.send_step(
            ACCOUNT, target, "text", text="Hallo", before_dispatch=marker
        )
    assert exc.value.code == "target_mismatch"
    marker.assert_not_awaited()
    composer.button.click.assert_not_awaited()


async def test_successful_send_marks_before_click(adapter):
    composer, _, target = setup_send(adapter)
    events = []

    async def marker():
        events.append("persist")

    async def click():
        events.append("click")

    composer.button.click.side_effect = click
    adapter._receipt = AsyncMock(
        return_value={
            "message_id": "new",
            "conversation_id": "conversation-1",
            "confirmed": True,
        }
    )
    receipt = await adapter.send_step(
        ACCOUNT, target, "text", text="Grüße", before_dispatch=marker
    )
    assert events == ["persist", "click"]
    assert receipt["confirmed"]


def test_pagination_anchor_and_expiration():
    items = [{"message_id": "m1"}, {"message_id": "m2"}, {"message_id": "m3"}]
    assert _page_slice(items, None, 2, "message_id") == (items[:2], "after:m2", True)
    assert _page_slice(items, "after:m2", 2, "message_id") == (items[2:], None, False)
    with pytest.raises(MessagingError) as exc:
        _page_slice(items, "after:missing", 2, "message_id")
    assert exc.value.code == "cursor_expired"


@pytest.mark.parametrize("value", ["../other", "/tmp/account", "a/b", "a\\b"])
async def test_profile_paths_cannot_escape_account_root(adapter, value):
    with pytest.raises(MessagingError):
        await adapter._context({"account_id": value})


async def test_profiles_are_distinct_and_login_reuses_visible_native_browser(adapter):
    contexts = []

    async def launch(path, **kwargs):
        context = SimpleNamespace(
            close=AsyncMock(),
            profile=Path(path),
            headless=kwargs["headless"],
            options=kwargs,
        )
        contexts.append(context)
        return context

    adapter._runtime = SimpleNamespace(
        chromium=SimpleNamespace(launch_persistent_context=launch)
    )
    a = await adapter._context(ACCOUNT)
    b = await adapter._context({"account_id": "account-b"})
    assert a.profile != b.profile
    assert not a.headless and not b.headless
    reopened = await adapter._context(ACCOUNT, headed=True)
    a.close.assert_not_awaited()
    b.close.assert_not_awaited()
    assert reopened is a
    assert len(contexts) == 2
    assert a.options["channel"] == b.options["channel"] == "chrome"
    assert "user_agent" not in a.options
    assert "args" not in a.options
    assert a.profile.stat().st_mode & 0o777 == 0o700


async def test_login_routing_allows_only_observed_auth_navigation(monkeypatch):
    routes = {}

    async def register(pattern, handler):
        routes[pattern] = handler

    page = SimpleNamespace(route=register, route_web_socket=AsyncMock())
    monkeypatch.setattr(browser, "validate_public_https_url", AsyncMock())
    await browser.install_messaging_page_routing(page)
    route = SimpleNamespace(abort=AsyncMock(), continue_=AsyncMock())
    request = SimpleNamespace(
        url="https://login.kleinanzeigen.de/u/login/identifier",
        is_navigation_request=lambda: True,
    )
    await routes["**/*"](route, request)
    route.continue_.assert_awaited_once()
    route.abort.assert_not_awaited()
    request.url = "https://evil.kleinanzeigen.de/"
    await routes["**/*"](route, request)
    route.abort.assert_awaited_once()
    assert route.continue_.await_count == 1


async def test_login_routing_rejects_private_dns_and_arbitrary_auth0_paths(monkeypatch):
    routes = {}

    async def register(pattern, handler):
        routes[pattern] = handler

    await browser.install_messaging_page_routing(
        SimpleNamespace(route=register, route_web_socket=AsyncMock())
    )
    route = SimpleNamespace(abort=AsyncMock(), continue_=AsyncMock())
    monkeypatch.setattr(
        browser,
        "validate_public_https_url",
        AsyncMock(side_effect=ValueError("unsafe DNS")),
    )
    request = SimpleNamespace(
        url="https://cdn.auth0.com/ulp/css.css", is_navigation_request=lambda: False
    )
    await routes["**/*"](route, request)
    request.url = "https://cdn.auth0.com/other-resource"
    await routes["**/*"](route, request)
    assert route.abort.await_count == 2
    route.continue_.assert_not_awaited()


async def test_missing_browser_is_sanitized_without_automatic_fallback(adapter):
    launch = AsyncMock(side_effect=browser.PlaywrightError("private launch details"))
    adapter._runtime = SimpleNamespace(
        chromium=SimpleNamespace(launch_persistent_context=launch)
    )
    with pytest.raises(MessagingError) as exc:
        await adapter._context(ACCOUNT)
    assert exc.value.code == "browser_launch_failed"
    assert exc.value.details == {"browser_channel": "chrome"}
    assert "private" not in str(exc.value)
    launch.assert_awaited_once()
    assert not adapter._contexts


@pytest.mark.parametrize("channel", ["chrome", "chromium", "safari"])
def test_messaging_browser_channel_is_explicit(monkeypatch, channel):
    from messaging.contracts import MessagingSettings

    monkeypatch.setenv("KLEINANZEIGEN_MESSAGING_BROWSER_CHANNEL", channel)
    assert MessagingSettings.from_env().browser_channel == channel


def test_messaging_browser_rejects_unknown_channel(monkeypatch):
    from messaging.contracts import MessagingSettings

    monkeypatch.setenv("KLEINANZEIGEN_MESSAGING_BROWSER_CHANNEL", "unknown")
    with pytest.raises(ValueError, match="BROWSER_CHANNEL"):
        MessagingSettings.from_env()


async def test_identity_waits_for_hydrated_current_user(adapter):
    adapter.dom_timeout_seconds = 0.5
    page = Page(None)
    page.evaluate = AsyncMock(
        side_effect=[
            {"ids": [], "login_form": False},
            {"ids": ["101"], "login_form": False},
        ]
    )
    assert (await adapter._identity(page, ACCOUNT))["user_id"] == "101"
    assert page.evaluate.await_count == 2


async def test_inbox_waits_for_hydration_but_never_invents_empty_result(adapter):
    adapter.dom_timeout_seconds = 0.5
    page = Page(None)
    empty_loading = {"conversations": [], "empty": False}
    loaded = {
        "conversations": [{"conversation_id": "c1"}],
        "empty": False,
        "more": False,
    }
    page.evaluate = AsyncMock(side_effect=[empty_loading, loaded])
    assert await adapter._inbox(page) == loaded


async def test_conversation_waits_until_selected_target_replaces_previous_view(adapter):
    adapter.dom_timeout_seconds = 0.5
    page = Page(None)
    previous = {
        "conversation_id": "old",
        "messages": [],
        "empty": True,
        "recipients": ["202"],
    }
    selected = {**previous, "conversation_id": "conversation-1"}
    page.evaluate = AsyncMock(side_effect=[previous, selected])
    assert (await adapter._conversation(page, ACCOUNT, "conversation-1"))[
        "recipient_user_id"
    ] == "202"


async def test_open_conversation_loads_later_page_before_selecting(adapter):
    first = {
        "conversations": [{"conversation_id": "old"}],
        "more": True,
        "known_container": True,
    }
    last = {
        "conversations": [{"conversation_id": "target"}],
        "more": False,
        "known_container": True,
    }
    adapter._inbox = AsyncMock(return_value=first)
    adapter._load_more = AsyncMock(return_value=last)
    row = SimpleNamespace(
        get_attribute=AsyncMock(return_value="target"), click=AsyncMock()
    )
    rows = SimpleNamespace(count=AsyncMock(return_value=1), nth=lambda _: row)
    page = SimpleNamespace(locator=lambda _: rows)
    await adapter._open_conversation(page, "target")
    adapter._load_more.assert_awaited_once()
    row.click.assert_awaited_once()


async def test_open_conversation_reports_truncation_instead_of_false_not_found(adapter):
    adapter._inbox = AsyncMock(
        return_value={
            "conversations": [],
            "more": False,
            "known_container": True,
            "potentially_truncated": True,
        }
    )
    with pytest.raises(MessagingError) as exc:
        await adapter._open_conversation(None, "target")
    assert exc.value.code == "ui_unsupported"


async def test_open_conversation_stops_at_bounded_page_count(adapter):
    adapter._inbox = AsyncMock(
        return_value={"conversations": [], "more": True, "known_container": True}
    )
    adapter._load_more = AsyncMock(
        return_value={"conversations": [], "more": True, "known_container": True}
    )
    with pytest.raises(MessagingError) as exc:
        await adapter._open_conversation(None, "target")
    assert exc.value.code == "ui_unsupported"
    assert adapter._load_more.await_count == 20


async def test_follow_links_must_be_observed_canonical_and_unambiguous(adapter):
    page = Page(
        [
            {
                "url": "https://www.kleinanzeigen.de/m-nachrichten.html?conversationId=c1",
                "message_id": "m1",
            }
        ]
    )
    assert (await adapter._observed_follow_target(page))["conversation_id"] == "c1"
    page.value.append(
        {
            "url": "https://www.kleinanzeigen.de/m-nachrichten.html?conversationId=c2",
            "message_id": "m2",
        }
    )
    assert await adapter._observed_follow_target(page) is None
    page.value = [{"url": "https://evil.test/m-nachrichten.html?conversationId=c1"}]
    assert await adapter._observed_follow_target(page) is None


async def test_first_contact_follow_cannot_confirm_an_old_identical_message(adapter):
    message = {
        "message_id": "old",
        "sender_user_id": "101",
        "text": "Hallo",
        "created_at": "2026-11-04T10:00:00Z",
        "images": [],
    }
    adapter._conversation = AsyncMock(
        return_value={
            "conversation_id": "conversation-1",
            "recipient_user_id": "202",
            "messages": [message],
        }
    )
    target = {**TARGET, "_not_before": "2026-11-04T11:00:00+00:00"}
    assert (
        await adapter._receipt(None, ACCOUNT, target, set(), "text", "Hallo", set())
        is None
    )
    message["created_at"] = "2026-11-04T11:00:01Z"
    assert (
        await adapter._receipt(None, ACCOUNT, target, set(), "text", "Hallo", set())
    )["message_id"] == "old"
    message["created_at"] = None
    assert (
        await adapter._receipt(None, ACCOUNT, target, set(), "text", "Hallo", set())
        is None
    )
    target["_expected_message_id"] = "old"
    assert (
        await adapter._receipt(None, ACCOUNT, target, set(), "text", "Hallo", set())
    )["message_id"] == "old"


async def test_first_contact_follows_observed_link_as_separate_read_without_resend(
    adapter,
):
    composer, page, _ = setup_send(adapter)
    target = {
        "target_kind": "listing",
        "target_id": "123456789",
        "listing_id": "123456789",
        "recipient_user_id": "202",
        "account_user_id": "101",
        "capabilities": {"can_send_text": True, "max_text_length": 20000},
    }
    adapter._prepare.return_value = target
    observed = {
        "url": "https://www.kleinanzeigen.de/m-nachrichten.html?conversationId=new-chat",
        "conversation_id": "new-chat",
        "message_id": "new-message",
    }
    adapter._observed_follow_target = AsyncMock(side_effect=[None, observed])
    adapter._identity = AsyncMock(return_value={"user_id": "101"})
    adapter._receipt = AsyncMock(
        return_value={
            "message_id": "new-message",
            "conversation_id": "new-chat",
            "recipient_user_id": "202",
            "confirmed": True,
        }
    )
    navigation_urls = []

    async def navigate(account, url, operation, **kwargs):
        navigation_urls.append(url)
        return await operation(page, None)

    adapter._navigate = navigate
    marker = AsyncMock()
    result = await adapter.send_step(
        ACCOUNT, target, "text", text="Hallo", before_dispatch=marker
    )
    assert result["confirmed"]
    assert navigation_urls == [
        "https://www.kleinanzeigen.de/s-anzeige/123456789",
        observed["url"],
    ]
    marker.assert_awaited_once()
    composer.button.click.assert_awaited_once()
    assert adapter._receipt.call_args.args[2]["_expected_message_id"] == "new-message"


async def test_staged_image_is_hashed_then_uploaded_from_immutable_bytes(
    adapter, tmp_path
):
    data = b"image-bytes"
    path = tmp_path / "staged.png"
    path.write_bytes(data)
    record = {
        "path": str(path),
        "mime_type": "image/png",
        "sha256": hashlib.sha256(data).hexdigest(),
        "attachment_id": "img_1",
    }
    payload = await adapter._image_payload(record)
    path.write_bytes(b"changed-after-reading")
    assert payload == {"name": "img_1.png", "mimeType": "image/png", "buffer": data}
    with pytest.raises(MessagingError) as exc:
        await adapter._image_payload(record)
    assert exc.value.code == "image_changed"


async def test_staged_image_size_is_bounded_while_reading(
    adapter, tmp_path, monkeypatch
):
    monkeypatch.setattr(browser, "MAX_IMAGE_BYTES", 4)
    path = tmp_path / "too-large.png"
    path.write_bytes(b"12345")
    with pytest.raises(MessagingError) as exc:
        await adapter._image_payload(
            {
                "path": str(path),
                "mime_type": "image/png",
                "attachment_id": "i",
                "sha256": "ignored",
            }
        )
    assert exc.value.code == "invalid_image"


@pytest.mark.parametrize(
    "draft,kind,text",
    [
        ({"texts": ["Privater alter Entwurf"]}, "text", "Beauftragter Text"),
        ({"texts": ["Privater alter Entwurf"]}, "image", None),
        ({"attachment_ids": ["other-image"]}, "text", "Hallo"),
        ({"filenames": ["private-photo.png"]}, "image", None),
        ({"unknown_previews": True}, "text", "Hallo"),
    ],
)
async def test_unrequested_drafts_block_dispatch(adapter, draft, kind, text):
    composer, _, target = setup_send(adapter)
    composer.evaluate.return_value = draft
    marker = AsyncMock()
    with pytest.raises(MessagingError) as exc:
        await adapter.send_step(
            ACCOUNT, target, kind, text=text, before_dispatch=marker
        )
    assert exc.value.code == "draft_conflict"
    marker.assert_not_awaited()
    composer.button.click.assert_not_awaited()


async def test_image_composer_allows_only_current_staged_attachment(adapter):
    composer = Composer()
    composer.evaluate.return_value = {
        "texts": [],
        "attachment_ids": ["upload-1"],
        "filenames": ["img_1.png"],
    }
    await adapter._assert_composer_clean(
        composer, "image", attachment_ids={"upload-1"}, filename="img_1.png"
    )
    composer.evaluate.return_value["texts"] = ["Unexpected text"]
    with pytest.raises(MessagingError) as exc:
        await adapter._assert_composer_clean(
            composer, "image", attachment_ids={"upload-1"}, filename="img_1.png"
        )
    assert exc.value.code == "draft_conflict"


@pytest.mark.parametrize(
    "auto_send,expected",
    [(None, None), ("true", "auto_send"), ("false", "explicit_submit")],
)
async def test_image_mode_requires_explicit_ui_contract(adapter, auto_send, expected):
    async def file_attribute(name):
        return auto_send if name == "data-auto-send" else "image/png"

    files = SimpleNamespace(
        count=AsyncMock(return_value=1),
        first=SimpleNamespace(get_attribute=file_attribute),
    )
    editors = SimpleNamespace(
        count=AsyncMock(return_value=1),
        first=SimpleNamespace(get_attribute=AsyncMock(return_value=None)),
    )
    buttons = SimpleNamespace(count=AsyncMock(return_value=1))
    composer = SimpleNamespace(
        locator=lambda selector: files if selector == 'input[type="file"]' else editors,
        get_by_role=lambda *args, **kwargs: buttons,
    )
    adapter._composer = AsyncMock(return_value=composer)
    caps = await adapter._capabilities(None)
    assert caps["image_send_mode"] == expected
    assert caps["can_send_images"] == (expected is not None)


async def test_listing_verification_accepts_only_matching_numeric_core(adapter):
    page = Page("123456789")
    page.url = "https://www.kleinanzeigen.de/s-anzeige/example/123456789-10-20"
    await adapter._verify_listing(page, "123456789")
    await adapter._verify_listing(page, "123456789-10-20")
    page.url = "https://www.kleinanzeigen.de/s-anzeige/other/987654321-10-20"
    with pytest.raises(MessagingError) as exc:
        await adapter._verify_listing(page, "123456789")
    assert exc.value.code == "listing_mismatch"
    page.url = "https://www.kleinanzeigen.de/s-anzeige/example/123456789-10-20"
    page.value = "987654321"
    with pytest.raises(MessagingError) as exc:
        await adapter._verify_listing(page, "123456789")
    assert exc.value.code == "listing_mismatch"


async def test_inline_first_contact_receipt_also_requires_fresh_evidence(adapter):
    composer, _, _ = setup_send(adapter)
    target = {
        "target_kind": "listing",
        "target_id": "123456789",
        "listing_id": "123456789",
        "recipient_user_id": "202",
        "account_user_id": "101",
        "capabilities": {"can_send_text": True, "max_text_length": 20000},
    }
    adapter._prepare.return_value = target
    adapter._receipt = AsyncMock(
        return_value={"message_id": "new", "conversation_id": "c", "confirmed": True}
    )
    await adapter.send_step(
        ACCOUNT, target, "text", text="Hallo", before_dispatch=AsyncMock()
    )
    assert adapter._receipt.call_args.args[2]["_not_before"]
    composer.button.click.assert_awaited_once()


@pytest.mark.parametrize("captcha", [False, True])
async def test_manual_login_detects_loaded_block_and_trips_shared_circuit_without_retry(
    adapter, captcha
):
    controller = OutboundController(OutboundSettings(cooldown_seconds=1))
    adapter.controller = controller
    page = Page({"ids": [], "login_form": False})
    page.url = "https://login.kleinanzeigen.de/u/login/identifier"
    page.close = AsyncMock()
    page.title = AsyncMock(return_value="Kleinanzeigen")
    page.query_selector = AsyncMock(
        side_effect=lambda selector: (
            SimpleNamespace(is_visible=AsyncMock(return_value=True))
            if captcha and selector == "#challenge-form"
            else None
        )
    )
    page.locator = lambda selector: SimpleNamespace(
        inner_text=AsyncMock(
            return_value="" if captcha else "Dieser IP-Bereich\n wurde gesperrt."
        )
    )
    context = SimpleNamespace(close=AsyncMock())
    adapter._contexts[ACCOUNT["account_id"]] = context
    adapter._headed[ACCOUNT["account_id"]] = True
    adapter._navigate = AsyncMock(return_value=page)
    adapter._identity = AsyncMock()
    try:
        with pytest.raises(MessagingError) as exc:
            await adapter.connect(ACCOUNT)
        assert exc.value.code == "upstream_blocked"
        assert exc.value.details["retry_after_seconds"] >= 1
        assert exc.value.details["browser_window_open"] is True
        assert exc.value.details["identity_present"] is False
        assert exc.value.details["path"] == "/u/login/identifier"
        assert set(exc.value.details) == {
            "retry_after_seconds",
            "block_indicator",
            "browser_window_open",
            "identity_present",
            "path",
        }
        adapter._navigate.assert_awaited_once()
        adapter._identity.assert_not_awaited()
        page.close.assert_not_awaited()
        context.close.assert_not_awaited()
        assert adapter._login_pages[ACCOUNT["account_id"]] is page
        assert adapter._contexts[ACCOUNT["account_id"]] is context
        status = await controller.status()
        assert status["circuit"]["state"] == "open"
        assert status["circuit"]["reason"] == "block_page"
        assert status["metrics"]["block_pages"] == 1
        assert status["active"] == 0
    finally:
        await adapter.close()
        await controller.close()


async def _capture_messaging_routes(on_blocked=None):
    captured = {}

    async def route(pattern, handler):
        captured["http"] = handler

    async def route_socket(pattern, handler):
        captured["websocket"] = handler

    await browser.install_messaging_page_routing(
        SimpleNamespace(route=route, route_web_socket=route_socket),
        on_blocked=on_blocked,
    )
    return captured


@pytest.mark.parametrize(
    "url",
    [
        "wss://www.kleinanzeigen.de/messagebox/socket",
        "wss://gateway.kleinanzeigen.de:443/messagebox/socket?opaque=not-recorded",
    ],
)
async def test_websocket_guard_preserves_allowed_secure_connection(monkeypatch, url):
    resolver = AsyncMock(return_value=("8.8.8.8",))
    monkeypatch.setattr(url_security, "resolve_host_addresses", resolver)
    callback = Mock()
    routes = await _capture_messaging_routes(callback)
    socket = SimpleNamespace(url=url, close=AsyncMock(), connect_to_server=Mock())
    await routes["websocket"](socket)
    socket.connect_to_server.assert_called_once_with()
    socket.close.assert_not_awaited()
    callback.assert_not_called()
    assert resolver.call_args.args[1] == 443


@pytest.mark.parametrize(
    "url",
    [
        "ws://gateway.kleinanzeigen.de/messagebox/socket",
        "wss://127.0.0.1/socket",
        "wss://[::1]/socket",
        "wss://evil.test/socket",
        "wss://kleinanzeigen.de.evil.test/socket",
        "wss://user:password@www.kleinanzeigen.de/socket",
        "wss://www.kleinanzeigen.de:8443/socket",
        "wss://www.kleinanzeigen.de/socket#fragment",
        "wss://www.kleinanzeigen.de/ambiguous\\path",
        "wss://www.kleinanzeigen.de/line\nbreak",
    ],
)
async def test_websocket_guard_rejects_unsafe_scheme_authority_and_url(
    monkeypatch, url
):
    resolver = AsyncMock(return_value=("8.8.8.8",))
    monkeypatch.setattr(url_security, "resolve_host_addresses", resolver)
    routes = await _capture_messaging_routes()
    socket = SimpleNamespace(url=url, close=AsyncMock(), connect_to_server=Mock())
    await routes["websocket"](socket)
    socket.connect_to_server.assert_not_called()
    socket.close.assert_awaited_once()
    resolver.assert_not_awaited()


@pytest.mark.parametrize(
    "addresses",
    [
        ("127.0.0.1",),
        ("10.0.0.1",),
        ("169.254.169.254",),
        ("::1",),
        ("fd00::1",),
        ("8.8.8.8", "192.168.1.1"),
        (),
    ],
)
async def test_websocket_guard_rejects_nonpublic_or_mixed_dns(monkeypatch, addresses):
    monkeypatch.setattr(
        url_security, "resolve_host_addresses", AsyncMock(return_value=addresses)
    )
    routes = await _capture_messaging_routes()
    socket = SimpleNamespace(
        url="wss://gateway.kleinanzeigen.de/socket",
        close=AsyncMock(),
        connect_to_server=Mock(),
    )
    await routes["websocket"](socket)
    socket.connect_to_server.assert_not_called()
    socket.close.assert_awaited_once()


async def test_routing_diagnostics_never_include_query_path_or_credentials(monkeypatch):
    callback = Mock()
    routes = await _capture_messaging_routes(callback)
    route = SimpleNamespace(abort=AsyncMock(), continue_=AsyncMock())
    request = SimpleNamespace(
        url="https://user:private-password@external.test/private-path?token=private-token",
        is_navigation_request=lambda: True,
        resource_type="document",
    )
    await routes["http"](route, request)
    socket = SimpleNamespace(
        url="wss://external.test/private-path?token=private-token",
        close=AsyncMock(),
        connect_to_server=Mock(),
    )
    await routes["websocket"](socket)
    assert callback.call_args_list == [
        (("external.test", "document"),),
        (("external.test", "websocket"),),
    ]
    assert "private" not in repr(callback.call_args_list)
    route.abort.assert_awaited_once()
    socket.close.assert_awaited_once()


async def test_routing_diagnostic_callback_is_bounded_and_cannot_disable_guard():
    calls = []

    def callback(host, kind):
        calls.append((host, kind))
        raise RuntimeError("diagnostic sink unavailable")

    routes = await _capture_messaging_routes(callback)
    route = SimpleNamespace(abort=AsyncMock(), continue_=AsyncMock())
    request = SimpleNamespace(
        url="https://blocked.test/path",
        is_navigation_request=lambda: True,
        resource_type="document",
    )
    for _ in range(105):
        await routes["http"](route, request)
    for number in range(40):
        request.url = f"https://other-{number}.test/path"
        await routes["http"](route, request)
    assert len(calls) == 131  # 100 repeated + 31 additional host/type pairs
    assert route.abort.await_count == 145
    route.continue_.assert_not_awaited()


@pytest.mark.parametrize("failure", ["upstream", "cancelled", "setup"])
async def test_initial_login_navigation_failure_closes_page_and_account_context(
    adapter, failure
):
    page = SimpleNamespace(close=AsyncMock())
    context = SimpleNamespace(close=AsyncMock())
    adapter._contexts[ACCOUNT["account_id"]] = context
    adapter._headed[ACCOUNT["account_id"]] = True
    adapter._new_page = AsyncMock(return_value=page)
    adapter._identity = AsyncMock()

    async def navigate(factory, url, operation, *, cleanup, **kwargs):
        allocated = await factory()
        try:
            if failure == "cancelled":
                raise asyncio.CancelledError()
            if failure == "upstream":
                raise UpstreamBlocked("blocked", retry_after=1)
            raise RuntimeError("initial navigation failed")
        finally:
            await cleanup(allocated)

    adapter.controller.run_navigation = AsyncMock(side_effect=navigate)
    expected = (
        asyncio.CancelledError
        if failure == "cancelled"
        else MessagingError
        if failure == "upstream"
        else RuntimeError
    )
    with pytest.raises(expected) as exc:
        await adapter.connect(ACCOUNT)
    if failure == "upstream":
        assert exc.value.code == "upstream_blocked"
    page.close.assert_awaited_once()
    context.close.assert_awaited_once()
    adapter._identity.assert_not_awaited()
    adapter._new_page.assert_awaited_once()
    assert ACCOUNT["account_id"] not in adapter._contexts
    assert ACCOUNT["account_id"] not in adapter._headed


async def test_retained_navigation_page_closes_when_setup_operation_fails(adapter):
    page = SimpleNamespace(close=AsyncMock())
    adapter._new_page = AsyncMock(return_value=page)

    async def navigate(factory, url, operation, *, cleanup, **kwargs):
        allocated = await factory()
        try:
            return await operation(allocated, None)
        finally:
            await cleanup(allocated)

    adapter.controller.run_navigation = AsyncMock(side_effect=navigate)
    operation = AsyncMock(side_effect=RuntimeError("setup failed"))
    with pytest.raises(RuntimeError):
        await adapter._navigate(ACCOUNT, browser.LOGIN_URL, operation, keep_page=True)
    page.close.assert_awaited_once()


async def test_login_cleanup_failure_does_not_hide_upstream_block(adapter):
    context = SimpleNamespace(
        close=AsyncMock(side_effect=RuntimeError("driver unavailable"))
    )
    adapter._contexts[ACCOUNT["account_id"]] = context
    adapter._headed[ACCOUNT["account_id"]] = True
    adapter._navigate = AsyncMock(side_effect=UpstreamBlocked("blocked", retry_after=1))
    with pytest.raises(MessagingError) as exc:
        await adapter.connect(ACCOUNT)
    assert exc.value.code == "upstream_blocked"
    context.close.assert_awaited_once()
    assert ACCOUNT["account_id"] not in adapter._contexts


@pytest.fixture
def login_page(adapter):
    page = Page({"ids": ["101"], "login_form": False})
    page.close = AsyncMock()
    context = SimpleNamespace(close=AsyncMock(), clear_cookies=AsyncMock())
    adapter._contexts[ACCOUNT["account_id"]] = context
    adapter._headed[ACCOUNT["account_id"]] = True
    adapter._navigate = AsyncMock(return_value=page)
    adapter._capabilities = AsyncMock(
        return_value={"text_status": "unverified", "images_status": "unverified"}
    )
    return page, context


async def test_chrome_login_reports_only_capabilities_of_loaded_page(
    adapter, login_page
):
    page, _context = login_page
    caps = {
        "scope": "current_page",
        "text_status": "supported",
        "can_send_text": True,
        "images_status": "unverified",
        "can_send_images": False,
    }
    adapter._capabilities.return_value = caps
    result = await adapter.connect(ACCOUNT)
    assert result["capabilities"] == caps
    adapter._capabilities.assert_awaited_once_with(page)


async def test_successful_login_keeps_native_context_and_reuses_page(
    adapter, login_page
):
    page, context = login_page
    results = await asyncio.gather(adapter.connect(ACCOUNT), adapter.connect(ACCOUNT))
    assert [result["user_id"] for result in results] == ["101", "101"]
    adapter._navigate.assert_awaited_once()
    page.close.assert_not_awaited()
    context.close.assert_not_awaited()
    assert adapter._login_pages == {ACCOUNT["account_id"]: page}
    assert adapter._contexts[ACCOUNT["account_id"]] is context


async def test_explicit_reconnect_after_block_only_rechecks_loaded_page(
    adapter, login_page
):
    page, context = login_page
    blocked = UpstreamBlocked("blocked", retry_after=12)
    blocked.block_indicator = "body:ip-bereich wurde gesperrt"
    adapter._inspect = AsyncMock(side_effect=[blocked, blocked, None])
    page.url = (
        "https://www.kleinanzeigen.de/m-meine-anzeigen.html?session=not-returned#secret"
    )
    for _ in range(2):
        with pytest.raises(MessagingError) as caught:
            await adapter.connect(ACCOUNT)
        assert caught.value.code == "upstream_blocked"
        assert caught.value.details["path"] == "/m-meine-anzeigen.html"
        assert "session" not in str(caught.value.details)
    assert (await adapter.connect(ACCOUNT))["user_id"] == "101"
    adapter._navigate.assert_awaited_once()
    assert adapter._inspect.await_count == 3
    page.close.assert_not_awaited()
    context.close.assert_not_awaited()


async def test_login_block_reports_only_identity_presence_not_identity_value(
    adapter, login_page
):
    page, _ = login_page
    page.evaluate = AsyncMock(return_value=True)
    adapter._inspect = AsyncMock(side_effect=UpstreamBlocked("blocked", retry_after=1))
    with pytest.raises(MessagingError) as caught:
        await adapter.connect(ACCOUNT)
    assert caught.value.details["identity_present"] is True
    assert "101" not in str(caught.value.details)


@pytest.mark.parametrize(
    "url",
    [
        "https://unrelated.example/private?secret=value",
        "https://www.kleinanzeigen.de.attacker.example/",
        "https://cdn.kleinanzeigen.de/",
        "http://www.kleinanzeigen.de/",
        "https://user:password@www.kleinanzeigen.de/",
        "https://www.kleinanzeigen.de:444/",
        "file:///private/example",
    ],
)
async def test_retained_login_rejects_foreign_origin_before_any_dom_read(
    adapter, login_page, url
):
    page, context = login_page
    page.url = url
    page.evaluate = AsyncMock()
    adapter._login_pages[ACCOUNT["account_id"]] = page
    adapter._identity = AsyncMock()
    with pytest.raises(MessagingError) as caught:
        await adapter.connect(ACCOUNT)
    assert caught.value.code == "unsafe_login_page"
    adapter._navigate.assert_not_awaited()
    adapter._identity.assert_not_awaited()
    adapter.controller.inspect_interaction_response.assert_not_awaited()
    page.evaluate.assert_not_awaited()
    context.close.assert_not_awaited()


async def test_login_timeout_keeps_page_for_later_manual_completion(
    adapter, login_page
):
    page, context = login_page
    adapter.login_timeout_seconds = 0.01
    page.value = {"ids": [], "login_form": True}
    with pytest.raises(MessagingError) as caught:
        await adapter.connect(ACCOUNT)
    assert caught.value.code == "reauth_required"
    assert caught.value.details["browser_window_open"] is True
    assert adapter._login_pages[ACCOUNT["account_id"]] is page
    context.close.assert_not_awaited()
    page.value = {"ids": ["101"], "login_form": False}
    assert (await adapter.connect(ACCOUNT))["user_id"] == "101"
    adapter._navigate.assert_awaited_once()


async def test_cancelled_login_after_initial_navigation_closes_context(
    adapter, login_page
):
    page, context = login_page
    adapter._inspect = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await adapter.connect(ACCOUNT)
    page.close.assert_awaited_once()
    context.close.assert_awaited_once()
    assert not adapter._contexts
    assert not adapter._headed
    assert not adapter._login_pages


@pytest.mark.parametrize("cleanup", ["disconnect", "close"])
async def test_explicit_login_cleanup_removes_retained_page(
    adapter, login_page, cleanup
):
    _, context = login_page
    await adapter.connect(ACCOUNT)
    if cleanup == "disconnect":
        await adapter.disconnect(ACCOUNT)
        context.clear_cookies.assert_awaited_once()
    else:
        await adapter.close()
    context.close.assert_awaited_once()
    assert not adapter._contexts
    assert not adapter._headed
    assert not adapter._login_pages


async def test_manually_closed_login_page_is_replaced_only_on_explicit_connect(
    adapter, login_page
):
    old_page, old_context = login_page
    adapter._login_pages[ACCOUNT["account_id"]] = old_page
    old_page.is_closed.return_value = True
    replacement = Page({"ids": ["101"], "login_form": False})
    adapter._navigate.return_value = replacement
    assert (await adapter.connect(ACCOUNT))["user_id"] == "101"
    adapter._navigate.assert_awaited_once()
    old_context.close.assert_awaited_once()
    assert adapter._login_pages[ACCOUNT["account_id"]] is replacement


async def test_retained_login_pages_remain_separate_per_account(adapter, login_page):
    page_a, context_a = login_page
    await adapter.connect(ACCOUNT)
    account_b = {"account_id": "account-b", "user_id": "202"}
    page_b = Page({"ids": ["202"], "login_form": False})
    adapter._navigate.return_value = page_b
    assert (await adapter.connect(account_b))["user_id"] == "202"
    assert (await adapter.connect(ACCOUNT))["user_id"] == "101"
    assert adapter._login_pages == {"account-a": page_a, "account-b": page_b}
    assert adapter._navigate.await_count == 2
    assert adapter._contexts["account-a"] is context_a
